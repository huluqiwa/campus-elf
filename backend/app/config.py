"""应用配置。

所有配置通过环境变量或 .env 注入；密钥不得写入代码或日志。
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录的 .env（与 .env.example 同目录）；不读取 backend/.env，避免双份配置。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """应用级配置。

    参数:
        无。字段从环境变量或项目根目录 .env 读取。

    返回值:
        Settings 实例。

    异常:
        配置类型/校验失败时，pydantic 抛出 ValidationError。
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # 校验失败时不得回显输入值：输入包含连接串/口令（见 tests/test_config.py）。
        hide_input_in_errors=True,
    )

    app_env: Literal["development", "test", "staging", "production"] = "development"
    app_name: str = "CampusCompanionElf"
    app_version: str = "0.1.0"
    api_prefix: str = "/api/v1"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    docs_enabled: bool = True

    # 必填：无默认值，缺失即 fail-fast，禁止静默回退到无凭证的本机 Redis。
    redis_url: str
    # Redis 键/队列命名空间前缀；arq 队列名形如 `<redis_prefix>:queue`。
    redis_prefix: str = "campus_elf"

    # 数据库：租户侧应用连接（必须是**非超级用户**，否则 RLS 被绕过）；
    # production 下必须配置，见下方校验。
    database_url: str | None = None
    # 平台侧应用连接（独立角色，可读平台表与审计表）；平台路径必须配置它。
    platform_database_url: str | None = None
    # 迁移专用连接（DDL/建表/建策略）；缺省时回落到 database_url。
    migration_database_url: SecretStr | None = None
    qdrant_url: str | None = None

    # 开发/测试专用：空壳 Loop SSE（无 LLM）；生产默认关闭，开启也仅用于联调。
    dev_skeleton_loop_enabled: bool = False
    # 开发/测试专用：跳过学校资质审核（直接开通）；生产必须为 false（强制校验）。
    dev_skip_school_review: bool = False
    # 开发/测试专用：平台审核接口的临时令牌；M1-C 用正式登录替换后删除。
    # production 下即使配置也拒绝服务（见校验器）。
    dev_platform_review_token: SecretStr | None = None

    # 邮件（开发用 MailHog：127.0.0.1:1025；生产由 SMTP 服务商提供）
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_from: str = "no-reply@campus-elf.local"
    smtp_use_tls: bool = False
    # 587 端口通常用 STARTTLS；465 端口用隐式 TLS（smtp_use_tls=true）
    smtp_start_tls: bool = False
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None

    # 对象存储：开发/测试用本地目录（CI 可跑），生产用 S3/MinIO
    object_store_backend: Literal["local", "s3"] = "local"
    object_store_dir: str = ".zcode/tmp/objects"
    s3_endpoint_url: str | None = None
    s3_region: str = "auto"
    s3_access_key: str | None = None
    s3_secret_key: SecretStr | None = None
    s3_bucket: str = "campus-elf"

    @model_validator(mode="after")
    def _require_production_safety(self) -> "Settings":
        """生产环境必须满足的两类前提，避免带着开发形态上线。

        1. 依赖地址必须显式配置（不允许沿用开发默认/缺省）；
        2. 开发专用开关必须关闭——空壳 Loop 无认证，开启即等于开放未鉴权入口
           （"生产开关可绕过"按审查清单属阻塞项）。
        """

        if self.app_env != "production":
            return self

        if self.dev_skeleton_loop_enabled:
            raise ValueError(
                "production 环境禁止开启 DEV_SKELETON_LOOP_ENABLED（空壳 Loop 无认证）"
            )
        if self.dev_skip_school_review:
            raise ValueError("production 环境禁止开启 DEV_SKIP_SCHOOL_REVIEW（学校资质必须审核）")
        if self.dev_platform_review_token is not None:
            raise ValueError(
                "production 环境禁止配置 DEV_PLATFORM_REVIEW_TOKEN（审核必须走正式登录鉴权）"
            )

        missing = [
            name
            for name, value in (
                ("DATABASE_URL", self.database_url),
                ("PLATFORM_DATABASE_URL", self.platform_database_url),
                ("QDRANT_URL", self.qdrant_url),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"production 环境缺少必需配置: {', '.join(missing)}")
        if not self.smtp_host or self.smtp_host in {"127.0.0.1", "localhost", "mailhog"}:
            raise ValueError("production 环境必须配置真实 SMTP_HOST（禁止本机 MailHog）")
        if not self.smtp_from or self.smtp_from.endswith("@campus-elf.local"):
            raise ValueError("production 环境必须配置真实 SMTP_FROM")
        if not self.smtp_username or not self.smtp_password:
            raise ValueError("production 环境必须配置 SMTP_USERNAME / SMTP_PASSWORD")
        if not self.smtp_use_tls and not self.smtp_start_tls:
            raise ValueError("production 环境 SMTP 必须启用 TLS 或 STARTTLS")
        if self.smtp_use_tls and self.smtp_start_tls:
            raise ValueError("production 环境 SMTP_USE_TLS 与 SMTP_START_TLS 只能启用一个")
        if "@" not in (self.smtp_from or ""):
            raise ValueError("production 环境 SMTP_FROM 必须是合法邮箱地址")
        if self.object_store_backend != "s3":
            raise ValueError("production 环境对象存储必须使用 s3（MinIO/OSS）")
        missing_s3 = [
            name
            for name, value in (
                ("S3_ENDPOINT_URL", self.s3_endpoint_url),
                ("S3_ACCESS_KEY", self.s3_access_key),
                (
                    "S3_SECRET_KEY",
                    self.s3_secret_key.get_secret_value() if self.s3_secret_key else None,
                ),
                ("S3_BUCKET", self.s3_bucket),
            )
            if not value
        ]
        if missing_s3:
            raise ValueError(f"production 环境缺少对象存储配置: {', '.join(missing_s3)}")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程内缓存的配置实例。

    参数:
        无。

    返回值:
        Settings 实例。

    异常:
        pydantic.ValidationError: 配置校验失败。
    """

    return Settings()
