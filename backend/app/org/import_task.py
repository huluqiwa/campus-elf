"""导入任务执行器（M2 ORG-02）：Arq worker 调用的逐行导入。

口径（`docs/design-briefs/M2-ORG02-import.md`，已确认）：

- **行级原子**：每行走 `app/org/service.py` 的建档函数（自带独立事务），
  失败行落 `import_row_errors`（含原始行 JSON），合法行照常入库——不产生半成品**行**；
- **执行者**：`ImportActor` 是 `OrgActor` 协议的系统执行者（代表上传者落审计），
  仓储范围显式 `ScopeBypass`——导入按 `school.personnel.import` 授权做全校维度批量操作；
- **租户上下文**：任务参数自带 `tenant_id`（入队时由上传接口传入；
  worker 无请求上下文，import_jobs 是租户表，平台连接不可见）；
- **幂等**：仅 `pending` 状态执行；重复投递直接返回当前状态；
- **进度**：每 PROGRESS_INTERVAL 行与收尾各回写一次（ORM 更新，TD-069）；
- **TD-174 对账**：任务收尾跑一次范围行对账，不一致记 message（导入不产生教师范围行，
  正常恒一致；断言是回归守卫）。
"""

import logging
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import func

from app.config import get_settings
from app.db.models.identity import User
from app.db.models.imports import ImportJob, ImportRowError
from app.db.models.org import OrgClass, Student, Teacher
from app.db.repository import ScopeBypass, TenantScopedRepository
from app.db.session import db_session
from app.org.id_card_service import IdCardNotConfigured
from app.org.import_parser import parse_import_file
from app.org.scopes_sync import actual_class_scope_pairs, expected_class_scope_pairs
from app.org.service import (
    OrgError,
    create_student,
    create_teacher,
)
from app.security.id_card import mask_id_card
from app.storage.objects import build_object_store

logger = logging.getLogger(__name__)

# 进度回写间隔（行）：频繁回写徒增事务，过疏则进度条失去意义
PROGRESS_INTERVAL = 50

row_no_key = "row_no"


@dataclass(frozen=True)
class ImportActor:
    """导入任务的系统执行者（OrgActor 协议；审计归属=上传者）。"""

    user_id: UUID
    tenant_id: UUID | None
    realm: str = "tenant"
    scope_context: ScopeBypass = field(
        default_factory=lambda: ScopeBypass(
            reason=(
                "导入任务：按上传时授权的 school.personnel.import 做全校维度批量建档，"
                "逐行不受调用者数据范围收窄"
            )
        )
    )


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """任务执行结果摘要（Arq 返回值，供排障）。"""

    job_id: str
    status: str
    total_rows: int
    success_rows: int
    failed_rows: int
    skipped_rows: int


async def _load_job(session: Any, tenant_id: UUID, job_id: UUID) -> ImportJob | None:
    repository: TenantScopedRepository[ImportJob] = TenantScopedRepository(
        session,
        ImportJob,
        tenant_id,
        scope=ScopeBypass(reason="导入任务读自身 job 行：系统执行者，按 job_id 幂等执行"),
    )
    return await repository.get(job_id)


async def _update_progress(
    tenant_id: UUID, job_id: UUID, *, total: int, success: int, failed: int, skipped: int
) -> None:
    """回写进度（独立短事务；条件更新，TD-069 口径）。

    仅当 job 仍为 running 时写入：看门狗收口后 worker 收尾不得「复活」终态。
    """

    from sqlalchemy import update as sa_update

    async with db_session(tenant_id=tenant_id) as session:
        result: Any = await session.execute(
            sa_update(ImportJob)
            .where(ImportJob.id == job_id, ImportJob.status == "running")
            .values(
                total_rows=total, success_rows=success, failed_rows=failed, skipped_rows=skipped
            )
        )
        if result.rowcount == 0:
            logger.warning("导入进度回写跳过：job=%s 已被看门狗收口", job_id)


