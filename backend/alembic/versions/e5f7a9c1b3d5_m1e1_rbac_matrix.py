"""M1-E1：权限矩阵落库 + 内置角色出厂默认（`docs/rbac-matrix.md` v1.1）。

做四件事：

1. **两张代码所有的定义表** `rbac_builtin_roles` / `rbac_builtin_role_defaults`：
   内置角色清单与其出厂默认权限映射。应用角色只读（无 INSERT/UPDATE/DELETE 授权），
   写入只由本迁移经 `app/rbac` 的同步完成 —— 避免"权限码清单在 SQL 里再抄一份"。
2. **`public.seed_builtin_roles(uuid)`**（SECURITY DEFINER）：给指定租户补齐内置角色与
   出厂默认映射（只补缺，不覆盖）。`provision_tenant()` 与 `review_registration_request()`
   的三条角色 INSERT + 超管授权块都由它替换 —— 生产开通走审核函数，DEV 开通走
   provision，两条路径必须同源，否则新租户权限因入口不同而不同。
3. **目录同步** `sync_rbac_definitions_sync(op.get_bind())`：删 9 个与矩阵重复的旧码、
   upsert 59 个矩阵码、重写定义表、给存量租户与平台侧补齐内置角色及其出厂默认。
4. **收尾校验**：每个租户都要有全部租户内置角色，缺一个就让迁移失败（fail-fast）。

函数体来源：直接读取 `e5c9d1f7a2b4_m1b_hardening2.py` 里的 `PROVISION_FUNCTION` /
`REVIEW_FUNCTION` 常量（当前库内生效版本），只做锚点替换。抄一遍 SQL 的风险是
"改了这份、漏了那份"；读原文 + 找不到锚点就抛错，把漂移变成迁移失败而不是静默差异。
downgrade 用读到的**原文**恢复旧函数体。

downgrade 口径：回到 M1-E1 之前的权限模型 —— 内置角色的映射重置为当时的出厂状态
（超管 = 旧目录 school+campus 模块，管理员/教师 B 端为空），删除 `student`/`teacher`
角色与 E1 新增的权限码，补回 9 个旧码。E1 之后运营在角色上做的增删不回滚保留：
版本既然退回 E1 之前，那些授权在旧模型里不存在。

角色/授权等集群级对象不随迁移回滚（与 M1-A 同一口径）。
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

from app.db.models.rbac import PERMISSION_CATALOG as LEGACY_PERMISSION_CATALOG
from app.rbac import sync_rbac_definitions_sync
from app.rbac.catalog import legacy_codes_removed_by_m1e1
from app.rbac.seeds import TENANT_BUILTIN_ROLES

revision: str = "e5f7a9c1b3d5"
down_revision: str | None = "c4d6e8f0a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HARDENING_MIGRATION = "e5c9d1f7a2b4_m1b_hardening2.py"

# E1 之前 provision/review 建的 3 个角色；downgrade 要把它们的映射重置回旧状态
_LEGACY_TENANT_ROLE_CODES: tuple[str, ...] = (
    "school_super_admin",
    "school_admin",
    "teacher_b_end",
)

SEED_ROLES_FUNCTION = """
CREATE OR REPLACE FUNCTION public.seed_builtin_roles(p_tenant_id uuid) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    -- 内置角色：按定义表补齐（realm='tenant'）。对象全部 public. 限定，防调用方 TEMP 同名遮蔽。
    INSERT INTO public.roles (id, tenant_id, code, name, is_system)
    SELECT gen_random_uuid(), p_tenant_id, b.code, b.name, true
    FROM public.rbac_builtin_roles b
    WHERE b.realm = 'tenant'
      AND NOT EXISTS (
          SELECT 1 FROM public.roles r
          WHERE r.tenant_id = p_tenant_id AND r.code = b.code
      );

    -- 出厂默认映射：只补缺，绝不覆盖运营已改过的授权。
    -- 校方超管因此只拿到 school/campus 模块（矩阵 §5-11：租户角色不得引用平台域权限，
    -- 与启动自检 `catalog.code_realm()` 同一口径；DB 触发器 role_permissions_realm_guard
    -- 目前只硬挡 module='platform'，收紧为模块白名单见 TD-149 → M1-E2）。
    INSERT INTO public.role_permissions (id, tenant_id, role_id, permission_id)
    SELECT gen_random_uuid(), p_tenant_id, r.id, p.id
    FROM public.roles r
    JOIN public.rbac_builtin_role_defaults d
      ON d.realm = 'tenant' AND d.role_code = r.code
    JOIN public.permissions p ON p.code = d.permission_code
    WHERE r.tenant_id = p_tenant_id
      AND r.is_system
      AND NOT EXISTS (
          SELECT 1 FROM public.role_permissions rp
          WHERE rp.role_id = r.id AND rp.permission_id = p.id
      );
