"""租户级仓储基座：把 `tenant_id` 与数据范围强制过滤收敛到一处。

规则（`docs/design-briefs/M1-tenant-identity.md` §3.1 + `M1-E2-data-scope.md` §2-A）：
- 读写都必须经过本基座，禁止直接用裸 session 操作租户表（守卫见
  `tests/test_bare_sql_scope_guard_unit.py`）；
- 构造时**必须**声明数据范围：`DataScopeContext`（按范围过滤）或 `ScopeBypass(reason=…)`
  （本人自助路径，理由必填）。没有"忘了配范围"这一档：缺范围=空集，绕过=显式声明；
- `add` 时对象未带 `tenant_id` 会按当前上下文回填（调用方通常如此）；显式带了不同租户则报错；
- `delete` 前同样校验归属（父子同租由数据库复合外键兜底）；
- 查询一律追加 `tenant_id == ctx` + 范围谓词，配合 RLS 形成第二、三道防线。
"""

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import Select, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.rbac.scopes import DataScopeContext, scope_predicate


@dataclass(frozen=True, slots=True)
class ScopeBypass:
    """显式声明「这条路径不按数据范围过滤」。

    只允许用在**本身已按 user_id 精确锁定本人**的自助路径（如会话设备管理）。
    理由必填：空理由的绕过与忘记配范围无法区分，所以直接拒绝构造。
    """

    reason: str

    def __post_init__(self) -> None:
        if not self.reason or not self.reason.strip():
            raise ValueError("ScopeBypass 必须写明绕过数据范围过滤的理由")


ScopeDecision = DataScopeContext | ScopeBypass


class TenantScopedModel(Protocol):
    """租户级模型的最小结构约束（必须带 tenant_id 列）。"""

    tenant_id: Any
    id: Any
    created_at: Any


