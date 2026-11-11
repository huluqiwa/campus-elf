"""重排端口与 TEI 适配器（M3 KB-07A；`docs/design.md` §7.5 第 4 步的沙箱侧实现）。

- `RerankPort`：重排端口（CI/集成用 fake，生产用 TEI rerank 实例）；
- `TEIReranker`：TEI `/rerank` 适配器（bge-reranker-v2-m3；`--auto-truncate`
  由服务端保证长文本截断）；响应按 `index` 映射回与输入 texts 对齐的分数表。

异常分层（简报 `docs/design-briefs/M3-KB-07.md` 卡 2 固定口径）：
`RerankTimeoutError` 单独成类——design §7.5「超时跳过」语义，检索测试按
`rerank_status="skipped"` 降级为融合序；其余失败按 `"failed"` 降级。
重排失败**永不**让诊断端点 5xx。

CPU 延迟事实（TD-056）：8 候选 p50≈11.7s——诊断面 opt-in（`with_rerank`），
chat 快路径在 M4 前不消费本端口。
"""

from collections.abc import Sequence
from typing import Protocol

import httpx


class RerankError(Exception):
    """重排失败（TEI 不可达/响应不合契约）；调用方降级为融合序并标注 failed。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RerankTimeoutError(RerankError):
    """重排超时；调用方按「跳过」降级（§7.5 同哲学），与一般失败区分开。"""


class RerankPort(Protocol):
    """重排端口：query + 候选文本 → 与输入顺序对齐的分数表。"""

    async def rerank(self, *, query: str, texts: Sequence[str]) -> list[float]:
        ...


class TEIReranker:
    """TEI `/rerank` 适配器（响应 `[{"index","score"}]` 按 index 归位）。

    客户端分批：TEI `--max-client-batch-size`（dev/e2e compose 为 8）限制单请求
    文本条数，超限被拒绝（KB-07B 全栈首跑实测）——按 `batch_size` 逐批请求后
    按全局下标拼回分数表（与 `TEIEmbedder` 的批次模式同款）。
    """

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

    async def rerank(self, *, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        scores: list[float | None] = [None] * len(texts)
        client = self._client or httpx.AsyncClient(timeout=self._timeout_seconds)
        try:
            for start in range(0, len(texts), self._batch_size):
                batch = list(texts[start : start + self._batch_size])
                batch_scores = await self._rerank_batch(client, query, batch)
                scores[start : start + self._batch_size] = batch_scores
        finally:
            if self._client is None:
                await client.aclose()
        mapped: list[float] = []
        for value in scores:
            if value is None:  # 分批覆盖全部下标；缺项属响应契约破坏，不静默补 0
                raise RerankError("RERANK_RESPONSE_MALFORMED", "TEI rerank 响应缺少部分文本的分数")
            mapped.append(value)
        return mapped

    async def _rerank_batch(
        self, client: httpx.AsyncClient, query: str, batch: list[str]
    ) -> list[float]:
        try:
            response = await client.post(
                f"{self._base_url}/rerank",
                json={"query": query, "texts": batch, "raw_scores": False},
            )
        except httpx.TimeoutException as exc:
            raise RerankTimeoutError("RERANK_TIMEOUT", "TEI rerank 请求超时") from exc
        except httpx.HTTPError as exc:
            raise RerankError(
                "RERANK_HTTP_ERROR", f"TEI rerank 网络错误：{type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            raise RerankError(
                "RERANK_HTTP_ERROR", f"TEI rerank 返回 HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RerankError("RERANK_HTTP_ERROR", "TEI rerank 返回非 JSON 响应") from exc
        return _map_scores(payload, len(batch))


def _map_scores(payload: object, count: int) -> list[float]:
    """`[{"index","score"}]`（score 降序）→ 与 texts 对齐的分数表。

    缺项/越界/类型错（含 bool——TD-231 教训）一律 `RERANK_RESPONSE_MALFORMED`，
    绝不静默补 0 让错误分数伪装成「重排结果」。
    """

    if not isinstance(payload, list):
        raise RerankError("RERANK_RESPONSE_MALFORMED", "TEI rerank 响应不是数组")
    scores: list[float | None] = [None] * count
    seen: set[int] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise RerankError("RERANK_RESPONSE_MALFORMED", "TEI rerank 响应项不是对象")
        index = item.get("index")
        score = item.get("score")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index >= count
        ):
            raise RerankError("RERANK_RESPONSE_MALFORMED", f"TEI rerank 索引越界：{index!r}")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise RerankError("RERANK_RESPONSE_MALFORMED", f"TEI rerank 分数类型非法：{score!r}")
        if index in seen:
            # 重复 index 会静默覆盖前者，错误分数可伪装成重排结果（审查 Minor 2）
            raise RerankError("RERANK_RESPONSE_MALFORMED", f"TEI rerank 索引重复：{index}")
        seen.add(index)
        scores[index] = float(score)
    mapped: list[float] = []
    for value in scores:
        if value is None:
            raise RerankError("RERANK_RESPONSE_MALFORMED", "TEI rerank 响应缺少部分文本的分数")
        mapped.append(value)
    return mapped
