"""M4-AI05 集成测试：C 端提交/幂等/删除匿名化 + B 端列表/详情/处理/答复（真实 PG）。

断言业务结果而非 HTTP 200：
- 提交：consent_at 落库、三 id 归属校验（非本人 404）、幂等重放 replayed=true、
  快照 AES-GCM 密文落库（非明文）、comment PII 脱敏；
- B 端：权限边界（无码 403）、详情受控解密（明文快照）、处理状态机幂等 + 审计、
  答复落表 + 审计、游标分页；
- 匿名化：自助 DELETE 置空身份/清空快照与评论（库层不变量）、幂等、
  匿名化后 B 端详情无快照、答复 409。
"""

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.auth import revocation
from app.config import get_settings
from app.db.session import dispose_engine
from app.main import create_app
from tests.support.auth import login_tenant, seed_tenant_user
from tests.support.db import (
    clean_test_data,
    database_url,
    make_session_factory,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.integration]

SCHOOL_CODE = "SCH-AI05"
PASSWORD = "Feedback-Passw0rd-1"
STUDENT_MAIN = "S-7101"
STUDENT_OTHER = "S-7102"
HANDLER = "SCH-7190"

CREATE_URL = "/api/v1/campus/feedback"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def fb_env(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    """测试库造数（学生×2 + 校方处理者）+ 应用客户端。"""

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
        await seed_tenant_user(
            admin_engine, school_code=SCHOOL_CODE, login_id=STUDENT_MAIN,
            password=PASSWORD, user_type="student",
            permissions=("campus.chat.use", "campus.feedback.create"),
        )
        await seed_tenant_user(
            admin_engine, school_code=SCHOOL_CODE, login_id=STUDENT_OTHER,
            password=PASSWORD, user_type="student",
            permissions=("campus.chat.use", "campus.feedback.create"),
        )
        await seed_tenant_user(
            admin_engine, school_code=SCHOOL_CODE, login_id=HANDLER,
            password=PASSWORD, user_type="teacher",
            permissions=("school.feedback.view", "school.feedback.update"),
        )
    finally:
        await admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    get_settings.cache_clear()
    await revocation.reset_client()

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    await revocation.reset_client()
    await dispose_engine()
    get_settings.cache_clear()


async def _identity(login_id: str) -> tuple[UUID, UUID]:
    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "select u.tenant_id, u.id from users u join tenants t on t.id = u.tenant_id "
                        "where t.school_code = :code and u.login_id = :login_id"
                    ),
                    {"code": SCHOOL_CODE, "login_id": login_id},
                )
            ).mappings().one()
        return UUID(str(row["tenant_id"])), UUID(str(row["id"]))
    finally:
        await engine.dispose()


async def _insert_ai_round(
    tenant_id: UUID, user_id: UUID,
) -> tuple[UUID, UUID, str]:
    """造一轮可反馈的 AI 回合（Conversation + ChatTrace + user/assistant 消息）。"""

    from app.db.models.ai import ChatTrace, Conversation, ConversationMessage
    from app.db.repository import ScopeBypass, TenantScopedRepository
    from app.db.session import db_session

    trace_id = uuid4().hex
    conversation_id = uuid4()
    turn_id = uuid4()
    async with db_session(tenant_id=tenant_id) as session:
        TenantScopedRepository(
            session, Conversation, tenant_id,
            scope=ScopeBypass(reason="测试造数：AI 回合反馈用例"),
        )
        session.add(Conversation(
            id=conversation_id, tenant_id=tenant_id, user_id=user_id, status="active",
        ))
        session.add(ChatTrace(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id,
            turn_id=turn_id, route_path="fast",
        ))
        TenantScopedRepository(
            session, ConversationMessage, tenant_id,
            scope=ScopeBypass(reason="测试造数：AI 回合反馈用例"),
        )
        session.add(ConversationMessage(
            tenant_id=tenant_id, conversation_id=conversation_id, trace_id=trace_id,
            role="user", content="差旅报销怎么办理？", status="completed",
        ))
        session.add(ConversationMessage(
            tenant_id=tenant_id, conversation_id=conversation_id, trace_id=trace_id,
            role="assistant", content="差旅报销需要发票和审批单 [1]。",
            citations_json=[{"index": 1, "chunk_id": "c1", "document": "财务报销手册",
                             "page": 3, "section": "第一章"}],
            status="completed",
        ))
        await session.commit()
    return conversation_id, turn_id, trace_id


