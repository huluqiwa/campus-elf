"""M3 KB-10A：切块编辑与派生重建——kb_chunk_edits 表 + versions 派生两列。

`docs/design-briefs/M3-KB-10.md`（已确认 D0-D6：2 PR 拆分 / Qdrant 向量复制不建
kb_embedding_cache 表〔偏离 design :227，登记 TD〕/ derive=确认即构建 / ACL 继承
沿用源已确认态〔state-machines :85〕/ 编辑记录三态 / 轻量结构校验）。

- `kb_document_versions` 加 `parent_version_id`（FK versions 自引用复合键，SET NULL）+
  `derivation_type`（CHECK ∈ initial/chunk_edit；NULL=initial 历史行不改数据）；
- 新表 `kb_chunk_edits`：块编辑提交记录（submitted/confirmed/discarded 三态），
  部分唯一 `(source_version_id, chunk_id) WHERE status='submitted'`——同块至多一个
  提交态（并发编辑后者吃 409 CHUNK_CONFLICT 由乐观锁承担，唯一约束兜底）；
  FORCE RLS + 策略（kb_document_audiences 同款）。

派生版本状态机复用 APPROVED→READY（derivation_type 列承载派生语义，不新增
DERIVED 状态名——简报 §0 登记偏离）。

downgrade：删派生两列、删新表。
**数据前提**：无（新表空降、新列可空，downgrade 无数据迁移要求）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "c7e9f1a3b5d7"
down_revision: str | None = "b8d4e6f0a2c4"
branch_labels: str | None = None
depends_on: str | None = None

_TENANT_EXPR = (
    "tenant_id = case when pg_input_is_valid("
    "current_setting('app.tenant_id', true), 'uuid') "
    "then current_setting('app.tenant_id', true)::uuid else null end"
)

_DERIVATION_TYPE_CHECK = "derivation_type IN ('initial', 'chunk_edit')"
_EDIT_STATUS_CHECK = "status IN ('submitted', 'confirmed', 'discarded')"


def upgrade() -> None:
    op.add_column(
        "kb_document_versions",
        sa.Column("parent_version_id", pg.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "kb_document_versions",
        sa.Column("derivation_type", sa.String(length=24), nullable=True),
    )
    op.create_check_constraint(
        "ck_kb_document_versions_derivation_type_valid",
        "kb_document_versions",
        _DERIVATION_TYPE_CHECK,
    )
    # 自引用复合 FK：派生版本追溯源版本（跨租防嫁接；源被删除时置 NULL 保留历史）
    op.create_foreign_key(
        "fk_kb_document_versions_parent_version_id",
        "kb_document_versions",
        "kb_document_versions",
        ["parent_version_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "kb_chunk_edits",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("before_hash", sa.String(length=64), nullable=False),
        sa.Column("after_hash", sa.String(length=64), nullable=False),
        sa.Column("new_content", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("derived_version_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("edited_by", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_chunk_edits_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_kb_chunk_edits_source_version_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id", "tenant_id"],
            ["kb_chunks.id", "kb_chunks.tenant_id"],
            name="fk_kb_chunk_edits_chunk_id_kb_chunks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["edited_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_kb_chunk_edits_edited_by_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["derived_version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_kb_chunk_edits_derived_version_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_chunk_edits")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_kb_chunk_edits_id_tenant_id"),
        sa.CheckConstraint(_EDIT_STATUS_CHECK, name=op.f("ck_kb_chunk_edits_status_valid")),
    )
    # 同块至多一个提交态（并发提交兜底；乐观锁 409 在应用层）
    op.create_index(
        "uq_kb_chunk_edits_source_chunk_submitted",
        "kb_chunk_edits",
        ["source_version_id", "chunk_id"],
        unique=True,
        postgresql_where=sa.text("status = 'submitted'"),
    )
    op.create_index("ix_kb_chunk_edits_tenant_id", "kb_chunk_edits", ["tenant_id"])
    op.create_index(
        "ix_kb_chunk_edits_source_version_id", "kb_chunk_edits", ["source_version_id"]
    )

    op.execute("ALTER TABLE kb_chunk_edits ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE kb_chunk_edits FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON kb_chunk_edits TO campus_elf_tenant "
        f"USING ({_TENANT_EXPR}) WITH CHECK ({_TENANT_EXPR})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON kb_chunk_edits TO campus_elf_tenant")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON kb_chunk_edits")
    op.drop_index("ix_kb_chunk_edits_source_version_id", table_name="kb_chunk_edits")
    op.drop_index("ix_kb_chunk_edits_tenant_id", table_name="kb_chunk_edits")
    op.drop_index(
        "uq_kb_chunk_edits_source_chunk_submitted", table_name="kb_chunk_edits"
    )
    op.drop_table("kb_chunk_edits")
    op.drop_constraint(
        "fk_kb_document_versions_parent_version_id",
        "kb_document_versions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_kb_document_versions_derivation_type_valid",
        "kb_document_versions",
        type_="check",
    )
    op.drop_column("kb_document_versions", "derivation_type")
    op.drop_column("kb_document_versions", "parent_version_id")