END
$$;
"""

# ---------------------------------------------------------------------------
# 两个开通函数中被替换的旧片段（与 e5c9d1f7a2b4 逐字一致；缩进随原文件）
# ---------------------------------------------------------------------------
_PROVISION_DECLARE_OLD = """
    v_role_super uuid := gen_random_uuid();
    v_role_admin uuid := gen_random_uuid();
    v_role_teacher uuid := gen_random_uuid();
"""
_PROVISION_DECLARE_NEW = """
    v_role_super uuid;
"""
_PROVISION_BODY_OLD = """
    INSERT INTO public.roles (id, tenant_id, code, name, is_system) VALUES
        (v_role_super, v_tenant_id, 'school_super_admin', '校方超管', true),
        (v_role_admin, v_tenant_id, 'school_admin', '校方授权管理员', true),
        (v_role_teacher, v_tenant_id, 'teacher_b_end', '教师（B 端权限）', true);

    INSERT INTO public.role_permissions (id, tenant_id, role_id, permission_id)
    SELECT gen_random_uuid(), v_tenant_id, v_role_super, p.id
    FROM public.permissions p
    WHERE p.module IN ('school', 'campus');
"""
_PROVISION_BODY_NEW = """
    PERFORM public.seed_builtin_roles(v_tenant_id);

    SELECT r.id INTO v_role_super
    FROM public.roles r
    WHERE r.tenant_id = v_tenant_id AND r.code = 'school_super_admin';
"""

_REVIEW_DECLARE_OLD = """
    v_role_super uuid;
    v_role_admin uuid;
    v_role_teacher uuid;
"""
_REVIEW_DECLARE_NEW = """
    v_role_super uuid;
"""
_REVIEW_BODY_OLD = """
        v_role_super := gen_random_uuid();
        v_role_admin := gen_random_uuid();
        v_role_teacher := gen_random_uuid();
        INSERT INTO public.roles (id, tenant_id, code, name, is_system) VALUES
            (v_role_super, v_tenant_id, 'school_super_admin', '校方超管', true),
            (v_role_admin, v_tenant_id, 'school_admin', '校方授权管理员', true),
            (v_role_teacher, v_tenant_id, 'teacher_b_end', '教师（B 端权限）', true);

        -- 校方超管只获得校方/C 端模块权限；platform.* 属平台 realm
        INSERT INTO public.role_permissions (id, tenant_id, role_id, permission_id)
        SELECT gen_random_uuid(), v_tenant_id, v_role_super, p.id
        FROM public.permissions p
        WHERE p.module IN ('school', 'campus');
"""
_REVIEW_BODY_NEW = """
        PERFORM public.seed_builtin_roles(v_tenant_id);

        SELECT r.id INTO v_role_super
        FROM public.roles r
        WHERE r.tenant_id = v_tenant_id AND r.code = 'school_super_admin';