async def _finish(
    tenant_id: UUID,
    job_id: UUID,
    *,
    status: str,
    total: int,
    success: int,
    failed: int,
    skipped: int,
    message: str | None = None,
) -> None:
    """回写终态（条件更新）：仅 running 可写，与看门狗竞态时不复活已收口的 job。"""

    from sqlalchemy import update as sa_update

    async with db_session(tenant_id=tenant_id) as session:
        result: Any = await session.execute(
            sa_update(ImportJob)
            .where(ImportJob.id == job_id, ImportJob.status == "running")
            .values(
                status=status,
                total_rows=total,
                success_rows=success,
                failed_rows=failed,
                skipped_rows=skipped,
                finished_at=func.now(),
                message=message,
            )
        )
        if result.rowcount == 0:
            logger.warning("导入终态回写跳过：job=%s 已被看门狗收口", job_id)


def _redact_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """错误行留痕脱敏（复审 B2）：身份证明文不得落 `import_row_errors.raw_json`，只留掩码。"""

    redacted = {k: v for k, v in raw.items() if k != row_no_key}
    number = redacted.get("id_card_number")
    if number is not None:
        if isinstance(number, str) and len(number.strip()) == 18:
            try:
                redacted["id_card_number"] = mask_id_card(number.strip().upper())
            except ValueError:
                redacted["id_card_number"] = "***"
        else:
            redacted["id_card_number"] = "***"
    return redacted


async def _record_row_error(
    tenant_id: UUID,
    job_id: UUID,
    *,
    row_no: int,
    field: str,
    error_code: str,
    message: str,
    raw: dict[str, Any],
) -> None:
    async with db_session(tenant_id=tenant_id) as session:
        repository: TenantScopedRepository[ImportRowError] = TenantScopedRepository(
            session,
            ImportRowError,
            tenant_id,
            scope=ScopeBypass(reason="导入任务落错误行：系统执行者，按 job 归属写入"),
        )
        await repository.add(
            ImportRowError(
                tenant_id=tenant_id,
                job_id=job_id,
                row_no=row_no,
                field=field,
                error_code=error_code,
                message=message,
                raw_json=_redact_raw(raw),
            )
        )


