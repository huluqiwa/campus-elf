"""LLM 路由器校验（M4-AI01c 步 2；决策卡 2-A/D2/D3）。"""

import json
from typing import Any

from app.harness.llm import LlmError, LlmErrorCode, LlmResponse
from app.harness.prompt_loading import build_default_registry
from app.harness.router import LlmRouter
from tests.support.llm import FakeLlm


def router_with(complete_result: Any) -> tuple[LlmRouter, FakeLlm]:
    """构造被测路由器（FakeLlm 脚本化 complete）。"""

    fake = FakeLlm(complete_scripts=[complete_result])
    return LlmRouter(llm_port=fake, prompt_registry=build_default_registry()), fake


async def test_route_parses_two_intents_not_simple() -> None:
    """合法 JSON 双意图：逐条转 Intent；simple=False；query 保留。"""

    router, fake = router_with(
        LlmResponse(
            text=json.dumps(
                {
                    "intents": [
                        {"name": "kb_qa", "query": "差旅报销怎么办理？"},
                        {"name": "navigate", "query": "请假入口在哪？"},
                    ]
                },
                ensure_ascii=False,
            ),
            finish_reason="stop",
        )
    )
    outcome = await router.route("差旅报销怎么办理？另外请假入口在哪？")

    assert outcome.simple is False
    assert outcome.degraded is False
    assert [intent.name for intent in outcome.route.intents] == ["kb_qa", "navigate"]
    assert outcome.route.intents[0].args["query"] == "差旅报销怎么办理？"
    assert outcome.route.primary_capability == "kb_qa"
    # 路由 prompt 走 route_v1 模板
    assert fake.requests[0].messages[0].content.startswith("你是")


async def test_route_single_kb_qa_is_simple() -> None:
    """单 kb_qa 意图：simple=True（分派快路径）。"""

    router, _ = router_with(
        LlmResponse(text=json.dumps({"intents": [{"name": "kb_qa", "query": "校历"}]}))
    )
    outcome = await router.route("校历是什么？")
    assert outcome.simple is True
    assert len(outcome.route.intents) == 1


async def test_route_unknown_intent_falls_back_to_kb_qa() -> None:
    """未知意图名（D3）：按 kb_qa 处理（诚实降级）。"""

    router, _ = router_with(
        LlmResponse(text=json.dumps({"intents": [{"name": "/weather", "query": "天气"}]}))
    )
    outcome = await router.route("今天天气")
    assert outcome.simple is True
    assert outcome.route.intents[0].name == "kb_qa"


async def test_route_invalid_json_falls_back_degraded() -> None:
    """非法 JSON（D2）：单意图 simple 兜底 + degraded 标记。"""

    router, _ = router_with(LlmResponse(text="我不是 JSON"))
    outcome = await router.route("校历是什么？")
    assert outcome.simple is True
    assert outcome.degraded is True
    assert outcome.route.intents[0].name == "kb_qa"
    assert outcome.route.intents[0].args["query"] == "校历是什么？"


async def test_route_llm_error_falls_back_degraded() -> None:
    """路由 LLM 异常（D2）：降级 simple，不向调用方抛。"""

    router, _ = router_with(LlmError(LlmErrorCode.TIMEOUT, retryable=True))
    outcome = await router.route("校历是什么？")
    assert outcome.simple is True
    assert outcome.degraded is True


async def test_route_empty_intents_falls_back_degraded() -> None:
    """空 intents 列表：视为解析失败降级。"""

    router, _ = router_with(LlmResponse(text=json.dumps({"intents": []})))
    outcome = await router.route("校历是什么？")
    assert outcome.simple is True
    assert outcome.degraded is True


async def test_route_confidence_defaults_when_absent() -> None:
    """模型未报 confidence：默认 0.5（不伪造更高值）。"""

    router, _ = router_with(
        LlmResponse(text=json.dumps({"intents": [{"name": "kb_qa", "query": "校历"}]}))
    )
    outcome = await router.route("校历是什么？")
    assert outcome.route.intents[0].confidence == 0.5


async def test_route_llm_none_falls_back_degraded() -> None:
    """llm_port=None（LLM 未配置）：路由直接降级 simple（D2 同构弧，审查 m3）。"""

    router = LlmRouter(llm_port=None, prompt_registry=build_default_registry())
    outcome = await router.route("校历是什么？")
    assert outcome.simple is True
    assert outcome.degraded is True
    assert outcome.route.intents[0].name == "kb_qa"
