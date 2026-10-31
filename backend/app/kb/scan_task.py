"""知识库扫描 worker（M3 KB-01）：ClamAV 扫描单版本 + 滞留补投看门狗。

口径（`docs/design-briefs/M3-KB-01-upload.md` §7，已确认）：

- **短事务**：读版本 → 结束事务 → 事务外读对象并扫描 → 短事务条件更新；
  扫描是 120s 级网络操作，不得全程占用数据库连接与行锁；
- **fail-closed**：ClamAV 瞬时故障（不可用/超时/协议异常）**不迁移状态**，
  保持 `UPLOADED`、累计 `scan_attempts` 后由看门狗补投；`CLAMAV_FAIL_CLOSED=false`
  只允许开发/测试（`Settings` 校验生产必须为 true），降级放行到 `PENDING_APPROVAL`
  并标记 `scan_engine="dev-degraded"`；
- **完整性**：对象 sha256 与登记值不符 → `SCAN_FAILED/HASH_MISMATCH`（不扫描、不放行）；
- **感染**：先复制到同目录 `quarantine/` 子路径留证，再删除原件；
- **幂等**：只处理 `UPLOADED`；重复投递/看门狗与扫描竞态时，后到者直接跳过；
- **审计**：仅在真实迁移时写 `school.kb.scan.passed/infected/failed`（系统执行者）。
"""

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.writer import append_audit
from app.config import get_settings
from app.db.models.kb import KbDocument, KbDocumentVersion
from app.db.repository import ScopeBypass, TenantScopedRepository
from app.db.session import db_session
from app.kb.status import (
    KB_STATUS_PENDING_APPROVAL,
    KB_STATUS_SCAN_FAILED,
    KB_STATUS_UPLOADED,
    assert_transition,
)
from app.security.clamav import ClamAVError, scan_bytes
from app.storage.objects import build_object_store

logger = logging.getLogger(__name__)

# 看门狗阈值：超过即认为投递丢失或 worker 中断（正常扫描 ≤120s，5 分钟足够宽）
STALE_UPLOADED_MINUTES = 5
WATCHDOG_BATCH_SIZE = 200


def scan_job_id(version_id: UUID, attempt: int) -> str:
    """扫描任务的 arq job_id（带尝试序号）。

    结果保留期内同 job_id 不会再次入队：瞬时故障后 `scan_attempts` 递增，
    下一轮看门狗用新序号补投，不会被上一轮的结果键挡住。
    """

    return f"scan:{version_id}:{attempt}"


def _version_repository(session: AsyncSession, tenant_id: UUID) -> TenantScopedRepository[KbDocumentVersion]:
    return TenantScopedRepository(
        session,
        KbDocumentVersion,
        tenant_id,
        scope=ScopeBypass(reason="扫描任务：按版本 id 精确操作，系统执行者"),
    )


def _quarantine_key(object_key: str) -> str:
    """把源对象 key 移到同版本目录下的 `quarantine/` 子路径。"""

    head, _, tail = object_key.rpartition("/")
    return f"{head}/quarantine/{tail}" if head else f"quarantine/{object_key}"


async def _load_version(
    session: AsyncSession, tenant_id: UUID, version_id: UUID
) -> KbDocumentVersion | None:
    return await _version_repository(session, tenant_id).get(version_id)


async def _mark_attempt(
    tenant_id: UUID,
    version_id: UUID,
    *,
    error_code: str,
    engine: str | None,
) -> None:
    """记录一次未达结论的尝试（瞬时故障；状态保持 `UPLOADED` 等补投）。

    只写扫描元数据、不写审计：状态未变，重试由看门狗接手。
    """

    async with db_session(tenant_id=tenant_id) as session:
        version = await _version_repository(session, tenant_id).get(
            version_id, with_for_update=True
        )
        if version is None or version.status != KB_STATUS_UPLOADED:
            return
        version.scan_attempts += 1
        version.scan_error_code = error_code
        if engine is not None:
            version.scan_engine = engine
        await session.flush()


