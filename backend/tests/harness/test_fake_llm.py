"""FakeLlm 脚本化行为校验（M4-AI01a 步 2）。

后续 M4 各点的测试写法都建立在本假端口上：脚本顺序、耗尽响亮失败、
流中断构造、协议符合性。
"""

import pytest

from app.harness.llm import (
    LlmError,
    LlmErrorCode,
    LlmPort,
    LlmRequest,
    LlmResponse,
    LlmStreamEvent,
)
from tests.support.llm import FakeLlm


def request() -> LlmRequest:
    """构造最小请求。"""

    return LlmRequest(messages=[{"role": "user", "content": "你好"}])


async def test_answer_construct_returns_single_response() -> None:
    """最简构造：complete 返回带 finish/usage 的固定回答。"""

    fake = FakeLlm.answer("校园规定如下")
    response = await fake.complete(request())
    assert response.text == "校园规定如下"
    assert response.finish_reason == "stop"
    assert response.tokens_in == 12
    assert response.tokens_out == 34
    assert len(fake.requests) == 1


async def test_complete_scripts_pop_in_order() -> None:
    """脚本按调用顺序弹出：先成功后失败。"""

    scripted = LlmResponse(text="第一轮")
    fake = FakeLlm(complete_scripts=[scripted, LlmError(LlmErrorCode.TIMEOUT)])
    assert await fake.complete(request()) is scripted
    with pytest.raises(LlmError):
        await fake.complete(request())


async def test_complete_script_exhaustion_fails_loudly() -> None:
    """脚本耗尽抛 RuntimeError——测试假绿（静默复用）在此被掐断。"""

    fake = FakeLlm(complete_scripts=[LlmResponse(text="仅一条")])
    await fake.complete(request())
    with pytest.raises(RuntimeError, match="脚本耗尽"):
        await fake.complete(request())


async def test_stream_scripts_yield_events_in_order() -> None:
    """流脚本按序产出 delta/finish/usage。"""

    fake = FakeLlm(
        stream_scripts=[
            [
                LlmStreamEvent(kind="delta", text_delta="你好"),
                LlmStreamEvent(kind="finish", finish_reason="stop"),
                LlmStreamEvent(kind="usage", tokens_in=3, tokens_out=2),
            ]
        ]
    )
    events = [event async for event in fake.stream(request())]
    assert [event.kind for event in events] == ["delta", "finish", "usage"]


async def test_stream_script_midstream_failure_after_deltas() -> None:
    """流中断构造：迭代中先产出 delta、随后抛异常（对齐适配器「流中断不重试」用例）。"""

    failure = LlmError(LlmErrorCode.PROVIDER_ERROR)
    fake = FakeLlm(
        stream_scripts=[
            [LlmStreamEvent(kind="delta", text_delta="部分"), failure],
        ]
    )
    collected: list[LlmStreamEvent] = []
    with pytest.raises(LlmError) as excinfo:
        async for event in fake.stream(request()):
            collected.append(event)
    assert [event.text_delta for event in collected] == ["部分"]
    assert excinfo.value is failure


def test_fake_llm_satisfies_llm_port_protocol() -> None:
    """FakeLlm 与真实适配器同构满足 LlmPort（runtime_checkable）。"""

    assert isinstance(FakeLlm.answer(), LlmPort)
