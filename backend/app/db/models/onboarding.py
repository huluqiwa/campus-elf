"""学校注册与审核模型（M1-B，TEN-01/02）。

两张表都是**平台级**（无 tenant_id）：只有平台侧连接（`campus_elf_platform`）有权限，
租户连接完全不可见；因此不需要租户 RLS（由数据库权限 + 平台鉴权双重约束）。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin

RegistrationStatus = str  # pending / approved / rejected
ReviewResult = str  # approved / rejected


class TenantRegistrationRequest(UUIDPrimaryKeyMixin, Base):
    """学校注册申请（资质凭证存对象存储，$snapshot 保存审核时的副本引用）。"""

    __tablename__ = "tenant_registration_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name="status_valid"
        ),
        CheckConstraint(
            "status <> 'rejected' OR (reject_reason IS NOT NULL AND length(reject_reason) > 0)",
            name="reject_reason_required",
        ),
        Index("ix_tenant_registration_requests_status", "status"),
        Index(
            "ix_tenant_registration_requests_submitted_at", "submitted_at"
        ),
        # 同一邮箱只允许存在一条"未拒绝"的申请（被拒后可重新提交）
        Index(
            "uq_tenant_registration_requests_active_email",
            "email",
            unique=True,
            postgresql_where=text("status IN ('pending', 'approved')"),
        ),
    )

    school_name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str] = mapped_column(String(300), nullable=False)
    leadership: Mapped[str] = mapped_column(String(200), nullable=False)
    # 规范化（小写/去空白）后的联系邮箱；也是校方管理员账号的 login_id
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    qualification_object_key: Mapped[str] = mapped_column(String(300), nullable=False)
    qualification_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    qualification_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    qualification_mime: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # 校名与已有租户/申请重复时置 true，供审核人重点核对（不阻断提交）
    school_name_duplicate_hint: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    tenant_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT")
    )


class SchoolReviewRecord(UUIDPrimaryKeyMixin, Base):
    """审核记录（一次申请只允许一条；含审核人/结果/理由/凭证快照）。"""

    __tablename__ = "school_review_records"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_school_review_records_request_id"),
        CheckConstraint("result IN ('approved', 'rejected')", name="result_valid"),
        Index("ix_school_review_records_reviewed_at", "reviewed_at"),
    )

    request_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenant_registration_requests.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT")
    )
    reviewer_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False
    )
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    # 审核时对资质凭证做快照（复制对象），保证审核依据不随原文件变化
    snapshot_object_key: Mapped[str | None] = mapped_column(String(300))
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
