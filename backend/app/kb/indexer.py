"""Qdrant 版本索引（M3 KB-03A；`docs/design.md` §7.4）。

- collection 命名 `tenant_{tenant_hash}_kb_v{version_id}`（tenant_hash = 租户 id
  sha256 前 12 位；alias 切换是 KB-05，本点只建 version collection）；
- named vectors：`dense`（cosine，BGE-M3 1024 维）+ `sparse`（BM25）；
- payload 十字段（design §7.4），`is_published` 构建期恒 false（发布侧翻转）；
- 幂等：重建前删旧 collection（重试/复建的 idempotent 路径）；
- 计数对账：VALIDATING 用 `count_points` 对齐 chunk 数。
"""

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qmodels

from app.kb.embedding import SparseVector

logger = logging.getLogger(__name__)


class IndexError_(Exception):
    """索引失败（Qdrant 不可达/对账不一致）；构建按 FAILED 收口，可重试。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def tenant_hash(tenant_id: UUID) -> str:
    """租户 id 的短哈希（collection 名内不出现明文租户 id）。"""

    return hashlib.sha256(str(tenant_id).encode("utf-8")).hexdigest()[:12]


def collection_name(tenant_id: UUID, version_id: UUID) -> str:
    """`tenant_{tenant_hash}_kb_v{version_id}`（design §7.4）。"""

    return f"tenant_{tenant_hash(tenant_id)}_kb_v{version_id}"


def document_hash(document_id: UUID) -> str:
    """文档 id 的短哈希（alias 名内不出现明文文档 id，与租户哈希同款口径）。"""

    return hashlib.sha256(str(document_id).encode("utf-8")).hexdigest()[:12]


def alias_name(tenant_id: UUID, document_id: UUID) -> str:
    """`tenant_{tenant_hash}_kb_d{document_hash}_active`（design §7.4 修订：alias 每文档一个）。

    design §7.4 原文的租户单 alias 名物理上不成立（Qdrant alias 指向单 collection，
    而 collection 是每版本一个、租户多文档必有多个活跃 collection）；
    KB-05 简报 D1-A 已确认按文档粒度修订，合入时回写 design。
    """

    return f"tenant_{tenant_hash(tenant_id)}_kb_d{document_hash(document_id)}_active"


def alias_switch_ops(
    alias: str, current_collection: str | None, target_collection: str
) -> list[qmodels.CreateAliasOperation | qmodels.DeleteAliasOperation]:
    """构造一次 `update_collection_aliases` 的原子操作序列（纯函数，单测覆盖）。

    - `current_collection=None`：alias 不存在（首次发布/重建）→ 单 Create；
    - 相同：无操作（幂等重放的已同步态）→ 空列表；
    - 不同：`[Delete, Create]` 同一次调用内原子完成（蓝绿切换，发布/回滚窗口零空档）。
    """

    if current_collection is None:
        return [
            qmodels.CreateAliasOperation(
                create_alias=qmodels.CreateAlias(
                    collection_name=target_collection, alias_name=alias
                )
            )
        ]
    if current_collection == target_collection:
        return []
    return [
        qmodels.DeleteAliasOperation(delete_alias=qmodels.DeleteAlias(alias_name=alias)),
        qmodels.CreateAliasOperation(
            create_alias=qmodels.CreateAlias(
                collection_name=target_collection, alias_name=alias
            )
        ),
    ]


async def resolve_alias_collection(client: AsyncQdrantClient, alias: str) -> str | None:
    """查 alias 当前指向的 collection（不存在返回 None；幂等重放判定用）。"""

    try:
        response = await client.get_aliases()
    except Exception as exc:
        logger.warning("Qdrant get_aliases 失败：%s.%s", type(exc).__module__, type(exc).__name__)
        raise IndexError_("ALIAS_LOOKUP_FAILED", "向量别名查询失败") from exc
    for entry in response.aliases:
        if entry.alias_name == alias:
            return entry.collection_name
    return None


async def switch_alias(
    client: AsyncQdrantClient, *, alias: str, current_collection: str | None, target_collection: str
) -> None:
    """原子切换 alias（或首次创建）；无操作时跳过。"""

    ops = alias_switch_ops(alias, current_collection, target_collection)
    if not ops:
        return
    try:
        await client.update_collection_aliases(change_aliases_operations=ops)
    except Exception as exc:
        logger.warning("Qdrant alias 切换失败：%s.%s", type(exc).__module__, type(exc).__name__)
        raise IndexError_("ALIAS_SWITCH_FAILED", "向量别名切换失败") from exc


async def mark_published_payload(
    client: AsyncQdrantClient, *, collection: str, version_id: UUID
) -> None:
    """发布侧翻转 payload `is_published=true`（indexer 模块注释的 KB-03A 预留）。

    payload 的 `version_id` 是字符串（`build_payload`），filter 用字符串匹配；
    检索（M4）主键是 `tenant_id_hash`+`version_id`，本标记为信息位。
    """

    try:
        await client.set_payload(
            collection_name=collection,
            payload={"is_published": True},
            points=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="version_id", match=qmodels.MatchValue(value=str(version_id))
                    )
                ]
            ),
            wait=True,
        )
    except Exception as exc:
        logger.warning("Qdrant set_payload 失败：%s.%s", type(exc).__module__, type(exc).__name__)
        raise IndexError_("PAYLOAD_UPDATE_FAILED", "发布标记写入失败") from exc


@dataclass(frozen=True, slots=True)
class IndexPoint:
    """一个待索引点（chunk 行 + 已算好的双向量）。"""

    chunk_id: UUID
    parent_id: UUID | None
    document_id: UUID
    chunk_type: str
    section_path: str | None
    page_start: int | None
    page_end: int | None
    text_hash: str
    dense: list[float]
    sparse: SparseVector


def build_payload(
    *,
    tenant_id: UUID,
    document_id: UUID,
    version_id: UUID,
    point: IndexPoint,
    doc_title: str,
) -> dict[str, Any]:
    """Qdrant payload（design §7.4 十字段；`tenant_id_hash` 而非明文租户 id）。"""

    return {
        "tenant_id_hash": tenant_hash(tenant_id),
        "kb_id": str(document_id),
        "document_id": str(document_id),
        "version_id": str(version_id),
        "chunk_id": str(point.chunk_id),
        "parent_id": str(point.parent_id) if point.parent_id else None,
        "doc_title": doc_title,
        "section_path": point.section_path,
        "page_start": point.page_start,
        "page_end": point.page_end,
        "chunk_type": point.chunk_type,
        "text_hash": point.text_hash,
        "is_published": False,
    }


async def ensure_collection(
    client: AsyncQdrantClient, *, collection: str, dense_dim: int
) -> None:
    """幂等创建 collection（已存在则删除重建——构建重试的干净起点）。"""

    if await client.collection_exists(collection):
        await client.delete_collection(collection)
    await client.create_collection(
        collection,
        vectors_config={
            "dense": qmodels.VectorParams(size=dense_dim, distance=qmodels.Distance.COSINE)
        },
        sparse_vectors_config={
            "sparse": qmodels.SparseVectorParams(index=qmodels.SparseIndexParams(on_disk=False))
        },
    )


async def upsert_points(
    client: AsyncQdrantClient,
    *,
    collection: str,
    tenant_id: UUID,
    version_id: UUID,
    doc_title: str,
    points: Sequence[IndexPoint],
) -> int:
    """批量 upsert（named vectors + payload）；返回写入点数。"""

    if not points:
        return 0
    qpoints = [
        qmodels.PointStruct(
            id=str(point.chunk_id),
            vector={
                "dense": point.dense,
                "sparse": qmodels.SparseVector(
                    indices=point.sparse.indices, values=point.sparse.values
                ),
            },
            payload=build_payload(
                tenant_id=tenant_id,
                document_id=point.document_id,
                version_id=version_id,
                point=point,
                doc_title=doc_title,
            ),
        )
        for point in points
    ]
    try:
        await client.upsert(collection, points=qpoints, wait=True)
    except Exception as exc:  # qdrant-client 异常类型跨版本不稳，统一按索引失败收口
        logger.warning("Qdrant upsert 失败：%s.%s", type(exc).__module__, type(exc).__name__)
        raise IndexError_("INDEX_UPSERT_FAILED", "向量索引写入失败") from exc
    return len(qpoints)


async def count_points(client: AsyncQdrantClient, *, collection: str) -> int:
    """精确点数（VALIDATING 对账用）。"""

    try:
        result = await client.count(collection, exact=True)
    except Exception as exc:
        logger.warning("Qdrant count 失败：%s.%s", type(exc).__module__, type(exc).__name__)
        raise IndexError_("INDEX_COUNT_FAILED", "向量索引计数失败") from exc
    return int(result.count)


async def search_points(
    client: AsyncQdrantClient,
    *,
    collection: str,
    dense: list[float],
    sparse: SparseVector,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Qdrant 混合检索（dense + sparse → 服务端 RRF 融合，design §7.5）。

    返回 `[{"chunk_id": str, "score": float, "payload": dict}]`，按 RRF 分数降序。
    """

    try:
        response = await client.query_points(
            collection,
            prefetch=[
                qmodels.Prefetch(query=dense, using="dense", limit=top_k * 2),
                qmodels.Prefetch(
                    query=qmodels.SparseVector(
                        indices=sparse.indices, values=sparse.values
                    ),
                    using="sparse",
                    limit=top_k * 2,
                ),
            ],
            query=qmodels.FusionQuery(fusion=qmodels.Fusion.RRF),
            limit=top_k,
            with_payload=True,
        )
    except Exception as exc:
        logger.warning("Qdrant search 失败：%s.%s", type(exc).__module__, type(exc).__name__)
        raise IndexError_("SEARCH_FAILED", "向量检索失败") from exc
    results = []
    for point in response.points:
        payload = point.payload or {}
        results.append(
            {
                "chunk_id": str(point.id),
                "score": point.score,
                "payload": payload,
            }
        )
    return results


