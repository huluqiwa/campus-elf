"""KB-07A 检索测试脚手架（集成与业务场景共用）。

- `TokenOverlapEmbedder`：词袋式确定性 dense 向量（token 哈希落 1024 维 + 频次，
  L2 归一）——构建侧（`seed_test_passed_version(embedder=...)`）与查询侧
  （`patch_query_embedder`）注入同一实现后，「query == 某子块全文」即该子块
  cosine=1.0（词袋与顺序无关），top-1 断言确定化；
- `FakeReranker` 家族：applied（升序分数 → 稳定翻转融合序）/ timeout / failed
  三态，供重排降级与重排生效断言。
"""

import math
from collections.abc import Sequence

import pytest

from app.kb.embedding import tokenize
from app.kb.rerank import RerankError, RerankTimeoutError


def _token_slot(token: str) -> int:
    import hashlib

    return int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:4], "big") % 1024


class TokenOverlapEmbedder:
    """词袋式确定性 embedder：同文本恒同向量；共享 token 越多 cosine 越高。"""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vec = [0.0] * 1024
            for token in tokenize(text):
                vec[_token_slot(token)] += 1.0
            norm = math.sqrt(sum(value * value for value in vec)) or 1.0
            vectors.append([value / norm for value in vec])
        return vectors


class FakeReranker:
    """按位置升序给分（第 i 个候选得 (i+1)/N）→ 重排后融合序稳定翻转。"""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.text_batches: list[list[str]] = []

    async def rerank(self, *, query: str, texts: Sequence[str]) -> list[float]:
        self.queries.append(query)
        self.text_batches.append(list(texts))
        n = len(texts)
        return [(position + 1) / n for position in range(n)]


class TimingOutReranker:
    """恒超时（§7.5「超时跳过」→ rerank_status="skipped"）。"""

    async def rerank(self, *, query: str, texts: Sequence[str]) -> list[float]:
        raise RerankTimeoutError("RERANK_TIMEOUT", "fake timeout")


class FailingReranker:
    """恒失败（→ rerank_status="failed"）。"""

    async def rerank(self, *, query: str, texts: Sequence[str]) -> list[float]:
        raise RerankError("RERANK_HTTP_ERROR", "fake failure")


def patch_query_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """查询侧注入词袋 embedder（检索测试模块的 fake 注入点）。"""

    from app.kb import retrieval_test

    monkeypatch.setattr(
        retrieval_test, "_build_embedder_for_test", lambda settings: TokenOverlapEmbedder()
    )
