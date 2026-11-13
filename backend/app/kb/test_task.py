"""知识库测试沙箱 worker（M3 KB-04A）：检索命中测试 + 状态收口。

口径（`docs/design-briefs/M3-KB-04A.md` 已确认 D1/D3/D5）：

- **检索命中 MVP（D1）**：每条 case 的 question → embed → Qdrant 混合搜索 → top_k
  → expected_chunk_ids 命中率；100% 命中 = TEST_PASSED（D5 宁严勿松）；
  LLM 回答生成 + Ragas → M4（TD-298）；
- **三段式短事务**（build_task 同款）：取件（READY/TEST_* → TESTING）→ 事务外
  逐 case 检索 → 收口（TEST_PASSED/FAILED + metrics_json + 审计）；
- **活跃唯一**：部分唯一索引 `(tenant_id, version_id) WHERE status='TESTING'` 兜底；
- **审计**：`school.kb.test.completed`（系统执行者）；
- **判分视角（KB-09B，简报 §3.2.3）**：`run.as_audiences` 显式人群优先（创建时
  结构校验，worker 重校验防御脏行）；否则发起人 `started_by` 执行期实时解析
  （调岗后重跑按新部门）。解析空集/异常 → 空 token 集 → **不发查询**
  （Qdrant 空 Filter 匹配全部点，fail-closed）→ 逐 case 判 FAIL（宁严勿松）；
  不支持 as_user_id/ignore_acl（milestone 口径）。
"""

import logging
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from qdrant_client import AsyncQdrantClient

from app.config import get_settings
from app.db.models.kb import KbDocument, KbDocumentVersion, KbTestRun, KbTestSet
from app.db.repository import ScopeBypass, TenantScopedRepository
from app.db.session import db_session
from app.kb.audience import AudienceValidationError, parse_audience_tokens, resolve_audience_tokens
from app.kb.audience_source import RepositoryAudienceSource
from app.kb.embedding import EmbeddingPort, embed_input_text, version_sparse_weights
from app.kb.indexer import acl_query_filter, collection_name, search_points
from app.kb.status import (
    KB_STATUS_TEST_FAILED,
    KB_STATUS_TEST_PASSED,
    KB_STATUS_TESTING,
    assert_test_allowed,
    assert_transition,
)

logger = logging.getLogger(__name__)

TEST_RUN_STATUS_TESTING = "TESTING"
TEST_RUN_STATUS_PASSED = "TEST_PASSED"
TEST_RUN_STATUS_FAILED = "TEST_FAILED"

KB_TEST_CODE = "school.kb.test"


