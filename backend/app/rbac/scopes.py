"""数据范围（RBAC-02）的判据与谓词生成。

口径：`docs/rbac-matrix.md` §2 + `docs/design-briefs/M1-E2-data-scope.md` §0 ⑨⑩。

要点：
- 范围来自 `data_scopes`（可绑角色，也可绑用户），**取并集**；
- `school`（全校）覆盖其余一切；只有 `self` 时按归属列钉到当前主体；
- **解析不到任何范围行 = 空集**（查询直接不可见），不回退成全校：
  授权缺失应当让用户看不到数据，而不是让他看到整个租户；
- `class`/`course` 是合法的值（库里 CHECK 允许），但服务端过滤要到 M2 有班级/任课关系才实现。
  在那之前它们**不贡献可见性**——否则"授了班级范围"会被读成"能看全校"。
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, get_args
from uuid import UUID

from sqlalchemy import ColumnElement, false, true

from app.db.models.rbac import ScopeType

# 与 `data_scopes.scope_type` 的数据库 CHECK 同源：值直接取自模型的 `ScopeType` 声明，
# 不在这里再抄一份字面量（抄了就有「改了模型忘了改这里」的静默漂移）。
# 库里那条 CHECK 本身由集成测试与启动自检验证（`tests/db/test_data_scope_integration.py`）。
KNOWN_SCOPE_TYPES: frozenset[str] = frozenset(get_args(ScopeType))

# 本期真正实现了服务端过滤的范围类型（`class`/`course` → M2，见 TD-151）
SUPPORTED_SCOPE_TYPES: frozenset[str] = frozenset({"school", "self"})
# 取数 SQL 不在这里：本模块只负责"范围行意味着什么"（纯判据，可单测、可被矩阵测试解析），
# 而"每请求怎么把范围行读出来"属于认证层（`app/auth/principal.py`），
# 那里已经是裸 SQL 守卫的豁免模块——把 SQL 放这里只会多养一个豁免条目（守卫的初衷是少而不是多）。


@dataclass(frozen=True, slots=True)
class DataScopeContext:
    """一次请求的数据范围判据（由认证层每请求从库解析，不写进令牌）。

    参数:
        subject_user_id: 当前主体用户 id（`self` 范围的锚点）。
        scope_types: 角色与用户范围的并集；空集表示「没有任何范围授权」。
    """

    subject_user_id: UUID
    scope_types: frozenset[str]

    @property
    def sees_whole_school(self) -> bool:
        """是否具备全校范围（覆盖其余全部范围类型）。"""

        return "school" in self.scope_types

    @property
    def denies_everything(self) -> bool:
        """是否看不到任何行：没有任何范围，或只有本期未实现的 `class`/`course`。"""

        return not self.sees_whole_school and "self" not in self.scope_types


def scope_context_from_rows(
    *, subject_user_id: UUID, rows: Iterable[tuple[str, str | None]]
) -> DataScopeContext:
    """把 `data_scopes` 查询结果（scope_type, scope_id）归一成一个上下文。

    `scope_id` 本期不参与判定（只有 `class`/`course` 才用它，见 TD-151），
    但仍然读出来：将来按班级过滤时不需要改这里的取数口径。

    异常:
        ValueError：出现数据库 CHECK 之外的 scope_type（说明模型与库口径漂移，
            或有人手工写入了脏值）。不能静默忽略——忽略会让「未知范围」退化成无过滤。
    """

    types: set[str] = set()
    for scope_type, _scope_id in rows:
        if scope_type not in KNOWN_SCOPE_TYPES:
            raise ValueError(
                f"未知的数据范围类型：{scope_type!r}（允许值 {sorted(KNOWN_SCOPE_TYPES)}）"
            )
        types.add(scope_type)
    return DataScopeContext(subject_user_id=subject_user_id, scope_types=frozenset(types))


def scope_predicate(
    model: type[Any], context: DataScopeContext, *, owner_attr: str
) -> ColumnElement[bool]:
    """生成这一行「当前主体是否可见」的 SQL 谓词。

    参数:
        model: 目标模型（需要归属列时用属性名寻址，避免调用方传错列）。
        context: 当前主体的范围判据。
        owner_attr: `self` 范围对应的归属列名（如 `users.id`、`leave_requests.user_id`）。

    返回值:
        SQLAlchemy 布尔表达式：全校 → 恒真；仅本人 → 归属列等于主体；无范围 → 恒假。

    异常:
        ValueError：需要归属列但模型上没有该列（配置错误，宁可报错也不放行整表）。
    """

    if context.sees_whole_school:
        return true()
    if context.denies_everything:
        # 没有任何范围，或只有 class/course（本期未实现）：按「看不到」处理
        return false()
    owner_column: ColumnElement[Any] | None = getattr(model, owner_attr, None)
    if owner_column is None:
        raise ValueError(
            f"{model.__name__} 缺少归属列 {owner_attr}，无法按 self 范围过滤；"
            "请显式声明归属列或改用全校范围"
        )
    return owner_column == context.subject_user_id


__all__ = [
    "KNOWN_SCOPE_TYPES",
    "SUPPORTED_SCOPE_TYPES",
    "DataScopeContext",
    "ScopeType",
    "scope_context_from_rows",
    "scope_predicate",
]
