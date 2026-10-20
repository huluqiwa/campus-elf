"""M1-B 复审硬化：审核原子化、平台最小权限、旧 schema 补齐（2026-10-20）。

背景：第一轮 `c688c44dec81` 已被部分环境应用，原地修改 revision 不会重新执行。
本 revision 以**幂等 DDL** 补齐以下硬化，既适用于旧 schema，也适用于全新库：

1. 审核（通过/拒绝）整体收进 `review_registration_request()`（SECURITY DEFINER）：
   锁定申请 → 校验 pending → 创建租户/角色/超管（通过时）→ 审核记录 → 审计 → 状态迁移；
   平台角色只保留 SELECT（申请单另有 INSERT 用于提交）与函数 EXECUTE。
2. 收回平台对 `tenants` 的 INSERT/UPDATE/DELETE（仅保留 SELECT + 列级 `status/status_reason` 更新）。
3. 收回平台对 `users/roles/user_roles/role_permissions` 的全部权限、对申请单的 UPDATE、
   对审核记录的 INSERT/UPDATE/DELETE；`provision_tenant()` 不再对平台开放。
4. 审核记录仅追加触发器、申请状态机触发器、`users.login_id/display_name` 加宽（旧 schema 补齐）。
5. SECURITY DEFINER 要求迁移角色为超级用户或 BYPASSRLS（FORCE RLS 下函数 owner 必须可绕过），
   不满足直接失败，避免生产首次审核才 500。

回滚：删除审核函数、恢复平台写权限与旧列宽；已开通租户置为 suspended（保留审计与数据）。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d9f4a2b7c3e1"
down_revision: str | None = "c688c44dec81"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PROVISION_FUNCTION = r"""
CREATE OR REPLACE FUNCTION provision_tenant(payload jsonb) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    v_tenant_id uuid := gen_random_uuid();
    v_role_super uuid := gen_random_uuid();
    v_role_admin uuid := gen_random_uuid();
    v_role_teacher uuid := gen_random_uuid();
    v_admin_id uuid := (payload->>'admin_id')::uuid;
BEGIN
    INSERT INTO public.tenants (
        id, school_code, name, status, address, email, leadership,
        qualification_ref, activated_at
    ) VALUES (
        v_tenant_id, payload->>'school_code', payload->>'name', 'active',
        payload->>'address', payload->>'email', payload->>'leadership',
        payload->>'qualification_ref', now()
    );

    INSERT INTO public.roles (id, tenant_id, code, name, is_system) VALUES
        (v_role_super, v_tenant_id, 'school_super_admin', '校方超管', true),
        (v_role_admin, v_tenant_id, 'school_admin', '校方授权管理员', true),
        (v_role_teacher, v_tenant_id, 'teacher_b_end', '教师（B 端权限）', true);

    INSERT INTO public.role_permissions (id, tenant_id, role_id, permission_id)
    SELECT gen_random_uuid(), v_tenant_id, v_role_super, p.id
    FROM public.permissions p
    WHERE p.module IN ('school', 'campus');

    INSERT INTO public.users (
        id, tenant_id, user_type, login_id, display_name, password_hash,
        status, must_change_password, mfa_enabled
    ) VALUES (
        v_admin_id, v_tenant_id, 'school_admin', payload->>'admin_login',
        payload->>'admin_display_name', payload->>'admin_password_hash',
        'active', true, false
    );

    INSERT INTO public.user_roles (id, tenant_id, user_id, role_id)
    VALUES (gen_random_uuid(), v_tenant_id, v_admin_id, v_role_super);

    RETURN v_tenant_id;
