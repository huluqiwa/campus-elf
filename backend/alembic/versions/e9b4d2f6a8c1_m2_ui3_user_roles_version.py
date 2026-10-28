"""M2-UI3：`users.roles_version`——角色绑定集的全量替换乐观锁（TD-187d）。

为什么不用既有列：

- `token_version` 的语义是"令牌作废代数"，绑定时不能碰它——权限每请求从库里重算，
  绑定要**立刻生效且不踢会话**（简报 §14.1）；借它做 CAS 会把正常绑定变成一次强制重登。
- `roles.version` 是角色自身的乐观锁，绑定不改角色行；拿它当绑定锁会让一个人在 A 角色上
  的绑定操作与另一个人的权限编辑互撞（同一角色被两人同时编辑是正常场景）。

单列一列，取值只由 `PUT /school/users/{user_id}/roles` 推进；不参与任何鉴权判定，
仅用于"预览→确认"之间的并发检测（简报 §14.4）。

downgrade：只删本列，不动数据。
"""

import sqlalchemy as sa
from alembic import op

revision: str = "e9b4d2f6a8c1"
down_revision: str | None = "d7a1c3e5b9f4"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("roles_version", sa.Integer(), server_default="0", nullable=False),
        schema="public",
    )


def downgrade() -> None:
    op.drop_column("users", "roles_version", schema="public")
