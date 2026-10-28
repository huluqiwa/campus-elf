"""Prometheus 指标暴露端点（运维用；`METRICS_ENABLED=false` 时返回 404）。

只输出 `app.core.metrics` 私有注册表里的 `campus_*` 计数器（方法/路由模板/状态码/原因码），
不含默认采集器、租户、用户与登录名。
生产开启后必须用网络策略限制到内网抓取端（`deploy/observability/README.md`）。
"""

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import get_settings
from app.core.metrics import REGISTRY

router = APIRouter(tags=["ops"])


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """输出 Prometheus 文本格式指标。

    参数:
        无。

    返回值:
        Response: `text/plain; version=0.0.4`（`CONTENT_TYPE_LATEST`）。

    异常:
        HTTPException: 未开启 `METRICS_ENABLED` 时 404（等同于该端点不存在）。
    """

    if not get_settings().metrics_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
