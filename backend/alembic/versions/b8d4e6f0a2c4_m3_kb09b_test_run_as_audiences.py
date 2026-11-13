"""M3 KB-09B：测试运行视角人群——kb_test_runs.as_audiences JSONB 列。

`docs/design-briefs/M3-KB-09.md` §3.2.3（2026-11-14 决策卡 B3 经用户拍板）：批量
测试运行默认按发起人（`started_by`）执行期实时解析人群 token；可选 `as_audiences`
（诊断用假人群 token 字符串数组）随 run 持久化，worker 执行期使用。复用
`metrics_json` 被否——语义不同列（metrics=运行指标，as_audiences=判分视角）。

- `as_audiences JSONB NULL`：null=默认操作者视角；非空=字符串数组（结构校验在
  服务层 `parse_audience_tokens`，创建时 422）；
- 批量运行**不加** `as_user_id`/`ignore_acl`（判分宁严勿松，简报卡 3/B3）。

downgrade：删列。
**数据前提**：无（新列可空，downgrade 无数据迁移要求）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "b8d4e6f0a2c4"
down_revision: str | None = "a5c7e9f1b3d7"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "kb_test_runs",
        sa.Column("as_audiences", pg.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("kb_test_runs", "as_audiences")