async def _token(client: AsyncClient, login_id: str) -> dict[str, str]:
    return await login_tenant(
        client, school_code=SCHOOL_CODE, login_id=login_id, password=PASSWORD,
    )


async def _audit_count(tenant_id: UUID, action: str) -> int:
    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "select count(*) as n from audit_logs where tenant_id = :t and action = :a"
                    ),
                    {"t": str(tenant_id), "a": action},
                )
            ).mappings().one()
        return int(row["n"])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_submit_feedback_idempotent_and_encrypted(fb_env: AsyncClient) -> None:
    """A1/A2：提交落库 + consent_at + 幂等重放 + 快照密文 + comment 脱敏。"""

    tenant_id, user_id = await _identity(STUDENT_MAIN)
    conversation_id, turn_id, trace_id = await _insert_ai_round(tenant_id, user_id)
    headers = await _token(fb_env, STUDENT_MAIN)

    payload = {
        "conversation_id": str(conversation_id),
        "turn_id": str(turn_id),
        "trace_id": trace_id,
        "rating": "unhelpful",
        "reason_tags": ["inaccurate", "irrelevant"],
        "comment": "答非所问，我的证件号 11010119900307777X 别存。",
    }
    resp = await fb_env.post(CREATE_URL, json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["replayed"] is False
    feedback_id = UUID(body["feedback_id"])

    # 库行断言：consent_at + 快照密文（非明文）+ comment 已脱敏
    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "select consent_at, content_snapshot_enc, comment, status, trace_id "
                        "from ai_feedback where id = :id"
                    ),
                    {"id": str(feedback_id)},
                )
            ).mappings().one()
        assert row["consent_at"] is not None
        assert row["status"] == "submitted"
        assert row["trace_id"] == trace_id
        enc = str(row["content_snapshot_enc"])
        assert enc.startswith("v1.")  # AES-GCM 密文格式
        assert "差旅报销怎么办理" not in enc  # 明文不落库
        assert "11010119900307777X" not in str(row["comment"])  # PII 已脱敏
        assert "答非所问" in str(row["comment"])  # 干净内容保留
    finally:
        await engine.dispose()

    # 审计
    assert await _audit_count(tenant_id, "campus.feedback.create") == 1

    # 幂等重放：同 (conversation, turn) 再提交 → 200 replayed=true 同 id
    resp2 = await fb_env.post(CREATE_URL, json={**payload, "rating": "helpful"}, headers=headers)
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["replayed"] is True
    assert body2["feedback_id"] == body["feedback_id"]
    # 重放也写审计（第 1 轮审查 Major-2：重放意图留痕 metadata.replayed=true）
    assert await _audit_count(tenant_id, "campus.feedback.create") == 2


