"""ChatOrchestrator 统一入口（M4-AI01c + M4-AI06/07 会话持久化）。

事件流：orchestrator 发 `meta` + `step(route)`（首事件立出），路由结果传入对应
loop（loop 跳过自身 route 段）。loop 事件透传给消费方，orchestrator 同时收集
assistant 回答文本与 done 数据，**回合结束后持久化** user+assistant 消息。

**TD-334**：`permissions` 必传。**B1 修复**：消息持久化 + 上下文注入 + trace/usage
落库在本层接线（不在 loop 层——loop 保持无状态纯计算）。
"""

import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from app.core.metrics import record_guard_hit
from app.harness.contracts import AgentEvent, AgentEventType, StopReason, TraceRecord
from app.harness.guards.input import detect_input
from app.harness.guards.scripts import safety_script_for
from app.harness.loop import FastPathLoop
from app.harness.persistence import (
    get_or_create_conversation,
    get_recent_messages,
    save_message,
)
from app.harness.react_loop import ReactLoop
from app.harness.router import LlmRouter

logger = logging.getLogger(__name__)


class ChatOrchestrator:
    """统一对话入口。

    参数:
        router: LLM 路由器。
        fast_loop: 快路径管道（AI-01b）。
        react_loop: ReAct 循环（AI-01c）。
        guard_events_sink: 护栏事件落库端口（AI-09）。
    """

    def __init__(
        self,
        *,
        router: LlmRouter,
        fast_loop: FastPathLoop,
        react_loop: ReactLoop,
        guard_events_sink: Any | None = None,
        trace_sink: Any | None = None,
    ) -> None:
        self._router = router
        self._fast_loop = fast_loop
        self._react_loop = react_loop
        self._guard_events_sink = guard_events_sink
        self._trace_sink = trace_sink

    async def _flush_trace(self) -> None:
        """trace 缓冲落库（DbTraceSink；InMemory 无 flush 自动跳过）。"""

        flush = getattr(self._trace_sink, "flush", None)
        if flush is not None:
            try:
                await flush()
            except Exception:
                logger.exception("trace 落库失败（回合完成但 trace 未落）")

    async def run(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        turn_id: UUID,
        message: str,
        conversation_id: UUID | None = None,
        permissions: frozenset[str],
        persist: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        """执行一个对话回合：路由 → 分派 → 持久化。

        参数:
            permissions: principal 实际权限集（**必传**——TD-334）。
            persist: True = 消息/trace/usage 落库（生产）；False = 仅测试。
        """

        trace_id = uuid4().hex
        # M3 修复：先查/创建 conversation（拿到真实 conv_id），再发 meta（回显真实值）
        conv_id = conversation_id if conversation_id is not None else uuid4()
        if persist:
            try:
                conv_id, _created = await get_or_create_conversation(
                    tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id,
                )
            except Exception:
                # 第 2 轮审查 Major-1：失败回退**绝不复用客户端原值**——同租户他人
                # conversation_id 撞归属校验时，复用会把他人的会话当成本回合落库/
                # 上下文来源（跨用户读写）。全新 uuid：消息落库走 FK 失败被吞、上下
                # 文为空；trace 仍落库（chat_traces.conversation_id 无 FK，指向不存在
                # 的会话 id——留痕语义可接受，口径统一随 AI-04/观测收尾点，第 3 轮备注 2）。
                logger.exception("会话创建/查询失败（消息不落库、trace 留痕）")
                conv_id = uuid4()

        yield AgentEvent(
            type=AgentEventType.META,
            payload={
                "turn_id": str(turn_id),
                "trace_id": trace_id,
                "conversation_id": str(conv_id),
            },
        )
        yield AgentEvent(
            type=AgentEventType.STEP,
            payload={"stage": "route", "label": "正在理解问题"},
        )

        # 输入护栏（AI-09）：路由之前
        input_hits = detect_input(message)
        if input_hits:
            for result in input_hits:
                record_guard_hit(result.rule, result.severity, "input")
            if self._guard_events_sink is not None:
                self._guard_events_sink.append(
                    tenant_id=tenant_id, trace_id=trace_id, direction="input",
                    results=input_hits,
                )
            script = safety_script_for({r.rule for r in input_hits})
            # M3 口径：自伤/注入/违法 → 话术+终止（安全优先 A24；偏离 D5 登记 §10.4）
            yield AgentEvent(type=AgentEventType.DELTA, payload={"text": script})
            yield AgentEvent(
                type=AgentEventType.DONE,
                payload={
                    "turn_id": str(turn_id),
                    "trace_id": trace_id,
                    "conversation_id": str(conv_id),
                    "tokens_in": 0,  # nosec B105
                    "tokens_out": 0,  # nosec B105
                    "first_token_ms": 0,  # nosec B105
                    "latency_ms": 0,  # nosec B105
                    "stop_reason": StopReason.GUARD_BLOCK.value,
                },
            )
            if persist:
                try:
                    await save_message(
                        tenant_id=tenant_id, conversation_id=conv_id,
                        role="user", content=message, trace_id=trace_id,
                        status="completed",
                    )
                    await save_message(
                        tenant_id=tenant_id, conversation_id=conv_id,
                        role="assistant", content=script, trace_id=trace_id,
                        status="completed",
                    )
                except Exception:
                    logger.exception("护栏终止消息持久化失败")
            flush = getattr(self._guard_events_sink, "flush", None)
            if flush is not None:
                await flush()
            # A5 每回合必写：护栏终止回合也落 trace（route_path="guard"）
            if persist and self._trace_sink is not None:
                self._trace_sink.write_turn(
                    TraceRecord(
                        trace_id=trace_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        conversation_id=conv_id,
                        turn_id=turn_id,
                        intents=[],
                        route_path="guard",
                        stop_reason=StopReason.GUARD_BLOCK,
                        spans=[],
                    )
                )
                await self._flush_trace()
            return

        outcome = await self._router.route(message)
        if outcome.degraded:
            logger.warning("路由降级（simple 兜底）：intent=%s", outcome.route.primary_capability)

        # 上下文注入（AI-06 卡 3-A）：查近 10 轮非 deleted 消息
        history: list[dict[str, str]] = []
        if persist:
            try:
                history = await get_recent_messages(
                    tenant_id=tenant_id, conversation_id=conv_id,
                )
            except Exception:
                logger.exception("上下文查取失败（降级为无历史回合）")

        # 分派 loop；同时收集 assistant 回答 + done 数据（B1 修复）
        assistant_parts: list[str] = []
        done_data: dict[str, Any] | None = None
        error_data: dict[str, Any] | None = None

        if outcome.simple:
            intent = outcome.route.intents[0]
            query = intent.args.get("query") if isinstance(intent.args, dict) else message
            event_stream = self._fast_loop.run(
                tenant_id=tenant_id,
                user_id=user_id,
                turn_id=turn_id,
                message=str(query) if query else message,
                conversation_id=conv_id,
                permissions=permissions,
                skip_route_stage=True,
                trace_id=trace_id,
                history=history,
            )
        else:
            event_stream = self._react_loop.run(
                tenant_id=tenant_id,
                user_id=user_id,
                turn_id=turn_id,
                message=message,
                route=outcome,
                conversation_id=conv_id,
                permissions=permissions,
                skip_route_stage=True,
                trace_id=trace_id,
                history=history,
            )

        async for agent_event in event_stream:
            if agent_event.type == AgentEventType.DELTA:
                assistant_parts.append(agent_event.payload.get("text", ""))
            if agent_event.type == AgentEventType.DONE:
                done_data = agent_event.payload
            if agent_event.type == AgentEventType.ERROR:
                error_data = agent_event.payload
            yield agent_event

        # A5：回合 trace 落库（loop finish_trace 已缓冲；ToolExecutor 独立 span 一并）
        if persist:
            await self._flush_trace()

        # B1 修复：回合结束持久化 user + assistant 消息
        if persist and done_data is not None:
            try:
                await save_message(
                    tenant_id=tenant_id, conversation_id=conv_id,
                    role="user", content=message, trace_id=trace_id,
                    status="completed",
                )
                assistant_text = "".join(assistant_parts)
                # 护栏替换后的安全话术与正常回答同口径（completed）
                await save_message(
                    tenant_id=tenant_id, conversation_id=conv_id,
                    role="assistant", content=assistant_text, trace_id=trace_id,
                    status="completed",
                    token_usage={
                        "tokens_in": done_data.get("tokens_in", 0),
                        "tokens_out": done_data.get("tokens_out", 0),
                    },
                )
            except Exception:
                logger.exception("消息持久化失败（回合完成但未落库）")
        elif persist and error_data is not None:
            try:
                await save_message(
                    tenant_id=tenant_id, conversation_id=conv_id,
                    role="user", content=message, trace_id=trace_id,
                    status="completed",
                )
                await save_message(
                    tenant_id=tenant_id, conversation_id=conv_id,
                    role="assistant", content="",
                    trace_id=trace_id, status="failed",
                )
            except Exception:
                logger.exception("错误回合消息持久化失败")
