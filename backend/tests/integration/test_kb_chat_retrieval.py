"""chat 侧检索服务集成测试（M4-AI01b 步 1）。

真实 PG（活跃版本发现 / 人群解析 / 证据文本回查）+ stub Qdrant（fan-out 编排与
ACL filter 下沉断言）+ stub 嵌入（维度无关）。TD-361 另有一条双租户用例在调用
记录层之后使用真实 Qdrant，验证 collection 访问范围、真实混合检索与正文回查。
Qdrant 端到端检索质量与真实 ACL 召回由 BS-M4-01/02（真实栈）覆盖。
"""

import hashlib
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from qdrant_client import AsyncQdrantClient

from app.db.models.kb import (
    KbChunk,
    KbDocument,
    KbDocumentVersion,
    KbPublishAlias,
    KbUploadBatch,
)
from app.db.session import db_session
from app.harness.chat_tools import KB_READ_CHUNK, TurnToolState, build_turn_tools
from app.harness.contracts import TenantContext, ToolCall
from app.kb.chat_retrieval import ChatRetrievalError, ChatRetriever
from app.kb.embedding import SparseVector, TEIEmbedder
from app.kb.indexer import (
    IndexError_,
    IndexPoint,
    collection_name,
    ensure_collection,
    upsert_points,
)
from tests.support.auth import seed_tenant_user
from tests.support.db import make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_env import PASSWORD, SCHOOL_A, SCHOOL_B, KbEnv, kb_env

pytestmark = [pytest.mark.integration]

CHAT_USER = "kb-chat-user"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def env(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[KbEnv]:
    async with kb_env(monkeypatch, tmp_path) as value:
        yield value


@pytest.fixture
async def chat_user_id(env: KbEnv) -> UUID:
    """租户内 school_admin 用户（人群解析=role+self+all，token 非空）。"""

    engine = make_test_engine()
    try:
        tenant_id, user_id = await seed_tenant_user(
            engine, school_code=SCHOOL_A, login_id=CHAT_USER, password=PASSWORD
        )
        assert tenant_id == env.tenant_a
        return user_id
    finally:
        await engine.dispose()


@dataclass
class _Point:
    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Response:
    points: list[Any]


class StubQdrant:
    """按 collection 名路由的 stub（记录调用与 filter；可注入异常）。"""

    def __init__(self, routes: dict[str, list[Any] | Exception]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, object]] = []

    async def query_points(self, collection: str, **kwargs: Any) -> object:
        self.calls.append((collection, kwargs.get("query_filter")))
        result = self.routes.get(collection)
        if isinstance(result, Exception):
            raise result
        return _Response(points=list(result or []))


class StubEmbedder(TEIEmbedder):
    """恒返回固定向量（stub Qdrant 不校验维度；继承真实类过类型面）。"""

    def __init__(self) -> None:
        super().__init__(base_url="http://127.0.0.1:1", timeout_seconds=1, batch_size=1)

    async def embed(self, texts: Any) -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]


class _RecordingRealQdrant:
    """记录 collection 后委托真实 AsyncQdrantClient；不替代任何存储行为。"""

    def __init__(self, client: AsyncQdrantClient) -> None:
        self.client = client
        self.calls: list[str] = []

    async def query_points(self, collection: str, **kwargs: Any) -> object:
        self.calls.append(collection)
        return await self.client.query_points(collection, **kwargs)


class _UnitEmbedder(TEIEmbedder):
    """真实 Qdrant 用例使用固定非零单位向量，dense/sparse 均可稳定命中。"""

    def __init__(self) -> None:
        super().__init__(base_url="http://127.0.0.1:1", timeout_seconds=1, batch_size=1)

    async def embed(self, texts: Any) -> list[list[float]]:
        return [[1.0] + [0.0] * 7 for _ in texts]


def _index_points(chunk_ids: list[str]) -> list[IndexPoint]:
    return [
        IndexPoint(
            chunk_id=UUID(chunk_id),
            parent_id=None,
            document_id=uuid4(),
            chunk_type="text",
            section_path=None,
            page_start=None,
            page_end=None,
            text_hash=hashlib.sha256(chunk_id.encode()).hexdigest(),
            dense=[1.0] + [0.0] * 7,
            sparse=SparseVector(indices=[1], values=[1.0]),
        )
        for chunk_id in chunk_ids
    ]


