"""测试环境护栏：确保连接串指向一次性测试资源（防止误连业务库）。

- Redis：必须显式带 DB 段且 DB 号 ≥ `MIN_DISPOSABLE_DB`（项目业务库为 DB 0）。
- PostgreSQL：库名必须以 `_test` 结尾。
"""

from urllib.parse import urlparse

import pytest

MIN_DISPOSABLE_DB = 10


def assert_disposable_redis_dsn(dsn: str) -> str:
    """校验 Redis DSN 指向一次性 DB。

    参数:
        dsn: TEST_REDIS_URL 的值。

    返回值:
        原样返回的 DSN。

    异常:
        pytest.fail: 未显式指定一次性 DB。
    """

    database_segment = urlparse(dsn).path.strip("/")
    if not database_segment.isdigit() or int(database_segment) < MIN_DISPOSABLE_DB:
        pytest.fail(
            "TEST_REDIS_URL 必须显式指向一次性 DB（如 redis://:pw@127.0.0.1:6379/15）；"
            f"当前 DB 段为 {database_segment!r}，禁止对业务库/默认 DB 0 运行集成测试。"
        )
    return dsn


def assert_test_database_dsn(dsn: str) -> str:
    """校验 PostgreSQL DSN 指向测试库（库名以 `_test` 结尾）。

    参数:
        dsn: TEST_DATABASE_URL 的值。

    返回值:
        原样返回的 DSN。

    异常:
        pytest.fail: 库名不符合测试库约定。
    """

    database_name = urlparse(dsn).path.strip("/")
    if not database_name.endswith("_test"):
        pytest.fail(
            "TEST_DATABASE_URL 必须指向测试库（库名以 `_test` 结尾）；"
            f"当前库名为 {database_name!r}，禁止对开发/业务库运行集成测试。"
        )
    return dsn
