"""配置校验测试（生产前提与敏感信息不外泄）。"""

import pytest
from pydantic import ValidationError

from app.config import Settings
from tests.support.fakes import FAKE_REDIS_PASSWORD


def test_production_requires_dependencies() -> None:
    """production 缺少 DATABASE_URL/QDRANT_URL 时启动失败。"""

    with pytest.raises(ValidationError, match="production 环境缺少必需配置"):
        Settings(
            _env_file=None,
            app_env="production",
            redis_url="redis://127.0.0.1:6379/0",
        )


def test_production_rejects_skeleton_loop_switch() -> None:
    """production 禁止开启空壳 Loop 开关（防止未鉴权入口上线）。"""

    with pytest.raises(ValidationError, match="DEV_SKELETON_LOOP_ENABLED"):
        Settings(
            _env_file=None,
            app_env="production",
            redis_url="redis://127.0.0.1:6379/0",
            dev_skeleton_loop_enabled=True,
            database_url="postgresql+asyncpg://u:p@127.0.0.1:5432/db",
            qdrant_url="http://127.0.0.1:6333",
        )


def test_development_allows_optional_dependencies_and_switch() -> None:
    """开发环境允许缺省依赖地址与开启空壳 Loop。"""

    settings = Settings(
        _env_file=None,
        app_env="development",
        redis_url="redis://127.0.0.1:6379/0",
        dev_skeleton_loop_enabled=True,
    )

    assert settings.database_url is None
    assert settings.qdrant_url is None
    assert settings.dev_skeleton_loop_enabled is True


def test_validation_error_does_not_echo_credentials() -> None:
    """校验失败信息不得回显连接串/口令（hide_input_in_errors）。"""

    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            app_env="production",
            redis_url=f"redis://:{FAKE_REDIS_PASSWORD}@127.0.0.1:6379/0",
        )

    message = str(excinfo.value)
    assert "production" in message
    assert FAKE_REDIS_PASSWORD not in message
    assert "redis://" not in message
