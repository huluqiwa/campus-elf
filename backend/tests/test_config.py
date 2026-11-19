"""配置校验测试（生产前提与敏感信息不外泄）。"""

import base64

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


def test_development_allows_optional_dependencies_and_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开发环境允许缺省依赖地址与开启空壳 Loop。"""

    # 全量跑时前序集成测试可能已设置 DATABASE_URL（防御性 delenv，确保默认值测试不受影响）
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(
        _env_file=None,
        app_env="development",
        redis_url="redis://127.0.0.1:6379/0",
    )

    assert settings.database_url is None
    assert settings.qdrant_url is None


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
        "jwt_secret": "production-test-jwt-secret-0123456789abcdef",
        "database_url": "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
        "platform_database_url": "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
        "migration_database_url": "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
        "qdrant_url": "http://127.0.0.1:6333",
        "auth_cookie_secure": True,
        "auth_refresh_token_in_body": False,
        "audit_hash_key": "production-audit-key-0123456789abcdef",
        "password_reset_url_base": "https://admin.example.com/reset-password",
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
    with pytest.raises(ValidationError, match="DEV_SKIP_FILE_SCAN"):
        _production_settings(dev_skip_file_scan=True)
    with pytest.raises(ValidationError, match="CLAMAV_FAIL_CLOSED"):
        _production_settings(clamav_fail_closed=False)
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _production_settings(jwt_secret="short")
    with pytest.raises(ValidationError, match="s3"):
        _production_settings(object_store_backend="local")
    with pytest.raises(ValidationError, match="S3_SECRET_KEY"):
        _production_settings(s3_secret_key=None)


def test_non_dev_requires_https_password_reset_url() -> None:
    """重置令牌走邮件链接，非开发环境必须 HTTPS（禁止明文链路泄露令牌）。"""

    with pytest.raises(ValidationError, match="PASSWORD_RESET_URL_BASE"):
        _staging_settings(password_reset_url_base="http://admin.example.com/reset-password")
    with pytest.raises(ValidationError, match="PASSWORD_RESET_URL_BASE"):
        _production_settings(password_reset_url_base="http://admin.example.com/reset-password")


@pytest.mark.parametrize(
    "value",
    [
        "/reset-password",
        "https://admin.example.com/reset-password?next=/home",
        "https://admin.example.com/reset-password#token=old",
        "https://user:secret@admin.example.com/reset-password",
    ],
)
def test_password_reset_url_base_must_be_clean_absolute_url(value: str) -> None:
    """拼接 `#token=` 前必须拒绝相对 URL、查询串、fragment 与内嵌凭证。"""

    with pytest.raises(ValidationError, match="PASSWORD_RESET_URL_BASE"):
        _staging_settings(password_reset_url_base=value)


def test_production_settings_accepts_complete_configuration() -> None:
    """完整生产配置可通过校验，且密钥字段以 SecretStr 保存（不随 repr 泄露）。"""

    settings = _production_settings()

    assert settings.smtp_password is not None
    assert settings.s3_secret_key is not None
    assert "smtp_password=SecretStr('**********')" in repr(settings)
    assert "s3_secret_key=SecretStr('**********')" in repr(settings)


def _staging_settings(**overrides: object) -> Settings:
    """构造 staging 配置（只需依赖地址即可通过后续校验）。"""

    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "staging",
        "redis_url": "redis://127.0.0.1:6379/0",
        "database_url": "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
        "platform_database_url": "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
        "qdrant_url": "http://127.0.0.1:6333",
        "jwt_secret": "9f" * 32,
        "auth_cookie_secure": True,
        "auth_refresh_token_in_body": False,
        "audit_hash_key": "production-audit-key-0123456789abcdef",
        "password_reset_url_base": "https://admin.example.com/reset-password",
        "smtp_host": "smtp.example.com",
        "smtp_from": "no-reply@example.com",
        "smtp_username": "mailer",
        "smtp_password": "secret",
        "smtp_start_tls": True,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "secret",
    ["", "short", "change-me", "change-me-please-generate", "my-example-key", "your-secret-here"],
)
def test_staging_rejects_weak_or_placeholder_jwt_secret(secret: str) -> None:
    """staging 与 production 同等对待：弱/占位/空密钥必须拒绝启动。"""

    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _staging_settings(jwt_secret=secret)


def test_staging_accepts_strong_jwt_secret() -> None:
    """64 位十六进制随机密钥可通过 staging 校验。"""

    settings = _staging_settings()

    assert settings.jwt_secret.get_secret_value() == "9f" * 32


def test_non_dev_requires_encrypted_smtp() -> None:
    """重置令牌经邮件传输：staging/production 禁止 MailHog 与明文 SMTP。"""

    with pytest.raises(ValidationError, match="SMTP_HOST"):
        _staging_settings(smtp_host="mailhog")
    with pytest.raises(ValidationError, match="SMTP"):
        _staging_settings(smtp_start_tls=False, smtp_use_tls=False)
    with pytest.raises(ValidationError, match="SMTP_USERNAME"):
        _staging_settings(smtp_username=None)


def test_development_skips_jwt_strength_check() -> None:
    """开发/测试环境不强制密钥强度（本地与 CI 用固定测试值）。"""

    settings = Settings(
        _env_file=None,
        redis_url="redis://127.0.0.1:6379/15",
        jwt_secret="dev",
    )

    assert settings.app_env == "development"


def test_staging_rejects_file_scan_bypass() -> None:
    """staging 同样禁止跳过文件扫描与放开 fail-closed（否则未扫描文件可进入审批）。"""

    with pytest.raises(ValidationError, match="DEV_SKIP_FILE_SCAN"):
        _staging_settings(dev_skip_file_scan=True)
    with pytest.raises(ValidationError, match="CLAMAV_FAIL_CLOSED"):
        _staging_settings(clamav_fail_closed=False)


def test_development_allows_file_scan_bypass() -> None:
    """开发/测试允许跳过扫描与放开 fail-closed；扫描默认 fail-closed。"""

    settings = Settings(
        _env_file=None,
        redis_url="redis://127.0.0.1:6379/15",
        dev_skip_file_scan=True,
        clamav_fail_closed=False,
    )

    assert settings.clamav_host == "127.0.0.1"
    assert settings.clamav_port == 3310
    assert settings.clamav_timeout_seconds == 120.0
    assert settings.dev_skip_file_scan is True

    defaults = Settings(_env_file=None, redis_url="redis://127.0.0.1:6379/15")
    assert defaults.dev_skip_file_scan is False
    assert defaults.clamav_fail_closed is True


def test_staging_requires_secure_refresh_cookie() -> None:
    """staging/production 的刷新令牌 Cookie 必须仅走 HTTPS（本地开发/测试除外）。"""

    with pytest.raises(ValidationError, match="AUTH_COOKIE_SECURE"):
        _staging_settings(auth_cookie_secure=False)

    with pytest.raises(ValidationError, match="AUTH_COOKIE_SECURE"):
        _production_settings(auth_cookie_secure=False)


def test_staging_requires_refresh_token_only_in_cookie() -> None:
    """staging/production 不得在响应体回显刷新令牌，且禁止 SameSite=none、Cookie 路径需联动。"""

    with pytest.raises(ValidationError, match="AUTH_REFRESH_TOKEN_IN_BODY"):
        _staging_settings(auth_refresh_token_in_body=True)

    with pytest.raises(ValidationError, match="SAMESITE"):
        _staging_settings(auth_cookie_samesite="none")

    with pytest.raises(ValidationError, match="AUTH_COOKIE_PATH"):
        _staging_settings(auth_cookie_path="/auth")


def test_production_cookie_settings_defaults_are_valid() -> None:
    """默认 Cookie 配置（lax + /api/v1/auth + 不回显）可通过生产校验。"""

    settings = _production_settings()

    assert settings.auth_refresh_token_in_body is False
    assert settings.auth_cookie_path == "/api/v1/auth"


def test_encryption_kek_must_be_base64_32_bytes() -> None:
    """KEK 在所有环境都必须是 base64 编码的 32 字节（否则启动失败）。"""

    with pytest.raises(ValidationError, match="ENCRYPTION_KEK"):
        Settings(_env_file=None, encryption_kek="not-base64!!")
    with pytest.raises(ValidationError, match="ENCRYPTION_KEK"):
        Settings(
            _env_file=None,
            encryption_kek=base64.b64encode(b"x" * 16).decode(),
        )


def test_non_dev_rejects_placeholder_encryption_kek() -> None:
    """staging/production 拒绝明显占位值的 KEK；development 允许。"""

    placeholder = base64.b64encode(b"change-me" * 3 + b"abcde").decode()

    with pytest.raises(ValidationError, match="占位"):
        _staging_settings(encryption_kek=placeholder)

    settings = Settings(_env_file=None, encryption_kek=placeholder)
    assert settings.app_env == "development"


def test_mfa_defaults() -> None:
    """MFA 默认：平台不强制、30 秒步长、±1 步、10 个恢复码。"""

    settings = Settings(_env_file=None)

    assert settings.mfa_require_platform is False
    assert settings.mfa_totp_interval_seconds == 30
    assert settings.mfa_totp_window_steps == 1
    assert settings.mfa_challenge_ttl_seconds == 300
    assert settings.mfa_challenge_max_attempts == 5
    assert settings.mfa_recovery_code_count == 10
    assert "encryption_kek=SecretStr('**********')" in repr(settings)


def test_staging_rejects_weak_audit_hash_key() -> None:
    """staging/production 的审计哈希密钥必须 ≥32 字节且非占位值。"""

    for bad in ("", "short", "change-me", "my-example-key", "your-secret-here"):
        with pytest.raises(ValidationError, match="AUDIT_HASH_KEY"):
            _staging_settings(audit_hash_key=bad)


def test_production_audit_hash_key_defaults_are_valid() -> None:
    """合法审计密钥可通过生产校验。"""

    settings = _production_settings()

    assert settings.audit_hash_key is not None
    assert settings.audit_hash_key.get_secret_value() == "production-audit-key-0123456789abcdef"


# ---------- LLM（M4-AI01a：OpenAI 兼容适配器） ----------


def test_llm_defaults_unset_and_a8_timeouts() -> None:
    """LLM 缺省未配置；超时/重试/熔断默认值对齐 A8 映射（5s/15s/2 重试/5 次 60s 开 30s）。"""

    settings = Settings(_env_file=None)

    assert settings.llm_api_key is None
    assert settings.llm_base_url is None
    assert settings.llm_model is None
    assert settings.llm_connect_timeout_seconds == 5.0
    assert settings.llm_first_token_timeout_seconds == 15.0
    assert settings.llm_max_attempts == 3
    assert settings.llm_breaker_failure_threshold == 5
    assert settings.llm_breaker_window_seconds == 60.0
    assert settings.llm_breaker_open_seconds == 30.0
    assert settings.llm_retry_backoff_seconds == 0.5


def test_llm_config_group_must_be_complete() -> None:
    """key/base_url/model 必须成组出现：只配 base_url 不配 key 即 fail-fast。"""

    with pytest.raises(ValidationError, match="LLM"):
        Settings(
            _env_file=None,
            llm_base_url="https://api.example.com/v1",
            llm_model="example-llm",
        )


def test_llm_config_complete_group_accepted() -> None:
    """成组配置可通过（dev）。"""

    settings = Settings(
        _env_file=None,
        llm_api_key="dev-llm-key",
        llm_base_url="https://api.example.com/v1",
        llm_model="example-llm",
    )

    assert settings.llm_api_key is not None
    assert settings.llm_api_key.get_secret_value() == "dev-llm-key"
    assert settings.llm_model == "example-llm"


def test_non_dev_rejects_placeholder_llm_key() -> None:
    """staging/production 禁止占位 LLM key（对齐 JWT_SECRET 占位值口径）。"""

    with pytest.raises(ValidationError, match="LLM_API_KEY"):
        _staging_settings(
            llm_api_key="change-me",
            llm_base_url="https://api.example.com/v1",
            llm_model="example-llm",
        )


def test_non_dev_rejects_llm_base_url_with_credentials() -> None:
    """LLM_BASE_URL 禁止携带凭证/查询串/fragment（URL 里的 key 会进日志与异常）。"""

    with pytest.raises(ValidationError, match="LLM_BASE_URL"):
        _staging_settings(
            llm_api_key="real-key-0123456789",
            llm_base_url="https://user:pass@api.example.com/v1",
            llm_model="example-llm",
        )


def test_production_allows_unconfigured_llm() -> None:
    """chat 未上线前生产允许缺省 LLM 配置（调用期 fail-closed，不做启动阻断）。"""

    settings = _production_settings()

    assert settings.llm_api_key is None
    assert settings.llm_base_url is None


def test_production_accepts_complete_llm_config() -> None:
    """生产配置完整 LLM（真实端点）时可通过校验。"""

    settings = _production_settings(
        llm_api_key="production-llm-key-0123456789",
        llm_base_url="https://api.production-llm.test/v1",
        llm_model="production-llm",
    )

    assert settings.llm_api_key is not None
    assert settings.llm_model == "production-llm"


def test_non_dev_rejects_placeholder_llm_base_url() -> None:
    """staging/production 禁止示例端点 api.example.com（审查 m5：运行期才失败不如启动即拦）。"""

    with pytest.raises(ValidationError, match=r"api\.example\.com"):
        _staging_settings(
            llm_api_key="real-llm-key-0123456789",
            llm_base_url="https://api.example.com/v1",
            llm_model="example-llm",
        )


def test_llm_blank_strings_do_not_bypass_group_check() -> None:
    """空白 base_url/model 不得绕过成组校验（审查 m6：bool(" ") 为 True）。"""

    for field, value in (("llm_base_url", "   "), ("llm_model", "\t")):
        kwargs: dict[str, object] = {
            "llm_api_key": "dev-llm-key",
            "llm_base_url": "https://api.example.com/v1",
            "llm_model": "some-model",
        }
        kwargs[field] = value
        with pytest.raises(ValidationError, match="成组出现"):
            Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]