class TestRunError(Exception):
    """测试运行失败（code/message 落 metrics_json + 审计）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _run_repository(session: Any, tenant_id: UUID) -> TenantScopedRepository[KbTestRun]:
    return TenantScopedRepository(
        session,
        KbTestRun,
        tenant_id,
        scope=ScopeBypass(reason="测试运行：按 run/version id 精确操作，系统执行者"),
    )


async def _claim_test(
    tenant_id: UUID, version_id: UUID
) -> tuple[KbTestRun, KbDocumentVersion, KbDocument] | None:
    """取件（短事务）：TESTING 状态落库 + 版本迁移 + 文档跟随。"""

    async with db_session(tenant_id=tenant_id) as session:
        run = (
            await session.execute(
                _run_repository(session, tenant_id)
                .base_query()
                .where(KbTestRun.version_id == version_id, KbTestRun.status == TEST_RUN_STATUS_TESTING)
            )
        ).scalar_one_or_none()
        if run is None:
            return None
        version_repo: TenantScopedRepository[KbDocumentVersion] = TenantScopedRepository(
            session, KbDocumentVersion, tenant_id,
            scope=ScopeBypass(reason="测试运行：按版本 id 精确操作，系统执行者"),
        )
        version = await version_repo.get(version_id, with_for_update=True)
        document = (
            await TenantScopedRepository(
                session, KbDocument, tenant_id,
                scope=ScopeBypass(reason="测试运行：文档状态跟随版本，系统执行者"),
            ).get(version.document_id, with_for_update=True)
            if version is not None
            else None
        )
        if version is None or document is None:
            return None
        try:
            assert_test_allowed(version.status)
        except Exception:
            return None
        assert_transition(version.status, KB_STATUS_TESTING)
        version.status = KB_STATUS_TESTING
        document.status = KB_STATUS_TESTING
        run.started_at = datetime.now(UTC)
        await session.flush()
        return run, version, document


async def _finish_test(
    tenant_id: UUID,
    version_id: UUID,
    *,
    passed: bool,
    metrics: dict[str, Any],
    request_id: str | None = None,
) -> None:
    """收口（短事务）：版本/文档/run 终态 + 审计。"""

    async with db_session(tenant_id=tenant_id) as session:
        run = (
            await session.execute(
                _run_repository(session, tenant_id)
                .base_query()
                .where(KbTestRun.version_id == version_id, KbTestRun.status == TEST_RUN_STATUS_TESTING)
            )
        ).scalar_one_or_none()
        if run is None:
            logger.warning("测试收口跳过：run 不在 TESTING（version=%s）", version_id)
            return
        version_repo: TenantScopedRepository[KbDocumentVersion] = TenantScopedRepository(
            session, KbDocumentVersion, tenant_id,
            scope=ScopeBypass(reason="测试收口：按版本 id 精确操作，系统执行者"),
        )
        version = await version_repo.get(version_id, with_for_update=True)
        document = (
            await TenantScopedRepository(
                session, KbDocument, tenant_id,
                scope=ScopeBypass(reason="测试收口：文档状态跟随版本，系统执行者"),
            ).get(version.document_id, with_for_update=True)
            if version is not None
            else None
        )
        if version is None or document is None:
            return
        target = KB_STATUS_TEST_PASSED if passed else KB_STATUS_TEST_FAILED
        if version.status != KB_STATUS_TESTING:
            return  # 竞态：版本已推进
        assert_transition(version.status, target)
        now = datetime.now(UTC)
        version.status = target
        document.status = target
        run.status = TEST_RUN_STATUS_PASSED if passed else TEST_RUN_STATUS_FAILED
        run.metrics_json = metrics
        run.finished_at = now
        await append_audit_test(
            session,
            tenant_id=tenant_id,
            version_id=version_id,
            run_id=run.id,
            passed=passed,
            metrics=metrics,
        )
        await session.flush()


async def append_audit_test(
    session: Any,
    *,
    tenant_id: UUID,
    version_id: UUID,
    run_id: UUID,
    passed: bool,
    metrics: dict[str, Any],
) -> None:
    """测试完成审计（系统执行者；与 build.completed/failed 同形）。"""

    from app.audit.writer import append_audit

    await append_audit(
        session,
        action="school.kb.test.completed",
        resource_type="kb_document_version",
        resource_id=str(version_id),
        actor_realm="system",
        actor_id=None,
        tenant_id=tenant_id,
        result="success" if passed else "failure",
        metadata={
            "run_id": str(run_id),
            "passed": passed,
            "pass_rate": metrics.get("pass_rate"),
            "total_cases": metrics.get("total_cases"),
        },
    )


def _build_embedder_for_test(settings: Any) -> EmbeddingPort:
    from app.kb.embedding import TEIEmbedder

    return TEIEmbedder(
        base_url=settings.tei_embed_url,
        timeout_seconds=settings.tei_embed_timeout_seconds,
        batch_size=settings.kb_embed_batch_size,
    )


async def run_kb_test(ctx: dict[str, Any], tenant_id: str, version_id: str) -> dict[str, Any]:
    """测试运行（Arq 入口；测试可直接调用）。

    返回 `{"status": "test_passed" | "test_failed" | "skipped"}`。
    """

    resolved = get_settings()
    tid = UUID(tenant_id)
    vid = UUID(version_id)

    claimed = await _claim_test(tid, vid)
    if claimed is None:
        return {"status": "skipped"}
    run, _version, document = claimed

    try:
        # ---- KB-09B 判分视角（fail-closed；简报 §3.2.3）----
        raw_as_audiences = run.as_audiences
        if raw_as_audiences is not None:
            try:
                view_tokens = parse_audience_tokens(list(raw_as_audiences))
            except AudienceValidationError as exc:
                logger.warning("测试运行 as_audiences 非法（fail-closed 空集）：%s", exc.code)
                view_tokens = []
        else:
            async with db_session(tenant_id=tid) as session:
                source = RepositoryAudienceSource(session, tid)
                try:
                    # 发起人已删除 → 空集 FAIL（宁严勿松；与检索测试 as_user_id
                    # 删态 404 同口径，审查 m1：locked/disabled 仍可判分）
                    user = await source.load_user(run.started_by)
                    if user is None or user.status == "deleted":
                        view_tokens = []
                    else:
                        view_tokens = await resolve_audience_tokens(source, run.started_by)
                except Exception as exc:  # 解析异常=不召回（fail-closed 空集）
                    logger.warning(
                        "KB-09 人群解析失败（fail-closed 空集）：%s.%s",
                        type(exc).__module__,
                        type(exc).__name__,
                    )
                    view_tokens = []
        query_filter = acl_query_filter(view_tokens) if view_tokens else None

        # 取测试集与用例
        async with db_session(tenant_id=tid) as session:
            test_set = await TenantScopedRepository(
                session, KbTestSet, tid,
                scope=ScopeBypass(reason="测试运行：读取测试集，系统执行者"),
            ).get(run.test_set_id)
            if test_set is None:
                raise TestRunError("TEST_SET_NOT_FOUND", "测试集不存在")
        # 简化：直接查 KbTestCase
        from app.db.models.kb import KbTestCase

        async with db_session(tenant_id=tid) as session:
            case_repository: TenantScopedRepository[KbTestCase] = TenantScopedRepository(
                session, KbTestCase, tid,
                scope=ScopeBypass(reason="测试运行：读取用例，系统执行者"),
            )

            case_rows = (
                (
                    await session.execute(
                        case_repository.base_query()
                        .where(KbTestCase.test_set_id == run.test_set_id)
                        .order_by(KbTestCase.row_no.asc())
                    )
                )
                .scalars()
                .all()
            )
        if not case_rows:
            raise TestRunError("NO_CASES", "测试集没有用例")

        # 逐 case 检索
        client = AsyncQdrantClient(url=resolved.qdrant_url) if resolved.qdrant_url else None
        if client is None:
            raise TestRunError("QDRANT_UNAVAILABLE", "Qdrant 未配置")
        collection = collection_name(tid, vid)
        embedder = _build_embedder_for_test(resolved)

        case_results: list[dict[str, Any]] = []
        passed_count = 0
        try:
            for case in case_rows:
                started = time.monotonic()
                if query_filter is None:
                    # 空集 fail-closed：不发查询也不做无谓嵌入——检索零召回与
                    # expected 非空判 FAIL 等价（简报 §3.2.3）
                    results: list[dict[str, Any]] = []
                else:
                    query_text = embed_input_text(
                        document_title=document.title, section_path=None, chunk_text=case.question
                    )
                    dense_vectors = await embedder.embed([query_text])
                    sparse = version_sparse_weights([case.question])[0]
                    results = await search_points(
                        client,
                        collection=collection,
                        dense=dense_vectors[0],
                        sparse=sparse,
                        top_k=10,
                        query_filter=query_filter,
                    )
                elapsed_ms = round((time.monotonic() - started) * 1000, 1)
                retrieved_ids = {item["chunk_id"] for item in results}
                expected = set(case.expected_chunk_ids or [])
                hit = expected <= retrieved_ids if expected else None  # None = 无期望引用不判定
                case_pass = hit if hit is not None else True  # 无期望 → 默认通过
                if case_pass:
                    passed_count += 1
                case_results.append(
                    {
                        "row_no": case.row_no,
                        "question": case.question[:100],
                        "pass": case_pass,
                        "hit": hit,
                        "expected_count": len(expected),
                        "retrieved_count": len(results),
                        "latency_ms": elapsed_ms,
                        "is_adversarial": case.is_adversarial,
                    }
                )
        finally:
            await client.close()

        total = len(case_rows)
        pass_rate = round(passed_count / total, 4) if total else 0.0
        latencies = sorted(item["latency_ms"] for item in case_results)
        metrics = {
            "total_cases": total,
            "passed_cases": passed_count,
            "pass_rate": pass_rate,
            "latency_p50_ms": latencies[len(latencies) // 2] if latencies else 0,
            "latency_p95_ms": latencies[int(len(latencies) * 0.95)] if latencies else 0,
            "cases": case_results,
        }
        passed = passed_count == total  # D5：100% 命中才通过
        await _finish_test(tid, vid, passed=passed, metrics=metrics)
        return {
            "status": "test_passed" if passed else "test_failed",
            "pass_rate": pass_rate,
            "total_cases": total,
        }
    except TestRunError as exc:
        metrics = {"error_code": exc.code, "error_message": exc.message}
        await _finish_test(tid, vid, passed=False, metrics=metrics)
        return {"status": "test_failed", "error_code": exc.code}
    except Exception as exc:
        logger.warning("测试运行未归类失败：%s.%s", type(exc).__module__, type(exc).__name__)
        metrics = {"error_code": type(exc).__name__, "error_message": str(exc)[:500]}
        await _finish_test(tid, vid, passed=False, metrics=metrics)
        return {"status": "test_failed", "error_code": type(exc).__name__}
