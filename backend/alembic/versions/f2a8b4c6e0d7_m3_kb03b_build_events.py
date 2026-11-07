"""M3 KB-03B：构建事件持久化（kb_build_events）+ SSE 回放基底。

`docs/design-briefs/M3-KB-03B.md`（已确认：D1 worker 同事务写事件、D2 终态即关流）。

- `kb_build_events`：job_id 复合 FK CASCADE、`(job_id, seq)` 唯一（D1 并发安全）、
  租户级 + FORCE RLS + 策略；
- 事件类型：`build.stage` / `build.progress` / `build.done` / `build.error`
  （`docs/design.md` §5.6）。

downgrade：直接删表。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "f2a8b4c6e0d7"
down_revision: str | None = "e8c2a6d0f4b9"
branch_labels: str | None = None
depends_on: str | None = None

_TENANT_EXPR = (
    "tenant_id = case when pg_input_is_valid("
    "current_setting('app.tenant_id', true), 'uuid') "
    "then current_setting('app.tenant_id', true)::uuid else null end"
)


def upgrade() -> None:
    op.create_table(
        "kb_build_events",
        sa.Column("job_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("payload_json", pg.JSONB(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "event_type IN ('build.stage', 'build.progress', 'build.done', 'build.error')",
            name=op.f("ck_kb_build_events_event_type_valid"),
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_kb_build_events_seq_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_build_events_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "tenant_id"],
            ["kb_build_jobs.id", "kb_build_jobs.tenant_id"],
            name="fk_kb_build_events_job_id_kb_build_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_build_events")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_kb_build_events_id_tenant_id"),
        sa.UniqueConstraint("job_id", "seq", name="uq_kb_build_events_job_seq"),
    )
    op.create_index("ix_kb_build_events_tenant_id", "kb_build_events", ["tenant_id"])
    op.create_index(
        "ix_kb_build_events_job_seq", "kb_build_events", ["job_id", "seq"], unique=True
    )
    op.execute("ALTER TABLE kb_build_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE kb_build_events FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON kb_build_events TO campus_elf_tenant "
        f"USING ({_TENANT_EXPR}) WITH CHECK ({_TENANT_EXPR})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON kb_build_events TO campus_elf_tenant")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON kb_build_events")
    op.drop_table("kb_build_events")
