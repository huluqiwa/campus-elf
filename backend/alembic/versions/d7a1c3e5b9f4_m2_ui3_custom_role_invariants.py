"""M2-UI3：自定义角色的库层不变量（`docs/design-briefs/M2-UI3-custom-roles-accounts.md`）。

做两件事，都是**库层**声明，因为接口层的正则会随重构漂移，而这两条一旦破就是权限面问题：

1. `ck_roles_code_format_valid`：角色 code 只允许 `^[a-z][a-z0-9_]{2,62}$`。自定义角色首次成为
   运营可输入的对象，绕过应用代码的手工 SQL 同样要被挡；内置 5 码本就满足该式，约束不是新增负担。
2. `ck_roles_builtin_code_is_system`：内置角色码只能由 `is_system=true` 的行持有。
   只靠 `uq_roles_tenant_id_code` 不够——那是「本租户恰好已有内置同码行」的偶然结果，
   而自定义角色与内置角色共享 `rbac_builtin_role_defaults` 的按 code 补种路径，
   一个伪装的 `is_system=false` 同码角色会让同步/重置出厂把它当内置角色对待。

权限目录新增 `school.role.delete` 由 `sync_rbac_definitions()` 落库（与 M2-UI2 的
`school.personnel.mfa.reset` 同口径，不写显式 insert）；`users.status` 的 CHECK 自 M1-A 起就含
`disabled`，停用账号不需要新约束。

downgrade：只删本迁移新增的两条 CHECK，不动数据。
"""

import sqlalchemy as sa
from alembic import op

revision: str = "d7a1c3e5b9f4"
down_revision: str | None = "c3f7a9d1e5b2"
branch_labels: str | None = None
depends_on: str | None = None

# 与 `app/rbac/seeds.py:TENANT_BUILTIN_ROLES` 一致；这里是**快照**，不 import 代码目录，
# 否则未来新增内置角色会让本迁移的语义跟着变（历史迁移必须冻结）。
_TENANT_BUILTIN_CODES = ("school_super_admin", "school_admin", "teacher_b_end", "student", "teacher")

_CODE_FORMAT = r"^[a-z][a-z0-9_]{2,62}$"


def upgrade() -> None:
    bind = op.get_bind()
    quoted = ", ".join(f"'{code}'" for code in _TENANT_BUILTIN_CODES)

    # 先验存量再动手：带着坏行加约束，PG 只会给一句 `check constraint ... for table "roles"
    # violates check constraint` —— 排查的人看不出那是"运营早年自建了同名角色"这种数据问题。
    violations = bind.execute(
        sa.text(
            f"select count(*) from public.roles where code !~ '{_CODE_FORMAT}' "
            f"or (not is_system and code in ({quoted}))"
        )
    ).scalar()
    if int(violations or 0) != 0:
        raise RuntimeError(
            f"存量 roles 有 {violations} 行不满足 M2-UI3 的角色码不变量；"
            "请先修正数据再升级（静默带着坏行上线会让自定义角色与内置角色混淆）"
        )

    op.execute(
        sa.text(
            "alter table public.roles add constraint ck_roles_code_format_valid "
            f"check (code ~ '{_CODE_FORMAT}')"
        )
    )
    op.execute(
        sa.text(
            "alter table public.roles add constraint ck_roles_builtin_code_is_system "
            f"check (is_system or code not in ({quoted}))"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("alter table public.roles drop constraint if exists ck_roles_builtin_code_is_system")
    )
    op.execute(sa.text("alter table public.roles drop constraint if exists ck_roles_code_format_valid"))
