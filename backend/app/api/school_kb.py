"""校方侧知识库接口（M3 KB-01A，`docs/design-briefs/M3-KB-01-upload.md`）。

- `POST /school/kb/documents`：multipart 批量上传（≤15 个、单文件 ≤50MB、合计 ≤300MB），
  必填 `Idempotency-Key`；整批原子，任一文件不合格整批 422；201 返回批次与文档（含派生扫描状态）；
- `GET /school/kb/documents`：按数据范围列出文档（`school` 全校 / `self` 只看自己上传的）；
- `GET /school/kb/documents/{document_id}`：文档详情；
- `GET /school/kb/documents/{document_id}/versions`：版本列表（分页）。

权限：上传 `school.kb.upload`；查询 `school.kb.view`。上传失败/幂等冲突写
`school.kb.upload.denied`（服务层独立事务）。本点不提供审批/构建接口。
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

from app.api.pagination import PageResponse
from app.auth.principal import Principal, require_permission
from app.kb.service import (
    DocumentView,
    IncomingFile,
    KbError,
    VersionView,
    get_document,
    list_documents,
    list_versions,
    upload_documents,
)
from app.kb.validation import MAX_FILE_BYTES

router = APIRouter(prefix="/school/kb", tags=["school-kb"])

UploaderDep = require_permission("school.kb.upload", realm="tenant")
ViewerDep = require_permission("school.kb.view", realm="tenant")

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
