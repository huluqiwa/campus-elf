"""M1-D2 TOTP MFA 与恢复码（2026-10-22）。

- `users` / `platform_users`：`mfa_last_used_step`（防 TOTP 重放）与一致性 CHECK；
- `refresh_tokens` / `platform_refresh_tokens`：`mfa_verified`（强制策略下的刷新校验）
  与新的 `mfa_change` 撤销原因（启用/关闭 MFA 轮换全部会话）；
- `mfa_recovery_codes`（租户，RLS）与 `platform_mfa_recovery_codes`（平台）：
  只存 HMAC 摘要，一次性消费。

回滚删除新增表/列并恢复旧撤销原因白名单；恢复前把 `mfa_change` 归一化为
`admin_revoke`，否则旧 CHECK 会拒绝存量行。TOTP secret 列沿用 M1-A 已有字段，
回滚不删除（旧代码不读取，不产生可用入口）。
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4d6e8f0a2b3"
down_revision: str | None = "b2d4f6a8c0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_EXPR = (
    "tenant_id = case when pg_input_is_valid("
    "current_setting('app.tenant_id', true), 'uuid') "
    "then current_setting('app.tenant_id', true)::uuid else null end"
)

_REFRESH_REASONS = (
    "'logout', 'password_change', 'reuse_detected', 'rotated', 'admin_revoke', "
    "'account_deleted', 'expired', 'session_kicked', 'session_revoked', "
    "'password_reset', 'mfa_change'"
)
_OLD_REFRESH_REASONS = (
    "'logout', 'password_change', 'reuse_detected', 'rotated', 'admin_revoke', "
    "'account_deleted', 'expired', 'session_kicked', 'session_revoked', 'password_reset'"
)


def _uuid_column(name: str, *, nullable: bool = False, server_default: Any = None) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        nullable=nullable,
        server_default=server_default,
    )


def _add_user_mfa_columns(table: str) -> None:
    """为用户表增加 TOTP 重放水位与一致性约束。"""

    op.add_column(table, sa.Column("mfa_last_used_step", sa.BigInteger(), nullable=True))
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_mfa_secret_required_when_enabled "
        "CHECK (NOT mfa_enabled OR mfa_secret_enc IS NOT NULL)"
    )
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_mfa_last_used_step_nonnegative "
        "CHECK (mfa_last_used_step IS NULL OR mfa_last_used_step >= 0)"
    )


def _add_refresh_mfa_columns(table: str) -> None:
    """刷新令牌记录会话是否完成过 MFA（旧会话默认 false）。"""

    op.add_column(
        table,
        sa.Column(
            "mfa_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def _set_refresh_revoked_reasons(reasons: str) -> None:
    """改写两张刷新令牌表的撤销原因白名单。"""

    for table, constraint in (
        ("refresh_tokens", "ck_refresh_tokens_revoked_reason_valid"),
        ("platform_refresh_tokens", "ck_platform_refresh_tokens_revoked_reason_valid"),
    ):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {constraint} "
            f"CHECK (revoked_reason IS NULL OR revoked_reason IN ({reasons}))"
        )


def _create_tenant_recovery_codes() -> None:
    """创建租户恢复码表：RLS + 最小授权。"""

    op.create_table(
        "mfa_recovery_codes",
        _uuid_column("id", server_default=sa.text("gen_random_uuid()")),
        _uuid_column("tenant_id"),
        _uuid_column("user_id"),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mfa_recovery_codes")),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "code_hash",
            name="uq_mfa_recovery_codes_tenant_user_hash",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_mfa_recovery_codes_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_mfa_recovery_codes_user_id_tenant_id_users",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        op.f("ix_mfa_recovery_codes_tenant_id"),
        "mfa_recovery_codes",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_mfa_recovery_codes_tenant_user_created_at",
        "mfa_recovery_codes",
        ["tenant_id", "user_id", "created_at"],
        unique=False,
    )
    op.execute("ALTER TABLE mfa_recovery_codes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mfa_recovery_codes FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON mfa_recovery_codes TO campus_elf_tenant "
        f"USING ({_TENANT_EXPR}) WITH CHECK ({_TENANT_EXPR})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON mfa_recovery_codes TO campus_elf_tenant")


def _create_platform_recovery_codes() -> None:
    """创建平台恢复码表（平台连接专用）。"""

    op.create_table(
        "platform_mfa_recovery_codes",
        _uuid_column("id", server_default=sa.text("gen_random_uuid()")),
        _uuid_column("platform_user_id"),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_mfa_recovery_codes")),
        sa.UniqueConstraint(
            "platform_user_id",
            "code_hash",
            name="uq_platform_mfa_recovery_codes_user_hash",
        ),
        sa.ForeignKeyConstraint(
            ["platform_user_id"],
            ["platform_users.id"],
            name="fk_platform_mfa_recovery_codes_platform_user_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_platform_mfa_recovery_codes_user_created_at",
        "platform_mfa_recovery_codes",
        ["platform_user_id", "created_at"],
        unique=False,
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON platform_mfa_recovery_codes TO campus_elf_platform"
    )


def _assert_can_bypass_rls() -> None:
    """迁移连接必须能看全所有租户行，否则下面的存量校验会被 RLS 遮蔽成 0 行。

    `users` 已 FORCE ROW LEVEL SECURITY，普通 DDL 角色即使拥有表也匹配不到策略；
    部署要求 `MIGRATION_DATABASE_URL` 使用超级用户或 BYPASSRLS 角色（docs/deployment.md §6.6），
    这里把它变成显式报错，而不是让新 CHECK 以难以理解的约束冲突失败。
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
            "迁移连接角色既非超级用户也无 BYPASSRLS，无法校验全部租户的 MFA 存量数据；"
            "请改用 MIGRATION_DATABASE_URL（见 docs/deployment.md §6.6）"
        )


