"""chat 侧检索服务（M4-AI01b 快路径 RETRIEVE；AI-10 消费 KB-09B 检索面）。

职责（简报 `docs/design-briefs/M4-AI01b-fast-path-loop.md` 卡 3/4）：

- **活跃版本发现**：租户内 `kb_publish_aliases` × 文档 `PUBLISHED` 态 → 待检索
  collection 列表（D4：全表一次发现，避免 N+1）；
- **人群解析 fail-closed**（AI-10）：`resolve_audience_tokens` 异常/空集 → 返回
  空结果且**不发 Qdrant 查询**（空 Filter 匹配全部点，绝不能下发）；
- **多版本 fan-out**：每版本一个 collection，并行检索后按 RRF 分数全局归并
  （分数跨 collection 可比；tie-break chunk_id 升序钉确定性——TD-P4）；
- **证据文本 PG 回查**：payload 只有 `text_hash`，文本在 `kb_chunks.text`；回查
  即引用存在性校验（索引在、库行不在的块直接丢弃）。

可见性口径：发布中的知识库对全租户可见，可见性由**人群 token**治理（ACL），
不经操作者 RBAC 数据范围过滤——因此 PG 读取用租户定域仓储 + `ScopeBypass`
显式留痕（与 KB-06A 管理端「版本 scope 过滤 + 分块 bypass」纪律不冲突：那是
B 端按版本管理读，这里是 C 端按发布面读）。检索基础设施故障聚合上报：单个
collection 失败跳过（记 warning，码不透传细节），全部失败才抛错。
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qmodels

from app.db.models.kb import KbChunk, KbDocument, KbPublishAlias
from app.db.repository import ScopeBypass, TenantScopedRepository
from app.db.session import db_session
from app.kb.audience import resolve_audience_tokens
from app.kb.audience_source import RepositoryAudienceSource
from app.kb.embedding import (
    EmbeddingError,
    EmbeddingPort,
    SparseVector,
    embed_input_text,
    version_sparse_weights,
)
from app.kb.indexer import acl_query_filter, collection_name, search_points

logger = logging.getLogger(__name__)

_REASON_PUBLISHED = "chat 检索：发布中知识库按人群 token 治理可见性，不经操作者数据范围"
_REASON_CHUNK = "chat 检索：证据文本按版本归属读取（发布版本经人群 token 过滤）"


class ChatRetrievalError(Exception):
    """chat 检索失败；只携带内部错误码（loop 层映射安全话术，不透传细节）。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ChatEvidence:
    """一条证据：检索元数据 + PG 回查文本（citation 事件与 GENERATE 上下文的单一事实源）。"""

    chunk_id: str
    score: float
    text: str
    doc_title: str
    section_path: str | None
    page_start: int | None
    page_end: int | None
    chunk_type: str
    version_id: str


def merge_hits(hits: Sequence[dict[str, Any]], *, top_k: int) -> list[dict[str, Any]]:
    """跨 collection 命中归并：分数降序 + chunk_id 升序 tie-break → 截 top_k。

    RRF 分数跨 collection 可比（秩的倒数）；Qdrant 对同分候选的返回序不保证
    跨调用稳定（KB-07A 集成实测），统一 app 侧 tie-break 钉确定性。
    """

    return sorted(
        hits, key=lambda hit: (-float(str(hit["score"])), str(hit["chunk_id"]))
    )[:top_k]


