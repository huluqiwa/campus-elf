"""M1-E2：数据范围出厂默认 + 审批职责分离 + realm 触发器收紧（`docs/design-briefs/M1-E2-data-scope.md`）。

做五件事：

1. **两张新表**：`rbac_builtin_role_scope_defaults`（内置角色出厂默认**数据范围**，代码所有的
   定义表，与 E1 的两张定义表同性质）与 `system_settings`（平台级策略开关，改值必填理由）。
2. **`seed_builtin_roles(uuid)` 扩写范围**：新租户开通时除角色与权限映射外，还要按定义表写
   `data_scopes`。函数体由 **E1 迁移里的原文**派生（追加一段 INSERT），不在这里重抄前面的
   建角色/授权 SQL —— 抄一份就会漂移。
3. **审核函数承担 RBAC-03**：`review_registration_request()` 把申请行的提交人冗余写入审核记录，
   并在写入前判一次「提交人不能自批」（例外读 `system_settings`，例外生效才置
   `self_exception_used`）。函数体同样由 E1 迁移派生，只做锚点替换。
4. **数据库硬闸门**：`school_review_records` 加同表 `CHECK`（同人审批必须有例外标记）——
   这是「没走审核函数的直写」唯一的拦截点（口径 ③：不用触发器）。
5. **realm 触发器收紧**（TD-149）：`role_permissions_realm_guard()` 从「只挡
   `module='platform'`」改成「只允许 `catalog.TENANT_MODULES` 白名单」，与代码侧
   `code_realm()`/`TENANT_PLATFORM_LEAK_CHECK_SQL` 同一份真相 —— 白名单直接由目录生成，
   不在 SQL 里抄字面量。`observability.*`/`eval.*` 从此在库层也进不了租户角色。

函数体来源：`app/rbac` 的同步 + E1 迁移模块的 `_provision_body()/_review_body()/SEED_ROLES_FUNCTION`。
读原文 + 找不到锚点就抛错，把漂移变成迁移失败而不是静默差异（与 E1 同一写法）。

downgrade 口径：退回 E2 之前的模型 —— 审核函数与种子函数恢复 E1 派生版；触发器恢复
`b7d1e3f5a9c2` 的原文（同样按名读取，不抄）；删除新表、提交人列、`self_exception_used` 与
`CHECK`，并清掉本次按出厂默认写入的内置角色范围行。E2 之后运营改过的范围不回滚保留：
版本既然退回 E2 之前，那些配置在旧模型里不存在。
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

from app.rbac import sync_rbac_definitions_sync
from app.rbac.catalog import TENANT_MODULES
from app.settings.store import SELF_EXCEPTION_KEY

revision: str = "f7a9b1c3d5e8"
down_revision: str | None = "e5f7a9c1b3d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_E1_MIGRATION = "e5f7a9c1b3d5_m1e1_rbac_matrix.py"
_GUARD_MIGRATION = "b7d1e3f5a9c2_m1c_realm_permission_guard.py"

_REVIEW_SIGNATURE = "uuid, text, text, uuid, text, uuid, text, uuid, text, text"

# ---------------------------------------------------------------------------
# 锚点（与 E1 迁移产出的函数体逐字一致；缩进随原文）
# ---------------------------------------------------------------------------
_SEED_DECLARE_TAIL = "END\n$$;"
_SEED_STALE_COMMENT_OLD = """    -- 与启动自检 `catalog.code_realm()` 同一口径；DB 触发器 role_permissions_realm_guard
    -- 目前只硬挡 module='platform'，收紧为模块白名单见 TD-149 → M1-E2）。"""
_SEED_STALE_COMMENT_NEW = """    -- 与启动自检 `catalog.code_realm()` 同一口径；DB 触发器 role_permissions_realm_guard
    -- 自 M1-E2 起同样只允许租户域模块白名单（TD-149 已收口）。"""

_SEED_SCOPE_BLOCK = f"""

    -- 出厂默认数据范围（M1-E2 口径 ⑨）：与权限映射同一"只补缺、不覆盖"口径 ——
    -- 范围入库后属于运营配置，人工撤掉的不能被开通流程悄悄加回来。
    INSERT INTO public.data_scopes (id, tenant_id, role_id, scope_type, scope_id)
    SELECT gen_random_uuid(), p_tenant_id, r.id, d.scope_type, null
    FROM public.roles r
    JOIN public.rbac_builtin_role_scope_defaults d
      ON d.realm = 'tenant' AND d.role_code = r.code
    WHERE r.tenant_id = p_tenant_id
      AND r.is_system
      AND NOT EXISTS (
          SELECT 1 FROM public.data_scopes s WHERE s.role_id = r.id
      );
