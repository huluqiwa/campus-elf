"""P0 观测单元：计数器自增、/metrics 开关与请求中间件。

不连数据库：中间件用最小 ASGI 替身验证"计数发生在哪一层"，应用级用例只打健康探针。
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.types import Message, Receive, Scope, Send

from app.api import metrics as metrics_api
from app.config import Settings, get_settings
from app.core import metrics as metrics_core
from app.core.metrics import REGISTRY
from app.main import app


def _value(name: str, labels: dict[str, str]) -> float:
    """读取计数器当前值；从未自增过的序列按 0 处理。"""

    sample = REGISTRY.get_sample_value(name, labels)
    return 0.0 if sample is None else sample


class _FixedSettings:
    """只带 metrics_enabled 的配置替身（避免改全局 lru_cache 配置）。"""

    def __init__(self, enabled: bool) -> None:
        self.metrics_enabled = enabled


class _Route:
    """路由模板替身（中间件只读 `.path`）。"""

    def __init__(self, path: str) -> None:
        self.path = path


def _http_scope(
    path: str,
    *,
    route_path: str | None = None,
    method: str = "GET",
    root_path: str = "",
) -> Scope:
    scope: dict[str, Any] = {
        "type": "http",
        "path": path,
        "method": method,
        "root_path": root_path,
    }
    if route_path is not None:
        scope["route"] = _Route(route_path)
    return cast(Scope, scope)


async def _receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _noop_send(message: Message) -> None:
    """空 send 替身。"""


def _lifespan_scope() -> Scope:
    return cast(Scope, {"type": "lifespan"})


def _status_app(status: int) -> Callable[[Scope, Receive, Send], Awaitable[None]]:
    async def _app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return _app


async def _run_asgi(
    asgi_app: Callable[[Scope, Receive, Send], Awaitable[None]], scope: Scope
) -> list[Message]:
    sent: list[Message] = []

    async def _send(message: Message) -> None:
        sent.append(message)

    await asgi_app(scope, _receive, _send)
    return sent


def test_settings_default_keeps_metrics_off() -> None:
    """生产默认不暴露 /metrics：默认值必须在代码里是 False（不依赖部署环境变量）。"""

    assert Settings.model_fields["metrics_enabled"].default is False


@pytest.mark.asyncio
async def test_metrics_endpoint_is_not_found_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认关闭语义：未开启 METRICS_ENABLED 时该端点等价于不存在。"""

    monkeypatch.setattr("app.api.metrics.get_settings", lambda: _FixedSettings(False))

    with pytest.raises(HTTPException) as excinfo:
        await metrics_api.metrics()

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_metrics_endpoint_serves_the_registry_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开启后返回 Prometheus 文本格式，且包含已知计数器名。"""

    monkeypatch.setattr("app.api.metrics.get_settings", lambda: _FixedSettings(True))
    metrics_core.record_audit_write_failure("test.metrics.enabled")

    response = await metrics_api.metrics()

    assert response.status_code == 200
    assert response.media_type is not None
    assert response.media_type.startswith("text/plain")
    body = bytes(response.body)
    assert b"campus_audit_write_failures_total" in body
    assert b'test.metrics.enabled' in body


@pytest.mark.asyncio
async def test_metrics_output_contains_only_campus_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """私有注册表只暴露 campus_*，且每个指标族的标签名都在白名单内（无 tenant/user）。"""

    monkeypatch.setattr("app.api.metrics.get_settings", lambda: _FixedSettings(True))
    metrics_core.record_http_request("GET", "/api/v1/test", 200)

    response = await metrics_api.metrics()
    samples = [
        line
        for line in bytes(response.body).decode().splitlines()
        if line and not line.startswith("#")
    ]
    families = {line.split("{", 1)[0].split(" ", 1)[0] for line in samples}

    assert families
    assert all(name.startswith("campus_") for name in families)

    allowed_labels: dict[str, set[str]] = {
        "campus_http_requests_total": {"method", "route", "status"},
        "campus_http_stream_errors_total": {"method", "route"},
        "campus_audit_write_failures_total": {"action"},
        "campus_permission_denials_total": {"realm", "reason"},
        "campus_data_scope_denials_total": {"reason"},
        "campus_auth_login_failures_total": {"realm", "reason"},
        "campus_auth_account_lockouts_total": {"realm"},
        "campus_guard_hits_total": {"rule", "severity", "direction"},
    }
    for line in samples:
        name, _, rest = line.partition("{")
        # Counter 的 `_created` 样本名由 `_total` 去掉后加 `_created` 得到。
        base = name.removesuffix("_created") + "_total" if name.endswith("_created") else name
        assert base in allowed_labels, name
        raw_labels = rest.split("}", 1)[0]
        keys = {pair.split("=", 1)[0] for pair in raw_labels.split(",") if pair}
        assert keys <= allowed_labels[base], (name, keys)


def test_record_helpers_increment_their_counters() -> None:
    """每个埋点入口都能独立自增；锁定同时推高失败与锁定两条计数。"""

    http_labels = {"method": "POST", "route": "/api/v1/test", "status": "201"}
    before_http = _value("campus_http_requests_total", http_labels)
    metrics_core.record_http_request("POST", "/api/v1/test", 201)
    assert _value("campus_http_requests_total", http_labels) == before_http + 1

    audit_labels = {"action": "test.audit.action"}
    before_audit = _value("campus_audit_write_failures_total", audit_labels)
    metrics_core.record_audit_write_failure("test.audit.action")
    assert _value("campus_audit_write_failures_total", audit_labels) == before_audit + 1

    denial_labels = {"realm": "tenant", "reason": "missing_permission"}
    before_denial = _value("campus_permission_denials_total", denial_labels)
    metrics_core.record_permission_denial("tenant", "missing_permission")
    assert _value("campus_permission_denials_total", denial_labels) == before_denial + 1

    scope_labels = {"reason": "out_of_data_scope"}
    before_scope = _value("campus_data_scope_denials_total", scope_labels)
    metrics_core.record_data_scope_denial()
    assert _value("campus_data_scope_denials_total", scope_labels) == before_scope + 1

    login_labels = {"realm": "tenant", "reason": "invalid_password"}
    lockout_labels = {"realm": "tenant"}
    before_login = _value("campus_auth_login_failures_total", login_labels)
    before_lockout = _value("campus_auth_account_lockouts_total", lockout_labels)
    metrics_core.record_login_failure("tenant", "invalid_password", lock_applied=True)
    assert _value("campus_auth_login_failures_total", login_labels) == before_login + 1
    assert _value("campus_auth_account_lockouts_total", lockout_labels) == before_lockout + 1


@pytest.mark.asyncio
async def test_middleware_counts_by_route_template_and_status() -> None:
    """按路由模板而不是真实路径计数，并保留状态码维度。"""

    labels = {"method": "GET", "route": "/api/v1/items/{item_id}", "status": "404"}
    before = _value("campus_http_requests_total", labels)

    sent = await _run_asgi(
        metrics_core.MetricsMiddleware(_status_app(404)),
        _http_scope("/api/v1/items/123", route_path="/api/v1/items/{item_id}"),
    )

    assert len(sent) == 2
    assert _value("campus_http_requests_total", labels) == before + 1


@pytest.mark.asyncio
async def test_middleware_buckets_unmatched_paths_together() -> None:
    """未命中路由（404 兜底）统一归入 unmatched，避免扫描路径制造高基数。"""

    labels = {"method": "GET", "route": "unmatched", "status": "404"}
    before = _value("campus_http_requests_total", labels)

    await _run_asgi(metrics_core.MetricsMiddleware(_status_app(404)), _http_scope("/random-1"))
    await _run_asgi(metrics_core.MetricsMiddleware(_status_app(404)), _http_scope("/random-2"))

    assert _value("campus_http_requests_total", labels) == before + 2


@pytest.mark.asyncio
async def test_middleware_normalizes_unknown_methods_to_other() -> None:
    """畸形/自定义动词统一归 OTHER，不因外部输入制造新标签。"""

    normalized_labels = {"method": "OTHER", "route": "/health/live", "status": "200"}
    raw_labels = {"method": "TRACE", "route": "/health/live", "status": "200"}
    before = _value("campus_http_requests_total", normalized_labels)

    await _run_asgi(
        metrics_core.MetricsMiddleware(_status_app(200)),
        _http_scope("/health/live", route_path="/health/live", method="TRACE"),
    )

    assert _value("campus_http_requests_total", normalized_labels) == before + 1
    assert _value("campus_http_requests_total", raw_labels) == 0


@pytest.mark.asyncio
async def test_middleware_counts_unhandled_exception_as_one_500() -> None:
    """无响应头就抛异常时记一次 500 并原样抛出（交给 ServerErrorMiddleware）。"""

    labels = {"method": "GET", "route": "/api/v1/boom", "status": "500"}
    before = _value("campus_http_requests_total", labels)

    async def _boom(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await _run_asgi(
            metrics_core.MetricsMiddleware(_boom),
            _http_scope("/api/v1/boom", route_path="/api/v1/boom"),
        )

    assert _value("campus_http_requests_total", labels) == before + 1


@pytest.mark.asyncio
async def test_middleware_counts_client_cancel_before_headers_as_aborted() -> None:
    """响应头发出前被取消（客户端断开）记 aborted，不冒充 500。"""

    labels = {"method": "GET", "route": "/api/v1/slow", "status": "aborted"}
    before = _value("campus_http_requests_total", labels)

    async def _cancel(scope: Scope, receive: Receive, send: Send) -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _run_asgi(
            metrics_core.MetricsMiddleware(_cancel),
            _http_scope("/api/v1/slow", route_path="/api/v1/slow"),
        )

    assert _value("campus_http_requests_total", labels) == before + 1


@pytest.mark.asyncio
async def test_middleware_counts_stream_cancel_after_headers_as_stream_error() -> None:
    """已发头后客户端断开：状态码只记一次，另补一条流中断。"""

    ok_labels = {"method": "GET", "route": "/api/v1/sse", "status": "200"}
    stream_labels = {"method": "GET", "route": "/api/v1/sse"}
    before_ok = _value("campus_http_requests_total", ok_labels)
    before_stream = _value("campus_http_stream_errors_total", stream_labels)

    async def _stream_then_cancel(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _run_asgi(
            metrics_core.MetricsMiddleware(_stream_then_cancel),
            _http_scope("/api/v1/sse", route_path="/api/v1/sse"),
        )

    assert _value("campus_http_requests_total", ok_labels) == before_ok + 1
    assert _value("campus_http_stream_errors_total", stream_labels) == before_stream + 1


@pytest.mark.asyncio
async def test_middleware_counts_a_stream_that_breaks_after_headers_only_once() -> None:
    """已发出响应头后再抛异常（SSE 生成器故障）：按已发状态计一次，另记流中断。"""

    ok_labels = {"method": "GET", "route": "/api/v1/stream", "status": "200"}
    error_labels = {"method": "GET", "route": "/api/v1/stream", "status": "500"}
    stream_labels = {"method": "GET", "route": "/api/v1/stream"}
    before_ok = _value("campus_http_requests_total", ok_labels)
    before_error = _value("campus_http_requests_total", error_labels)
    before_stream = _value("campus_http_stream_errors_total", stream_labels)

    async def _stream_then_break(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("client went away")

    with pytest.raises(RuntimeError):
        await _run_asgi(
            metrics_core.MetricsMiddleware(_stream_then_break),
            _http_scope("/api/v1/stream", route_path="/api/v1/stream"),
        )

    assert _value("campus_http_requests_total", ok_labels) == before_ok + 1
    assert _value("campus_http_requests_total", error_labels) == before_error
    assert _value("campus_http_stream_errors_total", stream_labels) == before_stream + 1


@pytest.mark.asyncio
async def test_middleware_skips_the_metrics_scrape_itself() -> None:
    """抓取自身直接透传不计数（含尾斜杠与 root_path 挂载）。"""

    labels = {"method": "GET", "route": "/metrics", "status": "200"}
    before = _value("campus_http_requests_total", labels)
    called: list[str] = []

    async def _inner(scope: Scope, receive: Receive, send: Send) -> None:
        called.append(str(scope["path"]))
        await _status_app(200)(scope, receive, send)

    middleware = metrics_core.MetricsMiddleware(_inner)
    await _run_asgi(middleware, _http_scope("/metrics", route_path="/metrics"))
    await _run_asgi(middleware, _http_scope("/metrics/", route_path="/metrics"))
    await _run_asgi(
        middleware,
        _http_scope("/api/metrics", route_path="/metrics", root_path="/api"),
    )

    assert called == ["/metrics", "/metrics/", "/api/metrics"]
    assert _value("campus_http_requests_total", labels) == before


@pytest.mark.asyncio
async def test_middleware_passes_through_non_http_scopes() -> None:
    """lifespan/websocket 等非 HTTP 作用域直接透传，不做计数。"""

    received: list[str] = []

    async def _inner(scope: Scope, receive: Receive, send: Send) -> None:
        received.append(str(scope["type"]))

    await metrics_core.MetricsMiddleware(_inner)(_lifespan_scope(), _receive, _noop_send)

    assert received == ["lifespan"]


@pytest.mark.asyncio
async def test_live_probe_is_counted_end_to_end() -> None:
    """真实应用里中间件已接线：健康探针请求会计入对应路由模板。"""

    assert get_settings().metrics_enabled is True
    labels = {"method": "GET", "route": "/health/live", "status": "200"}
    before = _value("campus_http_requests_total", labels)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")
        assert response.status_code == 200
        scrape = await client.get("/metrics")

    assert _value("campus_http_requests_total", labels) == before + 1
    assert scrape.status_code == 200
    assert 'route="/health/live"' in scrape.text
