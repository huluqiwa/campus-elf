"""知识库文档上传与查询服务（M3 KB-01A）+ 审批与构建门禁（M3 KB-02）。

口径（`docs/design-briefs/M3-KB-01-upload.md` D3/D4/D5 与 `docs/design-briefs/M3-KB-02.md`
D1–D6，均已确认）：
- **整批原子**：先逐个校验（只保留元数据，峰值内存 ≈ 单文件 50MB），任一失败整批 422；
- **幂等键必填**：`(tenant_id, idempotency_key)` 唯一；同键同指纹同创建人 → 重放原批次，
  否则 409 `IDEMPOTENCY_KEY_REUSED`（KB-02/TD-277 起指纹包含 metadata 规范串）；
- **对象先写、数据库后写**：DB 事务失败时逐个删除已写对象，避免留下无主对象；
- 上传要求权限即可，不按数据范围收窄；列表/详情/版本列表走 `principal.scope_context`，
  归属列 `created_by`，跨范围/跨租一律 404（fail-closed）；
- 扫描通过前版本保持 `UPLOADED`，接口派生 `scan.state`（pending/error/passed/infected/skipped）；
- **审批门禁（KB-02）**：只有 `PENDING_APPROVAL` 可提交审批；审批人 = 提交人与上传者之外
  持 `school.kb.approval` 的用户（自批恒 403，不接例外开关）；通过/驳回在**同一事务**内
  双写版本与文档状态；未审批创建构建 409 + 审计（`kb_build_jobs` 零行）；
- 提交审批/构建/元数据编辑的 403/409 业务拒绝在**独立事务**写 denied 审计
  （404 探测不审计，不泄露存在性）。
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

from app.approval.service import (
    ApprovalError,
    ApprovalRequestView,
    cancel_request,
    decide_request,
    submit_request,
)
from app.audit.writer import append_audit
from app.config import Settings, get_settings
from app.core.metrics import record_audit_write_failure
from app.db.models.approval import APPROVAL_TYPE_KB_DOCUMENT, ApprovalRequest, ApprovalType
from app.db.models.kb import (
    KbBuildJob,
    KbDocument,
    KbDocumentVersion,
    KbUploadBatch,
)
from app.db.models.kb_events import KbBuildEvent
from app.db.repository import CursorError, ScopeBypass, TenantScopedRepository
from app.db.session import db_session
from app.kb.status import (
    KB_STATUS_APPROVED,
    KB_STATUS_FAILED,
    KB_STATUS_PENDING_APPROVAL,
    KB_STATUS_REJECTED,
    KB_STATUS_SCAN_FAILED,
    KB_STATUS_UPLOADED,
    InvalidStatusTransition,
    NotBuildableError,
    NotRetriableError,
    NotSubmittableError,
    assert_build_allowed,
    assert_retriable,
    assert_submittable,
    assert_transition,
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
    - `APPROVED` / `REJECTED`（KB-02）→ passed（能进入审批必然已通过扫描）；
    - `SCAN_FAILED` + 签名 → infected；否则 error（完整性失败）。
    """

    if version.status == KB_STATUS_PENDING_APPROVAL:
        return "skipped" if version.scan_engine in {"dev-skip", "dev-degraded"} else "passed"
    if version.status in (KB_STATUS_APPROVED, KB_STATUS_REJECTED):
        return "passed"
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

    fingerprint = _request_fingerprint(prepared, metadatas)
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


