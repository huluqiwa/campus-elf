"""C 端对话 SSE（M4-AI01b 真快路径 Loop；M0 空壳已替换，TD-059 收口）。

事件序与兜底链由 `FastPathLoop` 产出（`app/harness/loop.py`）；本端点只负责
鉴权、入参解析与 SSE 编码（事件类型化 → `format_sse`）。

鉴权（M1-E1 起）：`campus.chat.use` + 租户 realm（学生/教师 C 端角色默认持有）；
消息文本不进 trace（loop 只记事件形状与计数，不记 PII）。

LLM 未配置时端点仍存活：`FastPathLoop` 走 `LLM_NOT_CONFIGURED` 兜底
（AI-01a D6 口径）；检索依赖故障 → `RETRIEVAL_FAILED`（可重试）。
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, tuple_

from app.audit.writer import append_audit
from app.auth.principal import Principal, require_permission
from app.core.sse import SSE_HEADERS, SSE_MEDIA_TYPE, format_sse
from app.db.models.ai import Conversation
from app.db.repository import (
    CursorError,
    ScopeBypass,
    TenantScopedRepository,
    decode_cursor,
    encode_cursor,
)
from app.db.session import db_session
from app.harness.orchestrator import ChatOrchestrator
from app.harness.persistence import clear_messages_in_session, get_recent_messages

router = APIRouter(prefix="/campus", tags=["campus"])

ChatTurnDep = require_permission("campus.chat.use", realm="tenant")


class ChatTurnRequest(BaseModel):
    """对话请求（M4-AI06/07 起有状态：服务端持久化消息 + 近 10 轮上下文注入）。"""

    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, max_length=64)


def _parse_uuid(value: str | None) -> UUID | None:
    """客户端会话标识解析为 UUID；非 UUID（或缺失）由服务端另分配并经 meta 回显。"""

    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _chat_loop(request: Request) -> ChatOrchestrator:
    """从应用状态取 Loop（create_app 装配；缺失即装配错误，显式失败）。"""

    loop = getattr(request.app.state, "chat_loop", None)
    if loop is None:
        raise RuntimeError("chat loop 未注入；请通过 create_app(chat_loop=...) 装配")
    return cast(ChatOrchestrator, loop)


@router.post("/chat/turns")
async def create_chat_turn(
    request: Request,
    payload: ChatTurnRequest,
    principal: Annotated[Principal, ChatTurnDep],
) -> StreamingResponse:
    """对话回合：SSE 返回 meta/step/tool_call/tool_result/reasoning_summary/
    delta/citation/done（或 error）。

    参数:
        request: FastAPI 请求（读取 app.state.chat_loop）。
        payload: 对话请求体（message 1..2000 字）。
        principal: 已鉴权的租户主体（`campus.chat.use`）。

    返回值:
        StreamingResponse: `text/event-stream`。

    异常:
        HTTPException: 租户身份缺失时 403；请求体非法时 422。
        RuntimeError: Loop 未装配（应用装配错误）。
    """

    loop = _chat_loop(request)
    tenant_id = principal.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="缺少租户上下文")
    turn_id = uuid4()
    conversation_uuid = _parse_uuid(payload.conversation_id)

    async def event_stream() -> AsyncIterator[str]:
        """类型化事件 → SSE 帧；客户端断开经生成器关闭传播到 Loop（USER_CANCELLED）。"""

        try:
            async for agent_event in loop.run(
                tenant_id=tenant_id,
                user_id=principal.user_id,
                turn_id=turn_id,
                message=payload.message,
                conversation_id=conversation_uuid,
                permissions=principal.permissions,
                school_names=principal.school_names,
            ):
                yield format_sse(agent_event.type.value, agent_event.payload)
        except (GeneratorExit, asyncio.CancelledError):
            # Loop 在自身生成器关闭时已写 USER_CANCELLED trace；此处仅向上传播。
            raise

    return StreamingResponse(event_stream(), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS)


# ---------------------------------------------------------------------------
# 会话管理端点（M4-AI06/07：列表/历史/清空）
# ---------------------------------------------------------------------------



_CLEAR_PERMISSION = require_permission("campus.conversation.clear", realm="tenant")


@router.get("/conversations")
async def list_conversations(
    request: Request,
    principal: Annotated[Principal, ChatTurnDep],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> JSONResponse:
    """当前用户的会话列表（keyset 游标分页；按最近消息降序）。

    游标 = 仓储标准编解码（base64url(JSON)：<sort_key ISO, id>）；sort_key =
    coalesce(last_message_at, created_at)（永不空，与排序键一致，M2 修复：cursor
    真正参与过滤）。坏游标 400（与其他列表端点同契约，绝不静默回第一页）。
    """

    tenant_id = principal.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="缺少租户上下文")

    if cursor is not None:
        try:
            cursor_dt, cursor_id = decode_cursor(cursor)
        except CursorError as exc:
            raise HTTPException(status_code=400, detail=f"cursor 非法：{exc}") from exc

    sort_key = func.coalesce(Conversation.last_message_at, Conversation.created_at)
    async with db_session(tenant_id=tenant_id) as session:
        repo = TenantScopedRepository(
            session, Conversation, tenant_id,
            scope=ScopeBypass(reason="会话列表：用户自己的会话"),
        )
        stmt = (
            repo.base_query()
            .where(Conversation.user_id == principal.user_id, Conversation.status == "active")
        )
        if cursor is not None:
            stmt = stmt.where(
                tuple_(sort_key, Conversation.id) < (cursor_dt, cursor_id),
            )
        stmt = stmt.order_by(sort_key.desc(), Conversation.id.desc()).limit(limit + 1)
        rows = (await session.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor(last.last_message_at or last.created_at, last.id)
    return JSONResponse(content={
        "conversations": [
            {
                "id": str(c.id),
                "title": c.title,
                "status": c.status,
                "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
                "created_at": c.created_at.isoformat(),
            }
            for c in items
        ],
        "next_cursor": next_cursor,
    })


@router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(
    request: Request,
    conversation_id: UUID,
    principal: Annotated[Principal, ChatTurnDep],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> JSONResponse:
    """会话消息历史（排除 deleted；按时间正序）。"""

    tenant_id = principal.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="缺少租户上下文")

    async with db_session(tenant_id=tenant_id) as session:
        conv_repo = TenantScopedRepository(
            session, Conversation, tenant_id,
            scope=ScopeBypass(reason="会话历史：归属校验"),
        )
        conv = await conv_repo.get(conversation_id)
        if conv is None or conv.user_id != principal.user_id:
            raise HTTPException(status_code=404, detail="会话不存在")
        messages = await get_recent_messages(
            tenant_id=tenant_id, conversation_id=conversation_id, limit=limit,
        )
    return JSONResponse(content={"messages": messages})


@router.delete("/conversations/{conversation_id}/messages")
async def clear_conversation_messages(
    request: Request,
    conversation_id: UUID,
    principal: Annotated[Principal, _CLEAR_PERMISSION],
) -> JSONResponse:
    """AI-07：清空当前会话消息（软删 + 审计 + 幂等；单事务——m4 修复）。

    软删 UPDATE 在 persistence 层（`clear_messages_in_session`，显式 tenant_id
    谓词）；端点只做鉴权/归属校验/审计编排——第 2 轮审查 Major-2。
    """

    tenant_id = principal.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="缺少租户上下文")

    async with db_session(tenant_id=tenant_id) as session:
        conv_repo = TenantScopedRepository(
            session, Conversation, tenant_id,
            scope=ScopeBypass(reason="清空会话：归属校验"),
        )
        conv = await conv_repo.get(conversation_id)
        if conv is None or conv.user_id != principal.user_id or conv.status != "active":
            raise HTTPException(status_code=404, detail="会话不存在")
        count = await clear_messages_in_session(
            session, tenant_id=tenant_id, conversation_id=conversation_id,
        )
        await append_audit(
            session,
            action="campus.conversation.clear",
            resource_type="conversation",
            resource_id=str(conversation_id),
            actor_realm="tenant",
            actor_id=principal.user_id,
            tenant_id=tenant_id,
            result="success",
            metadata={"cleared_count": count},
        )
        await session.commit()

    return JSONResponse(content={"cleared": count})
