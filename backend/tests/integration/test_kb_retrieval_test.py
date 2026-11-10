"""M3 KB-07A 检索测试集成测试（真实 PG + 真实 Qdrant；解析 fake、嵌入/重排 fake）。

覆盖简报 `docs/design-briefs/M3-KB-07.md` §5 集成清单：
- 确定性 top-1（词袋 embedder 两侧同源，query=子块全文 → cosine 1.0）与行结构
  不变量（final_rank 连续、每行至少一路命中、元数据与 PG 对账）；
- top_k 裁剪；重排 applied 稳定翻转 / timeout→skipped / failed 降级保融合序；
- ACL 预留参数 422（fail-closed）；query/top_k 校验 422；
- 404（不存在/跨租/self 范围外同码不泄露）、403（缺 view_chunks，view 不能顶替）、
  未索引 409；嵌入失败与 Qdrant 未配置 502。
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.kb import retrieval_test
from app.kb.embedding import EmbeddingError
from tests.support.db import upgrade_test_database
from tests.support.dsn import assert_test_database_dsn
from tests.support.kb_env import (
    KB_B_CHUNK_VIEWER,
    KB_CHUNK_SELF,
    KB_CHUNK_VIEWER,
    NO_PERM,
    SCHOOL_B,
    UPLOADER,
    KbEnv,
    execute,
    kb_env,
    login,
    make_client,
    rows,
)
from tests.support.kb_publish_seed import (
    seed_test_passed_version as _seed_test_passed_version,
)
from tests.support.kb_retrieval_seed import (
    FailingReranker,
    FakeReranker,
    TimingOutReranker,
    TokenOverlapEmbedder,
    patch_query_embedder,
)

pytestmark = [pytest.mark.integration]

URL = "/api/v1/school/kb/versions/{version_id}/retrieval-test"

RICH_MARKDOWN = """# 操作手册

## 第一章 安装

安装步骤1完成。安装步骤2完成。安装步骤3完成。安装步骤4完成。安装步骤5完成。
安装步骤6完成。安装步骤7完成。安装步骤8完成。安装步骤9完成。安装步骤10完成。

## 第二章 配置

配置项1已生效。配置项2已生效。配置项3已生效。配置项4已生效。配置项5已生效。
配置项6已生效。配置项7已生效。配置项8已生效。配置项9已生效。配置项10已生效。
配置项11已生效。配置项12已生效。配置项13已生效。配置项14已生效。配置项15已生效。
配置项16已生效。配置项17已生效。配置项18已生效。配置项19已生效。配置项20已生效。
配置项21已生效。配置项22已生效。配置项23已生效。配置项24已生效。配置项25已生效。
配置项26已生效。配置项27已生效。配置项28已生效。配置项29已生效。配置项30已生效。
配置项31已生效。配置项32已生效。配置项33已生效。配置项34已生效。配置项35已生效。
配置项36已生效。配置项37已生效。配置项38已生效。配置项39已生效。配置项40已生效。
配置项41已生效。配置项42已生效。配置项43已生效。配置项44已生效。配置项45已生效。

| 参数 | 默认值 |
| --- | --- |
| 超时 | 30 |
| 重试 | 3 |

```python
print("hello kb")
```

## 第三章 常见问题