async def _locate_class(
    session: Any, tenant_id: UUID, grade: str, name: str
) -> tuple[UUID | None, bool]:
    """按（当前年级, 班名）定位活跃班级（ORG-06：归档届不参与定位）。

    返回 (class_id, ambiguous)；不存在 → (None, False)；
    同年级+班名命中多个活跃班（留届等罕见形态）→ (None, True)（行错误 CLASS_AMBIGUOUS）。
    """

    repository: TenantScopedRepository[OrgClass] = TenantScopedRepository(
        session,
        OrgClass,
        tenant_id,
        scope=ScopeBypass(reason="导入定位目标班级：班级目录非个人数据"),
    )
    hits = (
        (
            await session.execute(
                repository.base_query().where(
                    OrgClass.grade == grade,
                    OrgClass.name == name,
                    OrgClass.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    if not hits:
        return None, False
    if len(hits) > 1:
        return None, True
    return hits[0].id, False


async def _existing_login(session: Any, tenant_id: UUID, login_id: str) -> bool:
    user_repository: TenantScopedRepository[User] = TenantScopedRepository(
        session,
        User,
        tenant_id,
        scope=ScopeBypass(reason="导入冲突探测：只读登录名存在性，不读档案内容"),
    )
    return (
        await session.execute(user_repository.base_query().where(User.login_id == login_id))
    ).scalars().first() is not None


async def _existing_no(session: Any, tenant_id: UUID, import_type: str, no_value: str) -> bool:
    model = Teacher if import_type == "teachers" else Student
    no_attr = Teacher.employee_no if import_type == "teachers" else Student.student_no
    repository: TenantScopedRepository[Any] = TenantScopedRepository(
        session,
        model,
        tenant_id,
        scope=ScopeBypass(reason="导入冲突探测：只读学号/工号存在性"),
    )
    return (
        await session.execute(repository.base_query().where(no_attr == no_value))
    ).scalars().first() is not None


def _validate_row(
    import_type: str, row: dict[str, Any]
) -> tuple[str | None, str | None, str | None]:
    """行级格式校验（数据库无关部分）。

    返回 (field, error_code, message)；全部合法时 (None, None, None)。
    """

    no_field = "employee_no" if import_type == "teachers" else "student_no"
    if not row.get(no_field):
        return no_field, "VALUE_REQUIRED", f"{no_field} 不能为空"
    if not row.get("name"):
        return "name", "VALUE_REQUIRED", "姓名不能为空"
    age = row.get("age") or None
    if (
        age is not None
        and import_type == "teachers"
        and (not age.isdigit() or not 16 <= int(age) <= 80)
    ):
        return "age", "VALUE_INVALID", "年龄须为 16~80 的整数"
    gender = row.get("gender") or None
    if gender is not None and gender not in {"male", "female", "other"}:
        return "gender", "VALUE_INVALID", "性别须为 male/female/other"
    email = row.get("email") or None
    if email is not None and "@" not in email:
        return "email", "VALUE_INVALID", "邮箱格式不正确"
    if import_type == "students" and (bool(row.get("class_grade")) != bool(row.get("class_name"))):
        return "class_grade", "VALUE_INVALID", "年级与班级须同时提供"
    return None, None, None


async def run_import_job(ctx: dict[str, Any], job_id: str, tenant_id: str | UUID) -> dict[str, Any]:
    """执行一次导入任务（Arq 入口；测试可直接调用）。

    参数:
        ctx: Arq 任务上下文（未使用）。
        job_id: 导入任务 id（字符串形式的 UUID）。
        tenant_id: 上传者所在租户 id（worker 无请求上下文，见模块 docstring）。

    返回:
        ImportOutcome 的字典形式（状态与计数，供排障）。

    异常:
        不向 Arq 抛业务异常：任务级失败（文件缺失/解析致命）落 `failed` 终态并返回，
        避免 arq 按 max_tries 重跑已完成部分（行级幂等已保证，但终态语义更清晰）。
    """

    job_uuid = UUID(str(job_id))
    tenant_uuid = UUID(str(tenant_id))
    settings = get_settings()

    # ---- 读 job（系统探针范围绕过；幂等：仅 pending 执行） ----
    async with db_session(tenant_id=tenant_uuid) as session:
        job = await _load_job(session, tenant_uuid, job_uuid)
        if job is None:
            logger.error("导入任务不存在：%s", job_id)
            return {"job_id": job_id, "status": "not_found"}
        if job.status != "pending":
            outcome = ImportOutcome(
                job_id=job_id,
                status=job.status,
                total_rows=job.total_rows,
                success_rows=job.success_rows,
                failed_rows=job.failed_rows,
                skipped_rows=job.skipped_rows,
            )
            return asdict(outcome)
        job.status = "running"
        job.started_at = func.now()
        import_type: str = job.type
        conflict: str = job.conflict
        file_key: str = job.file_object_key
        actor_user_id: UUID = job.created_by
        row_filter: set[int] | None = (
            {int(v) for v in job.row_filter_json} if job.row_filter_json is not None else None
        )
        await session.flush()

    actor = ImportActor(user_id=actor_user_id, tenant_id=tenant_uuid)

    # ---- 读文件并解析（任务级致命错误 → failed 终态） ----
    store = build_object_store(settings)
    try:
        data = await store.get(file_key)
        rows = parse_import_file(data, file_key, import_type)
    except Exception as exc:
        failure_message = f"导入文件读取/解析失败：{type(exc).__name__}: {exc}"
        logger.warning("%s（job=%s）", failure_message, job_id)
        await _finish(
            tenant_uuid,
            job_uuid,
            status="failed",
            total=0,
            success=0,
            failed=0,
            skipped=0,
            message=failure_message,
        )
        return {"job_id": job_id, "status": "failed"}

    if row_filter is not None:
        rows = [row for row in rows if row[row_no_key] in row_filter]

    total = len(rows)
    success = failed = skipped = 0
    # 文件内已见编号（学号/工号/登录名）：文件内互相矛盾的两行无论 conflict 策略都报错行（D3）
    seen_numbers: set[str] = set()
    logger.info("导入开始：job=%s type=%s rows=%d", job_id, import_type, total)

    for row in rows:
        row_no = int(row[row_no_key])
        field: str | None = "row"
        try:
            row_field, row_code, row_message = _validate_row(import_type, row)
            if row_field is not None:
                field = row_field
                raise OrgError(row_code or "VALUE_INVALID", row_message or "行数据不合法", 422)

            login_id = (
                row.get("login_id")
                or row.get("employee_no" if import_type == "teachers" else "student_no")
                or ""
            )
            no_value = row.get("employee_no" if import_type == "teachers" else "student_no") or ""

            # 文件内重复（D3）：与库内重复无关，矛盾行一律报错
            if login_id in seen_numbers or no_value in seen_numbers:
                field = "login_id"
                raise OrgError("FILE_DUPLICATE", "文件内存在重复的学号/工号", 409)
            seen_numbers.add(login_id)
            seen_numbers.add(no_value)

            conflict_hit: bool
            async with db_session(tenant_id=tenant_uuid) as session:
                # error 策略不预探测：建档函数内部的原子冲突检查给出精确字段；
                # skip 策略需要预探测以区分「跳过」
                if conflict == "skip":
                    conflict_hit = await _existing_login(session, tenant_uuid, login_id) or (
                        await _existing_no(session, tenant_uuid, import_type, no_value)
                    )
                else:
                    conflict_hit = False
                if conflict_hit:
                    skipped += 1
                    await _record_row_error(
                        tenant_uuid,
                        job_uuid,
                        row_no=row_no,
                        field="login_id",
                        error_code="SKIPPED_DUPLICATE",
                        message="学号/工号已存在，按 skip 策略跳过",
                        raw=row,
                    )

            if conflict_hit:
                continue

            if import_type == "teachers":
                department_id: UUID | None = None
                department_path = row.get("department") or None
                if department_path:
                    from app.org.departments import resolve_department_path

                    async with db_session(tenant_id=tenant_uuid) as session:
                        department_id = await resolve_department_path(
                            session, tenant_uuid, department_path
                        )
                    if department_id is None:
                        field = "department"
                        raise OrgError(
                            "DEPARTMENT_NOT_FOUND",
                            "部门不存在（按名称全路径逐级匹配失败）",
                            422,
                        )
                await create_teacher(
                    actor,
                    login_id=login_id,
                    employee_no=no_value,
                    name=row.get("name", ""),
                    age=int(row["age"]) if row.get("age") else None,
                    gender=row.get("gender") or None,
                    position=row.get("position") or None,
                    email=row.get("email") or None,
                    department_id=department_id,
                    id_card_number=row.get("id_card_number") or None,
                )
            else:
                class_grade = row.get("class_grade", "")
                class_name = row.get("class_name", "")
                class_id: UUID | None = None
                if class_grade and class_name:
                    async with db_session(tenant_id=tenant_uuid) as session:
                        class_id, ambiguous = await _locate_class(
                            session,
                            tenant_uuid,
                            class_grade,
                            class_name,
                        )
                    if ambiguous:
                        field = "class_name"
                        raise OrgError(
                            "CLASS_AMBIGUOUS",
                            "班级定位歧义：同年级同班名命中多个活跃班级",
                            422,
                        )
                    if class_id is None:
                        field = "class_grade"
                        raise OrgError("CLASS_NOT_FOUND", "目标班级不存在（年级/班名不匹配）", 422)
                # 未提供年级/班名 = 未分班学生（spec ORG-05 验收口径），class_id 置空
                await create_student(
                    actor,
                    login_id=login_id,
                    student_no=no_value,
                    name=row.get("name", ""),
                    gender=row.get("gender") or None,
                    class_id=class_id,
                    email=row.get("email") or None,
                    id_card_number=row.get("id_card_number") or None,
                )
            success += 1
        except OrgError as exc:
            failed += 1
            await _record_row_error(
                tenant_uuid,
                job_uuid,
                row_no=row_no,
                field=exc.field or field or "row",
                error_code=exc.code,
                message=exc.message,
                raw=row,
            )
        except IdCardNotConfigured as exc:
            # KEK 未配置：该行身份证无法加密（复审 M2），落结构化行错误而非 INTERNAL_ERROR
            failed += 1
            await _record_row_error(
                tenant_uuid,
                job_uuid,
                row_no=row_no,
                field="id_card_number",
                error_code="ID_CARD_NOT_CONFIGURED",
                message=str(exc),
                raw=row,
            )
        except Exception as exc:
            failed += 1
            logger.warning(
                "导入行意外失败 job=%s row=%d：%s: %s",
                job_id,
                row_no,
                type(exc).__module__,
                exc,
            )
            await _record_row_error(
                tenant_uuid,
                job_uuid,
                row_no=row_no,
                field="row",
                error_code="INTERNAL_ERROR",
                message=f"行处理发生意外错误：{exc}",
                raw=row,
            )

        if (success + failed + skipped) % PROGRESS_INTERVAL == 0:
            await _update_progress(
                tenant_uuid, job_uuid, total=total, success=success, failed=failed, skipped=skipped
            )

    # ---- TD-174 对账：导入不产生教师 class 范围行，对账应恒一致 ----
    reconciliation_note: str | None = None
    async with db_session(tenant_id=tenant_uuid) as session:
        expected = await expected_class_scope_pairs(session, tenant_uuid)
        actual = await actual_class_scope_pairs(session, tenant_uuid)
        if expected != actual:
            reconciliation_note = (
                "警告：导入结束后范围行与组织关系不一致（expected-actual="
                f"{len(expected - actual)}, actual-expected={len(actual - expected)}）"
            )
            logger.error("%s（job=%s）", reconciliation_note, job_id)

    final_status = "completed_with_errors" if failed else "completed"
    await _finish(
        tenant_uuid,
        job_uuid,
        status=final_status,
        total=total,
        success=success,
        failed=failed,
        skipped=skipped,
        message=reconciliation_note,
    )
    logger.info(
        "导入结束：job=%s status=%s total=%d success=%d failed=%d skipped=%d",
        job_id,
        final_status,
        total,
        success,
        failed,
        skipped,
    )
    outcome = ImportOutcome(
        job_id=job_id,
        status=final_status,
        total_rows=total,
        success_rows=success,
        failed_rows=failed,
        skipped_rows=skipped,
    )
    return asdict(outcome)


# running 状态的看门狗阈值（分钟）：超过即认为 worker 中断，收口为 failed（M2-ORG02 复审 M6）
STALE_RUNNING_MINUTES = 60


async def fail_stale_import_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    """看门狗：把长时间停留在 running 的导入任务收口为 failed（可对失败行重试）。

    worker 进程被杀/DB 抖动会让 job 停在 running（幂等守卫不放行重跑）；
    本任务按 started_at 超时收口；收口后的 failed 任务可经 retry 端点**整文件重跑**
    （不带 row_filter，已导入行以冲突错误行现形，覆盖收敛——方案 b，见简报 §10）。
    平台连接列出全部租户，逐租户在租户上下文内收口（与 cleanup_refresh_tokens 同模式）。
    """

    from datetime import timedelta

    from sqlalchemy import select
    from sqlalchemy import update as sa_update

    from app.db.models.tenant import Tenant

    cutoff = func.now() - timedelta(minutes=STALE_RUNNING_MINUTES)
    total = 0
    async with db_session(platform=True) as session:
        tenant_ids = (
            (await session.execute(select(Tenant.id).where(Tenant.status == "active")))
            .scalars()
            .all()
        )

    for tenant_id in tenant_ids:
        async with db_session(tenant_id=tenant_id) as session:
            result: Any = await session.execute(
                sa_update(ImportJob)
                .where(
                    ImportJob.status == "running",
                    ImportJob.started_at.is_not(None),
                    ImportJob.started_at < cutoff,
                )
                .values(
                    status="failed",
                    finished_at=func.now(),
                    message="任务执行中断（worker 异常或超时）；请对该任务重试（整文件重跑，已导入行将以冲突报错）",
                )
            )
            total += int(result.rowcount or 0)
    if total:
        logger.warning("看门狗收口了 %d 个卡死的导入任务", total)
    return {"failed_jobs": total}