async def _finish(
    tenant_id: UUID,
    version_id: UUID,
    *,
    status: str,
    engine: str | None,
    action: str,
    metadata: dict[str, Any],
    signature: str | None = None,
    error_code: str | None = None,
    source_key: str | None = None,
) -> bool:
    """把版本与所属文档一起迁到终态（条件更新；仅 `UPLOADED` 可写）。

    返回:
        True=已完成迁移；False=版本已不在 `UPLOADED`（重复投递）或状态不一致。

    异常:
        InvalidStatusTransition: 状态机拒绝迁移（编程错误，向上抛出让 Arq 标记失败）。
    """

    async with db_session(tenant_id=tenant_id) as session:
        version = await _version_repository(session, tenant_id).get(
            version_id, with_for_update=True
        )
        if version is None or version.status != KB_STATUS_UPLOADED:
            return False
        document_repository: TenantScopedRepository[KbDocument] = TenantScopedRepository(
            session,
            KbDocument,
            tenant_id,
            scope=ScopeBypass(reason="扫描收口：文档状态与当前版本保持一致"),
        )
        document = await document_repository.get(version.document_id, with_for_update=True)
        if document is None or document.status != KB_STATUS_UPLOADED:
            logger.error(
                "知识库扫描收口跳过：版本 %s 与文档状态不一致（version=UPLOADED, document=%s）",
                version_id,
                None if document is None else document.status,
            )
            return False

        assert_transition(version.status, status)
        version.status = status
        version.scan_attempts += 1
        version.scan_engine = engine
        version.scan_signature = signature
        version.scan_error_code = error_code
        version.scanned_at = datetime.now(UTC)
        if source_key is not None:
            version.source_object_key = source_key
        document.status = status
        await session.flush()
        await append_audit(
            session,
            action=action,
            resource_type="kb_document_version",
            resource_id=str(version_id),
            actor_realm="system",
            actor_id=None,
            tenant_id=tenant_id,
            result="failure" if status == KB_STATUS_SCAN_FAILED else "success",
            metadata=metadata,
            request_id=None,
        )
        return True


async def scan_kb_version(ctx: dict[str, Any], tenant_id: str, version_id: str) -> dict[str, str]:
    """扫描单个文档版本（Arq 入口；测试可直接调用）。

    参数:
        ctx: Arq 任务上下文（未使用）。
        tenant_id: 版本所属租户（字符串形式的 UUID）。
        version_id: 文档版本 id（字符串形式的 UUID）。

    返回值:
        `{"status": <结论>}`；结论 ∈ `not_found` / `skipped` / `retry_pending` /
        `hash_mismatch` / `infected` / `passed` / `degraded`。

    异常:
        不向 Arq 抛业务异常；数据库等基础设施异常向上抛出由 Arq 重试
        （状态守卫保证重复执行安全）。
    """

    tenant_uuid = UUID(str(tenant_id))
    version_uuid = UUID(str(version_id))
    settings = get_settings()

    async with db_session(tenant_id=tenant_uuid) as session:
        version = await _load_version(session, tenant_uuid, version_uuid)
        if version is None:
            logger.warning(
                "知识库扫描：版本不存在（tenant=%s version=%s）", tenant_uuid, version_uuid
            )
            return {"status": "not_found"}
        if version.status != KB_STATUS_UPLOADED:
            return {"status": "skipped"}
        object_key = version.source_object_key
        expected_hash = version.source_hash

    store = build_object_store(settings)
    try:
        data = await store.get(object_key)
    except Exception as exc:
        await _mark_attempt(
            tenant_uuid, version_uuid, error_code="OBJECT_UNAVAILABLE", engine=None
        )
        logger.warning("知识库扫描对象读取失败（保持 UPLOADED 待补投）：%s", type(exc).__name__)
        return {"status": "retry_pending"}

    if hashlib.sha256(data).hexdigest() != expected_hash:
        changed = await _finish(
            tenant_uuid,
            version_uuid,
            status=KB_STATUS_SCAN_FAILED,
            engine=None,
            error_code="HASH_MISMATCH",
            action="school.kb.scan.failed",
            metadata={"reason": "hash_mismatch"},
        )
        logger.error("知识库扫描完整性校验失败（版本 %s 对象哈希不符）", version_uuid)
        return {"status": "hash_mismatch" if changed else "skipped"}

    try:
        result = await scan_bytes(
            data,
            host=settings.clamav_host,
            port=settings.clamav_port,
            timeout_seconds=settings.clamav_timeout_seconds,
        )
    except ClamAVError as exc:
        if settings.clamav_fail_closed:
            await _mark_attempt(
                tenant_uuid, version_uuid, error_code=exc.code, engine="clamav"
            )
            logger.warning(
                "知识库扫描暂不可用（保持 UPLOADED 待补投）：%s", type(exc).__name__
            )
            return {"status": "retry_pending"}
        # 仅开发/测试可达（Settings 校验生产必须 fail-closed）：降级放行并留痕。
        changed = await _finish(
            tenant_uuid,
            version_uuid,
            status=KB_STATUS_PENDING_APPROVAL,
            engine="dev-degraded",
            error_code=exc.code,
            action="school.kb.scan.passed",
            metadata={"engine": "dev-degraded", "degraded_code": exc.code},
        )
        logger.warning("知识库扫描降级放行（dev-degraded）：%s", exc.code)
        return {"status": "degraded" if changed else "skipped"}

    if result.infected:
        quarantine_key = _quarantine_key(object_key)
        try:
            await store.copy(object_key, quarantine_key)
        except Exception as exc:
            await _mark_attempt(
                tenant_uuid, version_uuid, error_code="QUARANTINE_FAILED", engine="clamav"
            )
            logger.warning(
                "知识库感染对象隔离失败（保持 UPLOADED 待补投）：%s", type(exc).__name__
            )
            return {"status": "retry_pending"}
        # 先写终态再删原件：若进程在两步之间崩溃，原件仍在（多留一个对象），
        # 但版本已是 SCAN_FAILED 且指向隔离副本——不会卡在「对象已删、状态未写」的
        # 无限重试里。
        changed = await _finish(
            tenant_uuid,
            version_uuid,
            status=KB_STATUS_SCAN_FAILED,
            engine="clamav",
            signature=result.signature,
            action="school.kb.scan.infected",
            metadata={"engine": "clamav", "signature": result.signature},
            source_key=quarantine_key,
        )
        try:
            await store.delete(object_key)
        except Exception as exc:
            logger.warning(
                "知识库感染对象原件删除失败（隔离副本已在 %s）：%s",
                quarantine_key,
                type(exc).__name__,
            )
        logger.warning("知识库扫描检出感染（版本 %s 已隔离）", version_uuid)
        return {"status": "infected" if changed else "skipped"}

    changed = await _finish(
        tenant_uuid,
        version_uuid,
        status=KB_STATUS_PENDING_APPROVAL,
        engine="clamav",
        action="school.kb.scan.passed",
        metadata={"engine": "clamav"},
    )
    return {"status": "passed" if changed else "skipped"}


