"""M2-UI2：角色出厂标记、乐观锁与 MFA 重置权限（`docs/design-briefs/M2-UI2-rbac-admin.md`）。

做四件事：

1. **TD-150 先决**：`roles.defaults_applied` / `platform_roles.defaults_applied` 标记
   “出厂默认（权限+数据范围）是否已初始化”。存量角色一律回填 `true`——升级前它们都已走过
   开通/同步，当前状态就是事实；升级瞬间不补授，避免把刚被清空的角色又灌回默认。
2. **TD-137 并发控制**：`roles.version` 乐观锁，角色权限全量替换/重置出厂时递增。
3. **开通函数**：`seed_builtin_roles(uuid)` 改为“只为未初始化的角色种一次，种完置 true；
   全量型超管仍按只补不撤对齐”，函数体由 E2 原文派生，锚点不唯一即失败。
4. **TD-126 权限码**：`school.personnel.mfa.reset` 随目录同步落库（59→60）；校方超管由
   `code_realm` 的 school 模块全量自动获得，`school_admin` 不默认授予。

历史迁移重放：E1/E2 的 upgrade 调用的是**当前**同步代码，而那时本迁移的列还没建；
`app/rbac` 用 `defaults_marker_columns_exist()` 探测后回退旧启发式，全新库 `upgrade head`
因此不会引用不存在的列。

downgrade：恢复 E2 的 `seed_builtin_roles`，删除本迁移新增的权限码及其映射，再删三列。
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic import op

from app.rbac import sync_rbac_definitions_sync

revision: str = "c3f7a9d1e5b2"
down_revision: str | None = "b9d3f7c1e5a9"
branch_labels: str | None = None
depends_on: str | None = None

_E2_MIGRATION = "f7a9b1c3d5e8_m1e2_data_scope.py"
_NEW_PERMISSION_CODE = "school.personnel.mfa.reset"


def _load_migration(filename: str) -> object:
    """按文件名加载上游迁移模块（读函数体原文用）。"""

    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(f"alembic_source_{filename[:12]}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载函数体来源迁移：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replace_once(source: str, old: str, new: str, anchor: str) -> str:
    """唯一锚点替换；锚点数量不为 1 时失败，避免函数体漂移被静默吞掉。"""

    occurrences = source.count(old)
    if occurrences != 1:
        raise RuntimeError(f"锚点 {anchor} 出现 {occurrences} 次（期望 1 次）；函数体已漂移")
    return source.replace(old, new)


def _seed_body() -> str:
    """在 E2 的 `seed_builtin_roles` 原文上加入 TD-150 标记语义。"""

    source = _load_migration(_E2_MIGRATION).__dict__["_seed_body"]()
    if not isinstance(source, str):
        raise RuntimeError(f"{_E2_MIGRATION} 缺少 _seed_body()")

    permission_old = """    WHERE r.tenant_id = p_tenant_id
      AND r.is_system
      AND NOT EXISTS (
          SELECT 1 FROM public.role_permissions rp
          WHERE rp.role_id = r.id AND rp.permission_id = p.id
      );"""
    permission_new = """    WHERE r.tenant_id = p_tenant_id
      AND r.is_system
      AND (r.code = 'school_super_admin' OR r.defaults_applied IS NOT TRUE)
      AND NOT EXISTS (
          SELECT 1 FROM public.role_permissions rp
          WHERE rp.role_id = r.id AND rp.permission_id = p.id
      );"""
    source = _replace_once(source, permission_old, permission_new, "seed 权限补授")

    scope_old = """      AND NOT EXISTS (
          SELECT 1 FROM public.data_scopes s WHERE s.role_id = r.id
      );"""
    scope_new = """      AND (r.code = 'school_super_admin' OR r.defaults_applied IS NOT TRUE)
      AND NOT EXISTS (
          SELECT 1 FROM public.data_scopes s
          WHERE s.role_id = r.id
            AND s.scope_type = d.scope_type
            AND s.scope_id IS NULL
      );

    -- 标记出厂默认已初始化：之后运营主动清空不会被同步/开通流程补回（TD-150）。
    UPDATE public.roles
    SET defaults_applied = true,
        version = version + 1
    WHERE tenant_id = p_tenant_id
      AND is_system
      AND (defaults_applied IS NOT TRUE OR code = 'school_super_admin');"""
    return _replace_once(source, scope_old, scope_new, "seed 范围补授")


def _require_bypass_role() -> None:
    """SECURITY DEFINER 函数 owner 必须能绕过 FORCE RLS（与上游迁移同一前提）。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles
                WHERE rolname = current_user AND (rolsuper OR rolbypassrls)
            ) THEN
                RAISE EXCEPTION 'M2-UI2 requires migration role superuser or BYPASSRLS';
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    """加标记/版本列、回填存量、切换开通函数、同步目录与定义表。"""

    _require_bypass_role()

    op.add_column(
        "roles",
        sa.Column("defaults_applied", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "roles",
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "platform_roles",
        sa.Column("defaults_applied", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # 存量角色都已完成过开通/同步：当前状态即事实，升级不再自动补授。
    op.execute("UPDATE public.roles SET defaults_applied = true")
    op.execute("UPDATE public.platform_roles SET defaults_applied = true")

    op.execute(_seed_body())
    op.execute("REVOKE ALL ON FUNCTION public.seed_builtin_roles(uuid) FROM PUBLIC")

    # 目录（新增 school.personnel.mfa.reset）、定义表、内置角色补齐；列已存在，使用标记判据。
    sync_rbac_definitions_sync(op.get_bind())


def downgrade() -> None:
    """恢复 E2 开通函数、删除新增权限码与三列。"""

    e2 = _load_migration(_E2_MIGRATION)
    seed = e2.__dict__["_seed_body"]()
    if not isinstance(seed, str):
        raise RuntimeError(f"{_E2_MIGRATION} 缺少 _seed_body()")
    op.execute(seed)
    op.execute("REVOKE ALL ON FUNCTION public.seed_builtin_roles(uuid) FROM PUBLIC")

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "delete from public.role_permissions where permission_id in "
            "(select id from public.permissions where code = :code)"
        ),
        {"code": _NEW_PERMISSION_CODE},
    )
    bind.execute(
        sa.text(
            "delete from public.platform_role_permissions where permission_id in "
            "(select id from public.permissions where code = :code)"
        ),
        {"code": _NEW_PERMISSION_CODE},
    )
    bind.execute(
        sa.text("delete from public.rbac_builtin_role_defaults where permission_code = :code"),
        {"code": _NEW_PERMISSION_CODE},
    )
    bind.execute(
        sa.text("delete from public.permissions where code = :code"),
        {"code": _NEW_PERMISSION_CODE},
    )

    op.drop_column("platform_roles", "defaults_applied")
    op.drop_column("roles", "version")
    op.drop_column("roles", "defaults_applied")
