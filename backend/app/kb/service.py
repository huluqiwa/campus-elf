"""知识库文档上传与查询服务（M3 KB-01A）。

口径（`docs/design-briefs/M3-KB-01-upload.md` D3/D4/D5，已确认）：
- **整批原子**：先逐个校验（只保留元数据，峰值内存 ≈ 单文件 50MB），任一失败整批 422；
- **幂等键必填**：`(tenant_id, idempotency_key)` 唯一；同键同指纹同创建人 → 重放原批次，
  否则 409 `IDEMPOTENCY_KEY_REUSED`；
- **对象先写、数据库后写**：DB 事务失败时逐个删除已写对象，避免留下无主对象；
- 上传要求权限即可，不按数据范围收窄；列表/详情/版本列表走 `principal.scope_context`，
  归属列 `created_by`，跨范围/跨租一律 404（fail-closed）；
- 扫描通过前版本保持 `UPLOADED`，接口派生 `scan.state`（pending/error/passed/infected/skipped）。
"""

import contextlib
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, NoReturn, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.writer import append_audit
from app.config import Settings, get_settings
from app.core.metrics import record_audit_write_failure
from app.db.models.kb import KbDocument, KbDocumentVersion, KbUploadBatch
from app.db.repository import CursorError, ScopeBypass, TenantScopedRepository
from app.db.session import db_session
from app.kb.status import (
    KB_STATUS_PENDING_APPROVAL,
    KB_STATUS_SCAN_FAILED,
    KB_STATUS_UPLOADED,
)
from app.kb.validation import (
    MAX_BATCH_BYTES,
    MAX_FILES_PER_BATCH,
    FileValidationError,
    validate_file,
)
from app.rbac.scopes import DataScopeContext
from app.storage.objects import ObjectStore, ObjectStoreError, build_object_store

logger = logging.getLogger(__name__)

_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_DEV_SKIP_ENGINE = "dev-skip"

_Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]


class KbError(Exception):
    """知识库业务错误（`code` 稳定、`http_status` 由接口层映射）。"""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = 422,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.extra = extra or {}


