"""M1-D1 密码策略/历史/重置与登录失败锁定（2026-10-22）。

- `users` / `platform_users`：重置收件邮箱、失败计数窗口与锁定截止时间；
- `password_history` / `platform_password_history`：最近 5 次旧密码哈希；
- `password_reset_tokens` / `platform_password_reset_tokens`：SHA-256 令牌、单次使用；
- 校方管理员邮箱回填 `login_id`，并由 BEFORE INSERT 触发器为新建校管自动写入
  （开通走 SECURITY DEFINER 函数，平台运行角色无 users 写权限，触发器是唯一统一入口）；
- 租户新表强制 RLS + 租户策略 + 最小授权；平台表只授予平台连接。

回滚删除新增表/列/触发器。密码历史与未使用重置令牌属于短期安全数据，
回滚时丢弃；旧版本没有对应功能，不会产生"降级后仍可重置"的窗口。
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2d4f6a8c0e1"
down_revision: str | None = "d4e6f8a0b2c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 M1-A 完全一致的 fail-closed 表达式：空串/非法 UUID 退化为 NULL，读 0 行
_TENANT_EXPR = (
    "tenant_id = case when pg_input_is_valid("
    "current_setting('app.tenant_id', true), 'uuid') "
    "then current_setting('app.tenant_id', true)::uuid else null end"
)

# 重置成功撤销全部会话时使用 password_reset；回滚恢复 M1-C2 的白名单
_REFRESH_REASONS = (
    "'logout', 'password_change', 'reuse_detected', 'rotated', 'admin_revoke', "
    "'account_deleted', 'expired', 'session_kicked', 'session_revoked', 'password_reset'"
)
_OLD_REFRESH_REASONS = (
    "'logout', 'password_change', 'reuse_detected', 'rotated', 'admin_revoke', "
    "'account_deleted', 'expired', 'session_kicked', 'session_revoked'"
)


def _uuid_column(name: str, *, nullable: bool = False, server_default: Any = None) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        nullable=nullable,
        server_default=server_default,
    )


def _add_user_columns(table: str) -> None:
    """为租户/平台用户表增加邮箱与锁定字段。"""

    op.add_column(table, sa.Column("email", sa.String(length=254), nullable=True))
    op.add_column(
        table,
        sa.Column(
            "failed_login_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        table,
        sa.Column("failed_login_window_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(table, sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_failed_login_attempts_nonnegative "
        "CHECK (failed_login_attempts >= 0)"
    )


def _create_tenant_password_history() -> None:
    """创建租户密码历史表并启用 RLS。"""

    op.create_table(
        "password_history",
        _uuid_column("id", server_default=sa.text("gen_random_uuid()")),
        _uuid_column("tenant_id"),
        _uuid_column("user_id"),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_password_history")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_password_history_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_password_history_user_id_tenant_id_users",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        op.f("ix_password_history_tenant_id"),
        "password_history",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_password_history_tenant_id_user_id",
        "password_history",
        ["tenant_id", "user_id", "created_at"],
        unique=False,
    )
    op.execute("ALTER TABLE password_history ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE password_history FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON password_history TO campus_elf_tenant "
        f"USING ({_TENANT_EXPR}) WITH CHECK ({_TENANT_EXPR})"
    )
    op.execute("GRANT SELECT, INSERT, DELETE ON password_history TO campus_elf_tenant")


def _create_platform_password_history() -> None:
    """创建平台密码历史表（平台连接专用）。"""

    op.create_table(
        "platform_password_history",
        _uuid_column("id", server_default=sa.text("gen_random_uuid()")),
        _uuid_column("platform_user_id"),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_password_history")),
        sa.ForeignKeyConstraint(
            ["platform_user_id"],
            ["platform_users.id"],
            name="fk_platform_password_history_platform_user_id_platform_users",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_platform_password_history_user_created_at",
        "platform_password_history",
        ["platform_user_id", "created_at"],
        unique=False,
    )
    op.execute("GRANT SELECT, INSERT, DELETE ON platform_password_history TO campus_elf_platform")


def _create_tenant_reset_tokens() -> None:
    """创建租户密码重置令牌表并启用 RLS。"""

    op.create_table(
        "password_reset_tokens",
        _uuid_column("id", server_default=sa.text("gen_random_uuid()")),
        _uuid_column("tenant_id"),
        _uuid_column("user_id"),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("request_ip_hash", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_password_reset_tokens")),
        sa.UniqueConstraint("token_hash", name="uq_password_reset_tokens_token_hash"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_password_reset_tokens_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_password_reset_tokens_user_id_tenant_id_users",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        op.f("ix_password_reset_tokens_tenant_id"),
        "password_reset_tokens",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_password_reset_tokens_tenant_user_created_at",
        "password_reset_tokens",
        ["tenant_id", "user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_password_reset_tokens_expires_at",
        "password_reset_tokens",
        ["expires_at"],
        unique=False,
    )
    op.execute("ALTER TABLE password_reset_tokens ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE password_reset_tokens FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON password_reset_tokens TO campus_elf_tenant "
        f"USING ({_TENANT_EXPR}) WITH CHECK ({_TENANT_EXPR})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON password_reset_tokens TO campus_elf_tenant")


def _create_platform_reset_tokens() -> None:
    """创建平台密码重置令牌表（平台连接专用）。"""

    op.create_table(
        "platform_password_reset_tokens",
        _uuid_column("id", server_default=sa.text("gen_random_uuid()")),
        _uuid_column("platform_user_id"),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("request_ip_hash", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_password_reset_tokens")),
        sa.UniqueConstraint("token_hash", name="uq_platform_password_reset_tokens_token_hash"),
        sa.ForeignKeyConstraint(
            ["platform_user_id"],
            ["platform_users.id"],
            name="fk_platform_password_reset_tokens_user",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_platform_password_reset_tokens_user_created_at",
        "platform_password_reset_tokens",
        ["platform_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_platform_password_reset_tokens_expires_at",
        "platform_password_reset_tokens",
        ["expires_at"],
        unique=False,
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON platform_password_reset_tokens "
        "TO campus_elf_platform"
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


def _create_school_admin_email_trigger() -> None:
    """校方管理员（登录名即注册邮箱）自动写入 email；存量数据另行回填。"""

    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_school_admin_email() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.user_type = 'school_admin' AND NEW.email IS NULL
               AND position('@' in NEW.login_id) > 1 THEN
                NEW.email := NEW.login_id;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER users_school_admin_email BEFORE INSERT ON users "
        "FOR EACH ROW EXECUTE FUNCTION set_school_admin_email()"
    )


def upgrade() -> None:
    """增加密码/锁定字段、历史与重置令牌表，并回填校管邮箱。"""

    _add_user_columns("users")
    _add_user_columns("platform_users")
    op.execute(
        "UPDATE users SET email = login_id "
        "WHERE user_type = 'school_admin' AND email IS NULL "
        "AND position('@' in login_id) > 1"
    )

    _create_tenant_password_history()
    _create_platform_password_history()
    _create_tenant_reset_tokens()
    _create_platform_reset_tokens()
    _create_school_admin_email_trigger()
    _set_refresh_revoked_reasons(_REFRESH_REASONS)


def downgrade() -> None:
    """删除 D1 表、触发器与用户字段。"""

    # 旧白名单不含 password_reset：先归一化存量行，否则恢复 CHECK 会失败
    for table in ("refresh_tokens", "platform_refresh_tokens"):
        op.execute(
            f"UPDATE {table} SET revoked_reason = 'password_change' "
            "WHERE revoked_reason = 'password_reset'"
        )
    _set_refresh_revoked_reasons(_OLD_REFRESH_REASONS)
    op.execute("DROP TRIGGER IF EXISTS users_school_admin_email ON users")
    op.execute("DROP FUNCTION IF EXISTS set_school_admin_email()")

    op.drop_table("platform_password_reset_tokens")
    op.drop_table("password_reset_tokens")
    op.drop_table("platform_password_history")
    op.drop_table("password_history")

    for table in ("platform_users", "users"):
        # 约束只依赖 failed_login_attempts，随列一起删除
        op.drop_column(table, "locked_until")
        op.drop_column(table, "failed_login_window_started_at")
        op.drop_column(table, "failed_login_attempts")
        op.drop_column(table, "email")
