"""KB-10A 块编辑纯函数层（M3 K5；简报 `docs/design-briefs/M3-KB-10.md` 卡 D4-A/D5-A）。

只放**纯函数**（不碰库/网络）；乐观锁判读、端点编排与派生构建分支在
`service.py`（chunk edits 端点）与 `build_task.py`（派生分支）。

口径（spec :322-329）：
- 编辑仅子块文本；`base_hash` 乐观锁 → 409 `CHUNK_CONFLICT`；
- 内容校验（卡 D5-A 轻量结构检查）：非空 strip、长度上限（码点，与 chunker
  子块口径同源）、代码围栏 ``` 配对、表格行组 `|` 起止完整；422
  `CHUNK_CONTENT_INVALID` 人读文案；
- 派生判据：0 个提交 → `DERIVE_NO_EDITS`；已确认过 → `CHUNK_EDIT_STATE`；
  派生版本号 = max(version_no)+1（D2-A）。
"""

import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.kb import (
    KbBuildJob,
    KbChunk,
    KbChunkEdit,
    KbDocumentAudience,
    KbDocumentVersion,
)
from app.db.repository import ScopeBypass, TenantScopedRepository
from app.db.session import db_session
from app.kb.service import KbError, _integrity_constraint_name
from app.rbac.scopes import DataScopeContext

CHUNK_CONTENT_MAX_CHARS = 500  # 与 chunker 子块默认目标长度同源（TD-294 实验口径）

CHUNK_CONFLICT = "CHUNK_CONFLICT"
CHUNK_CONTENT_INVALID = "CHUNK_CONTENT_INVALID"
DERIVE_NO_EDITS = "DERIVE_NO_EDITS"
CHUNK_EDIT_STATE = "CHUNK_EDIT_STATE"
CHUNK_EDIT_TARGET_INVALID = "CHUNK_EDIT_TARGET_INVALID"


