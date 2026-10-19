"""依赖就绪探活的单元测试（不依赖真实服务）。"""

import pytest

from app.config import Settings
from app.core import readiness
from app.core.readiness import DependencyCheck, _plain_postgres_dsn, collect_readiness
from tests.support.fakes import FAKE_REDIS_PASSWORD


def test_plain_postgres_dsn_strips_sqlalchemy_driver() -> None:
    """SQLAlchemy 风格 DSN 转成 asyncpg 可用格式。"""

    assert (
        _plain_postgres_dsn("postgresql+asyncpg://u:p@127.0.0.1:5432/db")
        == "postgresql://u:p@127.0.0.1:5432/db"
    )
    assert _plain_postgres_dsn("postgresql://u:p@127.0.0.1:5432/db") == (
        "postgresql://u:p@127.0.0.1:5432/db"
    )


async def test_unconfigured_dependencies_are_marked_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未配置的 PG/Qdrant 标记 not_configured，且不发起连接。"""

    async def fake_ok(settings: Settings) -> DependencyCheck:
        return DependencyCheck("redis", "ok")

    monkeypatch.setattr(readiness, "_check_redis", fake_ok)

    checks = await collect_readiness(
        Settings(redis_url="redis://127.0.0.1:6379/15", database_url=None, qdrant_url=None)
    )

    by_name = {check.name: check for check in checks}
    assert by_name["postgres"].status == "not_configured"
    assert by_name["qdrant"].status == "not_configured"
    assert by_name["redis"].status == "ok"


async def test_error_detail_is_readable_and_does_not_leak_credentials() -> None:
    """探活失败只回显异常类型，不带凭证。"""

    settings = Settings(redis_url=f"redis://:{FAKE_REDIS_PASSWORD}@127.0.0.1:1/0")

    check = await readiness._check_redis(settings)

    assert check.status == "error"
    assert check.detail
    assert FAKE_REDIS_PASSWORD not in check.detail
    assert "redis://" not in check.detail


async def test_invalid_redis_dsn_is_error_not_exception() -> None:
    """DSN 非法（缺 scheme）必须归入 error，不能冒泡成 500。"""

    settings = Settings(redis_url="127.0.0.1:56379")

    check = await readiness._check_redis(settings)

    assert check.status == "error"
    assert check.name == "redis"
