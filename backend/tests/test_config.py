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


def _production_settings(**overrides: object) -> Settings:
    """构造一份满足生产必需项的最小配置。"""

    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "production",
        "redis_url": "redis://127.0.0.1:6379/0",
        "database_url": "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
        "platform_database_url": "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
        "migration_database_url": "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
        "qdrant_url": "http://127.0.0.1:6333",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_from": "no-reply@example.com",
        "smtp_username": "mailer",
        "smtp_password": "secret",
        "smtp_start_tls": True,
        "object_store_backend": "s3",
        "s3_endpoint_url": "https://s3.example.com",
        "s3_access_key": "ak",
        "s3_secret_key": "sk",
        "s3_bucket": "bucket",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_production_requires_platform_connection_but_not_migration_dsn() -> None:
    """运行进程只强制平台连接；DDL/迁移连接仅由迁移 Job 注入（避免超级用户凭据进应用）。"""

    with pytest.raises(ValidationError, match="PLATFORM_DATABASE_URL"):
        _production_settings(platform_database_url=None)

    settings = _production_settings(migration_database_url=None)
    assert settings.migration_database_url is None


def test_production_requires_real_smtp_with_tls() -> None:
    """生产禁止 MailHog/本机 SMTP，且必须启用 TLS/STARTTLS（二选一）。"""

    with pytest.raises(ValidationError, match="SMTP_HOST"):
        _production_settings(smtp_host="127.0.0.1")
    with pytest.raises(ValidationError, match="TLS"):
        _production_settings(smtp_start_tls=False, smtp_use_tls=False)
    with pytest.raises(ValidationError, match="SMTP_USERNAME"):
        _production_settings(smtp_username=None, smtp_password=None)
    with pytest.raises(ValidationError, match="只能启用一个"):
        _production_settings(smtp_use_tls=True, smtp_start_tls=True)


def test_production_rejects_dev_switches_and_local_storage() -> None:
    """生产禁止开发开关与本地对象存储（第一轮 M14 的配置失败路径）。"""

    with pytest.raises(ValidationError, match="DEV_SKIP_SCHOOL_REVIEW"):
        _production_settings(dev_skip_school_review=True)
    with pytest.raises(ValidationError, match="DEV_PLATFORM_REVIEW_TOKEN"):
        _production_settings(dev_platform_review_token="token")
    with pytest.raises(ValidationError, match="s3"):
        _production_settings(object_store_backend="local")
    with pytest.raises(ValidationError, match="S3_SECRET_KEY"):
        _production_settings(s3_secret_key=None)


def test_production_settings_accepts_complete_configuration() -> None:
    """完整生产配置可通过校验，且密钥字段以 SecretStr 保存（不随 repr 泄露）。"""

    settings = _production_settings()

    assert settings.smtp_password is not None
    assert settings.s3_secret_key is not None
    assert "smtp_password=SecretStr('**********')" in repr(settings)
    assert "s3_secret_key=SecretStr('**********')" in repr(settings)
