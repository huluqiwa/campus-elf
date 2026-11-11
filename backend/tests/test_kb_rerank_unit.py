"""KB-07A rerank 端口与 TEI 适配器单元测试（简报 `docs/design-briefs/M3-KB-07.md` §4 第 1 步）。

覆盖：请求体/URL 构造、响应按 index 映射回文本顺序（乱序/缺项/越界/类型错）、
超时单独成异常（§7.5「超时跳过」降级语义的判定依据）、空候选不发请求。
"""

import json
from collections.abc import Callable

import httpx
import pytest

from app.kb.rerank import RerankError, RerankTimeoutError, TEIReranker


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://tei")


def _reranker(handler: Callable[[httpx.Request], httpx.Response]) -> TEIReranker:
    return TEIReranker(
        base_url="http://tei",
        timeout_seconds=5,
        batch_size=10,  # 单批装下：除分批专项用例外都不触发分批
        client=_client(handler),
    )


async def test_rerank_posts_query_and_texts_and_maps_scores_by_index() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.read().decode("utf-8"))
        # TEI 按 score 降序返回，index 指回 texts 下标
        return httpx.Response(
            200,
            json=[
                {"index": 2, "score": 0.9},
                {"index": 0, "score": 0.5},
                {"index": 1, "score": 0.7},
            ],
        )

    scores = await _reranker(handler).rerank(query="奖学金条件", texts=["甲", "乙", "丙"])
    assert scores == [0.5, 0.7, 0.9]
    assert str(captured["url"]).endswith("/rerank")
    assert captured["body"] == {
        "query": "奖学金条件",
        "texts": ["甲", "乙", "丙"],
        "raw_scores": False,
    }


async def test_rerank_empty_texts_returns_without_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应到达
        raise AssertionError("空候选不应发请求")

    assert await _reranker(handler).rerank(query="q", texts=[]) == []


async def test_rerank_non_200_is_rerank_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    with pytest.raises(RerankError) as exc_info:
        await _reranker(handler).rerank(query="q", texts=["a"])
    assert exc_info.value.code == "RERANK_HTTP_ERROR"


async def test_rerank_network_error_is_rerank_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(RerankError) as exc_info:
        await _reranker(handler).rerank(query="q", texts=["a"])
    assert exc_info.value.code == "RERANK_HTTP_ERROR"


async def test_rerank_timeout_is_distinct_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(RerankTimeoutError) as exc_info:
        await _reranker(handler).rerank(query="q", texts=["a"])
    assert exc_info.value.code == "RERANK_TIMEOUT"


async def test_rerank_non_list_response_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    with pytest.raises(RerankError) as exc_info:
        await _reranker(handler).rerank(query="q", texts=["a"])
    assert exc_info.value.code == "RERANK_RESPONSE_MALFORMED"


async def test_rerank_index_out_of_range_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"index": 3, "score": 0.5}])

    with pytest.raises(RerankError) as exc_info:
        await _reranker(handler).rerank(query="q", texts=["a", "b"])
    assert exc_info.value.code == "RERANK_RESPONSE_MALFORMED"


async def test_rerank_bool_index_and_bool_score_are_rejected() -> None:
    # TD-231 教训：bool 是 int 子类，`True == 1` 会蒙混等值断言，必须按类型拒
    for payload in ([{"index": True, "score": 0.5}], [{"index": 0, "score": True}]):
        def handler(request: httpx.Request, payload: object = payload) -> httpx.Response:
            return httpx.Response(200, json=payload)

        with pytest.raises(RerankError) as exc_info:
            await _reranker(handler).rerank(query="q", texts=["a", "b"])
        assert exc_info.value.code == "RERANK_RESPONSE_MALFORMED"


async def test_rerank_missing_entries_are_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"index": 0, "score": 0.5}])

    with pytest.raises(RerankError) as exc_info:
        await _reranker(handler).rerank(query="q", texts=["a", "b"])
    assert exc_info.value.code == "RERANK_RESPONSE_MALFORMED"


async def test_rerank_duplicate_index_is_rejected() -> None:
    # 审查 Minor 2 + 复审 Minor 1：重复 index 会静默覆盖前者，错误分数可伪装成
    # 重排结果。payload 必须全覆盖（否则修复前也会被「缺项」以同码拒绝，钉不住
    # seen 集合）——本例 2 条 texts、3 个条目且 index 0 重复：修复前静默返回被
    # 覆盖的错误分数不抛错，修复后 RERANK_RESPONSE_MALFORMED。
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"index": 0, "score": 0.9},
                {"index": 0, "score": 0.1},
                {"index": 1, "score": 0.5},
            ],
        )

    with pytest.raises(RerankError) as exc_info:
        await _reranker(handler).rerank(query="q", texts=["a", "b"])
    assert exc_info.value.code == "RERANK_RESPONSE_MALFORMED"


async def test_rerank_non_json_response_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(RerankError) as exc_info:
        await _reranker(handler).rerank(query="q", texts=["a"])
    assert exc_info.value.code == "RERANK_HTTP_ERROR"


async def test_rerank_owns_short_lived_client_when_not_injected() -> None:
    """未注入 client 时自建短生命周期客户端（与 TEIEmbedder 同款；真实路径冒烟）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"index": 0, "score": 0.25}])

    reranker = TEIReranker(
        base_url="http://tei",
        timeout_seconds=5,
        batch_size=10,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://tei"),
    )
    assert await reranker.rerank(query="q", texts=["a"]) == [0.25]


async def test_rerank_batches_requests_and_reassembles_scores() -> None:
    """客户端分批（TEI --max-client-batch-size 上限，KB-07B 全栈实测）：逐批请求、按全局下标拼回。"""
    captured_batches: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode("utf-8"))
        texts = body["texts"]
        captured_batches.append(texts)
        # 每批独立返回本批下标的分数（TEI 的 index 是批内下标）
        return httpx.Response(
            200,
            json=[
                {"index": i, "score": 0.5 + len(captured_batches) + i * 0.1}
                for i in range(len(texts))
            ],
        )

    reranker = TEIReranker(
        base_url="http://tei", timeout_seconds=5, batch_size=2, client=_client(handler)
    )
    scores = await reranker.rerank(query="q", texts=["a", "b", "c", "d", "e"])

    assert captured_batches == [["a", "b"], ["c", "d"], ["e"]]
    assert scores == [1.5, 1.6, 2.5, 2.6, 3.5]


async def test_rerank_batch_failure_raises_rerank_error() -> None:
    """任一批失败即整体失败（调用方降级为融合序），不返回半截分数表。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode("utf-8"))
        if body["texts"] == ["c"]:
            return httpx.Response(503, text="boom")
        return httpx.Response(200, json=[{"index": 0, "score": 0.5}])

    reranker = TEIReranker(
        base_url="http://tei", timeout_seconds=5, batch_size=1, client=_client(handler)
    )
    with pytest.raises(RerankError) as exc_info:
        await reranker.rerank(query="q", texts=["a", "b", "c"])
    assert exc_info.value.code == "RERANK_HTTP_ERROR"
