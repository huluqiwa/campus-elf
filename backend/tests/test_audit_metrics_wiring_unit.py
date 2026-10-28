"""审计写入失败的指标埋点单元：flush 失败与提交失败都必须让运维看得见。

`append_audit` 只覆盖 `session.flush()`；best-effort 拒绝审计的提交发生在 `db_session`
上下文退出时，两层失败都要计数，且**一次尝试只记一次**（flush 失败已由 writer 计数，
调用方只在 flush 成功后补记提交失败）。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.audit.writer import append_audit
from app.auth.principal import Principal, _audit_denied
from app.personnel.service import _audit_denied as personnel_audit_denied
from tests.support.metrics import counter_value


class _FlushFailingSession:
    """只有 `add` 与 `flush` 的会话替身：flush 必失败。"""

    def add(self, event: object) -> None:
        return None

    async def flush(self) -> None:
        raise RuntimeError("flush failed")


@asynccontextmanager
async def _commit_failing_session(**_: Any) -> AsyncIterator[object]:
    """进入成功、退出（提交）必失败的会话替身。"""

    yield object()
    raise RuntimeError("commit failed")


@asynccontextmanager
async def _flush_failing_session(**_: Any) -> AsyncIterator[object]:
    """进入成功、flush 必失败的会话替身；交给真实 `append_audit` 去计数。"""

    yield cast(AsyncSession, _FlushFailingSession())


def _tenant_principal() -> Principal:
    now = datetime.now(UTC)
    return Principal(
        user_id=uuid4(),
        realm="tenant",
        tenant_id=uuid4(),
        jti=uuid4(),
        session_id=uuid4(),
        expires_at=now + timedelta(minutes=15),
        must_change_password=False,
        roles=frozenset({"student"}),
        permissions=frozenset(),
    )


def _request() -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/api/v1/school/personnel",
        "raw_path": b"/api/v1/school/personnel",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "state": {},
    }
    request = Request(scope)
    request.state.request_id = "req-metrics-wiring"
    return request


@pytest.mark.asyncio
async def test_append_audit_flush_failure_increments_counter() -> None:
    """真实 append_audit：flush 抛错时计数并原样抛出（由调用方决定降级）。"""

    labels = {"action": "test.metrics.flush"}
    before = counter_value("campus_audit_write_failures_total", labels)

    with pytest.raises(RuntimeError, match="flush failed"):
        await append_audit(
            cast(AsyncSession, _FlushFailingSession()),
            action="test.metrics.flush",
            resource_type="test",
            actor_realm="system",
        )

    assert counter_value("campus_audit_write_failures_total", labels) == before + 1


@pytest.mark.asyncio
async def test_permission_denial_commit_failure_increments_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """权限拒绝审计：flush 成功、提交失败时补记一次，拒绝本身仍是 403（不抛错）。"""

    async def _flush_ok(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr("app.auth.principal.db_session", _commit_failing_session)
    monkeypatch.setattr("app.audit.writer.append_audit", _flush_ok)

    labels = {"action": "auth.permission.denied"}
    before = counter_value("campus_audit_write_failures_total", labels)

    await _audit_denied(_tenant_principal(), "school.personnel.view", _request())

    assert counter_value("campus_audit_write_failures_total", labels) == before + 1


@pytest.mark.asyncio
async def test_data_scope_denial_commit_failure_increments_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数据范围拒绝审计：提交失败同样补记一次，404 结果不受影响。"""

    async def _flush_ok(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr("app.personnel.service.db_session", _commit_failing_session)
    monkeypatch.setattr("app.personnel.service.append_audit", _flush_ok)

    labels = {"action": "data_scope.denied"}
    before = counter_value("campus_audit_write_failures_total", labels)

    await personnel_audit_denied(_tenant_principal(), uuid4(), None)

    assert counter_value("campus_audit_write_failures_total", labels) == before + 1


@pytest.mark.asyncio
async def test_permission_denial_flush_failure_counts_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """flush 失败由 writer 记一次；调用方 `flushed` 仍为 False，不得补记第二次。"""

    monkeypatch.setattr("app.auth.principal.db_session", _flush_failing_session)

    labels = {"action": "auth.permission.denied"}
    before = counter_value("campus_audit_write_failures_total", labels)

    await _audit_denied(_tenant_principal(), "school.personnel.view", _request())

    assert counter_value("campus_audit_write_failures_total", labels) == before + 1


@pytest.mark.asyncio
async def test_data_scope_denial_flush_failure_counts_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """范围拒绝同为：flush 失败只记一次，拒绝本身不抛错。"""

    monkeypatch.setattr("app.personnel.service.db_session", _flush_failing_session)

    labels = {"action": "data_scope.denied"}
    before = counter_value("campus_audit_write_failures_total", labels)

    await personnel_audit_denied(_tenant_principal(), uuid4(), None)

    assert counter_value("campus_audit_write_failures_total", labels) == before + 1
