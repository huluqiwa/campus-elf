"""KB-07A 检索测试（单 query 诊断面）：三查询旁路 + 可选 CPU 重排（简报
`docs/design-briefs/M3-KB-07.md`）。

口径（均为固定口径，随简报拍板）：

- **三查询旁路**：融合 RRF 查询原样复用 `search_points`（生产/沙箱检索语义
  零漂移），同一 dense/sparse 输入旁路各跑一次单路查询（limit = pool×2），
  app 侧按 chunk_id 合并出各路分数与该路排名——「为什么召回」逐路可解释；
- **查询构造镜像 KB-04A**（`test_task.py`）：dense = `embed_input_text(title,
  None, query)`；sparse = `version_sparse_weights([query])[0]`。sparse 的
  query 侧 IDF 语料与 build 侧不对称是既有 MVP 缺口（TD 登记，M4 收口），
  本点如实镜像、不顺手修；
- **租户与范围**：版本走 scope 过滤仓储（不可见/跨租/不存在 404 同文不泄露
  存在性），chunk 回查走 `TenantScopedRepository + ScopeBypass`（kb_chunks
  无归属列，KB-06A 卡 7 同款），Qdrant 按 `collection_name(tenant, version)`
  天然定域；
- **重排永不 5xx**：超时 → `skipped`、其他失败 → `failed`，一律降级为融合序；
- 只读、不落库、无新迁移、日志零 query 明文。
"""

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal
from uuid import UUID

from qdrant_client import AsyncQdrantClient

from app.config import get_settings
from app.db.models.kb import KbChunk, KbDocument, KbDocumentVersion
from app.db.repository import ScopeBypass, TenantScopedRepository
from app.db.session import db_session
from app.kb.embedding import EmbeddingError, EmbeddingPort, embed_input_text, version_sparse_weights
from app.kb.indexer import (
    IndexError_,
    collection_name,
    search_dense,
    search_points,
    search_sparse,
)
from app.kb.rerank import RerankError, RerankPort, RerankTimeoutError, TEIReranker
from app.kb.service import KbError
from app.kb.status import (
    KB_STATUS_DELETED,
    KB_STATUS_OFFLINE,
    KB_STATUS_PUBLISHED,
    KB_STATUS_READY,
    KB_STATUS_TEST_FAILED,
    KB_STATUS_TEST_PASSED,
    KB_STATUS_TESTING,
)
from app.rbac.scopes import DataScopeContext

logger = logging.getLogger(__name__)

# collection 在 INDEXING 创建并保留（发布/回滚/下线/软删均不删，M6 才清理）；
# FAILED/QUEUED/BUILDING 与审批前状态没有完整索引，检索测试按 409 拒绝。
_INDEXED_STATUSES = frozenset(
    {
        KB_STATUS_READY,
        KB_STATUS_TESTING,
        KB_STATUS_TEST_PASSED,
        KB_STATUS_TEST_FAILED,
        KB_STATUS_PUBLISHED,
        KB_STATUS_OFFLINE,
        KB_STATUS_DELETED,
    }
)
_CHUNK_BYPASS = ScopeBypass(reason="检索测试：版本已按数据范围校验，分块按版本归属回查")

PREVIEW_LIMIT = 200

# 重排状态（applied=已重排；skipped=超时跳过；failed=其他失败；disabled=未启用）。
RerankStatus = Literal["applied", "skipped", "failed", "disabled"]


# ---------------------------------------------------------------------------
# 视图模型（读侧 dataclass；接口层再转 pydantic）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RouteHit:
    """单路（dense/sparse）命中：分数 + 该路 1-based 排名。"""

    score: float
    rank: int


@dataclass(frozen=True, slots=True)
class RetrievalRow:
    """检索测试结果行（final_rank 在融合序或重排后重编；PG 缺行不产生行）。"""

    chunk_id: UUID
    chunk_index: int
    chunk_type: str
    section_path: str | None
    page_start: int | None
    page_end: int | None
    text: str  # 全文（重排用）；响应侧做码点 preview 截断
    rrf_score: float
    dense: RouteHit | None
    sparse: RouteHit | None
    rerank_score: float | None
    final_rank: int


@dataclass(frozen=True, slots=True)
class RetrievalTestResult:
    """检索测试结果（pool_size = len(results)，由接口层派生）。"""

    version_id: UUID
    query: str
    top_k: int
    with_rerank: bool
    rerank_status: RerankStatus
    latency_ms: float
    results: list[RetrievalRow]


# ---------------------------------------------------------------------------
# 纯函数：preview / 单路归位 / 行构造 / 重排
# ---------------------------------------------------------------------------