async def _seed_published_doc(tenant_id: UUID, user_id: UUID, title: str) -> tuple[UUID, list[str]]:
    """直插最小发布面：PUBLISHED 文档 + READY 版本 + alias + 2 个子块；返回版本 id。"""

    async with db_session(tenant_id=tenant_id) as session:
        batch = KbUploadBatch(
            tenant_id=tenant_id,
            idempotency_key=uuid4().hex,
            request_hash=uuid4().hex,
            file_count=1,
            total_bytes=64,
            created_by=user_id,
        )
        session.add(batch)
        await session.flush()
        doc = KbDocument(
            tenant_id=tenant_id,
            title=title,
            status="PUBLISHED",
            created_by=user_id,
            upload_batch_id=batch.id,
            upload_file_index=0,
        )
        session.add(doc)
        await session.flush()
        version = KbDocumentVersion(
            tenant_id=tenant_id,
            document_id=doc.id,
            version_no=1,
            source_object_key=f"kb/{doc.id}/v1",
            source_filename="seed.md",
            source_hash=uuid4().hex,
            mime="text/markdown",
            size_bytes=64,
            status="READY",
            created_by=user_id,
        )
        session.add(version)
        await session.flush()
        session.add(
            KbPublishAlias(
                tenant_id=tenant_id,
                document_id=doc.id,
                alias=f"kb-doc-{doc.id}",
                collection=collection_name(tenant_id, version.id),
                version_id=version.id,
            )
        )
        chunk_objects: list[KbChunk] = []
        for index, body in enumerate((f"{title}第一条内容。", f"{title}第二条内容。")):
            chunk = KbChunk(
                tenant_id=tenant_id,
                document_version_id=version.id,
                parent_id=None,
                chunk_type="child",
                chunk_index=index,
                text=body,
                text_markdown=body,
                section_path=None,
                page_start=None,
                page_end=None,
                chunk_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                embedding_status="ready",
            )
            session.add(chunk)
            chunk_objects.append(chunk)
        await session.flush()
        await session.commit()
        return version.id, [str(chunk.id) for chunk in chunk_objects]


def _points_for(version_id: UUID, chunk_ids: list[str], *, doc_title: str = "校历") -> list[_Point]:
    """按 payload 十四字段构造 stub 命中（doc_title/section/page 同构真实 payload）。"""

    return [
        _Point(
            id=chunk_id,
            score=1.0 / (position + 1),
            payload={
                "tenant_id_hash": "hash",
                "document_id": "d",
                "version_id": str(version_id),
                "chunk_id": chunk_id,
                "parent_id": None,
                "doc_title": doc_title,
                "section_path": "第一章",
                "page_start": position + 1,
                "page_end": position + 1,
                "chunk_type": "text",
                "text_hash": "h",
                "acl_tokens": ["all"],
                "is_human_edited": False,
                "is_published": True,
            },
        )
        for position, chunk_id in enumerate(chunk_ids)
    ]


async def test_no_published_versions_returns_empty_without_qdrant(
    env: KbEnv, chat_user_id: UUID
) -> None:
    """租户内无发布版本：空结果且不触达 Qdrant（发现层短路）。"""

    tenant_id = env.tenant_a
    stub = StubQdrant(routes={})
    retriever = ChatRetriever(
        embedder=StubEmbedder(),
        qdrant_url="http://127.0.0.1:9999",
        top_k=5,
        client=stub,  # type: ignore[arg-type]
    )

    hits = await retriever.search_hits(tenant_id=tenant_id, user_id=chat_user_id, query="校历")

    assert hits == []
    assert stub.calls == []


async def test_search_returns_evidence_with_text_and_filter(env: KbEnv, chat_user_id: UUID) -> None:
    """发布版本 + 人群 token 非空：fan-out 命中 → PG 回查文本 → 证据齐字段。"""

    tenant_id = env.tenant_a
    version_id, chunk_ids = await _seed_published_doc(tenant_id, chat_user_id, "校历")
    stub = StubQdrant(
        routes={collection_name(tenant_id, version_id): _points_for(version_id, chunk_ids)}
    )
    retriever = ChatRetriever(
        embedder=StubEmbedder(),
        qdrant_url="http://127.0.0.1:9999",
        top_k=5,
        client=stub,  # type: ignore[arg-type]
    )

    hits = await retriever.search_hits(tenant_id=tenant_id, user_id=chat_user_id, query="校历")
    assert [item["chunk_id"] for item in hits] == chunk_ids
    evidence = await retriever.load_texts(tenant_id, hits)
    assert len(evidence) == 2
    assert [item.chunk_id for item in evidence] == chunk_ids
    assert evidence[0].text == "校历第一条内容。"
    assert evidence[0].doc_title == "校历"
    assert evidence[0].section_path == "第一章"
    assert evidence[0].version_id == str(version_id)
    # Qdrant 调用：预期 collection + ACL filter 非空（人群 token 下沉）
    assert len(stub.calls) == 1
    called_collection, called_filter = stub.calls[0]
    assert called_collection == collection_name(tenant_id, version_id)
    assert called_filter is not None


