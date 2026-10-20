"""M1-B 第四轮硬化：让已应用 d9 的环境也能获得函数/权限修复（2026-10-20）。

背景：第三/四轮的修复（`public.` 限定防 TEMP 遮蔽、`provision_tenant` 撤销 PUBLIC/平台
EXECUTE）曾被直接写入 `d9f4a2b7c3e1`，但该 revision 已应用于部分环境，Alembic 不会重跑。

本 revision **幂等**重放硬化：
1. `CREATE OR REPLACE provision_tenant(...)` 全限定 schema，创建后**无条件**撤销 PUBLIC 与
   平台的 EXECUTE（旧 schema 上函数不存在时，CREATE OR REPLACE 默认会给 PUBLIC 授权）；
2. `CREATE OR REPLACE review_registration_request(...)` 全限定 schema，仅供平台 EXECUTE；
3. 兜底收回身份表的任何写权限并删除遗留的 `platform_provision` 策略。

downgrade 为空操作：本 revision 只重放与 `d9f4a2b7c3e1` 完全相同的目标状态，不改变 schema 语义。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e5c9d1f7a2b4"
down_revision: str | None = "d9f4a2b7c3e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 d9 相同的函数体（此处内联，保证迁移文件自包含、可独立重放）
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
    """本 revision 同样依赖 SECURITY DEFINER 函数 owner 可绕过 FORCE RLS。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles
                WHERE rolname = current_user AND (rolsuper OR rolbypassrls)
            ) THEN
                RAISE EXCEPTION
                    'M1-B hardening2 requires migration role superuser or BYPASSRLS';
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    """幂等重放函数硬化与最小权限（含旧 schema 的补撤销）。"""

    _require_bypass_role()
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

    for table in ("roles", "users", "user_roles", "role_permissions"):
        op.execute(f"REVOKE ALL ON {table} FROM campus_elf_platform")
        op.execute(f"DROP POLICY IF EXISTS platform_provision ON {table}")

    # 幂等兜底：列宽、触发器、tenants 列级授权（防止旧 d9 环境存在 drift）
    op.execute("ALTER TABLE users ALTER COLUMN login_id TYPE varchar(254)")
    op.execute("ALTER TABLE users ALTER COLUMN display_name TYPE varchar(200)")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON tenants FROM campus_elf_platform")
    op.execute("GRANT UPDATE (status, status_reason) ON tenants TO campus_elf_platform")
    op.execute("REVOKE UPDATE, DELETE ON tenant_registration_requests FROM campus_elf_platform")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON school_review_records FROM campus_elf_platform")
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
    # 触发器函数不可被普通查询调用，仍统一收回 PUBLIC 做防御性硬化
    for function in (
        "audit_logs_append_only()",
        "school_review_records_append_only()",
        "tenant_registration_requests_status_guard()",
    ):
        op.execute(
            f"""
            DO $$
            BEGIN
                IF to_regprocedure('{function}') IS NOT NULL THEN
                    EXECUTE 'REVOKE ALL ON FUNCTION {function} FROM PUBLIC';
                END IF;
            END $$;
            """
        )


def downgrade() -> None:
    """空操作：本 revision 仅重放 d9 的目标状态，不引入新的 schema 语义。"""
