"""M1-C2 会话设备：最近使用时间、设备标签与撤销原因（2026-10-21）。

- 两张刷新令牌表增加 `last_seen_at`（最久未用排序）与 `device_label`（粗粒度 UA 标签；
  明文 UA/IP 仍只存哈希，不新增 PII 面）。
- 存量行 `last_seen_at = issued_at`，避免升级后排序出现 NULL。
- `revoked_reason` 增加 `session_kicked`（超限踢出）与 `session_revoked`（用户下线单台/
  退出其它设备），与旧值并存。

回滚：会话类撤销原因归一化为 `logout`（旧约束不含新值），`rotated` 保留
（down_revision 的约束已允许），再删除新列。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e6f8a0b2c1"
down_revision: str | None = "c8e2f4a6b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_REASONS = (
    "'logout', 'password_change', 'reuse_detected', 'rotated', 'admin_revoke', "
    "'account_deleted', 'expired', 'session_kicked', 'session_revoked'"
)
_PLATFORM_REASONS = _TENANT_REASONS
_OLD_TENANT_REASONS = (
    "'logout', 'password_change', 'reuse_detected', 'rotated', 'admin_revoke', "
    "'account_deleted', 'expired'"
)


def upgrade() -> None:
    """新增会话元数据列并扩展撤销原因。"""

    for table in ("refresh_tokens", "platform_refresh_tokens"):
        op.add_column(
            table, sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True)
        )
        op.add_column(table, sa.Column("device_label", sa.String(length=64), nullable=True))
        op.execute(f"UPDATE {table} SET last_seen_at = issued_at WHERE last_seen_at IS NULL")
        op.alter_column(table, "last_seen_at", nullable=False)

    op.execute("ALTER TABLE refresh_tokens DROP CONSTRAINT ck_refresh_tokens_revoked_reason_valid")
    op.execute(
        "ALTER TABLE refresh_tokens ADD CONSTRAINT ck_refresh_tokens_revoked_reason_valid "
        f"CHECK (revoked_reason IS NULL OR revoked_reason IN ({_TENANT_REASONS}))"
    )
    op.execute(
        "ALTER TABLE platform_refresh_tokens "
        "DROP CONSTRAINT ck_platform_refresh_tokens_revoked_reason_valid"
    )
    op.execute(
        "ALTER TABLE platform_refresh_tokens "
        "ADD CONSTRAINT ck_platform_refresh_tokens_revoked_reason_valid "
        f"CHECK (revoked_reason IS NULL OR revoked_reason IN ({_PLATFORM_REASONS}))"
    )


def downgrade() -> None:
    """回滚：会话撤销原因归一化后删除新列。"""

    op.execute(
        "UPDATE refresh_tokens SET revoked_reason = 'logout' "
        "WHERE revoked_reason IN ('session_kicked', 'session_revoked')"
    )
    op.execute(
        "UPDATE platform_refresh_tokens SET revoked_reason = 'logout' "
        "WHERE revoked_reason IN ('session_kicked', 'session_revoked')"
    )
    op.execute("ALTER TABLE refresh_tokens DROP CONSTRAINT ck_refresh_tokens_revoked_reason_valid")
    op.execute(
        "ALTER TABLE refresh_tokens ADD CONSTRAINT ck_refresh_tokens_revoked_reason_valid "
        f"CHECK (revoked_reason IS NULL OR revoked_reason IN ({_OLD_TENANT_REASONS}))"
    )
    op.execute(
        "ALTER TABLE platform_refresh_tokens "
        "DROP CONSTRAINT ck_platform_refresh_tokens_revoked_reason_valid"
    )
    op.execute(
        "ALTER TABLE platform_refresh_tokens "
        "ADD CONSTRAINT ck_platform_refresh_tokens_revoked_reason_valid "
        f"CHECK (revoked_reason IS NULL OR revoked_reason IN ({_OLD_TENANT_REASONS}))"
    )

    for table in ("refresh_tokens", "platform_refresh_tokens"):
        op.alter_column(table, "last_seen_at", nullable=True)
        op.drop_column(table, "device_label")
        op.drop_column(table, "last_seen_at")