class FileMetadata(BaseModel):
    """单文件的可选元数据（multipart `metadata` 数组元素）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=200)
    tags: list[_Tag] = Field(default_factory=list, max_length=10)
    description: str | None = Field(default=None, max_length=1000)


@dataclass(frozen=True)
class IncomingFile:
    """接口层交给服务的文件读取器（可重复读取，用于校验后回写对象）。"""

    filename: str
    declared_mime: str | None
    read: Callable[[], Awaitable[bytes]]


@dataclass(frozen=True)
class ScanView:
    """派生扫描状态（不新增 SCANNING 状态，见简报 D5）。"""

    state: str
    engine: str | None
    signature: str | None
    error_code: str | None
    attempts: int
    scanned_at: datetime | None


@dataclass(frozen=True)
class VersionView:
    """版本读取视图。"""

    id: UUID
    version_no: int
    filename: str
    mime: str
    size_bytes: int
    status: str
    scan: ScanView
    created_at: datetime


@dataclass(frozen=True)
class DocumentView:
    """文档读取视图（含当前版本）。"""

    id: UUID
    title: str
    tags: list[str]
    description: str | None
    status: str
    current_version: VersionView | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class UploadOutcome:
    """一次上传的结果（`replayed=True` 表示幂等重放）。"""

    batch_id: UUID
    replayed: bool
    documents: list[DocumentView]


@dataclass(frozen=True)
class DocumentPage:
    """文档分页结果。"""

    items: list[DocumentView]
    next_cursor: str | None


@dataclass(frozen=True)
class VersionPage:
    """版本分页结果。"""

    items: list[VersionView]
    next_cursor: str | None


@dataclass(frozen=True)
class _PreparedFile:
    """通过校验的文件元数据（不含字节，避免整批进内存）。"""

    index: int
    filename: str
    extension: str
    mime: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class _PlannedObject:
    """对象 key 与服务端判定字段（写入前一次性生成）。"""

    prepared: _PreparedFile
    document_id: UUID
    version_id: UUID
    object_key: str


def derive_scan_state(version: KbDocumentVersion) -> str:
    """把版本行映射为接口展示的 `scan.state`。

    - `UPLOADED` 且无错误码 → pending（排队/扫描中）；有错误码 → error（瞬时故障等待重试）；
    - `PENDING_APPROVAL` + 开发旁路引擎 → skipped；否则 passed；
    - `SCAN_FAILED` + 签名 → infected；否则 error（完整性失败）。
    """

    if version.status == KB_STATUS_PENDING_APPROVAL:
        return "skipped" if version.scan_engine in {"dev-skip", "dev-degraded"} else "passed"
    if version.status == KB_STATUS_SCAN_FAILED:
        return "infected" if version.scan_signature else "error"
    return "error" if version.scan_error_code else "pending"


async def upload_documents(
    *,
    tenant_id: UUID,
    user_id: UUID,
    idempotency_key: str,
    files: Sequence[IncomingFile],
    metadata_raw: str | None,
    request_id: str | None = None,
    settings: Settings | None = None,
    store: ObjectStore | None = None,
) -> UploadOutcome:
    """整批上传知识库文件。

    参数:
        tenant_id: 当前租户。
        user_id: 上传者（文档归属列 `created_by`）。
        idempotency_key: 必填幂等键（`[A-Za-z0-9._:-]{8,128}`）。
        files: 文件读取器（按上传顺序）。
        metadata_raw: multipart `metadata` 字段（JSON 数组；缺省用文件名作标题）。
        request_id: 请求 id（写入审计）。
        settings: 可选配置覆盖（测试注入）。
        store: 可选对象存储覆盖（测试注入）。

    返回值:
        UploadOutcome（含批次 id 与文档视图）。

    异常:
        KbError: 校验失败（422）、幂等键复用（409）、文档不存在（404）、
            对象存储不可用（503）或落库失败（503）。
    """

    resolved = settings if settings is not None else get_settings()
    if len(files) == 0:
        await _deny(
            KbError("NO_FILES", "请至少上传一个文件", 422),
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=request_id,
            reason="no_files",
            codes=["NO_FILES"],
            file_count=0,
        )
    if len(files) > MAX_FILES_PER_BATCH:
        await _deny(
            KbError(
                "TOO_MANY_FILES",
                f"一次最多上传 {MAX_FILES_PER_BATCH} 个文件（当前 {len(files)} 个）",
                422,
            ),
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=request_id,
            reason="too_many_files",
            codes=["TOO_MANY_FILES"],
            file_count=len(files),
        )
    if not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
        await _deny(
            KbError(
                "INVALID_IDEMPOTENCY_KEY",
                "Idempotency-Key 必须为 8–128 位字母、数字或 . _ : -",
                422,
            ),
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=request_id,
            reason="invalid_idempotency_key",
            codes=["INVALID_IDEMPOTENCY_KEY"],
            file_count=len(files),
        )

    try:
        metadatas = parse_metadata(metadata_raw, len(files))
    except KbError as exc:
        await _deny(
            exc,
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=request_id,
            reason="invalid_metadata",
            codes=[exc.code],
            file_count=len(files),
        )

    prepared: list[_PreparedFile] = []
    errors: list[dict[str, Any]] = []
    total_bytes = 0
    for index, incoming in enumerate(files):
        try:
            data = await incoming.read()
            validated = validate_file(
                filename=incoming.filename,
                declared_mime=incoming.declared_mime,
                data=data,
            )
        except FileValidationError as exc:
            errors.append(
                {
                    "index": index,
                    "filename": _display_filename(incoming.filename),
                    "code": exc.code,
                    "message": exc.message,
                }
            )
            continue
        total_bytes += validated.size_bytes
        prepared.append(
            _PreparedFile(
                index=index,
                filename=validated.filename,
                extension=validated.extension,
                mime=validated.mime,
                size_bytes=validated.size_bytes,
                sha256=validated.sha256,
            )
        )

    if errors:
        await _deny(
            KbError(
                "BATCH_VALIDATION_FAILED",
                f"{len(errors)} 个文件未通过校验，整批未保存",
                422,
                extra={"files": errors},
            ),
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=request_id,
            reason="batch_validation_failed",
            codes=[str(error["code"]) for error in errors],
            file_count=len(files),
        )
    if total_bytes > MAX_BATCH_BYTES:
        await _deny(
            KbError(
                "BATCH_TOO_LARGE",
                f"单批文件合计不能超过 {MAX_BATCH_BYTES // (1024 * 1024)}MB",
                422,
            ),
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=request_id,
            reason="batch_too_large",
            codes=["BATCH_TOO_LARGE"],
            file_count=len(files),
        )

    fingerprint = _request_fingerprint(prepared)
    existing = await _find_batch_by_key(tenant_id, idempotency_key)
    if existing is not None:
        return await _replay_or_conflict(
            existing,
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=request_id,
            fingerprint=fingerprint,
            file_count=len(files),
        )

    batch_id = uuid4()
    planned: list[_PlannedObject] = []
    for item in prepared:
        document_id = uuid4()
        version_id = uuid4()
        planned.append(
            _PlannedObject(
                prepared=item,
                document_id=document_id,
                version_id=version_id,
                object_key=(
                    f"kb/{tenant_id}/{document_id}/{version_id}/source{item.extension}"
                ),
            )
        )

    resolved_store = store if store is not None else build_object_store(resolved)
    written_keys = await _write_objects(
        resolved_store,
        planned,
        files=files,
        tenant_id=tenant_id,
        user_id=user_id,
        request_id=request_id,
        file_count=len(files),
    )

    try:
        async with db_session(tenant_id=tenant_id) as session:
            await _persist_batch(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                batch_id=batch_id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                planned=planned,
                metadatas=metadatas,
                total_bytes=total_bytes,
                request_id=request_id,
                dev_skip=resolved.dev_skip_file_scan,
            )
    except IntegrityError as exc:
        await _cleanup_objects(resolved_store, written_keys)
        # 并发同键：另一请求已落库。指纹与创建人一致则按幂等重放，否则 409。
        existing = await _find_batch_by_key(tenant_id, idempotency_key)
        if existing is not None:
            outcome = await _replay_or_conflict(
                existing,
                tenant_id=tenant_id,
                user_id=user_id,
                request_id=request_id,
                fingerprint=fingerprint,
                file_count=len(files),
            )
            return outcome
        raise KbError("UPLOAD_PERSIST_FAILED", "上传落库失败，请稍后重试", 503) from exc
    except SQLAlchemyError as exc:
        await _cleanup_objects(resolved_store, written_keys)
        logger.warning("知识库上传落库失败：%s.%s", type(exc).__module__, type(exc).__name__)
        raise KbError("UPLOAD_PERSIST_FAILED", "上传落库失败，请稍后重试", 503) from exc

    if not resolved.dev_skip_file_scan:
        await _enqueue_scans(
            tenant_id, [item.version_id for item in planned]
        )

    documents = await _load_documents_by_batch(tenant_id, batch_id)
    return UploadOutcome(batch_id=batch_id, replayed=False, documents=documents)


def parse_metadata(raw: str | None, file_count: int) -> list[FileMetadata]:
    """解析 multipart `metadata`（缺省返回与文件数等长的空元数据）。

    异常:
        KbError: JSON 非法、条数不符或字段校验失败（`METADATA_INVALID`）。
    """

    if raw is None or not raw.strip():
        return [FileMetadata() for _ in range(file_count)]
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise KbError("METADATA_INVALID", "metadata 必须是合法 JSON", 422) from exc
    if not isinstance(payload, list):
        raise KbError("METADATA_INVALID", "metadata 必须是数组", 422)
    if len(payload) != file_count:
        raise KbError(
            "METADATA_INVALID",
            f"metadata 条数（{len(payload)}）必须与文件数（{file_count}）一致",
            422,
        )
    parsed: list[FileMetadata] = []
    for index, item in enumerate(payload):
        try:
            parsed.append(FileMetadata.model_validate(item))
        except ValidationError as exc:
            first = exc.errors()[0]
            location = ".".join(str(part) for part in first.get("loc", ())) or "(根)"
            raise KbError(
                "METADATA_INVALID",
                f"metadata 第 {index} 项不合法：{location} {first.get('msg', '')}".strip(),
                422,
            ) from exc
    return parsed


async def list_documents(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    limit: int = 20,
    cursor: str | None = None,
) -> DocumentPage:
    """按数据范围列出文档（`created_at DESC, id DESC` 游标分页）。

    异常:
        KbError: 游标非法（`INVALID_CURSOR`，400）。
    """

    async with db_session(tenant_id=tenant_id) as session:
        repository = _document_repository(session, tenant_id, scope)
        try:
            documents, next_cursor = await repository.list_cursor(limit=limit, cursor=cursor)
        except CursorError as exc:
            raise KbError("INVALID_CURSOR", "分页游标无效", 400) from exc
        versions = await _versions_by_id(
            session, tenant_id, scope, [doc.current_version_id for doc in documents]
        )
    return DocumentPage(
        items=[_document_view(doc, _version_of(versions, doc)) for doc in documents],
        next_cursor=next_cursor,
    )


async def get_document(
    *, tenant_id: UUID, scope: DataScopeContext, document_id: UUID
) -> DocumentView:
    """按 id 取文档（范围外/跨租视为不存在）。

    异常:
        KbError: 文档不存在（`DOCUMENT_NOT_FOUND`，404）。
    """

    async with db_session(tenant_id=tenant_id) as session:
        document = await _document_repository(session, tenant_id, scope).get(document_id)
        if document is None:
            raise KbError("DOCUMENT_NOT_FOUND", "文档不存在", 404)
        versions = await _versions_by_id(
            session, tenant_id, scope, [document.current_version_id]
        )
    return _document_view(document, _version_of(versions, document))


async def list_versions(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    document_id: UUID,
    limit: int = 20,
    cursor: str | None = None,
) -> VersionPage:
    """列出某文档的版本（同范围过滤；文档不可见即 404）。

    异常:
        KbError: 文档不存在（404）或游标非法（400）。
    """

    async with db_session(tenant_id=tenant_id) as session:
        document = await _document_repository(session, tenant_id, scope).get(document_id)
        if document is None:
            raise KbError("DOCUMENT_NOT_FOUND", "文档不存在", 404)
        repository = TenantScopedRepository(
            session,
            KbDocumentVersion,
            tenant_id,
            scope=scope,
            owner_attr="created_by",
        )
        try:
            versions, next_cursor = await repository.list_cursor(
                limit=limit,
                cursor=cursor,
                extra=KbDocumentVersion.document_id == document_id,
            )
        except CursorError as exc:
            raise KbError("INVALID_CURSOR", "分页游标无效", 400) from exc
    return VersionPage(items=[_version_view(version) for version in versions], next_cursor=next_cursor)


def _document_repository(
    session: AsyncSession, tenant_id: UUID, scope: DataScopeContext
) -> TenantScopedRepository[KbDocument]:
    return TenantScopedRepository(
        session, KbDocument, tenant_id, scope=scope, owner_attr="created_by"
    )


async def _versions_by_id(
    session: AsyncSession,
    tenant_id: UUID,
    scope: DataScopeContext,
    version_ids: Sequence[UUID | None],
) -> dict[UUID, KbDocumentVersion]:
    ids = [version_id for version_id in version_ids if version_id is not None]
    if not ids:
        return {}
    repository: TenantScopedRepository[KbDocumentVersion] = TenantScopedRepository(
        session,
        KbDocumentVersion,
        tenant_id,
        scope=scope,
        owner_attr="created_by",
    )
    rows = (
        await session.execute(repository.base_query().where(KbDocumentVersion.id.in_(ids)))
    ).scalars().all()
    return {row.id: row for row in rows}


def _version_of(
    versions: dict[UUID, KbDocumentVersion], document: KbDocument
) -> KbDocumentVersion | None:
    """取文档当前版本（`current_version_id` 为空时返回 None）。"""

    version_id = document.current_version_id
    return versions.get(version_id) if version_id is not None else None


def _document_view(
    document: KbDocument, version: KbDocumentVersion | None
) -> DocumentView:
    return DocumentView(
        id=document.id,
        title=document.title,
        tags=[str(tag) for tag in (document.tags or [])],
        description=document.description,
        status=document.status,
        current_version=_version_view(version) if version is not None else None,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _version_view(version: KbDocumentVersion) -> VersionView:
    return VersionView(
        id=version.id,
        version_no=version.version_no,
        filename=version.source_filename,
        mime=version.mime,
        size_bytes=version.size_bytes,
        status=version.status,
        scan=ScanView(
            state=derive_scan_state(version),
            engine=version.scan_engine,
            signature=version.scan_signature,
            error_code=version.scan_error_code,
            attempts=version.scan_attempts,
            scanned_at=version.scanned_at,
        ),
        created_at=version.created_at,
    )


def _request_fingerprint(prepared: Sequence[_PreparedFile]) -> str:
    """按顺序把 `filename|size|sha256` 规范化后取 SHA-256（幂等键复用判据）。"""

    lines = [f"{item.filename}|{item.size_bytes}|{item.sha256}" for item in prepared]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _display_filename(raw: str) -> str:
    """错误回显用文件名（净化且绝不抛错）。"""

    name = PurePosixPath(raw.replace("\\", "/")).name
    cleaned = "".join(ch for ch in name if ch.isprintable()).strip()
    return cleaned[:255] or "(未命名)"


def _default_title(filename: str) -> str:
    stem = PurePosixPath(filename).stem.strip()
    return (stem or filename)[:200]


async def _find_batch_by_key(
    tenant_id: UUID, idempotency_key: str
) -> KbUploadBatch | None:
    """按幂等键查批次（同租户；绕开数据范围以便识别「他人复用了该键」）。"""

    async with db_session(tenant_id=tenant_id) as session:
        repository: TenantScopedRepository[KbUploadBatch] = TenantScopedRepository(
            session,
            KbUploadBatch,
            tenant_id,
            scope=ScopeBypass(reason="幂等键定位原批次：需要看到同租户内任意创建人的键归属"),
        )
        stmt = repository.base_query().where(
            KbUploadBatch.idempotency_key == idempotency_key
        )
        return cast(
            KbUploadBatch | None, (await session.execute(stmt)).scalar_one_or_none()
        )


async def _replay_or_conflict(
    existing: KbUploadBatch,
    *,
    tenant_id: UUID,
    user_id: UUID,
    request_id: str | None,
    fingerprint: str,
    file_count: int,
) -> UploadOutcome:
    """同键重放：指纹与创建人一致才返回原批次，否则 409。"""

    if existing.created_by == user_id and existing.request_hash == fingerprint:
        documents = await _load_documents_by_batch(tenant_id, existing.id)
        return UploadOutcome(batch_id=existing.id, replayed=True, documents=documents)
    await _deny(
        KbError(
            "IDEMPOTENCY_KEY_REUSED",
            "该 Idempotency-Key 已用于另一次上传（文件或提交人不同）",
            409,
        ),
        tenant_id=tenant_id,
        user_id=user_id,
        request_id=request_id,
        reason="idempotency_conflict",
        codes=["IDEMPOTENCY_KEY_REUSED"],
        file_count=file_count,
    )


async def _load_documents_by_batch(tenant_id: UUID, batch_id: UUID) -> list[DocumentView]:
    """读取原批次的文档与当前版本（幂等重放；创建人已在调用方校验）。"""

    async with db_session(tenant_id=tenant_id) as session:
        bypass = ScopeBypass(reason="幂等重放：返回原批次的创建结果（创建人已校验）")
        document_repository: TenantScopedRepository[KbDocument] = TenantScopedRepository(
            session, KbDocument, tenant_id, scope=bypass
        )
        documents = (
            await session.execute(
                document_repository.base_query()
                .where(KbDocument.upload_batch_id == batch_id)
                .order_by(KbDocument.upload_file_index.asc())
            )
        ).scalars().all()
        version_repository: TenantScopedRepository[KbDocumentVersion] = TenantScopedRepository(
            session, KbDocumentVersion, tenant_id, scope=bypass
        )
        version_ids = [
            doc.current_version_id for doc in documents if doc.current_version_id is not None
        ]
        versions = (
            (
                await session.execute(
                    version_repository.base_query().where(
                        KbDocumentVersion.id.in_(version_ids)
                    )
                )
            )
            .scalars()
            .all()
            if version_ids
            else []
        )
    by_id = {version.id: version for version in versions}
    return [_document_view(doc, by_id.get(doc.current_version_id)) for doc in documents]


async def _write_objects(
    store: ObjectStore,
    planned: Sequence[_PlannedObject],
    *,
    files: Sequence[IncomingFile],
    tenant_id: UUID,
    user_id: UUID,
    request_id: str | None,
    file_count: int,
) -> list[str]:
    """按计划写对象（逐文件重新读取，峰值 ≈ 单文件 50MB）。

    异常:
        KbError: 文件在两次读取间变化（409）或对象存储不可用（503）；
            失败前已写入的对象会被清理。
    """

    written: list[str] = []
    try:
        for item in planned:
            data = await files[item.prepared.index].read()
            if hashlib.sha256(data).hexdigest() != item.prepared.sha256:
                raise KbError(
                    "UPLOAD_CHANGED", "上传文件在读取过程中发生变化，请重试", 409
                )
            await store.put(item.object_key, data, item.prepared.mime)
            written.append(item.object_key)
    except KbError:
        await _cleanup_objects(store, written)
        raise
    except (ObjectStoreError, OSError) as exc:
        await _cleanup_objects(store, written)
        logger.warning("知识库对象写入失败：%s.%s", type(exc).__module__, type(exc).__name__)
        raise KbError("OBJECT_STORE_UNAVAILABLE", "对象存储不可用，请稍后重试", 503) from exc
    return written


async def _persist_batch(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    batch_id: UUID,
    idempotency_key: str,
    fingerprint: str,
    planned: Sequence[_PlannedObject],
    metadatas: Sequence[FileMetadata],
    total_bytes: int,
    request_id: str | None,
    dev_skip: bool,
) -> None:
    """单事务写入 1 批次 + N 文档 + N 版本（文档/版本状态一致）。"""

    bypass = ScopeBypass(reason="上传写入：上传要求权限、不按数据范围收窄")
    batch_repository: TenantScopedRepository[KbUploadBatch] = TenantScopedRepository(
        session, KbUploadBatch, tenant_id, scope=bypass
    )
    document_repository: TenantScopedRepository[KbDocument] = TenantScopedRepository(
        session, KbDocument, tenant_id, scope=bypass, owner_attr="created_by"
    )
    version_repository: TenantScopedRepository[KbDocumentVersion] = TenantScopedRepository(
        session, KbDocumentVersion, tenant_id, scope=bypass, owner_attr="created_by"
    )

    await batch_repository.add(
        KbUploadBatch(
            id=batch_id,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            request_hash=fingerprint,
            file_count=len(planned),
            total_bytes=total_bytes,
            created_by=user_id,
        )
    )

    status = KB_STATUS_PENDING_APPROVAL if dev_skip else KB_STATUS_UPLOADED
    scanned_at = datetime.now(UTC) if dev_skip else None
    for item, metadata in zip(planned, metadatas, strict=True):
        document = KbDocument(
            id=item.document_id,
            tenant_id=tenant_id,
            title=metadata.title or _default_title(item.prepared.filename),
            tags=list(metadata.tags),
            description=metadata.description,
            status=status,
            current_version_id=None,
            upload_batch_id=batch_id,
            upload_file_index=item.prepared.index,
            created_by=user_id,
        )
        await document_repository.add(document)
        version = KbDocumentVersion(
            id=item.version_id,
            tenant_id=tenant_id,
            document_id=document.id,
            version_no=1,
            source_object_key=item.object_key,
            source_filename=item.prepared.filename,
            source_hash=item.prepared.sha256,
            mime=item.prepared.mime,
            size_bytes=item.prepared.size_bytes,
            status=status,
            scan_attempts=0,
            scan_engine=_DEV_SKIP_ENGINE if dev_skip else None,
            scanned_at=scanned_at,
            created_by=user_id,
        )
        await version_repository.add(version)
        document.current_version_id = version.id
        await session.flush()
        if dev_skip:
            await append_audit(
                session,
                action="school.kb.scan.passed",
                resource_type="kb_document_version",
                resource_id=str(version.id),
                actor_realm="tenant",
                actor_id=user_id,
                tenant_id=tenant_id,
                result="success",
                metadata={"engine": _DEV_SKIP_ENGINE},
                request_id=request_id,
            )

    await append_audit(
        session,
        action="school.kb.documents.uploaded",
        resource_type="kb_upload_batch",
        resource_id=str(batch_id),
        actor_realm="tenant",
        actor_id=user_id,
        tenant_id=tenant_id,
        result="success",
        metadata={
            "batch_id": str(batch_id),
            "file_count": len(planned),
            "total_bytes": total_bytes,
            "extensions": sorted({item.prepared.extension for item in planned}),
        },
        request_id=request_id,
    )


async def _enqueue_scans(tenant_id: UUID, version_ids: Sequence[UUID]) -> None:
    """提交后 best-effort 入队（队列不可用不失败；看门狗每 5 分钟补投）。"""

    from app.kb.scan_task import scan_job_id
    from app.workers.settings import create_producer_pool

    pool = None
    try:
        pool = await create_producer_pool()
        for version_id in version_ids:
            await pool.enqueue_job(
                "scan_kb_version",
                str(tenant_id),
                str(version_id),
                _job_id=scan_job_id(version_id, 0),
            )
    except Exception as exc:
        # 入队失败不放大权限：文档保持 UPLOADED，看门狗会补投；只记异常类型。
        logger.warning("知识库扫描入队失败（看门狗将补投）：%s.%s", type(exc).__module__, type(exc).__name__)
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.aclose()


async def _cleanup_objects(store: ObjectStore, keys: Sequence[str]) -> None:
    """删除本次已写对象（幂等）；清理失败只记日志，不影响主错误的返回。"""

    for key in keys:
        try:
            await store.delete(key)
        except Exception as exc:
            logger.warning(
                "知识库对象清理失败：%s（%s.%s）",
                key,
                type(exc).__module__,
                type(exc).__name__,
            )


async def _audit_denied(
    *,
    tenant_id: UUID,
    user_id: UUID,
    request_id: str | None,
    reason: str,
    codes: Sequence[str],
    file_count: int,
) -> None:
    """独立事务写上传拒绝审计（只落判据码与数量，不落文件内容）。"""

    try:
        async with db_session(tenant_id=tenant_id) as session:
            await append_audit(
                session,
                action="school.kb.upload.denied",
                resource_type="kb_upload",
                actor_realm="tenant",
                actor_id=user_id,
                tenant_id=tenant_id,
                result="denied",
                metadata={
                    "reason": reason,
                    "codes": sorted({str(code) for code in codes})[:20],
                    "file_count": file_count,
                },
                request_id=request_id,
            )
    except Exception as exc:
        record_audit_write_failure("school.kb.upload.denied")
        logger.warning(
            "知识库上传拒绝审计写入失败：%s.%s", type(exc).__module__, type(exc).__name__
        )


async def _deny(
    error: KbError,
    *,
    tenant_id: UUID,
    user_id: UUID,
    request_id: str | None,
    reason: str,
    codes: Sequence[str],
    file_count: int,
) -> NoReturn:
    """写拒绝审计后抛出业务错误。"""

    await _audit_denied(
        tenant_id=tenant_id,
        user_id=user_id,
        request_id=request_id,
        reason=reason,
        codes=codes,
        file_count=file_count,
    )
    raise error
