"""知识库构建 worker（M3 KB-03A）：单版本管线执行 + 滞留看门狗。

口径（`docs/design-briefs/M3-KB-03A.md` 已确认 D1–D6；`docs/design.md` §7）：

- **短事务三段式**（`scan_task.py` 同款）：取件（BUILD_QUEUED → BUILDING）→
  事务外执行管线（分钟级，逐阶段短事务写进度/心跳）→ 收口（READY/FAILED）；
  全程不得占用数据库连接与行锁；
- **状态机**：版本 `APPROVED → BUILDING → READY/FAILED`；文档状态跟随版本；
  job 内阶段 `PARSING → … → VALIDATING`（阶段推进写 stage/heartbeat/progress）；
- **诚实失败**：任一阶段失败 → job/version/document 全部 FAILED（error_code +
  message），不静默吞；DOCX/PPTX 在 PARSING 阶段 `PARSER_UNAVAILABLE`（TD-293）；
- **chunks 原子落库**：全部子块在收口事务内一次性插入（embedding_status=ready），
  EMBEDDING/INDEXING 失败不留半成品行——重试从零开始且干净；
- **Qdrant 幂等**：collection 重建（ensure_collection 删旧建新），重试不留脏点；
- **审计**：收口写 `school.kb.build.completed/failed`（系统执行者，与扫描审计同形）；
  STALE 看门狗写 `school.kb.build.failed`（message 注明 stale）。
"""

import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
from qdrant_client import AsyncQdrantClient
from sqlalchemy import func
from sqlalchemy import select as sa_select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.writer import append_audit
from app.config import get_settings
from app.db.models.kb import (
    KbBuildJob,
    KbChunk,
    KbDocument,
    KbDocumentAudience,
    KbDocumentVersion,
    KbIndexVersion,
)
from app.db.models.kb_events import KbBuildEvent
from app.db.repository import ScopeBypass, TenantScopedRepository
from app.db.session import db_session
from app.kb.audience import (
    ACL_GATE_ALLOW,
    GATE_ACL_REQUIRED,
    acl_gate,
    audience_tokens,
)
from app.kb.chunker import CHUNK_TYPE_CHILD, CHUNK_TYPE_PARENT, RawChunk, chunk_markdown
from app.kb.embedding import (
    EmbeddingError,
    EmbeddingPort,
    embed_input_text,
    version_sparse_weights,
)
from app.kb.indexer import (
    IndexError_,
    IndexPoint,
    collection_name,
    count_points,
    ensure_collection,
    upsert_points,
)
from app.kb.parser import (
    MarkdownNormalizer,
    MinerUParser,
    ParsedDocument,
    ParserError,
    ParseTierOutcome,
    TieredParser,
    select_parser,
)
from app.kb.status import (
    KB_STATUS_APPROVED,
    KB_STATUS_BUILDING,
    KB_STATUS_FAILED,
    KB_STATUS_READY,
    assert_transition,
)
from app.storage.objects import ObjectStore, ObjectStoreError, build_object_store

logger = logging.getLogger(__name__)

# job 侧状态（`db/models/kb.py::BUILD_JOB_STATUSES`；版本状态机不含 QUEUED）
JOB_STATUS_QUEUED = "BUILD_QUEUED"
JOB_STATUS_BUILDING = "BUILDING"
JOB_STATUS_READY = "READY"
JOB_STATUS_FAILED = "FAILED"

# 阶段名（`db/models/kb.py::BUILD_STAGES` 的执行顺序）
STAGE_PARSING = "PARSING"
STAGE_NORMALIZING = "NORMALIZING"
STAGE_CHUNKING = "CHUNKING"
STAGE_EMBEDDING = "EMBEDDING"
STAGE_INDEXING = "INDEXING"
STAGE_VALIDATING = "VALIDATING"
STAGE_ORDER = (
    STAGE_PARSING,
    STAGE_NORMALIZING,
    STAGE_CHUNKING,
    STAGE_EMBEDDING,
    STAGE_INDEXING,
    STAGE_VALIDATING,
)


def build_job_id(version_id: UUID, attempt: int) -> str:
    """构建任务的 arq job_id（带尝试序号；重试/看门狗用新序号避免结果键挡路）。"""

    return f"build:{version_id}:{attempt}"


