"""Alembic 环境（异步引擎）。

- 迁移目标元数据来自 `app.db.models`（所有模型必须在该包内导入）；
- 连接串优先取 `-x db_url=...`（测试用），否则取应用配置的 `DATABASE_URL`；
- 迁移在**事务**中执行（`transaction_per_migration`）。
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.db import models as _models  # noqa: F401  导入以注册全部表
from app.db.base import Base
from app.db.session import migration_url_or_default

config = context.config

if config.config_file_name is not None:
    # 显式 utf-8：Windows 默认 GBK 会读不了含中文注释的 alembic.ini；
    # disable_existing_loggers=False：迁移不得禁用应用/uvicorn 的既有 logger
    # （否则在应用进程内跑迁移会把日志整体关掉）。
    fileConfig(config.config_file_name, encoding="utf-8", disable_existing_loggers=False)

target_metadata = Base.metadata


def _resolve_url() -> str:
    """返回迁移使用的数据库连接串。"""

    x_args = context.get_x_argument(as_dictionary=True)
    if "db_url" in x_args:
        return x_args["db_url"]
    # 迁移优先用 MIGRATION_DATABASE_URL（DDL 需要更高权限，与运行时连接分离）
    return migration_url_or_default()


def run_migrations_offline() -> None:
    """离线模式：仅生成 SQL（不连接数据库）。"""

    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在给定连接上执行迁移。"""

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """在线模式：使用异步引擎执行迁移。"""

    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _resolve_url()
    engine = async_engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
