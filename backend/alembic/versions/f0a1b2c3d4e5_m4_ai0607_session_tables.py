"""M4 AI-06/07：会话持久化 + trace 落库——5 张表。

design §4.4 AI、会话、反馈表族（不含 summaries/feedback/eval——后续点）：
- conversations（会话元数据 + pinned_kb_version_id）
- conversation_messages（user/assistant 消息，软删 status=deleted）
- chat_traces（回合 trace，TD-330 收口）
- chat_trace_spans（trace 子 span）
- llm_usage（LLM 用量）

全部 FORCE RLS + 租户策略 + 仅追加纪律（trace/usage 不授 update/delete）。
downgrade：删 5 张表。
**数据前提**：无（新表空降）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "f0a1b2c3d4e5"
down_revision: str | None = "e9f0a1b2c3d4"
branch_labels: str | None = None
depends_on: str | None = None

_TENANT_EXPR = (
    "tenant_id = case when pg_input_is_valid("
    "current_setting('app.tenant_id', true), 'uuid') "
    "then current_setting('app.tenant_id', true)::uuid else null end"
)


def _rls_and_grant(table: str, *, readonly: bool = False) -> None:
    """FORCE RLS + 租户策略 + 权限（kb09a 先例）。"""
    op.execute(f"alter table {table} enable row level security")
    op.execute(f"alter table {table} force row level security")
    op.execute(
        f"create policy {table}_tenant_isolation on {table} to campus_elf_tenant "
        f"using ({_TENANT_EXPR}) with check ({_TENANT_EXPR})"
    )
    priv = "select, insert" if readonly else "select, insert, update"
    op.execute(f"grant {priv} on {table} to campus_elf_tenant")
    if not readonly:
        op.execute(f"revoke delete on {table} from campus_elf_tenant")


def upgrade() -> None:
    # ---- conversations ----
    op.create_table(
        "conversations",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("pinned_kb_version_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('active', 'deleted')", name="ck_conversations_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_conversations_tenant_id_tenants", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_conversations_user_id_users", ondelete="RESTRICT"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_conversations_id_tenant_id"),
        comment="AI 对话会话（M4-AI06/07）",
    )
    op.create_index("ix_conversations_tenant_user", "conversations", ["tenant_id", "user_id"])
    op.create_index("ix_conversations_tenant_last_msg", "conversations", ["tenant_id", "last_message_at"])
    _rls_and_grant("conversations")

    # ---- conversation_messages ----
    op.create_table(
        "conversation_messages",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("reasoning_summary", sa.Text, nullable=True),
        sa.Column("citations_json", pg.JSONB, nullable=True),
        sa.Column("token_usage", pg.JSONB, nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="completed"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("role IN ('user', 'assistant', 'system')", name="ck_conv_messages_role"),
        sa.CheckConstraint(
            "status IN ('streaming', 'completed', 'partial', 'failed', 'deleted')",
            name="ck_conv_messages_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_conv_messages_tenant_id_tenants", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["conversation_id", "tenant_id"],
            ["conversations.id", "conversations.tenant_id"],
            name="fk_conv_messages_conv_id_tenant_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_conv_messages_id_tenant_id"),
        comment="AI 对话消息（M4-AI06/07；软删 status=deleted）",
    )
    op.create_index(
        "ix_conv_messages_conv_created",
        "conversation_messages",
        ["tenant_id", "conversation_id", "created_at"],
    )
    _rls_and_grant("conversation_messages")

    # ---- chat_traces ----
    op.create_table(
        "chat_traces",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("turn_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("intents_json", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("route_path", sa.String(16), nullable=False),
        sa.Column("model", sa.String(100), nullable=False, server_default=""),
        sa.Column("prompt_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("prompt_version", sa.String(32), nullable=False, server_default=""),
        sa.Column("prompt_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("tokens_in", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer, nullable=False, server_default="0"),
        sa.Column("first_token_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("stop_reason", sa.String(32), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_chat_traces_tenant_id_tenants", ondelete="RESTRICT"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_chat_traces_id_tenant_id"),
        comment="AI 回合 trace（TD-330 收口）",
    )
    op.create_index("ix_chat_traces_tenant_conv", "chat_traces", ["tenant_id", "conversation_id"])
    op.create_index("ix_chat_traces_tenant_user", "chat_traces", ["tenant_id", "user_id"])
    _rls_and_grant("chat_traces", readonly=True)

    # ---- chat_trace_spans ----
    op.create_table(
        "chat_trace_spans",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("span_type", sa.String(16), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("start_ms", sa.BigInteger, nullable=False),
        sa.Column("duration_ms", sa.BigInteger, nullable=False),
        sa.Column("attributes_json", pg.JSONB, nullable=True),
        sa.Column("status", sa.String(8), nullable=False, server_default="ok"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('ok', 'error')", name="ck_chat_trace_spans_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_chat_trace_spans_tenant_id_tenants", ondelete="RESTRICT"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_chat_trace_spans_id_tenant_id"),
        comment="AI trace 子 span（TD-330）",
    )
    op.create_index(
        "ix_chat_trace_spans_tenant_trace",
        "chat_trace_spans",
        ["tenant_id", "trace_id"],
    )
    _rls_and_grant("chat_trace_spans", readonly=True)

    # ---- llm_usage ----
    op.create_table(
        "llm_usage",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False, server_default=""),
        sa.Column("model", sa.String(100), nullable=False, server_default=""),
        sa.Column("tokens_in", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer, nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_estimate", sa.Numeric(10, 6), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_llm_usage_tenant_id_tenants", ondelete="RESTRICT"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_llm_usage_id_tenant_id"),
        comment="LLM 用量",
    )
    op.create_index("ix_llm_usage_tenant_trace", "llm_usage", ["tenant_id", "trace_id"])
    _rls_and_grant("llm_usage", readonly=True)

    # TenantScopedMixin 自动加 tenant_id index（模型对齐）
    for tbl in ("conversations", "conversation_messages", "chat_traces", "chat_trace_spans", "llm_usage"):
        op.execute(f"create index if not exists ix_{tbl}_tenant_id on {tbl} (tenant_id)")



def downgrade() -> None:
    for table in ("llm_usage", "chat_trace_spans", "chat_traces", "conversation_messages", "conversations"):
        op.execute(f"drop policy if exists {table}_tenant_isolation on {table}")
        op.drop_table(table)
