"""M2-ORG04（复审 M3）：身份证指纹同租唯一兜底索引。

`docs/design-briefs/M2-ORG04-id-card.md`（D2-A 写入路径；独立审查 M2-ORG04 复审 M3）：

服务层 HMAC 查重是 check-then-act，并发写入存在竞窗；
部分唯一索引（`WHERE id_card_hmac IS NOT NULL`，未采集不参与唯一性）
把「同租同指纹至多一行」下沉到数据库，IntegrityError 由服务层翻译为 409
`ID_CARD_DUPLICATE`（仅识别 `*idcard_hmac*` 索引名，其他唯一冲突原样上抛）。

跨表（教师↔学生）并发同号仍由服务层两表查询兜底，残余竞窗登记 TD-180。

downgrade：摘索引（列位不动）。
"""

import sqlalchemy as sa
from alembic import op

revision: str = "f8b2d6a0c4e8"
down_revision: str | None = "a4c8e2f6b1d3"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "uq_teachers_idcard_hmac",
        "teachers",
        ["tenant_id", "id_card_hmac"],
        unique=True,
        postgresql_where=sa.text("id_card_hmac IS NOT NULL"),
    )
    op.create_index(
        "uq_students_idcard_hmac",
        "students",
        ["tenant_id", "id_card_hmac"],
        unique=True,
        postgresql_where=sa.text("id_card_hmac IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_students_idcard_hmac", table_name="students")
    op.drop_index("uq_teachers_idcard_hmac", table_name="teachers")