def text_preview(text: str, *, limit: int = PREVIEW_LIMIT) -> str:
    """码点截断（Python len 即码点；前端预检按 codePointLength 对齐）。"""

    return text if len(text) <= limit else text[:limit]


def route_rank_map(hits: Sequence[dict[str, Any]]) -> dict[str, RouteHit]:
    """单路结果 `[{"chunk_id","score"}]`（降序）→ `{chunk_id: RouteHit(score, rank)}`。"""

    return {
        str(hit["chunk_id"]): RouteHit(score=float(hit["score"]), rank=position)
        for position, hit in enumerate(hits, start=1)
    }


def build_rows(
    *,
    fused: Sequence[dict[str, Any]],
    chunk_rows: Mapping[UUID, KbChunk],
    dense_hits: Sequence[dict[str, Any]],
    sparse_hits: Sequence[dict[str, Any]],
) -> list[RetrievalRow]:
    """融合序行构造：RRF 名次 = 列表位置；逐路分数按 chunk_id 归位。

    索引有而 PG 缺行的分块跳过（构建双对账已保证一致，防御性容错），跳过后
    final_rank 仍连续。
    """

    dense_map = route_rank_map(dense_hits)
    sparse_map = route_rank_map(sparse_hits)
    rows: list[RetrievalRow] = []
    for hit in fused:
        chunk_id = UUID(str(hit["chunk_id"]))
        chunk = chunk_rows.get(chunk_id)
        if chunk is None:
            continue
        rows.append(
            RetrievalRow(
                chunk_id=chunk_id,
                chunk_index=chunk.chunk_index,
                chunk_type=chunk.chunk_type,
                section_path=chunk.section_path,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                text=chunk.text,
                rrf_score=float(hit["score"]),
                dense=dense_map.get(str(chunk_id)),
                sparse=sparse_map.get(str(chunk_id)),
                rerank_score=None,
                final_rank=len(rows) + 1,
            )
        )
    return rows


def apply_rerank(
    rows: Sequence[RetrievalRow], scores: Sequence[float]
) -> list[RetrievalRow]:
    """按重排分数降序**稳定**重排（同分保持融合序）；final_rank 重编为 1..N。"""

    if len(scores) != len(rows):
        raise RerankError("RERANK_COUNT_MISMATCH", "重排分数条数与候选数不一致")
    scored = [
        (score, row) for row, score in zip(rows, scores, strict=True)
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        replace(row, rerank_score=score, final_rank=position)
        for position, (score, row) in enumerate(scored, start=1)
    ]


# ---------------------------------------------------------------------------
# 编排（读链路；fake 注入点与 test_task 同款命名）
# ---------------------------------------------------------------------------


def _build_embedder_for_test(settings: Any) -> EmbeddingPort:
    from app.kb.embedding import TEIEmbedder

    return TEIEmbedder(
        base_url=settings.tei_embed_url,
        timeout_seconds=settings.tei_embed_timeout_seconds,
        batch_size=settings.kb_embed_batch_size,
    )


def _build_reranker_for_test(settings: Any) -> RerankPort:
    return TEIReranker(
        base_url=settings.tei_rerank_url,
        timeout_seconds=settings.tei_rerank_timeout_seconds,
        batch_size=settings.tei_rerank_batch_size,
    )


def _stable_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """分数降序 + chunk_id 升序的确定性排序。

    Qdrant 对 RRF/单路的同分候选顺序不保证跨调用稳定（集成实测同查询两次
    调用在并列区翻转）；诊断面要可复现，统一在 app 侧 tie-break（只稳显示
    序，不改集合成员——批量判分是集合对账，不受影响）。
    """

    return sorted(
        hits, key=lambda hit: (-float(hit["score"]), str(hit["chunk_id"]))
    )