@pytest.mark.asyncio
async def test_submit_feedback_target_validation(fb_env: AsyncClient) -> None:
    """D5：他人会话 404（不泄露存在性）；载荷非法 422。"""

    tenant_id, user_id = await _identity(STUDENT_MAIN)
    conversation_id, turn_id, trace_id = await _insert_ai_round(tenant_id, user_id)
    other_headers = await _token(fb_env, STUDENT_OTHER)

    # 他人会话 → 404
    resp = await fb_env.post(CREATE_URL, json={
        "conversation_id": str(conversation_id), "turn_id": str(turn_id),
        "trace_id": trace_id, "rating": "helpful", "reason_tags": [],
    }, headers=other_headers)
    assert resp.status_code == 404
    # 不存在的会话 → 404
    resp_missing = await fb_env.post(CREATE_URL, json={
        "conversation_id": str(uuid4()), "turn_id": str(turn_id),
        "trace_id": trace_id, "rating": "helpful", "reason_tags": [],
    }, headers=other_headers)
    assert resp_missing.status_code == 404

    headers = await _token(fb_env, STUDENT_MAIN)
    bad_rating = await fb_env.post(CREATE_URL, json={
        "conversation_id": str(conversation_id), "turn_id": str(turn_id),
        "trace_id": trace_id, "rating": "great", "reason_tags": [],
    }, headers=headers)
    assert bad_rating.status_code == 422
    bad_tags = await fb_env.post(CREATE_URL, json={
        "conversation_id": str(conversation_id), "turn_id": str(turn_id),
        "trace_id": trace_id, "rating": "helpful", "reason_tags": ["自定义"],
    }, headers=headers)
    assert bad_tags.status_code == 422

    # Major-1：已有反馈的轮，他人提交 → 404（重放路径不绕过归属校验/不泄露存在性）
    fb_resp = await fb_env.post(CREATE_URL, json={
        "conversation_id": str(conversation_id), "turn_id": str(turn_id),
        "trace_id": trace_id, "rating": "helpful", "reason_tags": [],
    }, headers=headers)
    assert fb_resp.status_code == 201
    other_replay = await fb_env.post(CREATE_URL, json={
        "conversation_id": str(conversation_id), "turn_id": str(turn_id),
        "trace_id": trace_id, "rating": "helpful", "reason_tags": [],
    }, headers=other_headers)
    assert other_replay.status_code == 404

    # Minor-5 关联：本人删除后对同轮重新提交 → 409（唯一键被匿名化行占位，
    # 「复活行」会破坏断链不变量；语义决策随 TD-343）
    fb_id = fb_resp.json()["feedback_id"]
    deleted = await fb_env.delete(f"{CREATE_URL}/{fb_id}", headers=headers)
    assert deleted.status_code == 200
    resubmit = await fb_env.post(CREATE_URL, json={
        "conversation_id": str(conversation_id), "turn_id": str(turn_id),
        "trace_id": trace_id, "rating": "helpful", "reason_tags": [],
    }, headers=headers)
    assert resubmit.status_code == 409
    assert resubmit.json()["detail"] == "FEEDBACK_ALREADY_EXISTS"


