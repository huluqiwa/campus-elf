"""系统设置模型（M1-E2，运营可调的策略开关）。

平台级表：无 `tenant_id`，与 `permissions` 同性质（由数据库权限约束，不启用租户 RLS）。
只有平台连接可写（`platform.config.edit` + 理由 + 审计），租户连接没有授权 → 读不到也改不了。
键的白名单、类型与出厂默认在代码里（`app/settings/store.py:SETTING_DEFS`）：
本表只存**被运营改过的值**，没落库的键走代码默认，因此删一行就等于恢复默认。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SystemSetting(Base):
    """一个已落库的策略值（`key` 为主键，改值即 upsert）。"""

    __tablename__ = "system_settings"
    __table_args__ = (
        PrimaryKeyConstraint("key"),
        # 理由必填：安全策略的每一次改动都要能回答"为什么改"（审计里只有"谁改的"不够用）
        CheckConstraint(
            "reason IS NOT NULL AND length(btrim(reason)) > 0", name="reason_required"
        ),
        CheckConstraint("length(value) BETWEEN 1 AND 32", name="value_length"),
    )

    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_by: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("platform_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