async def test_unknown_user_is_fail_closed_without_qdrant(env: KbEnv, chat_user_id: UUID) -> None:
    """人群解析空集（users 行缺失）：fail-closed 空结果，不发 Qdrant 查询。"""

    tenant_id = env.tenant_a
    await _seed_published_doc(tenant_id, chat_user_id, "校历")
    stub = StubQdrant(routes={})
    retriever = ChatRetriever(
        embedder=StubEmbedder(),
        qdrant_url="http://127.0.0.1:9999",
        top_k=5,
        client=stub,  # type: ignore[arg-type]
    )

    hits = await retriever.search_hits(tenant_id=tenant_id, user_id=uuid4(), query="校历")

    assert hits == []
    assert stub.calls == []


async def test_partial_collection_failure_skips_and_serves(env: KbEnv, chat_user_id: UUID) -> None:
    """单 collection 失败跳过（诚实降级）；全部失败才抛错。"""

    tenant_id = env.tenant_a
    version_id, _ = await _seed_published_doc(tenant_id, chat_user_id, "校历")
    healthy, healthy_chunks = await _seed_published_doc(tenant_id, chat_user_id, "财务制度")
    stub = StubQdrant(
        routes={
            collection_name(tenant_id, version_id): IndexError_("SEARCH_FAILED", "boom"),
            collection_name(tenant_id, healthy): _points_for(
                healthy, healthy_chunks, doc_title="财务制度"
            ),
        }
    )
    retriever = ChatRetriever(
        embedder=StubEmbedder(),
        qdrant_url="http://127.0.0.1:9999",
        top_k=5,
        client=stub,  # type: ignore[arg-type]
    )

    hits = await retriever.search_hits(tenant_id=tenant_id, user_id=chat_user_id, query="制度")
    evidence = await retriever.load_texts(tenant_id, hits)

    assert len(evidence) == 2
    assert {item.doc_title for item in evidence} == {"财务制度"}


async def test_all_collections_failed_raises(env: KbEnv, chat_user_id: UUID) -> None:
    """全部 collection 失败：抛 ChatRetrievalError（loop 层转 error 事件）。"""

    tenant_id = env.tenant_a
    version_id, _ = await _seed_published_doc(tenant_id, chat_user_id, "校历")
    stub = StubQdrant(
        routes={collection_name(tenant_id, version_id): IndexError_("SEARCH_FAILED", "boom")}
    )
    retriever = ChatRetriever(
        embedder=StubEmbedder(),
        qdrant_url="http://127.0.0.1:9999",
        top_k=5,
        client=stub,  # type: ignore[arg-type]
    )

    with pytest.raises(ChatRetrievalError) as excinfo:
        await retriever.search_hits(tenant_id=tenant_id, user_id=chat_user_id, query="校历")
    assert excinfo.value.code == "CHAT_SEARCH_FAILED"


async def test_payload_chunk_missing_in_pg_is_dropped(env: KbEnv, chat_user_id: UUID) -> None:
    """索引在、库行不在：回查丢弃（引用存在性校验），不产出幽灵引用。"""

    tenant_id = env.tenant_a
    version_id, _ = await _seed_published_doc(tenant_id, chat_user_id, "校历")
    ghost = "44444444-4444-4444-8444-444444444444"
    stub = StubQdrant(
        routes={collection_name(tenant_id, version_id): _points_for(version_id, [ghost])}
    )
    retriever = ChatRetriever(
        embedder=StubEmbedder(),
        qdrant_url="http://127.0.0.1:9999",
        top_k=5,
        client=stub,  # type: ignore[arg-type]
    )

    hits = await retriever.search_hits(tenant_id=tenant_id, user_id=chat_user_id, query="校历")
    evidence = await retriever.load_texts(tenant_id, hits)

    assert evidence == []