def _request_fingerprint(
    prepared: Sequence[_PreparedFile], metadatas: Sequence[FileMetadata]
) -> str:
    """幂等键复用判据（TD-277）：文件 `filename|size|sha256` + **metadata 规范串**一起入哈希。

    metadata 规范化：标题/描述取 strip 后值（缺省空串）、标签排序（集合语义，顺序不敏感）。
    否则「同键同文件、只改标题/标签」会被静默当成重放，元数据改动被吞。
    """

    lines = []
    for item, metadata in zip(prepared, metadatas, strict=True):
        meta_canonical = json.dumps(
            [
                (metadata.title or "").strip(),
                sorted(metadata.tags),
                (metadata.description or "").strip(),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        lines.append(f"{item.filename}|{item.size_bytes}|{item.sha256}|{meta_canonical}")
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


# ---------------------------------------------------------------------------
# KB-02：审批门禁与构建门禁（docs/design-briefs/M3-KB-02.md，已确认 D1–D6）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalOutcome:
    """提交/决定审批后的结果（请求视图 + 文档视图）。"""

    request: ApprovalRequestView
    document: DocumentView


@dataclass(frozen=True)
class BuildView:
    """构建任务读取视图（本点只到 BUILD_QUEUED；进度字段随 KB-03）。"""

    id: UUID
    version_id: UUID
    status: str
    attempt: int
    created_at: datetime


_APPROVAL_BYPASS = ScopeBypass(
    reason="审批是租户级治理动作（简报 D5）：按权限码鉴权，不按提交人数据范围裁剪"
)
_BUILD_BYPASS = ScopeBypass(
    reason="构建任务是租户级治理对象：按 school.kb.build 鉴权，不按提交人数据范围裁剪"
)


def _approval_request_repository(
    session: AsyncSession, tenant_id: UUID
) -> TenantScopedRepository[ApprovalRequest]:
    return TenantScopedRepository(
        session, ApprovalRequest, tenant_id, scope=_APPROVAL_BYPASS
    )


def _build_job_repository(
    session: AsyncSession, tenant_id: UUID
) -> TenantScopedRepository[KbBuildJob]:
    return TenantScopedRepository(session, KbBuildJob, tenant_id, scope=_BUILD_BYPASS)


def _as_kb_error(exc: ApprovalError) -> KbError:
    return KbError(exc.code, exc.message, exc.http_status, extra=exc.extra)


def _integrity_constraint_name(exc: IntegrityError) -> str | None:
    """从 IntegrityError 里取约束名；取不到返回 None（= 按「不是该约束」处理，宁可 500）。

    名字在 `exc.orig.__cause__` 上：SQLAlchemy 的 asyncpg 适配异常只搬运 `sqlstate`/`detail`，
    `constraint_name` 留在驱动的原生异常里。不做错误文本子串匹配——那会把名字恰好出现在
    SQL 文本里的其它失败一并误判（先例与理由见 `app/rbac/admin.py::_integrity_constraint_name`）。
    """

    orig: Any = exc.orig
    for candidate in (orig, getattr(orig, "__cause__", None)):
        name: Any = getattr(candidate, "constraint_name", None)
        if name is not None:
            return str(name)
    return None


def _version_snapshot(document: KbDocument, version: KbDocumentVersion) -> dict[str, Any]:
    """提交审批时的版本快照（进 `approval_requests.payload_json`，审批记录可追溯）。"""


    return {
        "document_id": str(document.id),
        "document_title": document.title,
        "tags": [str(tag) for tag in (document.tags or [])],
        "description": document.description,
        "version_id": str(version.id),
        "version_no": version.version_no,
        "source_filename": version.source_filename,
        "source_hash": version.source_hash,
        "mime": version.mime,
        "size_bytes": version.size_bytes,
        "scan_engine": version.scan_engine,
        "scanned_at": version.scanned_at.isoformat() if version.scanned_at else None,
        "created_by": str(version.created_by),
    }


async def _action_denied(
    *,
    action: str,
    tenant_id: UUID,
    user_id: UUID,
    request_id: str | None,
    code: str,
    context: dict[str, Any],
) -> None:
    """独立事务写业务拒绝审计（403/409；404 探测不审计，不泄露存在性）。"""

    try:
        async with db_session(tenant_id=tenant_id) as session:
            await append_audit(
                session,
                action=action,
                resource_type="kb_document_version",
                actor_realm="tenant",
                actor_id=user_id,
                tenant_id=tenant_id,
                result="denied",
                metadata={"code": code, **context},
                request_id=request_id,
            )
    except Exception as exc:
        record_audit_write_failure(action)
        logger.warning(
            "知识库拒绝审计写入失败（%s）：%s.%s", action, type(exc).__module__, type(exc).__name__
        )


async def submit_version_approval(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    version_id: UUID,
    user_id: UUID,
    request_id: str | None = None,
) -> ApprovalOutcome:
    """把 `PENDING_APPROVAL` 版本提交审批（开审批单 + submit 留痕 + 审计）。

    异常:
        KbError: VERSION_NOT_FOUND/DOCUMENT_NOT_FOUND（404，不审计）；
            KB_NOT_SUBMITTABLE / APPROVAL_ALREADY_PENDING（409，独立事务审计）。
    """

    try:
        async with db_session(tenant_id=tenant_id) as session:
            version_repository: TenantScopedRepository[KbDocumentVersion] = (
                TenantScopedRepository(
                    session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
                )
            )
            version = await version_repository.get(version_id)
            if version is None:
                raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
            try:
                assert_submittable(version.status)
            except NotSubmittableError as exc:
                raise KbError("KB_NOT_SUBMITTABLE", str(exc), 409) from exc
            document_repository: TenantScopedRepository[KbDocument] = TenantScopedRepository(
                session, KbDocument, tenant_id, scope=scope, owner_attr="created_by"
            )
            document = await document_repository.get(version.document_id)
            if document is None:
                raise KbError("DOCUMENT_NOT_FOUND", "文档不存在", 404)

            request_repository = _approval_request_repository(session, tenant_id)
            pending = (
                await session.execute(
                    request_repository.base_query().where(
                        ApprovalRequest.document_version_id == version.id,
                        ApprovalRequest.status == "PENDING",
                    )
                )
            ).scalar_one_or_none()
            if pending is not None:
                raise KbError(
                    "APPROVAL_ALREADY_PENDING", "该版本已有待审批的申请", 409
                )

            try:
                request_view = await submit_request(
                    session,
                    tenant_id=tenant_id,
                    # 列宽 200：前缀 8 字 + 文档标题（≤200）会溢出，按 200 截断
                    # （完整标题在 payload 快照的 document_title 里，不丢信息）。
                    title=f"知识库文档审批：{document.title}"[:200],
                    submitter_id=user_id,
                    payload=_version_snapshot(document, version),
                    document_version_id=version.id,
                )
            except IntegrityError as exc:
                # 并发同版本二次提交：部分唯一索引兜底。其余完整性错误按库层故障原样上抛，
                # 不把 FK 违约等伪装成业务冲突（口径同 M2-UI2 的 ROLE_CODE_TAKEN 判据）。
                if _integrity_constraint_name(
                    exc
                ) != "uq_approval_requests_tenant_version_pending":
                    raise
                raise KbError(
                    "APPROVAL_ALREADY_PENDING", "该版本已有待审批的申请", 409
                ) from exc

            await append_audit(
                session,
                action="school.kb.approval.submitted",
                resource_type="kb_document_version",
                resource_id=str(version.id),
                actor_realm="tenant",
                actor_id=user_id,
                tenant_id=tenant_id,
                result="success",
                metadata={
                    "approval_request_id": str(request_view.id),
                    "version_no": version.version_no,
                    "document_id": str(document.id),
                    "source_filename": version.source_filename,
                },
                request_id=request_id,
            )
            return ApprovalOutcome(
                request=request_view, document=_document_view(document, version)
            )
    except ApprovalError as exc:
        # 同上：兄弟 handler 不接力，APPROVAL_TYPE_DISABLED（409）的拒绝审计在这里做。
        error = _as_kb_error(exc)
        if error.http_status in (403, 409):
            await _action_denied(
                action="school.kb.approval.denied",
                tenant_id=tenant_id,
                user_id=user_id,
                request_id=request_id,
                code=error.code,
                context={"version_id": str(version_id)},
            )
        raise error from exc
    except KbError as exc:
        if exc.http_status in (403, 409):
            await _action_denied(
                action="school.kb.approval.denied",
                tenant_id=tenant_id,
                user_id=user_id,
                request_id=request_id,
                code=exc.code,
                context={"version_id": str(version_id)},
            )
        raise


async def decide_version_approval(
    *,
    tenant_id: UUID,
    approval_request_id: UUID,
    actor_id: UUID,
    action: str,
    reason: str | None,
    request_id: str | None = None,
) -> ApprovalOutcome:
    """通过/驳回一个 KB_DOCUMENT 审批单（同一事务内迁移版本与文档状态）。

    自批禁令：审批人 = 提交人或版本上传者 → 403（`sod.py` 判定内核，无例外通道）。
    """

    target = KB_STATUS_APPROVED if action == "approve" else KB_STATUS_REJECTED
    try:
        async with db_session(tenant_id=tenant_id) as session:
            request_repository = _approval_request_repository(session, tenant_id)
            approval = await request_repository.get(approval_request_id)
            if approval is None:
                raise KbError("APPROVAL_NOT_FOUND", "审批单不存在", 404)

            type_repository: TenantScopedRepository[ApprovalType] = TenantScopedRepository(
                session, ApprovalType, tenant_id, scope=_APPROVAL_BYPASS
            )
            approval_type = await type_repository.get(approval.type_id)
            if approval_type is None or approval_type.code != APPROVAL_TYPE_KB_DOCUMENT:
                raise KbError(
                    "APPROVAL_TYPE_NOT_SUPPORTED", "该审批单类型不是知识库文档审批", 409
                )

            version_repository: TenantScopedRepository[KbDocumentVersion] = (
                TenantScopedRepository(
                    session,
                    KbDocumentVersion,
                    tenant_id,
                    scope=_APPROVAL_BYPASS,
                    owner_attr="created_by",
                )
            )
            if approval.document_version_id is None:
                raise KbError(
                    "APPROVAL_TYPE_NOT_SUPPORTED", "该审批单未绑定文档版本", 409
                )
            version = await version_repository.get(approval.document_version_id)
            if version is None:
                raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
            # 状态机先行判据：版本必须仍处于 PENDING_APPROVAL 且目标迁移合法。
            assert_transition(version.status, target)

            request_view = await decide_request(
                session,
                tenant_id=tenant_id,
                request_id=approval_request_id,
                actor_id=actor_id,
                action=action,
                reason=reason,
                forbidden_actor_ids=(approval.submitter_id, version.created_by),
            )

            # 锁序：审批单（engine 内）→ 版本 → 文档；与 scan_task 的 版本→文档 无环。
            # 行锁下重校验：version 在前面已被普通读过（身份映射缓存 PENDING），
            # 锁读必须 populate_existing 才能拿到锁等待结束后的最新行（见引擎 _locked_request 注释）。
            locked_stmt = (
                version_repository.base_query()
                .where(KbDocumentVersion.id == version.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            locked_version = cast(
                KbDocumentVersion | None,
                (await session.execute(locked_stmt)).scalar_one_or_none(),
            )
            if locked_version is None or locked_version.status != KB_STATUS_PENDING_APPROVAL:
                raise KbError("KB_INVALID_TRANSITION", "文档版本状态已变化，审批未生效", 409)
            locked_version.status = target
            document_repository: TenantScopedRepository[KbDocument] = TenantScopedRepository(
                session,
                KbDocument,
                tenant_id,
                scope=ScopeBypass(
                    reason="审批决定的同事务状态双写：授权已由权限码与自批判据完成"
                ),
            )
            locked_document = await document_repository.get(
                version.document_id, with_for_update=True
            )
            if locked_document is None:
                raise KbError("DOCUMENT_NOT_FOUND", "文档不存在", 404)
            locked_document.status = target

            await append_audit(
                session,
                action="school.kb.approval.approved"
                if action == "approve"
                else "school.kb.approval.rejected",
                resource_type="kb_document_version",
                resource_id=str(version.id),
                actor_realm="tenant",
                actor_id=actor_id,
                tenant_id=tenant_id,
                result="success",
                metadata={
                    "approval_request_id": str(approval_request_id),
                    "version_no": version.version_no,
                    "from_status": KB_STATUS_PENDING_APPROVAL,
                    "to_status": target,
                    "reason": (reason or "").strip() or None,
                },
                request_id=request_id,
            )
            # flush 会 expire onupdate 列（updated_at）：惰性刷新在 greenlet 外会炸
            # （MissingGreenlet）。按 KB-01A 的 `_load_documents_by_batch` 范式重查建视图。
            fresh_document = await document_repository.get(version.document_id)
            fresh_version = await version_repository.get(version.id)
            if fresh_document is None or fresh_version is None:
                raise KbError("DOCUMENT_NOT_FOUND", "文档不存在", 404)
            return ApprovalOutcome(
                request=request_view,
                document=_document_view(fresh_document, fresh_version),
            )
    except ApprovalError as exc:
        # 注意：本分支 re-raise 的 KbError 不会再被下方 except KbError 捕获
        # （Python 语义：同一 try 内兄弟 handler 不接力），拒绝审计必须在这里做。
        error = _as_kb_error(exc)
        if error.http_status in (403, 409):
            await _action_denied(
                action="school.kb.approval.denied",
                tenant_id=tenant_id,
                user_id=actor_id,
                request_id=request_id,
                code=error.code,
                context={"approval_request_id": str(approval_request_id)},
            )
        raise error from exc
    except InvalidStatusTransition as exc:
        raise KbError("KB_INVALID_TRANSITION", str(exc), 409) from exc
    except KbError as exc:
        if exc.http_status in (403, 409):
            await _action_denied(
                action="school.kb.approval.denied",
                tenant_id=tenant_id,
                user_id=actor_id,
                request_id=request_id,
                code=exc.code,
                context={"approval_request_id": str(approval_request_id)},
            )
        raise


async def cancel_version_approval(
    *,
    tenant_id: UUID,
    approval_request_id: UUID,
    actor_id: UUID,
    request_id: str | None = None,
) -> ApprovalRequestView:
    """提交人撤销自己的 PENDING 审批单（版本保持 PENDING_APPROVAL，可再次提交）。"""

    try:
        async with db_session(tenant_id=tenant_id) as session:
            request_view = await cancel_request(
                session,
                tenant_id=tenant_id,
                request_id=approval_request_id,
                actor_id=actor_id,
            )
            version_id = request_view.document_version_id
            await append_audit(
                session,
                action="school.kb.approval.cancelled",
                resource_type="kb_document_version",
                resource_id=str(version_id) if version_id else None,
                actor_realm="tenant",
                actor_id=actor_id,
                tenant_id=tenant_id,
                result="success",
                metadata={"approval_request_id": str(approval_request_id)},
                request_id=request_id,
            )
            return request_view
    except ApprovalError as exc:
        error = _as_kb_error(exc)
        if error.http_status in (403, 409):
            await _action_denied(
                action="school.kb.approval.denied",
                tenant_id=tenant_id,
                user_id=actor_id,
                request_id=request_id,
                code=error.code,
                context={"approval_request_id": str(approval_request_id)},
            )
        raise error from exc


@dataclass(frozen=True)
class BuildEventView:
    """构建事件读取视图（SSE 回放帧负载）。"""

    seq: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class BuildJobSnapshot:
    """构建任务快照（TD-288：全字段；进度/阶段/tier/错误供轮询兜底与 KB-03B SSE 复用）。"""

    id: UUID
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
    created_by: UUID
    created_at: datetime
    started_at: datetime | None
    heartbeat_at: datetime | None
    finished_at: datetime | None


async def list_build_events(
    *,
    tenant_id: UUID,
    job_id: UUID,
    after_seq: int = 0,
) -> list[BuildEventView]:
    """按 seq 升序列出 job 的构建事件（SSE 回放/续传基底；`?since=N` 只回 N 之后）。"""

    async with db_session(tenant_id=tenant_id) as session:
        repository: TenantScopedRepository[KbBuildEvent] = TenantScopedRepository(
            session,
            KbBuildEvent,
            tenant_id,
            scope=ScopeBypass(reason="构建事件回放：按 job id 精确读取，school.kb.view 判据在外层"),
        )
        rows_out = (
            (
                await session.execute(
                    repository.base_query()
                    .where(
                        KbBuildEvent.job_id == job_id,
                        KbBuildEvent.seq > after_seq,
                    )
                    .order_by(KbBuildEvent.seq.asc())
                )
            )
            .scalars()
            .all()
        )
        return [
            BuildEventView(
                seq=event.seq,
                event_type=event.event_type,
                payload=dict(event.payload_json),
                created_at=event.created_at,
            )
            for event in rows_out
        ]


async def list_version_approvals(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    version_id: UUID,
    limit: int = 20,
) -> list[ApprovalRequestView]:
    """TD-291：某版本的审批单列表（created_at DESC；撤销/重提交的多行历史全可见）。"""

    from app.approval.service import _request_repository, _request_view

    async with db_session(tenant_id=tenant_id) as session:
        version_repository: TenantScopedRepository[KbDocumentVersion] = TenantScopedRepository(
            session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
        )
        version = await version_repository.get(version_id)
        if version is None:
            raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
        repository = _request_repository(session, tenant_id)
        rows_out = (
            (
                await session.execute(
                    repository.base_query()
                    .where(ApprovalRequest.document_version_id == version_id)
                    .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
                    .limit(max(1, min(limit, 100)))
                )
            )
            .scalars()
            .all()
        )
        if not rows_out:
            return []
        type_repository: TenantScopedRepository[ApprovalType] = TenantScopedRepository(
            session, ApprovalType, tenant_id, scope=_APPROVAL_BYPASS
        )
        type_rows = (
            (
                await session.execute(
                    type_repository.base_query().where(
                        ApprovalType.id.in_({row.type_id for row in rows_out})
                    )
                )
            )
            .scalars()
            .all()
        )
        type_codes = {row.id: row.code for row in type_rows}
        return [_request_view(row, type_codes.get(row.type_id, "")) for row in rows_out]


async def get_build_job(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    job_id: UUID,
) -> BuildJobSnapshot:
    """构建任务快照（`school.kb.view` + 数据范围；job 挂在文档上，范围外 404）。"""

    async with db_session(tenant_id=tenant_id) as session:
        job = await _build_job_repository(session, tenant_id).get(job_id)
        if job is None:
            raise KbError("BUILD_JOB_NOT_FOUND", "构建任务不存在", 404)
        version_repository: TenantScopedRepository[KbDocumentVersion] = TenantScopedRepository(
            session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
        )
        version = await version_repository.get(job.version_id)
        if version is None:
            raise KbError("BUILD_JOB_NOT_FOUND", "构建任务不存在", 404)
        return BuildJobSnapshot(
            id=job.id,
            version_id=job.version_id,
            document_id=version.document_id,
            status=job.status,
            stage=job.stage,
            progress=job.progress,
            processed_units=job.processed_units,
            total_units=job.total_units,
            tier=job.tier,
            fallback_reason=job.fallback_reason,
            attempt=job.attempt,
            retry_count=job.retry_count,
            error_code=job.error_code,
            message=job.message,
            created_by=job.created_by,
            created_at=job.created_at,
            started_at=job.started_at,
            heartbeat_at=job.heartbeat_at,
            finished_at=job.finished_at,
        )


async def retry_build(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    job_id: UUID,
    user_id: UUID,
    request_id: str | None = None,
) -> BuildJobSnapshot:
    """重试失败的构建任务（只有 FAILED 可重试；attempt+1，历史不覆盖）。

    状态机：job FAILED → BUILD_QUEUED（attempt+1/retry_count+1，stage/error 清空）；
    版本 FAILED → 取件时迁移 BUILDING（本端点只把 job 排回队列）。
    """

    try:
        async with db_session(tenant_id=tenant_id) as session:
            job = await _build_job_repository(session, tenant_id).get(job_id)
            if job is None:
                raise KbError("BUILD_JOB_NOT_FOUND", "构建任务不存在", 404)
            try:
                assert_retriable(job.status)
            except NotRetriableError as exc:
                raise KbError("KB_BUILD_NOT_RETRIABLE", str(exc), 409) from exc
            version_repository: TenantScopedRepository[KbDocumentVersion] = TenantScopedRepository(
                session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
            )
            version = await version_repository.get(job.version_id)
            if version is None:
                raise KbError("BUILD_JOB_NOT_FOUND", "构建任务不存在", 404)
            if version.status != KB_STATUS_FAILED:
                raise KbError(
                    "KB_BUILD_NOT_RETRIABLE",
                    f"版本状态 {version.status} 与失败任务不一致，不能重试",
                    409,
                )
            job.status = "BUILD_QUEUED"
            job.stage = None
            job.error_code = None
            job.message = None
            job.progress = None
            job.processed_units = None
            job.total_units = None
            job.finished_at = None
            job.started_at = None
            job.heartbeat_at = None
            job.attempt += 1
            job.retry_count += 1
            await append_audit(
                session,
                action="school.kb.build.retry",
                resource_type="kb_document_version",
                resource_id=str(job.version_id),
                actor_realm="tenant",
                actor_id=user_id,
                tenant_id=tenant_id,
                result="success",
                metadata={
                    "job_id": str(job.id),
                    "attempt": job.attempt,
                    "retry_count": job.retry_count,
                },
                request_id=request_id,
            )
            snapshot = BuildJobSnapshot(
                id=job.id,
                version_id=job.version_id,
                document_id=version.document_id,
                status=job.status,
                stage=job.stage,
                progress=job.progress,
                processed_units=job.processed_units,
                total_units=job.total_units,
                tier=job.tier,
                fallback_reason=job.fallback_reason,
                attempt=job.attempt,
                retry_count=job.retry_count,
                error_code=job.error_code,
                message=job.message,
                created_by=job.created_by,
                created_at=job.created_at,
                started_at=job.started_at,
                heartbeat_at=job.heartbeat_at,
                finished_at=job.finished_at,
            )
            attempt = job.attempt
    except KbError as exc:
        if exc.http_status in (403, 409):
            await _action_denied(
                action="school.kb.build.denied",
                tenant_id=tenant_id,
                user_id=user_id,
                request_id=request_id,
                code=exc.code,
                context={"job_id": str(job_id)},
            )
        raise
    await _enqueue_build(tenant_id, snapshot.version_id, attempt)
    return snapshot


async def create_build(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    version_id: UUID,
    user_id: UUID,
    request_id: str | None = None,
) -> BuildView:
    """创建构建任务（门禁：只有 APPROVED；同版本最多一个活跃任务）。

    KB-03A：落 `BUILD_QUEUED` 行 + 审计后 **best-effort 入队**（worker 取件执行；
    入队失败不失败——retry 端点可手动兜底，QUEUED 行也不会被看门狗误标 STALE）。
    """

    job_id: UUID | None = None
    job_attempt = 1
    job_created: datetime | None = None
    try:
        async with db_session(tenant_id=tenant_id) as session:
            version_repository: TenantScopedRepository[KbDocumentVersion] = (
                TenantScopedRepository(
                    session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
                )
            )
            version = await version_repository.get(version_id)
            if version is None:
                raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
            try:
                assert_build_allowed(version.status)
            except NotBuildableError as exc:
                raise KbError("KB_NOT_APPROVED", str(exc), 409) from exc

            job_repository = _build_job_repository(session, tenant_id)
            active = (
                await session.execute(
                    job_repository.base_query().where(
                        KbBuildJob.version_id == version.id,
                        KbBuildJob.status == "BUILD_QUEUED",
                    )
                )
            ).scalar_one_or_none()
            if active is not None:
                raise KbError(
                    "BUILD_ALREADY_ACTIVE",
                    "该版本已有进行中的构建任务",
                    409,
                    extra={"job_id": str(active.id)},
                )
            try:
                job = KbBuildJob(
                    tenant_id=tenant_id,
                    version_id=version.id,
                    status="BUILD_QUEUED",
                    attempt=1,
                    created_by=user_id,
                )
                await job_repository.add(job)
                await session.flush()
            except IntegrityError as exc:
                # 并发创建：部分唯一索引 (tenant_id, version_id) WHERE active 兜底。
                # 其余完整性错误（如并发删除版本导致 FK 违约）原样上抛，不伪装成业务冲突。
                if _integrity_constraint_name(exc) != "uq_kb_build_jobs_tenant_version_active":
                    raise
                raise KbError("BUILD_ALREADY_ACTIVE", "该版本已有进行中的构建任务", 409) from exc

            await append_audit(
                session,
                action="school.kb.build.created",
                resource_type="kb_document_version",
                resource_id=str(version.id),
                actor_realm="tenant",
                actor_id=user_id,
                tenant_id=tenant_id,
                result="success",
                metadata={"job_id": str(job.id), "version_no": version.version_no},
                request_id=request_id,
            )
            job_id = job.id
            job_attempt = job.attempt
            job_created = job.created_at
    except KbError as exc:
        if exc.http_status in (403, 409):
            await _action_denied(
                action="school.kb.build.denied",
                tenant_id=tenant_id,
                user_id=user_id,
                request_id=request_id,
                code=exc.code,
                context={"version_id": str(version_id)},
            )
        raise
    if job_id is None or job_created is None:
        raise KbError("BUILD_CREATE_FAILED", "构建任务创建失败，请稍后重试", 503)
    await _enqueue_build(tenant_id, version_id, job_attempt)
    return BuildView(
        id=job_id,
        version_id=version_id,
        status="BUILD_QUEUED",
        attempt=job_attempt,
        created_at=job_created,
    )


async def _enqueue_build(tenant_id: UUID, version_id: UUID, attempt: int) -> None:
    """构建任务 best-effort 入队（与扫描同款：队列不可用不失败，retry 端点兜底）。"""

    from app.kb.build_task import build_job_id
    from app.workers.settings import create_producer_pool

    pool = None
    try:
        pool = await create_producer_pool()
        await pool.enqueue_job(
            "build_kb_version",
            str(tenant_id),
            str(version_id),
            _job_id=build_job_id(version_id, attempt),
        )
    except Exception as exc:
        logger.warning(
            "构建任务入队失败（可用 retry 端点兜底）：%s.%s",
            type(exc).__module__,
            type(exc).__name__,
        )
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.aclose()


async def update_document_metadata(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    document_id: UUID,
    user_id: UUID,
    title: str | None = None,
    tags: list[str] | None = None,
    description: str | None = None,
    description_provided: bool = False,
    request_id: str | None = None,
) -> DocumentView:
    """更新文档标题/标签/描述（免审但审计：KB-02 验收「不重审，审计前后值」）。

    字段缺省 = 不更新；`description_provided=True` 时以 `description` 为准（None = 清空）。
    版本状态不因元数据变更而重审。
    """

    _validate_metadata_fields(title=title, tags=tags, description=description)

    async with db_session(tenant_id=tenant_id) as session:
        document_repository: TenantScopedRepository[KbDocument] = TenantScopedRepository(
            session, KbDocument, tenant_id, scope=scope, owner_attr="created_by"
        )
        document = await document_repository.get(document_id)
        if document is None:
            raise KbError("DOCUMENT_NOT_FOUND", "文档不存在", 404)

        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        # 存储/比较一律用 strip 后值（复审 Minor 2）：API 层已 strip，这里对齐
        # 服务层直调口径——校验用 strip 值、落库也用 strip 值，两条通道同形。
        cleaned_title = title.strip() if title is not None else None
        cleaned_tags = [tag.strip() for tag in tags] if tags is not None else None
        cleaned_description = description.strip() if description is not None else None
        if cleaned_title is not None and cleaned_title != document.title:
            before["title"] = document.title
            after["title"] = cleaned_title
            document.title = cleaned_title
        if cleaned_tags is not None and cleaned_tags != [
            str(tag) for tag in (document.tags or [])
        ]:
            before["tags"] = [str(tag) for tag in (document.tags or [])]
            after["tags"] = cleaned_tags
            document.tags = cleaned_tags
        if description_provided and cleaned_description != document.description:
            before["description"] = document.description
            after["description"] = cleaned_description
            document.description = cleaned_description

        if after:
            await append_audit(
                session,
                action="school.kb.metadata.updated",
                resource_type="kb_document",
                resource_id=str(document.id),
                actor_realm="tenant",
                actor_id=user_id,
                tenant_id=tenant_id,
                result="success",
                metadata={"document_id": str(document.id), "before": before, "after": after},
                request_id=request_id,
            )
            await session.flush()
            # flush 会 expire onupdate 列：按 KB-01A 范式重查建视图（避免 greenlet 外刷新）。
            document = await document_repository.get(document_id)
            if document is None:
                raise KbError("DOCUMENT_NOT_FOUND", "文档不存在", 404)

        versions = await _versions_by_id(
            session, tenant_id, scope, [document.current_version_id]
        )
        return _document_view(document, _version_of(versions, document))


def _validate_metadata_fields(
    *,
    title: str | None,
    tags: list[str] | None,
    description: str | None,
) -> None:
    """元数据字段校验（与 `FileMetadata` 同界：标题 ≤200、标签 ≤10×32、描述 ≤1000）。"""

    if title is not None:
        cleaned = title.strip()
        if not cleaned or len(cleaned) > 200:
            raise KbError("METADATA_INVALID", "标题长度必须为 1–200 字", 422)
    if tags is not None:
        if len(tags) > 10:
            raise KbError("METADATA_INVALID", "标签最多 10 个", 422)
        for tag in tags:
            cleaned = tag.strip()
            if not cleaned or len(cleaned) > 32:
                raise KbError("METADATA_INVALID", "每个标签长度必须为 1–32 字", 422)
    if description is not None and len(description) > 1000:
        raise KbError("METADATA_INVALID", "描述长度不能超过 1000 字", 422)
