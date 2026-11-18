"""LLM 端口契约校验测试（M4-AI01a 步 1）。

错误码/请求/响应/流事件的字段边界与 `extra="forbid"` 语义；`LlmError` 的
安全话术保证（构造器不接受任意文本，杜绝 provider 原文透传）。
"""

import pytest
from pydantic import ValidationError

from app.harness.llm import (
    LlmError,
    LlmErrorCode,
    LlmMessage,
    LlmPort,
    LlmRequest,
    LlmResponse,
    LlmStreamEvent,
)


def message(role: str = "user", content: str = "你好") -> LlmMessage:
    """构造合法消息。"""

    return LlmMessage(role=role, content=content)


def test_llm_error_code_values() -> None:
    """错误码六分类与 A8/A19/护栏口径一一对应。"""

    assert {code.value for code in LlmErrorCode} == {
        "LLM_NOT_CONFIGURED",
        "LLM_AUTH",
        "LLM_TIMEOUT",
        "LLM_BREAKER_OPEN",
        "LLM_RATE_LIMITED",
        "LLM_PROVIDER_ERROR",
    }


def test_llm_error_carries_safe_message_only() -> None:
    """LlmError 只接受 code/retryable；文本由错误码决定，无透传通道。"""

    error = LlmError(LlmErrorCode.TIMEOUT, retryable=True)
    assert error.code is LlmErrorCode.TIMEOUT
    assert error.retryable is True
    assert "LLM_TIMEOUT" in str(error)
    # 构造器没有 message 形参：provider 文本无处可传（签名级红线）。
    with pytest.raises(TypeError):
        LlmError(LlmErrorCode.TIMEOUT, message="provider raw text")  # type: ignore[call-arg]


def test_llm_error_retryable_defaults_false() -> None:
    """默认不可重试：AUTH/流中断等场景由调用点显式标注。"""

    assert LlmError(LlmErrorCode.AUTH).retryable is False


def test_llm_message_rejects_unknown_role() -> None:
    """role 只允许 system/user/assistant/tool。"""

    with pytest.raises(ValidationError):
        LlmMessage(role="developer", content="x")


def test_llm_message_rejects_extra_fields() -> None:
    """extra=forbid：拼写错误不得被静默忽略（对齐 harness 契约口径）。

    用 ``model_validate`` 驱动：多出的字段本就是运行时边界（静态参数检查管不到）。
    """

    with pytest.raises(ValidationError):
        LlmMessage.model_validate({"role": "user", "content": "x", "metadata": {"a": 1}})


def test_llm_request_requires_at_least_one_message() -> None:
    """请求至少一条消息。"""

    with pytest.raises(ValidationError):
        LlmRequest(messages=[])


def test_llm_request_rejects_non_positive_timeout_override() -> None:
    """超时覆盖必须为正数（只允许收紧，见简报 D4）。"""

    with pytest.raises(ValidationError):
        LlmRequest(messages=[message()], timeout_override_s=0)


def test_llm_request_tool_specs_passthrough() -> None:
    """tool_specs 为 OpenAI tools 形态的只读透传（本点不解析，TD-P3）。"""

    spec = {"type": "function", "function": {"name": "kb.search", "parameters": {}}}
    request = LlmRequest(messages=[message()], tool_specs=(spec,))
    assert request.tool_specs == (spec,)


def test_llm_response_defaults_do_not_fabricate_usage() -> None:
    """provider 未报 usage 时保持 None（不伪造，A7 红线第 2 类同源纪律）。"""

    response = LlmResponse(text="回答")
    assert response.text == "回答"
    assert response.finish_reason is None
    assert response.tokens_in is None
    assert response.tokens_out is None
    assert response.tool_calls == ()


def test_llm_stream_event_kind_is_closed_set() -> None:
    """流事件只有 delta/finish/usage 三类。"""

    with pytest.raises(ValidationError):
        LlmStreamEvent(kind="step")


def test_llm_port_is_runtime_checkable_protocol() -> None:
    """LlmPort 为运行时可检协议：具备 complete/stream 两个调用面即视为实现。"""

    class _Minimal:
        async def complete(self, request: LlmRequest) -> LlmResponse:
            return LlmResponse(text="ok")

        def stream(self, request: LlmRequest) -> object:
            return iter(())  # 形态占位：真实实现返回 AsyncIterator

    assert isinstance(_Minimal(), LlmPort)