async def test_tenant_a_never_queries_or_reads_tenant_b_collection(
    env: KbEnv, chat_user_id: UUID
) -> None:
    """真实 PG 发布面 + 记录 collection 的 Qdrant 替身：A 只访问 A 的版本。

    B 的真实 chunk 与随机不存在 id 经同一证据白名单返回完全相同的拒绝，
    不形成存在性或正文侧信道。
    """

    engine = make_test_engine("admin")
    try:
        _tenant_b, user_b = await seed_tenant_user(
            engine,
            school_code=SCHOOL_B,
            login_id="td361-retrieval-b",
            password=PASSWORD,
        )
    finally:
        await engine.dispose()

    version_a, chunks_a = await _seed_published_doc(env.tenant_a, chat_user_id, "A校校历")
    version_b, chunks_b = await _seed_published_doc(env.tenant_b, user_b, "B校机密名单")
    collection_a = collection_name(env.tenant_a, version_a)
    collection_b = collection_name(env.tenant_b, version_b)
    stub = StubQdrant(
        routes={
            collection_a: _points_for(version_a, chunks_a, doc_title="A校校历"),
            collection_b: _points_for(version_b, chunks_b, doc_title="B校机密名单"),
        }
    )
    retriever = ChatRetriever(
        embedder=StubEmbedder(),
        qdrant_url="http://127.0.0.1:9999",
        top_k=5,
        client=stub,  # type: ignore[arg-type]
    )

    hits = await retriever.search_hits(tenant_id=env.tenant_a, user_id=chat_user_id, query="校历")
    evidence = await retriever.load_texts(env.tenant_a, hits)

    assert {hit["chunk_id"] for hit in hits} == set(chunks_a)
    assert [call[0] for call in stub.calls] == [collection_a]
    assert collection_b not in [call[0] for call in stub.calls]
    assert {item.text for item in evidence} == {"A校校历第一条内容。", "A校校历第二条内容。"}

    # 即使索引污染让 A 回合命中 B chunk_id，PG 租户过滤也不会返回正文。
    b_point = _points_for(version_b, [chunks_b[0]], doc_title="B校机密名单")[0]
    poisoned = await retriever.load_texts(
        env.tenant_a,
        [{"chunk_id": b_point.id, "score": b_point.score, "payload": b_point.payload}],
    )
    assert poisoned == []

    state = TurnToolState()
    state.set_evidence(evidence)
    handler = build_turn_tools(retriever=retriever, state=state).get_handler(KB_READ_CHUNK)
    ctx = TenantContext(
        tenant_id=env.tenant_a,
        user_id=chat_user_id,
        permissions=frozenset({"campus.chat.use"}),
    )
    b_chunk = await handler(ToolCall(tool=KB_READ_CHUNK, args={"chunk_id": chunks_b[0]}), ctx)
    random_chunk = await handler(ToolCall(tool=KB_READ_CHUNK, args={"chunk_id": str(uuid4())}), ctx)
    assert b_chunk.ok is False and random_chunk.ok is False
    assert b_chunk.error_code == random_chunk.error_code == "CHUNK_NOT_IN_EVIDENCE"
    assert b_chunk.data == random_chunk.data
    assert b_chunk.redacted_summary == random_chunk.redacted_summary


@pytest.mark.asyncio
async def test_tenant_a_real_qdrant_never_queries_tenant_b_collection(
    env: KbEnv, chat_user_id: UUID
) -> None:
    """真实 PG + 真实 Qdrant：A 只访问 A collection，B collection 调用数为零。"""

    qdrant_url = os.environ.get("TEST_QDRANT_URL")
    if not qdrant_url:
        pytest.skip("TEST_QDRANT_URL 未设置；先启动真实测试栈")

    engine = make_test_engine("admin")
    try:
        _tenant_b, user_b = await seed_tenant_user(
            engine,
            school_code=SCHOOL_B,
            login_id="td361-real-qdrant-b",
            password=PASSWORD,
        )
    finally:
        await engine.dispose()

    version_a, chunks_a = await _seed_published_doc(env.tenant_a, chat_user_id, "A校校历")
    version_b, chunks_b = await _seed_published_doc(env.tenant_b, user_b, "B校机密名单")
    collection_a = collection_name(env.tenant_a, version_a)
    collection_b = collection_name(env.tenant_b, version_b)
    client = AsyncQdrantClient(url=qdrant_url)
    recorder = _RecordingRealQdrant(client)
    try:
        await ensure_collection(client, collection=collection_a, dense_dim=8)
        await ensure_collection(client, collection=collection_b, dense_dim=8)
        await upsert_points(
            client,
            collection=collection_a,
            tenant_id=env.tenant_a,
            version_id=version_a,
            doc_title="A校校历",
            points=_index_points(chunks_a),
            acl_tokens=["all"],
        )
        await upsert_points(
            client,
            collection=collection_b,
            tenant_id=env.tenant_b,
            version_id=version_b,
            doc_title="B校机密名单",
            points=_index_points(chunks_b),
            acl_tokens=["all"],
        )
        retriever = ChatRetriever(
            embedder=_UnitEmbedder(),
            qdrant_url=qdrant_url,
            top_k=5,
            client=recorder,  # type: ignore[arg-type]
        )

        hits = await retriever.search_hits(
            tenant_id=env.tenant_a, user_id=chat_user_id, query="校历"
        )
        evidence = await retriever.load_texts(env.tenant_a, hits)

        assert recorder.calls == [collection_a]
        assert {hit["chunk_id"] for hit in hits} == set(chunks_a)
        assert {item.text for item in evidence} == {
            "A校校历第一条内容。",
            "A校校历第二条内容。",
        }
        assert await client.collection_exists(collection_b)
    finally:
        await client.delete_collection(collection_a)
        await client.delete_collection(collection_b)
        await client.close()
