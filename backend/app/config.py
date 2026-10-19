"""应用配置。

所有配置通过环境变量或 .env 注入；密钥不得写入代码或日志。
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

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
    )

    app_env: Literal["development", "test", "staging", "production"] = "development"
    app_name: str = "CampusCompanionElf"
    app_version: str = "0.1.0"
    api_prefix: str = "/api/v1"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    docs_enabled: bool = True


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