@pytest.mark.asyncio
async def test_b_side_view_handle_reply_and_anonymize(fb_env: AsyncClient) -> None:
    """A3~A7：B 端权限/详情解密/处理/答复 + 自助删除匿名化全弧。"""

    tenant_id, user_id = await _identity(STUDENT_MAIN)
    conversation_id, turn_id, trace_id = await _insert_ai_round(tenant_id, user_id)
    student_headers = await _token(fb_env, STUDENT_MAIN)
    handler_headers = await _token(fb_env, HANDLER)

    resp = await fb_env.post(CREATE_URL, json={
        "conversation_id": str(conversation_id), "turn_id": str(turn_id),
        "trace_id": trace_id, "rating": "unhelpful", "reason_tags": ["inaccurate"],
        "comment": "引用对不上。",
    }, headers=student_headers)
    assert resp.status_code == 201, resp.text
    feedback_id = resp.json()["feedback_id"]

    # C 端学生调 B 端端点 → 403（权限边界）
    forbidden = await fb_env.get(f"/api/v1/school/feedback/{feedback_id}", headers=student_headers)
    assert forbidden.status_code == 403

    # 详情：快照受控解密（明文返回）
    detail = await fb_env.get(f"/api/v1/school/feedback/{feedback_id}", headers=handler_headers)
    assert detail.status_code == 200, detail.text
    detail_body = detail.json()
    assert detail_body["status"] == "submitted"
    assert detail_body["anonymized"] is False
    snapshot = detail_body["snapshot"]
    assert snapshot["question"] == "差旅报销怎么办理？"
    assert "差旅报销需要发票和审批单" in snapshot["answer"]
    assert snapshot["citations"][0]["document"] == "财务报销手册"

    # 列表 + rating 筛选
    listed = await fb_env.get(
        "/api/v1/school/feedback", params={"rating": "unhelpful"}, headers=handler_headers,
    )
    assert listed.status_code == 200
    ids = [item["id"] for item in listed.json()["feedback"]]
    assert feedback_id in ids

    # 标记处理（幂等）+ 审计
    handled = await fb_env.post(f"/api/v1/school/feedback/{feedback_id}/handle", headers=handler_headers)
    assert handled.status_code == 200
    assert handled.json()["executed_now"] is True
    handled_again = await fb_env.post(f"/api/v1/school/feedback/{feedback_id}/handle", headers=handler_headers)
    assert handled_again.json()["executed_now"] is False
    assert await _audit_count(tenant_id, "school.feedback.handled") == 1

    # 答复 + 审计 + replies 行
    reply = await fb_env.post(
        f"/api/v1/school/feedback/{feedback_id}/reply",
        json={"content": "已核实，下版知识库会更新该条目。"}, headers=handler_headers,
    )
    assert reply.status_code == 200, reply.text
    reply_id = reply.json()["reply_id"]
    assert await _audit_count(tenant_id, "school.feedback.replied") == 1
    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            reply_row = (
                await session.execute(
                    text("select content, feedback_id from ai_feedback_replies where id = :id"),
                    {"id": reply_id},
                )
            ).mappings().one()
        assert reply_row["feedback_id"] == UUID(feedback_id)
        assert "下版知识库" in str(reply_row["content"])
    finally:
        await engine.dispose()

    # 自助删除（匿名化）：身份断链 + 快照/评论清空 + 审计
    deleted = await fb_env.delete(f"{CREATE_URL}/{feedback_id}", headers=student_headers)
    assert deleted.status_code == 200
    assert deleted.json()["anonymized"] is True
    assert await _audit_count(tenant_id, "campus.feedback.delete") == 1

    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "select user_id, comment, content_snapshot_enc, status from ai_feedback "
                        "where id = :id"
                    ),
                    {"id": feedback_id},
                )
            ).mappings().one()
    finally:
        await engine.dispose()
    assert row["user_id"] is None  # 库层不变量 ck_ai_feedback_anonymized_no_user
    assert row["comment"] is None
    assert row["content_snapshot_enc"] is None
    assert row["status"] == "anonymized"

    # 身份断链：原本人再删也 404（不泄露存在性——简报 §10.2 语义修正）
    deleted_again = await fb_env.delete(f"{CREATE_URL}/{feedback_id}", headers=student_headers)
    assert deleted_again.status_code == 404

    # 匿名化后：B 端详情无快照/comment（A7）；答复 409
    detail_after = await fb_env.get(f"/api/v1/school/feedback/{feedback_id}", headers=handler_headers)
    assert detail_after.status_code == 200
    after_body = detail_after.json()
    assert after_body["anonymized"] is True
    assert after_body["snapshot"] is None
    assert after_body["comment"] is None
    reply_after = await fb_env.post(
        f"/api/v1/school/feedback/{feedback_id}/reply",
        json={"content": "补充说明。"}, headers=handler_headers,
    )
    assert reply_after.status_code == 409

    # 他人删除 → 404（不泄露存在性）
    other_headers = await _token(fb_env, STUDENT_OTHER)
    conv2, turn2, trace2 = await _insert_ai_round(tenant_id, user_id)
    resp2 = await fb_env.post(CREATE_URL, json={
        "conversation_id": str(conv2), "turn_id": str(turn2),
        "trace_id": trace2, "rating": "helpful", "reason_tags": [],
    }, headers=student_headers)
    fb2 = resp2.json()["feedback_id"]
    cross = await fb_env.delete(f"{CREATE_URL}/{fb2}", headers=other_headers)
    assert cross.status_code == 404

    # Minor-8：跨租 404（第二租户的校方 handler 查第一租户反馈）
    admin_engine = make_test_engine("admin")
    try:
        await seed_tenant_user(
            admin_engine, school_code="SCH-AI05-B", login_id="SCH-B-7190",
            password=PASSWORD, user_type="teacher",
            permissions=("school.feedback.view", "school.feedback.update"),
        )
    finally:
        await admin_engine.dispose()
    other_tenant_headers = await login_tenant(
        fb_env, school_code="SCH-AI05-B", login_id="SCH-B-7190", password=PASSWORD,
    )
    cross_detail = await fb_env.get(f"/api/v1/school/feedback/{fb2}", headers=other_tenant_headers)
    assert cross_detail.status_code == 404
    cross_list = await fb_env.get("/api/v1/school/feedback", headers=other_tenant_headers)
    assert cross_list.status_code == 200
    assert cross_list.json()["feedback"] == []  # 跨租列表只见本租（空）


