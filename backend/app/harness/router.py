"""LLM 路由器（M4-AI01c；TD-361 v3：语义范围路由）。

route_v3 用一次模型调用同时返回租户范围结论与本校意图。范围结论只有稳定枚举；
混合请求只可返回原消息中逐字存在的 safety fragments。JSON/字段冲突时退化为
旧式单 kb_qa 降级，由编排器在存在外校证据时 fail-closed。
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum

from app.harness.contracts import Intent, LlmCallUsage, RouteResult
from app.harness.llm import LlmError, LlmMessage, LlmPort, LlmRequest, LlmResponse
from app.harness.prompts import PromptRegistry

logger = logging.getLogger(__name__)

PROMPT_NAME = "route"
PROMPT_VERSION = "v3"

KNOWN_INTENTS: tuple[str, ...] = ("kb_qa", "navigate")
_DEFAULT_CONFIDENCE = 0.5
_JSON_PATTERN = re.compile(r"\{.*\}", re.DOTALL)
_V3_RESPONSE_FIELDS = frozenset({"scope", "blocked_count", "safe_fragments", "intents"})
_INTENT_FIELDS = frozenset({"name", "query", "confidence"})


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """拒绝 JSON 对象重复键，避免“后者覆盖前者”制造冲突结论。"""

    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


class ScopeDecision(StrEnum):
    """route_v3 的租户范围结论。"""

    ALLOW = "allow"
    BLOCK = "block"
    MIXED = "mixed"
    RESTRICTION = "restriction"
    IDENTITY_CLAIM = "identity_claim"


@dataclass(frozen=True)
class RouterOutcome:
    """范围结论与本校区路由结果。

    ``scope is None`` 只有两种合法解释：``unavailable=False`` 是回滚期间完整的
    intent-only 旧响应；``unavailable=True`` 是超时、非法 JSON 或 schema 冲突。
    后者不能把原消息交给检索链。
    """

    route: RouteResult | None
    simple: bool
    degraded: bool = False
    unavailable: bool = False
    scope: ScopeDecision | None = None
    safe_fragments: tuple[str, ...] = ()
    blocked_count: int = 0
    usage: LlmCallUsage | None = None

    @property
    def terminal_scope(self) -> bool:
        return self.scope in {
            ScopeDecision.BLOCK,
            ScopeDecision.RESTRICTION,
            ScopeDecision.IDENTITY_CLAIM,
        }

    @property
    def scope_unavailable(self) -> bool:
        """是否因路由故障而没有可信范围结论。"""

        return self.scope is None and self.unavailable


class LlmRouter:
    """scope-aware LLM 路由器（use-site 构造；端口注入对齐既有纪律）。"""

    def __init__(
        self,
        *,
        llm_port: LlmPort | None,
        prompt_registry: PromptRegistry,
        prompt_version: str = PROMPT_VERSION,
    ) -> None:
        self._llm_port = llm_port
        self._registry = prompt_registry
        self._prompt_version = prompt_version

    async def route(self, message: str) -> RouterOutcome:
        """分类用户消息；网络/结构失败不抛，保留降级与用量标记。"""

        spec = self._registry.get(PROMPT_NAME, self._prompt_version)
        prompt = self._registry.render(
            PROMPT_NAME,
            self._prompt_version,
            message=message,
        )
        if self._llm_port is None:
            return self._fallback(message, degraded=True)
        started = time.monotonic()
        try:
            response = await self._llm_port.complete(
                LlmRequest(
                    messages=[LlmMessage(role="user", content=prompt)],
                    temperature=0,
                    reasoning_effort="none",
                )
            )
        except LlmError as exc:
            logger.warning("路由 LLM 失败（降级单意图）：%s", exc.code.value)
            usage = self._usage(
                spec=spec,
                response=None,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            return self._fallback(message, degraded=True, usage=usage)
        usage = self._usage(
            spec=spec,
            response=response,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        outcome = self._parse(
            response.text,
            fallback_message=message,
            require_scope=self._prompt_version == "v3",
            usage=usage,
        )
        if outcome is None:
            return self._fallback(message, degraded=True, usage=usage)
        return outcome

    def _usage(
        self,
        *,
        spec: object,
        response: LlmResponse | None,
        elapsed_ms: int,
    ) -> LlmCallUsage:
        """把本次路由调用的模型与 usage 固化为无正文元数据。"""

        provider = self._llm_port.provider_name if self._llm_port is not None else ""
        configured_model = self._llm_port.model_name if self._llm_port is not None else ""
        response_latency = response.latency_ms if response is not None else 0
        return LlmCallUsage(
            provider=provider,
            model=(response.model if response is not None and response.model else configured_model),
            prompt_name=PROMPT_NAME,
            prompt_version=self._prompt_version,
            prompt_hash=getattr(spec, "sha256", ""),
            tokens_in=int(response.tokens_in or 0) if response is not None else 0,
            tokens_out=int(response.tokens_out or 0) if response is not None else 0,
            latency_ms=response_latency or elapsed_ms,
        )

    def _parse(
        self,
        text: str,
        *,
        fallback_message: str,
        require_scope: bool,
        usage: LlmCallUsage,
    ) -> RouterOutcome | None:
        """解析范围结论与意图；缺字段、重复键或任何结构冲突返回 None。"""

        match = _JSON_PATTERN.search(text)
        if match is None:
            return None
        try:
            payload = json.loads(
                match.group(0),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None

        raw_scope = payload.get("scope")
        scope: ScopeDecision | None
        legacy_response = raw_scope is None and not require_scope
        if raw_scope is None and require_scope:
            return None
        if legacy_response:
            # 只有显式选择 route_v1/v2 的回滚实例才接受 intent-only 响应。
            if set(payload) != {"intents"}:
                return None
            scope = None
        elif isinstance(raw_scope, str):
            if set(payload) != _V3_RESPONSE_FIELDS:
                return None
            try:
                scope = ScopeDecision(raw_scope)
            except ValueError:
                return None
        else:
            return None

        raw_fragments = payload.get("safe_fragments", [])
        if not isinstance(raw_fragments, list) or any(
            not isinstance(item, str) or not item or item not in fallback_message
            for item in raw_fragments
        ):
            return None
        safe_fragments = tuple(raw_fragments)

        raw_blocked_count = payload.get("blocked_count", 1 if scope in {
            ScopeDecision.BLOCK,
            ScopeDecision.MIXED,
        } else 0)
        if (
            not isinstance(raw_blocked_count, int)
            or isinstance(raw_blocked_count, bool)
            or raw_blocked_count < 0
            or (scope in {ScopeDecision.BLOCK, ScopeDecision.MIXED} and raw_blocked_count < 1)
            or (scope not in {ScopeDecision.BLOCK, ScopeDecision.MIXED} and raw_blocked_count != 0)
        ):
            return None

        raw_intents = payload.get("intents", [])
        if not isinstance(raw_intents, list):
            return None
        if scope in {
            ScopeDecision.BLOCK,
            ScopeDecision.RESTRICTION,
            ScopeDecision.IDENTITY_CLAIM,
        }:
            if raw_intents or safe_fragments:
                return None
            return RouterOutcome(
                route=None,
                simple=False,
                scope=scope,
                blocked_count=raw_blocked_count,
                usage=usage,
            )
        if scope is ScopeDecision.MIXED and not safe_fragments:
            return None
        if scope is ScopeDecision.ALLOW and safe_fragments:
            return None
        if not raw_intents:
            return None

        intents: list[Intent] = []
        for item in raw_intents:
            if not isinstance(item, dict):
                return None
            fields = set(item)
            if legacy_response:
                if not {"name", "query"} <= fields <= _INTENT_FIELDS:
                    return None
            elif fields != _INTENT_FIELDS:
                return None
            name = item.get("name")
            query = item.get("query")
            if not isinstance(name, str) or name not in KNOWN_INTENTS:
                return None
            if not isinstance(query, str) or not query or query not in fallback_message:
                return None
            confidence = item.get("confidence", _DEFAULT_CONFIDENCE if legacy_response else None)
            if (
                not isinstance(confidence, (int, float))
                or isinstance(confidence, bool)
                or not 0 <= confidence <= 1
            ):
                return None
            intents.append(
                Intent(
                    name=name,
                    confidence=float(confidence),
                    args={"query": query},
                )
            )
        if not intents:
            return None
        if scope is ScopeDecision.MIXED and any(
            not any(intent.args["query"] in fragment for fragment in safe_fragments)
            for intent in intents
        ):
            return None
        simple = len(intents) == 1 and intents[0].name == "kb_qa"
        primary = intents[0].name
        return RouterOutcome(
            route=RouteResult(intents=intents, primary_capability=primary),
            simple=simple,
            scope=scope,
            safe_fragments=safe_fragments,
            blocked_count=raw_blocked_count,
            usage=usage,
        )

    def _fallback(
        self,
        message: str,
        *,
        degraded: bool,
        usage: LlmCallUsage | None = None,
    ) -> RouterOutcome:
        """单 kb_qa 路由兜底；scope=None 由调用方按证据决定是否放行。"""

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
            unavailable=degraded,
            usage=usage,
        )
