"""租户级仓储基座：把 `tenant_id` 强制过滤收敛到一处。

规则（`docs/design-briefs/M1-tenant-identity.md` §3.1）：
- 读写都必须经过本基座，禁止直接用裸 session 操作租户表；
- `add` 时对象未带 `tenant_id` 会按当前上下文回填（调用方通常如此）；显式带了不同租户则报错；
- `delete` 前同样校验归属（父子同租由数据库复合外键兜底）；
- 查询一律追加 `tenant_id == ctx`，配合 RLS 形成双保险。
"""

from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession


class TenantScopedModel(Protocol):
    """租户级模型的最小结构约束（必须带 tenant_id 列）。"""

    tenant_id: Any
    id: Any
    created_at: Any


class CrossTenantAccessError(RuntimeError):
    """试图访问/写入非当前租户的数据。"""


class TenantScopedRepository[ModelT: TenantScopedModel]:
    """租户级仓储：所有访问都带租户边界。"""

    def __init__(self, session: AsyncSession, model: type[ModelT], tenant_id: UUID) -> None:
        """绑定会话、模型与租户上下文。

        参数:
            session: 处于事务中的会话（应已 `set_config('app.tenant_id', ...)`）。
            model: 目标模型（必须带 tenant_id 列）。
            tenant_id: 当前租户。

        异常:
            ValueError: 模型缺少 tenant_id（说明用错了仓储）。
        """

        if not hasattr(model, "tenant_id"):
            raise ValueError(f"{model.__name__} 不是租户级模型，禁止使用 TenantScopedRepository")
        self._session = session
        self._model = model
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> UUID:
        """当前租户。"""

        return self._tenant_id

    def scoped(self, stmt: Select[Any]) -> Select[Any]:
        """给查询追加租户过滤条件。"""

        return stmt.where(self._model.tenant_id == self._tenant_id)

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
