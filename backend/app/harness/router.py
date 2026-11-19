"""LLM 路由器（M4-AI01c 步 2；决策卡 2-A/D2/D3）。

route_v1 分类 prompt → JSON `intents[]` → `RouteResult`。**失败降级不报错**
（D2）：JSON 解析失败/意图越集/LLM 异常一律落「单 kb_qa 意图 simple=true」+
`degraded` 标记——用户始终有回答（快路径）。

simple 判定由路由器计算（不信模型自报）：单意图且为 kb_qa → 快路径；其余
（多意图/navigate）→ ReAct。
"""

import json
import logging
import re
from dataclasses import dataclass

from app.harness.contracts import Intent, RouteResult
from app.harness.llm import LlmError, LlmMessage, LlmPort, LlmRequest
from app.harness.prompts import PromptRegistry

logger = logging.getLogger(__name__)

PROMPT_NAME = "route"
PROMPT_VERSION = "v1"

KNOWN_INTENTS: tuple[str, ...] = ("kb_qa", "navigate")
_DEFAULT_CONFIDENCE = 0.5
_JSON_PATTERN = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class RouterOutcome:
    """路由结果：`RouteResult` + 分派标记（simple/degraded）。"""

    route: RouteResult
    simple: bool
    degraded: bool = False


class LlmRouter:
    """LLM 路由器（use-site 构造；端口注入对齐既有纪律）。"""

    def __init__(self, *, llm_port: LlmPort | None, prompt_registry: PromptRegistry) -> None:
        self._llm_port = llm_port
        self._registry = prompt_registry

    async def route(self, message: str) -> RouterOutcome:
        """分类用户消息；任何失败落 degraded simple 兜底（不抛）。"""

        prompt = self._registry.render(PROMPT_NAME, PROMPT_VERSION, message=message)
        if self._llm_port is None:
            # LLM 未配置：路由直接降级单意图（D2——用户始终有回答）
            return self._fallback(message, degraded=True)
        try:
            response = await self._llm_port.complete(
                LlmRequest(messages=[LlmMessage(role="user", content=prompt)])
            )
        except LlmError as exc:
            logger.warning("路由 LLM 失败（降级单意图）：%s", exc.code.value)
            return self._fallback(message, degraded=True)
        outcome = self._parse(response.text, fallback_message=message)
        if outcome is None:
            return self._fallback(message, degraded=True)
        return outcome

    def _parse(self, text: str, *, fallback_message: str) -> RouterOutcome | None:
        """解析模型 JSON；任何结构问题返回 None（调用方降级）。"""

        match = _JSON_PATTERN.search(text)
        if match is None:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        raw_intents = payload.get("intents") if isinstance(payload, dict) else None
        if not isinstance(raw_intents, list) or not raw_intents:
            return None
        intents: list[Intent] = []
        for item in raw_intents:
            if not isinstance(item, dict):
                return None
            name = item.get("name")
            query = item.get("query")
            if not isinstance(name, str) or not name:
                return None
            if name not in KNOWN_INTENTS:
                # D3：未知意图名按 kb_qa 处理（诚实降级，不虚构能力）
                name = "kb_qa"
            confidence = item.get("confidence")
            if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                confidence = _DEFAULT_CONFIDENCE
            intents.append(
                Intent(
                    name=name,
                    confidence=float(confidence),
                    args={"query": query if isinstance(query, str) and query else fallback_message},
                )
            )
        if not intents:
            return None
        simple = len(intents) == 1 and intents[0].name == "kb_qa"
        primary = intents[0].name
        return RouterOutcome(
            route=RouteResult(intents=intents, primary_capability=primary),
            simple=simple,
        )

    def _fallback(self, message: str, *, degraded: bool) -> RouterOutcome:
        """单 kb_qa 意图兜底（query=原始消息）。"""

        return RouterOutcome(
            route=RouteResult(
                intents=[
                    Intent(
                        name="kb_qa",
                        confidence=_DEFAULT_CONFIDENCE,
                        args={"query": message},
                    )
                ],
                primary_capability="kb_qa",
            ),
            simple=True,
            degraded=degraded,
        )
