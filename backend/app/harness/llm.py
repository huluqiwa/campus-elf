"""LLM 调用端口（M4-AI01a）。

Harness 与具体 provider 之间的稳定边界：上层（Loop/护栏/引用校验）只依赖
``LlmPort`` 协议与类型化契约，不感知任何厂商 SDK。

红线（简报 §1.2-A3/A4）：

- API key 只从 ``Settings``（SecretStr）注入；不入日志/审计/trace/异常文本。
- provider 异常映射为内部错误码，文本一律不透传（``LlmError`` 构造器没有
  任意文本形参，杜绝透传通道）；异常链不携带原始 provider 异常——
  ``raise ... from None`` 只清显示与 ``__cause__``，``__context__`` 由
  ``_raise_sanitized`` 在 finally 中显式摘除（其 request/response 链含 key）。

超时/重试/熔断（A8 映射，简报 §7）：连接 5s / 首 token 15s；首 token 前 ≤2 次
重试（1 次初始 + 2 次重试）；流中断不重试；熔断 5 次/60s 开 30s。回合级 30s
总预算由 Loop 层承担，端口只管单次调用（简报 D4）。
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import BaseModel, ConfigDict, Field, SecretStr

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)


class LlmErrorCode(StrEnum):
    """LLM 调用失败的内部错误码（对 design §5.8 error 事件的 code 字段）。"""

    NOT_CONFIGURED = "LLM_NOT_CONFIGURED"
    AUTH = "LLM_AUTH"
    TIMEOUT = "LLM_TIMEOUT"
    BREAKER_OPEN = "LLM_BREAKER_OPEN"
    RATE_LIMITED = "LLM_RATE_LIMITED"
    PROVIDER_ERROR = "LLM_PROVIDER_ERROR"


# 每个错误码的固定安全话术：客户端可见文本从这里取，杜绝拼接 provider 原文。
_SAFE_MESSAGES: dict[LlmErrorCode, str] = {
    LlmErrorCode.NOT_CONFIGURED: "AI 能力未配置，请联系管理员",
    LlmErrorCode.AUTH: "AI 服务鉴权失败，请联系管理员",
    LlmErrorCode.TIMEOUT: "回答生成超时，请重试",
    LlmErrorCode.BREAKER_OPEN: "AI 服务暂时不可用，请稍后重试",
    LlmErrorCode.RATE_LIMITED: "提问过于频繁，请稍后重试",
    LlmErrorCode.PROVIDER_ERROR: "回答生成失败，请重试",
}


class LlmMessage(BaseModel):
    """单条对话消息（MVP 纯文本；多模态 part 归 VLM 点扩展）。"""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str


class LlmRequest(BaseModel):
    """一次 LLM 调用的输入。

    ``tool_specs`` 是 OpenAI ``tools`` 参数形态的只读透传（本点不解析，
    ReAct 点落地时以双向用例钉住拼装语义，见简报 TD-P3）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: list[LlmMessage] = Field(min_length=1)
    model: str | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    tool_specs: tuple[dict[str, Any], ...] = ()
    # 只允许收紧超时（Loop 层做回合级预算时使用）；0/负数会放大超时，直接拒绝。
    timeout_override_s: float | None = Field(default=None, gt=0)


class LlmResponse(BaseModel):
    """非流式调用结果。

    usage 缺失时保持 ``None``（不伪造，与 A7 红线第 2 类同源的如实口径）。
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    finish_reason: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    model: str = ""
    latency_ms: int = Field(default=0, ge=0)
    tool_calls: tuple[dict[str, Any], ...] = ()


class LlmStreamEvent(BaseModel):
    """流式事件（delta 正文片段 / finish 结束原因 / usage 用量报数）。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["delta", "finish", "usage"]
    text_delta: str | None = None
    finish_reason: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None


