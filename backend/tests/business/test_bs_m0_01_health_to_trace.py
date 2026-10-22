"""业务场景骨架 BS-M0-01：健康检查 → 认证 → 空壳 Loop SSE → 本地 trace → 审计链。

- 依赖独立测试栈（`scripts/test-stack.sh up`）暴露的 TEST_* 环境变量；未设置时跳过。
- 断言的是业务输出：就绪逐项状态、SSE 事件序列与 stop_reason、trace 落点与内容边界、
  审计哈希链可校验——而不是「接口返回 200」。
- 对话接口自 M1-E1 起需 `campus.chat.use`：本场景用**出厂默认的学生内置角色**登录，
  顺带验证「权限矩阵落到真实可对话的账号」这条业务链。
- Langfuse 关闭（M0 未接入 SDK）不应影响链路；真实 Loop 在 M4 替换空壳实现。
"""

import os
from collections.abc import AsyncIterator, Iterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.auth import revocation
from app.config import get_settings
from app.db.session import dispose_engine
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
from app.rbac import sync_rbac_definitions
from tests.support.auth import seed_tenant_user
from tests.support.db import (
    clean_test_data,
    make_session_factory,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_disposable_redis_dsn, assert_test_database_dsn
from tests.support.fakes import FAKE_AUDIT_SECRET

pytestmark = [pytest.mark.business, pytest.mark.integration]

CHAT_ENDPOINT = "/api/v1/campus/chat/turns"
CHAT_PERMISSION = "campus.chat.use"
SCHOOL_CODE = "SCH-BSM001"
LOGIN_ID = "S-7001"
STUDENT_PASSWORD = "Student-Pass-Word-1"
REQUIRED_ENV = (
    "TEST_DATABASE_URL",
    "TEST_APP_DATABASE_URL",
    "TEST_PLATFORM_DATABASE_URL",
    "TEST_REDIS_URL",
    "TEST_QDRANT_URL",
)


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    """把测试库迁移到 head（内置角色定义表与 seed 函数由 M1-E1 迁移提供）。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
def service_env(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """把测试栈连接串注入应用配置；缺失时跳过（默认套件不依赖外部服务）。"""

    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.skip(f"{', '.join(missing)} 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_* ")
    # 与集成用例同一套一次性资源护栏：防误连业务库/开发库。
    assert_test_database_dsn(os.environ["TEST_DATABASE_URL"])
    redis_url = assert_disposable_redis_dsn(os.environ["TEST_REDIS_URL"])
    # 应用连接必须用非超级用户（RLS 生效），且平台/租户分离
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_APP_DATABASE_URL"])
    monkeypatch.setenv("PLATFORM_DATABASE_URL", os.environ["TEST_PLATFORM_DATABASE_URL"])
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("QDRANT_URL", os.environ["TEST_QDRANT_URL"])
    monkeypatch.setenv("DEV_SKELETON_LOOP_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def student(
    service_env: None, sink: InMemoryTraceSink
) -> AsyncIterator[tuple[AsyncClient, UUID, UUID]]:
    """指向独立测试栈的应用客户端 + 已按出厂默认拿到对话权限的学生身份。"""

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
        tenant_id, user_id = await seed_tenant_user(
            admin_engine,
            school_code=SCHOOL_CODE,
            login_id=LOGIN_ID,
            password=STUDENT_PASSWORD,
            user_type="student",
        )
        # 走生产同步入口补齐该租户的内置角色与出厂默认，再绑定 student 角色：
        # 断言因此落在「矩阵配置真的能让一个学生说话」上，而不是测试自己塞权限。
        factory = make_session_factory(admin_engine)
        async with factory() as session:
            await sync_rbac_definitions(session)
            await session.commit()
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "insert into user_roles (tenant_id, user_id, role_id) "
                    "select :tenant_id, :user_id, r.id from roles r "
                    "where r.tenant_id = :tenant_id and r.code = 'student'"
                ),
                {"tenant_id": tenant_id, "user_id": user_id},
            )
    finally:
        await admin_engine.dispose()

    transport = ASGITransport(app=create_app(trace_sink=sink))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, tenant_id, user_id

    # 场景现在走真实认证：会建全局连接池。必须在本用例的事件循环关闭前释放，
    # 否则下一个业务用例复用该连接时得到「Event loop is closed」。
    await revocation.reset_client()
    await dispose_engine()
    get_settings.cache_clear()


@pytest.fixture
async def client(student: tuple[AsyncClient, UUID, UUID]) -> AsyncIterator[AsyncClient]:
    """业务场景第一步只需要未认证的客户端（就绪检查无鉴权）。"""

    yield student[0]


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
    student: tuple[AsyncClient, UUID, UUID], sink: InMemoryTraceSink
) -> None:
    """场景第二步：学生按出厂默认可对话，产出 meta→step*→done，本地 trace 必发且内容不越界。"""

    client, tenant_id, user_id = student
    message = "奖学金申请条件是什么"

    login = await client.post(
        "/api/v1/auth/login",
        json={
            "realm": "tenant",
            "school_code": SCHOOL_CODE,
            "login_id": LOGIN_ID,
            "password": STUDENT_PASSWORD,
        },
    )
    assert login.status_code == 200, login.text
    body = login.json()
    # 矩阵落地的业务结果：学生拿到内置角色，并因此具备对话权限
    assert body["roles"] == ["student"]
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert CHAT_PERMISSION in me.json()["permissions"]

    response = await client.post(
        CHAT_ENDPOINT, json={"message": message, "conversation_id": "bs-m0-01"}, headers=headers
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    text_body = response.text
    assert "event: meta" in text_body
    assert "event: done" in text_body
    assert f'"stop_reason":"{StopReason.FINAL_ANSWER.value}"' in text_body

    assert len(sink.turns) == 1
    record = sink.turns[0]
    assert record.stop_reason == StopReason.FINAL_ANSWER
    assert [span.name for span in record.spans] == ["route", "answer"]
    # 身份边界：trace 用认证主体的租户与用户，不再是占位值
    assert record.tenant_id == tenant_id
    assert record.user_id == user_id
    # 内容边界：trace 只保留元数据，不含用户原文（隐私红线）。
    assert message not in record.model_dump_json()


async def test_bs_m0_01_anonymous_turn_is_rejected_without_trace(
    student: tuple[AsyncClient, UUID, UUID], sink: InMemoryTraceSink
) -> None:
    """场景第二步补充：未登录不能再打开对话流（M0 占位身份的遗留入口已关闭）。"""

    client, _, _ = student
    response = await client.post(CHAT_ENDPOINT, json={"message": "匿名提问"})

    assert response.status_code == 401
    assert sink.turns == []


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