问题1已解决。问题2已解决。问题3已解决。问题4已解决。问题5已解决。
"""


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
    async with kb_env(
        monkeypatch, tmp_path, overrides={"QDRANT_URL": "http://127.0.0.1:56333"}
    ) as value:
        yield value


@pytest.fixture
async def client(env: KbEnv) -> AsyncIterator[AsyncClient]:
    async with make_client() as ac:
        yield ac


async def _seed(env: KbEnv, monkeypatch: pytest.MonkeyPatch) -> tuple[UUID, UUID]:
    """构建多章节 READY 版本（构建/查询两侧同源词袋 embedder）。"""

    return await _seed_test_passed_version(
        env,
        monkeypatch,
        title="检索测试用文档",
        markdown=RICH_MARKDOWN,
        embedder=TokenOverlapEmbedder(),
    )


async def _child_chunks(version_id: UUID) -> list[dict[str, Any]]:
    return await rows(
        "select id, chunk_index, text, section_path from kb_chunks "
        "where document_version_id = :v and chunk_type = 'child' order by chunk_index",
        v=str(version_id),
    )


async def test_exact_chunk_query_is_top_reference_with_route_scores(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """query=子块全文 → 该子块 top-1（dense/sparse 双路第 1）+ 行结构不变量。"""

    _doc, version_id = await _seed(env, monkeypatch)
    patch_query_embedder(monkeypatch)
    children = await _child_chunks(version_id)
    assert len(children) >= 5, "种子应产出多个子块（只索子块）"
    target = children[2]
    headers = await login(client, KB_CHUNK_VIEWER)

    resp = await client.post(
        URL.format(version_id=version_id), json={"query": target["text"]}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version_id"] == str(version_id)
    assert body["query"] == target["text"]
    assert body["top_k"] == 10
    assert body["with_rerank"] is False
    assert body["rerank_status"] == "disabled"
    assert body["pool_size"] == len(body["results"])
    assert body["latency_ms"] >= 0

    results = body["results"]
    assert results
    top = results[0]
    assert top["chunk_id"] == str(target["id"])
    assert top["final_rank"] == 1
    assert top["is_final_reference"] is True
    # 词袋同正文 → dense 最高（build 侧向量多含章节路径 token，非严格 1.0）；
    # query token 全集命中 → sparse 第 1
    assert top["dense"] is not None and top["dense"]["rank"] == 1
    assert top["dense"]["score"] > 0.9
    assert top["sparse"] is not None and top["sparse"]["rank"] == 1
    assert top["rrf_score"] > 0
    assert top["rerank_score"] is None
    # 元数据与 PG 对账
    assert top["chunk_index"] == target["chunk_index"]
    assert top["section_path"] == target["section_path"]
    assert top["text_preview"] == target["text"][:200]
    # 结构不变量：名次连续、最终引用计数 = min(top_k, 行数)、每行至少一路命中
    assert [row["final_rank"] for row in results] == list(range(1, len(results) + 1))
    assert sum(1 for row in results if row["is_final_reference"]) == min(10, len(results))
    assert all(row["dense"] is not None or row["sparse"] is not None for row in results)


async def test_top_k_trims_results(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await _seed(env, monkeypatch)
    patch_query_embedder(monkeypatch)
    target = (await _child_chunks(version_id))[0]
    headers = await login(client, KB_CHUNK_VIEWER)

    resp = await client.post(
        URL.format(version_id=version_id),
        json={"query": target["text"], "top_k": 2},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["results"]) == 2
    assert [row["final_rank"] for row in body["results"]] == [1, 2]
    assert all(row["is_final_reference"] for row in body["results"])


async def test_with_rerank_applied_stably_reorders_fused_results(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """重排生效：升序分数 → 融合序稳定翻转；最终引用 = 重排后前 top_k。"""

    _doc, version_id = await _seed(env, monkeypatch)
    patch_query_embedder(monkeypatch)
    target = (await _child_chunks(version_id))[0]
    fake = FakeReranker()
    monkeypatch.setattr(retrieval_test, "_build_reranker_for_test", lambda settings: fake)
    headers = await login(client, KB_CHUNK_VIEWER)

    resp = await client.post(
        URL.format(version_id=version_id),
        json={"query": target["text"], "with_rerank": True},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["with_rerank"] is True
    assert body["rerank_status"] == "applied"
    results = body["results"]
    assert results

    # fake 收到的候选 = 融合池全文（融合序）；升序分数 → 结果是融合序的稳定翻转
    texts = fake.text_batches[0]
    assert fake.queries == [target["text"]]
    assert len(texts) == len(results)
    assert [row["text_preview"] for row in results] == [text[:200] for text in reversed(texts)]
    assert [row["final_rank"] for row in results] == list(range(1, len(results) + 1))
    scores = [row["rerank_score"] for row in results]
    assert all(score is not None for score in scores)
    assert scores == sorted(scores, reverse=True)
    assert sum(1 for row in results if row["is_final_reference"]) == min(10, len(results))


async def test_with_rerank_timeout_degrades_to_fused_order(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """重排超时 → skipped 降级为融合序（永不 5xx）；与同池无重排结果逐行一致。"""

    _doc, version_id = await _seed(env, monkeypatch)
    patch_query_embedder(monkeypatch)
    target = (await _child_chunks(version_id))[0]
    monkeypatch.setattr(
        retrieval_test, "_build_reranker_for_test", lambda settings: TimingOutReranker()
    )
    headers = await login(client, KB_CHUNK_VIEWER)

    degraded = await client.post(
        URL.format(version_id=version_id),
        json={"query": target["text"], "top_k": 20, "with_rerank": True},
        headers=headers,
    )
    assert degraded.status_code == 200, degraded.text
    body = degraded.json()
    assert body["rerank_status"] == "skipped"
    assert all(row["rerank_score"] is None for row in body["results"])

    # 同池（pool=20）无重排运行：同集合确定性 → 顺序与降级结果逐行一致
    monkeypatch.setattr(retrieval_test, "_build_reranker_for_test", lambda settings: FakeReranker())
    fused_only = await client.post(
        URL.format(version_id=version_id),
        json={"query": target["text"], "top_k": 20, "with_rerank": False},
        headers=headers,
    )
    assert fused_only.status_code == 200, fused_only.text
    assert [row["chunk_id"] for row in body["results"]] == [
        row["chunk_id"] for row in fused_only.json()["results"]
    ]


async def test_with_rerank_error_degrades_to_fused_order(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await _seed(env, monkeypatch)
    patch_query_embedder(monkeypatch)
    target = (await _child_chunks(version_id))[0]
    monkeypatch.setattr(
        retrieval_test, "_build_reranker_for_test", lambda settings: FailingReranker()
    )
    headers = await login(client, KB_CHUNK_VIEWER)

    resp = await client.post(
        URL.format(version_id=version_id),
        json={"query": target["text"], "top_k": 20, "with_rerank": True},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rerank_status"] == "failed"
    assert all(row["rerank_score"] is None for row in body["results"])
    assert [row["final_rank"] for row in body["results"]] == list(
        range(1, len(body["results"]) + 1)
    )


async def test_with_rerank_unexpected_exception_degrades_not_500(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """审查 Major 1：端口实现抛非 RerankError 异常（如畸形 URL 的 InvalidURL）也降级。"""

    class _ExplodingReranker:
        async def rerank(self, *, query: str, texts: list[str]) -> list[float]:
            raise ValueError("unexpected port bug")

    _doc, version_id = await _seed(env, monkeypatch)
    patch_query_embedder(monkeypatch)
    target = (await _child_chunks(version_id))[0]
    monkeypatch.setattr(
        retrieval_test, "_build_reranker_for_test", lambda settings: _ExplodingReranker()
    )
    headers = await login(client, KB_CHUNK_VIEWER)

    resp = await client.post(
        URL.format(version_id=version_id),
        json={"query": target["text"], "with_rerank": True},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rerank_status"] == "failed"
    assert all(row["rerank_score"] is None for row in body["results"])


async def test_with_rerank_count_mismatch_degrades_not_500(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """复审 Major 1：端口违约返回条数不符的有效分数 → apply_rerank 的守卫降级，不 500。"""

    class _WrongLengthReranker:
        async def rerank(self, *, query: str, texts: list[str]) -> list[float]:
            return [0.5] * (len(texts) + 1)  # 条数不符的有效分数表

    _doc, version_id = await _seed(env, monkeypatch)
    patch_query_embedder(monkeypatch)
    target = (await _child_chunks(version_id))[0]
    monkeypatch.setattr(
        retrieval_test, "_build_reranker_for_test", lambda settings: _WrongLengthReranker()
    )
    headers = await login(client, KB_CHUNK_VIEWER)

    resp = await client.post(
        URL.format(version_id=version_id),
        json={"query": target["text"], "with_rerank": True},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rerank_status"] == "failed"
    assert all(row["rerank_score"] is None for row in body["results"])
    # 降级 = 融合序（final_rank 连续且未发生重排翻转）
    assert [row["final_rank"] for row in body["results"]] == list(
        range(1, len(body["results"]) + 1)
    )


async def test_rerank_pool_below_top_k_is_floored(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """审查 Minor 1：误配 pool<top_k 时兜底 max(pool, top_k)，最终引用不少给。"""

    _doc, version_id = await _seed(env, monkeypatch)
    patch_query_embedder(monkeypatch)
    target = (await _child_chunks(version_id))[0]
    fake = FakeReranker()
    monkeypatch.setattr(retrieval_test, "_build_reranker_for_test", lambda settings: fake)
    settings_ns = SimpleNamespace(
        qdrant_url="http://127.0.0.1:56333",
        tei_embed_url="http://127.0.0.1:8081",
        tei_embed_timeout_seconds=5.0,
        kb_embed_batch_size=8,
        tei_rerank_url="http://127.0.0.1:8082",
        tei_rerank_timeout_seconds=5.0,
        kb_retrieval_test_rerank_pool=2,  # 误配：小于 top_k=5
    )
    monkeypatch.setattr(retrieval_test, "get_settings", lambda: settings_ns)
    headers = await login(client, KB_CHUNK_VIEWER)

    resp = await client.post(
        URL.format(version_id=version_id),
        json={"query": target["text"], "top_k": 5, "with_rerank": True},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["top_k"] == 5
    # 池被兜到 5：重排收到的候选至少 top_k 条（词袋种子子块数 ≥5）
    assert len(fake.text_batches[0]) >= 5
    assert sum(1 for row in body["results"] if row["is_final_reference"]) == min(
        5, len(body["results"])
    )


async def test_acl_reserved_params_are_rejected_fail_closed(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """KB-09 契约预留：任一 ACL 视角参数设值即 422（绝不静默忽略）。"""

    _doc, version_id = await _seed(env, monkeypatch)
    headers = await login(client, KB_CHUNK_VIEWER)
    for payload in (
        {"query": "q", "as_user_id": str(uuid4())},
        {"query": "q", "as_audiences": ["department:whatever"]},
        {"query": "q", "ignore_acl": True},
    ):
        resp = await client.post(
            URL.format(version_id=version_id), json=payload, headers=headers
        )
        assert resp.status_code == 422, f"{payload} 应 422：{resp.text}"
        assert resp.json()["detail"]["code"] == "RETRIEVAL_TEST_ACL_UNAVAILABLE"


async def test_query_and_top_k_validation(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await _seed(env, monkeypatch)
    headers = await login(client, KB_CHUNK_VIEWER)

    blank = await client.post(
        URL.format(version_id=version_id), json={"query": "   "}, headers=headers
    )
    assert blank.status_code == 422
    assert blank.json()["detail"]["code"] == "RETRIEVAL_TEST_QUERY_REQUIRED"

    for payload in ({"query": ""}, {"query": "长" * 301}, {"query": "q", "top_k": 0}, {"query": "q", "top_k": 21}):
        resp = await client.post(
            URL.format(version_id=version_id), json=payload, headers=headers
        )
        assert resp.status_code == 422, f"{payload} 应 422：{resp.text}"


async def test_version_not_found_and_cross_tenant_same_code(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await _seed(env, monkeypatch)
    headers = await login(client, KB_CHUNK_VIEWER)

    missing = await client.post(
        URL.format(version_id=uuid4()), json={"query": "q"}, headers=headers
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "VERSION_NOT_FOUND"

    b_headers = await login(client, KB_B_CHUNK_VIEWER, school_code=SCHOOL_B)
    cross = await client.post(
        URL.format(version_id=version_id), json={"query": "q"}, headers=b_headers
    )
    assert cross.status_code == 404
    assert cross.json()["detail"]["code"] == "VERSION_NOT_FOUND"


async def test_self_scope_viewer_cannot_see_others_version(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await _seed(env, monkeypatch)  # created_by = kb-uploader
    self_headers = await login(client, KB_CHUNK_SELF)
    resp = await client.post(
        URL.format(version_id=version_id), json={"query": "q"}, headers=self_headers
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "VERSION_NOT_FOUND"


async def test_endpoint_requires_view_chunks_permission(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await _seed(env, monkeypatch)
    no_perm_headers = await login(client, NO_PERM)
    view_only_headers = await login(client, UPLOADER)  # upload+view，无 view_chunks

    for headers in (no_perm_headers, view_only_headers):
        resp = await client.post(
            URL.format(version_id=version_id), json={"query": "q"}, headers=headers
        )
        assert resp.status_code == 403, f"缺码主体应 403：{resp.text}"


async def test_not_indexed_version_is_rejected_409(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await _seed(env, monkeypatch)
    await execute(
        "update kb_document_versions set status = 'APPROVED' where id = :v", v=str(version_id)
    )
    headers = await login(client, KB_CHUNK_VIEWER)
    resp = await client.post(
        URL.format(version_id=version_id), json={"query": "q"}, headers=headers
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "RETRIEVAL_TEST_NOT_INDEXED"


async def test_embed_failure_is_502(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await _seed(env, monkeypatch)

    class _Boom:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            raise EmbeddingError("EMBED_HTTP_ERROR", "boom")

    monkeypatch.setattr(retrieval_test, "_build_embedder_for_test", lambda settings: _Boom())
    headers = await login(client, KB_CHUNK_VIEWER)
    resp = await client.post(
        URL.format(version_id=version_id), json={"query": "q"}, headers=headers
    )
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["code"] == "RETRIEVAL_TEST_EMBED_FAILED"


async def test_qdrant_unconfigured_is_502(
    env: KbEnv, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    _doc, version_id = await _seed(env, monkeypatch)
    settings_ns = SimpleNamespace(
        qdrant_url=None,
        tei_embed_url="http://127.0.0.1:8081",
        tei_embed_timeout_seconds=5.0,
        kb_embed_batch_size=8,
        tei_rerank_url="http://127.0.0.1:8082",
        tei_rerank_timeout_seconds=5.0,
        kb_retrieval_test_rerank_pool=20,
    )
    monkeypatch.setattr(retrieval_test, "get_settings", lambda: settings_ns)
    headers = await login(client, KB_CHUNK_VIEWER)
    resp = await client.post(
        URL.format(version_id=version_id), json={"query": "q"}, headers=headers
    )
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["code"] == "QDRANT_UNAVAILABLE"
