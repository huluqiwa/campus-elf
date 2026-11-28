"""会话持久化（M4-AI06/07 步 2）：消息写入 + 上下文查取 + trace/usage 落库。

所有函数在 `db_session(tenant_id=...)` 上下文中执行（租户定域 + RLS）。
"""

import logging
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import func, update

from app.db.models.ai import Conversation, ConversationMessage
from app.db.repository import ScopeBypass, TenantScopedRepository
from app.db.session import db_session
from app.harness.audit import redact_mapping
from app.harness.contracts import LlmCallUsage, StopReason, TraceRecord

logger = logging.getLogger(__name__)

# 卡 3-A：近 10 **轮**原文注入（1 轮 = user + assistant 两条）
_CONTEXT_WINDOW_MESSAGES = 20

_PUBLISHED_READ_REASON = "会话持久化：活跃版本解析（kb_publish_aliases × PUBLISHED 只读）"


async def _resolve_pinned_in_session(
    session: Any,
    tenant_id: UUID,
) -> UUID | None:
    """会话内解析租户当前活跃发布版本（pinned_kb_version 落库用）。

    alias 行 × 文档 PUBLISHED（与检索活跃版本发现同源）；多个取最新 alias 行；
    无活跃发布 → None（fail-closed，不回退任选）。仅作记录语义（TD-P3：
    版本切换手动触发，本点不绑定检索）。
    """

    from app.db.models.kb import KbDocument, KbPublishAlias

    alias_repo = TenantScopedRepository(
        session,
        KbPublishAlias,
        tenant_id,
        scope=ScopeBypass(reason=_PUBLISHED_READ_REASON),
    )
    alias_rows = (await session.execute(alias_repo.base_query())).scalars().all()
    if not alias_rows:
        return None
    doc_repo = TenantScopedRepository(
        session,
        KbDocument,
        tenant_id,
        scope=ScopeBypass(reason=_PUBLISHED_READ_REASON),
    )
    doc_ids = {row.document_id for row in alias_rows}
    docs = (
        (
            await session.execute(
                doc_repo.base_query().where(
                    KbDocument.id.in_(doc_ids),
                    KbDocument.status == "PUBLISHED",
                )
            )
        )
        .scalars()
        .all()
    )
    published_doc_ids = {doc.id for doc in docs}
    candidates = [row for row in alias_rows if row.document_id in published_doc_ids]
    if not candidates:
        return None
    best = max(candidates, key=lambda row: row.created_at)
    return UUID(str(best.version_id))


async def get_or_create_conversation(
    *,
    tenant_id: UUID,
    user_id: UUID,
    conversation_id: UUID | None = None,
) -> tuple[UUID, bool]:
    """查找或创建会话；返回 (conversation_id, created)。

    D1：客户端传 UUID → 查找（非本人/已删 → 按 D1 语义新建**同名 id 行**，
    并发撞唯一键时回查重取）；None → 服务端分配。
    D3：创建时写入当前活跃版本（pinned_kb_version_id），后续轮固定不变。
    """

    from sqlalchemy.exc import IntegrityError

    async with db_session(tenant_id=tenant_id) as session:
        repo = TenantScopedRepository(
            session,
            Conversation,
            tenant_id,
            scope=ScopeBypass(reason="会话持久化：用户自己的会话（personal scope）"),
        )
        if conversation_id is not None:
            existing = await repo.get(conversation_id)
            if existing is not None and existing.user_id == user_id and existing.status == "active":
                return existing.id, False
        pinned = await _resolve_pinned_in_session(session, tenant_id)
        conv = Conversation(
            # m6：客户端指定 id 时按该 id 建（并发同 id 双建靠主键唯一 + 回查收敛）
            id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            status="active",
            pinned_kb_version_id=pinned,
        )
        session.add(conv)
        try:
            await session.flush()
            await session.commit()
        except IntegrityError:
            # 并发双建/他人占用同 id：回查（新事务）；非本人活跃会话 → 显式失败
            await session.rollback()
            if conversation_id is None:
                raise
            async with db_session(tenant_id=tenant_id) as retry_session:
                retry_repo = TenantScopedRepository(
                    retry_session,
                    Conversation,
                    tenant_id,
                    scope=ScopeBypass(reason="会话持久化：并发双建回查"),
                )
                winner = await retry_repo.get(conversation_id)
            if winner is not None and winner.user_id == user_id and winner.status == "active":
                return winner.id, False
            raise ValueError("conversation id 冲突（非本人或不可复用）") from None
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
    """写入单条消息并更新 conversation.last_message_at（DB 时钟）。"""

    async with db_session(tenant_id=tenant_id) as session:
        TenantScopedRepository(
            session,
            ConversationMessage,
            tenant_id,
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
        # m8：last_message_at 用数据库时钟（应用时钟在多进程下会漂移）
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id)
            .values(last_message_at=func.now())
        )
        await session.commit()
        return msg.id


