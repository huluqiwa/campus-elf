"""校方侧批量导入接口（M2 ORG-02，`docs/design-briefs/M2-ORG02-import.md`）。

- POST /imports：multipart 上传（≤10MB、.csv/.xlsx）→ 对象存储 → 建 job → Arq 入队 → 201；
- GET /imports/{job_id}：进度与汇总；
- GET /imports/{job_id}/errors：错误行列表（≤5000 行，row_no 升序，整数游标）；
- POST /imports/{job_id}/retry：基于原文件 + 失败行号生成派生任务（只导失败行）。

权限：`school.personnel.import`（矩阵 §5 行 174）。上传即入队，无同步导入路径；
解析的致命错误（表头/超限）在上传期即 422，任务层只处理行级问题。
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from app.auth.principal import Principal, require_permission
from app.config import get_settings
from app.db.models.imports import ImportJob, ImportRowError
from app.db.repository import ScopeBypass, TenantScopedRepository
from app.db.session import db_session
from app.org.import_parser import parse_import_file
from app.org.service import MAX_PAGE_SIZE, OrgError
from app.storage.objects import build_object_store
from app.workers.settings import create_producer_pool

router = APIRouter(prefix="/school", tags=["school"])

ImporterDep = require_permission("school.personnel.import", realm="tenant")

MAX_UPLOAD_SIZE = 10 * 1024 * 1024


async def _audit_job(
    principal: Principal,
    tenant_id: UUID,
    *,
    action: str,
    resource_id: str,
    metadata: dict[str, Any],
) -> None:
    """job 生命周期审计（org.import.*；行级效果已由建档审计覆盖）。"""

    import logging

    from app.audit.writer import append_audit

    try:
        async with db_session(tenant_id=tenant_id) as session:
            await append_audit(
                session,
                action=action,
                resource_type="org_import",
                resource_id=resource_id,
                actor_realm="tenant",
                actor_id=principal.user_id,
                tenant_id=tenant_id,
                result="success",
                metadata=metadata,
                request_id=None,
            )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "导入 job 审计写入失败：%s.%s", type(exc).__module__, type(exc).__name__
        )


class ImportJobResponse(BaseModel):
    id: UUID
    type: str
    conflict: str
    status: str
    total_rows: int
    success_rows: int
    failed_rows: int
    skipped_rows: int
    retried_by: UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    message: str | None


class ImportRowErrorResponse(BaseModel):
    row_no: int
    field: str
    error_code: str
    message: str


class ImportErrorsResponse(BaseModel):
    items: list[ImportRowErrorResponse]
    next_cursor: int | None


class ImportRetryResponse(BaseModel):
    source_job_id: UUID
    job_id: UUID


def _job_repository(
    session: Any, tenant_id: UUID, principal: Principal
) -> TenantScopedRepository[ImportJob]:
    return TenantScopedRepository(session, ImportJob, tenant_id, scope=principal.scope_context)


def _job_response(job: ImportJob) -> ImportJobResponse:
    return ImportJobResponse(
        id=job.id,
        type=job.type,
        conflict=job.conflict,
        status=job.status,
        total_rows=job.total_rows,
        success_rows=job.success_rows,
        failed_rows=job.failed_rows,
        skipped_rows=job.skipped_rows,
        retried_by=job.retried_by,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        message=job.message,
    )


async def _enqueue(tenant_id: UUID, job_id: UUID, *, source_job_id: UUID | None = None) -> None:
    """入队；Redis 不可达 → job 置 failed 并 503（避免悬挂 pending）。

    重试路径（source_job_id 非空）：补偿必须**同事务清除源 job 的 retried_by**，
    否则用户会被「已重试过」与「仅已完成可重试」双重 409 永久锁死。
    """

    try:
        pool = await create_producer_pool(get_settings())
        await pool.enqueue_job("run_import_job", str(job_id), str(tenant_id))
    except Exception as exc:
        async with db_session(tenant_id=tenant_id) as session:
            repository: TenantScopedRepository[ImportJob] = TenantScopedRepository(
                session,
                ImportJob,
                tenant_id,
                scope=ScopeBypass(reason="入队失败回写自身 job 终态"),
            )
            job = await repository.get(job_id)
            if job is not None:
                job.status = "failed"
                job.message = "任务队列不可用，请稍后重试"
                await session.flush()
            if source_job_id is not None:
                source = await repository.get(source_job_id)
                if source is not None and source.retried_by == job_id:
                    source.retried_by = None
                    await session.flush()
        raise OrgError("IMPORT_ENQUEUE_FAILED", "任务队列不可用，请稍后重试", 503) from exc


@router.post("/imports", status_code=201, response_model=ImportJobResponse)
async def create_import(
    viewer: Annotated[Principal, ImporterDep],
    file: Annotated[UploadFile, File(description="CSV/XLSX 人员数据文件")],
    import_type: Annotated[str, Form(alias="type", pattern="^(teachers|students)$")],
    conflict: Annotated[str, Form(pattern="^(error|skip)$")] = "error",
) -> ImportJobResponse:
    tenant_id = _tenant_or_403(viewer)
    data = await file.read()
    if len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=422, detail="文件超过 10MB 上限")
    filename = file.filename or "upload.csv"

    # 上传期即拒绝解析致命错误（表头/超限/伪装扩展名）：任务层只处理行级问题。
    # XLSX 解析可达秒级 CPU：下放线程，不阻塞事件循环
    import anyio

    def _parse_upload() -> None:
        try:
            parse_import_file(data, filename, import_type)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    await anyio.to_thread.run_sync(_parse_upload)

    suffix = ".xlsx" if filename.lower().endswith(".xlsx") else ".csv"
    object_key = f"imports/{tenant_id}/{uuid4().hex}{suffix}"
    content_type = (
        "text/csv"
        if suffix == ".csv"
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    store = build_object_store(get_settings())
    await store.put(object_key, data, content_type)

    async with db_session(tenant_id=tenant_id) as session:
        repository = _job_repository(session, tenant_id, viewer)
        job = ImportJob(
            tenant_id=tenant_id,
            type=import_type,
            conflict=conflict,
            file_object_key=object_key,
            status="pending",
            created_by=viewer.user_id,
        )
        await repository.add(job)

    await _enqueue(tenant_id, job.id)
    await _audit_job(
        viewer,
        tenant_id,
        action="org.import.created",
        resource_id=str(job.id),
        metadata={"type": import_type, "conflict": conflict},
    )
    return _job_response(job)


@router.get("/imports/{job_id}", response_model=ImportJobResponse)
async def get_import(job_id: UUID, viewer: Annotated[Principal, ImporterDep]) -> ImportJobResponse:
    tenant_id = _tenant_or_403(viewer)
    async with db_session(tenant_id=tenant_id) as session:
        repository = _job_repository(session, tenant_id, viewer)
        job = await repository.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="导入任务不存在")
        return _job_response(job)


@router.get("/imports/{job_id}/errors", response_model=ImportErrorsResponse)
async def list_import_errors(
    job_id: UUID,
    viewer: Annotated[Principal, ImporterDep],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 100,
    cursor: Annotated[int | None, Query(ge=0)] = None,
) -> ImportErrorsResponse:
    """错误行列表（row_no 升序；整数游标 = 已取的最大 row_no）。"""

    tenant_id = _tenant_or_403(viewer)
    async with db_session(tenant_id=tenant_id) as session:
        job_repository = _job_repository(session, tenant_id, viewer)
        if await job_repository.get(job_id) is None:
            raise HTTPException(status_code=404, detail="导入任务不存在")
        error_repository: TenantScopedRepository[ImportRowError] = TenantScopedRepository(
            session,
            ImportRowError,
            tenant_id,
            scope=viewer.scope_context,
        )
        stmt = (
            error_repository.base_query()
            .where(ImportRowError.job_id == job_id)
            .order_by(ImportRowError.row_no.asc())
            .limit(limit + 1)
        )
        if cursor is not None:
            stmt = stmt.where(ImportRowError.row_no > cursor)
        rows = (await session.execute(stmt)).scalars().all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        return ImportErrorsResponse(
            items=[
                ImportRowErrorResponse(
                    row_no=row.row_no,
                    field=row.field,
                    error_code=row.error_code,
                    message=row.message,
                )
                for row in rows
            ],
            next_cursor=rows[-1].row_no if has_more and rows else None,
        )


@router.post("/imports/{job_id}/retry", status_code=201, response_model=ImportRetryResponse)
async def retry_import(
    job_id: UUID, viewer: Annotated[Principal, ImporterDep]
) -> ImportRetryResponse:
    """对已完成任务重试失败行：派生新 job（row_filter=失败行号），原 job 指向派生。"""

    tenant_id = _tenant_or_403(viewer)
    async with db_session(tenant_id=tenant_id) as session:
        repository = _job_repository(session, tenant_id, viewer)
        source = await repository.get(job_id)
        if source is None:
            raise HTTPException(status_code=404, detail="导入任务不存在")
        # failed（看门狗收口/入队失败）→ 整文件重跑（无 row_filter，已导入行以冲突报错）；
        # completed_with_errors → 仅失败行（row_filter）
        if source.status == "failed":
            row_filter: list[int] | None = None
        elif source.status in {"completed", "completed_with_errors"}:
            if source.failed_rows == 0:
                raise HTTPException(status_code=409, detail="没有可重试的失败行")
            row_filter = sorted(await _failed_row_numbers(session, tenant_id, job_id, viewer))
        else:
            raise HTTPException(status_code=409, detail="任务仍在排队或执行中，请稍后再试")
        if source.retried_by is not None:
            raise HTTPException(status_code=409, detail="该任务已重试过，请对派生任务操作")

        derived = ImportJob(
            tenant_id=tenant_id,
            type=source.type,
            conflict=source.conflict,
            file_object_key=source.file_object_key,
            status="pending",
            created_by=viewer.user_id,
            row_filter_json=row_filter,
            retry_count=source.retry_count + 1,
        )
        await repository.add(derived)
        # CAS：并发双击只有一个 retry 成功（retried_by 仍为 NULL 才置值）
        from sqlalchemy import update as sa_update

        claimed: Any = await session.execute(
            sa_update(ImportJob)
            .where(ImportJob.id == source.id, ImportJob.retried_by.is_(None))
            .values(retried_by=derived.id)
        )
        if claimed.rowcount == 0:
            raise HTTPException(status_code=409, detail="该任务已重试过，请对派生任务操作")

    await _enqueue(tenant_id, derived.id, source_job_id=job_id)
    await _audit_job(
        viewer,
        tenant_id,
        action="org.import.retried",
        resource_id=str(job_id),
        metadata={
            "derived_job_id": str(derived.id),
            "failed_rows": source.failed_rows,
            "full_rerun": source.status == "failed",
        },
    )
    return ImportRetryResponse(source_job_id=job_id, job_id=derived.id)


async def _failed_row_numbers(
    session: Any, tenant_id: UUID, job_id: UUID, principal: Principal
) -> set[int]:
    """失败行号集合（重试行过滤；走仓储与上传者范围——job 本身在其范围内）。"""

    repository: TenantScopedRepository[ImportRowError] = TenantScopedRepository(
        session,
        ImportRowError,
        tenant_id,
        scope=principal.scope_context,
    )
    rows = (
        (await session.execute(repository.base_query().where(ImportRowError.job_id == job_id)))
        .scalars()
        .all()
    )
    return {row.row_no for row in rows}


def _tenant_or_403(principal: Principal) -> UUID:
    if principal.realm != "tenant" or principal.tenant_id is None:
        raise HTTPException(status_code=403, detail="批量导入只在校方租户上下文内可用")
    return principal.tenant_id
