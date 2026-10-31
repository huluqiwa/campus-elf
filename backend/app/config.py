"""应用配置。

所有配置通过环境变量或 .env 注入；密钥不得写入代码或日志。
"""

import base64
import binascii
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
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
    # 可信反向代理地址（逗号分隔）；仅直连对端命中时才信任 X-Forwarded-For
    trusted_proxy_ips: str = ""
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    docs_enabled: bool = True
    # Prometheus /metrics 开关：默认关闭（不新增默认暴露面）；开启后仍须由网络策略限制
    # 到内网抓取端（见 deploy/observability/README.md）。指标标签不含租户/用户标识。
    metrics_enabled: bool = False

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

    # ---------- 认证与令牌（M1-C） ----------
    # JWT 签名密钥：只从 .env/密钥管理注入；≥32 字节，生产禁止占位值。
    jwt_secret: SecretStr
    jwt_algorithm: Literal["HS256"] = "HS256"
    jwt_issuer: str = "campus-elf"
    # realm 分离：租户与平台令牌的 audience 不同，跨 realm 令牌一律拒绝（TD-074）
    jwt_audience_tenant: str = "campus-elf-tenant"
    jwt_audience_platform: str = "campus-elf-platform"
    # 密钥版本（header kid）；轮换时可并存多个 kid，当前签发用 active
    jwt_active_kid: str = "k1"
    # MFA 挑战/强制绑定令牌的独立 audience（不能当 access token 使用）
    jwt_audience_mfa: str = "campus-elf-mfa"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 604800
    # 同一账号同时保持登录的设备上限：超限踢最久未用（平台账号权限更大，默认更严）
    max_sessions_per_user_tenant: int = Field(default=3, ge=1)
    max_sessions_per_user_platform: int = Field(default=2, ge=1)
    # 浏览器端刷新令牌走 HttpOnly Cookie（JS 读不到）；非浏览器客户端仍可用响应体字段
    auth_cookie_name: str = "campus_refresh_token"
    auth_cookie_path: str = "/api/v1/auth"
    auth_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    # 是否要求 HTTPS 才发送 Cookie；staging/production 强制 true（见校验器）
    auth_cookie_secure: bool = False
    # 是否在响应体回显刷新令牌：仅开发/测试需要（自动化测试与非浏览器客户端）；
    # staging/production 必须 false——否则 XSS 可读走长期凭证，抵消 HttpOnly 的意义（审查 MA3）
    auth_refresh_token_in_body: bool = True
    # 审计哈希链密钥（M1-F）：≥32 字节随机值，与 JWT_SECRET/ENCRYPTION_KEK 相互独立。
    # 缺失时（dev/test）审计仍写入但标 hash_version=0 不入链（fail-open，链校验可检测此类行）
    audit_hash_key: SecretStr | None = None
    # 身份证加密密钥（M2-ORG04）：base64 32 字节，与 ENCRYPTION_KEK/审计链密钥相互独立（D1 决策）。
    # 可选（复审登记）：任何环境缺失时身份证写入/查看接口返回 501（服务层门控，不阻塞启动）；
    # 生产部署应配置（见 .env.example ID_CARD_KEK），轮换流程延后 TD（简报 §6）。
    id_card_kek: SecretStr | None = None

    # ---------- 密码策略 / 失败锁定 / 邮件重置（M1-D1） ----------
    # 同一账号在窗口内连续失败达到上限即锁定（AUTH-05）
    auth_max_login_failures: int = Field(default=5, ge=1)
    auth_login_failure_window_seconds: int = Field(default=900, ge=1)
    auth_lockout_seconds: int = Field(default=900, ge=1)
    # 记住最近多少个旧密码（拒绝复用）
    auth_password_history_size: int = Field(default=5, ge=1)
    # 重置令牌 TTL 与申请冷却（同一账号两次申请的最小间隔）
    password_reset_token_ttl_seconds: int = Field(default=1800, ge=60)
    password_reset_request_cooldown_seconds: int = Field(default=60, ge=0)
    # 同一来源 IP 的固定窗口申请上限（防邮件轰炸/账号枚举）
    password_reset_ip_limit: int = Field(default=20, ge=1)
    # 确认接口单独计数：伪造令牌不触达账号，但会写失败审计，需限制放大
    password_reset_confirm_ip_limit: int = Field(default=30, ge=1)
    password_reset_ip_window_seconds: int = Field(default=900, ge=1)
    # 重置邮件里的前端链接前缀（confirm 接口由前端调用）；非开发环境必须 HTTPS
    password_reset_url_base: str = "http://127.0.0.1:5173/reset-password"

    # ---------- MFA（M1-D2） ----------
    # MFA secret 与恢复码派生密钥：base64 编码的 32 字节；只从 .env/密钥管理注入。
    # 生成：openssl rand -base64 32
    encryption_kek: SecretStr
    # 平台 realm 强制 MFA（默认关闭；生产部署可按需开启）。租户用户始终可选。
    mfa_require_platform: bool = False
    mfa_totp_issuer: str = "CampusCompanionElf"
    mfa_totp_interval_seconds: int = Field(default=30, ge=15, le=60)
    # 允许的时钟偏移（步数）：±1 步 = ±30 秒；0 表示只接受当前步
    mfa_totp_window_steps: int = Field(default=1, ge=0, le=2)
    mfa_challenge_ttl_seconds: int = Field(default=300, ge=60, le=900)
    mfa_enroll_token_ttl_seconds: int = Field(default=600, ge=120, le=1800)
    # 单次挑战最多尝试次数（失败同时计入 AUTH-05 账号锁定）
    mfa_challenge_max_attempts: int = Field(default=5, ge=1, le=10)
    mfa_recovery_code_count: int = Field(default=10, ge=5, le=20)

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

    # ---------- ClamAV（M3-KB-01 上传扫描） ----------
    # 扫描异步执行；不可用/超时时版本保持 UPLOADED 等重试（fail-closed，绝不放行未扫描文件）。
    clamav_host: str = "127.0.0.1"
    clamav_port: int = Field(default=3310, ge=1, le=65535)
    clamav_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    clamav_fail_closed: bool = True
    # 开发/测试专用：跳过扫描直接把版本置为 PENDING_APPROVAL；非开发环境必须 false。
    dev_skip_file_scan: bool = False

    def _validate_encryption_kek(self) -> None:
        """校验字段加密密钥：必须是 base64 编码的 32 字节。"""

        raw = self.encryption_kek.get_secret_value().strip()
        try:
            decoded = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                "ENCRYPTION_KEK 必须是 base64 编码的 32 字节密钥"
                "（生成：openssl rand -base64 32）"
            ) from exc
        if len(decoded) != 32:
            raise ValueError(
                "ENCRYPTION_KEK 必须是 base64 编码的 32 字节密钥"
                "（生成：openssl rand -base64 32）"
            )
        if self.app_env not in {"development", "test"}:
            # 占位值通常整体不是合法 base64；这里额外检查"被 base64 编码过的占位短语"
            lowered = raw.lower()
            lowered_bytes = decoded.lower()
            markers = (b"change-me", b"changeme", b"placeholder")
            if "change-me" in lowered or "changeme" in lowered or "placeholder" in lowered:
                raise ValueError(
                    f"{self.app_env} 环境 ENCRYPTION_KEK 禁止使用占位值"
                    "（生成：openssl rand -base64 32）"
                )
            if any(marker in lowered_bytes for marker in markers):
                raise ValueError(
                    f"{self.app_env} 环境 ENCRYPTION_KEK 禁止使用占位值"
                    "（生成：openssl rand -base64 32）"
                )

    @model_validator(mode="after")
    def _require_production_safety(self) -> "Settings":
        """生产环境必须满足的两类前提，避免带着开发形态上线。

        1. 依赖地址必须显式配置（不允许沿用开发默认/缺省）；
        2. 开发专用开关必须关闭——空壳 Loop 无认证，开启即等于开放未鉴权入口
           （"生产开关可绕过"按审查清单属阻塞项）。
        """

        self._validate_encryption_kek()

        reset_url = urlsplit(self.password_reset_url_base)
        if (
            reset_url.scheme not in {"http", "https"}
            or not reset_url.netloc
            or reset_url.username is not None
            or reset_url.password is not None
            or reset_url.query
            or reset_url.fragment
        ):
            raise ValueError(
                "PASSWORD_RESET_URL_BASE 必须是绝对 URL，且不能包含凭证/查询串/fragment"
            )

        if self.app_env in {"development", "test"}:
            return self

        # 开发专用开关在非开发环境一律禁止（staging 也按真实部署对待）
        if self.dev_skeleton_loop_enabled:
            raise ValueError(
                f"{self.app_env} 环境禁止开启 DEV_SKELETON_LOOP_ENABLED（空壳 Loop 无认证）"
            )
        if self.dev_skip_school_review:
            raise ValueError(
                f"{self.app_env} 环境禁止开启 DEV_SKIP_SCHOOL_REVIEW（学校资质必须审核）"
            )
        if self.dev_skip_file_scan:
            raise ValueError(
                f"{self.app_env} 环境禁止开启 DEV_SKIP_FILE_SCAN（上传文件必须扫描）"
            )
        if not self.clamav_fail_closed:
            raise ValueError(
                f"{self.app_env} 环境必须开启 CLAMAV_FAIL_CLOSED（扫描不可用时禁止放行）"
            )

        if self.app_env == "production":
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

        # 浏览器 Cookie 必须只能走 HTTPS（本地开发/测试放行，便于 http://127.0.0.1 调试）
        if not self.auth_cookie_secure:
            raise ValueError(f"{self.app_env} 环境必须开启 AUTH_COOKIE_SECURE（Cookie 仅走 HTTPS）")
        if self.auth_refresh_token_in_body:
            raise ValueError(
                f"{self.app_env} 环境必须关闭 AUTH_REFRESH_TOKEN_IN_BODY"
                "（刷新令牌只走 HttpOnly Cookie，避免 XSS 读取）"
            )
        audit_key = (
            self.audit_hash_key.get_secret_value() if self.audit_hash_key else ""
        )
        if len(audit_key) < 32 or any(
            marker in audit_key.lower()
            for marker in ("change-me", "changeme", "placeholder", "example", "your-secret")
        ):
            raise ValueError(
                f"{self.app_env} 环境 AUDIT_HASH_KEY 必须是 ≥32 字节的随机密钥"
                "（生成：openssl rand -hex 32）"
            )
        if self.auth_cookie_samesite == "none":
            raise ValueError(f"{self.app_env} 环境禁止 AUTH_COOKIE_SAMESITE=none（跨站请求风险）")
        if reset_url.scheme != "https":
            raise ValueError(
                f"{self.app_env} 环境 PASSWORD_RESET_URL_BASE 必须使用 https://"
                "（重置令牌不得经明文链路传输）"
            )
        expected_cookie_path = f"{self.api_prefix.rstrip('/')}/auth"
        if self.auth_cookie_path.rstrip("/") != expected_cookie_path:
            raise ValueError(
                f"AUTH_COOKIE_PATH 必须为 {expected_cookie_path}（与 API_PREFIX 联动），"
                f"当前为 {self.auth_cookie_path}"
            )

        # staging/production 都按真实部署对待：密钥必须具备最低强度（TD-073 自检同样对 staging 生效）
        secret = self.jwt_secret.get_secret_value()
        lowered = secret.lower()
        placeholder_markers = ("change-me", "changeme", "placeholder", "example", "your-secret")
        if len(secret) < 32 or any(marker in lowered for marker in placeholder_markers):
            raise ValueError(
                f"{self.app_env} 环境 JWT_SECRET 必须是 ≥32 字节的随机密钥"
                "（禁止空值/占位/示例值；生成：openssl rand -hex 32）"
            )

        # 重置邮件含可改密的限时令牌，staging 也必须走加密 SMTP
        if not self.smtp_host or self.smtp_host in {"127.0.0.1", "localhost", "mailhog"}:
            raise ValueError(f"{self.app_env} 环境必须配置真实 SMTP_HOST（禁止本机 MailHog）")
        if not self.smtp_from or self.smtp_from.endswith("@campus-elf.local"):
            raise ValueError(f"{self.app_env} 环境必须配置真实 SMTP_FROM")
        if not self.smtp_username or not self.smtp_password:
            raise ValueError(f"{self.app_env} 环境必须配置 SMTP_USERNAME / SMTP_PASSWORD")
        if not self.smtp_use_tls and not self.smtp_start_tls:
            raise ValueError(f"{self.app_env} 环境 SMTP 必须启用 TLS 或 STARTTLS")
        if self.smtp_use_tls and self.smtp_start_tls:
            raise ValueError(f"{self.app_env} 环境 SMTP_USE_TLS 与 SMTP_START_TLS 只能启用一个")
        if "@" not in (self.smtp_from or ""):
            raise ValueError(f"{self.app_env} 环境 SMTP_FROM 必须是合法邮箱地址")

        if self.app_env != "production":
            return self
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
