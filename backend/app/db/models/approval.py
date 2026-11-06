"""审批引擎模型（M3 KB-02 / APV-01 最小子集）。

口径（`docs/design-briefs/M3-KB-02.md` 已确认 D6；`docs/design.md` §4.5；
`docs/state-machines.md` §6）：

- 本点只有单步审批：`approval_steps` 恒 1 行（step_no=1），审批人池 =
  持 `school.kb.approval` 且非提交人非上传者；`approver_id` 在动作时落。
- `approval_requests.document_version_id` 是可空列，仅 KB_DOCUMENT 类型使用；
  部分唯一索引 `(tenant_id, document_version_id) WHERE status='PENDING'`
  兜底「同一版本最多一个活跃审批单」。**不建**版本表反向指针：提交可撤销再提交，
  同一版本会有多行审批历史（按 `document_version_id` 正向查询取最近一单）。
- 附件表、多步审批、超时/通知不在本点（TD-286 → M5 APV-01 完整版）。
- 全部租户级：`tenant_id NOT NULL` + FORCE RLS + 租户角色策略（迁移负责）。
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

APPROVAL_TYPE_KB_DOCUMENT = "KB_DOCUMENT"

REQUEST_STATUSES = ("PENDING", "APPROVED", "REJECTED", "CANCELLED")
REQUEST_RESULTS = ("approve", "reject", "cancel")
ACTION_TYPES = ("submit", "approve", "reject", "cancel")
STEP_STATUSES = ("PENDING", "APPROVED", "REJECTED", "CANCELLED")

_REQUEST_STATUS_SQL = ", ".join(f"'{status}'" for status in REQUEST_STATUSES)
_REQUEST_RESULT_SQL = ", ".join(f"'{result}'" for result in REQUEST_RESULTS)
_ACTION_SQL = ", ".join(f"'{action}'" for action in ACTION_TYPES)
_STEP_STATUS_SQL = ", ".join(f"'{status}'" for status in STEP_STATUSES)


class ApprovalType(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """审批类型注册（每租户一行；首次提交时 get-or-create）。"""

    __tablename__ = "approval_types"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_approval_types_id_tenant_id"),
        UniqueConstraint("tenant_id", "code", name="uq_approval_types_tenant_code"),
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class ApprovalRequest(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """审批单（status 为请求状态；result 为最终动作；payload_json 存提交时快照）。"""

    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_REQUEST_STATUS_SQL})", name="status_valid"
        ),
        UniqueConstraint("id", "tenant_id", name="uq_approval_requests_id_tenant_id"),
        CheckConstraint(
            f"result IS NULL OR result IN ({_REQUEST_RESULT_SQL})",
            name="result_valid",
        ),
        # 终态单必须有结果与完成时间；PENDING 单两者都为空。
        CheckConstraint(
            "("
            "(status = 'PENDING' AND result IS NULL AND completed_at IS NULL)"
            " OR (status <> 'PENDING' AND result IS NOT NULL AND completed_at IS NOT NULL)"
            ")",
            name="result_completion_consistent",
        ),
        CheckConstraint(
            "current_step >= 1", name="current_step_positive"
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_approval_requests_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["type_id", "tenant_id"],
            ["approval_types.id", "approval_types.tenant_id"],
            name="fk_approval_requests_type_id_approval_types",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_approval_requests_document_version_id_kb_document_versions",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["submitter_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_approval_requests_submitter_id_users",
            ondelete="CASCADE",
        ),
        # 部分唯一索引：同一版本最多一个 PENDING 审批单（迁移与模型同表达式，防漂移）。
        Index(
            "uq_approval_requests_tenant_version_pending",
            "tenant_id",
            "document_version_id",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index("ix_approval_requests_tenant_status_created", "tenant_id", "status", "created_at"),
        Index("ix_approval_requests_tenant_submitter", "tenant_id", "submitter_id"),
    )

    type_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    document_version_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    submitter_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PENDING")
    current_step: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result: Mapped[str | None] = mapped_column(String(16))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalStep(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """审批步骤（本点恒 1 步；approver 在动作时落）。"""

    __tablename__ = "approval_steps"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STEP_STATUS_SQL})", name="status_valid"),
        UniqueConstraint("id", "tenant_id", name="uq_approval_steps_id_tenant_id"),
        CheckConstraint("step_no >= 1", name="step_no_positive"),
        ForeignKeyConstraint(
            ["request_id", "tenant_id"],
            ["approval_requests.id", "approval_requests.tenant_id"],
            name="fk_approval_steps_request_id_approval_requests",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["acted_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_approval_steps_acted_by_users",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id", "request_id", "step_no", name="uq_approval_steps_tenant_request_step"
        ),
    )

    request_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PENDING")
    acted_by: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(Text)


class ApprovalAction(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """审批动作留痕（逐笔仅追加；reason 随动作）。"""

    __tablename__ = "approval_actions"
    __table_args__ = (
        CheckConstraint(f"action IN ({_ACTION_SQL})", name="action_valid"),
        UniqueConstraint("id", "tenant_id", name="uq_approval_actions_id_tenant_id"),
        ForeignKeyConstraint(
            ["request_id", "tenant_id"],
            ["approval_requests.id", "approval_requests.tenant_id"],
            name="fk_approval_actions_request_id_approval_requests",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["step_id", "tenant_id"],
            ["approval_steps.id", "approval_steps.tenant_id"],
            name="fk_approval_actions_step_id_approval_steps",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["actor_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_approval_actions_actor_id_users",
            ondelete="CASCADE",
        ),
        Index("ix_approval_actions_tenant_request", "tenant_id", "request_id", "created_at"),
        Index("ix_approval_actions_tenant_actor", "tenant_id", "actor_id", "created_at"),
    )

    request_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    step_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    actor_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
