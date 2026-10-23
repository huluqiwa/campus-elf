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
    ForeignKeyConstraint,
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
        # 显式命名：默认的约定名 `fk_<table>_<column>_platform_users` 有 72 字符，
        # 超过 PostgreSQL 的 63 字节标识符上限会被截断，导致 `alembic check` 报差异。
        ForeignKeyConstraint(
            ["submitted_by_platform_user_id"],
            ["platform_users.id"],
            name="fk_tenant_reg_requests_submitted_by_platform_users",
            ondelete="RESTRICT",
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
    # 平台代校方提交时记录提交人（RBAC-03 的判据）；校方自助提交为 NULL——
    # 那时还没有任何平台账号，也就没有"自己审自己"的可能。
    # 外键在 `__table_args__` 里显式命名（约定名超过 PostgreSQL 63 字节标识符上限）。
    submitted_by_platform_user_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))


class SchoolReviewRecord(UUIDPrimaryKeyMixin, Base):
    """审核记录（一次申请只允许一条；含审核人/结果/理由/凭证快照）。"""

    __tablename__ = "school_review_records"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_school_review_records_request_id"),
        CheckConstraint("result IN ('approved', 'rejected')", name="result_valid"),
        # RBAC-03 的数据库硬闸门：同人审批必须有例外标记（例外由审核函数按库内开关+理由判定）
        CheckConstraint(
            "submitted_by_platform_user_id IS NULL"
            " OR reviewer_id <> submitted_by_platform_user_id"
            " OR self_exception_used IS TRUE",
            name="no_self_approval_without_exception",
        ),
        Index("ix_school_review_records_reviewed_at", "reviewed_at"),
    )

    request_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenant_registration_requests.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 审核时从申请行**冗余抄一份**提交人（口径 ③：不加触发器也要有数据库硬约束）。
    # 为什么冗余：`reviewer_id` 在本表、`submitted_by` 在申请表，跨表 CHECK 无法表达，
    # 而"提交人不能审自己"是唯一一条希望连手工 SQL 都挡得住的规则。
    # 两份由同一个 SECURITY DEFINER 审核函数一起写入，一致性由集成测试钉住。
    submitted_by_platform_user_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    # 例外标记：只有审核函数在**库里的例外开关为真且本次审批带理由**时才置真。
    # 于是这道 CHECK 的语义是"同人审批必须有例外标记"，而不是"同人一律不许"，
    # 例外路径既留了证据（提交人列不被抹掉）又仍然要过库这一道。
    self_exception_used: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
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