class ChatRetriever:
    """快路径检索器（use-site 构造；端口注入与 AI-01a LlmPort 同款纪律）。

    参数:
        embedder: 查询向量化端口（dense）；sparse 与索引侧同款函数本地计算。
        qdrant_url: Qdrant 地址；未配置时检索抛 `QDRANT_UNAVAILABLE`。
        top_k: 全局归并后的最终引用条数（Settings `kb_chat_top_k`）。
        client: 测试注入的 Qdrant 客户端（缺省按 url 构建并自行关闭）。
    """

    def __init__(
        self,
        *,
        embedder: EmbeddingPort,
        qdrant_url: str | None,
        top_k: int = 5,
        client: AsyncQdrantClient | None = None,
    ) -> None:
        self._embedder = embedder
        self._qdrant_url = qdrant_url
        self._top_k = top_k
        self._client = client

    async def search_hits(
        self, *, tenant_id: UUID, user_id: UUID, query: str
    ) -> list[dict[str, Any]]:
        """检索命中元数据（`kb.search` 工具面；不含证据文本——卡 3-A）。

        空集语义统一为「零命中」（发现为空/人群空集/无召回都返回 `[]`，
        调用方走兜底话术，不区分原因——不提示「存在但无权」）。
        """

        version_ids = await self._active_version_ids(tenant_id)
        if not version_ids:
            return []
        tokens = await self._resolve_tokens(tenant_id=tenant_id, user_id=user_id)
        if not tokens:
            return []
        dense = await self._embed_query(query)
        sparse = version_sparse_weights([query])[0]
        return await self._fanout(
            tenant_id, version_ids, dense=dense[0], sparse=sparse, tokens=tokens
        )

    async def _active_version_ids(self, tenant_id: UUID) -> list[UUID]:
        """活跃发布版本：alias 全表 × 文档 PUBLISHED（D4，两次查询避免跨模型 join）。"""

        async with db_session(tenant_id=tenant_id) as session:
            alias_repo = TenantScopedRepository(
                session, KbPublishAlias, tenant_id, scope=ScopeBypass(reason=_REASON_PUBLISHED)
            )
            alias_rows = (await session.execute(alias_repo.base_query())).scalars().all()
            if not alias_rows:
                return []
            doc_repo = TenantScopedRepository(
                session, KbDocument, tenant_id, scope=ScopeBypass(reason=_REASON_PUBLISHED)
            )
            doc_ids = {row.document_id for row in alias_rows}
            docs = (
                (
                    await session.execute(
                        doc_repo.base_query().where(
                            KbDocument.id.in_(doc_ids), KbDocument.status == "PUBLISHED"
                        )
                    )
                )
                .scalars()
                .all()
            )
            published_doc_ids = {doc.id for doc in docs}
            return [
                row.version_id for row in alias_rows if row.document_id in published_doc_ids
            ]

    async def _resolve_tokens(self, *, tenant_id: UUID, user_id: UUID) -> list[str]:
        """人群 token 解析（KB-09B）：异常一律 fail-closed 空集（不召回不引用不提示存在）。"""

        async with db_session(tenant_id=tenant_id) as session:
            source = RepositoryAudienceSource(session, tenant_id)
            try:
                return await resolve_audience_tokens(source, user_id)
            except Exception as exc:  # noqa: BLE001——解析异常=空集，细节只进日志
                logger.warning(
                    "chat 人群解析失败（fail-closed 空集）：%s.%s",
                    type(exc).__module__,
                    type(exc).__name__,
                )
                return []

    async def _embed_query(self, query: str) -> list[list[float]]:
        """查询向量化：dense 前缀无文档上下文（空 title → 纯查询文本）；sparse 本地同款函数。"""

        try:
            return await self._embedder.embed(
                [embed_input_text(document_title="", section_path=None, chunk_text=query)]
            )
        except EmbeddingError as exc:
            raise ChatRetrievalError("CHAT_EMBED_FAILED") from exc

    async def _fanout(
        self,
        tenant_id: UUID,
        version_ids: Sequence[UUID],
        *,
        dense: list[float],
        sparse: SparseVector,
        tokens: list[str],
    ) -> list[dict[str, Any]]:
        """多 collection 并行检索 → 归并截 top_k。

        单个 collection 失败跳过（记 warning，码不透传细节）；全部失败才抛
        `CHAT_SEARCH_FAILED`——健康集合的部分结果仍可服务（诚实降级）。
        """

        client = self._client
        owns_client = False
        if client is None:
            if not self._qdrant_url:
                raise ChatRetrievalError("QDRANT_UNAVAILABLE")
            client = AsyncQdrantClient(url=self._qdrant_url)
            owns_client = True
        query_filter = acl_query_filter(tokens)
        try:
            results = await asyncio.gather(
                *(
                    self._search_one(
                        client, tenant_id, version_id, dense=dense, sparse=sparse,
                        query_filter=query_filter,
                    )
                    for version_id in version_ids
                ),
                return_exceptions=True,
            )
        finally:
            if owns_client:
                await client.close()
        ok: list[list[dict[str, object]]] = []
        failed = 0
        for result in results:
            if isinstance(result, BaseException):
                failed += 1
            else:
                ok.append(result)
        if failed:
            logger.warning("chat 检索部分 collection 失败：failed=%s ok=%s", failed, len(ok))
        if not ok:
            raise ChatRetrievalError("CHAT_SEARCH_FAILED")
        return merge_hits([hit for hits in ok for hit in hits], top_k=self._top_k)

    async def _search_one(
        self,
        client: AsyncQdrantClient,
        tenant_id: UUID,
        version_id: UUID,
        *,
        dense: list[float],
        sparse: SparseVector,
        query_filter: qmodels.Filter | None,
    ) -> list[dict[str, Any]]:
        """单版本 collection 检索（ACL filter 下沉到融合查询，KB-09B 口径）。"""

        from app.kb.indexer import IndexError_

        collection = collection_name(tenant_id, version_id)
        try:
            return await search_points(
                client,
                collection=collection,
                dense=dense,
                sparse=sparse,
                top_k=self._top_k,
                query_filter=query_filter,
            )
        except IndexError_ as exc:
            raise ChatRetrievalError("CHAT_SEARCH_FAILED") from exc

    async def load_texts(
        self, tenant_id: UUID, hits: Sequence[dict[str, Any]]
    ) -> list[ChatEvidence]:
        """证据文本回查（`kb_chunks.text`；编排器直调，非工具面——卡 3-A）。

        索引在、库行不在的块直接丢弃（回查即引用存在性校验），不产出幽灵引用。
        """

        if not hits:
            return []
        chunk_ids = [UUID(str(hit["chunk_id"])) for hit in hits]
        async with db_session(tenant_id=tenant_id) as session:
            repo = TenantScopedRepository(
                session, KbChunk, tenant_id, scope=ScopeBypass(reason=_REASON_CHUNK)
            )
            rows = (
                (await session.execute(repo.base_query().where(KbChunk.id.in_(chunk_ids))))
                .scalars()
                .all()
            )
            by_id = {str(row.id): row for row in rows}
            evidence: list[ChatEvidence] = []
            for hit in hits:
                row = by_id.get(str(hit["chunk_id"]))
                if row is None:
                    logger.warning("chat 证据回查缺行（丢弃）：chunk=%s", hit["chunk_id"])
                    continue
                payload = hit["payload"] if isinstance(hit["payload"], dict) else {}
                evidence.append(
                    ChatEvidence(
                        chunk_id=str(hit["chunk_id"]),
                        score=float(str(hit["score"])),
                        text=row.text,
                        doc_title=str(payload.get("doc_title") or ""),
                        section_path=payload.get("section_path"),
                        page_start=payload.get("page_start"),
                        page_end=payload.get("page_end"),
                        chunk_type=str(payload.get("chunk_type") or "text"),
                        version_id=str(payload.get("version_id") or ""),
                    )
                )
            return evidence
