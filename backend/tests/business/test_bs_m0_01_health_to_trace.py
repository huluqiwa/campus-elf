"""业务场景骨架 BS-M0-01：健康检查 → 空壳 Loop SSE → 本地 trace → 审计链。

- 依赖独立测试栈（`scripts/test-stack.sh up`）暴露的 TEST_* 环境变量；未设置时跳过。
- 断言的是业务输出：就绪逐项状态、SSE 事件序列与 stop_reason、trace 落点与内容边界、
  审计哈希链可校验——而不是「接口返回 200」。
- Langfuse 关闭（M0 未接入 SDK）不应影响链路；真实 Loop 在 M4 替换空壳实现。
"""

import os
from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.harness.audit import InMemoryAuditSink
from app.harness.contracts import (
    StopReason,
    TenantContext,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from app.harness.registry import ToolRegistry
from app.harness.tools import ToolExecutor
from app.harness.trace import InMemoryTraceSink, TraceRecorder
from app.main import create_app
from tests.support.dsn import assert_disposable_redis_dsn, assert_test_database_dsn
from tests.support.fakes import FAKE_AUDIT_SECRET

pytestmark = [pytest.mark.business, pytest.mark.integration]

CHAT_ENDPOINT = "/api/v1/campus/chat/turns"
REQUIRED_ENV = ("TEST_DATABASE_URL", "TEST_REDIS_URL", "TEST_QDRANT_URL")


@pytest.fixture
def service_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把测试栈连接串注入应用配置；缺失时跳过（默认套件不依赖外部服务）。"""

    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.skip(f"{', '.join(missing)} 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_* ")
    # 与集成用例同一套一次性资源护栏：防误连业务库/开发库。
    database_url = assert_test_database_dsn(os.environ["TEST_DATABASE_URL"])
    redis_url = assert_disposable_redis_dsn(os.environ["TEST_REDIS_URL"])
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("QDRANT_URL", os.environ["TEST_QDRANT_URL"])
    monkeypatch.setenv("DEV_SKELETON_LOOP_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(service_env: None, sink: InMemoryTraceSink) -> AsyncIterator[AsyncClient]:
    """指向独立测试栈的应用客户端。"""

    transport = ASGITransport(app=create_app(trace_sink=sink))
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def sink() -> InMemoryTraceSink:
    """注入的本地 trace sink。"""

    return InMemoryTraceSink()


async def test_bs_m0_01_health_ready_reports_real_dependencies(client: AsyncClient) -> None:
    """场景第一步：就绪检查对真实 PG/Redis/Qdrant 逐项探活通过。"""

    response = await client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert {name: item["status"] for name, item in body["checks"].items()} == {
        "postgres": "ok",
        "redis": "ok",
        "qdrant": "ok",
    }


async def test_bs_m0_01_turn_streams_events_and_records_trace(
    client: AsyncClient, sink: InMemoryTraceSink
) -> None:
    """场景第二步：一次回合产出 meta→step*→done，且本地 trace 必发、内容不越界。"""

    message = "奖学金申请条件是什么"

    response = await client.post(
        CHAT_ENDPOINT, json={"message": message, "conversation_id": "bs-m0-01"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    body = response.text
    assert "event: meta" in body
    assert "event: done" in body
    assert f'"stop_reason":"{StopReason.FINAL_ANSWER.value}"' in body

    assert len(sink.turns) == 1
    record = sink.turns[0]
    assert record.stop_reason == StopReason.FINAL_ANSWER
    assert [span.name for span in record.spans] == ["route", "answer"]
    # 内容边界：trace 只保留元数据，不含用户原文（隐私红线）。
    assert message not in record.model_dump_json()


async def test_bs_m0_01_readonly_tool_call_is_audited_and_verifiable() -> None:
    """场景第三步：只读工具调用产生可校验的 HMAC 审计链（无 LLM、无写操作）。"""

    audit_sink = InMemoryAuditSink(FAKE_AUDIT_SECRET)
    trace_sink = InMemoryTraceSink()
    registry = ToolRegistry()

    async def handler(call: ToolCall, ctx: TenantContext) -> ToolResult:
        return ToolResult(ok=True, data={"hits": []}, redacted_summary="0 hits")

    registry.register(
        ToolSpec(
            name="kb.search",
            version="bs-m0-01",
            description="业务场景骨架的只读检索桩",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"hits": {"type": "array"}},
                "required": ["hits"],
                "additionalProperties": False,
            },
            side_effect=False,
            permission="kb.read",
            data_scope="school",
            timeout_ms=1000,
        ),
        handler,
    )

    executor = ToolExecutor(
        registry,
        trace=TraceRecorder(trace_sink),
        audit=audit_sink,
    )
    ctx = TenantContext(
        tenant_id=uuid4(),
        user_id=uuid4(),
        permissions=frozenset({"kb.read"}),
        data_scopes=("school",),
    )

    result = await executor.execute(
        ToolCall(tool="kb.search", args={"query": "奖学金"}, trace_id="bs-m0-01-trace"), ctx
    )

    assert result.ok
    assert audit_sink.count == 1
    assert audit_sink.verify(expected_head=audit_sink.head_hash, expected_count=1)
    # 审计事件与 trace 通过 trace_id 关联。
    assert any(span.trace_id == "bs-m0-01-trace" for span in trace_sink.spans)
