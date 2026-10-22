"""M1-E1 单元：权限拒绝审计「写失败」时的行为与日志卫生。

为什么要单独测：拒绝审计是 best-effort，它失败时**不能**把 403 变成 500，也不能静默吞掉
（安全事件失去可观测性）。而"留下痕迹"最容易踩的坑是把 traceback 打开 —— SQLAlchemy 的
异常字符串自带 `[SQL: ...] [parameters: ...]`，`exc_info=True` 会把审计参数（请求路径、
主体 id）原样写进日志，违反红线「日志不得包含明文敏感信息」（第三轮审查 Minor 4）。
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.auth.principal import Principal, _audit_denied
from app.auth.tokens import Realm

_LEAKED_SQL = "[SQL: insert into audit_logs ...] [parameters: ('/api/chat?token=abc',)]"


class _FakeAuditDriverError(Exception):
    """模拟 SQLAlchemy DBAPIError：消息里带完整 SQL 与参数。"""


def _principal(realm: Realm) -> Principal:
    now = datetime.now(UTC)
    return Principal(
        user_id=uuid4(),
        realm=realm,
        tenant_id=uuid4() if realm == "tenant" else None,
        jti=uuid4(),
        session_id=uuid4(),
        expires_at=now + timedelta(minutes=15),
        must_change_password=False,
        roles=frozenset({"student"}),
        permissions=frozenset(),
    )


def _request(path: str = "/api/campus/chat/turns") -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "state": {},
    }
    request = Request(scope)
    request.state.request_id = "req-unit-1"
    return request


def _fake_db_session(
    sink: list[dict[str, Any]],
) -> Callable[..., AbstractAsyncContextManager[object]]:
    """记录 `db_session()` 收到的连接参数。

    两个 realm 用**不同连接**（租户侧带 tenant_id、平台侧 platform=True），审计必须写进对应那条；
    不接住这组参数就等于没测"审计落在哪个库"（第四轮审查 Minor 2）。
    """

    @asynccontextmanager
    async def _session(**kwargs: Any) -> AsyncIterator[object]:
        sink.append(kwargs)
        yield object()

    return _session


async def test_denial_audit_failure_keeps_denial_and_scrubs_sql(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """审计写失败：拒绝照常返回（不抛错），日志只留异常类型，不留 SQL 与参数。"""

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise _FakeAuditDriverError(_LEAKED_SQL)

    sessions: list[dict[str, Any]] = []
    monkeypatch.setattr("app.auth.principal.db_session", _fake_db_session(sessions))
    monkeypatch.setattr("app.audit.writer.append_audit", _boom)

    tenant_principal = _principal("tenant")
    platform_principal = _principal("platform")
    with caplog.at_level(logging.WARNING, logger="app.auth.principal"):
        await _audit_denied(tenant_principal, "campus.chat.use", _request())
        await _audit_denied(
            platform_principal, "platform.tenant.view", _request("/api/platform/tenants")
        )

    messages = [record.getMessage() for record in caplog.records]
    failures = [message for message in messages if "权限拒绝审计写入失败" in message]
    assert len(failures) == 2, "两个 realm 的拒绝审计失败都要留下告警（租户侧与平台侧同一口径）"
    assert sessions == [
        {"tenant_id": tenant_principal.tenant_id},
        {"platform": True},
    ], f"拒绝审计写到了错误的连接：{sessions}"
    joined = " | ".join(messages)
    assert "[parameters" not in joined, "审计失败的日志带出了 SQL 参数"
    assert "token=abc" not in joined, "日志出现了令牌样文本"
    assert "_FakeAuditDriverError" in joined, "日志必须保留异常类型，否则运维无法定位故障类别"


async def test_denial_audit_success_path_writes_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """正向：审计确实被调用，且落对连接、metadata 只带 path 与 reason（realm 漂移与缺权限可区分）。"""

    written: list[dict[str, Any]] = []

    async def _capture(session: Any, **kwargs: Any) -> None:
        written.append(kwargs)

    sessions: list[dict[str, Any]] = []
    monkeypatch.setattr("app.auth.principal.db_session", _fake_db_session(sessions))
    monkeypatch.setattr("app.audit.writer.append_audit", _capture)

    principal = _principal("tenant")
    await _audit_denied(principal, "campus.chat.use", _request(), reason="realm_mismatch")

    assert len(written) == 1
    entry = written[0]
    assert entry["action"] == "auth.permission.denied"
    assert entry["resource_id"] == "campus.chat.use"
    assert entry["result"] == "failure"
    assert entry["actor_realm"] == "tenant"
    assert entry["actor_id"] == principal.user_id
    assert entry["tenant_id"] == principal.tenant_id
    assert entry["metadata"] == {"path": "/api/campus/chat/turns", "reason": "realm_mismatch"}
    assert sessions == [{"tenant_id": principal.tenant_id}]