def _points_to_hits(response: Any) -> list[dict[str, Any]]:
    """查询响应 → `[{"chunk_id", "score"}]`（保持 Qdrant 的降序）。"""

    return [{"chunk_id": str(point.id), "score": float(point.score)} for point in response.points]


async def search_dense(
    client: AsyncQdrantClient,
    *,
    collection: str,
    dense: list[float],
    limit: int,
) -> list[dict[str, Any]]:
    """单路 dense 检索（KB-07 逐路分数旁路；与融合查询同 collection 定域）。

    返回 `[{"chunk_id": str, "score": float}]`，按分数降序；不带 payload
    （归位只按 id，元数据由 PG 回查）。
    """

    try:
        response = await client.query_points(
            collection, query=dense, using="dense", limit=limit, with_payload=False
        )
    except Exception as exc:
        logger.warning("Qdrant dense 检索失败：%s.%s", type(exc).__module__, type(exc).__name__)
        raise IndexError_("SEARCH_FAILED", "向量检索失败") from exc
    return _points_to_hits(response)


async def search_sparse(
    client: AsyncQdrantClient,
    *,
    collection: str,
    sparse: SparseVector,
    limit: int,
) -> list[dict[str, Any]]:
    """单路 sparse（BM25）检索（KB-07 逐路分数旁路）；返回形状同 `search_dense`。"""

    try:
        response = await client.query_points(
            collection,
            query=qmodels.SparseVector(indices=sparse.indices, values=sparse.values),
            using="sparse",
            limit=limit,
            with_payload=False,
        )
    except Exception as exc:
        logger.warning("Qdrant sparse 检索失败：%s.%s", type(exc).__module__, type(exc).__name__)
        raise IndexError_("SEARCH_FAILED", "向量检索失败") from exc
    return _points_to_hits(response)
