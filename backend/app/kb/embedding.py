"""嵌入端口与适配器（M3 KB-03A；`docs/design.md` §7.3）。

- `EmbeddingPort`：dense 嵌入端口（CI 用 fake，生产用 TEI）；
- `TEIEmbedder`：TEI `/embed` 真实适配器（BGE-M3，1024 维，CPU）；
- `version_sparse_weights`：jieba 分词 + BM25 权重（按版本语料计算 IDF——
  「按 KB 版本维护 IDF」的构建时实现）+ token uint32 哈希 → Qdrant SparseVector。

上下文前缀（§7.2）：每个子块嵌入前拼「文档名+章节路径」——只作用于 dense 输入，
sparse 仍按子块原文分词（检索词匹配的是原文）。
"""

import hashlib
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx
import jieba

_SPARSE_DIM_MASK = 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class SparseVector:
    """Qdrant sparse vector： indices（uint32 token hash）+ values（BM25 权重）。"""

    indices: list[int]
    values: list[float]


class EmbeddingError(Exception):
    """嵌入失败（TEI 不可达/维度漂移）；构建按 FAILED 收口，可重试。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class EmbeddingPort(Protocol):
    """dense 嵌入端口：批量文本 → 批量向量（顺序一一对应）。"""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...


class TEIEmbedder:
    """TEI `/embed` 适配器（批量；维度一致性在响应侧校验）。"""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        batch_size: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._batch_size = batch_size
        self._client = client

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        client = self._client or httpx.AsyncClient(timeout=self._timeout_seconds)
        try:
            for start in range(0, len(texts), self._batch_size):
                batch = list(texts[start : start + self._batch_size])
                try:
                    response = await client.post(
                        f"{self._base_url}/embed",
                        json={"inputs": batch, "truncate": True},
                    )
                except httpx.HTTPError as exc:
                    raise EmbeddingError(
                        "EMBED_HTTP_ERROR",
                        f"TEI 网络错误：{type(exc).__name__}",
                    ) from exc
                if response.status_code != 200:
                    raise EmbeddingError(
                        "EMBED_HTTP_ERROR",
                        f"TEI 返回 HTTP {response.status_code}",
                    )
                try:
                    batch_vectors = response.json()
                except ValueError as exc:
                    raise EmbeddingError("EMBED_HTTP_ERROR", "TEI 返回非 JSON 响应") from exc
                if not isinstance(batch_vectors, list) or len(batch_vectors) != len(batch):
                    raise EmbeddingError(
                        "EMBED_DIM_MISMATCH",
                        f"TEI 返回条数不一致（期望 {len(batch)}）",
                    )
                vectors.extend(batch_vectors)
        finally:
            if self._client is None:
                await client.aclose()
        _validate_dims(vectors)
        return vectors


def _validate_dims(vectors: list[list[float]]) -> None:
    dims = {len(vector) for vector in vectors if isinstance(vector, list)}
    if len(dims) != 1 or dims == {0}:
        raise EmbeddingError(
            "EMBED_DIM_MISMATCH",
            f"TEI 返回向量维度不一致或为空（{sorted(dims)}）",
        )


def _token_hash(token: str) -> int:
    """token → uint32（sha256 前 4 字节；碰撞概率对检索排序无实质影响，design §7.3）。"""

    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & _SPARSE_DIM_MASK


def tokenize(text: str) -> list[str]:
    """jieba 分词（去空白 token）。"""

    jieba.initialize()
    return [token for token in jieba.lcut(text) if token.strip()]


def version_sparse_weights(
    chunk_texts: Sequence[str], *, k1: float = 1.2, b: float = 0.75
) -> list[SparseVector]:
    """按版本语料计算 BM25 sparse 向量（design §7.3「按 KB 版本维护 IDF」）。

    IDF = ln((N - df + 0.5) / (df + 0.5) + 1)（BM25+ 平滑，避免负权）；
    权重 = tf 饱和（k1/b 长度归一）× IDF。同版本内 token 哈希碰撞会在同一条
    sparse 向量里合并（indices 去重、值相加）。
    """

    n = len(chunk_texts)
    tokenized = [tokenize(text) for text in chunk_texts]
    df: Counter[str] = Counter()
    for tokens in tokenized:
        df.update(set(tokens))
    # 标准 BM25 的长度归一需要版本语料平均长度（avgdl）——否则 b 项恒等于 b、
    # 长块不被惩罚（审查 M4）
    avgdl = (sum(len(tokens) for tokens in tokenized) / n) if n else 0.0
    vectors: list[SparseVector] = []
    for tokens in tokenized:
        counts = Counter(tokens)
        length = len(tokens)
        indices: list[int] = []
        values: list[float] = []
        merged: dict[int, float] = {}
        for token, tf in counts.items():
            idf = math.log((n - df[token] + 0.5) / (df[token] + 0.5) + 1.0)
            denom_norm = (1.0 - b + b * (length / avgdl)) if avgdl > 0 else 1.0
            tf_norm = (tf * (k1 + 1.0)) / (tf + k1 * denom_norm)
            weight = tf_norm * idf
            slot = _token_hash(token)
            merged[slot] = merged.get(slot, 0.0) + weight
        for slot in sorted(merged):
            indices.append(slot)
            values.append(round(merged[slot], 6))
        vectors.append(SparseVector(indices=indices, values=values))
    return vectors


def embed_input_text(*, document_title: str, section_path: str | None, chunk_text: str) -> str:
    """dense 嵌入前拼「文档名+章节路径」上下文（design §7.2）。"""

    prefix = document_title.strip()
    if section_path and section_path.strip():
        prefix = f"{prefix} > {section_path.strip()}"
    return f"{prefix}\n{chunk_text}" if prefix else chunk_text