class LlmError(Exception):
    """LLM 调用失败。

    构造器只接受错误码与可重试标记：话术由 ``_SAFE_MESSAGES`` 决定，
    provider 原文没有进入本异常的通道（签名级保证，测试钉住）。
    """

    def __init__(self, code: LlmErrorCode, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(f"{code.value}: {_SAFE_MESSAGES[code]}")


@runtime_checkable
class LlmPort(Protocol):
    """LLM 调用端口协议（M4 各功能点只依赖此契约与 FakeLlm）。"""

    async def complete(self, request: LlmRequest) -> LlmResponse:
        """非流式调用（ReAct 决策/路由/摘要等中间步骤用）。"""
        ...

    def stream(self, request: LlmRequest) -> AsyncIterator[LlmStreamEvent]:
        """流式调用（最终回答生成用）；失败语义见模块 docstring。"""
        ...


class LlmCircuitBreaker:
    """进程内熔断器（A8：窗口内失败达阈值开路，开路期满半开试探 1 次）。

    - closed：滑动窗口计数失败；达 ``failure_threshold`` → open。
    - open：``before_call`` 直接抛 ``LLM_BREAKER_OPEN``（不发起网络请求）；
      ``open_seconds`` 过后放行**一次**试探（half_open 单飞）。
    - half_open：试探成功 → closed（失败清零）；试探失败 → open（窗口重新计起）；
      试探被放弃（取消/中断）→ ``release_trial()`` 回收名额，开路保持（第三条出口弧）。

    已知边界（简报 TD-P1）：进程内实现，多 worker 各自计数；check-and-mark 均
    在无 await 的同步段完成，单事件循环内原子。
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        window_seconds: float = 60.0,
        open_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """参数与注入时钟（测试不 sleep 真实时间）。"""

        self._failure_threshold = failure_threshold
        self._window_seconds = window_seconds
        self._open_seconds = open_seconds
        self._clock = clock
        self._failures: list[float] = []
        self._opened_at: float | None = None
        self._trial_in_flight = False

    @property
    def state(self) -> Literal["closed", "open", "half_open"]:
        """当前状态（开路期满但试探未发生时仍报 open——试探动作发生在 before_call）。"""

        if self._opened_at is None:
            return "closed"
        if self._trial_in_flight:
            return "half_open"
        return "open"

    def before_call(self) -> None:
        """调用前置检查；开路抛 ``LLM_BREAKER_OPEN``，半开放行并占用试探名额。"""

        if self._opened_at is not None:
            elapsed = self._clock() - self._opened_at
            if elapsed < self._open_seconds:
                raise LlmError(LlmErrorCode.BREAKER_OPEN, retryable=True)
            if self._trial_in_flight:
                raise LlmError(LlmErrorCode.BREAKER_OPEN, retryable=True)
            # 开路期满且无试探在飞：放行一次试探并立即占位（同步段内完成，单飞原子）。
            self._trial_in_flight = True
            return
        # closed：滑动窗口清理过期失败
        now = self._clock()
        self._failures = [ts for ts in self._failures if now - ts < self._window_seconds]

    def record_success(self) -> None:
        """记录成功：清空失败计数、释放试探名额、闭合。"""

        self._failures.clear()
        self._opened_at = None
        self._trial_in_flight = False

    def record_failure(self) -> None:
        """记录失败：closed 态滑动窗口计数达阈值即开路；半开试探失败重新开路。"""

        now = self._clock()
        if self._opened_at is not None:
            # 半开试探失败：重新开路，窗口从现在重新计起。
            self._trial_in_flight = False
            self._opened_at = now
            return
        self._failures = [ts for ts in self._failures if now - ts < self._window_seconds]
        self._failures.append(now)
        if len(self._failures) >= self._failure_threshold:
            self._opened_at = now
            self._failures.clear()

    def release_trial(self) -> None:
        """释放未结算的半开试探名额（调用被取消/消费方中断等放弃路径）。

        试探被放弃不代表 provider 健康或不健康：不记失败、不改开路状态；
        名额回收后下一次 ``before_call`` 在开路期满仍可再授予试探。
        若不释放，``_trial_in_flight`` 将永久为真——所有后续调用恒被
        ``LLM_BREAKER_OPEN`` 拒绝直到进程重启（审查 M1）。
        """

        self._trial_in_flight = False


def _classify_exception(exc: Exception) -> tuple[LlmErrorCode, bool]:
    """provider/SDK 异常 → 内部错误码与可重试标记（A8 口径）。

    返回值:
        (错误码, 可重试)。429/5xx/连接与超时类可重试；401/403/其他 4xx 不重试。

    注意:
        只读异常的**类型与状态码**，绝不读取异常文本（防 provider 原文出圈）。
    """

    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return LlmErrorCode.AUTH, False
    if isinstance(exc, RateLimitError):
        return LlmErrorCode.RATE_LIMITED, True
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", 0)
        return LlmErrorCode.PROVIDER_ERROR, isinstance(status, int) and status >= 500
    if isinstance(exc, (APITimeoutError, httpx.TimeoutException)):
        return LlmErrorCode.TIMEOUT, True
    if isinstance(exc, APIConnectionError):
        # 连接类失败通常是瞬态（DNS/对端重置），按可重试处理。
        return LlmErrorCode.PROVIDER_ERROR, True
    return LlmErrorCode.PROVIDER_ERROR, False


def _provider_status(exc: Exception) -> int | None:
    """提取 HTTP 状态码（仅数字；日志脱敏口径的合法维度）。"""

    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _raise_sanitized(err: LlmError) -> None:
    """抛出 LlmError 并确保异常链不滞留 provider 异常（审查 M2）。

    ``raise ... from None`` 只抑制 traceback 显示并清 ``__cause__``；
    ``__context__`` 仍指向原始异常——openai SDK 的异常对象持有
    ``.request``/``.response``（headers 含 Authorization key 与 provider 原文）。
    finally 里显式摘除，保证向上传播的 LlmError 不随身携带密钥。
    """

    try:
        raise err from None
    finally:
        err.__context__ = None


class OpenAICompatLlm:
    """OpenAI 兼容适配器（``LlmPort`` 实现）。

    超时/重试/熔断（A8 映射，简报 §7/D4）：

    - 单次调用：连接 ``connect_timeout_s``（默认 5s）/ 读 ``first_token_timeout_s``
      （默认 15s）；回合级 30s 总预算由 Loop 层承担。
    - 重试：首 token 前 ≤ ``max_attempts - 1`` 次（超时/429/5xx/连接类，指数退避）；
      401/403 与其他 4xx 不重试；**流中断（已产出 delta）一律不重试**。
    - 熔断：共享 ``LlmCircuitBreaker``；开路时不发起网络请求。
    - SDK 内建重试必须关闭（``max_retries=0``），否则重试计数与熔断记账失真。

    红线：key 不进日志/异常；异常链 ``from None``；日志只含错误码/状态码/类名。
    """

    def __init__(
        self,
        *,
        api_key: SecretStr,
        base_url: str,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        client: AsyncOpenAI | None = None,
        breaker: LlmCircuitBreaker | None = None,
        connect_timeout_s: float = 5.0,
        first_token_timeout_s: float = 15.0,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """构造适配器；``client`` 供测试注入 MockTransport 版 SDK 客户端。"""

        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        # SDK 内建重试必须关闭：重试语义（次数/退避/熔断记账）由本适配器统一承担。
        self._client = client if client is not None else AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=base_url,
            max_retries=0,
        )
        self._breaker = breaker if breaker is not None else LlmCircuitBreaker()
        self._connect_timeout_s = connect_timeout_s
        self._first_token_timeout_s = first_token_timeout_s
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._sleep = sleep

    def _timeout(self, request: LlmRequest) -> httpx.Timeout:
        """单次调用超时；``timeout_override_s`` 只允许收紧（简报 D4）。"""

        connect = self._connect_timeout_s
        read = self._first_token_timeout_s
        override = request.timeout_override_s
        if override is not None:
            connect = min(connect, override)
            read = min(read, override)
        return httpx.Timeout(connect=connect, read=read, write=10.0, pool=5.0)

    def _payload(self, request: LlmRequest, *, stream: bool) -> dict[str, Any]:
        """构造 SDK 调用参数（tool_specs 为 OpenAI tools 形态透传，TD-P3）。"""

        payload: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "timeout": self._timeout(request),
        }
        temperature = request.temperature if request.temperature is not None else self._temperature
        if temperature is not None:
            payload["temperature"] = temperature
        max_tokens = request.max_tokens if request.max_tokens is not None else self._max_tokens
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if request.tool_specs:
            payload["tools"] = list(request.tool_specs)
        if stream:
            payload["stream"] = True
            # D3：usage 取 provider 报数（末 chunk 携带），不做本地估算。
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _log_failure(self, exc: Exception, code: LlmErrorCode, attempt: int) -> None:
        """失败日志：只含内部错误码/异常类名/HTTP 状态码（无 key、无 provider 原文）。"""

        logger.warning(
            "LLM 调用失败 code=%s provider_class=%s status=%s attempt=%s/%s",
            code.value,
            type(exc).__name__,
            _provider_status(exc),
            attempt + 1,
            self._max_attempts,
        )

    async def complete(self, request: LlmRequest) -> LlmResponse:
        """非流式调用；可重试失败按指数退避，直至成功或尝试耗尽。"""

        started = time.monotonic()
        attempt = 0
        while True:
            self._breaker.before_call()
            try:
                response = await self._client.chat.completions.create(
                    **self._payload(request, stream=False)
                )
            except LlmError:
                # 前提（复审 m3）：当前 try 内无 LlmError 抛点（before_call 在 try 外、
                # SDK 不抛内部类型）；若未来引入，名额已由 record_failure 结算——
                # 这里仍做幂等释放，防 M1 类名额泄漏复活。
                self._breaker.release_trial()
                raise
            except Exception as exc:  # noqa: BLE001——统一映射为内部错误码
                code, retryable = _classify_exception(exc)
                self._breaker.record_failure()
                self._log_failure(exc, code, attempt)
                if retryable and attempt + 1 < self._max_attempts:
                    await self._sleep(self._retry_backoff_seconds * (2**attempt))
                    attempt += 1
                    continue
                _raise_sanitized(LlmError(code, retryable=retryable))
            except BaseException:
                # 取消/中断（CancelledError 等 BaseException）：试探名额必须释放
                # （审查 M1），否则半开名额永久泄漏 → 熔断卡死到进程重启。
                # 取消不是 provider 失败：不记失败、不改开路状态。
                self._breaker.release_trial()
                raise
            self._breaker.record_success()
            choices = response.choices
            message = choices[0].message if choices else None
            usage = response.usage
            tool_calls = (
                tuple(call.model_dump() for call in message.tool_calls)
                if message is not None and message.tool_calls
                else ()
            )
            return LlmResponse(
                text=message.content if message is not None and message.content else "",
                finish_reason=choices[0].finish_reason if choices else None,
                tokens_in=usage.prompt_tokens if usage is not None else None,
                tokens_out=usage.completion_tokens if usage is not None else None,
                model=response.model or "",
                latency_ms=int((time.monotonic() - started) * 1000),
                tool_calls=tool_calls,
            )

    async def stream(self, request: LlmRequest) -> AsyncIterator[LlmStreamEvent]:
        """流式调用：首 token 前失败可重试；已产出 delta 后失败一律不重试。

        语义注记（审查 m2，Loop 层消费须幂等）：``yielded`` 只在 delta 产出后
        置位——若 provider 已发 finish/usage 但尚未发 delta 时流断且重试成功，
        消费方会看到两次 finish/usage；Loop 层以后到的 finish 为准。
        """

        yielded = False
        attempt = 0
        while True:
            self._breaker.before_call()
            try:
                sdk_stream = await self._client.chat.completions.create(
                    **self._payload(request, stream=True)
                )
                async for chunk in sdk_stream:
                    for event in self._map_chunk(chunk):
                        if event.kind == "delta":
                            yielded = True
                        yield event
            except LlmError:
                # 前提与防御同 complete（复审 m3）：幂等释放试探名额。
                self._breaker.release_trial()
                raise
            except Exception as exc:  # noqa: BLE001——统一映射为内部错误码
                code, retryable = _classify_exception(exc)
                self._breaker.record_failure()
                self._log_failure(exc, code, attempt)
                # 流中断（已产出 delta）不重试：上层可能已把 delta 发给客户端。
                if not yielded and retryable and attempt + 1 < self._max_attempts:
                    await self._sleep(self._retry_backoff_seconds * (2**attempt))
                    attempt += 1
                    continue
                _raise_sanitized(LlmError(code, retryable=retryable and not yielded))
            except BaseException:
                # 取消/消费方中断（GeneratorExit 在 yield 点抛出）：释放试探名额
                # （审查 M1）；取消不是 provider 失败，不记失败。
                self._breaker.release_trial()
                raise
            self._breaker.record_success()
            return

    async def aclose(self) -> None:
        """关闭底层 SDK/httpx 客户端（应用 shutdown 钩子调用，审查 m7）。"""

        await self._client.close()

    def _map_chunk(self, chunk: Any) -> list[LlmStreamEvent]:
        """SDK chunk → 事件列表（delta/finish/usage；usage chunk 的 choices 为空）。"""

        events: list[LlmStreamEvent] = []
        choices = getattr(chunk, "choices", None)
        if choices:
            choice = choices[0]
            delta = getattr(choice, "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
            if content:
                events.append(LlmStreamEvent(kind="delta", text_delta=content))
            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason:
                events.append(LlmStreamEvent(kind="finish", finish_reason=finish_reason))
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            events.append(
                LlmStreamEvent(
                    kind="usage",
                    tokens_in=usage.prompt_tokens,
                    tokens_out=usage.completion_tokens,
                )
            )
        return events


def build_llm_port(settings: "Settings") -> OpenAICompatLlm | None:
    """按 Settings 构建 LLM 端口（create_app 缺省装配入口）。

    返回值:
        已配置 → ``OpenAICompatLlm``（进程内熔断器，参数取 A8 默认/覆盖值）；
        未配置（key 缺省/空白，Settings 校验已保证三项成组）→ ``None``，
        由调用方在对话链路上 fail-closed（``LLM_NOT_CONFIGURED`` / 能力兜底话术）。
    """

    key = settings.llm_api_key
    if key is None or not key.get_secret_value().strip():
        return None
    return OpenAICompatLlm(
        api_key=key,
        base_url=settings.llm_base_url or "",
        model=settings.llm_model or "",
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        connect_timeout_s=settings.llm_connect_timeout_seconds,
        first_token_timeout_s=settings.llm_first_token_timeout_seconds,
        max_attempts=settings.llm_max_attempts,
        retry_backoff_seconds=settings.llm_retry_backoff_seconds,
        breaker=LlmCircuitBreaker(
            failure_threshold=settings.llm_breaker_failure_threshold,
            window_seconds=settings.llm_breaker_window_seconds,
            open_seconds=settings.llm_breaker_open_seconds,
        ),
    )
