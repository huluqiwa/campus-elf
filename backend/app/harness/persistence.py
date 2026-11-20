"""会话持久化（M4-AI06/07 步 2）：消息写入 + 上下文查取 + trace/usage 落库。

所有函数在 `db_session(tenant_id=...)` 上下文中执行（租户定域 + RLS）。
"""

import logging
from collections.abc import Sequence
from datetime import UTC
from typing import Any
from uuid import UUID

from app.db.models.ai import Conversation, ConversationMessage
from app.db.repository import ScopeBypass, TenantScopedRepository
from app.db.session import db_session
from app.harness.contracts import TraceRecord

logger = logging.getLogger(__name__)

_CONTEXT_WINDOW = 10


async def get_or_create_conversation(
    *,
    tenant_id: UUID,
    user_id: UUID,
    conversation_id: UUID | None = None,
    pinned_kb_version_id: UUID | None = None,
) -> tuple[UUID, bool]:
    """查找或创建会话；返回 (conversation_id, created)。

    D1：客户端传 UUID → 查找（不存在则创建）；None → 创建。
    D3：创建时写入当前活跃版本（pinned_kb_version_id），后续轮固定不变。
    """

    async with db_session(tenant_id=tenant_id) as session:
        repo = TenantScopedRepository(
            session, Conversation, tenant_id,
            scope=ScopeBypass(reason="会话持久化：用户自己的会话（personal scope）"),
        )
        if conversation_id is not None:
            existing = await repo.get(conversation_id)
            if existing is not None and existing.user_id == user_id and existing.status == "active":
                return existing.id, False
        conv = Conversation(
            tenant_id=tenant_id,
            user_id=user_id,
            status="active",
            pinned_kb_version_id=pinned_kb_version_id,
        )
        session.add(conv)
        await session.flush()
        await session.commit()
        return conv.id, True


async def save_message(
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    role: str,
    content: str,
    trace_id: str | None = None,
    status: str = "completed",
    citations_json: dict[str, Any] | None = None,
    token_usage: dict[str, Any] | None = None,
) -> UUID:
    """写入单条消息并更新 conversation.last_message_at。"""

    from datetime import datetime

    async with db_session(tenant_id=tenant_id) as session:
        TenantScopedRepository(
            session, ConversationMessage, tenant_id,
            scope=ScopeBypass(reason="会话持久化：消息写入"),
        )
        msg = ConversationMessage(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            trace_id=trace_id,
            status=status,
            citations_json=citations_json,
            token_usage=token_usage,
        )
        session.add(msg)
        await session.flush()
        conv_repo = TenantScopedRepository(
            session, Conversation, tenant_id,
            scope=ScopeBypass(reason="会话持久化：更新 last_message_at"),
        )
        conv = await conv_repo.get(conversation_id)
        if conv is not None:
            conv.last_message_at = datetime.now(UTC)
        await session.commit()
        return msg.id


async def get_recent_messages(
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    limit: int = _CONTEXT_WINDOW,
) -> list[dict[str, str]]:
    """查近 N 轮非 deleted 消息（时间正序）；返回 [{"role": ..., "content": ...}]。"""


    async with db_session(tenant_id=tenant_id) as session:
        repo = TenantScopedRepository(
            session, ConversationMessage, tenant_id,
            scope=ScopeBypass(reason="会话持久化：上下文查取"),
        )
        stmt = (
            repo.base_query()
            .where(
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.status != "deleted",
            )
            .order_by(ConversationMessage.created_at.desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {"role": row.role, "content": row.content}
            for row in reversed(rows)  # 时间正序
        ]


async def clear_messages(*, tenant_id: UUID, conversation_id: UUID) -> int:
    """软删该会话全部非 deleted 消息；返回受影响条数（AI-07）。"""

    from sqlalchemy import update

    async with db_session(tenant_id=tenant_id) as session:
        TenantScopedRepository(
            session, ConversationMessage, tenant_id,
            scope=ScopeBypass(reason="清空会话：AI-07"),
        )
        stmt = (
            update(ConversationMessage)
            .where(
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.status != "deleted",
            )
            .values(status="deleted")
        )
        result = await session.execute(stmt)
        await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)


async def save_trace(*, tenant_id: UUID, record: TraceRecord) -> None:
    """写入 chat_traces + chat_trace_spans（TD-330 收口）。"""

    from app.db.models.ai import ChatTrace, ChatTraceSpan

    async with db_session(tenant_id=tenant_id) as session:
        session.add(
            ChatTrace(
                tenant_id=tenant_id,
                user_id=record.user_id,
                conversation_id=record.conversation_id,
                turn_id=record.turn_id,
                intents_json=list(record.intents),
                route_path=record.route_path,
                model=record.model,
                prompt_name=record.prompt_name,
                prompt_version=record.prompt_version,
                prompt_hash=record.prompt_hash,
                tokens_in=record.tokens_in,
                tokens_out=record.tokens_out,
                first_token_ms=record.first_token_ms,
                latency_ms=record.latency_ms,
                stop_reason=record.stop_reason.value if record.stop_reason else None,
                error_code=record.error_code,
            )
        )
        for span in record.spans:
            session.add(
                ChatTraceSpan(
                    tenant_id=tenant_id,
                    trace_id=record.trace_id,
                    span_type=span.span_type,
                    name=span.name,
                    start_ms=span.start_ms,
                    duration_ms=span.duration_ms,
                    attributes_json=span.attributes,
                    status=span.status,
                )
            )
        await session.commit()


def build_context_messages(
    history: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    """近 10 轮对话 → LLM messages 列表（卡 3-A：纯原文注入）。

    纯函数（D2 无状态）：历史消息已按时间正序排列。
    """

    return [{"role": m["role"], "content": m["content"]} for m in history]
