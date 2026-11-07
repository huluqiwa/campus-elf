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
- `PUT /school/kb/documents/{document_id}`：标题/标签/描述编辑（免审但审计前后值）。

权限：上传/提交/元数据 `school.kb.upload`；查询 `school.kb.view`；构建 `school.kb.build`。
拒绝审计：提交/构建的 403/409 写 `school.kb.approval.denied` / `school.kb.build.denied`
（服务层独立事务；404 探测不审计）。
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, StringConstraints

from app.api.pagination import PageResponse
from app.auth.principal import Principal, require_permission
from app.core.sse import SSE_HEADERS, format_heartbeat, format_sse
from app.kb.service import (
    ApprovalOutcome,
    BuildEventView,
    DocumentView,
    IncomingFile,
    KbError,
    VersionView,
    create_build,
    get_build_job,
    get_document,
    list_build_events,
    list_documents,
    list_version_approvals,
    list_versions,
    retry_build,
    submit_version_approval,
    update_document_metadata,
    upload_documents,
)
from app.kb.validation import MAX_FILE_BYTES

router = APIRouter(prefix="/school/kb", tags=["school-kb"])

UploaderDep = require_permission("school.kb.upload", realm="tenant")
ViewerDep = require_permission("school.kb.view", realm="tenant")
BuilderDep = require_permission("school.kb.build", realm="tenant")

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
