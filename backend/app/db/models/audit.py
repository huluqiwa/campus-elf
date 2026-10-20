"""审计表模型（`audit_logs` 仅追加 + HMAC 哈希链）。

- `sequence` 为单调递增序号（哈希链顺序）；
- `tenant_id` 可为空（平台级事件，如学校审核）；
- 仅追加由数据库触发器强制（UPDATE/DELETE 抛异常），见基线迁移；
- 读取策略：租户上下文只能读本租户；平台上下文（显式 `app.platform_context='on'`）可读全部。
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

ActorRealm = str  # 'platform' | 'tenant' | 'system'


class AuditLog(Base):
    """审计事件（只追加）。"""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_audit_logs_action_created_at", "action", "created_at"),
        CheckConstraint(
            "actor_realm IN ('platform', 'tenant', 'system')", name="actor_realm_valid"
        ),
        CheckConstraint("result IN ('success', 'failure', 'denied')", name="result_valid"),
    )

    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, unique=True)
    tenant_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT")
    )
    actor_realm: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(16), nullable=False, default="success")
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    request_id: Mapped[str | None] = mapped_column(String(64))
    before_json_redacted: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after_json_redacted: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # M1-A 只建表与仅追加约束：链值由 M1-F 的写入方计算（TD-068）。
    # NULL + hash_version=0 显式表示"未入链"，避免占位哈希被永久固化（表只允许 INSERT）。
    prev_hash: Mapped[str | None] = mapped_column(String(64))
    hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 预留：外部锚点（WORM/时间戳）在 M6 落地时写入
    anchor_ref: Mapped[str | None] = mapped_column(String(200))
    # 0=未入链（M1-A 阶段），≥1 为链算法版本
    hash_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