class BuildPipelineError(Exception):
    """管线失败（code/message 直接落 job.error_code/message）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def _append_event(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    job_id: UUID,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """追加构建事件（D1：调用方事务内执行，事件与状态同事务一致）。

    seq 按 job 递增（max+1）；`(job_id, seq)` 唯一约束兜底并发（理论同一事务内
    串行，约束是防御性兜底）。事件写入失败向上抛——事件与状态同事务，写不进
    就回滚整笔（管线会按 FAILED 收口，不留「状态推进了但事件丢失」的裂缝）。
    """

    max_seq = (
        await session.execute(
            sa_select(func.max(KbBuildEvent.seq)).where(KbBuildEvent.job_id == job_id)
        )
    ).scalar_one_or_none()
    repository: TenantScopedRepository[KbBuildEvent] = TenantScopedRepository(
        session,
        KbBuildEvent,
        tenant_id,
        scope=ScopeBypass(reason="构建事件：随状态推进同事务写入，系统执行者"),
    )
    await repository.add(
        KbBuildEvent(
            tenant_id=tenant_id,
            job_id=job_id,
            seq=(max_seq or 0) + 1,
            event_type=event_type,
            payload_json=payload,
        )
    )


def _job_repository(session: AsyncSession, tenant_id: UUID) -> TenantScopedRepository[KbBuildJob]:
    return TenantScopedRepository(
        session,
        KbBuildJob,
        tenant_id,
        scope=ScopeBypass(reason="构建任务：按 job/version id 精确操作，系统执行者"),
    )


def _version_repository(
    session: AsyncSession, tenant_id: UUID
) -> TenantScopedRepository[KbDocumentVersion]:
    return TenantScopedRepository(
        session,
        KbDocumentVersion,
        tenant_id,
        scope=ScopeBypass(reason="构建任务：按版本 id 精确操作，系统执行者"),
    )


def _audience_repository(
    session: AsyncSession, tenant_id: UUID
) -> TenantScopedRepository[KbDocumentAudience]:
    return TenantScopedRepository(
        session,
        KbDocumentAudience,
        tenant_id,
        scope=ScopeBypass(reason="构建任务：按版本读人群 token，系统执行者"),
    )


async def _acl_gate_for_build(tenant_id: UUID, version_id: UUID) -> str | None:
    """执行期 ACL 门禁（K4 fail-closed；KB-09A D1-A）：返回 ACL_GATE_ALLOW 或 409 错误码。

    与创建端点同判据（`audience.acl_gate`）：0 条 → `ACL_REQUIRED`、非空未确认 →
    `ACL_UNCONFIRMED`；不过 → BuildPipelineError 落 job FAILED（可重试语义）。
    """

    async with db_session(tenant_id=tenant_id) as session:
        version = await _version_repository(session, tenant_id).get(version_id)
        if version is None:
            return GATE_ACL_REQUIRED
        audience_repository = _audience_repository(session, tenant_id)
        scoped_rows = audience_repository.base_query().where(
            KbDocumentAudience.version_id == version_id
        )
        audience_count = int(
            (
                await session.execute(
                    sa_select(func.count()).select_from(scoped_rows.subquery())
                )
            ).scalar_one()
        )
        gate = acl_gate(audience_count, version.acl_confirmed_at)
        return None if gate == ACL_GATE_ALLOW else gate


async def _confirmed_audience_tokens(tenant_id: UUID, version_id: UUID) -> list[str]:
    """确认人群 → payload token 列表（`type:<id>` / `all`；`audience.audience_tokens`）。"""

    async with db_session(tenant_id=tenant_id) as session:
        audience_repository = _audience_repository(session, tenant_id)
        rows_ = (
            await session.execute(
                audience_repository.base_query()
                .where(KbDocumentAudience.version_id == version_id)
                .with_only_columns(KbDocumentAudience.audience_type, KbDocumentAudience.audience_id)
            )
        ).all()
    return audience_tokens([(row.audience_type, row.audience_id) for row in rows_])


def _document_repository(
    session: AsyncSession, tenant_id: UUID
) -> TenantScopedRepository[KbDocument]:
    return TenantScopedRepository(
        session,
        KbDocument,
        tenant_id,
        scope=ScopeBypass(reason="构建任务：文档状态跟随版本，系统执行者"),
    )


def _chunk_repository(session: AsyncSession, tenant_id: UUID) -> TenantScopedRepository[KbChunk]:
    return TenantScopedRepository(
        session,
        KbChunk,
        tenant_id,
        scope=ScopeBypass(reason="构建任务：按版本写入分块，系统执行者"),
    )


def _index_repository(
    session: AsyncSession, tenant_id: UUID
) -> TenantScopedRepository[KbIndexVersion]:
    return TenantScopedRepository(
        session,
        KbIndexVersion,
        tenant_id,
        scope=ScopeBypass(reason="构建任务：维护版本索引记录，系统执行者"),
    )


async def _claim(
    tenant_id: UUID, version_id: UUID
) -> tuple[KbDocumentVersion, KbBuildJob] | None:
    """取件（短事务）：BUILD_QUEUED → BUILDING，版本 APPROVED → BUILDING。

    重复投递/看门狗与取件竞态时后到者返回 None（与扫描同款幂等）；版本状态
    非法（assert_transition 失败）按取件失败处理并置 job FAILED（编程错误级）。
    """

    async with db_session(tenant_id=tenant_id) as session:
        # job 按「该版本的活跃任务」取：QUEUED 行就是被投递的那一行
        job = (
            await session.execute(
                _job_repository(session, tenant_id)
                .base_query()
                .where(KbBuildJob.version_id == version_id, KbBuildJob.status == JOB_STATUS_QUEUED)
            )
        ).scalar_one_or_none()
        if job is None:
            return None
        version = await _version_repository(session, tenant_id).get(
            version_id, with_for_update=True
        )
        document = (
            await _document_repository(session, tenant_id).get(
                version.document_id, with_for_update=True
            )
            if version is not None
            else None
        )
        # 首次构建：版本/文档 APPROVED；重试（retry 端点把 job 排回 QUEUED）：FAILED
        if (
            version is None
            or document is None
            or version.status not in (KB_STATUS_APPROVED, KB_STATUS_FAILED)
            or document.status not in (KB_STATUS_APPROVED, KB_STATUS_FAILED)
        ):
            return None
        now = datetime.now(UTC)
        assert_transition(version.status, KB_STATUS_BUILDING)
        version.status = KB_STATUS_BUILDING
        document.status = KB_STATUS_BUILDING
        job.status = JOB_STATUS_BUILDING
        job.started_at = now
        job.heartbeat_at = now
        job.error_code = None
        job.message = None
        # tier/fallback_reason 一并清空：TXT/MD（tier=None）重试或「上轮降级这轮
        # 首选档成功」时不得残留上一 attempt 的旧值（审查 Minor 2）
        job.tier = None
        job.fallback_reason = None
        job.finished_at = None
        await session.flush()
        return version, job


async def _stage_progress(
    tenant_id: UUID,
    job_id: UUID,
    *,
    stage: str,
    processed_units: int | None = None,
    total_units: int | None = None,
    tier: str | None = None,
    fallback_reason: str | None = None,
) -> None:
    """阶段推进（独立短事务）：stage/progress/心跳落库；失败只记日志不中断管线。"""

    progress = None
    if processed_units is not None and total_units:
        progress = min(processed_units / total_units, 1.0)
    try:
        async with db_session(tenant_id=tenant_id) as session:
            job = await _job_repository(session, tenant_id).get(job_id)
            if job is None or job.status != JOB_STATUS_BUILDING:
                return
            job.stage = stage
            job.processed_units = processed_units
            job.total_units = total_units
            job.progress = progress
            job.heartbeat_at = datetime.now(UTC)
            if tier is not None:
                job.tier = tier
            if fallback_reason is not None:
                job.fallback_reason = fallback_reason
            event_type = "build.stage" if processed_units in (None, 0) else "build.progress"
            await _append_event(
                session,
                tenant_id,
                job_id=job_id,
                event_type=event_type,
                payload={
                    "stage": stage,
                    "progress": progress,
                    "processed_units": processed_units,
                    "total_units": total_units,
                    "tier": tier,
                },
            )
            await session.flush()
    except SQLAlchemyError as exc:
        logger.warning(
            "构建进度写入失败（stage=%s）：%s.%s", stage, type(exc).__module__, type(exc).__name__
        )


async def _finish(
    tenant_id: UUID,
    version_id: UUID,
    *,
    attempt: int,
    success: bool,
    error_code: str | None = None,
    message: str | None = None,
    chunks: list[tuple[UUID, RawChunk, list[float] | None]] | None = None,
    collection: str | None = None,
    dense_dim: int | None = None,
    request_id: str | None = None,
) -> None:
    """收口（短事务）：版本/文档/job 终态 + chunks 原子落库 + 索引记录 + 审计。

    `chunks` 仅在 success=True 且已切分嵌入完成时携带（(chunk, metadata, dense) 三元组，
    sparse 在本函数内按最终语料重算——BM25 的 IDF 依赖全量语料）。失败时三态全 FAILED。
    """

    async with db_session(tenant_id=tenant_id) as session:
        job = (
            await session.execute(
                _job_repository(session, tenant_id)
                .base_query()
                .where(
                    KbBuildJob.version_id == version_id,
                    KbBuildJob.status == JOB_STATUS_BUILDING,
                    # attempt 校验：STALE→重试后旧 worker 的迟到收口不得覆盖新 attempt
                    # （否则 chunks 与 Qdrant 向量会跨 attempt 混装，审查 M1）
                    KbBuildJob.attempt == attempt,
                )
            )
        ).scalar_one_or_none()
        if job is None:
            # 看门狗可能已把超时任务置 FAILED：不覆盖别人的结论
            logger.warning("构建收口跳过：job 不在 BUILDING（version=%s）", version_id)
            return
        version = await _version_repository(session, tenant_id).get(
            version_id, with_for_update=True
        )
        document = (
            await _document_repository(session, tenant_id).get(
                version.document_id, with_for_update=True
            )
            if version is not None
            else None
        )
        if version is None or document is None:
            logger.error("构建收口异常：版本/文档不存在（version=%s）", version_id)
            return
        now = datetime.now(UTC)
        target_status = KB_STATUS_READY if success else KB_STATUS_FAILED
        assert_transition(version.status, target_status)
        version.status = target_status
        document.status = target_status
        job.status = JOB_STATUS_READY if success else JOB_STATUS_FAILED
        job.finished_at = now
        job.heartbeat_at = now
        if success:
            job.error_code = None
            job.message = None
            job.progress = 1.0
            job.processed_units = job.total_units
        else:
            job.error_code = error_code
            job.message = message

        chunk_rows: list[KbChunk] = []
        if success and chunks:
            for index, (chunk_id, chunk, _dense) in enumerate(chunks):
                row = KbChunk(
                    id=chunk_id,
                    tenant_id=tenant_id,
                    document_version_id=version_id,
                    parent_id=None,
                    chunk_type=chunk.chunk_type,
                    chunk_index=index,
                    text=chunk.text,
                    text_markdown=chunk.text,
                    section_path=chunk.section_path,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    token_count=chunk.token_count,
                    char_count=chunk.char_count,
                    overlap_prev=chunk.overlap_prev,
                    metadata_json={
                        "table": bool(chunk.metadata.get("table")),
                        "code": bool(chunk.metadata.get("code")),
                    },
                    embedding_status="ready",
                    chunk_hash=chunk.chunk_hash,
                )
                await _chunk_repository(session, tenant_id).add(row)
                chunk_rows.append(row)
            await session.flush()
            # 父子关系回填：输出序 = 每组父块紧邻其子块；子块 parent_id 指向所属父块行
            parent_row: KbChunk | None = None
            for row in chunk_rows:
                if row.chunk_type == CHUNK_TYPE_PARENT:
                    parent_row = row
                elif parent_row is not None:
                    row.parent_id = parent_row.id

        if collection is not None:
            existing = (
                await session.execute(
                    _index_repository(session, tenant_id)
                    .base_query()
                    .where(KbIndexVersion.document_version_id == version_id)
                )
            ).scalar_one_or_none()
            if existing is None:
                await _index_repository(session, tenant_id).add(
                    KbIndexVersion(
                        tenant_id=tenant_id,
                        document_version_id=version_id,
                        qdrant_collection=collection,
                        status="ready" if success else "failed",
                    )
                )
            else:
                existing.qdrant_collection = collection
                existing.status = "ready" if success else "failed"

        await _append_event(
            session,
            tenant_id,
            job_id=job.id,
            event_type="build.done" if success else "build.error",
            payload={
                "status": "READY" if success else "FAILED",
                "stage": job.stage,
                "error_code": error_code,
                "message": message,
                "chunks": len(chunk_rows) if success else None,
            },
        )
        await append_audit(
            session,
            action="school.kb.build.completed" if success else "school.kb.build.failed",
            resource_type="kb_document_version",
            resource_id=str(version_id),
            actor_realm="system",
            actor_id=None,
            tenant_id=tenant_id,
            result="success" if success else "failure",
            metadata={
                "job_id": str(job.id),
                "attempt": job.attempt,
                "stage": job.stage,
                "tier": job.tier,
                "fallback_reason": job.fallback_reason,
                "error_code": error_code,
                "chunks": len(chunk_rows) if success else None,
                "stale": bool(message and "stale" in message),
            },
            request_id=request_id,
        )
        await session.flush()


async def _run_pipeline(
    tenant_id: UUID,
    job_id: UUID,
    *,
    version_id: UUID,
    document_id: UUID,
    doc_title: str,
    source_object_key: str,
    source_mime: str,
    filename: str,
    settings: Any,
    store: ObjectStore | None = None,
) -> tuple[list[tuple[UUID, RawChunk, list[float] | None]], str]:
    """事务外执行管线，返回 ((chunk_id, chunk, dense) 列表, collection 名)。

    chunk_id 在 CHUNKING 阶段即分配（uuid4），INDEXING 的 Qdrant 点 id 与收口事务的
    chunk 行 id 同源——不存在占位 id 二次覆盖。任何阶段抛 `BuildPipelineError`。
    """

    resolved_store = store if store is not None else build_object_store(settings)
    data = await resolved_store.get(source_object_key)

    # ---------- PARSING ----------
    await _stage_progress(tenant_id, job_id, stage=STAGE_PARSING, processed_units=0, total_units=1)
    mineru_chain = TieredParser(
        [
            (
                tier,
                MinerUParser(
                    tier=tier,
                    base_url=settings.mineru_base_url,
                    timeout_seconds=settings.mineru_timeout_seconds,
                ),
            )
            for tier in settings.kb_parser_tiers()
        ]
    )
    try:
        parser, tiered_flag = select_parser(
            mime=source_mime, mineru_chain=mineru_chain, normalizer=MarkdownNormalizer()
        )
        if tiered_flag:
            outcome = await mineru_chain.parse(data=data, filename=filename)
        else:
            # tiered_flag=False 时 select_parser 返回的是 normalizer（返回 ParsedDocument）
            document = cast(ParsedDocument, await parser.parse(data=data, filename=filename))
            outcome = ParseTierOutcome(document=document, tier=None, fallback_reason=None)
    except ParserError as exc:
        # 含 ParserUnavailableError（TD-293 诚实失败）：code 落 job.error_code
        raise BuildPipelineError(exc.code, exc.message) from exc
    await _stage_progress(
        tenant_id,
        job_id,
        stage=STAGE_PARSING,
        processed_units=1,
        total_units=1,
        tier=outcome.tier,
        fallback_reason=outcome.fallback_reason,
    )

    # ---------- NORMALIZING ----------
    await _stage_progress(
        tenant_id, job_id, stage=STAGE_NORMALIZING, processed_units=0, total_units=1
    )
    markdown = outcome.document.markdown.strip()
    if not markdown:
        raise BuildPipelineError("NORMALIZE_EMPTY_OUTPUT", "解析产出为空文档")
    await _stage_progress(
        tenant_id, job_id, stage=STAGE_NORMALIZING, processed_units=1, total_units=1
    )

    # ---------- CHUNKING ----------
    await _stage_progress(tenant_id, job_id, stage=STAGE_CHUNKING, processed_units=0, total_units=1)
    raw_chunks = chunk_markdown(
        markdown,
        chunk_size=settings.kb_chunk_size,
        overlap_ratio=settings.kb_chunk_overlap_ratio,
        parent_size=settings.kb_parent_chunk_size,
    )
    if not raw_chunks:
        raise BuildPipelineError("CHUNK_EMPTY", "切分结果为空（文档无可索引内容）")
    chunk_ids = [uuid4() for _ in raw_chunks]
    # 父子映射在 CHUNKING 时即确定（组序 = 父块紧邻其子块）；子块 parent_id 供
    # Qdrant payload 与检索侧上下文展开（Minor 3）
    parent_of: dict[int, UUID] = {}
    current_parent_position: int | None = None
    for position, chunk in enumerate(raw_chunks):
        if chunk.chunk_type == CHUNK_TYPE_PARENT:
            current_parent_position = position
        elif current_parent_position is not None:
            parent_of[position] = chunk_ids[current_parent_position]
    await _stage_progress(
        tenant_id,
        job_id,
        stage=STAGE_CHUNKING,
        processed_units=len(raw_chunks),
        total_units=len(raw_chunks),
    )

    # ---------- EMBEDDING ----------
    child_dense_texts = [
        embed_input_text(
            document_title=doc_title,
            section_path=chunk.section_path,
            chunk_text=chunk.text,
        )
        for chunk in raw_chunks
        if chunk.chunk_type == CHUNK_TYPE_CHILD
    ]
    dense_texts = child_dense_texts
    total = len(dense_texts)
    await _stage_progress(
        tenant_id, job_id, stage=STAGE_EMBEDDING, processed_units=0, total_units=total
    )
    embedder = _build_embedder(settings)
    dense_vectors: list[list[float]] = []
    for start in range(0, total, settings.kb_embed_batch_size):
        batch = dense_texts[start : start + settings.kb_embed_batch_size]
        try:
            dense_vectors.extend(await embedder.embed(batch))
        except EmbeddingError as exc:
            raise BuildPipelineError(exc.code, exc.message) from exc
        await _stage_progress(
            tenant_id,
            job_id,
            stage=STAGE_EMBEDDING,
            processed_units=min(start + len(batch), total),
            total_units=total,
        )
    # D4：实际维度与配置比对（TEI 配错模型在构建期暴露，而非 KB-07 检索期）
    if dense_vectors and len(dense_vectors[0]) != settings.kb_embed_dim:
        raise BuildPipelineError(
            "EMBED_DIM_MISMATCH",
            f"嵌入维度漂移（实际 {len(dense_vectors[0])}，配置 {settings.kb_embed_dim}）；"
            "检查 TEI 模型与 KB_EMBED_DIM 配置",
        )

    # 只索引子块（简报 D3 定案）：父块用于检索后上下文展开，不参与召回——
    # 嵌入与存储省一半；对账语义 = 点数 == 子块数（Minor 3）
    child_positions = [
        position
        for position, chunk in enumerate(raw_chunks)
        if chunk.chunk_type == CHUNK_TYPE_CHILD
    ]

    # ---------- INDEXING ----------
    await _stage_progress(
        tenant_id, job_id, stage=STAGE_INDEXING, processed_units=0, total_units=len(child_positions)
    )
    collection = collection_name(tenant_id, version_id)
    sparse_vectors_all = version_sparse_weights([chunk.text for chunk in raw_chunks])
    # K4 payload token 快照（D2-B）：执行期门禁已过，此处读当前确认人群写入每 chunk；
    # 构建后 ACL 变更由 service 层 `sync_acl_payload` 即时重写（spec :309）。
    acl_tokens = await _confirmed_audience_tokens(tenant_id, version_id)
    points = [
        IndexPoint(
            chunk_id=chunk_ids[position],
            parent_id=parent_of.get(position),
            document_id=document_id,
            chunk_type=chunk.chunk_type,
            section_path=chunk.section_path,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            text_hash=chunk.chunk_hash,
            dense=dense_vectors[child_index],
            sparse=sparse_vectors_all[position],
        )
        for child_index, position in enumerate(child_positions)
        for chunk in [raw_chunks[position]]
    ]
    if not settings.qdrant_url:
        raise BuildPipelineError("INDEX_UNAVAILABLE", "Qdrant 未配置（QDRANT_URL 缺失）")
    dense_dim = len(dense_vectors[0]) if dense_vectors else 0
    client = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        try:
            await ensure_collection(client, collection=collection, dense_dim=dense_dim)
        except IndexError_ as exc:
            raise BuildPipelineError(exc.code, exc.message) from exc
        except Exception as exc:
            logger.warning("Qdrant 建集合失败：%s.%s", type(exc).__module__, type(exc).__name__)
            raise BuildPipelineError(
                "INDEX_UNAVAILABLE", "向量索引不可用（Qdrant 建集合失败）"
            ) from exc
        written = await upsert_points(
            client,
            collection=collection,
            tenant_id=tenant_id,
            version_id=version_id,
            doc_title=doc_title,
            points=points,
            acl_tokens=acl_tokens,
        )
    finally:
        await client.close()
    if written != len(child_positions):
        raise BuildPipelineError(
            "INDEX_UPSERT_FAILED", f"向量写入条数不一致（{written}/{len(child_positions)}）"
        )
    await _stage_progress(
        tenant_id,
        job_id,
        stage=STAGE_INDEXING,
        processed_units=written,
        total_units=len(child_positions),
    )

    # ---------- VALIDATING ----------
    await _stage_progress(
        tenant_id, job_id, stage=STAGE_VALIDATING, processed_units=0, total_units=1
    )
    client = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        indexed = await count_points(client, collection=collection)
    finally:
        await client.close()
    if indexed != len(child_positions):
        raise BuildPipelineError(
            "INDEX_VALIDATE_MISMATCH",
            f"索引对账不一致（Qdrant {indexed} vs 子块 {len(child_positions)}）",
        )
    await _stage_progress(
        tenant_id, job_id, stage=STAGE_VALIDATING, processed_units=1, total_units=1
    )
    dense_by_position = {
        position: dense_vectors[child_index]
        for child_index, position in enumerate(child_positions)
    }
    return [
        (chunk_ids[position], chunk, dense_by_position.get(position))
        for position, chunk in enumerate(raw_chunks)
    ], collection


def _build_embedder(settings: Any) -> EmbeddingPort:
    """按配置构造 TEI 嵌入器（测试可 monkeypatch 本函数注入 fake）。"""

    from app.kb.embedding import TEIEmbedder

    return TEIEmbedder(
        base_url=settings.tei_embed_url,
        timeout_seconds=settings.tei_embed_timeout_seconds,
        batch_size=settings.kb_embed_batch_size,
    )


async def build_kb_version(ctx: dict[str, Any], tenant_id: str, version_id: str) -> dict[str, Any]:
    """构建单版本（Arq 入口；测试可直接调用）。

    返回 `{"status": "ready" | "failed" | "skipped", ...}`（与 scan_kb_version
    同形；skipped = 取件竞态后到者/版本状态不匹配）。
    """

    resolved = get_settings()
    tid = UUID(tenant_id)
    vid = UUID(version_id)

    claimed = await _claim(tid, vid)
    if claimed is None:
        return {"status": "skipped"}
    version, job = claimed
    claimed_attempt = job.attempt

    try:
        # ---------- ACL 门禁（K4 fail-closed 执行期二次校验；KB-09A 简报 D1-A）----------
        # 创建端点已过门禁，但「创建后被清确认/直落库种子」在取件时再校验一次；
        # 不过 → job FAILED `ACL_UNCONFIRMED`（确认后 retry/create 可达，可重试语义）。
        gate = await _acl_gate_for_build(tid, vid)
        if gate is not None:
            raise BuildPipelineError(gate, "人群 ACL 未配置或未确认（fail-closed，K4）")
        store = build_object_store(resolved)
        prepared, collection = await _run_pipeline(
            tid,
            job.id,
            version_id=vid,
            document_id=version.document_id,
            doc_title=(await _load_doc_title(tid, version.document_id)),
            source_object_key=version.source_object_key,
            source_mime=version.mime,
            filename=version.source_filename,
            settings=resolved,
            store=store,
        )
    except BuildPipelineError as exc:
        await _finish(
            tid,
            vid,
            attempt=claimed_attempt,
            success=False,
            error_code=exc.code,
            message=exc.message,
        )
        return {"status": "failed", "error_code": exc.code}
    except (
        ParserError,
        EmbeddingError,
        IndexError_,
        ObjectStoreError,
        UnicodeDecodeError,
        SQLAlchemyError,
        httpx.HTTPError,
        OSError,
        ValueError,
    ) as exc:
        # 未归类失败（对象存储抖动/GBK 文本/Qdrant 原生异常等）：code 用异常类名，
        # message 截断——绝不让失败逃逸成 arq 重试→滞留 BUILDING→假 STALE（审查 B1）
        code = type(exc).__name__
        logger.warning("构建管线未归类失败：%s.%s", type(exc).__module__, type(exc).__name__)
        await _finish(
            tid,
            vid,
            attempt=claimed_attempt,
            success=False,
            error_code=code,
            message=str(exc)[:500],
        )
        return {"status": "failed", "error_code": code}

    await _finish(
        tid, vid, attempt=claimed_attempt, success=True, chunks=prepared, collection=collection
    )
    return {"status": "ready", "chunks": len(prepared)}


async def _load_doc_title(tenant_id: UUID, document_id: UUID) -> str:
    async with db_session(tenant_id=tenant_id) as session:
        document = await _document_repository(session, tenant_id).get(document_id)
        return document.title if document is not None else ""


async def retry_stale_build_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    """看门狗 cron：心跳超时的 BUILDING → FAILED（STALE，可重试）。

    租户表带 RLS：先用平台连接列出启用租户，再逐租户在租户上下文内查询
    （与 `retry_pending_kb_scans` / `fail_stale_import_jobs` 同模式）。
    """

    from sqlalchemy import select

    from app.db.models.tenant import Tenant

    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.kb_build_heartbeat_timeout_seconds)
    stale = 0
    requeued = 0
    async with db_session(platform=True) as session:
        tenant_ids = (
            (await session.execute(select(Tenant.id).where(Tenant.status == "active")))
            .scalars()
            .all()
        )
    for tenant_id in tenant_ids:
        stale += await _mark_stale_in_tenant(tenant_id, cutoff)
        requeued += await _requeue_lost_queued(tenant_id, settings)
    return {"stale": stale, "requeued": requeued}


async def _requeue_lost_queued(tenant_id: UUID, settings: Any) -> int:
    """M2（审查）：BUILD_QUEUED 行入队丢失的恢复路径。

    `_enqueue_build` 是 best-effort——Redis 抖动一次，QUEUED 行会永久滞留
    （retry 端点只接 FAILED、create_build 撞活跃唯一、旧看门狗只扫 BUILDING）。
    本分支对「QUEUED 且 created_at 超过看门狗阈值」的行重投（attempt 原样，
    arq job_id 同 key 不会被上一轮结果挡住——同 key 失败后新投递按 arq 语义入队）。
    """

    from app.workers.settings import create_producer_pool

    cutoff = datetime.now(UTC) - timedelta(seconds=settings.kb_build_heartbeat_timeout_seconds)
    requeued = 0
    pool = None
    owned = False
    try:
        async with db_session(tenant_id=tenant_id) as session:
            lost = (
                (
                    await session.execute(
                        _job_repository(session, tenant_id)
                        .base_query()
                        .where(
                            KbBuildJob.status == JOB_STATUS_QUEUED,
                            KbBuildJob.created_at < cutoff,
                        )
                        .limit(50)
                    )
                )
                .scalars()
                .all()
            )
            if not lost:
                return 0
            pool = await create_producer_pool()
            owned = True
            for job in lost:
                enqueued = await pool.enqueue_job(
                    "build_kb_version",
                    str(tenant_id),
                    str(job.version_id),
                    _job_id=build_job_id(job.version_id, job.attempt),
                )
                if enqueued is not None:
                    requeued += 1
    except Exception as exc:
        logger.warning(
            "QUEUED 滞留重投失败（下轮看门狗再试）：%s.%s",
            type(exc).__module__,
            type(exc).__name__,
        )
    finally:
        if pool is not None and owned:
            with contextlib.suppress(Exception):
                await pool.aclose()
    return requeued


async def _mark_stale_in_tenant(tenant_id: UUID, cutoff: datetime) -> int:
    """单租户内把心跳超时的 BUILDING 收口为 FAILED/STALE。"""

    stale = 0
    async with db_session(tenant_id=tenant_id) as session:
        jobs = (
            (
                await session.execute(
                    _job_repository(session, tenant_id)
                    .base_query()
                    .where(
                        KbBuildJob.status == JOB_STATUS_BUILDING,
                        KbBuildJob.heartbeat_at < cutoff,
                    )
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        for job in jobs:
            version = await _version_repository(session, tenant_id).get(
                job.version_id, with_for_update=True
            )
            document = (
                await _document_repository(session, tenant_id).get(
                    version.document_id, with_for_update=True
                )
                if version is not None
                else None
            )
            if version is None or document is None:
                continue
            # 版本可能已被收口推进；只有 BUILDING 才标记 STALE（条件更新语义）
            if version.status != KB_STATUS_BUILDING:
                continue
            assert_transition(version.status, KB_STATUS_FAILED)
            now = datetime.now(UTC)
            version.status = KB_STATUS_FAILED
            document.status = KB_STATUS_FAILED
            job.status = JOB_STATUS_FAILED
            job.error_code = "BUILD_STALE"
            job.message = "构建心跳超时（worker 中断或任务丢失），已标记失败；可重试"
            job.stage = job.stage
            job.finished_at = now
            await append_audit(
                session,
                action="school.kb.build.failed",
                resource_type="kb_document_version",
                resource_id=str(job.version_id),
                actor_realm="system",
                actor_id=None,
                tenant_id=tenant_id,
                result="failure",
                metadata={
                    "job_id": str(job.id),
                    "attempt": job.attempt,
                    "stage": job.stage,
                    "error_code": "BUILD_STALE",
                    "stale": True,
                },
            )
            stale += 1
        await session.flush()
    return stale