def _assert_no_enabled_mfa_without_secret() -> None:
    """存量数据必须满足新 CHECK；否则迁移失败并提示先修复。"""

    _assert_can_bypass_rls()
    bind = op.get_bind()
    for table in ("users", "platform_users"):
        count = bind.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE mfa_enabled AND mfa_secret_enc IS NULL")
        ).scalar_one()
        if count:
            raise RuntimeError(
                f"{table} 有 {count} 行 mfa_enabled=true 但缺少 mfa_secret_enc，"
                "请先修复数据再执行迁移"
            )


def upgrade() -> None:
    """增加 MFA 字段、恢复码表与撤销原因。"""

    _assert_no_enabled_mfa_without_secret()
    _add_user_mfa_columns("users")
    _add_user_mfa_columns("platform_users")
    _add_refresh_mfa_columns("refresh_tokens")
    _add_refresh_mfa_columns("platform_refresh_tokens")
    _create_tenant_recovery_codes()
    _create_platform_recovery_codes()
    _set_refresh_revoked_reasons(_REFRESH_REASONS)


def downgrade() -> None:
    """删除 D2 表/列并恢复旧撤销原因白名单。"""

    # 旧白名单不含 mfa_change：先归一化存量行，否则恢复 CHECK 会失败
    for table in ("refresh_tokens", "platform_refresh_tokens"):
        op.execute(
            f"UPDATE {table} SET revoked_reason = 'admin_revoke' "
            "WHERE revoked_reason = 'mfa_change'"
        )
    _set_refresh_revoked_reasons(_OLD_REFRESH_REASONS)

    op.drop_table("platform_mfa_recovery_codes")
    op.drop_table("mfa_recovery_codes")

    for table in ("refresh_tokens", "platform_refresh_tokens"):
        op.drop_column(table, "mfa_verified")

    for table in ("platform_users", "users"):
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT ck_{table}_mfa_secret_required_when_enabled"
        )
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT ck_{table}_mfa_last_used_step_nonnegative")
        op.drop_column(table, "mfa_last_used_step")