async def get_recent_messages(
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    limit: int = _CONTEXT_WINDOW_MESSAGES,
) -> list[dict[str, str]]:
    """查近 N 条非 deleted/failed 消息（时间正序）；返回 [{"role": ..., "content": ...}]。

    上下文注入口径为近 10 轮 = 20 条（卡 3-A）；`limit` 为消息条数。
    failed（失败回合的空 assistant 占位）不进上下文——空文本既浪费窗口又污染
    对话形状（第 2 轮审查 Minor-6）；失败回合的 user 提问仍保留。
    **口径注**：本函数同时服务历史视图端点（GET /conversations/{id}/messages），
    failed 消息在历史视图中同样不可见（第 3 轮审查备注 1 登记）。
    """

    async with db_session(tenant_id=tenant_id) as session:
        repo = TenantScopedRepository(
            session,
            ConversationMessage,
            tenant_id,
            scope=ScopeBypass(reason="会话持久化：上下文查取"),
        )
        stmt = (
            repo.base_query()
            .where(
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.status.not_in(("deleted", "failed")),
            )
            .order_by(ConversationMessage.created_at.desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {"role": row.role, "content": row.content}
            for row in reversed(rows)  # 时间正序
        ]


async def get_context_messages(
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    limit: int = _CONTEXT_WINDOW_MESSAGES,
) -> list[dict[str, str]]:
    """只查可安全回灌模型的 completed 消息。

    blocked/redacted/mixed 回合仍以 partial 状态保留在历史视图，但不作为下一轮
    模型上下文。这样原始敏感或外校请求可被展示和审计，却不会再次进入 LLM。
    """

    async with db_session(tenant_id=tenant_id) as session:
        repo = TenantScopedRepository(
            session,
            ConversationMessage,
            tenant_id,
            scope=ScopeBypass(reason="会话持久化：模型上下文查取"),
        )
        stmt = (
            repo.base_query()
            .where(
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.status == "completed",
            )
            .order_by(ConversationMessage.created_at.desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {"role": row.role, "content": row.content}
            for row in reversed(rows)
        ]


async def clear_messages_in_session(
    session: Any,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
) -> int:
    """在调用方事务内软删该会话全部非 deleted 消息；返回受影响条数（AI-07）。

    供清空端点在「归属校验 + 软删 + 审计」单事务内复用（第 2 轮审查 Major-2：
    端点层不做裸 UPDATE，数据访问收敛到 persistence 层）；显式 tenant_id 谓词
    是第一道防线，RLS 是第二道。
    """

    stmt = (
        update(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == conversation_id,
            ConversationMessage.tenant_id == tenant_id,
            ConversationMessage.status != "deleted",
        )
        .values(status="deleted")
    )
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)


async def save_trace(*, tenant_id: UUID, record: TraceRecord) -> None:
    """写入 chat_traces + chat_trace_spans + llm_usage（TD-330 收口，A5/A6）。"""

    from app.db.models.ai import ChatTrace, ChatTraceSpan, LlmUsage

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
                    attributes_json=redact_mapping(span.attributes or {}),
                    status=span.status,
                )
            )
        # A6：每回合一条 usage（护栏回合 provider/model 为空串也落）
        session.add(
            LlmUsage(
                tenant_id=tenant_id,
                trace_id=record.trace_id,
                provider=record.provider,
                model=record.model,
                tokens_in=record.tokens_in,
                tokens_out=record.tokens_out,
                latency_ms=record.latency_ms,
            )
        )
        await session.commit()