def encode_cursor(created_at: datetime, object_id: UUID) -> str:
    """把分页游标编码为不透明字符串（base64url(JSON)）。"""

    payload = json.dumps({"t": created_at.isoformat(), "id": str(object_id)}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """解码分页游标。

    异常:
        CursorError: 游标不是合法的 base64/JSON/字段类型（由路由层映射 400，
            绝不能当成「第一页」静默吞掉——那会让翻页悄悄回到开头）。
    """

    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        created_at = datetime.fromisoformat(payload["t"])
        object_id = UUID(str(payload["id"]))
    except Exception as exc:
        raise CursorError(f"无法解码分页游标：{cursor[:32]}…") from exc
    return created_at, object_id


class CursorError(ValueError):
    """分页游标无法解码（HTTP 400 语义；与仓储配置类 ValueError 区分）。"""


class CrossTenantAccessError(RuntimeError):
    """试图访问/写入非当前租户的数据。"""


class TenantScopedRepository[ModelT: TenantScopedModel]:
    """租户级仓储：所有访问都带租户边界与数据范围。"""

    def __init__(
        self,
        session: AsyncSession,
        model: type[ModelT],
        tenant_id: UUID,
        *,
        scope: ScopeDecision,
        owner_attr: str = "user_id",
        class_scope_factory: Callable[[frozenset[UUID]], Select[Any]] | None = None,
    ) -> None:
        """绑定会话、模型、租户与数据范围上下文。

        参数:
            session: 处于事务中的会话（应已 `set_config('app.tenant_id', ...)`）。
            model: 目标模型（必须带 tenant_id 列）。
            tenant_id: 当前租户。
            scope: 数据范围判据，或显式的 `ScopeBypass(reason=…)`。
            owner_attr: `self` 范围对应的归属列名（默认 `user_id`；主键即归属的表传 `id`）。
            class_scope_factory: class 范围的成员子查询工厂——输入当前主体可见的
                班级 id 集合，返回「归属列可见值」的子查询（例如学生表中这些班级的
                user_id 集合）。为 None 时 class 范围对该模型不贡献可见性
                （fail-closed，绝不放大成全校）。

        异常:
            ValueError: 模型缺少 tenant_id（用错了仓储），
                或范围需要归属列而模型上没有（宁可报错也不放行整表）。
        """

        if not hasattr(model, "tenant_id"):
            raise ValueError(f"{model.__name__} 不是租户级模型，禁止使用 TenantScopedRepository")
        if isinstance(scope, DataScopeContext) and not (
            scope.sees_whole_school or scope.denies_everything
        ) and not hasattr(model, owner_attr):
            raise ValueError(
                f"{model.__name__} 缺少归属列 {owner_attr}，无法按 self 范围过滤；"
                "请显式声明归属列（owner_attr）"
            )
        self._session = session
        self._model = model
        self._tenant_id = tenant_id
        self._scope = scope
        self._owner_attr = owner_attr
        self._class_scope_factory = class_scope_factory

    @property
    def tenant_id(self) -> UUID:
        """当前租户。"""

        return self._tenant_id

    @property
    def scope(self) -> ScopeDecision:
        """本仓储使用的范围判据（测试与审计需要读到它）。"""

        return self._scope

    def scoped(self, stmt: Select[Any]) -> Select[Any]:
        """给查询追加租户过滤与数据范围谓词。"""

        stmt = stmt.where(self._model.tenant_id == self._tenant_id)
        if isinstance(self._scope, DataScopeContext):
            class_member_subquery = (
                self._class_scope_factory(self._scope.class_ids)
                if self._scope.class_ids and self._class_scope_factory is not None
                else None
            )
            stmt = stmt.where(
                scope_predicate(
                    self._model,
                    self._scope,
                    owner_attr=self._owner_attr,
                    class_member_subquery=class_member_subquery,
                )
            )
        return stmt

    def base_query(self) -> Select[Any]:
        """带租户过滤的全表查询。"""

        return self.scoped(select(self._model))

    async def get(self, object_id: UUID) -> ModelT | None:
        """按主键取单行（自动带租户边界）。"""

        result = await self._session.execute(
            self.scoped(select(self._model).where(self._model.id == object_id))
        )
        # SQLAlchemy 对泛型模型的返回类型推断不精确，这里显式声明
        return cast(ModelT | None, result.scalar_one_or_none())

    async def list_all(self, *, limit: int = 100, offset: int = 0) -> list[ModelT]:
        """列出当前租户数据（分页）。"""

        result = await self._session.execute(
            # 稳定次序：同事务批量写入的 created_at 可能相同，追加 id 保证分页不重不漏
            self.base_query()
            .order_by(self._model.created_at.desc(), self._model.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [cast(ModelT, row) for row in result.scalars().all()]

    async def list_cursor(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        extra: Any | None = None,
    ) -> tuple[list[ModelT], str | None]:
        """按 cursor 分页列出（TD-070：稳定键集分页，不重不漏）。

        游标 = 最后一行的 (created_at, id)；与 `list_all` 同一稳定次序
        （created_at DESC, id DESC），keyset 条件 `(created_at, id) < 游标`。

        返回:
            (本页行, 下一页游标)；下一页游标为 None 表示没有更多数据
            （按「取满 limit 才给游标」判定，代价是恰好整页时多查一次，可接受）。

        异常:
            ValueError: cursor 无法解码（由路由层映射为 400）。
        """

        page_size = max(1, limit)
        stmt = self.base_query().order_by(self._model.created_at.desc(), self._model.id.desc())
        if extra is not None:
            # 过滤必须在游标推进**之前**：先分页后过滤会让游标跳过被过滤的行
            stmt = stmt.where(extra)
        if cursor is not None:
            marker_created_at, marker_id = decode_cursor(cursor)
            stmt = stmt.where(
                tuple_(self._model.created_at, self._model.id) < tuple_(marker_created_at, marker_id)
            )
        stmt = stmt.limit(page_size)
        rows = [cast(ModelT, row) for row in (await self._session.execute(stmt)).scalars().all()]
        next_cursor = (
            encode_cursor(rows[-1].created_at, rows[-1].id) if len(rows) == page_size else None
        )
        return rows, next_cursor

    def assert_same_tenant(self, obj: ModelT) -> None:
        """校验对象归属当前租户。

        异常:
            CrossTenantAccessError: 对象属于其他租户（或未设置 tenant_id）。
        """

        obj_tenant = getattr(obj, "tenant_id", None)
        if obj_tenant != self._tenant_id:
            raise CrossTenantAccessError(
                f"对象租户 {obj_tenant} 与当前上下文 {self._tenant_id} 不一致，拒绝访问"
            )

    async def add(self, obj: ModelT) -> ModelT:
        """新增（写入前校验租户一致，并回填缺失的 tenant_id）。

        异常:
            CrossTenantAccessError: 对象显式带其它租户的 tenant_id。
        """

        obj_tenant = getattr(obj, "tenant_id", None)
        if obj_tenant is None:
            obj.tenant_id = self._tenant_id
        else:
            self.assert_same_tenant(obj)
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def delete(self, obj: ModelT) -> None:
        """删除（先校验租户一致）。

        异常:
            CrossTenantAccessError: 对象属于其他租户。
        """

        self.assert_same_tenant(obj)
        await self._session.delete(obj)
        await self._session.flush()