async def retry_pending_kb_scans(ctx: dict[str, Any]) -> dict[str, int]:
    """看门狗：给滞留 `UPLOADED` 的版本补投扫描任务（cron 每 5 分钟）。

    租户表带 RLS：先用平台连接列出启用租户，再逐租户在租户上下文内查询
    （与 `fail_stale_import_jobs` 同模式）。

    参数:
        ctx: Arq 任务上下文；`ctx["redis"]` 存在时复用，否则临时建生产者池。

    返回值:
        `{"queued": 本轮成功入队条数}`（同 job_id 已存在时 arq 返回 None，不计入）。
    """

    from sqlalchemy import select

    from app.db.models.tenant import Tenant

    cutoff = datetime.now(UTC) - timedelta(minutes=STALE_UPLOADED_MINUTES)
    pool = ctx.get("redis")
    owned_pool = None
    if pool is None:
        from app.workers.settings import create_producer_pool

        owned_pool = await create_producer_pool()
        pool = owned_pool

    queued = 0
    try:
        async with db_session(platform=True) as session:
            tenant_ids = (
                (await session.execute(select(Tenant.id).where(Tenant.status == "active")))
                .scalars()
                .all()
            )
        for tenant_id in tenant_ids:
            async with db_session(tenant_id=tenant_id) as session:
                repository: TenantScopedRepository[KbDocumentVersion] = TenantScopedRepository(
                    session,
                    KbDocumentVersion,
                    tenant_id,
                    scope=ScopeBypass(reason="扫描看门狗：系统执行者补投全租户滞留版本"),
                )
                stmt = (
                    repository.base_query()
                    .where(
                        KbDocumentVersion.status == KB_STATUS_UPLOADED,
                        KbDocumentVersion.updated_at < cutoff,
                    )
                    .order_by(KbDocumentVersion.updated_at.asc())
                    .limit(WATCHDOG_BATCH_SIZE)
                )
                pending = [
                    (row.id, row.scan_attempts)
                    for row in (await session.execute(stmt)).scalars().all()
                ]
            for version_id, attempt in pending:
                job = await pool.enqueue_job(
                    "scan_kb_version",
                    str(tenant_id),
                    str(version_id),
                    _job_id=scan_job_id(version_id, attempt),
                )
                if job is not None:
                    queued += 1
    finally:
        if owned_pool is not None:
            await owned_pool.aclose()

    if queued:
        logger.info("知识库扫描看门狗补投 %d 个滞留版本", queued)
    return {"queued": queued}
