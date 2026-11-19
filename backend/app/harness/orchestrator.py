"""ChatOrchestrator 统一入口（M4-AI01c 步 4；决策卡 1-A/D1/D2 + TD-334）。

事件流：orchestrator 发 `meta` + `step(route)`（**首事件立出**——路由耗时不阻塞
首状态事件，D1），路由结果作参数传入对应 loop（loop 跳过自身 route 段，避免
meta 重复）：

- `simple=true`（单 kb_qa）→ `FastPathLoop`（AI-01b 管道，事件序逐字节不变）
- 其余（多意图/navigate）→ `ReactLoop`（有界 ReAct 循环）

**TD-334**：`permissions` 必传（principal 实际权限集），loop 的 ctx 从实际权限
派生——禁止 loop 内硬编码权限构造。
"""

import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from app.core.metrics import record_guard_hit
from app.harness.contracts import AgentEvent, AgentEventType, StopReason
from app.harness.guards.input import detect_input
from app.harness.guards.scripts import safety_script_for
from app.harness.loop import FastPathLoop
from app.harness.react_loop import ReactLoop
from app.harness.router import LlmRouter

logger = logging.getLogger(__name__)


class ChatOrchestrator:
    """统一对话入口（use-site 构造；端口注入对齐既有纪律）。

    参数:
        router: LLM 路由器（route_v1 分类；失败自带降级——D2）。
        fast_loop: 快路径管道（AI-01b）。
        react_loop: ReAct 循环（本点）。
    """

    def __init__(
        self,
        *,
        router: LlmRouter,
        fast_loop: FastPathLoop,
        react_loop: ReactLoop,
        guard_events_sink: Any | None = None,
    ) -> None:
        self._router = router
        self._fast_loop = fast_loop
        self._react_loop = react_loop
        self._guard_events_sink = guard_events_sink

    async def run(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        turn_id: UUID,
        message: str,
        conversation_id: UUID | None = None,
        permissions: frozenset[str],
    ) -> AsyncIterator[AgentEvent]:
        """执行一个对话回合：路由 → 分派快路径/ReAct（TD-334 权限透传）。

        参数:
            permissions: principal 实际权限集（**必传**——TD-334：漏传即静默越权，
                签名层强制；loop 层 None 兜底仅 standalone/测试专用）。
        """

        trace_id = uuid4().hex
        conversation = conversation_id if conversation_id is not None else uuid4()
        yield AgentEvent(
            type=AgentEventType.META,
            payload={
                "turn_id": str(turn_id),
                "trace_id": trace_id,
                "conversation_id": str(conversation),
            },
        )
        yield AgentEvent(
            type=AgentEventType.STEP,
            payload={"stage": "route", "label": "正在理解问题"},
        )
        # 输入护栏（AI-09）：路由之前——安全/成本双优（注入/自伤不消耗路由 LLM 调用）
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
            # M3 产品口径修正（安全优先 A24 宁枉勿纵）：自伤场景话术+终止
            # （不放行检索——危机时刻延迟即风险；检索结果经 GENERATE 后仍需过输出
            # 护栏自伤类可能再命中不可控）。偏离 D5 原口径已在简报 §10.4 登记留痕。
            yield AgentEvent(type=AgentEventType.DELTA, payload={"text": script})
            yield AgentEvent(
                type=AgentEventType.DONE,
                payload={
                    "turn_id": str(turn_id),
                    "trace_id": trace_id,
                    "conversation_id": str(conversation),
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "first_token_ms": 0,
                    "latency_ms": 0,
                    "stop_reason": StopReason.GUARD_BLOCK.value,
                },
            )
            flush = getattr(self._guard_events_sink, "flush", None)
            if flush is not None:
                await flush()
            return
        outcome = await self._router.route(message)
        if outcome.degraded:
            logger.warning("路由降级（simple 兜底）：intent=%s", outcome.route.primary_capability)
        if outcome.simple:
            intent = outcome.route.intents[0]
            query = intent.args.get("query") if isinstance(intent.args, dict) else message
            async for agent_event in self._fast_loop.run(
                tenant_id=tenant_id,
                user_id=user_id,
                turn_id=turn_id,
                message=str(query) if query else message,
                conversation_id=conversation,
                permissions=permissions,
                skip_route_stage=True,
                trace_id=trace_id,
            ):
                yield agent_event
            return
        async for agent_event in self._react_loop.run(
            tenant_id=tenant_id,
            user_id=user_id,
            turn_id=turn_id,
            message=message,
            route=outcome,
            conversation_id=conversation,
            permissions=permissions,
            skip_route_stage=True,
            trace_id=trace_id,
        ):
            yield agent_event
