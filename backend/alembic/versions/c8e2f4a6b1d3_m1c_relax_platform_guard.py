"""M1-C 复审修复：放宽平台侧权限模块限制（2026-10-21）。

背景：`b7d1e3f5a9c2` 为两个方向都加了守卫，但 M1-A 的种子把**全量权限**（含 school.*/
campus.*）授予平台超管角色——平台作为运营方需要全量权限，且平台角色只能被平台 realm
主体使用（路由另有 `realm="platform"` 断言），因此"平台角色只能引用 platform 模块"
既与既有数据矛盾、也会挡住 M1-E 的权限矩阵落库。

本 revision 删除平台方向守卫，只保留**租户方向**（租户角色不得引用 platform.*），
并在 `docs/rbac-matrix.md` 明确该约束。

回滚：恢复平台方向守卫（与 b7d1e3f5a9c2 一致）。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c8e2f4a6b1d3"
down_revision: str | None = "b7d1e3f5a9c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """删除平台方向守卫（保留租户方向）。"""

    op.execute(
        "DROP TRIGGER IF EXISTS platform_role_permissions_realm_guard "
        "ON platform_role_permissions"
    )
    op.execute("DROP FUNCTION IF EXISTS platform_role_permissions_realm_guard()")


def downgrade() -> None:
    """恢复平台方向守卫。"""

    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform_role_permissions_realm_guard() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_module text;
        BEGIN
            SELECT module INTO v_module FROM public.permissions WHERE id = NEW.permission_id;
            IF v_module IS DISTINCT FROM 'platform' THEN
                RAISE EXCEPTION
                    'platform roles can only reference platform permissions (permission_id=%)',
                    NEW.permission_id;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER platform_role_permissions_realm_guard
        BEFORE INSERT OR UPDATE ON platform_role_permissions
        FOR EACH ROW EXECUTE FUNCTION platform_role_permissions_realm_guard()
        """
    )
