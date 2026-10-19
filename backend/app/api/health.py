"""健康检查路由。

`/health/live`：进程存活；`/health/ready`：依赖（PostgreSQL/Redis/Qdrant）逐项探活，
任一已配置依赖失败即返回 503（探针据此摘流量）。两者固定在根路径，不在 `/api/v1` 下。
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from app.config import get_settings
from app.core.readiness import CheckStatus, DependencyCheck, collect_readiness

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """存活检查响应。"""

    status: Literal["ok"]
    service: str
    version: str
    env: str


class DependencyStatus(BaseModel):
    """单个依赖的探活状态。"""

    status: CheckStatus
    detail: str = ""


class ReadyResponse(BaseModel):
    """就绪检查响应。"""

    status: Literal["ok", "degraded"]
    service: str
    version: str
    env: str
    checks: dict[str, DependencyStatus]


def _base_fields() -> dict[str, str]:
    """返回响应中的服务标识字段。"""

    settings = get_settings()
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "env": settings.app_env,
    }


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

    return HealthResponse(status="ok", **_base_fields())


@router.get("/health/ready", response_model=ReadyResponse)
async def ready(
    response: Response,
    checks: Annotated[list[DependencyCheck], Depends(collect_readiness)],
) -> ReadyResponse:
    """返回依赖就绪状态；任一已配置依赖失败时 HTTP 503。

    参数:
        response: FastAPI 响应对象（用于按结果设置状态码）。
        checks: 依赖探活结果（测试可通过依赖注入覆盖）。

    返回值:
        ReadyResponse: status=ok 或 degraded。

    异常:
        无。
    """

    degraded = any(check.status == "error" for check in checks)
    if degraded:
        response.status_code = 503

    return ReadyResponse(
        status="degraded" if degraded else "ok",
        checks={
            check.name: DependencyStatus(status=check.status, detail=check.detail)
            for check in checks
        },
        **_base_fields(),
    )
