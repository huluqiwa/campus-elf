"""数据范围（RBAC-02）的判据与谓词生成。

口径：`docs/rbac-matrix.md` §2 + `docs/design-briefs/M1-E2-data-scope.md` §0 ⑨⑩。

要点：
- 范围来自 `data_scopes`（可绑角色，也可绑用户），**取并集**；
- `school`（全校）覆盖其余一切；只有 `self` 时按归属列钉到当前主体；
- **解析不到任何范围行 = 空集**（查询直接不可见），不回退成全校：
  授权缺失应当让用户看不到数据，而不是让他看到整个租户；
- `class` 自 M2-ORG01-03 起参与过滤：范围行携带 `scope_id`（班级 id），
  由仓储方提供「班级 → 成员归属列」的子查询工厂后并入谓词；模型没有工厂时
  class 范围**不贡献可见性**（fail-closed，绝不放大成全校）；
- `course` 仍是合法值但本期不贡献可见性（可按课程过滤的业务表在 M3，TD-151）。
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, get_args
from uuid import UUID

from sqlalchemy import ColumnElement, Select, false, true

from app.db.models.rbac import ScopeType

# 与 `data_scopes.scope_type` 的数据库 CHECK 同源：值直接取自模型的 `ScopeType` 声明，
# 不在这里再抄一份字面量（抄了就有「改了模型忘了改这里」的静默漂移）。
# 库里那条 CHECK 本身由集成测试与启动自检验证（`tests/db/test_data_scope_integration.py`）。
KNOWN_SCOPE_TYPES: frozenset[str] = frozenset(get_args(ScopeType))

# `class` 自 M2-ORG01-03 起在提供了成员子查询工厂的模型上生效；
# `course` 仍不贡献可见性（按课程过滤的业务表在 M3，TD-151）。
SUPPORTED_SCOPE_TYPES: frozenset[str] = frozenset({"school", "self", "class"})
# 取数 SQL 不在这里：本模块只负责"范围行意味着什么"（纯判据，可单测、可被矩阵测试解析），
# 而"每请求怎么把范围行读出来"属于认证层（`app/auth/principal.py`），
# 那里已经是裸 SQL 守卫的豁免模块——把 SQL 放这里只会多养一个豁免条目（守卫的初衷是少而不是多）。


@dataclass(frozen=True, slots=True)
class DataScopeContext:
    """一次请求的数据范围判据（由认证层每请求从库解析，不写进令牌）。

    参数:
        subject_user_id: 当前主体用户 id（`self` 范围的锚点）。
        scope_types: 角色与用户范围的并集；空集表示「没有任何范围授权」。
        class_ids: `class` 范围行的 scope_id 集合（空集 = 没有班级范围）。
            `course` 范围行本期不解析（不贡献可见性，TD-151）。
    """

    subject_user_id: UUID
    scope_types: frozenset[str]
    class_ids: frozenset[UUID] = field(default_factory=frozenset)

    @property
    def sees_whole_school(self) -> bool:
        """是否具备全校范围（覆盖其余全部范围类型）。"""

        return "school" in self.scope_types

    @property
    def denies_everything(self) -> bool:
        """是否看不到任何行：没有 school/self，也没有任何 class 范围。

        只有 `course` 范围仍算「看不到」（course 过滤在 M3 才有业务表承载）。
        """

        return (
            not self.sees_whole_school
            and "self" not in self.scope_types
            and not self.class_ids
        )


def scope_context_from_rows(
    *, subject_user_id: UUID, rows: Iterable[tuple[str, str | None]]
) -> DataScopeContext:
    """把 `data_scopes` 查询结果（scope_type, scope_id）归一成一个上下文。

    `scope_id` 只有 `class` 行参与判定（M2-ORG01-03）；`course` 行读取并校验后不解析。
    scope_id 出现非法 UUID 视为脏数据：不能静默忽略——忽略会让范围悄悄放大。

    异常:
        ValueError：出现数据库 CHECK 之外的 scope_type，或 class 行的 scope_id 不是合法 UUID
            （说明模型与库口径漂移，或有人手工写入了脏值）。不能静默忽略——
            忽略会让「未知范围」退化成无过滤。
    """

    types: set[str] = set()
    class_ids: set[UUID] = set()
    for scope_type, scope_id in rows:
        if scope_type not in KNOWN_SCOPE_TYPES:
            raise ValueError(
                f"未知的数据范围类型：{scope_type!r}（允许值 {sorted(KNOWN_SCOPE_TYPES)}）"
            )
        types.add(scope_type)
        if scope_type == "class":
            if scope_id is None:
                raise ValueError("class 范围行缺少 scope_id（库 CHECK 之外的脏数据）")
            try:
                class_ids.add(scope_id if isinstance(scope_id, UUID) else UUID(str(scope_id)))
            except ValueError as exc:
                raise ValueError(f"class 范围行的 scope_id 不是合法 UUID：{scope_id!r}") from exc
    return DataScopeContext(
        subject_user_id=subject_user_id, scope_types=frozenset(types), class_ids=frozenset(class_ids)
    )


def scope_predicate(
    model: type[Any],
    context: DataScopeContext,
    *,
    owner_attr: str,
    class_member_subquery: Select[Any] | None = None,
) -> ColumnElement[bool]:
    """生成这一行「当前主体是否可见」的 SQL 谓词。

    参数:
        model: 目标模型（需要归属列时用属性名寻址，避免调用方传错列）。
        context: 当前主体的范围判据。
        owner_attr: `self` 范围对应的归属列名（如 `users.id`、`leave_requests.user_id`）。
        class_member_subquery: 「班级范围可见成员」的取数子查询（返回归属列的值集合，
            由仓储方按模型构造，例如学生表中我班学生的 user_id）。为 None 时
            class 范围对该模型**不贡献可见性**（fail-closed，绝不放大成全校）。

    返回值:
        SQLAlchemy 布尔表达式：全校 → 恒真；(self ∪ class 子查询) 的并集；
        无任何范围 → 恒假。

    异常:
        ValueError：需要归属列但模型上没有该列（配置错误，宁可报错也不放行整表）。
    """

    if context.sees_whole_school:
        return true()
    if context.denies_everything:
        # 没有 school/self/class 任一范围：按「看不到」处理
        return false()
    owner_column: ColumnElement[Any] | None = getattr(model, owner_attr, None)
    if owner_column is None:
        raise ValueError(
            f"{model.__name__} 缺少归属列 {owner_attr}，无法按数据范围过滤；"
            "请显式声明归属列或改用全校范围"
        )
    predicate: ColumnElement[bool] = false()
    if "self" in context.scope_types:
        predicate |= owner_column == context.subject_user_id
    if context.class_ids and class_member_subquery is not None:
        predicate |= owner_column.in_(class_member_subquery)
    return predicate


__all__ = [
    "KNOWN_SCOPE_TYPES",
    "SUPPORTED_SCOPE_TYPES",
    "DataScopeContext",
    "ScopeType",
    "scope_context_from_rows",
    "scope_predicate",
]