"""

_REVIEW_DECLARE_OLD = """    v_masked_email text;
BEGIN"""
_REVIEW_DECLARE_NEW = """    v_masked_email text;
    v_submitter uuid;
    v_self_exception boolean := false;
BEGIN"""

_REVIEW_GATE_OLD = """    IF p_result = 'approved' THEN"""
_REVIEW_GATE_NEW = f"""    -- RBAC-03 提交人不能自批。本函数也供 DEV 开通之外的内部调用直接使用，
    -- 不能假设调用方已经判过，所以这里再判一次；接口层的第一道判定给出干净的 403/422，
    -- `school_review_records` 上的同表 CHECK 是「没走本函数的直写」的最后一道。
    v_submitter := v_req.submitted_by_platform_user_id;
    IF v_submitter IS NOT NULL AND v_submitter = p_reviewer_id THEN
        -- 例外开关以**库里的当前值**为准（运营在平台接口改完即刻生效，不重启、不踢会话）
        SELECT coalesce((
                   SELECT s.value = 'true'
                   FROM public.system_settings s
                   WHERE s.key = '{SELF_EXCEPTION_KEY}'
               ), false)
        INTO v_self_exception;
        IF NOT v_self_exception THEN
            RETURN jsonb_build_object('error', 'SELF_APPROVAL_FORBIDDEN');
        END IF;
        IF p_reason IS NULL OR btrim(p_reason) = '' THEN
            RETURN jsonb_build_object('error', 'SELF_APPROVAL_REASON_REQUIRED');
        END IF;
    END IF;

    IF p_result = 'approved' THEN"""

_REVIEW_INSERT_APPROVED_OLD = """        INSERT INTO public.school_review_records (
            id, request_id, tenant_id, reviewer_id, result, reason,
            snapshot_object_key, reviewed_at
        ) VALUES (
            p_review_record_id, p_request_id, v_tenant_id, p_reviewer_id,
            'approved', p_reason, p_snapshot_key, now()
        );"""
_REVIEW_INSERT_APPROVED_NEW = """        INSERT INTO public.school_review_records (
            id, request_id, tenant_id, reviewer_id, result, reason,
            snapshot_object_key, reviewed_at,
            submitted_by_platform_user_id, self_exception_used
        ) VALUES (
            p_review_record_id, p_request_id, v_tenant_id, p_reviewer_id,
            'approved', p_reason, p_snapshot_key, now(),
            v_submitter, v_self_exception
        );"""

_REVIEW_INSERT_REJECTED_OLD = """        INSERT INTO public.school_review_records (
            id, request_id, tenant_id, reviewer_id, result, reason,
            snapshot_object_key, reviewed_at
        ) VALUES (
            p_review_record_id, p_request_id, NULL, p_reviewer_id,
            'rejected', p_reason, p_snapshot_key, now()
        );"""
_REVIEW_INSERT_REJECTED_NEW = """        INSERT INTO public.school_review_records (
            id, request_id, tenant_id, reviewer_id, result, reason,
            snapshot_object_key, reviewed_at,
            submitted_by_platform_user_id, self_exception_used
        ) VALUES (
            p_review_record_id, p_request_id, NULL, p_reviewer_id,
            'rejected', p_reason, p_snapshot_key, now(),
            v_submitter, v_self_exception
        );"""


def _tenant_modules_in_list() -> str:
    """租户域模块白名单的 SQL `IN (...)` 取值串（唯一来源：`catalog.TENANT_MODULES`）。"""

    return ", ".join(f"'{module}'" for module in sorted(TENANT_MODULES))


def _load_migration(filename: str) -> object:
    """按文件名加载迁移模块（读函数体原文用；文件缺失或常量改名一律抛错）。

    异常:
        RuntimeError：无法加载该迁移文件。
    """

    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(f"alembic_source_{filename[:12]}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载函数体来源迁移：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _e1_module() -> any:
    """M1-E1 迁移模块（当前库内生效的函数体原文来源）。"""

    return _load_migration(_E1_MIGRATION)


def _replace_once(source: str, old: str, new: str, anchor: str) -> str:
    """唯一锚点替换：锚点必须恰好出现一次。

    异常:
        RuntimeError：锚点出现 0 次或多次（上游函数体被改过，必须人工确认而不是猜）。
    """

    occurrences = source.count(old)
    if occurrences != 1:
        raise RuntimeError(f"锚点 {anchor} 出现 {occurrences} 次（期望 1 次）；函数体已漂移")
    return source.replace(old, new)


def _seed_body() -> str:
    """新版 `seed_builtin_roles(uuid)`：E1 原文 + 一段范围补齐（前面的 SQL 逐字沿用）。

    异常:
        RuntimeError：E1 原文里没有预期锚点。
    """

    source = _e1_module().__dict__["SEED_ROLES_FUNCTION"]
    if not isinstance(source, str):
        raise RuntimeError(f"{_E1_MIGRATION} 缺少 SEED_ROLES_FUNCTION 常量")
    body = _replace_once(source, _SEED_STALE_COMMENT_OLD, _SEED_STALE_COMMENT_NEW, "seed 口径注释")
    return _replace_once(body, _SEED_DECLARE_TAIL, _SEED_SCOPE_BLOCK + _SEED_DECLARE_TAIL, "seed END")


def _review_body() -> str:
    """新版 `review_registration_request(...)`：在 E1 产出的函数体上做三处锚点替换。

    异常:
        RuntimeError：E1 函数体缺少预期锚点（说明开通函数被改过，需要人工核对）。
    """

    body = _e1_module().__dict__["_review_body"]()
    body = _replace_once(body, _REVIEW_DECLARE_OLD, _REVIEW_DECLARE_NEW, "review DECLARE")
    body = _replace_once(body, _REVIEW_GATE_OLD, _REVIEW_GATE_NEW, "review 分权闸门")
    body = _replace_once(
        body, _REVIEW_INSERT_APPROVED_OLD, _REVIEW_INSERT_APPROVED_NEW, "review 通过记录"
    )
    return _replace_once(
        body, _REVIEW_INSERT_REJECTED_OLD, _REVIEW_INSERT_REJECTED_NEW, "review 拒绝记录"
    )


def _realm_guard_body() -> str:
    """收紧后的 realm 守卫函数体（TD-149）：白名单来自 `catalog.TENANT_MODULES`。"""

    return f"""
