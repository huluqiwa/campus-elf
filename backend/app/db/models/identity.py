"""租户用户与刷新令牌模型（`users` / `refresh_tokens`）。

`users` 是 C 端与 B 端共用身份（学生/教师），**必须带 tenant_id**；
`refresh_tokens` 记录令牌家族与轮换链，是"复用检测撤销整族"的持久事实来源
（Redis 只做加速标记，见 `docs/design-briefs/M1-tenant-identity.md` §3.2）。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

UserType = Literal["student", "teacher", "school_admin"]
UserStatus = Literal["active", "locked", "disabled", "deleted"]


class User(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """租户内用户（学号/工号 = `login_id`）。"""

    __tablename__ = "users"
    __table_args__ = (
        # 学号/工号在租户内唯一；跨租可比对哈希但不重（跨租隐私红线）
        UniqueConstraint("tenant_id", "login_id", name="uq_users_tenant_id_login_id"),
        # 供子表做复合外键 (user_id, tenant_id)：保证父子同租（防跨租对象图嫁接）
        UniqueConstraint("id", "tenant_id", name="uq_users_id_tenant_id"),
        CheckConstraint(
            "user_type IN ('student', 'teacher', 'school_admin')",
            name="user_type_valid",
        ),
        CheckConstraint(
            "status IN ('active', 'locked', 'disabled', 'deleted')", name="status_valid"
        ),
    )

    user_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # 登录标识：学号或工号（可含字母数字）
    login_id: Mapped[str] = mapped_column(String(254), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # 无 server_default：初始密码账号必须由创建方显式置 true（fail-loud）
    must_change_password: Mapped[bool] = mapped_column(nullable=False, default=True)
    mfa_secret_enc: Mapped[str | None] = mapped_column(Text)
    mfa_enabled: Mapped[bool] = mapped_column(nullable=False, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 权限/状态变更时自增：access token 校验时比对，旧令牌立即失效
    token_version: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RefreshToken(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """刷新令牌（按 jti 记录；`family_id` 标识同一条登录链）。"""

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_family_id", "family_id"),
        # 过期清理（TD-067）与"撤销该用户全部令牌"按这两列过滤
        Index("ix_refresh_tokens_tenant_id_user_id", "tenant_id", "user_id"),
        Index("ix_refresh_tokens_expires_at", "expires_at"),
        UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
        # 撤销原因取值（轮换、登出、改密、重用、超限踢出、用户下线等）
        CheckConstraint(
            "revoked_reason IS NULL OR revoked_reason IN "
            "('logout', 'password_change', 'reuse_detected', 'rotated', 'admin_revoke', "
            "'account_deleted', 'expired', 'session_kicked', 'session_revoked')",
            name="revoked_reason_valid",
        ),
        # 复合外键：令牌只能属于同租户用户
        ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_refresh_tokens_user_id_tenant_id_users",
            ondelete="CASCADE",
        ),
    )

    jti: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, unique=True)
    family_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # 与 tenant_id 组成复合外键指向 users(id, tenant_id)
    user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # 只存令牌哈希，明文令牌不落库
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # "最久未用"排序依据：登录/刷新时写入（粗粒度，约刷新周期）
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 粗粒度设备标签（如 `Chrome / Windows`）；明文 UA 仍只存 ua_hash
    device_label: Mapped[str | None] = mapped_column(String(64))
    rotated_to_jti: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(32))
    ua_hash: Mapped[str | None] = mapped_column(String(64))
    ip_hash: Mapped[str | None] = mapped_column(String(64))


class PlatformRefreshToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """平台账号刷新令牌（平台 realm；与租户 `refresh_tokens` 同构但不带 tenant_id）。"""

    __tablename__ = "platform_refresh_tokens"
    __table_args__ = (
        Index("ix_platform_refresh_tokens_platform_user_id", "platform_user_id"),
        Index("ix_platform_refresh_tokens_family_id", "family_id"),
        Index("ix_platform_refresh_tokens_expires_at", "expires_at"),
        UniqueConstraint("token_hash", name="uq_platform_refresh_tokens_token_hash"),
        CheckConstraint(
            "revoked_reason IS NULL OR revoked_reason IN "
            "('logout', 'password_change', 'reuse_detected', 'rotated', "
            "'admin_revoke', 'account_deleted', 'expired', 'session_kicked', "
            "'session_revoked')",
            name="revoked_reason_valid",
        ),
        ForeignKeyConstraint(
            ["platform_user_id"],
            ["platform_users.id"],
            name="fk_platform_refresh_tokens_platform_user_id_platform_users",
            ondelete="CASCADE",
        ),
    )

    jti: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, unique=True)
    family_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    platform_user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    device_label: Mapped[str | None] = mapped_column(String(64))
    rotated_to_jti: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(32))
    ua_hash: Mapped[str | None] = mapped_column(String(64))
    ip_hash: Mapped[str | None] = mapped_column(String(64))
