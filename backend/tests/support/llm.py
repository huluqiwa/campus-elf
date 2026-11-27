"""脚本化 LLM 端口（M4 各功能点通用测试基建）。

按脚本顺序返回/抛出；脚本耗尽抛 RuntimeError（让测试响亮失败，不静默复用
上一条脚本）。请求逐条记录供断言。
"""

import json
from collections.abc import AsyncIterator, Sequence

from app.harness.llm import LlmRequest, LlmResponse, LlmStreamEvent

# TD-052 口径：统一 FAKE_ 前缀的测试假值（不用于任何真实服务）。
FAKE_LLM_API_KEY = "fake-llm-api-key-0123456789"

CompleteScript = LlmResponse | Exception
StreamScriptItem = LlmStreamEvent | Exception


class FakeLlm:
    """``LlmPort`` 的脚本化实现（仅测试用，绝不进生产装配）。"""

    provider_name = "fake"
    model_name = "fake-model"

    def __init__(
        self,
        *,
        complete_scripts: Sequence[CompleteScript] = (),
        stream_scripts: Sequence[Sequence[StreamScriptItem]] = (),
    ) -> None:
        """绑定每类调用的脚本队列（按调用顺序弹出）。"""

        self._complete_scripts: list[CompleteScript] = list(complete_scripts)
        self._stream_scripts: list[list[StreamScriptItem]] = [
            list(script) for script in stream_scripts
        ]
        self.requests: list[LlmRequest] = []

    @classmethod
    def answer(
        cls,
        text: str = "默认回答",
        *,
        tokens_in: int = 12,
        tokens_out: int = 34,
    ) -> "FakeLlm":
        """单轮固定回答的最简构造（大多数用例只需一个正常回答）。"""

        return cls(
            complete_scripts=[
                LlmResponse(
                    text=text,
                    finish_reason="stop",
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
            ]
        )

    async def complete(self, request: LlmRequest) -> LlmResponse:
        """弹出下一条 complete 脚本：Response 返回、Exception 抛出。"""

        self.requests.append(request)
        if not self._complete_scripts:
            raise RuntimeError("FakeLlm complete 脚本耗尽")
        item = self._complete_scripts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def stream(self, request: LlmRequest) -> AsyncIterator[LlmStreamEvent]:
        """弹出下一条 stream 脚本：逐个产出事件，遇到 Exception 即抛（可构造流中断）。"""

        self.requests.append(request)
        if not self._stream_scripts:
            raise RuntimeError("FakeLlm stream 脚本耗尽")
        for item in self._stream_scripts.pop(0):
            if isinstance(item, Exception):
                raise item
            yield item


_ROUTE_V3_PROMPT_MARKER = "你是「校园陪伴精灵」的租户范围语义路由器。"
_ROUTE_MESSAGE_MARKER = "用户消息：\n"


def route_v3_allow_response(message: str) -> LlmResponse:
    """为一条原文消息构造合法的 route_v3 放行响应。"""

    return LlmResponse(
        text=json.dumps(
            {
                "scope": "allow",
                "blocked_count": 0,
                "safe_fragments": [],
                "intents": [
                    {"name": "kb_qa", "query": message, "confidence": 0.9}
                ],
            },
            ensure_ascii=False,
        ),
        finish_reason="stop",
    )


class AllowRouteFakeLlm(FakeLlm):
    """从 Git 版本化 route_v3 prompt 提取用户原文并返回合法放行结论。

    只用于不验证路由器本身的业务链路测试；router 契约的严格失败路径由
    ``tests/harness/test_router.py`` 使用脚本化 FakeLlm 单独覆盖。
    """

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        prompt = request.messages[-1].content
        if prompt.count(_ROUTE_V3_PROMPT_MARKER) != 1:
            raise RuntimeError("AllowRouteFakeLlm 只接受 route_v3 prompt")
        message = prompt.rsplit(_ROUTE_MESSAGE_MARKER, 1)[1].strip()
        if not message:
            raise RuntimeError("route_v3 prompt 缺少用户消息")
        return route_v3_allow_response(message)