class ChunkEditValidationError(ValueError):
    """编辑内容非法（code 人读错误码；服务层映射 KbError 422）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_edit_content(new_content: str, *, max_chars: int = CHUNK_CONTENT_MAX_CHARS) -> str:
    """校验并规范化编辑内容（trim）；非法抛 `ChunkEditValidationError`。

    卡 D5-A 轻量结构检查（不做 markdown 全解析）：
    - 非空（strip 后）；
    - 长度 ≤ max_chars（Python len 即码点）；
    - 代码围栏：``` 出现次数为偶数（开合配对；```python 带语言标记同算）；
    - 表格行组：以 `|` 开头的连续行块（单行组亦校验，审查 N7——实现为准），
      组内每行首尾管道齐备；围栏内管道行不排除（展示「残缺表格示例」类合法
      内容会被误伤——轻量校验边界登记简报 §6）。
    """

    trimmed = new_content.strip()
    if not trimmed:
        raise ChunkEditValidationError(CHUNK_CONTENT_INVALID, "编辑内容不能为空")
    if len(trimmed) > max_chars:
        raise ChunkEditValidationError(
            CHUNK_CONTENT_INVALID,
            f"编辑内容超出长度上限（{len(trimmed)} > {max_chars} 码点）",
        )
    if trimmed.count("```") % 2 != 0:
        raise ChunkEditValidationError(
            CHUNK_CONTENT_INVALID, "代码围栏未闭合（``` 开合不配对）"
        )
    lines = trimmed.splitlines()
    # 表格组 = 以 `|` 开头的连续行块（GFM 表格行惯例）；组内每行须首尾管道齐备——
    # 断裂行（有管道无收尾）即结构破坏（卡 D5-A 轻量校验，静态可判的边界）
    table_runs: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.lstrip().startswith("|"):
            current.append(line)
        elif current:
            table_runs.append(current)
            current = []
    if current:
        table_runs.append(current)
    for run in table_runs:
        if not all(
            row.lstrip().startswith("|") and row.rstrip().endswith("|") for row in run
        ):
            raise ChunkEditValidationError(
                CHUNK_CONTENT_INVALID, "表格结构不完整（管道行未以 | 起止）"
            )
    return trimmed


def check_optimistic_lock(base_hash: str, current_hash: str) -> str | None:
    """乐观锁判据（spec :324）：base_hash 与当前 chunk_hash 不一致 → `CHUNK_CONFLICT`。"""

    return None if base_hash == current_hash else CHUNK_CONFLICT


def derive_gate(submitted_count: int, already_confirmed: bool) -> str | None:
    """派生判据（spec :326-327）：已确认过派生 → `CHUNK_EDIT_STATE`（优先——重复派生
    检测不被提交数掩盖）；0 个提交 → `DERIVE_NO_EDITS`；放行返回 None。"""

    if already_confirmed:
        return CHUNK_EDIT_STATE
    if submitted_count <= 0:
        return DERIVE_NO_EDITS
    return None


def derive_target_version_no(existing_version_nos: list[int]) -> int:
    """派生版本号 = max(version_no)+1（D2-A；空版本历史从 1 起）。"""

    return max(existing_version_nos, default=0) + 1


# ---------------------------------------------------------------------------
# 服务编排（KB-10A；KbError 从 service 导入；查询全走 TenantScopedRepository）
# ---------------------------------------------------------------------------


_READER_SCOPE = ScopeBypass(reason="块编辑记录按版本/块 id 精确读取，权限在端点层")
_AUDIT_REASON = "块编辑记录按 id 精确操作"


@dataclass(frozen=True)
class ChunkEditView:
    """一条块编辑提交记录视图（GET 列表/POST 回包共用形态；M2：new_content/diff 摘要透出）。"""

    chunk_id: UUID
    source_version_id: UUID
    status: str
    before_hash: str
    after_hash: str
    new_content: str
    reason: str
    derived_version_id: UUID | None


def _view(row: KbChunkEdit) -> ChunkEditView:
    return ChunkEditView(
        chunk_id=row.chunk_id,
        source_version_id=row.source_version_id,
        status=row.status,
        before_hash=row.before_hash,
        after_hash=row.after_hash,
        new_content=row.new_content,
        reason=row.reason,
        derived_version_id=row.derived_version_id,
    )


def _reader_repository(
    session: AsyncSession, tenant_id: UUID
) -> TenantScopedRepository[KbChunkEdit]:
    return TenantScopedRepository(session, KbChunkEdit, tenant_id, scope=_READER_SCOPE)


async def _audit_edit_denied(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    chunk_id: UUID,
    user_id: UUID,
    code: str,
    request_id: str | None = None,
) -> None:
    from app.audit.writer import append_audit

    await append_audit(
        session,
        action="school.kb.chunk.edit_denied",
        resource_type="kb_chunk",
        resource_id=str(chunk_id),
        actor_realm="tenant",
        actor_id=user_id,
        tenant_id=tenant_id,
        result="denied",
        metadata={"code": code},
        request_id=request_id,
    )


async def _audit_edit_denied_standalone(
    *,
    tenant_id: UUID,
    chunk_id: UUID,
    user_id: UUID,
    code: str,
    request_id: str | None = None,
) -> None:
    """拒绝审计（独立短事务——KB-02 先例：409 前置校验不涉及主事务写）。"""


    async with db_session(tenant_id=tenant_id) as session:
        await _audit_edit_denied(
            session,
            tenant_id=tenant_id,
            chunk_id=chunk_id,
            user_id=user_id,
            code=code,
            request_id=request_id,
        )
        await session.flush()


async def submit_chunk_edit(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    chunk_id: UUID,
    user_id: UUID,
    new_content: str,
    reason: str,
    base_hash: str,
    request_id: str | None = None,
) -> ChunkEditView:
    """提交块编辑（spec :324；权限 `school.kb.chunk.edit` 在端点层）。

    跨租/不存在 404 同文不泄露 → 乐观锁 409 `CHUNK_CONFLICT`（拒绝审计）→ 内容校验
    422 → 同块已有 submitted 409（部分唯一兜底前置）→ 写 submitted + 审计
    `school.kb.chunk.edited`。
    """

    from app.audit.writer import append_audit

    async with db_session(tenant_id=tenant_id) as session:
        chunk = await TenantScopedRepository(
            session, KbChunk, tenant_id, scope=_READER_SCOPE
        ).get(chunk_id)
        if chunk is None:
            raise KbError("CHUNK_NOT_FOUND", "分块不存在", 404)
        # M1：编辑必须先通过所属版本的数据范围校验；仅 tenant/RLS 不足以
        # 防止同租 self/空范围主体拿到 chunk_id 后越权提交。
        version_repository = TenantScopedRepository(
            session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
        )
        if await version_repository.get(chunk.document_version_id) is None:
            raise KbError("CHUNK_NOT_FOUND", "分块不存在", 404)
        # M1（spec :323 仅子块文本）：父块编辑 = 结构性改动，走重传+重审——服务端拦截。
        # 拒绝审计走独立事务（同乐观锁 409：主事务 raise 回滚主事务内审计写入）
        if chunk.chunk_type != "child":
            await _audit_edit_denied_standalone(
                tenant_id=tenant_id,
                chunk_id=chunk_id,
                user_id=user_id,
                code=CHUNK_EDIT_TARGET_INVALID,
                request_id=request_id,
            )
            raise KbError(
                CHUNK_EDIT_TARGET_INVALID, "仅子块文本可编辑（父块/结构性改动走重传+重审）", 422
            )
        conflict = check_optimistic_lock(base_hash, chunk.chunk_hash)
        if conflict is not None:
            # 拒绝审计走独立事务（KB-02 先例：主事务 raise 会回滚主事务内的审计写入）
            await _audit_edit_denied_standalone(
                tenant_id=tenant_id,
                chunk_id=chunk_id,
                user_id=user_id,
                code=conflict,
                request_id=request_id,
            )
            raise KbError(conflict, "分块内容已被他人编辑（base_hash 过期），请刷新后重试", 409)
        try:
            normalized = validate_edit_content(new_content)
        except ChunkEditValidationError as exc:
            raise KbError(exc.code, str(exc), 422) from exc
        submitted = (
            (
                await session.execute(
                    _reader_repository(session, tenant_id)
                    .base_query()
                    .where(KbChunkEdit.chunk_id == chunk_id, KbChunkEdit.status == "submitted")
                )
            )
            .scalars()
            .all()
        )
        if submitted:
            await _audit_edit_denied_standalone(
                tenant_id=tenant_id,
                chunk_id=chunk_id,
                user_id=user_id,
                code=CHUNK_CONFLICT,
                request_id=request_id,
            )
            raise KbError(CHUNK_CONFLICT, "该分块已有待派生的编辑提交（先派生或等待处理）", 409)
        row = KbChunkEdit(
            tenant_id=tenant_id,
            source_version_id=chunk.document_version_id,
            chunk_id=chunk.id,
            before_hash=chunk.chunk_hash,
            after_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            new_content=normalized,
            reason=reason,
            status="submitted",
            edited_by=user_id,
        )
        # M2（审查）：唯一冲突在 repository.add() 的内部 flush 抛出——必须把 add
        # 本身放进 try，并只映射预期约束名；非预期 IntegrityError 原样上抛。
        try:
            await _reader_repository(session, tenant_id).add(row)
        except IntegrityError as exc:
            constraint = _integrity_constraint_name(exc)
            if constraint != "uq_kb_chunk_edits_source_chunk_submitted":
                raise
            # 失败主事务回滚后写独立拒绝审计（所需 id 先于 flush 捕获）
            await _audit_edit_denied_standalone(
                tenant_id=tenant_id,
                chunk_id=chunk_id,
                user_id=user_id,
                code=CHUNK_CONFLICT,
                request_id=request_id,
            )
            raise KbError(
                CHUNK_CONFLICT, "该分块已有待派生的编辑提交（并发提交，先派生或等待处理）", 409
            ) from exc
        await append_audit(
            session,
            action="school.kb.chunk.edited",
            resource_type="kb_chunk",
            resource_id=str(chunk_id),
            actor_realm="tenant",
            actor_id=user_id,
            tenant_id=tenant_id,
            result="success",
            metadata={
                "source_version_id": str(chunk.document_version_id),
                "before_hash": chunk.chunk_hash,
                "after_hash": row.after_hash,
                "reason": reason,
            },
            request_id=request_id,
        )
        return _view(row)


async def list_chunk_edits(
    *, tenant_id: UUID, scope: DataScopeContext, version_id: UUID
) -> list[ChunkEditView]:
    """块编辑提交记录列表（`school.kb.view`；跨租/范围外版本 404 同文不泄露）。"""

    async with db_session(tenant_id=tenant_id) as session:
        version_repo = TenantScopedRepository(
            session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
        )
        if await version_repo.get(version_id) is None:
            raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
    async with db_session(tenant_id=tenant_id) as session:
        rows_ = (
            (
                await session.execute(
                    _reader_repository(session, tenant_id)
                    .base_query()
                    .where(KbChunkEdit.source_version_id == version_id)
                    .order_by(KbChunkEdit.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
    return [_view(row) for row in rows_]


async def derive_version(
    *,
    tenant_id: UUID,
    scope: DataScopeContext,
    version_id: UUID,
    user_id: UUID,
    request_id: str | None = None,
) -> dict[str, Any]:
    """确认并派生新版本（spec :326；权限 `school.kb.chunk.confirm` 在端点层）。

    单事务（源版本行 FOR UPDATE 串行化并发派生）：状态判据 → gate → 新版本行
    （version_no=max+1、parent_version_id、derivation_type='chunk_edit'、status=APPROVED、
    源行字段复制）→ ACL 继承（audiences 行 + 确认态列，D3-A）→ 提交态翻转
    （submitted → confirmed + derived_version_id）→ BUILD_QUEUED → 审计 → 事务外
    best-effort 入队（复用 `_enqueue_build`，retry 端点兜底）。
    """

    from app.audit.writer import append_audit

    async with db_session(tenant_id=tenant_id) as session:
        version_repo = TenantScopedRepository(
            session, KbDocumentVersion, tenant_id, scope=scope, owner_attr="created_by"
        )
        source = (
            await session.execute(
                version_repo.base_query().where(KbDocumentVersion.id == version_id).with_for_update()
            )
        ).scalar_one_or_none()
        if source is None:
            raise KbError("VERSION_NOT_FOUND", "文档版本不存在", 404)
        if source.status not in ("READY", "TEST_PASSED", "PUBLISHED", "APPROVED"):
            raise KbError("CHUNK_EDIT_STATE", f"版本状态 {source.status} 不可派生", 409)
        submitted = (
            (
                await session.execute(
                    _reader_repository(session, tenant_id)
                    .base_query()
                    .where(KbChunkEdit.source_version_id == version_id, KbChunkEdit.status == "submitted")
                )
            )
            .scalars()
            .all()
        )
        confirmed = (
            (
                await session.execute(
                    _reader_repository(session, tenant_id)
                    .base_query()
                    .where(
                        KbChunkEdit.source_version_id == version_id,
                        KbChunkEdit.status == "confirmed",
                        KbChunkEdit.derived_version_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        gate = derive_gate(len(submitted), bool(confirmed))
        if gate is not None:
            messages = {
                DERIVE_NO_EDITS: "该版本没有待派生的块编辑提交",
                CHUNK_EDIT_STATE: "该版本的块编辑已派生过（不可重复派生）",
            }
            raise KbError(gate, messages[gate], 409)

        existing_nos = (
            (
                await session.execute(
                    TenantScopedRepository(
                        session,
                        KbDocumentVersion,
                        tenant_id,
                        scope=ScopeBypass(reason=_AUDIT_REASON),
                    )
                    .base_query()
                    .where(KbDocumentVersion.document_id == source.document_id)
                    .with_only_columns(KbDocumentVersion.version_no)
                )
            )
            .scalars()
            .all()
        )
        derived_id = uuid4()
        derived_version = KbDocumentVersion(
            id=derived_id,
            tenant_id=tenant_id,
            document_id=source.document_id,
            version_no=derive_target_version_no(list(existing_nos)),
            source_object_key=source.source_object_key,
            source_filename=source.source_filename,
            source_hash=source.source_hash,
            mime=source.mime,
            size_bytes=source.size_bytes,
            status="APPROVED",  # 派生走 APPROVED→READY（超管确认替代平台审批，state-machines :92）
            scan_engine=source.scan_engine,
            scanned_at=source.scanned_at,
            parent_version_id=source.id,
            derivation_type="chunk_edit",
            acl_confirmed_by=source.acl_confirmed_by,  # D3-A：沿用源已确认 ACL
            acl_confirmed_at=source.acl_confirmed_at,
            created_by=user_id,
        )
        # M2（审查）：version_no 唯一冲突在 repository.add() 的内部 flush 抛出——
        # 把 add 本身放进 try，只映射预期约束名；失败事务回滚（无半成品），独立审计在
        # 事务退出后写。非预期 IntegrityError 原样上抛。
        try:
            await TenantScopedRepository(
                session, KbDocumentVersion, tenant_id, scope=ScopeBypass(reason=_AUDIT_REASON)
            ).add(derived_version)
        except IntegrityError as exc:
            constraint = _integrity_constraint_name(exc)
            if constraint != "uq_kb_document_versions_tenant_document_version":
                raise
            raise KbError(
                "CHUNK_EDIT_STATE", "该文档正有并发派生（版本号冲突），请刷新后重试", 409
            ) from exc

        # ACL 继承（D3-A）：复制源版本人群行 + 确认态列
        audience_scope = ScopeBypass(reason="人群行随派生版本复制，系统判据")
        source_audiences = (
            (
                await session.execute(
                    TenantScopedRepository(
                        session, KbDocumentAudience, tenant_id, scope=audience_scope
                    )
                    .base_query()
                    .where(KbDocumentAudience.version_id == source.id)
                )
            )
            .scalars()
            .all()
        )
        audience_repo = TenantScopedRepository(
            session, KbDocumentAudience, tenant_id, scope=audience_scope
        )
        for row in source_audiences:
            await audience_repo.add(
                KbDocumentAudience(
                    tenant_id=tenant_id,
                    version_id=derived_id,
                    audience_type=row.audience_type,
                    audience_id=row.audience_id,
                    created_by=user_id,
                )
            )

        # 提交态翻转：submitted → confirmed + derived_version_id
        for edit in submitted:
            edit.status = "confirmed"
            edit.derived_version_id = derived_id

        job = KbBuildJob(
            tenant_id=tenant_id,
            version_id=derived_id,
            status="BUILD_QUEUED",
            attempt=1,
            created_by=user_id,
        )
        await TenantScopedRepository(
            session, KbBuildJob, tenant_id, scope=ScopeBypass(reason="派生构建任务创建，系统执行者")
        ).add(job)
        await append_audit(
            session,
            action="school.kb.chunk.derive_confirmed",
            resource_type="kb_document_version",
            resource_id=str(version_id),
            actor_realm="tenant",
            actor_id=user_id,
            tenant_id=tenant_id,
            result="success",
            metadata={
                "derived_version_id": str(derived_id),
                "edited_chunks": len(submitted),
                "edited_by": [str(edit.edited_by) for edit in submitted],
                "acl_inherited": source.acl_confirmed_at is not None,
            },
            request_id=request_id,
        )
        # version_no 唯一冲突已在 add(derived_version) 处按约束名映射 409（M2）；
        # 此处收尾 flush 的意外完整性错误不再伪装成业务冲突——原样上抛（宁可 500）。
        await session.flush()

    from app.kb.service import _enqueue_build

    await _enqueue_build(tenant_id, derived_id, 1)
    return {
        "version_id": derived_id,
        "parent_version_id": source.id,
        "derivation_type": "chunk_edit",
        "edited_chunks": len(submitted),
    }
