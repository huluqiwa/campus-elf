"""M1-C 认证：平台刷新令牌表 + 轮换原因（2026-10-21）。

- 新增 `platform_refresh_tokens`：平台 realm 的刷新令牌（`refresh_tokens` 只服务租户用户，
  带 `tenant_id` 且复合外键指向 `users`，不能承载平台账号）。
- `refresh_tokens.revoked_reason` 的取值增加 `rotated`：正常轮换时旧令牌标记为"已轮换"，
  与"被撤销"区分开，便于重用检测与排障。

权限：平台表只授权 `campus_elf_platform`（租户角色显式回收）；平台表无 tenant_id，不需要 RLS。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e5c9d1f7a2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REVOKED_REASONS = (
    "'logout', 'password_change', 'reuse_detected', 'rotated', "
    "'admin_revoke', 'account_deleted', 'expired'"
)


def upgrade() -> None:
    """建平台刷新令牌表并扩展轮换原因取值。"""

    op.create_table(
        "platform_refresh_tokens",
        sa.Column("jti", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("platform_user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_to_jti", sa.UUID(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=32), nullable=True),
        sa.Column("ua_hash", sa.String(length=64), nullable=True),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            f"revoked_reason IS NULL OR revoked_reason IN ({_REVOKED_REASONS})",
            name=op.f("ck_platform_refresh_tokens_revoked_reason_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["platform_user_id"],
            ["platform_users.id"],
            name=op.f("fk_platform_refresh_tokens_platform_user_id_platform_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_refresh_tokens")),
        sa.UniqueConstraint("jti", name=op.f("uq_platform_refresh_tokens_jti")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_platform_refresh_tokens_token_hash")),
    )
    op.create_index(
        op.f("ix_platform_refresh_tokens_family_id"),
        "platform_refresh_tokens",
        ["family_id"],
    )
    op.create_index(
        op.f("ix_platform_refresh_tokens_platform_user_id"),
        "platform_refresh_tokens",
        ["platform_user_id"],
    )
    op.create_index(
        op.f("ix_platform_refresh_tokens_expires_at"),
        "platform_refresh_tokens",
        ["expires_at"],
    )

    op.execute(
        "REVOKE ALL ON platform_refresh_tokens FROM campus_elf_tenant"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON platform_refresh_tokens TO campus_elf_platform"
    )

    # 轮换原因：旧令牌标记 rotated（区别于"被撤销"）
    op.execute("ALTER TABLE refresh_tokens DROP CONSTRAINT ck_refresh_tokens_revoked_reason_valid")
    op.execute(
        "ALTER TABLE refresh_tokens ADD CONSTRAINT ck_refresh_tokens_revoked_reason_valid "
        f"CHECK (revoked_reason IS NULL OR revoked_reason IN ({_REVOKED_REASONS}))"
    )


def downgrade() -> None:
    """回滚：删除平台刷新令牌表并还原轮换原因取值。

    回滚前先把 `rotated` 归一化为 `revoked_at` 已存在、`revoked_reason` 置空
    （旧 CHECK 不含 rotated），否则存量轮换行会让 ADD CONSTRAINT 失败。
    """

    op.execute(
        "UPDATE refresh_tokens SET revoked_reason = NULL WHERE revoked_reason = 'rotated'"
    )
    op.execute("ALTER TABLE refresh_tokens DROP CONSTRAINT ck_refresh_tokens_revoked_reason_valid")
    op.execute(
        "ALTER TABLE refresh_tokens ADD CONSTRAINT ck_refresh_tokens_revoked_reason_valid "
        "CHECK (revoked_reason IS NULL OR revoked_reason IN "
        "('logout', 'password_change', 'reuse_detected', 'admin_revoke', "
        "'account_deleted', 'expired'))"
    )
    op.drop_index(
        op.f("ix_platform_refresh_tokens_expires_at"), table_name="platform_refresh_tokens"
    )
    op.drop_index(
        op.f("ix_platform_refresh_tokens_platform_user_id"),
        table_name="platform_refresh_tokens",
    )
    op.drop_index(op.f("ix_platform_refresh_tokens_family_id"), table_name="platform_refresh_tokens")
    op.drop_table("platform_refresh_tokens")
