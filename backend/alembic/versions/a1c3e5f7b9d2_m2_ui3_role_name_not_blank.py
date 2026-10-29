"""M2-UI3 TD-187g：`roles.name` 的"非空白"落到库层（`docs/tech-debt.md` TD-198）。

问题：角色名的"非空白"此前只在接口层拦（`RoleCreatePayload` 的 `field_validator` 去空白后判非法），
服务层仅 `strip` 不校验，库层只有 NOT NULL + `String(100)`。绕过应用代码的手工 SQL 仍能落一行
没有名字的角色——列表里只能靠 code 认。

为什么是 CHECK 而不是只补服务层校验：`code` 的格式不变量已经用库层 CHECK 兜住
（`ck_roles_code_format_valid`，迁移 `d7a1c3e5b9f4`），`name` 只有接口一处是同一族缺口；
而 `name` 与 `code` 不同——它不参与任何判据、只影响可读性——所以这条约束是"兜底"而非承重墙。

空白字符集为什么不是默认的 `btrim(name)`：默认 `btrim` **只去空格**，去不掉制表符/换行。
接口层用的是 Python `str.strip()`（去**全部** Unicode 空白，比这里这份 ASCII 集更严）：
若库层更宽松，就会出现"接口拒、库收"的反向缺口——兜底反而漏掉它本该拦的那类行（制表符-only
的名字在列表里同样是一行"看不见名字"的角色）。因此这里显式给出一份 ASCII 空白字符集，
它 ⊆ 接口层那一份。方向只能是接口层更严；反过来的窄缝（绕过应用直接写 SQL 时，一个只含
U+3000 的名字库层会放行）登记在 `docs/tech-debt.md` TD-239。

字符集字面量与应用层常量 `DB_ROLE_NAME_BLANK_CHARS`（`app/rbac/admin.py`）由单元测试比对，
防止两边漂移（与 `ck_roles_code_format_valid` 的同款回归网）。

存量预检的前提：迁移连接必须能绕过 RLS（`roles` 已 FORCE ROW LEVEL SECURITY），
否则存量计数会被 RLS 遮蔽成 0、预检形同虚设——这一条在 `upgrade()` 里显式断言
（同 `c4d6e8f0a2b3` 的 `_assert_can_bypass_rls`）。

downgrade：只删本迁移新增的 CHECK，不动数据。
"""

import sqlalchemy as sa
from alembic import op

revision: str = "a1c3e5f7b9d2"
down_revision: str | None = "e9b4d2f6a8c1"
branch_labels: str | None = None
depends_on: str | None = None

# 与 `app/rbac/admin.py:DB_ROLE_NAME_BLANK_CHARS` 同一份空白字符集（这里是**快照**，
# 历史迁移不 import 代码目录，否则将来改了常量会让本迁移的语义跟着变）。
# 反斜杠转义由 PG 的 E'' 字面量解释（\t \n \r \f \v）。
_BLANK_CHARS = r" \t\n\r\f\v"


def _assert_can_bypass_rls() -> None:
    """迁移连接必须能看全所有租户行，否则下面的存量校验会被 RLS 遮蔽成 0 行。

    `roles` 已 FORCE ROW LEVEL SECURITY（迁移 `779f731db342`），普通 DDL 角色即使拥有表
    也匹配不到策略，存量违例会被数成 0 → 预检形同虚设、迁移带着坏行继续走，
    直到 CHECK 生效时才以一句难读的约束冲突失败。部署要求 `MIGRATION_DATABASE_URL`
    使用超级用户或 BYPASSRLS 角色（docs/deployment.md §6.6），这里把它变成显式报错。
    """

    bind = op.get_bind()
    allowed = bind.execute(
        sa.text(
            "SELECT coalesce("
            "(SELECT rolsuper OR rolbypassrls FROM pg_roles "
            "WHERE rolname = current_user), false)"
        )
    ).scalar_one()
    if not allowed:
        raise RuntimeError(
            "迁移连接角色既非超级用户也无 BYPASSRLS，无法校验全部租户的角色名存量数据；"
            "请改用 MIGRATION_DATABASE_URL（见 docs/deployment.md §6.6）"
        )


def upgrade() -> None:
    _assert_can_bypass_rls()

    bind = op.get_bind()
    # 先验存量再动手：带着坏行加约束，PG 只会给一句 `check constraint ... for table "roles"
    # violates check constraint` —— 排查的人看不出那是"早年手工插了一行没名字的角色"这种数据问题。
    violations = bind.execute(
        sa.text(
            "select count(*) from public.roles "
            f"where char_length(btrim(name, E'{_BLANK_CHARS}')) = 0"
        )
    ).scalar()
    if int(violations or 0) != 0:
        raise RuntimeError(
            f"存量 roles 有 {violations} 行的 name 去空白后为空；"
            "请先修正数据再升级（静默带着坏行上线会让列表里出现没有名字的角色）"
        )

    op.execute(
        sa.text(
            "alter table public.roles add constraint ck_roles_name_not_blank "
            f"check (char_length(btrim(name, E'{_BLANK_CHARS}')) > 0)"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("alter table public.roles drop constraint if exists ck_roles_name_not_blank"))