END
$$;
"""


REVIEW_FUNCTION = r"""
CREATE OR REPLACE FUNCTION review_registration_request(
    p_request_id uuid,
    p_result text,
    p_reason text,
    p_reviewer_id uuid,
    p_http_request_id text,
    p_review_record_id uuid,
    p_snapshot_key text,
    p_admin_id uuid,
    p_admin_password_hash text,
    p_school_code text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    v_req public.tenant_registration_requests%ROWTYPE;
    v_tenant_id uuid;
    v_role_super uuid;
    v_role_admin uuid;
    v_role_teacher uuid;
    v_action text;
    v_metadata jsonb;
    v_masked_email text;
BEGIN
    -- 全限定 schema：即使调用方建了同名 TEMP 表也无法遮蔽（B3）
    SELECT * INTO v_req
    FROM public.tenant_registration_requests
    WHERE id = p_request_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('error', 'REQUEST_NOT_FOUND');
    END IF;
    IF v_req.status <> 'pending' THEN
        RETURN jsonb_build_object('error', 'ALREADY_REVIEWED');
    END IF;

    IF p_http_request_id IS NOT NULL THEN
        p_http_request_id := left(p_http_request_id, 64);
    END IF;
    IF p_reason IS NOT NULL THEN
        p_reason := left(btrim(p_reason), 500);
    END IF;

    v_masked_email := case
        when position('@' in v_req.email) > 1
            then left(v_req.email, 1) || '***@' || split_part(v_req.email, '@', 2)
        else '***'
    end;

    IF p_result = 'approved' THEN
        IF p_admin_id IS NULL OR p_admin_password_hash IS NULL OR p_school_code IS NULL THEN
            RETURN jsonb_build_object('error', 'INVALID_PAYLOAD');
        END IF;

        v_tenant_id := gen_random_uuid();
        INSERT INTO public.tenants (
            id, school_code, name, status, address, email, leadership,
            qualification_ref, activated_at
        ) VALUES (
            v_tenant_id, p_school_code, v_req.school_name, 'active', v_req.address,
            v_req.email, v_req.leadership, v_req.qualification_object_key, now()
        );

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

        INSERT INTO public.users (
            id, tenant_id, user_type, login_id, display_name, password_hash,
            status, must_change_password, mfa_enabled
        ) VALUES (
            p_admin_id, v_tenant_id, 'school_admin', v_req.email,
            left(v_req.school_name || ' 管理员', 200), p_admin_password_hash,
            'active', true, false
        );

        INSERT INTO public.user_roles (id, tenant_id, user_id, role_id)
        VALUES (gen_random_uuid(), v_tenant_id, p_admin_id, v_role_super);

        INSERT INTO public.school_review_records (
            id, request_id, tenant_id, reviewer_id, result, reason,
            snapshot_object_key, reviewed_at
        ) VALUES (
            p_review_record_id, p_request_id, v_tenant_id, p_reviewer_id,
            'approved', p_reason, p_snapshot_key, now()
        );

        UPDATE public.tenant_registration_requests
        SET status = 'approved', reviewed_at = now(), tenant_id = v_tenant_id
        WHERE id = p_request_id;

        v_action := 'tenant.approve';
        v_metadata := jsonb_build_object(
            'registration_request_id', p_request_id::text,
            'school_code', p_school_code,
            'admin_login_id_masked', v_masked_email
        );
    ELSIF p_result = 'rejected' THEN
        IF p_reason IS NULL OR btrim(p_reason) = '' THEN
            RETURN jsonb_build_object('error', 'REASON_REQUIRED');
        END IF;

        INSERT INTO public.school_review_records (
            id, request_id, tenant_id, reviewer_id, result, reason,
            snapshot_object_key, reviewed_at
        ) VALUES (
            p_review_record_id, p_request_id, NULL, p_reviewer_id,
            'rejected', p_reason, p_snapshot_key, now()
        );

        UPDATE public.tenant_registration_requests
        SET status = 'rejected', reviewed_at = now(), reject_reason = p_reason
        WHERE id = p_request_id;

        v_action := 'tenant.reject';
        v_metadata := jsonb_build_object(
            'school_name', v_req.school_name,
            'contact_email_masked', v_masked_email,
            'reason_sha256', encode(sha256(convert_to(p_reason, 'UTF8')), 'hex')
        );
    ELSE
        RETURN jsonb_build_object('error', 'INVALID_RESULT');
    END IF;

    -- 审计与业务写入同一函数/事务，无法出现"开通了但没有审计"
    INSERT INTO public.audit_logs (
        event_id, tenant_id, actor_realm, actor_id, action, resource_type,
        resource_id, result, request_id, metadata_json, created_at
    ) VALUES (
        gen_random_uuid(), v_tenant_id, 'platform', p_reviewer_id, v_action,
        CASE WHEN p_result = 'approved' THEN 'tenant' ELSE 'tenant_registration_request' END,
        CASE WHEN p_result = 'approved' THEN v_tenant_id::text ELSE p_request_id::text END,
        'success', p_http_request_id, v_metadata, now()
    );

    RETURN jsonb_build_object(
        'status', p_result,
        'tenant_id', v_tenant_id,
        'school_code', p_school_code,
        'review_record_id', p_review_record_id
    );
END;
$$;
"""


def _require_bypass_role() -> None:
    """SECURITY DEFINER 函数 owner 必须能绕过 FORCE RLS（否则首次审核才失败）。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles
                WHERE rolname = current_user AND (rolsuper OR rolbypassrls)
            ) THEN
                RAISE EXCEPTION
                    'M1-B hardening requires migration role superuser or BYPASSRLS';
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    """应用硬化（对旧/新 schema 都幂等）。"""

    # 旧 schema 补齐列宽
    op.execute("ALTER TABLE users ALTER COLUMN login_id TYPE varchar(254)")
    op.execute("ALTER TABLE users ALTER COLUMN display_name TYPE varchar(200)")

    # 旧 schema 的平台开通策略/表级写权限一律收回
    for table in ("roles", "users", "user_roles", "role_permissions"):
        op.execute(f"DROP POLICY IF EXISTS platform_provision ON {table}")
        op.execute(f"REVOKE ALL ON {table} FROM campus_elf_platform")
    op.execute("REVOKE UPDATE, DELETE ON tenant_registration_requests FROM campus_elf_platform")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON school_review_records FROM campus_elf_platform")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON tenants FROM campus_elf_platform")
    op.execute("GRANT UPDATE (status, status_reason) ON tenants TO campus_elf_platform")

    _require_bypass_role()
    # 内部分通函数不再对外开放，但同样硬化（全限定 schema，防 TEMP 遮蔽）。
    # 注意：旧 schema 上函数不存在时 CREATE OR REPLACE 会**新建**并默认授予 PUBLIC EXECUTE，
    # 因此撤销必须放在创建之后且无条件执行（B4）。
    op.execute(PROVISION_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION provision_tenant(jsonb) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION provision_tenant(jsonb) FROM campus_elf_platform")
    op.execute(REVIEW_FUNCTION)
    op.execute(
        "REVOKE ALL ON FUNCTION review_registration_request("
        "uuid, text, text, uuid, text, uuid, text, uuid, text, text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION review_registration_request("
        "uuid, text, text, uuid, text, uuid, text, uuid, text, text) TO campus_elf_platform"
    )

    # 审核记录仅追加
    op.execute(
        """
        CREATE OR REPLACE FUNCTION school_review_records_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'school_review_records is append-only (attempted %)', TG_OP;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("DROP TRIGGER IF EXISTS school_review_records_no_update_delete ON school_review_records")
    op.execute(
        """
        CREATE TRIGGER school_review_records_no_update_delete
        BEFORE UPDATE OR DELETE ON school_review_records
        FOR EACH ROW EXECUTE FUNCTION school_review_records_append_only()
        """
    )

    # 申请状态机
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tenant_registration_requests_status_guard()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.status <> 'pending' THEN
                RAISE EXCEPTION 'registration request already reviewed (status=%)', OLD.status;
            END IF;
            IF NEW.status NOT IN ('approved', 'rejected') THEN
                RAISE EXCEPTION 'invalid registration status transition: % -> %', OLD.status, NEW.status;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS tenant_registration_requests_status_guard "
        "ON tenant_registration_requests"
    )
    op.execute(
        """
        CREATE TRIGGER tenant_registration_requests_status_guard
        BEFORE UPDATE ON tenant_registration_requests
        FOR EACH ROW EXECUTE FUNCTION tenant_registration_requests_status_guard()
        """
    )


def downgrade() -> None:
    """回滚硬化：删除审核函数并恢复第一轮授权；已开通租户置为 suspended。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF to_regprocedure(
                'review_registration_request(uuid, text, text, uuid, text, uuid, text, uuid, text, text)'
            ) IS NOT NULL THEN
                EXECUTE 'DROP FUNCTION review_registration_request(uuid, text, text, uuid, text, uuid, text, uuid, text, text)';
            END IF;
        END $$;
        """
    )

    # 精确恢复 c688c44dec81 的**规范状态**（B5）：
    # - 身份表（users/roles/user_roles/role_permissions）不给平台任何权限，也没有开通策略
    #   （避免把 B1"平台可向存量租户植入账号"的路径带回来）；
    # - 申请单恢复 SELECT/INSERT/UPDATE，审核记录恢复 SELECT/INSERT，tenants 恢复 M1-A 的 DML；
    # - 内部分通函数恢复平台 EXECUTE（c688 当时确实授权，这是历史状态而非新增能力）。
    for table in ("roles", "users", "user_roles", "role_permissions"):
        op.execute(f"REVOKE ALL ON {table} FROM campus_elf_platform")
        op.execute(f"DROP POLICY IF EXISTS platform_provision ON {table}")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON tenants TO campus_elf_platform")
    op.execute("GRANT SELECT, INSERT, UPDATE ON tenant_registration_requests TO campus_elf_platform")
    op.execute("GRANT SELECT, INSERT ON school_review_records TO campus_elf_platform")
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regprocedure('provision_tenant(jsonb)') IS NOT NULL THEN
                EXECUTE 'GRANT EXECUTE ON FUNCTION provision_tenant(jsonb) TO campus_elf_platform';
            END IF;
        END $$;
        """
    )

    # 降级后不再有超管账号：保留租户数据但停用，避免"active 但无人可登录"
    op.execute(
        """
        UPDATE tenants SET status = 'suspended', status_reason = 'M1-B downgrade'
        WHERE id IN (
            SELECT tenant_id FROM tenant_registration_requests WHERE tenant_id IS NOT NULL
        )
        """
    )
    # 列宽恢复由 c688c44dec81 的 downgrade 在删除 school_admin 账号后执行
