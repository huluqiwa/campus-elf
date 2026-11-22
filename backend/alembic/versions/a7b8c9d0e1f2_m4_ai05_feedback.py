"""M4 AI-05：反馈采集——2 张表（design §4.4）。

- ai_feedback（C 端对 AI 回合的反馈；快照 AES-GCM 密文；匿名化置空 user_id/comment/快照）
- ai_feedback_replies（B 端答复，仅追加）

全部 FORCE RLS + 租户策略；ai_feedback 授 update（处理/匿名化）、replies 仅追加
（readonly 授权）。downgrade：删 2 张表。
**数据前提**：无（新表空降）。
**与 design §4.4 字段偏离**：补 `turn_id`（幂等键 (tenant, conversation, turn)
唯一约束——spec「重复提交幂等」需要；design 字段列表缺 turn_id，登记简报 §10）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f0a1b2c3d4e5"
branch_labels: str | None = None
depends_on: str | None = None

_TENANT_EXPR = (
    "tenant_id = case when pg_input_is_valid("
    "current_setting('app.tenant_id', true), 'uuid') "
    "then current_setting('app.tenant_id', true)::uuid else null end"
)

_REASON_TAGS_ENUM = '["inaccurate","irrelevant","outdated","harmful","other"]'


def _rls_and_grant(table: str, *, readonly: bool = False) -> None:
    """FORCE RLS + 租户策略 + 权限（AI-06/07 先例）。"""
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
    # ---- ai_feedback ----
    op.create_table(
        "ai_feedback",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("conversation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("rating", sa.String(16), nullable=False),
        sa.Column("reason_tags", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("content_snapshot_enc", sa.Text, nullable=True),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("status", sa.String(16), nullable=False, server_default="submitted"),
        sa.Column("handled_by", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("rating IN ('helpful', 'unhelpful')", name="ck_ai_feedback_rating"),
        sa.CheckConstraint(
            "jsonb_typeof(reason_tags) = 'array' "
            f"AND reason_tags <@ '{_REASON_TAGS_ENUM}'::jsonb",
            name="ck_ai_feedback_reason_tags",
        ),
        sa.CheckConstraint(
            "status IN ('submitted', 'handled', 'anonymized')",
            name="ck_ai_feedback_status",
        ),
        # 匿名化不变量：匿名化行必须已断开身份（user_id 置空）
        sa.CheckConstraint(
            "status != 'anonymized' OR user_id IS NULL",
            name="ck_ai_feedback_anonymized_no_user",
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_ai_feedback_id_tenant_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_ai_feedback_tenant_id_tenants", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_ai_feedback_user_id_users", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["conversation_id", "tenant_id"],
            ["conversations.id", "conversations.tenant_id"],
            name="fk_ai_feedback_conv_id_tenant_id",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "conversation_id", "turn_id", name="uq_ai_feedback_tenant_conv_turn"),
        comment="AI 反馈（M4-AI05）",
    )
    op.create_index("ix_ai_feedback_tenant_created", "ai_feedback", ["tenant_id", "created_at"])
    _rls_and_grant("ai_feedback")

    # ---- ai_feedback_replies（仅追加） ----
    op.create_table(
        "ai_feedback_replies",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("feedback_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("sender_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_ai_feedback_replies_tenant_id_tenants", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["sender_id"], ["users.id"], name="fk_ai_feedback_replies_sender_id_users", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["feedback_id", "tenant_id"],
            ["ai_feedback.id", "ai_feedback.tenant_id"],
            name="fk_ai_feedback_replies_feedback_id_tenant_id",
            ondelete="CASCADE",
        ),
        comment="AI 反馈答复（M4-AI05）",
    )
    op.create_index(
        "ix_ai_feedback_replies_tenant_feedback",
        "ai_feedback_replies",
        ["tenant_id", "feedback_id"],
    )
    _rls_and_grant("ai_feedback_replies", readonly=True)

    # TenantScopedMixin 自动加 tenant_id index（模型对齐）
    for tbl in ("ai_feedback", "ai_feedback_replies"):
        op.execute(f"create index if not exists ix_{tbl}_tenant_id on {tbl} (tenant_id)")


def downgrade() -> None:
    for table in ("ai_feedback_replies", "ai_feedback"):
        op.execute(f"drop policy if exists {table}_tenant_isolation on {table}")
        op.drop_table(table)
