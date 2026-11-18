"""DbAuditSink 落库集成测试（M4-AI01b 步 2；业务断言=审计表真实状态）。

settings 经 kb_env 注入测试连接串（db_session 读 get_settings）；审计断言走
admin 引擎直查（audit_logs 有 RLS，应用面写入由 sink 内部完成）。
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.harness.audit import AuditEvent
from app.harness.audit_db import DbAuditSink
from tests.support.auth import seed_tenant_user
from tests.support.db import make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_env import KbEnv, kb_env

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def env(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[KbEnv]:
    async with kb_env(monkeypatch, tmp_path) as value:
        yield value


@pytest.fixture
async def audit_identity(env: KbEnv) -> tuple[UUID, UUID]:
    """真实租户 + 真实用户（audit_logs 的 tenant/actor 外键）。"""

    engine = make_test_engine()
    try:
        tenant, user = await seed_tenant_user(
            engine,
            school_code="SCH-AUD-CHAT",
            login_id="chat-audit-admin",
            password="Chat-Audit-Pass-1",
        )
        return tenant, user
    finally:
        await engine.dispose()


async def test_flush_writes_audit_rows_for_real(
    env: KbEnv, audit_identity: tuple[UUID, UUID]
) -> None:
    """flush 后 audit_logs 出现 tool.execute 行（业务结果断言：库内真实状态）。"""

    tenant_id, user_id = audit_identity
    sink = DbAuditSink(actor_realm="tenant")
    sink.append(
        AuditEvent(
            tenant_id=tenant_id,
            actor_id=user_id,
            action="tool.execute",
            resource_type="tool",
            resource_id="kb.search",
            result="success",
            trace_id="trace-1",
            metadata={"call_id": "c1", "ok": True},
        )
    )
    sink.append(
        AuditEvent(
            tenant_id=tenant_id,
            actor_id=user_id,
            action="tool.execute",
            resource_type="tool",
            resource_id="kb.search",
            result="error",
            trace_id="trace-1",
            metadata={"call_id": "c2", "ok": False, "error_code": "CHAT_SEARCH_FAILED"},
        )
    )

    written = await sink.flush()

    assert written == 2
    engine = make_test_engine()
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "select resource_id, result from audit_logs "
                        "where tenant_id = :t and action = 'tool.execute'"
                    ),
                    {"t": str(tenant_id)},
                )
            ).all()
    finally:
        await engine.dispose()
    assert {row.resource_id for row in rows} == {"kb.search"}
    assert {row.result for row in rows} == {"success", "failure"}


async def test_flush_is_safe_when_database_write_fails(env: KbEnv) -> None:
    """单事件落库失败（无效租户 FK）：记 warning 不抛、缓冲清空不重复。"""

    sink = DbAuditSink(actor_realm="tenant")
    sink.append(
        AuditEvent(
            tenant_id=uuid4(),
            actor_id=uuid4(),
            action="tool.execute",
            resource_type="tool",
            resource_id="kb.search",
            result="success",
            metadata={},
        )
    )
    written = await sink.flush()
    assert written == 0
    assert sink.pending_count() == 0
