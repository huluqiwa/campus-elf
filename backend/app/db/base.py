"""SQLAlchemy 声明基类与通用列 mixin。

约定（对齐 `docs/design.md` §4）：
- 主键 `id UUID`（PG 内置 `gen_random_uuid()`）；
- 时间戳 `created_at`/`updated_at` 为 `timestamptz`；
- **租户级表必须带 `tenant_id UUID NOT NULL`**（`TenantScopedMixin`），
  这是 RLS 策略与仓储强制过滤的前提；平台级表（tenants/platform_*）不带。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, MetaData, func, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 统一约束命名，保证 Alembic 迁移可重复生成且不产生匿名约束。
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有模型的声明基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    """`id UUID` 主键（数据库侧生成，便于迁移与批量导入）。"""

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


class TimestampMixin:
    """创建/更新时间戳（服务端时间，避免客户端时钟偏差）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class TenantScopedMixin:
    """租户级表统一列：`tenant_id NOT NULL` + 索引。

    带此 mixin 的表必须同时启用 RLS（见基线迁移与 `tests/integration/test_rls_*.py`），
    并且只能通过 `app/db/repository.py` 的租户级仓储访问。
    """

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
