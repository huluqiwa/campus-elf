"""Prometheus 指标（质量与安全运维 P0 的最小集）。

设计约束：
- 标签只放**有界、低敏**的维度：HTTP 方法（固定动词集合，异常值归 `OTHER`）、路由模板、
  状态码，以及只来自代码字面量的原因码（`reason`/`action`/`realm` 不接受外部输入）；
  禁止 tenant_id、user_id、角色名、登录名——高基数会拖垮抓取，PII 会违反审计脱敏红线。
- 私有 `CollectorRegistry`：`/metrics` 只输出本模块定义的 `campus_*` 指标，不夹带
  Python/进程默认采集器，也不受第三方库注册到全局默认注册表的指标影响。
- 指标只做进程内累加，不落库、不参与业务判定；任何埋点失败都不得影响请求结果。
- 多 worker 部署时每个进程各自计数，需要 `PROMETHEUS_MULTIPROC_DIR` 才能汇总；
  当前默认单 worker，多 worker 的落地随 TD-214（M6 生产观测）一起做。
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from prometheus_client import CollectorRegistry, Counter
from starlette.types import Message, Receive, Scope, Send

REGISTRY = CollectorRegistry()

HTTP_REQUESTS = Counter(
    "campus_http_requests_total",
    "HTTP 请求总数（方法 / 路由模板 / 状态码；响应头发出前被取消记 aborted）",
    ["method", "route", "status"],
    registry=REGISTRY,
)
HTTP_STREAM_ERRORS = Counter(
    "campus_http_stream_errors_total",
    "响应头发出后请求以异常或取消结束的次数（客户端断开 / 流生成器 / 后台任务）",
    ["method", "route"],
    registry=REGISTRY,
)
AUDIT_WRITE_FAILURES = Counter(
    "campus_audit_write_failures_total",
    "审计写入失败次数（按动作；flush 与提交失败各记一次成功尝试的失败）",
    ["action"],
    registry=REGISTRY,
)
PERMISSION_DENIALS = Counter(
    "campus_permission_denials_total",
    "权限拒绝次数（realm_mismatch / missing_permission）",
    ["realm", "reason"],
    registry=REGISTRY,
)
DATA_SCOPE_DENIALS = Counter(
    "campus_data_scope_denials_total",
    "数据范围拒绝次数",
    ["reason"],
    registry=REGISTRY,
)
AUTH_LOGIN_FAILURES = Counter(
    "campus_auth_login_failures_total",
    "登录失败次数（按 realm 与原因）",
    ["realm", "reason"],
    registry=REGISTRY,
)
AUTH_LOCKOUTS = Counter(
    "campus_auth_account_lockouts_total",
    "账号因连续失败被锁定的次数",
    ["realm"],
    registry=REGISTRY,
)

# ASGI 规范只有这些标准方法；自定义/畸形动词归 OTHER，防止标签爆炸。
_KNOWN_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_METRICS_PATH = "/metrics"


def _method_label(method: str) -> str:
    """HTTP 方法归一到固定集合。"""

    return method if method in _KNOWN_METHODS else "OTHER"


def record_http_request(method: str, route: str, status: int) -> None:
    """登记一次 HTTP 请求（路由用模板而不是真实路径，避免 id 造成高基数）。"""

    HTTP_REQUESTS.labels(method=_method_label(method), route=route, status=str(status)).inc()


def record_http_abort(method: str, route: str) -> None:
    """登记一次响应头未发出就被取消的请求（客户端断开）。"""

    HTTP_REQUESTS.labels(method=_method_label(method), route=route, status="aborted").inc()


def record_http_stream_error(method: str, route: str) -> None:
    """登记一次响应头发出后的流中断（状态码已计过，这里只补中断事实）。"""

    HTTP_STREAM_ERRORS.labels(method=_method_label(method), route=route).inc()


def record_audit_write_failure(action: str) -> None:
    """登记一次审计写入失败。"""

    AUDIT_WRITE_FAILURES.labels(action=action).inc()


def record_permission_denial(realm: str, reason: str) -> None:
    """登记一次权限拒绝（realm 为 platform/tenant）。"""

    PERMISSION_DENIALS.labels(realm=realm, reason=reason).inc()


def record_data_scope_denial(reason: str = "out_of_data_scope") -> None:
    """登记一次数据范围拒绝。"""

    DATA_SCOPE_DENIALS.labels(reason=reason).inc()


def record_login_failure(realm: str, reason: str, *, lock_applied: bool = False) -> None:
    """登记一次登录失败；本次失败触发锁定时另记一条锁定计数。"""

    AUTH_LOGIN_FAILURES.labels(realm=realm, reason=reason).inc()
    if lock_applied:
        AUTH_LOCKOUTS.labels(realm=realm).inc()


def _route_template(scope: Scope) -> str:
    """取路由模板；未命中路由（404）统一归到 `unmatched`。"""

    route: Any = scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


def _is_metrics_path(scope: Scope) -> bool:
    """判断请求是否指向 `/metrics` 自身（去掉 root_path 与尾斜杠后精确比较）。"""

    path = str(scope.get("path", ""))
    root_path = str(scope.get("root_path", "")).rstrip("/")
    if root_path and path.startswith(root_path):
        path = path[len(root_path) :]
    return path.rstrip("/") == _METRICS_PATH


class MetricsMiddleware:
    """纯 ASGI 中间件：按路由模板与状态码统计请求。

    放在 RequestContext 之外（最后 `add_middleware`），保证 413/500/未匹配路由也能计数；
    `/metrics` 自身不计数，避免抓取频率污染业务曲线。

    在响应头（`http.response.start`）发出时计数：SSE 长连接不必等到流结束。
    未发头的异常记一次 500；未发头被取消记 `status="aborted"`；
    已发头后的异常/取消另记 `campus_http_stream_errors_total`，不重复计入状态码。
    客户端断开是否以取消形式冒泡取决于 ASGI 服务器与响应实现（`StreamingResponse`
    可能在内层任务组消化断连），本中间件只统计实际向上传播的取消/异常。
    """

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _is_metrics_path(scope):
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", ""))
        recorded = False

        async def _send(message: Message) -> None:
            nonlocal recorded
            if message["type"] == "http.response.start" and not recorded:
                record_http_request(method, _route_template(scope), int(message["status"]))
                recorded = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except asyncio.CancelledError:
            # 客户端断开通常表现为取消（BaseException，不会被下面的 except 捕获）。
            if recorded:
                record_http_stream_error(method, _route_template(scope))
            else:
                record_http_abort(method, _route_template(scope))
            raise
        except Exception:
            # 未处理异常由外层 ServerErrorMiddleware 变成 500；已发响应头的（如流中断）
            # 不重复计数状态码，只补一条流中断。
            if recorded:
                record_http_stream_error(method, _route_template(scope))
            else:
                record_http_request(method, _route_template(scope), 500)
            raise
