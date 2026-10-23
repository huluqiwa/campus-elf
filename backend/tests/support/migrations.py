"""迁移回归用的临时数据库工具（与 `tests/integration/test_migration_rollback.py` 同一套路）。

为什么单独成模块：涉及「升级到某版本 → 写数据 → 降级 → 再升级」的迁移，必须在**独立临时库**
里跑，否则会破坏主测试库的模块级 fixture（其他用例假设库结构已是 head）。
临时库名一律以主测试库名为前缀，避免误操作非测试库。
"""

from typing import Any

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


async def scalar(dsn: str, sql: str, *args: Any) -> Any:
    """临时库上取一个标量（迁移往返断言用；每次单独连接，避免跨连接看到旧状态）。"""

    connection = await asyncpg.connect(dsn)
    try:
        return await connection.fetchval(sql, *args)
    finally:
        await connection.close()


async def fetch(dsn: str, sql: str, *args: Any) -> list[asyncpg.Record]:
    """临时库上取多行。"""

    connection = await asyncpg.connect(dsn)
    try:
        rows: list[asyncpg.Record] = await connection.fetch(sql, *args)
        return rows
    finally:
        await connection.close()


async def table_exists(dsn: str, table_name: str) -> bool:
    """`public` 下该表是否存在。"""

    value = await scalar(
        dsn,
        """
        select count(*) from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'public' and c.relname = $1
        """,
        table_name,
    )
    return int(value) > 0


async def column_exists(dsn: str, table_name: str, column_name: str) -> bool:
    """`public.<table>` 上该列是否存在。"""

    value = await scalar(
        dsn,
        """
        select count(*) from pg_attribute a
        join pg_class c on c.oid = a.attrelid
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'public' and c.relname = $1
          and a.attname = $2 and a.attnum > 0 and not a.attisdropped
        """,
        table_name,
        column_name,
    )
    return int(value) > 0


async def constraint_exists(dsn: str, constraint_name: str) -> bool:
    """该约束是否存在（不区分类型：CHECK / FK / UNIQUE 都查）。"""

    value = await scalar(
        dsn, "select count(*) from pg_constraint con where con.conname = $1", constraint_name
    )
    return int(value) > 0


async def function_defines(dsn: str, function_name: str) -> str:
    """函数体原文（`pg_get_functiondef`），用于断言迁移确实换掉了函数。

    异常:
        AssertionError：该 `public` 函数不存在。
    """

    value = await scalar(
        dsn,
        """
        select pg_get_functiondef(p.oid)
        from pg_proc p join pg_namespace n on n.oid = p.pronamespace
        where n.nspname = 'public' and p.proname = $1
        """,
        function_name,
    )
    if value is None:
        raise AssertionError(f"函数不存在：{function_name}")
    return str(value)


async def role_can_select(dsn: str, role_name: str, table_name: str) -> bool:
    """某个数据库角色对该表是否有 SELECT 权限（授权面也是迁移要钉住的口径）。"""

    value = await scalar(
        dsn, "select has_table_privilege($1, 'public.' || $2, 'SELECT')", role_name, table_name
    )
    return bool(value)