@pytest.mark.asyncio
async def test_b_side_cursor_pagination(fb_env: AsyncClient) -> None:
    """A3：keyset 游标翻页不重不漏（B 端列表）。"""

    tenant_id, user_id = await _identity(STUDENT_MAIN)
    student_headers = await _token(fb_env, STUDENT_MAIN)
    handler_headers = await _token(fb_env, HANDLER)

    created: list[str] = []
    for i in range(3):
        conversation_id, turn_id, trace_id = await _insert_ai_round(tenant_id, user_id)
        resp = await fb_env.post(CREATE_URL, json={
            "conversation_id": str(conversation_id), "turn_id": str(turn_id),
            "trace_id": trace_id, "rating": "helpful" if i % 2 == 0 else "unhelpful",
            "reason_tags": ["outdated"],
            # created_at 间隔由插行时间自然错开；同批插入可能同秒——用游标仍按 (created,id) 唯一
        }, headers=student_headers)
        assert resp.status_code == 201, resp.text
        created.append(resp.json()["feedback_id"])
        await _bump_created_at(tenant_id, UUID(created[-1]), minutes=-i)

    page1 = await fb_env.get("/api/v1/school/feedback", params={"limit": 2}, headers=handler_headers)
    assert page1.status_code == 200
    body1 = page1.json()
    page1_ids = [item["id"] for item in body1["feedback"]]
    assert len(page1_ids) == 2
    assert body1["next_cursor"] is not None

    page2 = await fb_env.get(
        "/api/v1/school/feedback",
        params={"limit": 2, "cursor": body1["next_cursor"]}, headers=handler_headers,
    )
    page2_ids = [item["id"] for item in page2.json()["feedback"]]
    assert len(page2_ids) == 1
    assert not (set(page1_ids) & set(page2_ids))
    assert set(page1_ids) | set(page2_ids) == set(created)

    bad_cursor = await fb_env.get(
        "/api/v1/school/feedback", params={"cursor": "not-a-cursor"}, headers=handler_headers,
    )
    assert bad_cursor.status_code == 400


async def _bump_created_at(tenant_id: UUID, feedback_id: UUID, *, minutes: int) -> None:
    """错开 created_at（游标排序确定性）。"""

    engine = make_test_engine("admin")
    try:
        factory = make_session_factory(engine)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "update ai_feedback set created_at = :ts, updated_at = :ts "
                    "where id = :id and tenant_id = :t"
                ),
                {
                    "ts": datetime.now(UTC) + timedelta(minutes=minutes),
                    "id": str(feedback_id), "t": str(tenant_id),
                },
            )
    finally:
        await engine.dispose()
