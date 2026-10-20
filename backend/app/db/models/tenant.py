"""租户与平台账号模型（`tenants` / `platform_users`）。

- `tenants`：**平台级表**，不带 `tenant_id`，但启用 RLS 并按 `id = app.tenant_id` 自隔离
  （租户只见自己那一行）；平台连接可读写全部（学校开通/审核）。
- `platform_users`：SaaS 账号（超管/运营/审计），不开放注册；与租户用户物理分离，
  这样 RLS 在"未设置租户上下文"时是 fail-closed（租户表查不到任何行），
  而不会把平台账号暴露给租户上下文。
"""

from datetime import datetime
from typing import Literal

from sqlalchemy import CheckConstraint, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

TenantStatus = Literal["pending", "active", "suspended", "rejected", "deleted"]
PlatformUserStatus = Literal["active", "disabled"]


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """学校 = 租户。"""

    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'active', 'suspended', 'rejected', 'deleted')",
            name="status_valid",
        ),
    )

    school_code: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    address: Mapped[str | None] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(254))
    leadership: Mapped[str | None] = mapped_column(String(200))
    qualification_ref: Mapped[str | None] = mapped_column(String(300))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 暂停/删除原因（状态机审计在 audit_logs，此处保留最近一次原因便于运营查询）
    status_reason: Mapped[str | None] = mapped_column(Text)


class PlatformUser(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """SaaS 平台账号（不开放注册，由初始化脚本/平台超管创建）。"""

    __tablename__ = "platform_users"
    __table_args__ = (CheckConstraint("status IN ('active', 'disabled')", name="status_valid"),)

    account: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # 无 server_default：初始密码账号必须由创建方显式置 true（fail-loud）
    must_change_password: Mapped[bool] = mapped_column(nullable=False, default=True)
    mfa_secret_enc: Mapped[str | None] = mapped_column(Text)
    mfa_enabled: Mapped[bool] = mapped_column(nullable=False, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 权限/状态变更时自增，使已签发的 access token 立即失效（校验时比对）
    token_version: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
