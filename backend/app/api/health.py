"""健康检查路由。

M0-A 只做进程存活/就绪的基础响应；依赖检查在 M0-B 接入。
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import get_settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """健康检查响应。"""

    status: Literal["ok"]
    service: str
    version: str
    env: str


@router.get("/health/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    """返回进程存活状态。

    参数:
        无。

    返回值:
        HealthResponse: status=ok。

    异常:
        无。
    """

    settings = get_settings()
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
        env=settings.app_env,
    )


@router.get("/health/ready", response_model=HealthResponse)
async def ready() -> HealthResponse:
    """返回服务就绪状态。

    M0-A 阶段尚未接入 DB/Redis/Qdrant；接入后在此检查依赖。

    参数:
        无。

    返回值:
        HealthResponse: status=ok。

    异常:
        无。
    """

    settings = get_settings()
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
        env=settings.app_env,
    )