async def save_trace_span(*, tenant_id: UUID, record: Any) -> None:
    """独立写入单个 span（ToolExecutor 经 recorder 独立上报、不随 turn 内嵌时）。"""

    from app.db.models.ai import ChatTraceSpan

    if record.tenant_id is None:
        logger.warning("trace span 缺 tenant_id，跳过落库 trace_id=%s", record.trace_id)
        return
    async with db_session(tenant_id=record.tenant_id) as session:
        session.add(
            ChatTraceSpan(
                tenant_id=record.tenant_id,
                trace_id=record.trace_id,
                span_type=record.span_type,
                name=record.name,
                start_ms=record.start_ms,
                duration_ms=record.duration_ms,
                attributes_json=redact_mapping(record.attributes or {}),
                status=record.status,
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


def _add_guard_event_in_session(
    session: Any,
    *,
    tenant_id: UUID,
    trace_id: str,
    result: Any,
) -> None:
    """在调用方事务内追加一条脱敏 guard event。"""

    from app.db.models.ai import GuardEvent

    session.add(
        GuardEvent(
            tenant_id=tenant_id,
            trace_id=trace_id[:64],
            direction="input",
            rule=result.rule,
            severity=result.severity,
            action=result.action,
            excerpt_redacted=result.reason,
        )
    )


def _add_trace_rows_in_session(
    session: Any,
    *,
    tenant_id: UUID,
    record: TraceRecord,
) -> None:
    """在调用方事务内写入 trace/span/usage（与 save_trace 同构）。"""

    from app.db.models.ai import ChatTrace, ChatTraceSpan, LlmUsage

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
                attributes_json=redact_mapping(span.attributes or {}),
                status=span.status,
            )
        )
    session.add(
        LlmUsage(
            tenant_id=tenant_id,
            trace_id=record.trace_id,
            provider=record.provider,
            model=record.model,
            tokens_in=record.tokens_in,
            tokens_out=record.tokens_out,
            latency_ms=record.latency_ms,
        )
    )


def _usage_attributes(usage: LlmCallUsage | None) -> dict[str, object]:
    """把中间调用 usage 投影为不含提示词/正文的 trace span 属性。"""

    if usage is None:
        return {}
    return {
        "router_provider": usage.provider,
        "router_model": usage.model,
        "router_prompt_name": usage.prompt_name,
        "router_prompt_version": usage.prompt_version,
        "router_prompt_hash": usage.prompt_hash,
        "router_tokens_in": usage.tokens_in,
        "router_tokens_out": usage.tokens_out,
        "router_latency_ms": usage.latency_ms,
    }


