"""迁移回归用的临时数据库工具（与 `tests/integration/test_migration_rollback.py` 同一套路）。

为什么单独成模块：涉及「升级到某版本 → 写数据 → 降级 → 再升级」的迁移，必须在**独立临时库**
里跑，否则会破坏主测试库的模块级 fixture（其他用例假设库结构已是 head）。
临时库名一律以主测试库名为前缀，避免误操作非测试库。
"""

import asyncpg
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from tests.support.db import BACKEND_ROOT


def alembic_config(url: str) -> Config:
    """构造指向指定数据库的 Alembic 配置（`-x db_url=`）。"""

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.cmd_opts = type("Args", (), {"x": [f"db_url={url}"]})()
    return config


def temp_database_url(admin_url: str, suffix: str) -> tuple[str, str, str]:
    """由主测试库连接串派生临时库的 DSN / URL / 库名。

    返回值:
        `(admin_dsn, temp_url, temp_database)`：
        admin_dsn 用于 asyncpg 建删库，temp_url 交给 Alembic。

    异常:
        AssertionError：连接串缺少数据库名（无法派生前缀安全的临时库名）。
    """

    parsed = make_url(admin_url)
    if not parsed.database:
        raise AssertionError(f"测试连接串缺少数据库名: {admin_url}")
    temp_database = f"{parsed.database}_{suffix}"
    admin_dsn = admin_url.replace("postgresql+asyncpg://", "postgresql://")
    temp_url = admin_url.rsplit("/", 1)[0] + f"/{temp_database}"
    return admin_dsn, temp_url, temp_database


async def _execute_on_admin(admin_dsn: str, statement: str) -> None:
    """在管理连接上执行一条 DDL（asyncpg 的 CREATE/DROP DATABASE 不能带参数）。"""

    connection = await asyncpg.connect(admin_dsn)
    try:
        await connection.execute(statement)
    finally:
        await connection.close()


async def create_database(admin_dsn: str, database: str) -> None:
    """创建临时数据库（先删除同名残留，保证从空库开始）。

    异常:
        AssertionError：库名不以 `_test` 结尾（防止误建/误删非测试库）。
    """

    if "_test" not in database:
        raise AssertionError(f"拒绝在非测试库上操作：{database}")
    await _execute_on_admin(admin_dsn, f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    await _execute_on_admin(admin_dsn, f'CREATE DATABASE "{database}"')


async def drop_database(admin_dsn: str, database: str) -> None:
    """删除临时数据库。"""

    if "_test" not in database:
        raise AssertionError(f"拒绝在非测试库上操作：{database}")
    await _execute_on_admin(admin_dsn, f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


def upgrade(config: Config, revision: str = "head") -> None:
    """执行 `alembic upgrade`。"""

    command.upgrade(config, revision)


def downgrade(config: Config, revision: str) -> None:
    """执行 `alembic downgrade`。"""

    command.downgrade(config, revision)
