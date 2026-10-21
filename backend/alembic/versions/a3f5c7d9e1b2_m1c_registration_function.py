"""M1-C：公开注册提交收进 SECURITY DEFINER 函数（TD-094 完成）。

背景：公开注册与平台审核共用同一条平台连接。M1-B 后平台凭据已可真实登录，若不收口，
平台凭据可绕过公开接口的字段/对象校验直接 INSERT 申请单进入审核队列（伪造申请）。

本 revision：
1. 新增 `submit_tenant_registration(...)`：函数内做邮箱去重、落库、写审计并返回 jsonb；
2. 撤销平台对 `tenant_registration_requests` 的 INSERT（保留 SELECT），公开路径只能走函数；
3. 函数仅授予 `campus_elf_platform`，PUBLIC/租户角色不可执行。

回滚：删除函数并恢复平台 INSERT 授权。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a3f5c7d9e1b2"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SUBMIT_FUNCTION = r"""
CREATE OR REPLACE FUNCTION submit_tenant_registration(
    p_id uuid,
    p_school_name text,
    p_address text,
    p_leadership text,
    p_email text,
    p_object_key text,
    p_sha256 text,
    p_size integer,
    p_mime text,
    p_duplicate_hint boolean,
    p_http_request_id text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    v_email text := lower(btrim(p_email));
    v_masked text;
BEGIN
    IF p_http_request_id IS NOT NULL THEN
        p_http_request_id := left(p_http_request_id, 64);
    END IF;

    IF EXISTS (
        SELECT 1 FROM public.tenant_registration_requests
        WHERE email = v_email AND status IN ('pending', 'approved')
    ) THEN
        RETURN jsonb_build_object('error', 'DUPLICATE_EMAIL');
    END IF;

    INSERT INTO public.tenant_registration_requests (
        id, school_name, address, leadership, email, qualification_object_key,
        qualification_sha256, qualification_size, qualification_mime, status,
        school_name_duplicate_hint
    ) VALUES (
        p_id, p_school_name, p_address, p_leadership, v_email, p_object_key,
        p_sha256, p_size, p_mime, 'pending', coalesce(p_duplicate_hint, false)
    );

    v_masked := case
        when position('@' in v_email) > 1
            then left(v_email, 1) || '***@' || split_part(v_email, '@', 2)
        else '***'
    end;

    INSERT INTO public.audit_logs (
        event_id, actor_realm, action, resource_type, resource_id, result,
        request_id, metadata_json, created_at
    ) VALUES (
        gen_random_uuid(), 'system', 'tenant.register.submitted',
        'tenant_registration_request', p_id::text, 'success', p_http_request_id,
        jsonb_build_object(
            'school_name', p_school_name,
            'duplicate_hint', coalesce(p_duplicate_hint, false),
            'qualification_sha256', p_sha256,
            'contact_email_masked', v_masked
        ), now()
    );

    RETURN jsonb_build_object('request_id', p_id, 'status', 'pending');
END;
$$;
"""


def upgrade() -> None:
    """创建注册函数并收回平台直接写申请单的权限。"""

    op.execute(SUBMIT_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION submit_tenant_registration(uuid, text, text, text, text, text, text, integer, text, boolean, text) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION submit_tenant_registration("
        "uuid, text, text, text, text, text, text, integer, text, boolean, text) "
        "TO campus_elf_platform"
    )
    op.execute("REVOKE INSERT ON tenant_registration_requests FROM campus_elf_platform")
    op.execute("REVOKE ALL ON tenant_registration_requests FROM campus_elf_tenant")


def downgrade() -> None:
    """回滚：删除函数并恢复平台 INSERT。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF to_regprocedure(
                'submit_tenant_registration(uuid, text, text, text, text, text, text, '
                'integer, text, boolean, text)'
            ) IS NOT NULL THEN
                EXECUTE 'DROP FUNCTION submit_tenant_registration('
                    'uuid, text, text, text, text, text, text, integer, text, boolean, text)';
            END IF;
        END $$;
        """
    )
    op.execute("GRANT INSERT ON tenant_registration_requests TO campus_elf_platform")
