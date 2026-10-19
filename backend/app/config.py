"""应用配置。

所有配置通过环境变量或 .env 注入；密钥不得写入代码或日志。
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
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

    # 数据库/向量库：M0 期间可选（未配置时就绪检查标记 not_configured）；
    # production 下必须配置，见下方校验。
    database_url: str | None = None
    qdrant_url: str | None = None

    # 开发/测试专用：空壳 Loop SSE（无 LLM）；生产默认关闭，开启也仅用于联调。
    dev_skeleton_loop_enabled: bool = False

    @model_validator(mode="after")
    def _require_production_safety(self) -> "Settings":
        """生产环境必须满足的两类前提，避免带着开发形态上线。

        1. 依赖地址必须显式配置（不允许沿用开发默认/缺省）；
        2. 开发专用开关必须关闭——空壳 Loop 无认证，开启即等于开放未鉴权入口
           （"生产开关可绕过"按审查清单属阻塞项）。
        """

        if self.app_env != "production":
            return self

        missing = [
            name
            for name, value in (
                ("DATABASE_URL", self.database_url),
                ("QDRANT_URL", self.qdrant_url),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"production 环境缺少必需配置: {', '.join(missing)}")
        if self.dev_skeleton_loop_enabled:
            raise ValueError(
                "production 环境禁止开启 DEV_SKELETON_LOOP_ENABLED（空壳 Loop 无认证）"
            )
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