CREATE OR REPLACE FUNCTION role_permissions_realm_guard() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_module text;
BEGIN
    SELECT module INTO v_module FROM public.permissions WHERE id = NEW.permission_id;
    -- 白名单而不是黑名单：`observability.*`/`eval.*` 同属平台运维域（矩阵 §3.4），
    -- 只挡 module='platform' 会给它们留空档。目录若新增租户域模块，改 `TENANT_MODULES` 即可。
    IF v_module NOT IN ({_tenant_modules_in_list()}) THEN
        RAISE EXCEPTION
            'tenant roles can only reference tenant-domain permissions (module=%, permission_id=%)',
            v_module, NEW.permission_id;
    END IF;
    RETURN NEW;
END;
$$;
"""


def _guard_legacy_body() -> str:
    """E2 之前生效的守卫原文（downgrade 用；来自 `b7d1e3f5a9c2` 的常量）。

    异常:
        RuntimeError：来源迁移缺少 `_TENANT_GUARD` 常量。
    """

    source = _load_migration(_GUARD_MIGRATION).__dict__["_TENANT_GUARD"]
    if not isinstance(source, str):
        raise RuntimeError(f"{_GUARD_MIGRATION} 缺少 _TENANT_GUARD 常量")
    return source


def _require_bypass_role() -> None:
    """SECURITY DEFINER 函数 owner 必须能绕过 FORCE RLS（与 M1-B/M1-E1 同一前提）。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles
                WHERE rolname = current_user AND (rolsuper OR rolbypassrls)
            ) THEN
                RAISE EXCEPTION 'M1-E2 requires migration role superuser or BYPASSRLS';
            END IF;
        END $$;
        """
    )


def _grant_review_execute() -> None:
    """审核函数的 EXECUTE 只给平台角色（与 M1-B/M1-E1 同一口径）。"""

    op.execute(
        f"REVOKE ALL ON FUNCTION review_registration_request({_REVIEW_SIGNATURE}) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION review_registration_request({_REVIEW_SIGNATURE}) "
        "TO campus_elf_platform"
    )


def upgrade() -> None:
    """建两张新表、加提交人列与 CHECK、切换种子/审核函数与守卫触发器、同步范围默认并校验。"""

    _require_bypass_role()

    # 1) 内置角色出厂默认数据范围（代码所有的定义表，与 E1 两张同性质）
    op.create_table(
        "rbac_builtin_role_scope_defaults",
        sa.Column("realm", sa.String(length=16), nullable=False),
        sa.Column("role_code", sa.String(length=64), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint(
            "realm", "role_code", "scope_type", name="pk_rbac_builtin_role_scope_defaults"
        ),
        sa.CheckConstraint("realm = 'tenant'", name="ck_rbac_builtin_role_scope_defaults_realm_only_tenant"),
        sa.CheckConstraint(
            "scope_type IN ('school', 'class', 'course', 'self')",
            name="ck_rbac_builtin_role_scope_defaults_scope_type_valid",
        ),
    )

    # 2) 平台级系统设置：只有平台连接可读写（租户连接没有任何授权）
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.String(length=32), nullable=False),
        sa.Column("updated_by", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("key", name="pk_system_settings"),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["platform_users.id"],
            name="fk_system_settings_updated_by_platform_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "reason IS NOT NULL AND length(btrim(reason)) > 0",
            name="ck_system_settings_reason_required",
        ),
        sa.CheckConstraint(
            "length(value) BETWEEN 1 AND 32", name="ck_system_settings_value_length"
        ),
    )

    for role_name in ("campus_elf_tenant", "campus_elf_platform"):
        op.execute(f"GRANT SELECT ON rbac_builtin_role_scope_defaults TO {role_name}")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON system_settings TO campus_elf_platform"
    )

    # 3) RBAC-03 的判据列与硬闸门
    op.add_column(
        "tenant_registration_requests",
        sa.Column("submitted_by_platform_user_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_tenant_reg_requests_submitted_by_platform_users",
        "tenant_registration_requests",
        "platform_users",
        ["submitted_by_platform_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column(
        "school_review_records",
        sa.Column("submitted_by_platform_user_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "school_review_records",
        sa.Column(
            "self_exception_used", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.create_check_constraint(
        "no_self_approval_without_exception",
        "school_review_records",
        "submitted_by_platform_user_id IS NULL"
        " OR reviewer_id <> submitted_by_platform_user_id"
        " OR self_exception_used IS TRUE",
    )

    # 4) 函数与触发器（全部由上游原文派生，锚点不唯一就抛错）
    op.execute(_seed_body())
    op.execute("REVOKE ALL ON FUNCTION public.seed_builtin_roles(uuid) FROM PUBLIC")
    op.execute(_review_body())
    _grant_review_execute()
    op.execute(_realm_guard_body())

    # 收紧触发器前先确认存量数据已经符合白名单（E1 的同步保证这一点；不符就 fail-fast，
    # 而不是让触发器在明天某次授权时才炸）
    op.execute(
        f"""
        DO $$
        DECLARE
            v_bad text;
        BEGIN
            SELECT p.module || '/' || p.code INTO v_bad
            FROM public.role_permissions rp
            JOIN public.permissions p ON p.id = rp.permission_id
            WHERE p.module NOT IN ({_tenant_modules_in_list()})
            LIMIT 1;
            IF v_bad IS NOT NULL THEN
                RAISE EXCEPTION '存量租户角色已引用租户域外权限（%），先按矩阵改授再迁移', v_bad;
            END IF;
        END $$;
        """
    )

    # 5) 目录 + 定义表 + 内置角色映射 + 默认数据范围（幂等；与集成测试执行同一份语句）
    sync_rbac_definitions_sync(op.get_bind())

    # 6) 收尾校验：升级即生效的口径是「管理员看得到全校」，
    #    所以每个租户至少要有一条 school 范围落在内置角色上（缺了就是补写没跑）
    op.execute(
        """
        DO $$
        DECLARE
            v_missing text;
        BEGIN
            SELECT s.missing INTO v_missing
            FROM (
                SELECT t.id::text AS missing
                FROM public.tenants t
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM public.data_scopes s
                    JOIN public.roles r ON r.id = s.role_id AND r.tenant_id = s.tenant_id
                    WHERE s.tenant_id = t.id AND r.is_system AND s.scope_type = 'school'
                )
                LIMIT 1
            ) s;
            IF v_missing IS NOT NULL THEN
                RAISE EXCEPTION 'M1-E2 后仍有租户的内置角色缺少 school 范围：%s', v_missing;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    """恢复 E1 的种子/审核函数与 M1-C 的守卫原文，删除 E2 的表、列与约束。"""

    e1 = _e1_module()
    op.execute(e1.__dict__["SEED_ROLES_FUNCTION"])
    op.execute("REVOKE ALL ON FUNCTION public.seed_builtin_roles(uuid) FROM PUBLIC")
    op.execute(e1.__dict__["_review_body"]())
    _grant_review_execute()

    op.execute(_guard_legacy_body())

    # 内置角色的默认映射与提交人列（审核记录仍保留，只丢掉 E2 加的判据列）
    op.drop_constraint(
        "no_self_approval_without_exception", "school_review_records", type_="check"
    )
    op.drop_column("school_review_records", "self_exception_used")
    op.drop_column("school_review_records", "submitted_by_platform_user_id")
    op.drop_constraint(
        "fk_tenant_reg_requests_submitted_by_platform_users",
        "tenant_registration_requests",
        type_="foreignkey",
    )
    op.drop_column("tenant_registration_requests", "submitted_by_platform_user_id")

    # 本次按出厂默认写入的范围行：退回 E2 之前等于「库里没有范围配置」（TD-138 的原状态）。
    # 只删内置（is_system）角色上的行——运营自建角色上的范围是 E2 之后的人工作证，不动。
    op.execute(
        """
        DELETE FROM public.data_scopes s
        USING public.roles r
        WHERE r.id = s.role_id AND r.is_system
        """
    )

    for role_name in ("campus_elf_tenant", "campus_elf_platform"):
        op.execute(f"REVOKE SELECT ON rbac_builtin_role_scope_defaults FROM {role_name}")
    op.execute("REVOKE ALL ON system_settings FROM campus_elf_platform")
    op.drop_table("system_settings")
    op.drop_table("rbac_builtin_role_scope_defaults")
