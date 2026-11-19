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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.auth.principal import Principal, require_permission
from app.core.sse import SSE_HEADERS, SSE_MEDIA_TYPE, format_sse
from app.harness.loop import FastPathLoop

router = APIRouter(prefix="/campus", tags=["campus"])

ChatTurnDep = require_permission("campus.chat.use", realm="tenant")


class ChatTurnRequest(BaseModel):
    """对话请求（MVP：单轮；会话持久化随 AI-06/07 点建表）。"""

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


def _chat_loop(request: Request) -> FastPathLoop:
    """从应用状态取 Loop（create_app 装配；缺失即装配错误，显式失败）。"""

    loop = getattr(request.app.state, "chat_loop", None)
    if loop is None:
        raise RuntimeError("chat loop 未注入；请通过 create_app(chat_loop=...) 装配")
    return cast(FastPathLoop, loop)


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
            ):
                yield format_sse(agent_event.type.value, agent_event.payload)
        except (GeneratorExit, asyncio.CancelledError):
            # Loop 在自身生成器关闭时已写 USER_CANCELLED trace；此处仅向上传播。
            raise

    return StreamingResponse(event_stream(), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS)
