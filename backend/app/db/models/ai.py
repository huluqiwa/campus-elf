"""AI 域模型（M4-AI09 guard_events + M4-AI06/07 会话/trace/用量；design §4.4）。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class GuardEvent(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """护栏事件（M4-AI09；design §4.4 `guard_events`，仅追加——不授 update/delete）。

    `excerpt_redacted` 为脱敏摘要（D3：命中本体以 `[REDACTED:<rule>]` 占位，
    事件表不存原始攻击载荷全文）。
    """

    __tablename__ = "guard_events"
    __table_args__ = (
        CheckConstraint("direction IN ('input', 'output')", name="ck_guard_events_direction"),
        CheckConstraint("severity IN ('low', 'medium', 'high')", name="ck_guard_events_severity"),
        CheckConstraint("action IN ('allow', 'block', 'redact', 'fallback')", name="ck_guard_events_action"),
        UniqueConstraint("id", "tenant_id", name="uq_guard_events_id_tenant_id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_guard_events_tenant_id_tenants", ondelete="RESTRICT"),
        Index("ix_guard_events_tenant_trace", "tenant_id", "trace_id"),
        Index("ix_guard_events_tenant_created", "tenant_id", "created_at"),
        {"comment": "AI 输入/输出护栏事件（M4-AI09；仅追加）"},
    )

    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    rule: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    excerpt_redacted: Mapped[str] = mapped_column(Text, nullable=False)


class Conversation(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """AI 对话会话（M4-AI06/07；design §4.4 `conversations`）。"""

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'deleted')", name="ck_conversations_status"),
        UniqueConstraint("id", "tenant_id", name="uq_conversations_id_tenant_id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_conversations_tenant_id_tenants", ondelete="RESTRICT"),
        ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_conversations_user_id_users", ondelete="RESTRICT"),
        Index("ix_conversations_tenant_user", "tenant_id", "user_id"),
        Index("ix_conversations_tenant_last_msg", "tenant_id", "last_message_at"),
        {"comment": "AI 对话会话（M4-AI06/07）"},
    )

    user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    pinned_kb_version_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationMessage(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """AI 对话消息（M4-AI06/07；design §4.4 `conversation_messages`，软删）。"""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant', 'system')", name="ck_conv_messages_role"),
        CheckConstraint("status IN ('streaming', 'completed', 'partial', 'failed', 'deleted')", name="ck_conv_messages_status"),
        UniqueConstraint("id", "tenant_id", name="uq_conv_messages_id_tenant_id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_conv_messages_tenant_id_tenants", ondelete="RESTRICT"),
        ForeignKeyConstraint(["conversation_id", "tenant_id"], ["conversations.id", "conversations.tenant_id"], name="fk_conv_messages_conv_id_tenant_id", ondelete="CASCADE"),
        Index("ix_conv_messages_conv_created", "tenant_id", "conversation_id", "created_at"),
        {"comment": "AI 对话消息（M4-AI06/07；软删 status=deleted）"},
    )

    conversation_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    reasoning_summary: Mapped[str | None] = mapped_column(Text)
    citations_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="completed")


class ChatTrace(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """AI 回合 trace（M4-AI06/07；design §4.4 `chat_traces`，TD-330 收口）。"""

    __tablename__ = "chat_traces"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_chat_traces_id_tenant_id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_chat_traces_tenant_id_tenants", ondelete="RESTRICT"),
        Index("ix_chat_traces_tenant_conv", "tenant_id", "conversation_id"),
        Index("ix_chat_traces_tenant_user", "tenant_id", "user_id"),
        {"comment": "AI 回合 trace（TD-330 收口）"},
    )

    user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    turn_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    intents_json: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    route_path: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False, server_default="")
    prompt_name: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="")
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    first_token_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))


class ChatTraceSpan(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """AI trace 子 span（M4-AI06/07；design §4.4 `chat_trace_spans`，TD-330）。"""

    __tablename__ = "chat_trace_spans"
    __table_args__ = (
        CheckConstraint("status IN ('ok', 'error')", name="ck_chat_trace_spans_status"),
        UniqueConstraint("id", "tenant_id", name="uq_chat_trace_spans_id_tenant_id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_chat_trace_spans_tenant_id_tenants", ondelete="RESTRICT"),
        Index("ix_chat_trace_spans_tenant_trace", "tenant_id", "trace_id"),
        {"comment": "AI trace 子 span（TD-330）"},
    )

    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    span_type: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attributes_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(8), nullable=False, server_default="ok")


class LlmUsage(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """LLM 用量（M4-AI06/07；design §4.4 `llm_usage`）。"""

    __tablename__ = "llm_usage"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_llm_usage_id_tenant_id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_llm_usage_tenant_id_tenants", ondelete="RESTRICT"),
        Index("ix_llm_usage_tenant_trace", "tenant_id", "trace_id"),
        {"comment": "LLM 用量"},
    )

    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    model: Mapped[str] = mapped_column(String(100), nullable=False, server_default="")
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_estimate: Mapped[float | None] = mapped_column(Numeric(10, 6))

class AiFeedback(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """AI 反馈（M4-AI05；design §4.4 `ai_feedback`）。

    匿名化（A6）：user_id 置空 + comment/快照清除 + status=anonymized
    （库层不变量 ck_ai_feedback_anonymized_no_user 钉住）。
    快照 content_snapshot_enc 为 AES-256-GCM 密文（AAD 绑定 tenant+feedback）。
    """

    __tablename__ = "ai_feedback"
    __table_args__ = (
        CheckConstraint("char_length(comment) <= 500", name="ck_ai_feedback_comment_len"),
        UniqueConstraint("id", "tenant_id", name="uq_ai_feedback_id_tenant_id"),
        UniqueConstraint(
            "tenant_id", "conversation_id", "turn_id",
            name="uq_ai_feedback_tenant_conv_turn",
        ),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_ai_feedback_tenant_id_tenants", ondelete="RESTRICT"),
        ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_ai_feedback_user_id_users", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["conversation_id", "tenant_id"],
            ["conversations.id", "conversations.tenant_id"],
            name="fk_ai_feedback_conv_id_tenant_id",
            ondelete="RESTRICT",
        ),
        Index("ix_ai_feedback_tenant_created", "tenant_id", "created_at"),
        {"comment": "AI 反馈（M4-AI05）"},
    )

    # 匿名化后置空（A6）；提交时必填
    user_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    conversation_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    turn_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    message_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rating: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_tags: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    comment: Mapped[str | None] = mapped_column(Text)
    content_snapshot_enc: Mapped[str | None] = mapped_column(Text)
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="submitted")
    handled_by: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AiFeedbackReply(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    """AI 反馈答复（M4-AI05；design §4.4 `ai_feedback_replies`；仅追加）。"""

    __tablename__ = "ai_feedback_replies"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_ai_feedback_replies_tenant_id_tenants", ondelete="RESTRICT"),
        ForeignKeyConstraint(["sender_id"], ["users.id"], name="fk_ai_feedback_replies_sender_id_users", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["feedback_id", "tenant_id"],
            ["ai_feedback.id", "ai_feedback.tenant_id"],
            name="fk_ai_feedback_replies_feedback_id_tenant_id",
            ondelete="CASCADE",
        ),
        Index("ix_ai_feedback_replies_tenant_feedback", "tenant_id", "feedback_id"),
        {"comment": "AI 反馈答复（M4-AI05）"},
    )

    feedback_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    sender_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    message_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    content: Mapped[str] = mapped_column(Text, nullable=False)