async def run_retrieval_test(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    version_id: UUID,
    query: str,
    top_k: int = 10,
    with_rerank: bool = False,
) -> RetrievalTestResult:
    """单 query 检索测试：三查询旁路合并 + 可选重排；只读，不落库。

    异常:
        KbError: 版本不可见（`VERSION_NOT_FOUND`，404）、无有效索引
            （`RETRIEVAL_TEST_NOT_INDEXED`，409）、嵌入/检索基础设施故障
            （`RETRIEVAL_TEST_EMBED_FAILED` / `RETRIEVAL_TEST_SEARCH_FAILED`
            / `QDRANT_UNAVAILABLE`，502）。重排失败不抛——降级 + `rerank_status`。
    """

    started = time.monotonic()
    resolved = get_settings()
    # 池不得小于 top_k：否则最终引用少于请求数（审查 Minor 1——config 下限 1，
    # 运营误配 pool<top_k 时这里兜底，默认 20 = top_k 上限，默认组合不受影响）
    pool = max(resolved.kb_retrieval_test_rerank_pool, top_k) if with_rerank else top_k

    async with db_session(tenant_id=tenant_id) as session:
        version = await TenantScopedRepository(
            session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
        ).get(version_id)
        if version is None:
            raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
        if version.status not in _INDEXED_STATUSES:
            raise KbError("RETRIEVAL_TEST_NOT_INDEXED", "该版本暂无可检索的索引", 409)
        document = await TenantScopedRepository(
            session, KbDocument, tenant_id, scope=scope, owner_attr="created_by"
        ).get(version.document_id)
        if document is None:
            raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
        resolved_version_id = version.id
        document_title = document.title

    collection = collection_name(tenant_id, resolved_version_id)
    client = AsyncQdrantClient(url=resolved.qdrant_url) if resolved.qdrant_url else None
    if client is None:
        raise KbError("QDRANT_UNAVAILABLE", "向量检索未配置", 502)
    try:
        embedder = _build_embedder_for_test(resolved)
        query_text = embed_input_text(
            document_title=document_title, section_path=None, chunk_text=query
        )
        try:
            dense_vectors = await embedder.embed([query_text])
            sparse = version_sparse_weights([query])[0]
        except EmbeddingError as exc:
            raise KbError("RETRIEVAL_TEST_EMBED_FAILED", "嵌入服务暂不可用", 502) from exc
        try:
            fused = _stable_hits(
                await search_points(
                    client,
                    collection=collection,
                    dense=dense_vectors[0],
                    sparse=sparse,
                    top_k=pool,
                )
            )
            dense_hits = _stable_hits(
                await search_dense(
                    client, collection=collection, dense=dense_vectors[0], limit=pool * 2
                )
            )
            sparse_hits = _stable_hits(
                await search_sparse(
                    client, collection=collection, sparse=sparse, limit=pool * 2
                )
            )
        except IndexError_ as exc:
            raise KbError("RETRIEVAL_TEST_SEARCH_FAILED", "向量检索失败", 502) from exc
    finally:
        await client.close()

    async with db_session(tenant_id=tenant_id) as session:
        chunk_repository = TenantScopedRepository(
            session, KbChunk, tenant_id, scope=_CHUNK_BYPASS
        )
        stmt = chunk_repository.base_query().where(
            KbChunk.document_version_id == resolved_version_id,
            KbChunk.id.in_([UUID(str(hit["chunk_id"])) for hit in fused]),
        )
        chunk_rows = {
            chunk.id: chunk for chunk in (await session.execute(stmt)).scalars().all()
        }

    rows = build_rows(
        fused=fused,
        chunk_rows=chunk_rows,
        dense_hits=dense_hits,
        sparse_hits=sparse_hits,
    )

    rerank_status: RerankStatus = "disabled"
    if with_rerank:
        # 降级留痕（KB-07B 全栈排障教训：静默降级让 TEI 批量上限问题不可诊断）；
        # 只记异常类型名，不记 query/文本（零 PII 红线）
        reranker = _build_reranker_for_test(resolved)
        try:
            scores = await reranker.rerank(query=query, texts=[row.text for row in rows])
        except RerankTimeoutError:
            logger.warning("rerank 降级（skipped）：RerankTimeoutError")
            rerank_status = "skipped"
        except RerankError as exc:
            logger.warning("rerank 降级（failed）：%s", type(exc).__name__)
            rerank_status = "failed"
        except Exception as exc:
            # 审查 Major 1：端口实现抛出非 RerankError 异常（如畸形 URL 的
            # httpx.InvalidURL）也必须降级——rerank 失败永不 5xx（卡 2 硬规则）
            logger.warning("rerank 降级（failed/未归类）：%s", type(exc).__name__)
            rerank_status = "failed"
        else:
            # 复审 Major 1：try/else 的 else 子句不被本语句 except 接住——
            # apply_rerank 的 RERANK_COUNT_MISMATCH（端口返回条数不符）单独兜底，
            # 同样诚实降级；其非 RerankError 内部 bug 仍照常上抛（不吞编排 bug）
            try:
                rows = apply_rerank(rows, scores)
                rerank_status = "applied"
            except RerankError:
                rerank_status = "failed"

    return RetrievalTestResult(
        version_id=resolved_version_id,
        query=query,
        top_k=top_k,
        with_rerank=with_rerank,
        rerank_status=rerank_status,
        latency_ms=round((time.monotonic() - started) * 1000, 1),
        results=rows,
    )