class TenantScopePersister:
    """TD-361 可靠安全事件端口；失败直接抛出，调用方 fail-closed。"""

    async def persist_refusal(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        conversation_id: UUID,
        turn_id: UUID,
        trace_id: str,
        guard_result: Any,
        rule_codes: tuple[str, ...],
        blocked_count: int,
        stop_reason: StopReason = StopReason.REFUSAL,
        scope_mode: str = "block",
        call_usage: LlmCallUsage | None = None,
    ) -> None:
        """单事务提交纯拒绝的 guard/trace/span/usage。

        stop_reason/scope_mode 由调用方给出：纯跨租拒答是 REFUSAL/block，
        身份声称澄清回合是 GUARD_BLOCK/identity_claim——trace 必须与用户看到的
        话术同源（TD-361 六轮审查 m2）。
        """

        from app.harness.contracts import TraceSpan

        span = TraceSpan(
            trace_id=trace_id,
            tenant_id=tenant_id,
            span_type="tenant_scope",
            name="tenant_scope",
            start_ms=0,
            duration_ms=0,
            attributes={
                "mode": scope_mode,
                "rule_codes": list(rule_codes),
                "blocked_count": blocked_count,
                **_usage_attributes(call_usage),
            },
        )
        record = TraceRecord(
            trace_id=trace_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            intents=[],
            route_path="guard",
            provider=call_usage.provider if call_usage is not None else "",
            model=call_usage.model if call_usage is not None else "",
            prompt_name=call_usage.prompt_name if call_usage is not None else "",
            prompt_version=call_usage.prompt_version if call_usage is not None else "",
            prompt_hash=call_usage.prompt_hash if call_usage is not None else "",
            tokens_in=call_usage.tokens_in if call_usage is not None else 0,
            tokens_out=call_usage.tokens_out if call_usage is not None else 0,
            latency_ms=call_usage.latency_ms if call_usage is not None else 0,
            stop_reason=stop_reason,
            spans=[span],
        )
        async with db_session(tenant_id=tenant_id) as session:
            _add_guard_event_in_session(
                session,
                tenant_id=tenant_id,
                trace_id=trace_id,
                result=guard_result,
            )
            _add_trace_rows_in_session(session, tenant_id=tenant_id, record=record)
            await session.commit()

    async def persist_route_usage(
        self,
        *,
        tenant_id: UUID,
        trace_id: str,
        call_usage: LlmCallUsage,
    ) -> None:
        """持久化 ALLOW 回合的 router 调用，不保存提示词或回答正文。"""

        from app.db.models.ai import ChatTraceSpan

        async with db_session(tenant_id=tenant_id) as session:
            session.add(
                ChatTraceSpan(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    span_type="llm",
                    name="scope_router",
                    start_ms=0,
                    duration_ms=call_usage.latency_ms,
                    attributes_json=_usage_attributes(call_usage),
                    status="ok",
                )
            )
            await session.commit()

    async def persist_event(
        self,
        *,
        tenant_id: UUID,
        trace_id: str,
        guard_result: Any,
        rule_codes: tuple[str, ...],
        blocked_count: int,
        safe_intents: tuple[str, ...],
        degraded: bool,
        scope_finalized: bool = True,
        mode: str = "mixed",
        call_usage: LlmCallUsage | None = None,
    ) -> None:
        """混合回合在首个 SSE 前提交 guard event 与脱敏 scope span。

        mode="quote_redacted"：引述内指令被净化但整体放行（审计可见，行动为 redact）。
        """

        from app.db.models.ai import ChatTraceSpan

        async with db_session(tenant_id=tenant_id) as session:
            _add_guard_event_in_session(
                session,
                tenant_id=tenant_id,
                trace_id=trace_id,
                result=guard_result,
            )
            session.add(
                ChatTraceSpan(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    span_type="tenant_scope",
                    name="tenant_scope",
                    start_ms=0,
                    duration_ms=0,
                    attributes_json={
                        "mode": mode,
                        "rule_codes": list(rule_codes),
                        "refused_count": blocked_count,
                        "safe_intents": list(safe_intents),
                        "degraded": degraded,
                        "scope_finalized": scope_finalized,
                        **_usage_attributes(call_usage),
                    },
                    status="ok",
                )
            )
            await session.commit()

    async def finalize_event(
        self,
        *,
        tenant_id: UUID,
        trace_id: str,
        rule_codes: tuple[str, ...],
        blocked_count: int,
        safe_intents: tuple[str, ...],
        degraded: bool,
        call_usage: LlmCallUsage | None = None,
    ) -> None:
        """追加最终混合 scope span；基础保守记录在断流时仍保留。"""

        from app.db.models.ai import ChatTraceSpan

        async with db_session(tenant_id=tenant_id) as session:
            session.add(
                ChatTraceSpan(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    span_type="tenant_scope",
                    name="tenant_scope_outcome",
                    start_ms=0,
                    duration_ms=0,
                    attributes_json={
                        "mode": "mixed",
                        "rule_codes": list(rule_codes),
                        "refused_count": blocked_count,
                        "safe_intents": list(safe_intents),
                        "degraded": degraded,
                        "scope_finalized": True,
                        **_usage_attributes(call_usage),
                    },
                    status="ok",
                )
            )
            await session.commit()