"""

_REVIEW_SIGNATURE = "uuid, text, text, uuid, text, uuid, text, uuid, text, text"


def _legacy_functions() -> tuple[str, str]:
    """读取当前库内生效的两个函数体原文（来自 `e5c9d1f7a2b4`）。

    异常:
        RuntimeError：迁移文件缺失或常量改名（不允许静默退化成"跳过替换"）。
    """

    path = Path(__file__).resolve().parent / _HARDENING_MIGRATION
    spec = importlib.util.spec_from_file_location("m1b_hardening2_source", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载函数体来源迁移：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    provision = getattr(module, "PROVISION_FUNCTION", None)
    review = getattr(module, "REVIEW_FUNCTION", None)
    if not isinstance(provision, str) or not isinstance(review, str):
        raise RuntimeError(f"{_HARDENING_MIGRATION} 缺少 PROVISION_FUNCTION/REVIEW_FUNCTION 常量")
    return provision, review


def _replace_once(source: str, old: str, new: str, anchor: str) -> str:
    """唯一锚点替换：锚点必须恰好出现一次。

    异常:
        RuntimeError：锚点出现 0 次或多次（上游函数体被改过，必须人工确认而不是猜）。
    """

    occurrences = source.count(old)
    if occurrences != 1:
        raise RuntimeError(f"锚点 {anchor} 出现 {occurrences} 次（期望 1 次）；函数体已漂移")
    return source.replace(old, new)


def _provision_body() -> str:
    """新版 `provision_tenant(jsonb)`：建角色与授权改由 seed_builtin_roles 承担。"""

    provision, _ = _legacy_functions()
    body = _replace_once(
        provision, _PROVISION_DECLARE_OLD, _PROVISION_DECLARE_NEW, "provision DECLARE"
    )
    return _replace_once(body, _PROVISION_BODY_OLD, _PROVISION_BODY_NEW, "provision roles")


def _review_body() -> str:
    """新版 `review_registration_request(...)`：审核通过分支同样走 seed_builtin_roles。"""

    _, review = _legacy_functions()
    body = _replace_once(review, _REVIEW_DECLARE_OLD, _REVIEW_DECLARE_NEW, "review DECLARE")
    return _replace_once(body, _REVIEW_BODY_OLD, _REVIEW_BODY_NEW, "review roles")


def _sql_array(values: Sequence[str]) -> str:
    """把代码内常量转成 SQL 字面量数组。

    取值全部来自本仓库的 Python 常量（`seeds.py` / 旧目录），不是外部输入；
    bandit 对 alembic 目录只跳过 B608（字符串拼 SQL），其余规则照常。
    """

    return "array[" + ", ".join(f"'{value}'" for value in values) + "]"


def _new_tenant_role_codes() -> list[str]:
    """E1 才引入的租户内置角色码（downgrade 要删掉它们）。"""

    return sorted(
        role.code for role in TENANT_BUILTIN_ROLES if role.code not in _LEGACY_TENANT_ROLE_CODES
    )


def _legacy_permission_codes() -> list[str]:
    """E1 之前 `permissions` 里的全部码（downgrade 之后目录必须恰好等于这些）。"""

    return [code for code, _, _ in LEGACY_PERMISSION_CATALOG]


def _require_bypass_role() -> None:
    """SECURITY DEFINER 函数 owner 必须能绕过 FORCE RLS（与 M1-B 同一前提）。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles
                WHERE rolname = current_user AND (rolsuper OR rolbypassrls)
            ) THEN
                RAISE EXCEPTION 'M1-E1 requires migration role superuser or BYPASSRLS';
            END IF;
        END $$;
        """
    )


def _grant_review_execute() -> None:
    """审核函数的 EXECUTE 只给平台角色（与 M1-B 硬化同一口径）。"""

    op.execute(
        f"REVOKE ALL ON FUNCTION review_registration_request({_REVIEW_SIGNATURE}) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION review_registration_request({_REVIEW_SIGNATURE}) "
        "TO campus_elf_platform"
    )


def upgrade() -> None:
    """建定义表与种子函数、切换两个开通函数、执行目录同步、校验存量租户。"""

    _require_bypass_role()

    op.create_table(
        "rbac_builtin_roles",
        sa.Column("realm", sa.String(length=16), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.PrimaryKeyConstraint("realm", "code", name="pk_rbac_builtin_roles"),
        sa.CheckConstraint(
            "realm IN ('platform', 'tenant')", name="ck_rbac_builtin_roles_realm_valid"
        ),
    )
    op.create_table(
        "rbac_builtin_role_defaults",
        sa.Column("realm", sa.String(length=16), nullable=False),
        sa.Column("role_code", sa.String(length=64), nullable=False),
        sa.Column("permission_code", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["permission_code"],
            ["permissions.code"],
            name="fk_rbac_builtin_role_defaults_permission_code_permissions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "realm", "role_code", "permission_code", name="pk_rbac_builtin_role_defaults"
        ),
        sa.CheckConstraint(
            "realm IN ('platform', 'tenant')",
            name="ck_rbac_builtin_role_defaults_realm_valid",
        ),
    )

    # 定义表是只读元数据（与 permissions 同一待遇）：应用角色可读用于展示"出厂默认"，
    # 不可写；写入只发生在迁移里。
    for role in ("campus_elf_tenant", "campus_elf_platform"):
        op.execute(f"GRANT SELECT ON rbac_builtin_roles TO {role}")
        op.execute(f"GRANT SELECT ON rbac_builtin_role_defaults TO {role}")

    op.execute(SEED_ROLES_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION seed_builtin_roles(uuid) FROM PUBLIC")

    op.execute(_provision_body())
    op.execute("REVOKE ALL ON FUNCTION provision_tenant(jsonb) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION provision_tenant(jsonb) FROM campus_elf_platform")
    op.execute(_review_body())
    _grant_review_execute()

    # 目录 + 内置角色 + 出厂默认（幂等；与集成测试执行同一份语句）
    sync_rbac_definitions_sync(op.get_bind())

    # 收尾校验：开通路径已切换，存量租户必须已齐角色
    op.execute(
        """
        DO $$
        DECLARE
            v_missing text;
        BEGIN
            SELECT s.missing INTO v_missing
            FROM (
                SELECT (t.id::text || ' 缺 ' || b.code) AS missing
                FROM public.tenants t
                CROSS JOIN public.rbac_builtin_roles b
                WHERE b.realm = 'tenant'
                  AND NOT EXISTS (
                      SELECT 1 FROM public.roles r
                      WHERE r.tenant_id = t.id AND r.code = b.code
                  )
                LIMIT 1
            ) s;
            IF v_missing IS NOT NULL THEN
                RAISE EXCEPTION 'M1-E1 后仍有租户缺少内置角色：%', v_missing;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    """恢复旧函数体、把内置角色映射重置为 E1 之前的出厂状态、删除 E1 对象。"""

    provision_legacy, review_legacy = _legacy_functions()

    op.execute(provision_legacy)
    op.execute(review_legacy)
    op.execute("REVOKE ALL ON FUNCTION provision_tenant(jsonb) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION provision_tenant(jsonb) FROM campus_elf_platform")
    _grant_review_execute()
    op.execute("DROP FUNCTION IF EXISTS public.seed_builtin_roles(uuid)")

    # 1) 租户内置角色的映射全部清空（超管在第 5 步按旧目录重授；管理员/教师 B 端旧模型无默认）
    op.execute(
        f"""
        DELETE FROM public.role_permissions rp
        USING public.roles r
        WHERE rp.role_id = r.id
          AND r.is_system
          AND r.code = ANY ({_sql_array([role.code for role in TENANT_BUILTIN_ROLES])})
        """
    )
    # 2) 删除 E1 新增的 C 端角色（user_roles / role_permissions 由复合外键 CASCADE 清理）
    # 带 `is_system`：口径与第 1 步一致，回滚只碰内置角色，绝不删运营自建的同名角色。
    op.execute(
        f"DELETE FROM public.roles WHERE is_system AND code = ANY "
        f"({_sql_array(_new_tenant_role_codes())})"
    )

    # 3) 删除 E1 新增的权限码（角色映射由 FK CASCADE 一并清理）
    op.execute(
        f"""
        DELETE FROM public.permissions
        WHERE code <> ALL ({_sql_array(_legacy_permission_codes())})
        """
    )
    # 4) 补回矩阵归一删掉的旧码（module/action 按旧目录；description 恢复为空）
    values = ", ".join(
        f"('{code}', '{module}', '{action}')" for code, module, action in LEGACY_PERMISSION_CATALOG
    )
    op.execute(
        f"INSERT INTO public.permissions (id, code, module, action) "
        f"SELECT gen_random_uuid(), v.c, v.m, v.a FROM (VALUES {values}) AS v(c, m, a) "
        "ON CONFLICT (code) DO NOTHING"
    )

    # 5) 超管回到「旧目录的 school + campus 模块」；平台超管回到全量（M1-A 口径）
    op.execute(
        """
        INSERT INTO public.role_permissions (id, tenant_id, role_id, permission_id)
        SELECT gen_random_uuid(), r.tenant_id, r.id, p.id
        FROM public.roles r
        CROSS JOIN public.permissions p
        WHERE r.code = 'school_super_admin'
          AND p.module IN ('school', 'campus')
          AND NOT EXISTS (
              SELECT 1 FROM public.role_permissions rp
              WHERE rp.role_id = r.id AND rp.permission_id = p.id
          )
        """
    )
    op.execute(
        """
        INSERT INTO public.platform_role_permissions (id, platform_role_id, permission_id)
        SELECT gen_random_uuid(), pr.id, p.id
        FROM public.platform_roles pr
        CROSS JOIN public.permissions p
        WHERE pr.code = 'platform_super_admin'
          AND NOT EXISTS (
              SELECT 1 FROM public.platform_role_permissions prp
              WHERE prp.platform_role_id = pr.id AND prp.permission_id = p.id
          )
        """
    )

    # 6) 收回只读授权并删除定义表
    for role in ("campus_elf_tenant", "campus_elf_platform"):
        op.execute(f"REVOKE SELECT ON rbac_builtin_roles FROM {role}")
        op.execute(f"REVOKE SELECT ON rbac_builtin_role_defaults FROM {role}")
    op.drop_table("rbac_builtin_role_defaults")
    op.drop_table("rbac_builtin_roles")

    # 7) 兜底：旧码必须已补齐（矩阵 §6 的 9 条）
    op.execute(
        f"""
        DO $$
        DECLARE
            v_count int;
        BEGIN
            SELECT count(*) INTO v_count FROM public.permissions
            WHERE code = ANY ({_sql_array(list(legacy_codes_removed_by_m1e1()))});
            IF v_count <> 9 THEN
                RAISE EXCEPTION 'downgrade 后旧权限码未补齐（实际 % 条，期望 9 条）', v_count;
            END IF;
        END $$;
        """
    )
