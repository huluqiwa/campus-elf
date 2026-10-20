"""审计事件写入（M1-A 表结构；M1-F 补哈希链）。

M1-A 决策：`prev_hash/hash` 可空、`hash_version=0` 表示"未入链"。因此这里只写事件本身，
不伪造哈希。M1-F 实现链计算与校验后会替换本写入器（TD-068）。
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

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
    """写入一条审计事件（仅追加；不计算哈希链）。

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
        prev_hash=None,
        hash=None,
        hash_version=0,
        created_at=datetime.now(UTC),
    )
    session.add(event)
    await session.flush()
    return event
