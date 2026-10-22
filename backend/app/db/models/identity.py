"""租户用户与刷新令牌模型（`users` / `refresh_tokens`）。

`users` 是 C 端与 B 端共用身份（学生/教师），**必须带 tenant_id**；
`refresh_tokens` 记录令牌家族与轮换链，是"复用检测撤销整族"的持久事实来源
（Redis 只做加速标记，见 `docs/design-briefs/M1-tenant-identity.md` §3.2）。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
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
        CheckConstraint("failed_login_attempts >= 0", name="failed_login_attempts_nonnegative"),
        # 已启用 MFA 必须有加密 secret；未启用时允许存在"待激活" secret
        CheckConstraint(
            "NOT mfa_enabled OR mfa_secret_enc IS NOT NULL",
            name="mfa_secret_required_when_enabled",
        ),
        CheckConstraint(
            "mfa_last_used_step IS NULL OR mfa_last_used_step >= 0",
            name="mfa_last_used_step_nonnegative",
        ),
    )

    user_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # 登录标识：学号或工号（可含字母数字）
    login_id: Mapped[str] = mapped_column(String(254), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    # 密码重置收件地址；校方管理员由 login_id（注册邮箱）回填/写入
    email: Mapped[str | None] = mapped_column(String(254))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # 无 server_default：初始密码账号必须由创建方显式置 true（fail-loud）
    must_change_password: Mapped[bool] = mapped_column(nullable=False, default=True)
    mfa_secret_enc: Mapped[str | None] = mapped_column(Text)
    mfa_enabled: Mapped[bool] = mapped_column(nullable=False, default=False)
    # 最近一次成功使用的 TOTP 时间步（防同一步内重放）
    mfa_last_used_step: Mapped[int | None] = mapped_column(BigInteger)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 权限/状态变更时自增：access token 校验时比对，旧令牌立即失效
    token_version: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # 失败锁定窗口（AUTH-05）：每用户 advisory lock 内读改写
    failed_login_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failed_login_window_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
            "'account_deleted', 'expired', 'session_kicked', 'session_revoked', "
            "'password_reset', 'mfa_change')",
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
    # 该刷新链是否完成过 MFA（平台强制策略开启时，false 的旧会话刷新会被拒绝）
    mfa_verified: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )


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
            "'session_revoked', 'password_reset', 'mfa_change')",
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
    mfa_verified: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )


class PasswordHistory(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """租户用户最近使用过的密码哈希（改密/重置时写入，保留最近 N 条）。"""

    __tablename__ = "password_history"
    __table_args__ = (
        Index(
            "ix_password_history_tenant_id_user_id",
            "tenant_id",
            "user_id",
            "created_at",
        ),
        ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_password_history_user_id_tenant_id_users",
            ondelete="CASCADE",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PasswordResetToken(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """租户密码重置令牌（只存 SHA-256 摘要；单次使用、30 分钟过期）。"""

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        Index(
            "ix_password_reset_tokens_tenant_user_created_at",
            "tenant_id",
            "user_id",
            "created_at",
        ),
        Index("ix_password_reset_tokens_expires_at", "expires_at"),
        UniqueConstraint("token_hash", name="uq_password_reset_tokens_token_hash"),
        ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_password_reset_tokens_user_id_tenant_id_users",
            ondelete="CASCADE",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    request_ip_hash: Mapped[str | None] = mapped_column(String(64))


class PlatformPasswordHistory(UUIDPrimaryKeyMixin, Base):
    """平台账号最近使用过的密码哈希。"""

    __tablename__ = "platform_password_history"
    __table_args__ = (
        Index(
            "ix_platform_password_history_user_created_at",
            "platform_user_id",
            "created_at",
        ),
        ForeignKeyConstraint(
            ["platform_user_id"],
            ["platform_users.id"],
            name="fk_platform_password_history_platform_user_id_platform_users",
            ondelete="CASCADE",
        ),
    )

    platform_user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MfaRecoveryCode(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """租户用户 MFA 恢复码（只存 HMAC 摘要；一次性，启用时整批重发）。"""

    __tablename__ = "mfa_recovery_codes"
    __table_args__ = (
        Index(
            "ix_mfa_recovery_codes_tenant_user_created_at",
            "tenant_id",
            "user_id",
            "created_at",
        ),
        UniqueConstraint(
            "tenant_id", "user_id", "code_hash", name="uq_mfa_recovery_codes_tenant_user_hash"
        ),
        ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_mfa_recovery_codes_user_id_tenant_id_users",
            ondelete="CASCADE",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PlatformMfaRecoveryCode(UUIDPrimaryKeyMixin, Base):
    """平台账号 MFA 恢复码（平台表，不带 tenant_id，仅平台连接可访问）。"""

    __tablename__ = "platform_mfa_recovery_codes"
    __table_args__ = (
        Index(
            "ix_platform_mfa_recovery_codes_user_created_at",
            "platform_user_id",
            "created_at",
        ),
        UniqueConstraint(
            "platform_user_id", "code_hash", name="uq_platform_mfa_recovery_codes_user_hash"
        ),
        ForeignKeyConstraint(
            ["platform_user_id"],
            ["platform_users.id"],
            name="fk_platform_mfa_recovery_codes_platform_user_id",
            ondelete="CASCADE",
        ),
    )

    platform_user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PlatformPasswordResetToken(UUIDPrimaryKeyMixin, Base):
    """平台密码重置令牌（只存 SHA-256 摘要；单次使用、30 分钟过期）。"""

    __tablename__ = "platform_password_reset_tokens"
    __table_args__ = (
        Index(
            "ix_platform_password_reset_tokens_user_created_at",
            "platform_user_id",
            "created_at",
        ),
        Index("ix_platform_password_reset_tokens_expires_at", "expires_at"),
        UniqueConstraint("token_hash", name="uq_platform_password_reset_tokens_token_hash"),
        ForeignKeyConstraint(
            ["platform_user_id"],
            ["platform_users.id"],
            name="fk_platform_password_reset_tokens_user",
            ondelete="CASCADE",
        ),
    )

    platform_user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    request_ip_hash: Mapped[str | None] = mapped_column(String(64))
