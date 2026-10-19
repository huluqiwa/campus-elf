"""FastAPI 应用入口。

M0-A 只挂载健康检查；业务路由在后续里程碑按功能点逐步接入。
"""

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
)
# 健康检查固定在根路径，供 Compose/Nginx/探针使用；业务路由后续挂 api_prefix。
app.include_router(health_router)
