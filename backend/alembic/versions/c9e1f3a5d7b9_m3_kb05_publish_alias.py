"""M3 KB-05：发布/热更新——kb_publish_aliases 表 + 状态 CHECK 放宽。

`docs/design-briefs/M3-KB-05.md`（已确认 D1-A 每文档 alias、D2-A 任意 PUBLISHED
目标回滚、D4-A ACL 门禁归 KB-09、D5+D6-A 纯后端先行）。

- 新表 `kb_publish_aliases`（发布别名指针：alias/collection/version_id，唯一
  `(tenant_id, document_id)` 兜底并发首发布恰一行）：租户级 + FORCE RLS + 策略；
  `collection`/`version_id` 是活跃发布的事实来源（M4 检索解析），同名 Qdrant
  named alias 是派生可观测视图；
- `kb_document_versions`/`kb_documents` status CHECK 加入
  `PUBLISHED/OFFLINE/DELETED`（约束名不变，drop 后按原名重建，KB-03A 手法）；
  同批把模型侧 `KB_STATUSES` 补齐 KB-04A 已入库的 `TESTING/TEST_PASSED/TEST_FAILED`
  （Alembic 默认不比对 CHECK，模型此前落后于库，本次对齐）。

downgrade：删新表、恢复原状态 CHECK。
**数据前提**：库里已有 PUBLISHED/OFFLINE/DELETED 版本/文档行时，恢复旧 CHECK 会违约——
回滚前需先把发布态数据迁回（或清库）；开发/预生产按此处理。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "c9e1f3a5d7b9"
down_revision: str | None = "b7d3e9f1a5c3"
branch_labels: str | None = None
depends_on: str | None = None

_TENANT_EXPR = (
    "tenant_id = case when pg_input_is_valid("
    "current_setting('app.tenant_id', true), 'uuid') "
    "then current_setting('app.tenant_id', true)::uuid else null end"
)

_VERSION_STATUS_BEFORE = (
    "status IN ('UPLOADED', 'PENDING_APPROVAL', 'SCAN_FAILED', 'APPROVED', 'REJECTED', "
    "'BUILDING', 'READY', 'FAILED', 'TESTING', 'TEST_PASSED', 'TEST_FAILED')"
)
_VERSION_STATUS_AFTER = (
    "status IN ('UPLOADED', 'PENDING_APPROVAL', 'SCAN_FAILED', 'APPROVED', 'REJECTED', "
    "'BUILDING', 'READY', 'FAILED', 'TESTING', 'TEST_PASSED', 'TEST_FAILED', "
    "'PUBLISHED', 'OFFLINE', 'DELETED')"
)
_STATUS_TABLES = ("kb_documents", "kb_document_versions")


def upgrade() -> None:
    op.create_table(
        "kb_publish_aliases",
        sa.Column("document_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("alias", sa.String(length=200), nullable=False),
        sa.Column("collection", sa.String(length=200), nullable=False),
        sa.Column("version_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_publish_aliases_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["kb_documents.id", "kb_documents.tenant_id"],
            name="fk_kb_publish_aliases_document_id_kb_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["version_id", "tenant_id"],
            ["kb_document_versions.id", "kb_document_versions.tenant_id"],
            name="fk_kb_publish_aliases_version_id_kb_document_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_publish_aliases")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_kb_publish_aliases_id_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id", "document_id", name="uq_kb_publish_aliases_tenant_document"
        ),
    )
    op.create_index("ix_kb_publish_aliases_tenant_id", "kb_publish_aliases", ["tenant_id"])

    op.execute("ALTER TABLE kb_publish_aliases ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE kb_publish_aliases FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON kb_publish_aliases TO campus_elf_tenant "
        f"USING ({_TENANT_EXPR}) WITH CHECK ({_TENANT_EXPR})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON kb_publish_aliases TO campus_elf_tenant")

    for table in _STATUS_TABLES:
        op.drop_constraint(op.f(f"ck_{table}_status_valid"), table, type_="check")
        op.create_check_constraint(op.f(f"ck_{table}_status_valid"), table, _VERSION_STATUS_AFTER)


def downgrade() -> None:
    for table in _STATUS_TABLES:
        op.drop_constraint(op.f(f"ck_{table}_status_valid"), table, type_="check")
        op.create_check_constraint(op.f(f"ck_{table}_status_valid"), table, _VERSION_STATUS_BEFORE)
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON kb_publish_aliases")
    op.drop_table("kb_publish_aliases")
