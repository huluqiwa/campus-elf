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
from uuid import UUID, uuid4

from app.harness.contracts import AgentEvent, AgentEventType
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
    ) -> None:
        self._router = router
        self._fast_loop = fast_loop
        self._react_loop = react_loop

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
