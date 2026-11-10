"""校方侧知识库接口（M3 KB-01A 上传/查询 + M3 KB-02 审批/构建/元数据）。

- `POST /school/kb/documents`：multipart 批量上传（≤15 个、单文件 ≤50MB、合计 ≤300MB），
  必填 `Idempotency-Key`；整批原子，任一文件不合格整批 422；201 返回批次与文档（含派生扫描状态）；
- `GET /school/kb/documents`：按数据范围列出文档（`school` 全校 / `self` 只看自己上传的）；
- `GET /school/kb/documents/{document_id}`：文档详情；
- `GET /school/kb/documents/{document_id}/versions`：版本列表（分页）；
- `POST /school/kb/document-versions/{version_id}/submit-approval`：提交审批
  （`PENDING_APPROVAL` 才可提交；409 `KB_NOT_SUBMITTABLE`/`APPROVAL_ALREADY_PENDING`）；
- `POST /school/kb/document-versions/{version_id}/build`：创建构建任务（只有 `APPROVED`；
  未审批 409 `KB_NOT_APPROVED` + 审计；本点只落 `BUILD_QUEUED`，进度随 KB-03）；
- `PUT /school/kb/documents/{document_id}`：标题/标签/描述编辑（免审但审计前后值）；
- `GET /school/kb/versions/{version_id}/chunks`：分块列表（游标分页 + q/章节/类型/父块筛选，KB-06A）；
- `GET /school/kb/chunks/{chunk_id}`：分块详情（父/子引用，KB-06A）；
- `GET /school/kb/versions/{version_id}/chunks/stats`：版本分块统计（KB-06A）；
- `POST /school/kb/versions/{version_id}/retrieval-test`：单 query 检索测试
  （dense/sparse/RRF/rerank 分数 + 最终引用，KB-07A；重排失败降级不 5xx）。

权限：上传/提交/元数据 `school.kb.upload`；查询 `school.kb.view`；构建 `school.kb.build`；
分块预览与检索测试 `school.kb.view_chunks`（KB-06A/07A）；
发布/回滚/下线 `school.kb.publish`（KB-05）；删除 `school.kb.delete`（KB-05）。
拒绝审计：提交/构建的 403/409 写 `school.kb.approval.denied` / `school.kb.build.denied`，
KB-05 发布/回滚/下线/删除的 403/409 写 `school.kb.publish.denied` / `school.kb.rollback.denied`
/ `school.kb.offline.denied` / `school.kb.delete.denied`（服务层独立事务；404 探测不审计）。
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, StringConstraints

from app.api.pagination import PageResponse
from app.auth.principal import Principal, require_permission
from app.core.sse import SSE_HEADERS, format_heartbeat, format_sse
from app.kb.chunk_read import (
    ChunkRef,
    ChunkStats,
    ChunkView,
    chunk_stats,
    get_chunk,
    list_chunks,
)
from app.kb.retrieval_test import (
    RerankStatus,
    RetrievalRow,
    RetrievalTestResult,
    run_retrieval_test,
    text_preview,
)
from app.kb.service import (
    ApprovalOutcome,
    BuildEventView,
    DocumentView,
    IncomingFile,
    KbError,
    PublishView,
    VersionView,
    create_build,
    create_test_set,
    delete_version,
    get_build_job,
    get_document,
    get_test_run,
    list_build_events,
    list_documents,
    list_version_approvals,
    list_versions,
    offline_version,
    publish_version,
    retry_build,
    rollback_version,
    start_test_run,
    submit_version_approval,
    update_document_metadata,
    upload_documents,
)
from app.kb.validation import MAX_FILE_BYTES

router = APIRouter(prefix="/school/kb", tags=["school-kb"])

UploaderDep = require_permission("school.kb.upload", realm="tenant")
ViewerDep = require_permission("school.kb.view", realm="tenant")
BuilderDep = require_permission("school.kb.build", realm="tenant")
PublisherDep = require_permission("school.kb.publish", realm="tenant")
DeleterDep = require_permission("school.kb.delete", realm="tenant")
ChunkViewerDep = require_permission("school.kb.view_chunks", realm="tenant")

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


class KbScanState(BaseModel):
    """接口派生的扫描状态（不新增 SCANNING 状态）。"""

    state: str
    engine: str | None
    signature: str | None
    error_code: str | None
    attempts: int
    scanned_at: datetime | None


class KbVersionResponse(BaseModel):
    """文档版本。"""

    id: UUID
    version_no: int
    filename: str
    mime: str
    size_bytes: int
    status: str
    scan: KbScanState
    created_at: datetime


class KbDocumentResponse(BaseModel):
    """知识库文档（含当前版本）。"""

    id: UUID
    title: str
    tags: list[str]
    description: str | None
    status: str
    current_version: KbVersionResponse | None
    created_at: datetime
    updated_at: datetime


class KbUploadResponse(BaseModel):
    """批量上传结果（`replayed=true` 表示命中幂等键并返回原批次）。"""

    batch_id: UUID
    replayed: bool
    documents: list[KbDocumentResponse]


def _version_response(view: VersionView) -> KbVersionResponse:
    return KbVersionResponse(
        id=view.id,
        version_no=view.version_no,
        filename=view.filename,
        mime=view.mime,
        size_bytes=view.size_bytes,
        status=view.status,
        scan=KbScanState(
            state=view.scan.state,
            engine=view.scan.engine,
            signature=view.scan.signature,
            error_code=view.scan.error_code,
            attempts=view.scan.attempts,
            scanned_at=view.scan.scanned_at,
        ),
        created_at=view.created_at,
    )


def _document_response(view: DocumentView) -> KbDocumentResponse:
    return KbDocumentResponse(
        id=view.id,
        title=view.title,
        tags=view.tags,
        description=view.description,
        status=view.status,
        current_version=(
            _version_response(view.current_version)
            if view.current_version is not None
            else None
        ),
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _http_error(error: KbError) -> HTTPException:
    detail: dict[str, object] = {"code": error.code, "message": error.message}
    detail.update(error.extra)
    return HTTPException(status_code=error.http_status, detail=detail)


def _reader(upload: UploadFile) -> Callable[[], Awaitable[bytes]]:
    """可重复读取的文件读取器；最多读「单文件上限 + 1 字节」以先判超限。"""

    async def _read() -> bytes:
        await upload.seek(0)
        return await upload.read(MAX_FILE_BYTES + 1)

    return _read


def _tenant_or_403(principal: Principal) -> UUID:
    if principal.realm != "tenant" or principal.tenant_id is None:
        raise HTTPException(status_code=403, detail="知识库只在校方租户上下文内可用")
    return principal.tenant_id


@router.post("/documents", status_code=201, response_model=KbUploadResponse)
async def upload_kb_documents(
    request: Request,
    uploader: Annotated[Principal, UploaderDep],
    files: Annotated[list[UploadFile], File(description="知识库文件（PDF/MD/DOCX/PPT(X)/TXT）")],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", description="幂等键（8–128 位）")
    ],
    metadata: Annotated[str | None, Form(description="可选元数据 JSON 数组")] = None,
) -> KbUploadResponse:
    """批量上传知识库文件（整批原子 + 幂等键）。"""

    tenant_id = _tenant_or_403(uploader)
    incoming = [
        IncomingFile(
            filename=upload.filename or "",
            declared_mime=upload.content_type,
            read=_reader(upload),
        )
        for upload in files
    ]
    try:
        outcome = await upload_documents(
            tenant_id=tenant_id,
            user_id=uploader.user_id,
            idempotency_key=idempotency_key,
            files=incoming,
            metadata_raw=metadata,
            request_id=getattr(request.state, "request_id", None),
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    finally:
        for upload in files:
            await upload.close()
    return KbUploadResponse(
        batch_id=outcome.batch_id,
        replayed=outcome.replayed,
        documents=[_document_response(document) for document in outcome.documents],
    )


@router.get("/documents", response_model=PageResponse[KbDocumentResponse])
async def list_kb_documents(
    viewer: Annotated[Principal, ViewerDep],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> PageResponse[KbDocumentResponse]:
    """按数据范围列出知识库文档（默认 20，上限 100）。"""

    tenant_id = _tenant_or_403(viewer)
    try:
        page = await list_documents(
            tenant_id=tenant_id,
            scope=viewer.scope_context,
            limit=limit,
            cursor=cursor,
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return PageResponse(
        items=[_document_response(document) for document in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/documents/{document_id}", response_model=KbDocumentResponse)
async def get_kb_document(
    document_id: UUID, viewer: Annotated[Principal, ViewerDep]
) -> KbDocumentResponse:
    """文档详情（范围外/跨租一律 404）。"""

    tenant_id = _tenant_or_403(viewer)
    try:
        document = await get_document(
            tenant_id=tenant_id,
            scope=viewer.scope_context,
            document_id=document_id,
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return _document_response(document)


@router.get(
    "/documents/{document_id}/versions",
    response_model=PageResponse[KbVersionResponse],
)
async def list_kb_document_versions(
    document_id: UUID,
    viewer: Annotated[Principal, ViewerDep],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> PageResponse[KbVersionResponse]:
    """某文档的版本列表（分页）。"""

    tenant_id = _tenant_or_403(viewer)
    try:
        page = await list_versions(
            tenant_id=tenant_id,
            scope=viewer.scope_context,
            document_id=document_id,
            limit=limit,
            cursor=cursor,
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return PageResponse(
        items=[_version_response(version) for version in page.items],
        next_cursor=page.next_cursor,
    )


# ---------------------------------------------------------------------------
# KB-02：审批提交 / 构建门禁 / 元数据编辑
# ---------------------------------------------------------------------------


class KbApprovalSubmitResponse(BaseModel):
    """提交审批结果（审批单关键字段；完整审批记录见 `/school/approvals/{id}`）。

    `document_version_id` 在 KB 提交路径恒非空（引擎层可空列是为非 KB 类型预留的）。
    """

    request_id: UUID
    document_version_id: UUID | None
    title: str
    status: str
    submitter_id: UUID
    created_at: datetime


class KbBuildJobResponse(BaseModel):
    """构建任务创建结果（本点只到 BUILD_QUEUED；进度接口随 KB-03）。"""

    job_id: UUID
    version_id: UUID
    status: str
    attempt: int
    created_at: datetime


_TitleStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
_TagStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
# strip 与上传侧 `FileMetadata`（str_strip_whitespace=True）同口径：纯空白描述视为空串语义。
_DescriptionStr = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]


class KbDocumentMetadataUpdate(BaseModel):
    """文档元数据编辑（字段缺省 = 不更新；`description` 显式 null = 清空）。"""

    title: _TitleStr | None = None
    tags: Annotated[list[_TagStr], Field(max_length=10)] | None = None
    description: _DescriptionStr | None = None


def _approval_response(outcome: ApprovalOutcome) -> KbApprovalSubmitResponse:
    request = outcome.request
    return KbApprovalSubmitResponse(
        request_id=request.id,
        document_version_id=request.document_version_id,
        title=request.title,
        status=request.status,
        submitter_id=request.submitter_id,
        created_at=request.created_at,
    )


@router.post(
    "/document-versions/{version_id}/submit-approval",
    status_code=201,
    response_model=KbApprovalSubmitResponse,
)
async def submit_kb_document_version_approval(
    version_id: UUID,
    submitter: Annotated[Principal, UploaderDep],
    request: Request,
) -> KbApprovalSubmitResponse:
    """提交文档版本审批（只有 `PENDING_APPROVAL` 可提交；提交人不能自批由审批侧裁决）。"""

    tenant_id = _tenant_or_403(submitter)
    try:
        outcome = await submit_version_approval(
            tenant_id=tenant_id,
            scope=submitter.scope_context,
            version_id=version_id,
            user_id=submitter.user_id,
            request_id=getattr(request.state, "request_id", None),
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return _approval_response(outcome)


@router.post(
    "/document-versions/{version_id}/build",
    status_code=201,
    response_model=KbBuildJobResponse,
)
async def create_kb_document_version_build(
    version_id: UUID,
    builder: Annotated[Principal, BuilderDep],
    request: Request,
) -> KbBuildJobResponse:
    """创建构建任务（只有 `APPROVED`；未审批 409 并审计，不产生任务行）。"""

    tenant_id = _tenant_or_403(builder)
    try:
        job = await create_build(
            tenant_id=tenant_id,
            scope=builder.scope_context,
            version_id=version_id,
            user_id=builder.user_id,
            request_id=getattr(request.state, "request_id", None),
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return KbBuildJobResponse(
        job_id=job.id,
        version_id=job.version_id,
        status=job.status,
        attempt=job.attempt,
        created_at=job.created_at,
    )


@router.put("/documents/{document_id}", response_model=KbDocumentResponse)
async def update_kb_document_metadata(
    document_id: UUID,
    payload: KbDocumentMetadataUpdate,
    editor: Annotated[Principal, UploaderDep],
    request: Request,
) -> KbDocumentResponse:
    """编辑文档标题/标签/描述（免审但审计前后值；版本状态不变）。"""

    tenant_id = _tenant_or_403(editor)
    provided = payload.model_fields_set
    try:
        document = await update_document_metadata(
            tenant_id=tenant_id,
            scope=editor.scope_context,
            document_id=document_id,
            user_id=editor.user_id,
            title=payload.title if "title" in provided else None,
            tags=payload.tags if "tags" in provided else None,
            description=payload.description if "description" in provided else None,
            description_provided="description" in provided,
            request_id=getattr(request.state, "request_id", None),
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return _document_response(document)


# ---------------------------------------------------------------------------
# KB-03A：构建任务快照（TD-288）与重试
# ---------------------------------------------------------------------------


class KbBuildJobSnapshotResponse(BaseModel):
    """构建任务快照（全字段；SSE 事件流与前端进度随 KB-03B）。"""

    job_id: UUID
    version_id: UUID
    document_id: UUID
    status: str
    stage: str | None
    progress: float | None
    processed_units: int | None
    total_units: int | None
    tier: str | None
    fallback_reason: str | None
    attempt: int
    retry_count: int
    error_code: str | None
    message: str | None
    created_at: datetime
    started_at: datetime | None
    heartbeat_at: datetime | None
    finished_at: datetime | None


@router.get("/build-jobs/{job_id}", response_model=KbBuildJobSnapshotResponse)
async def get_kb_build_job(
    job_id: UUID,
    viewer: Annotated[Principal, ViewerDep],
) -> KbBuildJobSnapshotResponse:
    """构建任务快照（数据范围外/跨租 404；轮询兜底 3s 由前端执行）。"""

    tenant_id = _tenant_or_403(viewer)
    try:
        snapshot = await get_build_job(
            tenant_id=tenant_id,
            scope=viewer.scope_context,
            job_id=job_id,
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return KbBuildJobSnapshotResponse(
        job_id=snapshot.id,
        version_id=snapshot.version_id,
        document_id=snapshot.document_id,
        status=snapshot.status,
        stage=snapshot.stage,
        progress=snapshot.progress,
        processed_units=snapshot.processed_units,
        total_units=snapshot.total_units,
        tier=snapshot.tier,
        fallback_reason=snapshot.fallback_reason,
        attempt=snapshot.attempt,
        retry_count=snapshot.retry_count,
        error_code=snapshot.error_code,
        message=snapshot.message,
        created_at=snapshot.created_at,
        started_at=snapshot.started_at,
        heartbeat_at=snapshot.heartbeat_at,
        finished_at=snapshot.finished_at,
    )


@router.post("/build-jobs/{job_id}/retry", response_model=KbBuildJobSnapshotResponse)
async def retry_kb_build_job(
    job_id: UUID,
    builder: Annotated[Principal, BuilderDep],
    request: Request,
) -> KbBuildJobSnapshotResponse:
    """重试失败的构建任务（只有 FAILED；attempt+1 不覆盖历史；审计）。"""

    tenant_id = _tenant_or_403(builder)
    try:
        snapshot = await retry_build(
            tenant_id=tenant_id,
            scope=builder.scope_context,
            job_id=job_id,
            user_id=builder.user_id,
            request_id=getattr(request.state, "request_id", None),
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return KbBuildJobSnapshotResponse(
        job_id=snapshot.id,
        version_id=snapshot.version_id,
        document_id=snapshot.document_id,
        status=snapshot.status,
        stage=snapshot.stage,
        progress=snapshot.progress,
        processed_units=snapshot.processed_units,
        total_units=snapshot.total_units,
        tier=snapshot.tier,
        fallback_reason=snapshot.fallback_reason,
        attempt=snapshot.attempt,
        retry_count=snapshot.retry_count,
        error_code=snapshot.error_code,
        message=snapshot.message,
        created_at=snapshot.created_at,
        started_at=snapshot.started_at,
        heartbeat_at=snapshot.heartbeat_at,
        finished_at=snapshot.finished_at,
    )


# ---------------------------------------------------------------------------
# KB-03B：SSE 构建事件流 + TD-291 版本审批单查询
# ---------------------------------------------------------------------------


class KbVersionApprovalResponse(BaseModel):
    """版本审批单（TD-291；复用 KB-02B 契约的关键字段）。"""

    request_id: UUID
    title: str
    status: str
    result: str | None
    reject_reason: str | None
    submitter_id: UUID
    created_at: datetime
    completed_at: datetime | None


@router.get(
    "/document-versions/{version_id}/approvals",
    response_model=PageResponse[KbVersionApprovalResponse],
)
async def list_kb_version_approvals(
    version_id: UUID,
    viewer: Annotated[Principal, ViewerDep],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PageResponse[KbVersionApprovalResponse]:
    """某版本的审批单列表（created_at DESC；抽屉直显审批状态与驳回理由——TD-291）。"""

    tenant_id = _tenant_or_403(viewer)
    try:
        approvals = await list_version_approvals(
            tenant_id=tenant_id,
            scope=viewer.scope_context,
            version_id=version_id,
            limit=limit,
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return PageResponse(
        items=[
            KbVersionApprovalResponse(
                request_id=item.id,
                title=item.title,
                status=item.status,
                result=item.result,
                reject_reason=item.reject_reason,
                submitter_id=item.submitter_id,
                created_at=item.created_at,
                completed_at=item.completed_at,
            )
            for item in approvals
        ],
        next_cursor=None,
    )


_SSE_HEARTBEAT_SECONDS = 15
_SSE_IDLE_POLL_SECONDS = 1.0
_SSE_MAX_STREAM_SECONDS = 3600


@router.get("/build-jobs/{job_id}/events")
async def stream_kb_build_events(
    job_id: UUID,
    viewer: Annotated[Principal, ViewerDep],
    since: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """SSE 构建事件流（`text/event-stream`；fetch + Bearer，不用原生 EventSource）。

    - 回放 `?since=<seq>`/`Last-Event-ID` 之后的事件 → 跟随新事件 →
      `build.done`/`build.error` 后服务端关流（终态即关，不挂空流）；
    - 空闲期 15s 心跳帧；job 终态后连入只回放历史即关；
    - 权限同快照：`school.kb.view` + 数据范围，跨租 404。
    """

    tenant_id = _tenant_or_403(viewer)
    # job 存在性与范围校验在连入时一次（与快照同判据）；后续事件按 seq 读取
    try:
        await get_build_job(tenant_id=tenant_id, scope=viewer.scope_context, job_id=job_id)
    except KbError as exc:
        raise _http_error(exc) from exc

    # Last-Event-ID 头优先（重连语义），其次 ?since
    after_seq = since or 0
    if last_event_id is not None and last_event_id.isdigit():
        after_seq = max(after_seq, int(last_event_id))

    async def event_stream() -> AsyncIterator[str]:
        import asyncio
        import time

        started = time.monotonic()
        cursor = after_seq
        while True:
            events: list[BuildEventView] = []
            try:
                events = await list_build_events(
                    tenant_id=tenant_id, job_id=job_id, after_seq=cursor
                )
            except KbError:
                return
            terminal_reached = False
            for event in events:
                cursor = event.seq
                yield format_sse(event.event_type, {**event.payload, "seq": event.seq})
                if event.event_type in ("build.done", "build.error"):
                    terminal_reached = True
            if terminal_reached:
                return
            # job 已终态且无新事件：回放完历史即关（终态后连入）
            current = await get_build_job(
                tenant_id=tenant_id,
                scope=viewer.scope_context,
                job_id=job_id,
            )
            if current.status in ("READY", "FAILED"):
                return
            if time.monotonic() - started > _SSE_MAX_STREAM_SECONDS:
                return
            yield format_heartbeat()
            await asyncio.sleep(_SSE_IDLE_POLL_SECONDS)

    return StreamingResponse(
        event_stream(),
        headers=SSE_HEADERS,
        media_type="text/event-stream",
    )


# ---------------------------------------------------------------------------
# KB-04A：测试沙箱（test-sets 上传 / tests 运行 / test-runs 查询）
# ---------------------------------------------------------------------------


class KbTestSetCreateResponse(BaseModel):
    """测试集上传结果。"""

    test_set_id: UUID
    name: str
    case_count: int


class KbTestSetUploadForm(BaseModel):
    """测试集上传表单（multipart 中 name 为字符串字段）。"""

    name: str


class KbTestRunStartResponse(BaseModel):
    """测试运行发起结果。"""

    run_id: UUID
    version_id: UUID
    test_set_id: UUID
    status: str


class KbTestRunDetailResponse(BaseModel):
    """测试运行详情（metrics_json 全字段）。"""

    run_id: UUID
    version_id: UUID
    test_set_id: UUID
    status: str
    metrics_json: dict[str, object] | None
    started_at: datetime | None
    finished_at: datetime | None


TestSetDep = require_permission("school.kb.test", realm="tenant")


@router.post(
    "/versions/{version_id}/test-sets",
    status_code=201,
    response_model=KbTestSetCreateResponse,
)
async def upload_kb_test_set(
    version_id: UUID,
    builder: Annotated[Principal, TestSetDep],
    request: Request,
    file: Annotated[UploadFile, File(description="测试集文件（CSV/XLSX/JSON）")],
    name: Annotated[str, Form(description="测试集名称")],
) -> KbTestSetCreateResponse:
    """上传测试集（CSV/XLSX/JSON；行级校验，整批原子）。"""

    tenant_id = _tenant_or_403(builder)
    file_data = await file.read()
    await file.close()
    try:
        result = await create_test_set(
            tenant_id=tenant_id,
            scope=builder.scope_context,
            version_id=version_id,
            user_id=builder.user_id,
            name=name,
            file_data=file_data,
            filename=file.filename or "test.csv",
            request_id=getattr(request.state, "request_id", None),
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return KbTestSetCreateResponse(
        test_set_id=result["test_set_id"],
        name=result["name"],
        case_count=result["case_count"],
    )


@router.post(
    "/versions/{version_id}/tests",
    status_code=201,
    response_model=KbTestRunStartResponse,
)
async def start_kb_test_run(
    version_id: UUID,
    builder: Annotated[Principal, TestSetDep],
    request: Request,
    test_set_id: Annotated[UUID, Form(description="测试集 id")],
) -> KbTestRunStartResponse:
    """发起测试运行（READY/TEST_FAILED/TEST_PASSED 可测试；活跃 run 唯一）。"""

    tenant_id = _tenant_or_403(builder)
    try:
        result = await start_test_run(
            tenant_id=tenant_id,
            scope=builder.scope_context,
            version_id=version_id,
            test_set_id=test_set_id,
            user_id=builder.user_id,
            request_id=getattr(request.state, "request_id", None),
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return KbTestRunStartResponse(
        run_id=result["run_id"],
        version_id=result["version_id"],
        test_set_id=result["test_set_id"],
        status=result["status"],
    )


@router.get("/test-runs/{run_id}", response_model=KbTestRunDetailResponse)
async def get_kb_test_run(
    run_id: UUID,
    viewer: Annotated[Principal, ViewerDep],
) -> KbTestRunDetailResponse:
    """查询测试运行结果（metrics_json 全字段；跨租/范围外 404）。"""

    tenant_id = _tenant_or_403(viewer)
    try:
        result = await get_test_run(
            tenant_id=tenant_id,
            scope=viewer.scope_context,
            run_id=run_id,
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return KbTestRunDetailResponse(
        run_id=result["run_id"],
        version_id=result["version_id"],
        test_set_id=result["test_set_id"],
        status=result["status"],
        metrics_json=result["metrics_json"],
        started_at=result["started_at"],
        finished_at=result["finished_at"],
    )


# ---------------------------------------------------------------------------
# KB-05：发布 / 回滚 / 下线 / 删除（docs/design-briefs/M3-KB-05.md，D1-A/D2-A/D4-A/D5+D6-A）
# ---------------------------------------------------------------------------


class KbPublishResponse(BaseModel):
    """发布/回滚结果；`alias_synced=false` 时可对同一活跃版本重放发布重建。"""

    document_id: UUID
    version_id: UUID
    version_no: int
    status: str
    alias: str
    collection: str
    previous_version_id: UUID | None = None
    published_at: datetime
    replayed: bool = False
    alias_synced: bool = True


class KbVersionStateResponse(BaseModel):
    """下线/删除结果（was_active=该版本是否是操作时的活跃发布）。"""

    document_id: UUID
    version_id: UUID
    status: str
    was_active: bool


def _publish_response(view: PublishView) -> KbPublishResponse:
    return KbPublishResponse(
        document_id=view.document_id,
        version_id=view.version_id,
        version_no=view.version_no,
        status=view.status,
        alias=view.alias,
        collection=view.collection,
        previous_version_id=view.previous_version_id,
        published_at=view.published_at,
        replayed=view.replayed,
        alias_synced=view.alias_synced,
    )


@router.post("/versions/{version_id}/publish", response_model=KbPublishResponse)
async def publish_kb_document_version(
    version_id: UUID,
    publisher: Annotated[Principal, PublisherDep],
    request: Request,
) -> KbPublishResponse:
    """发布版本（门禁：TEST_PASSED；alias 行 + Qdrant named alias 原子切换）。

    幂等重放：同一活跃版本再次发布 = 重建 alias/payload（`replayed=true`）。
    """

    tenant_id = _tenant_or_403(publisher)
    try:
        view = await publish_version(
            tenant_id=tenant_id,
            scope=publisher.scope_context,
            version_id=version_id,
            user_id=publisher.user_id,
            request_id=getattr(request.state, "request_id", None),
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return _publish_response(view)


@router.post("/versions/{version_id}/rollback", response_model=KbPublishResponse)
async def rollback_kb_document_version(
    version_id: UUID,
    publisher: Annotated[Principal, PublisherDep],
    request: Request,
) -> KbPublishResponse:
    """回滚：切到任意 `PUBLISHED` 目标版本（path version_id = 目标；D2-A）。"""

    tenant_id = _tenant_or_403(publisher)
    try:
        view = await rollback_version(
            tenant_id=tenant_id,
            scope=publisher.scope_context,
            target_version_id=version_id,
            user_id=publisher.user_id,
            request_id=getattr(request.state, "request_id", None),
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return _publish_response(view)


@router.post("/versions/{version_id}/offline", response_model=KbVersionStateResponse)
async def offline_kb_document_version(
    version_id: UUID,
    publisher: Annotated[Principal, PublisherDep],
    request: Request,
) -> KbVersionStateResponse:
    """下线版本（PUBLISHED → OFFLINE 终态；活跃发布撤指针，collection 保留）。"""

    tenant_id = _tenant_or_403(publisher)
    try:
        view = await offline_version(
            tenant_id=tenant_id,
            scope=publisher.scope_context,
            version_id=version_id,
            user_id=publisher.user_id,
            request_id=getattr(request.state, "request_id", None),
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return KbVersionStateResponse(
        document_id=view.document_id,
        version_id=view.version_id,
        status=view.status,
        was_active=view.was_active,
    )


@router.delete("/versions/{version_id}", response_model=KbVersionStateResponse)
async def delete_kb_document_version(
    version_id: UUID,
    deleter: Annotated[Principal, DeleterDep],
    request: Request,
) -> KbVersionStateResponse:
    """软删版本（PUBLISHED → DELETED 终态；审计；collection 保留至 M6 清理）。"""

    tenant_id = _tenant_or_403(deleter)
    try:
        view = await delete_version(
            tenant_id=tenant_id,
            scope=deleter.scope_context,
            version_id=version_id,
            user_id=deleter.user_id,
            request_id=getattr(request.state, "request_id", None),
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return KbVersionStateResponse(
        document_id=view.document_id,
        version_id=view.version_id,
        status=view.status,
        was_active=view.was_active,
    )


# ---------------------------------------------------------------------------
# 分块预览（KB-06A：只读；权限 school.kb.view_chunks；实现见 app/kb/chunk_read.py）
# ---------------------------------------------------------------------------


class KbChunkResponse(BaseModel):
    """知识库分块（KB-10 的人工编辑三列不暴露）。"""

    id: UUID
    chunk_type: str
    chunk_index: int
    parent_id: UUID | None
    text: str
    text_markdown: str
    section_path: str | None
    page_start: int | None
    page_end: int | None
    token_count: int
    char_count: int
    embedding_status: str
    created_at: datetime


class KbChunkRef(BaseModel):
    """父/子块引用。"""

    id: UUID
    chunk_index: int


class KbChunkDetailResponse(KbChunkResponse):
    """分块详情（含版本归属与父/子引用）。"""

    document_version_id: UUID
    parent: KbChunkRef | None
    children: list[KbChunkRef]


class KbChunkSectionStat(BaseModel):
    """章节统计行（chunk_count 降序、路径升序）。"""

    section_path: str
    chunk_count: int


class KbChunkStatsResponse(BaseModel):
    """版本分块统计（per-version 固定口径，不随筛选参数变化）。"""

    total_chunks: int
    parent_count: int
    child_count: int
    embedding_status: dict[str, int]
    total_tokens: int
    total_chars: int
    section_count: int
    sections: list[KbChunkSectionStat]


def _chunk_response(view: ChunkView) -> KbChunkResponse:
    return KbChunkResponse(
        id=view.id,
        chunk_type=view.chunk_type,
        chunk_index=view.chunk_index,
        parent_id=view.parent_id,
        text=view.text,
        text_markdown=view.text_markdown,
        section_path=view.section_path,
        page_start=view.page_start,
        page_end=view.page_end,
        token_count=view.token_count,
        char_count=view.char_count,
        embedding_status=view.embedding_status,
        created_at=view.created_at,
    )


def _chunk_ref_response(ref: ChunkRef) -> KbChunkRef:
    return KbChunkRef(id=ref.id, chunk_index=ref.chunk_index)


def _chunk_stats_response(stats: ChunkStats) -> KbChunkStatsResponse:
    return KbChunkStatsResponse(
        total_chunks=stats.total_chunks,
        parent_count=stats.parent_count,
        child_count=stats.child_count,
        embedding_status=stats.embedding_status,
        total_tokens=stats.total_tokens,
        total_chars=stats.total_chars,
        section_count=stats.section_count,
        sections=[
            KbChunkSectionStat(section_path=s.section_path, chunk_count=s.chunk_count)
            for s in stats.sections
        ],
    )


@router.get("/versions/{version_id}/chunks", response_model=PageResponse[KbChunkResponse])
async def list_kb_document_chunks(
    version_id: UUID,
    viewer: Annotated[Principal, ChunkViewerDep],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    section_path: Annotated[str | None, Query(max_length=500)] = None,
    chunk_type: Annotated[Literal["parent", "child"] | None, Query()] = None,
    parent_id: UUID | None = None,
) -> PageResponse[KbChunkResponse]:
    """按版本列出分块（`chunk_index ASC` 游标分页；q/章节/类型/父块筛选）。"""

    tenant_id = _tenant_or_403(viewer)
    try:
        page = await list_chunks(
            tenant_id=tenant_id,
            scope=viewer.scope_context,
            version_id=version_id,
            limit=limit,
            cursor=cursor,
            q=q,
            section_path=section_path,
            chunk_type=chunk_type,
            parent_id=parent_id,
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return PageResponse(
        items=[_chunk_response(chunk) for chunk in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/chunks/{chunk_id}", response_model=KbChunkDetailResponse)
async def get_kb_chunk(
    chunk_id: UUID, viewer: Annotated[Principal, ChunkViewerDep]
) -> KbChunkDetailResponse:
    """分块详情（不存在/跨租/范围外同码 404，不泄露存在性）。"""

    tenant_id = _tenant_or_403(viewer)
    try:
        detail = await get_chunk(
            tenant_id=tenant_id, scope=viewer.scope_context, chunk_id=chunk_id
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return KbChunkDetailResponse(
        **_chunk_response(detail.chunk).model_dump(),
        document_version_id=detail.document_version_id,
        parent=_chunk_ref_response(detail.parent) if detail.parent is not None else None,
        children=[_chunk_ref_response(ref) for ref in detail.children],
    )


@router.get("/versions/{version_id}/chunks/stats", response_model=KbChunkStatsResponse)
async def get_kb_chunk_stats(
    version_id: UUID, viewer: Annotated[Principal, ChunkViewerDep]
) -> KbChunkStatsResponse:
    """版本分块统计（总数/父子/embedding/token/字符/章节清单）。"""

    tenant_id = _tenant_or_403(viewer)
    try:
        stats = await chunk_stats(
            tenant_id=tenant_id, scope=viewer.scope_context, version_id=version_id
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return _chunk_stats_response(stats)


# ---------------------------------------------------------------------------
# KB-07A：检索测试（docs/design-briefs/M3-KB-07.md；单 query 诊断面，只读不落库）
# ---------------------------------------------------------------------------


class KbRetrievalTestRouteHit(BaseModel):
    """单路（dense/sparse）命中：分数 + 该路 1-based 排名（None = 该路 pool×2 内未召回）。"""

    score: float
    rank: int


class KbRetrievalTestResultRow(BaseModel):
    """检索测试结果行（「为什么召回」逐路可解释 + 最终引用标记）。"""

    chunk_id: UUID
    chunk_index: int
    chunk_type: str
    section_path: str | None
    page_start: int | None
    page_end: int | None
    text_preview: str
    rrf_score: float
    dense: KbRetrievalTestRouteHit | None
    sparse: KbRetrievalTestRouteHit | None
    rerank_score: float | None
    final_rank: int
    is_final_reference: bool


class KbRetrievalTestResponse(BaseModel):
    """检索测试响应（pool_size = len(results)；results 按 final_rank 升序）。"""

    version_id: UUID
    query: str
    top_k: int
    with_rerank: bool
    rerank_status: RerankStatus
    latency_ms: float
    pool_size: int
    results: list[KbRetrievalTestResultRow]


class KbRetrievalTestRequest(BaseModel):
    """检索测试请求（ACL 视角三参数为 KB-09 契约预留，设值即 422，见路由守卫）。"""

    query: str = Field(min_length=1, max_length=300, description="raw 码点 ≤300；strip 后非空")
    top_k: int = Field(default=10, ge=1, le=20)
    with_rerank: bool = False
    as_user_id: UUID | None = None
    as_audiences: list[str] | None = None
    ignore_acl: bool = False


def _retrieval_test_response(result: RetrievalTestResult) -> KbRetrievalTestResponse:
    return KbRetrievalTestResponse(
        version_id=result.version_id,
        query=result.query,
        top_k=result.top_k,
        with_rerank=result.with_rerank,
        rerank_status=result.rerank_status,
        latency_ms=result.latency_ms,
        pool_size=len(result.results),
        results=[_retrieval_row_response(row, result.top_k) for row in result.results],
    )


def _retrieval_row_response(row: RetrievalRow, top_k: int) -> KbRetrievalTestResultRow:
    return KbRetrievalTestResultRow(
        chunk_id=row.chunk_id,
        chunk_index=row.chunk_index,
        chunk_type=row.chunk_type,
        section_path=row.section_path,
        page_start=row.page_start,
        page_end=row.page_end,
        text_preview=text_preview(row.text),
        rrf_score=row.rrf_score,
        dense=(
            KbRetrievalTestRouteHit(score=row.dense.score, rank=row.dense.rank)
            if row.dense is not None
            else None
        ),
        sparse=(
            KbRetrievalTestRouteHit(score=row.sparse.score, rank=row.sparse.rank)
            if row.sparse is not None
            else None
        ),
        rerank_score=row.rerank_score,
        final_rank=row.final_rank,
        is_final_reference=row.final_rank <= top_k,
    )


@router.post("/versions/{version_id}/retrieval-test", response_model=KbRetrievalTestResponse)
async def run_kb_retrieval_test(
    version_id: UUID,
    request: KbRetrievalTestRequest,
    viewer: Annotated[Principal, ChunkViewerDep],
) -> KbRetrievalTestResponse:
    """单 query 检索测试（dense/sparse/RRF/rerank 分数与最终引用；跨租/范围外 404）。"""

    tenant_id = _tenant_or_403(viewer)
    # KB-09 契约预留：任何 ACL 视角参数都显式拒绝（fail-closed），绝不静默忽略
    if request.as_user_id is not None or request.as_audiences is not None or request.ignore_acl:
        raise _http_error(
            KbError("RETRIEVAL_TEST_ACL_UNAVAILABLE", "ACL 视角检索未开放：随 KB-09 交付", 422)
        )
    query = request.query.strip()
    if not query:
        raise _http_error(KbError("RETRIEVAL_TEST_QUERY_REQUIRED", "检索词不能为空", 422))
    try:
        result = await run_retrieval_test(
            tenant_id=tenant_id,
            scope=viewer.scope_context,
            version_id=version_id,
            query=query,
            top_k=request.top_k,
            with_rerank=request.with_rerank,
        )
    except KbError as exc:
        raise _http_error(exc) from exc
    return _retrieval_test_response(result)
