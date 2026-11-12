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
from sqlalchemy import func, select
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
    KbChunk,
    KbDocument,
    KbDocumentAudience,
    KbDocumentVersion,
    KbIndexVersion,
    KbPublishAlias,
    KbUploadBatch,
)
from app.db.models.kb_events import KbBuildEvent
from app.db.repository import CursorError, ScopeBypass, TenantScopedRepository
from app.db.session import db_session
from app.kb.audience import (
    ACL_GATE_ALLOW,
    AudienceValidationError,
    acl_gate,
    audience_tokens,
    parse_audience_items,
)
from app.kb.chunker import CHUNK_TYPE_CHILD
from app.kb.indexer import (
    alias_name,
    count_points,
    mark_published_payload,
    resolve_alias_collection,
    switch_alias,
)
from app.kb.status import (
    KB_STATUS_APPROVED,
    KB_STATUS_DELETED,
    KB_STATUS_FAILED,
    KB_STATUS_OFFLINE,
    KB_STATUS_PENDING_APPROVAL,
    KB_STATUS_PUBLISHED,
    KB_STATUS_REJECTED,
    KB_STATUS_SCAN_FAILED,
    KB_STATUS_UPLOADED,
    InvalidStatusTransition,
    NotBuildableError,
    NotPublishableError,
    NotPublishedError,
    NotRetriableError,
    NotSubmittableError,
    assert_build_allowed,
    assert_deletable,
    assert_offline_allowed,
    assert_publishable,
    assert_retriable,
    assert_rollback_target,
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
class DocumentDetailView:
    """文档详情视图（含当前活跃发布指针；TD-305）。

    `active_version_id` 取 `kb_publish_aliases` 库行（与 `active_publish` 同源）；
    无活跃发布（未发布/已下线/已软删）为 None（fail-closed，不得回退任选版本）。
    """

    document: DocumentView
    active_version_id: UUID | None


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


async def get_document_detail(
    *, tenant_id: UUID, scope: DataScopeContext, document_id: UUID
) -> DocumentDetailView:
    """按 id 取文档详情（范围外/跨租视为不存在；含活跃发布指针，TD-305）。

    alias 读与文档读同一事务（一次额外主键点查）；READ COMMITTED 下两者之间存在
    并发发布竞态窗口（单次响应可能「文档字段旧、活跃指针新」），消费侧由
    409 `KB_ROLLBACK_TARGET_IS_ACTIVE` 兜底承担——简报卡 3A 口径。

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
        alias_row = await _active_alias_row(
            session, tenant_id=tenant_id, document_id=document_id
        )
    return DocumentDetailView(
        document=_document_view(document, _version_of(versions, document)),
        active_version_id=None if alias_row is None else UUID(str(alias_row.version_id)),
    )


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


async def create_test_set(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    version_id: UUID,
    user_id: UUID,
    name: str,
    file_data: bytes,
    filename: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    """上传测试集（解析 + 行级校验 + 落库；整批原子）。"""

    from app.kb.test_parser import TestSetParseError, detect_test_set_format, parse_test_set

    fmt = detect_test_set_format(filename)
    try:
        parse_result = parse_test_set(file_data, fmt)
    except TestSetParseError as exc:
        raise KbError(exc.code, exc.message, 422) from exc
    if not parse_result.is_valid:
        raise KbError(
            "TEST_SET_INVALID",
            f"测试集校验失败（{len(parse_result.errors)} 条行级错误），整批未保存",
            422,
            extra={"errors": parse_result.errors[:20]},
        )
    if len(parse_result.cases) > 200:
        raise KbError("TOO_MANY_CASES", "测试集最多 200 条用例", 422)

    try:
        async with db_session(tenant_id=tenant_id) as session:
            version_repository: TenantScopedRepository[KbDocumentVersion] = TenantScopedRepository(
                session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
            )
            version = await version_repository.get(version_id)
            if version is None:
                raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
            store = build_object_store(get_settings())
            object_key = f"kb-test-sets/{tenant_id}/{version_id}/{uuid4()}/{filename}"
            await store.put(object_key, file_data, "application/octet-stream")
            from app.db.models.kb import KbTestCase, KbTestSet

            set_id = uuid4()
            test_set = KbTestSet(
                id=set_id,
                tenant_id=tenant_id,
                document_version_id=version_id,
                name=name,
                file_object_key=object_key,
                file_format=fmt,
                case_count=len(parse_result.cases),
                created_by=user_id,
            )
            set_repository: TenantScopedRepository[KbTestSet] = TenantScopedRepository(
                session, KbTestSet, tenant_id,
                scope=ScopeBypass(reason="测试集上传：按版本 id 操作，school.kb.test 判据在外层"),
            )
            await set_repository.add(test_set)
            await session.flush()
            case_repository: TenantScopedRepository[KbTestCase] = TenantScopedRepository(
                session, KbTestCase, tenant_id,
                scope=ScopeBypass(reason="测试集上传：写入用例行，系统执行者"),
            )
            for case in parse_result.cases:
                await case_repository.add(
                    KbTestCase(
                        tenant_id=tenant_id,
                        test_set_id=set_id,
                        row_no=case.row_no,
                        question=case.question,
                        expected_answer=case.expected_answer,
                        expected_points=case.expected_points,
                        expected_chunk_ids=case.expected_chunk_ids,
                        category=case.category,
                        is_adversarial=case.is_adversarial,
                    )
                )
            await append_audit(
                session,
                action="school.kb.test_set.created",
                resource_type="kb_document_version",
                resource_id=str(version_id),
                actor_realm="tenant",
                actor_id=user_id,
                tenant_id=tenant_id,
                result="success",
                metadata={"test_set_id": str(set_id), "name": name, "case_count": len(parse_result.cases), "format": fmt},
                request_id=request_id,
            )
            await session.flush()
            return {"test_set_id": set_id, "name": name, "case_count": len(parse_result.cases)}
    except KbError:
        raise
    except Exception as exc:
        logger.warning("测试集上传失败：%s.%s", type(exc).__module__, type(exc).__name__)
        raise KbError("TEST_SET_UPLOAD_FAILED", "测试集上传失败，请稍后重试", 503) from exc


async def start_test_run(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    version_id: UUID,
    test_set_id: UUID,
    user_id: UUID,
    request_id: str | None = None,
) -> dict[str, Any]:
    """发起测试运行（门禁：READY/TEST_FAILED/TEST_PASSED 可测试；活跃 run 唯一）。"""

    from app.kb.status import NotTestableError, assert_test_allowed

    try:
        async with db_session(tenant_id=tenant_id) as session:
            version_repository: TenantScopedRepository[KbDocumentVersion] = TenantScopedRepository(
                session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
            )
            version = await version_repository.get(version_id)
            if version is None:
                raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
            try:
                assert_test_allowed(version.status)
            except NotTestableError as exc:
                raise KbError("KB_NOT_TESTABLE", str(exc), 409) from exc
            from app.db.models.kb import KbTestRun, KbTestSet

            test_set = await TenantScopedRepository(
                session, KbTestSet, tenant_id,
                scope=ScopeBypass(reason="测试运行：读取测试集，school.kb.test 判据在外层"),
            ).get(test_set_id)
            if test_set is None or test_set.document_version_id != version_id:
                raise KbError("TEST_SET_NOT_FOUND", "测试集不存在或不属于该版本", 404)
            run_repository: TenantScopedRepository[KbTestRun] = TenantScopedRepository(
                session, KbTestRun, tenant_id,
                scope=ScopeBypass(reason="测试运行：创建运行，school.kb.test 判据在外层"),
            )
            active = (
                await session.execute(
                    run_repository.base_query().where(
                        KbTestRun.version_id == version_id,
                        KbTestRun.status == "TESTING",
                    )
                )
            ).scalar_one_or_none()
            if active is not None:
                raise KbError(
                    "TEST_ALREADY_RUNNING",
                    "该版本已有进行中的测试运行",
                    409,
                    extra={"run_id": str(active.id)},
                )
            run_id = uuid4()
            run = KbTestRun(
                id=run_id,
                tenant_id=tenant_id,
                test_set_id=test_set_id,
                version_id=version_id,
                status="TESTING",
                started_by=user_id,
                started_at=datetime.now(UTC),
            )
            await run_repository.add(run)
            await session.flush()
            await append_audit(
                session,
                action="school.kb.test.started",
                resource_type="kb_document_version",
                resource_id=str(version_id),
                actor_realm="tenant",
                actor_id=user_id,
                tenant_id=tenant_id,
                result="success",
                metadata={"run_id": str(run_id), "test_set_id": str(test_set_id)},
                request_id=request_id,
            )
            run_created = run.created_at
    except KbError as exc:
        if exc.http_status in (403, 409):
            await _action_denied(
                action="school.kb.test.denied",
                tenant_id=tenant_id,
                user_id=user_id,
                request_id=request_id,
                code=exc.code,
                context={"version_id": str(version_id), "test_set_id": str(test_set_id)},
            )
        raise

    from app.workers.settings import create_producer_pool

    pool = None
    try:
        pool = await create_producer_pool()
        await pool.enqueue_job("run_kb_test", str(tenant_id), str(version_id))
    except Exception as exc:
        logger.warning("测试任务入队失败：%s.%s", type(exc).__module__, type(exc).__name__)
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.aclose()

    return {"run_id": run_id, "version_id": version_id, "test_set_id": test_set_id, "status": "TESTING", "created_at": run_created}


async def get_test_run(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    run_id: UUID,
) -> dict[str, Any]:
    """查询测试运行结果（metrics_json 全字段）。"""

    async with db_session(tenant_id=tenant_id) as session:
        from app.db.models.kb import KbTestRun

        run = await TenantScopedRepository(
            session, KbTestRun, tenant_id,
            scope=ScopeBypass(reason="测试查询：按 run id 精确读取，外层 school.kb.view 判据"),
        ).get(run_id)
        if run is None:
            raise KbError("TEST_RUN_NOT_FOUND", "测试运行不存在", 404)
        version_repository: TenantScopedRepository[KbDocumentVersion] = TenantScopedRepository(
            session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
        )
        version = await version_repository.get(run.version_id)
        if version is None:
            raise KbError("TEST_RUN_NOT_FOUND", "测试运行不存在", 404)
        return {
            "run_id": run.id,
            "version_id": run.version_id,
            "test_set_id": run.test_set_id,
            "status": run.status,
            "metrics_json": run.metrics_json,
            "started_by": run.started_by,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
        }


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
            # K4 ACL 门禁（KB-09A D1-A）：与执行期同判据；拒绝走既有 build.denied 审计
            acl_gate_result = await _acl_gate_for_version(session, version)
            if acl_gate_result is not None:
                raise KbError(acl_gate_result, "人群 ACL 未配置或未确认（K4 fail-closed）", 409)

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


# ---------------------------------------------------------------------------
# KB-09A：文档查阅人群 ACL（K4；docs/design-briefs/M3-KB-09.md，已确认
# D1-A 门禁形态〔构建/发布同形 409 + 执行期二次校验〕、D2-B payload 单一事实源
# + set_payload 即时同步、422 AUDIENCE_ALL_MIXED / 502 AUDIENCE_SYNC_FAILED）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudienceItem:
    """一条人群条目（`all` 时 audience_id 为空）。"""

    audience_type: str
    audience_id: str | None


@dataclass(frozen=True)
class AudienceView:
    """版本人群配置视图（GET/PUT/confirm 统一回包形态）。"""

    version_id: UUID
    audiences: list[AudienceItem]
    acl_confirmed: bool
    acl_confirmed_by: UUID | None
    acl_confirmed_at: datetime | None


def _audience_view(version: KbDocumentVersion, rows_: Sequence[KbDocumentAudience]) -> AudienceView:
    """行集 + 确认态 → 视图（响应序 = (type, id) 字典序，确定性）。"""

    items = sorted(
        (AudienceItem(audience_type=row.audience_type, audience_id=row.audience_id) for row in rows_),
        key=lambda item: (item.audience_type, item.audience_id or ""),
    )
    return AudienceView(
        version_id=version.id,
        audiences=items,
        acl_confirmed=version.acl_confirmed_at is not None,
        acl_confirmed_by=version.acl_confirmed_by,
        acl_confirmed_at=version.acl_confirmed_at,
    )


async def _version_with_audiences(
    session: AsyncSession, tenant_id: UUID, version_id: UUID, scope: DataScopeContext
) -> tuple[KbDocumentVersion, Sequence[KbDocumentAudience]]:
    """取版本（范围过滤 + FOR UPDATE）+ 其人群行集（同事务；跨租/范围外 404 同文不泄露）。

    版本行加锁（`_version_locked` 先例）：PUT 与 confirm 对同一版本行串行化——
    否则 confirm 读行集与提交之间插入并发 PUT 会产生「DB=新行集+已确认、
    Qdrant=旧 tokens」的收紧方向 fail-open 窗口（审查 Major 3）。
    """

    version_repository: TenantScopedRepository[KbDocumentVersion] = TenantScopedRepository(
        session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
    )
    version = await _version_locked(session, version_repository, version_id=version_id)
    if version is None:
        raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
    audience_repository: TenantScopedRepository[KbDocumentAudience] = TenantScopedRepository(
        session, KbDocumentAudience, tenant_id, scope=ScopeBypass(reason="人群行随版本范围走")
    )
    rows_ = (
        await session.execute(
            audience_repository.base_query().where(KbDocumentAudience.version_id == version_id)
        )
    ).scalars().all()
    return version, rows_


async def _ensure_audience_target_exists(
    session: AsyncSession, tenant_id: UUID, audience_type: str, audience_id: str
) -> None:
    """同租存在性校验（spec 验收 :319：跨租/不存在 audience_id 拒绝；422 同文）。

    全部走仓储（bare-SQL 守卫：租户谓词在仓储注入，RLS 为第二道防线）。
    存在性校验不做状态过滤（禁用部门/归档班级仍可配置——「收紧」语义由检索侧
    人群解析承担，解析器只认活跃关系；删除的对象行不存在即 422）。
    """

    from app.db.models.identity import User
    from app.db.models.org import Course, Department, OrgClass
    from app.db.models.rbac import Role

    # 非 grade 类型按主键 get；UUID 非法视同不存在（422 同文，防 500 泄漏格式差异）
    target: UUID
    try:
        target = UUID(audience_id)
    except ValueError as exc:
        raise KbError("AUDIENCE_NOT_FOUND", "人群对象不存在或不属于本租户", 422) from exc

    model_by_type: dict[str, type] = {
        "department": Department,
        "role": Role,
        "class": OrgClass,
        "course": Course,
        "user": User,
    }
    if audience_type == "grade":
        # grade：对租户班级现值校验（A30 唯一数据源口径）
        repository: TenantScopedRepository[OrgClass] = TenantScopedRepository(
            session, OrgClass, tenant_id, scope=ScopeBypass(reason="人群对象存在性校验：同租即判据")
        )
        found = (
            await session.execute(
                repository.base_query().where(OrgClass.grade == audience_id).limit(1)
            )
        ).first()
    else:
        model = model_by_type[audience_type]
        repository = TenantScopedRepository(
            session, model, tenant_id, scope=ScopeBypass(reason="人群对象存在性校验：同租即判据")
        )
        found = await repository.get(target)
    if found is None:
        raise KbError("AUDIENCE_NOT_FOUND", "人群对象不存在或不属于本租户", 422)


async def get_audiences(
    *, tenant_id: UUID, scope: DataScopeContext, version_id: UUID
) -> AudienceView:
    """读取版本人群配置（`school.kb.view`；跨租/范围外 404 同文不泄露）。"""

    async with db_session(tenant_id=tenant_id) as session:
        version, rows_ = await _version_with_audiences(session, tenant_id, version_id, scope)
    return _audience_view(version, rows_)


async def replace_audiences(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    version_id: UUID,
    user_id: UUID,
    items: list[dict[str, Any]],
    request_id: str | None = None,
) -> AudienceView:
    """全量设置版本人群并置未确认（`school.kb.acl.manage`；spec :309「保存后置为未确认」）。

    结构校验（纯函数 422 族）→ 同租存在性校验（422 `AUDIENCE_NOT_FOUND`）→
    重写行集 + 清确认列 → 审计 `school.kb.acl.updated`（前后集合）→
    已构建版本 set_payload 清空 `acl_tokens`（恒不召回，fail-closed）；
    同步失败 → 502 `AUDIENCE_SYNC_FAILED`（DB 已提交，重试 PUT+confirm 恢复）。
    """

    try:
        parsed = parse_audience_items(items)
    except AudienceValidationError as exc:
        raise KbError(exc.code, str(exc), 422) from exc

    async with db_session(tenant_id=tenant_id) as session:
        version, old_rows = await _version_with_audiences(session, tenant_id, version_id, scope)
        for audience_type, audience_id in parsed:
            if audience_id is not None:
                await _ensure_audience_target_exists(
                    session, tenant_id, audience_type, audience_id
                )
        audience_repository: TenantScopedRepository[KbDocumentAudience] = TenantScopedRepository(
            session, KbDocumentAudience, tenant_id, scope=ScopeBypass(reason="人群行随版本范围走")
        )
        for row in old_rows:
            await audience_repository.delete(row)
        new_rows: list[KbDocumentAudience] = []
        for audience_type, audience_id in parsed:
            new_rows.append(
                KbDocumentAudience(
                    tenant_id=tenant_id,
                    version_id=version.id,
                    audience_type=audience_type,
                    audience_id=audience_id,
                    created_by=user_id,
                )
            )
            await audience_repository.add(new_rows[-1])
        before = sorted(
            ([row.audience_type, row.audience_id] for row in old_rows),
            key=lambda pair: (pair[0], pair[1] or ""),
        )
        after = sorted(
            ([t, i] for t, i in parsed),
            key=lambda pair: (pair[0], pair[1] or ""),
        )
        version.acl_confirmed_by = None
        version.acl_confirmed_at = None
        await append_audit(
            session,
            action="school.kb.acl.updated",
            resource_type="kb_document_version",
            resource_id=str(version.id),
            actor_realm="tenant",
            actor_id=user_id,
            tenant_id=tenant_id,
            result="success",
            metadata={"before": before, "after": after},
            request_id=request_id,
        )
        await session.flush()
    await _sync_acl_if_built(tenant_id, version_id, tokens=[])
    return _audience_view(version, new_rows)


async def confirm_audiences(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    version_id: UUID,
    user_id: UUID,
    request_id: str | None = None,
) -> AudienceView:
    """确认版本人群（`school.kb.acl.manage`；空集 409 `ACL_REQUIRED`、已确认 409）。"""

    async with db_session(tenant_id=tenant_id) as session:
        version, rows_ = await _version_with_audiences(session, tenant_id, version_id, scope)
        if not rows_:
            raise KbError("ACL_REQUIRED", "人群 ACL 未配置（先设置查阅人群）", 409)
        if version.acl_confirmed_at is not None:
            raise KbError("ACL_ALREADY_CONFIRMED", "人群 ACL 已确认", 409)
        version.acl_confirmed_by = user_id
        version.acl_confirmed_at = datetime.now(UTC)
        await append_audit(
            session,
            action="school.kb.acl.confirmed",
            resource_type="kb_document_version",
            resource_id=str(version.id),
            actor_realm="tenant",
            actor_id=user_id,
            tenant_id=tenant_id,
            result="success",
            metadata={"audience_count": len(rows_)},
            request_id=request_id,
        )
        tokens = audience_tokens(
            [(row.audience_type, row.audience_id) for row in rows_]
        )
        await session.flush()
    await _sync_acl_if_built(tenant_id, version_id, tokens=tokens)
    return _audience_view(version, rows_)


async def _sync_acl_if_built(tenant_id: UUID, version_id: UUID, *, tokens: list[str]) -> None:
    """已构建版本同步 payload `acl_tokens`（D2-B；未构建跳过；失败 502 人读码）。

    不按 status 过滤：构建落 `ready`、发布推进为 `published`（KB-05）——同步语义
    只看「该版本已构建」（有索引行即有 collection）；`retired` 属被替换旧版本行，
    对同一 version_id 不出现（唯一 `(tenant_id, document_version_id)`）。
    """

    async with db_session(tenant_id=tenant_id) as session:
        index_row = await _index_version_of(session, tenant_id=tenant_id, version_id=version_id)
        collection = index_row.qdrant_collection if index_row is not None else None
    if collection is None:
        return
    settings = get_settings()
    if not settings.qdrant_url:
        logger.warning("KB-09 ACL 同步跳过：QDRANT_URL 未配置")
        return
    from qdrant_client import AsyncQdrantClient

    from app.kb.indexer import sync_acl_payload

    client = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        await sync_acl_payload(client, collection=collection, version_id=version_id, tokens=tokens)
    except Exception as exc:
        logger.warning(
            "KB-09 ACL 同步失败：%s.%s", type(exc).__module__, type(exc).__name__
        )
        raise KbError(
            "AUDIENCE_SYNC_FAILED",
            "人群 token 同步失败（配置已保存，请重试设置并确认以恢复一致性）",
            502,
        ) from exc
    finally:
        await client.close()


async def _acl_gate_for_version(session: AsyncSession, version: KbDocumentVersion) -> str | None:
    """构建/发布门禁判据（与执行期同函数口径）：None=放行，否则 409 错误码。"""

    audience_repository: TenantScopedRepository[KbDocumentAudience] = TenantScopedRepository(
        session, KbDocumentAudience, tenant_id=version.tenant_id,
        scope=ScopeBypass(reason="门禁计数按版本定域；version 行已范围校验/加锁"),
    )
    scoped_rows = audience_repository.base_query().where(
        KbDocumentAudience.version_id == version.id
    )
    audience_count = int(
        (
            await session.execute(
                select(func.count()).select_from(scoped_rows.subquery())
            )
        ).scalar_one()
    )
    gate = acl_gate(audience_count, version.acl_confirmed_at)
    return None if gate == ACL_GATE_ALLOW else gate


# ---------------------------------------------------------------------------
# KB-05：发布 / 回滚 / 下线 / 删除（docs/design-briefs/M3-KB-05.md，已确认
# D1-A 每文档 alias、D2-A 任意 PUBLISHED 目标回滚、D4-A ACL 门禁归 KB-09、
# D5+D6-A 缓存失效归 M4 / retired 清理归 M6 / 纯后端先行）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishView:
    """发布/回滚结果（alias 行是事实来源；alias_synced=False 时可幂等重放重建）。"""

    document_id: UUID
    version_id: UUID
    version_no: int
    status: str
    alias: str
    collection: str
    previous_version_id: UUID | None
    published_at: datetime
    replayed: bool = False
    alias_synced: bool = True


@dataclass(frozen=True)
class VersionStateView:
    """下线/删除结果（was_active=该版本是否是操作时的活跃发布）。"""

    document_id: UUID
    version_id: UUID
    status: str
    was_active: bool


def _publish_alias_repository(session: AsyncSession, tenant_id: UUID) -> TenantScopedRepository[KbPublishAlias]:
    return TenantScopedRepository(
        session,
        KbPublishAlias,
        tenant_id,
        scope=ScopeBypass(reason="发布指针：派生状态（无归属列），按文档 id 精确操作"),
    )


def _index_version_repository(
    session: AsyncSession, tenant_id: UUID
) -> TenantScopedRepository[KbIndexVersion]:
    return TenantScopedRepository(
        session,
        KbIndexVersion,
        tenant_id,
        scope=ScopeBypass(reason="版本索引记录：构建/发布系统状态，按版本 id 精确操作"),
    )


async def _publish_alias_row(
    session: AsyncSession, *, tenant_id: UUID, document_id: UUID
) -> KbPublishAlias | None:
    """取文档的发布指针行（FOR UPDATE，同文档发布/回滚串行化）。"""

    result = await session.execute(
        _publish_alias_repository(session, tenant_id)
        .base_query()
        .where(KbPublishAlias.document_id == document_id)
        .with_for_update()
    )
    return cast("KbPublishAlias | None", result.scalar_one_or_none())


async def _active_alias_row(
    session: AsyncSession, *, tenant_id: UUID, document_id: UUID
) -> KbPublishAlias | None:
    """文档的活跃发布指针行（无锁读；kb_publish_aliases 库行是事实来源）。"""

    result = await session.execute(
        _publish_alias_repository(session, tenant_id)
        .base_query()
        .where(KbPublishAlias.document_id == document_id)
    )
    return cast("KbPublishAlias | None", result.scalar_one_or_none())


async def active_publish(
    *, tenant_id: UUID, document_id: UUID
) -> tuple[UUID, str] | None:
    """解析文档当前活跃发布（M4 检索的数据基础；库行是事实来源）。

    返回 `(version_id, collection)`；无活跃发布（未发布/已下线/已删除）返回 None
    （fail-closed：调用方不得回退到别的 collection）。
    """

    async with db_session(tenant_id=tenant_id) as session:
        record = await _active_alias_row(
            session, tenant_id=tenant_id, document_id=document_id
        )
        if record is None:
            return None
        return UUID(str(record.version_id)), str(record.collection)


async def _version_locked(
    session: AsyncSession,
    repository: TenantScopedRepository[KbDocumentVersion],
    *,
    version_id: UUID,
) -> KbDocumentVersion | None:
    """范围过滤 + FOR UPDATE 取版本行（第二个事务里的重校验锁读）。"""

    result = await session.execute(
        repository.base_query()
        .where(KbDocumentVersion.id == version_id)
        .with_for_update()
    )
    return cast("KbDocumentVersion | None", result.scalar_one_or_none())


async def _index_version_of(
    session: AsyncSession, *, tenant_id: UUID, version_id: UUID
) -> KbIndexVersion | None:
    """版本的索引记录行（构建成功时由 worker 落 `ready`）。"""

    result = await session.execute(
        _index_version_repository(session, tenant_id)
        .base_query()
        .where(KbIndexVersion.document_version_id == version_id)
    )
    return cast("KbIndexVersion | None", result.scalar_one_or_none())


async def _child_chunk_count(session: AsyncSession, *, tenant_id: UUID, version_id: UUID) -> int:
    """子块数（发布前与 Qdrant 点数对账；KB-03A 起只索引子块）。"""

    chunk_repository = TenantScopedRepository(
        session,
        KbChunk,
        tenant_id,
        scope=ScopeBypass(reason="分块：发布对账只读计数，按版本 id 过滤"),
    )
    subquery = (
        chunk_repository.base_query()
        .where(
            KbChunk.document_version_id == version_id,
            KbChunk.chunk_type == CHUNK_TYPE_CHILD,
        )
        .subquery()
    )
    result = await session.execute(select(func.count()).select_from(subquery))
    return int(result.scalar_one())


async def _verify_index_ready(
    *, tenant_id: UUID, collection: str, expected_points: int
) -> None:
    """发布前复核 collection 存在且点数与子块数一致（KB-03A VALIDATING 之后的第二道对账）。"""

    from qdrant_client import AsyncQdrantClient

    settings = get_settings()
    if not settings.qdrant_url:
        raise KbError("KB_INDEX_UNAVAILABLE", "向量索引未配置（QDRANT_URL 缺失）", 503)
    client = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        try:
            exists = await client.collection_exists(collection)
            indexed = await count_points(client, collection=collection) if exists else 0
        except Exception as exc:
            raise KbError("KB_INDEX_UNAVAILABLE", "向量索引不可用（Qdrant 访问失败）", 503) from exc
    finally:
        await client.close()
    if not exists:
        raise KbError("KB_INDEX_NOT_READY", "该版本的向量索引不存在（请先构建）", 409)
    if indexed != expected_points:
        raise KbError(
            "KB_INDEX_INCOMPLETE",
            f"向量索引不完整（索引 {indexed} / 子块 {expected_points}），请重新构建",
            409,
        )


async def _sync_publish_qdrant(
    *,
    tenant_id: UUID,
    document_id: UUID,
    version_id: UUID,
    target_collection: str,
) -> bool:
    """提交后切换 Qdrant named alias 并翻转 payload `is_published`（best-effort）。

    库行（kb_publish_aliases）是事实来源；本函数失败不影响已提交的发布状态，
    返回 False 由调用方标注 `alias_synced=false`，幂等重放（再次发布同一活跃版本）
    可重建 alias。切换前一律实时解析 alias 现状（一次 get_aliases；管理端点低频）——
    库行的 collection 可能从未在 Qdrant 侧落地（上次同步失败），按库行构造的
    [Delete, Create] 会对不存在的 alias 报错，实时解析覆盖该路径。
    """

    from qdrant_client import AsyncQdrantClient

    settings = get_settings()
    if not settings.qdrant_url:
        logger.warning("KB-05 发布同步跳过：QDRANT_URL 未配置")
        return False
    client = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        current = await resolve_alias_collection(client, alias_name(tenant_id, document_id))
        await switch_alias(
            client,
            alias=alias_name(tenant_id, document_id),
            current_collection=current,
            target_collection=target_collection,
        )
        await mark_published_payload(
            client, collection=target_collection, version_id=version_id
        )
        return True
    except Exception as exc:
        logger.warning(
            "KB-05 发布后 Qdrant 同步失败（可重放发布重建）：%s.%s",
            type(exc).__module__,
            type(exc).__name__,
        )
        return False
    finally:
        await client.close()


async def publish_version(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    version_id: UUID,
    user_id: UUID,
    request_id: str | None = None,
) -> PublishView:
    """发布版本（403/409 拒绝在独立事务写 `school.kb.publish.denied`；404 探测不审计）。"""

    try:
        return await _publish_version(
            tenant_id=tenant_id,
            scope=scope,
            version_id=version_id,
            user_id=user_id,
            request_id=request_id,
        )
    except KbError as exc:
        if exc.http_status in (403, 409):
            await _action_denied(
                action="school.kb.publish.denied",
                tenant_id=tenant_id,
                user_id=user_id,
                request_id=request_id,
                code=exc.code,
                context={"version_id": str(version_id)},
            )
        raise


async def _publish_version(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    version_id: UUID,
    user_id: UUID,
    request_id: str | None = None,
) -> PublishView:
    """发布版本（门禁：TEST_PASSED；alias 行 + Qdrant named alias 原子切换）。

    幂等重放：版本已 `PUBLISHED` 且仍是活跃发布时再次发布 = 重建 alias/payload
    （恢复路径）；已 `PUBLISHED` 但**不是**活跃发布的版本走 rollback 端点切换。
    """

    # ---- 事务 1：读版本/索引行/子块数（Qdrant 复核在事务外） ----
    async with db_session(tenant_id=tenant_id) as session:
        version_repository: TenantScopedRepository[KbDocumentVersion] = TenantScopedRepository(
            session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
        )
        version = await version_repository.get(version_id)
        if version is None:
            raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
        replay = version.status == KB_STATUS_PUBLISHED
        if not replay:
            try:
                assert_publishable(version.status)
            except NotPublishableError as exc:
                raise KbError("KB_NOT_TEST_PASSED", str(exc), 409) from exc
        # K4 ACL 门禁（KB-09A D1-A）：与构建侧同形（TD-302）；**含 replay 路径**
        # （重放不是新发布，但确认态是「可发布」不变量——未确认重放同样 409；
        # 拒绝走既有 publish.denied 审计）
        acl_gate_result = await _acl_gate_for_version(session, version)
        if acl_gate_result is not None:
            raise KbError(acl_gate_result, "人群 ACL 未配置或未确认（K4 fail-closed）", 409)
        index_row = await _index_version_of(session, tenant_id=tenant_id, version_id=version.id)
        if index_row is None:
            raise KbError("KB_INDEX_NOT_READY", "该版本的向量索引记录不存在（请先构建）", 409)
        collection = index_row.qdrant_collection
        expected = await _child_chunk_count(session, tenant_id=tenant_id, version_id=version.id)
        document_id = version.document_id
        version_no = version.version_no

    await _verify_index_ready(tenant_id=tenant_id, collection=collection, expected_points=expected)

    # ---- 事务 2：锁版本 + 指针行 upsert + 状态迁移 + 审计 ----
    async with db_session(tenant_id=tenant_id) as session:
        version_repository = TenantScopedRepository(
            session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
        )
        version = await _version_locked(session, version_repository, version_id=version_id)
        if version is None:
            raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
        replay = version.status == KB_STATUS_PUBLISHED
        if not replay:
            try:
                assert_publishable(version.status)
            except NotPublishableError as exc:
                raise KbError("KB_NOT_TEST_PASSED", str(exc), 409) from exc
        # K4 ACL 门禁（KB-09A D1-A）：与构建侧同形（TD-302）；**含 replay 路径**
        # （锁版本后权威判定；拒绝走既有 publish.denied 审计）
        acl_gate_result = await _acl_gate_for_version(session, version)
        if acl_gate_result is not None:
            raise KbError(acl_gate_result, "人群 ACL 未配置或未确认（K4 fail-closed）", 409)

        row = await _publish_alias_row(
            session, tenant_id=tenant_id, document_id=version.document_id
        )
        if replay:
            if row is None:
                raise KbError(
                    "KB_PUBLISHED_NOT_ACTIVE",
                    "版本已发布但活跃发布指针已撤除（活跃版本被下线或删除），不可重放发布",
                    409,
                )
            if row.version_id != version.id:
                raise KbError(
                    "KB_PUBLISHED_NOT_ACTIVE",
                    "版本已发布但不是活跃发布，请使用回滚端点切换",
                    409,
                )
            previous_version_id: UUID | None = None
        else:
            if row is None:
                row = KbPublishAlias(
                    tenant_id=tenant_id,
                    document_id=version.document_id,
                    alias=alias_name(tenant_id, version.document_id),
                    collection=collection,
                    version_id=version.id,
                )
                try:
                    async with session.begin_nested():
                        session.add(row)
                        await session.flush()
                except IntegrityError as exc:
                    # 并发首发布：唯一 (tenant_id, document_id) 兜底，落回更新路径。
                    if _integrity_constraint_name(exc) != "uq_kb_publish_aliases_tenant_document":
                        raise
                    row = await _publish_alias_row(
                        session, tenant_id=tenant_id, document_id=version.document_id
                    )
                    if row is None:  # pragma: no cover - SAVEPOINT 回滚后必然可读
                        raise KbError(
                            "KB_PUBLISH_CONFLICT", "发布指针创建冲突，请重试", 409
                        ) from exc
            if row.version_id != version.id:
                previous = await _index_version_of(
                    session, tenant_id=tenant_id, version_id=row.version_id
                )
                if previous is not None:
                    previous.status = "retired"
                    previous.retired_at = datetime.now(UTC)
            previous_version_id = row.version_id if row.version_id != version.id else None
            row.alias = alias_name(tenant_id, version.document_id)
            row.collection = collection
            row.version_id = version.id

        index_row = await _index_version_of(session, tenant_id=tenant_id, version_id=version.id)
        if index_row is None:  # pragma: no cover - 事务 1 已核验，防御并发删除
            raise KbError("KB_INDEX_NOT_READY", "该版本的向量索引记录不存在（请先构建）", 409)
        index_row.published_at = index_row.published_at or datetime.now(UTC)
        index_row.status = "published"

        if not replay:
            assert_transition(version.status, KB_STATUS_PUBLISHED)
            version.status = KB_STATUS_PUBLISHED
        document_repository: TenantScopedRepository[KbDocument] = TenantScopedRepository(
            session, KbDocument, tenant_id, scope=scope, owner_attr="created_by"
        )
        document = (
            await session.execute(
                document_repository.base_query()
                .where(KbDocument.id == version.document_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if document is None:
            raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
        document.status = KB_STATUS_PUBLISHED
        document.current_version_id = version.id

        await append_audit(
            session,
            action="school.kb.published",
            resource_type="kb_document_version",
            resource_id=str(version.id),
            actor_realm="tenant",
            actor_id=user_id,
            tenant_id=tenant_id,
            result="success",
            metadata={
                "document_id": str(version.document_id),
                "version_no": version.version_no,
                "collection": collection,
                "alias": alias_name(tenant_id, version.document_id),
                "previous_version_id": str(previous_version_id) if previous_version_id else None,
                "replayed": replay,
            },
            request_id=request_id,
        )
        published_at = index_row.published_at
        previous_for_view = previous_version_id
        replayed = replay

    alias_synced = await _sync_publish_qdrant(
        tenant_id=tenant_id,
        document_id=document_id,
        version_id=version_id,
        target_collection=collection,
    )
    return PublishView(
        document_id=document_id,
        version_id=version_id,
        version_no=version_no,
        status=KB_STATUS_PUBLISHED,
        alias=alias_name(tenant_id, document_id),
        collection=collection,
        previous_version_id=previous_for_view,
        published_at=published_at,
        replayed=replayed,
        alias_synced=alias_synced,
    )


async def rollback_version(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    target_version_id: UUID,
    user_id: UUID,
    request_id: str | None = None,
) -> PublishView:
    """回滚：把活跃发布切到任意 `PUBLISHED` 目标版本（D2-A；只切 alias 不改分块）。

    当前活跃版本保持 `PUBLISHED`（蓝绿共存）；目标集合在 Qdrant 侧复核存在且
    点数与子块数一致后才切换。403/409 拒绝审计 `school.kb.rollback.denied`。
    """

    try:
        # ---- 事务 1：读目标版本/索引行/子块数 ----
        async with db_session(tenant_id=tenant_id) as session:
            target_repository: TenantScopedRepository[KbDocumentVersion] = TenantScopedRepository(
                session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
            )
            target = await target_repository.get(target_version_id)
            if target is None:
                raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
            try:
                assert_rollback_target(target.status)
            except NotPublishedError as exc:
                raise KbError("KB_NOT_PUBLISHED", str(exc), 409) from exc
            index_row = await _index_version_of(
                session, tenant_id=tenant_id, version_id=target.id
            )
            if index_row is None:
                raise KbError("KB_INDEX_NOT_READY", "目标版本的向量索引记录不存在", 409)
            collection = index_row.qdrant_collection
            expected = await _child_chunk_count(
                session, tenant_id=tenant_id, version_id=target.id
            )
            document_id = target.document_id
            version_no = target.version_no

        await _verify_index_ready(
            tenant_id=tenant_id, collection=collection, expected_points=expected
        )

        # ---- 事务 2：锁目标版本 + 锁指针行（与 publish 同序：版本 → 指针）+ 切换 + 审计 ----
        async with db_session(tenant_id=tenant_id) as session:
            target_repository = TenantScopedRepository(
                session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
            )
            target = await _version_locked(
                session, target_repository, version_id=target_version_id
            )
            if target is None:
                raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
            try:
                assert_rollback_target(target.status)
            except NotPublishedError as exc:
                raise KbError("KB_NOT_PUBLISHED", str(exc), 409) from exc
            row = await _publish_alias_row(
                session, tenant_id=tenant_id, document_id=document_id
            )
            if row is None:
                raise KbError("KB_NO_ACTIVE_PUBLISH", "该文档当前没有活跃发布，无法回滚", 409)
            if row.version_id == target_version_id:
                raise KbError(
                    "KB_ROLLBACK_TARGET_IS_ACTIVE", "目标版本已是当前活跃发布", 409
                )
            from_version_id = row.version_id
            current_index = await _index_version_of(
                session, tenant_id=tenant_id, version_id=from_version_id
            )
            if current_index is not None:
                current_index.status = "retired"
                current_index.retired_at = datetime.now(UTC)
            target_index = await _index_version_of(
                session, tenant_id=tenant_id, version_id=target.id
            )
            if target_index is None:  # pragma: no cover - 事务 1 已核验
                raise KbError("KB_INDEX_NOT_READY", "目标版本的向量索引记录不存在", 409)
            target_index.published_at = target_index.published_at or datetime.now(UTC)
            target_index.status = "published"

            row.alias = alias_name(tenant_id, target.document_id)
            row.collection = collection
            row.version_id = target.id

            document_repository: TenantScopedRepository[KbDocument] = TenantScopedRepository(
                session, KbDocument, tenant_id, scope=scope, owner_attr="created_by"
            )
            document = (
                await session.execute(
                    document_repository.base_query()
                    .where(KbDocument.id == target.document_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if document is None:
                raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
            document.status = KB_STATUS_PUBLISHED
            document.current_version_id = target.id

            await append_audit(
                session,
                action="school.kb.rolled_back",
                resource_type="kb_document_version",
                resource_id=str(target.id),
                actor_realm="tenant",
                actor_id=user_id,
                tenant_id=tenant_id,
                result="success",
                metadata={
                    "document_id": str(target.document_id),
                    "from_version_id": str(from_version_id),
                    "to_version_id": str(target.id),
                    "collection": collection,
                    "alias": alias_name(tenant_id, target.document_id),
                },
                request_id=request_id,
            )
            published_at = target_index.published_at

        alias_synced = await _sync_publish_qdrant(
            tenant_id=tenant_id,
            document_id=document_id,
            version_id=target_version_id,
            target_collection=collection,
        )
        return PublishView(
            document_id=document_id,
            version_id=target_version_id,
            version_no=version_no,
            status=KB_STATUS_PUBLISHED,
            alias=alias_name(tenant_id, document_id),
            collection=collection,
            previous_version_id=from_version_id,
            published_at=published_at,
            replayed=False,
            alias_synced=alias_synced,
        )
    except KbError as exc:
        if exc.http_status in (403, 409):
            await _action_denied(
                action="school.kb.rollback.denied",
                tenant_id=tenant_id,
                user_id=user_id,
                request_id=request_id,
                code=exc.code,
                context={"version_id": str(target_version_id)},
            )
        raise


async def _retire_or_take_active(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    version: KbDocumentVersion,
) -> bool:
    """下线/删除共用：索引行退役；若版本是活跃发布则撤指针并返回 True。"""

    index_row = await _index_version_of(session, tenant_id=tenant_id, version_id=version.id)
    if index_row is not None:
        index_row.status = "retired"
        index_row.retired_at = datetime.now(UTC)
    row = await _publish_alias_row(session, tenant_id=tenant_id, document_id=version.document_id)
    if row is not None and row.version_id == version.id:
        await session.delete(row)
        return True
    return False


async def offline_version(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    version_id: UUID,
    user_id: UUID,
    request_id: str | None = None,
) -> VersionStateView:
    """下线版本（PUBLISHED → OFFLINE 终态；活跃发布撤指针，collection 保留至 M6 清理）。"""

    try:
        async with db_session(tenant_id=tenant_id) as session:
            version_repository: TenantScopedRepository[KbDocumentVersion] = TenantScopedRepository(
                session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
            )
            version = await _version_locked(session, version_repository, version_id=version_id)
            if version is None:
                raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
            try:
                assert_offline_allowed(version.status)
            except NotPublishedError as exc:
                raise KbError("KB_NOT_PUBLISHED", str(exc), 409) from exc
            document_id = version.document_id
            was_active = await _retire_or_take_active(
                session, tenant_id=tenant_id, version=version
            )
            assert_transition(version.status, KB_STATUS_OFFLINE)
            version.status = KB_STATUS_OFFLINE
            if was_active:
                document_repository: TenantScopedRepository[KbDocument] = TenantScopedRepository(
                    session, KbDocument, tenant_id, scope=scope, owner_attr="created_by"
                )
                document = (
                    await session.execute(
                        document_repository.base_query()
                        .where(KbDocument.id == document_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if document is None:
                    # 与 publish/rollback 对齐：范围外文档行一律 404，不留状态镜像漂移。
                    raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
                document.status = KB_STATUS_OFFLINE
            await append_audit(
                session,
                action="school.kb.offlined",
                resource_type="kb_document_version",
                resource_id=str(version.id),
                actor_realm="tenant",
                actor_id=user_id,
                tenant_id=tenant_id,
                result="success",
                metadata={
                    "document_id": str(document_id),
                    "was_active": was_active,
                },
                request_id=request_id,
            )
            return VersionStateView(
                document_id=document_id,
                version_id=version_id,
                status=KB_STATUS_OFFLINE,
                was_active=was_active,
            )
    except KbError as exc:
        if exc.http_status in (403, 409):
            await _action_denied(
                action="school.kb.offline.denied",
                tenant_id=tenant_id,
                user_id=user_id,
                request_id=request_id,
                code=exc.code,
                context={"version_id": str(version_id)},
            )
        raise


async def delete_version(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    version_id: UUID,
    user_id: UUID,
    request_id: str | None = None,
) -> VersionStateView:
    """软删版本（PUBLISHED → DELETED 终态；状态机唯一删除出边；collection 保留至 M6）。"""

    try:
        async with db_session(tenant_id=tenant_id) as session:
            version_repository: TenantScopedRepository[KbDocumentVersion] = TenantScopedRepository(
                session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
            )
            version = await _version_locked(session, version_repository, version_id=version_id)
            if version is None:
                raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
            try:
                assert_deletable(version.status)
            except NotPublishedError as exc:
                raise KbError("KB_NOT_PUBLISHED", str(exc), 409) from exc
            document_id = version.document_id
            was_active = await _retire_or_take_active(
                session, tenant_id=tenant_id, version=version
            )
            assert_transition(version.status, KB_STATUS_DELETED)
            version.status = KB_STATUS_DELETED
            if was_active:
                document_repository: TenantScopedRepository[KbDocument] = TenantScopedRepository(
                    session, KbDocument, tenant_id, scope=scope, owner_attr="created_by"
                )
                document = (
                    await session.execute(
                        document_repository.base_query()
                        .where(KbDocument.id == document_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if document is None:
                    # 与 publish/rollback 对齐：范围外文档行一律 404，不留状态镜像漂移。
                    raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
                document.status = KB_STATUS_DELETED
            await append_audit(
                session,
                action="school.kb.deleted",
                resource_type="kb_document_version",
                resource_id=str(version.id),
                actor_realm="tenant",
                actor_id=user_id,
                tenant_id=tenant_id,
                result="success",
                metadata={
                    "document_id": str(document_id),
                    "was_active": was_active,
                },
                request_id=request_id,
            )
            return VersionStateView(
                document_id=document_id,
                version_id=version_id,
                status=KB_STATUS_DELETED,
                was_active=was_active,
            )
    except KbError as exc:
        if exc.http_status in (403, 409):
            await _action_denied(
                action="school.kb.delete.denied",
                tenant_id=tenant_id,
                user_id=user_id,
                request_id=request_id,
                code=exc.code,
                context={"version_id": str(version_id)},
            )
        raise
