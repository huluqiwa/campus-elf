"""M4 AI-09：输入/输出护栏全量——guard_events 表（护栏事件落库）。

`docs/design-briefs/M4-AI09-guards.md`（决策卡 3-A：本点唯一迁移；design §4.4
`guard_events` 字段设计落地）。字段：direction(input/output)/rule/severity/
action/excerpt_redacted（D3：命中本体占位脱敏——事件表不存原始攻击载荷全文）。

FORCE RLS + 租户策略（kb_chunk_edits 同款模式）；downgrade：删新表。
**数据前提**：无（新表空降，downgrade 无数据迁移要求）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "e9f0a1b2c3d4"
down_revision: str | None = "c7e9f1a3b5d7"
branch_labels: str | None = None
depends_on: str | None = None

_TENANT_EXPR = (
    "tenant_id = case when pg_input_is_valid("
    "current_setting('app.tenant_id', true), 'uuid') "
    "then current_setting('app.tenant_id', true)::uuid else null end"
)


def upgrade() -> None:
    op.create_table(
        "guard_events",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("rule", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(8), nullable=False),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("excerpt_redacted", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_guard_events_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "direction IN ('input', 'output')", name="ck_guard_events_direction"
        ),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high')", name="ck_guard_events_severity"
        ),
        sa.CheckConstraint(
            "action IN ('allow', 'block', 'redact', 'fallback')",
            name="ck_guard_events_action",
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_guard_events_id_tenant_id"),
        comment="AI 输入/输出护栏事件（M4-AI09；仅追加）",
    )
    op.create_index(
        "ix_guard_events_tenant_id",
        "guard_events",
        ["tenant_id"],
    )
    op.create_index(
        "ix_guard_events_tenant_trace",
        "guard_events",
        ["tenant_id", "trace_id"],
    )
    op.create_index(
        "ix_guard_events_tenant_created",
        "guard_events",
        ["tenant_id", "created_at"],
    )
    op.execute("alter table guard_events enable row level security")
    op.execute("alter table guard_events force row level security")
    op.execute(
        "create policy guard_events_tenant_isolation on guard_events to campus_elf_tenant "
        f"using ({_TENANT_EXPR}) with check ({_TENANT_EXPR})"
    )
    # 仅追加：不授 update/delete（audit_logs 同款纪律）
    op.execute("grant select, insert on guard_events to campus_elf_tenant")


def downgrade() -> None:
    op.execute("drop policy if exists guard_events_tenant_isolation on guard_events")
    op.drop_table("guard_events")
