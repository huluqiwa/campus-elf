"""审计事件写入（M1-A 表结构；M1-F 哈希链由数据库触发器计算）。

M1-F 起：`prev_hash` / `hash` / `hash_version` 由 `audit_logs_compute_hash()` 触发器
在 INSERT 前自动填充（应用层不再硬编码 None/0）。
密钥由 `db_session()` 在事务内注入 GUC `app.audit_hash_key`，触发器读取。
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import record_audit_write_failure
from app.db.models.audit import AuditLog


async def append_audit(
    session: AsyncSession,
    *,
    action: str,
    resource_type: str,
    actor_realm: str,
    resource_id: str | None = None,
    actor_id: UUID | None = None,
    tenant_id: UUID | None = None,
    result: str = "success",
    metadata: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> AuditLog:
    """写入一条审计事件（仅追加；哈希链由数据库触发器计算）。

    参数:
        session: 已处于事务中的会话（平台路径需已设置 `app.platform_context`）。
        action: 动作标识（如 `tenant.approve`）。
        resource_type: 资源类型（如 `tenant`）。
        actor_realm: `platform` / `tenant` / `system`。
        resource_id: 资源 id（字符串）。
        actor_id: 操作者 id（平台用户/租户用户；系统动作为 None）。
        tenant_id: 租户 id（平台级事件为 None）。
        result: `success` / `failure` / `denied`。
        metadata: 已脱敏的附加信息（禁止放入密码/令牌）。
        request_id: HTTP 请求 id（便于关联）。

    返回值:
        AuditLog 实例。

    异常:
        无（数据库约束/RLS 错误向上抛出）。
    """

    event = AuditLog(
        event_id=uuid4(),
        tenant_id=tenant_id,
        actor_realm=actor_realm,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result=result,
        request_id=request_id,
        metadata_json=metadata,
        # prev_hash / hash / hash_version 由数据库触发器在 INSERT 前自动填充（M1-F）
        created_at=datetime.now(UTC),
    )
    session.add(event)
    try:
        await session.flush()
    except Exception:
        # 审计写失败意味着"该留痕的操作没有留痕"，必须让运维从指标上看得见；
        # 计数后原样抛出，由调用方决定是否降级（如拒绝审计的 best-effort 分支）。
        record_audit_write_failure(action)
        raise
    return event
