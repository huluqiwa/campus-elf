"""M1-C 审查修复：realm/模块矩阵的数据库层守卫（B1 第二道防线）。

背景：`permissions` 是单一全局目录（`platform.*` 与校方/C 端码同表），
`role_permissions` 与 `platform_role_permissions` 都只按 permission_id 外键引用它。
因此"租户角色被映射到 platform.* 码"在数据库层无人阻止，一旦发生，租户 realm 令牌
可能命中平台路由（平台路由内部固定使用平台连接）→ 跨租读。

本 revision 用触发器把矩阵钉在数据库层：
- `role_permissions` 只能引用 `module IN ('school','campus')` 的权限；
- `platform_role_permissions` 只能引用 `module = 'platform'` 的权限。

回滚：删除两个触发器与函数（不改数据）。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b7d1e3f5a9c2"
down_revision: str | None = "a3f5c7d9e1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_GUARD = r"""
CREATE OR REPLACE FUNCTION role_permissions_realm_guard() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_module text;
BEGIN
    SELECT module INTO v_module FROM public.permissions WHERE id = NEW.permission_id;
    IF v_module = 'platform' THEN
        RAISE EXCEPTION 'tenant roles cannot reference platform permissions (permission_id=%)',
            NEW.permission_id;
    END IF;
    RETURN NEW;
END;
$$;
"""

_PLATFORM_GUARD = r"""
CREATE OR REPLACE FUNCTION platform_role_permissions_realm_guard() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_module text;
BEGIN
    SELECT module INTO v_module FROM public.permissions WHERE id = NEW.permission_id;
    IF v_module IS DISTINCT FROM 'platform' THEN
        RAISE EXCEPTION 'platform roles can only reference platform permissions (permission_id=%)',
            NEW.permission_id;
    END IF;
    RETURN NEW;
END;
$$;
"""


def upgrade() -> None:
    """创建 realm/模块矩阵守卫触发器。"""

    op.execute(_TENANT_GUARD)
    op.execute(
        """
        CREATE TRIGGER role_permissions_realm_guard
        BEFORE INSERT OR UPDATE ON role_permissions
        FOR EACH ROW EXECUTE FUNCTION role_permissions_realm_guard()
        """
    )
    op.execute(_PLATFORM_GUARD)
    op.execute(
        """
        CREATE TRIGGER platform_role_permissions_realm_guard
        BEFORE INSERT OR UPDATE ON platform_role_permissions
        FOR EACH ROW EXECUTE FUNCTION platform_role_permissions_realm_guard()
        """
    )


def downgrade() -> None:
    """删除守卫触发器与函数。"""

    op.execute(
        "DROP TRIGGER IF EXISTS role_permissions_realm_guard ON role_permissions"
    )
    op.execute("DROP FUNCTION IF EXISTS role_permissions_realm_guard()")
    op.execute(
        "DROP TRIGGER IF EXISTS platform_role_permissions_realm_guard "
        "ON platform_role_permissions"
    )
    op.execute("DROP FUNCTION IF EXISTS platform_role_permissions_realm_guard()")
