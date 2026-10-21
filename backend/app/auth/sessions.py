"""登录设备（会话）管理：限额踢出、设备列表、单台下线、退出其它设备。

一个 family（`refresh_tokens.family_id` / `platform_refresh_tokens.family_id`）即一台设备。
- 超限时踢"最久未用"的 family，并写 `session_kicked`；
- 列表/下线只作用于本人；跨租/他人一律不可见（RLS + tenant_id + user_id 过滤）；
- 同一账号的登录/刷新/撤销/改密共享事务级 advisory lock，避免撤销与轮换竞态；
- Redis 会话标记在数据库事务**内**登记（随提交生效），撤销后删除，避免"提交后补写"
  把已踢会话复活；
- 审计与数据库撤销同事务；不记录明文 UA/IP。
"""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.writer import append_audit
from app.auth import revocation
from app.db.session import db_session

Realm = Literal["tenant", "platform"]

UNKNOWN_DEVICE = "未知设备"

# 按 realm 的字面量 SQL（表名不参与拼接）
_SQL: dict[Realm, dict[str, str]] = {
    "tenant": {
        "active_families": (
            "select * from ("
            "  select distinct on (family_id) family_id, "
            "    coalesce(device_label, '未知设备') as device_label, "
            "    last_seen_at, issued_at "
            "  from refresh_tokens "
            "  where tenant_id = :tid and user_id = :uid "
            "    and revoked_at is null and expires_at > now() "
            "  order by family_id, issued_at desc"
            ") latest order by last_seen_at desc, issued_at desc"
        ),
        "revoke_family": (
            "update refresh_tokens set revoked_at = now(), revoked_reason = :reason "
            "where tenant_id = :tid and family_id = :fid and user_id = :uid "
            "  and revoked_at is null "
            "returning family_id, coalesce(device_label, '未知设备') as device_label"
        ),
    },
    "platform": {
        "active_families": (
            "select * from ("
            "  select distinct on (family_id) family_id, "
            "    coalesce(device_label, '未知设备') as device_label, "
            "    last_seen_at, issued_at "
            "  from platform_refresh_tokens "
            "  where platform_user_id = :uid and revoked_at is null and expires_at > now() "
            "  order by family_id, issued_at desc"
            ") latest order by last_seen_at desc, issued_at desc"
        ),
        "revoke_family": (
            "update platform_refresh_tokens set revoked_at = now(), "
            "revoked_reason = :reason, updated_at = now() "
            "where family_id = :fid and platform_user_id = :uid and revoked_at is null "
            "returning family_id, coalesce(device_label, '未知设备') as device_label"
        ),
    },
}


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """设备列表项（不含明文 IP/UA）。"""

    session_id: UUID
    device_label: str
    last_seen_at: datetime
    current: bool


@dataclass(frozen=True, slots=True)
class _FamilyRow:
    """数据库中的活跃 family（每个 family 的最新一行）。"""

    family_id: UUID
    device_label: str
    last_seen_at: datetime
    issued_at: datetime


def device_label_from_user_agent(user_agent: str | None) -> str:
    """把 UA 解析为粗粒度"浏览器 / 操作系统"标签（不保留原文）。

    无法识别（包括空/非浏览器 UA）时返回"未知设备"。
    """

    if not user_agent:
        return UNKNOWN_DEVICE

    browser: str | None = None
    if "Edg/" in user_agent or "Edge/" in user_agent:
        browser = "Edge"
    elif "OPR/" in user_agent:
        browser = "Opera"
    elif "Chrome/" in user_agent or "CriOS/" in user_agent:
        browser = "Chrome"
    elif "Firefox/" in user_agent or "FxiOS/" in user_agent:
        browser = "Firefox"
    elif "Safari/" in user_agent:
        browser = "Safari"

    os_name: str | None = None
    if "Windows" in user_agent:
        os_name = "Windows"
    elif "Android" in user_agent:
        os_name = "Android"
    elif "iPhone" in user_agent or "iPad" in user_agent or "iPod" in user_agent:
        os_name = "iOS"
    elif "Macintosh" in user_agent or "Mac OS X" in user_agent:
        os_name = "macOS"
    elif "Linux" in user_agent:
        os_name = "Linux"

    if browser is None or os_name is None:
        return UNKNOWN_DEVICE
    return f"{browser} / {os_name}"[:64]


def session_scope(
    *, realm: Realm, tenant_id: UUID | None
) -> AbstractAsyncContextManager[AsyncSession]:
    """按 realm 返回新的数据库会话上下文（租户连接带租户上下文）。"""

    if realm == "tenant":
        if tenant_id is None:
            raise ValueError("租户会话操作必须提供 tenant_id")
        return db_session(tenant_id=tenant_id)
    return db_session(platform=True)


def _scope_params(realm: Realm, tenant_id: UUID | None) -> dict[str, UUID]:
    """租户查询必须携带 tenant_id（RLS 之外的第二道显式过滤）。"""

    if realm == "tenant":
        if tenant_id is None:
            raise ValueError("租户会话操作必须提供 tenant_id")
        return {"tid": tenant_id}
    return {}


async def acquire_user_lock(
    session: AsyncSession, *, realm: Realm, user_id: UUID
) -> None:
    """按账号获取事务级 advisory lock（同一账号的会话写操作串行化）。

    登录/刷新/撤销/改密共享同一把锁：撤销提交后，等待中的刷新必须重新读取数据库，
    从而看到已撤销的 family；锁随事务提交/回滚自动释放。
    """

    await session.execute(
        text("select pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"{realm}:session:{user_id}"},
    )


async def _active_family_rows(
    session: AsyncSession, *, realm: Realm, tenant_id: UUID | None, user_id: UUID
) -> list[_FamilyRow]:
    """读取本人当前活跃的 family（每个 family 取最新一行，按最近使用倒序）。"""

    params = {"uid": user_id, **_scope_params(realm, tenant_id)}
    rows = (
        await session.execute(text(_SQL[realm]["active_families"]), params)
    ).mappings().all()
    return [
        _FamilyRow(
            family_id=row["family_id"],
            device_label=str(row["device_label"]),
            last_seen_at=row["last_seen_at"],
            issued_at=row["issued_at"],
        )
        for row in rows
    ]


async def enforce_limit(
    session: AsyncSession,
    *,
    realm: Realm,
    tenant_id: UUID | None,
    user_id: UUID,
    limit: int,
    request_id: str | None = None,
) -> list[UUID]:
    """登录前执行设备上限：踢最久未用，返回被踢 family 的 sid。

    调用方必须在**同一登录事务**内调用（审计与撤销同事务），并在提交后删除
    Redis 会话标记。调用方应已在读取账号状态前取得 `acquire_user_lock`；
    这里再次获取（同一事务内可重入）以保证函数独立使用时也安全。
    """

    await acquire_user_lock(session, realm=realm, user_id=user_id)
    rows = await _active_family_rows(
        session, realm=realm, tenant_id=tenant_id, user_id=user_id
    )
    overflow = len(rows) - limit + 1
    if overflow <= 0:
        return []

    # 列表按最近使用倒序；踢出最旧的 overflow 台
    oldest = sorted(rows, key=lambda row: (row.last_seen_at, row.issued_at))[:overflow]
    kicked: list[UUID] = []
    for row in oldest:
        await session.execute(
            text(_SQL[realm]["revoke_family"]),
            {
                "fid": row.family_id,
                "uid": user_id,
                "reason": "session_kicked",
                **_scope_params(realm, tenant_id),
            },
        )
        kicked.append(row.family_id)
        await append_audit(
            session,
            action="auth.session.kicked",
            resource_type="auth",
            resource_id=str(row.family_id),
            actor_realm=realm,
            actor_id=user_id,
            tenant_id=tenant_id if realm == "tenant" else None,
            metadata={
                "family_id": str(row.family_id),
                "device_label": row.device_label,
                "reason": "limit_exceeded",
            },
            request_id=request_id,
        )
    return kicked


async def list_sessions(
    *,
    realm: Realm,
    tenant_id: UUID | None,
    user_id: UUID,
    current_session_id: UUID,
) -> list[SessionInfo]:
    """列出本人仍有效的设备（数据库活跃 + Redis 标记存在），按最近使用倒序。"""

    async with session_scope(realm=realm, tenant_id=tenant_id) as session:
        rows = await _active_family_rows(
            session, realm=realm, tenant_id=tenant_id, user_id=user_id
        )

    active_ids = await revocation.filter_active(
        [row.family_id for row in rows], realm=realm
    )
    return [
        SessionInfo(
            session_id=row.family_id,
            device_label=row.device_label,
            last_seen_at=row.last_seen_at,
            current=row.family_id == current_session_id,
        )
        for row in rows
        if row.family_id in active_ids
    ]


async def revoke_session(
    *,
    realm: Realm,
    tenant_id: UUID | None,
    user_id: UUID,
    session_id: UUID,
    current_session_id: UUID,
    request_id: str | None = None,
) -> bool:
    """下线本人的一台设备；不存在/非本人返回 False（接口层统一 404）。"""

    async with session_scope(realm=realm, tenant_id=tenant_id) as session:
        await acquire_user_lock(session, realm=realm, user_id=user_id)
        row = (
            await session.execute(
                text(_SQL[realm]["revoke_family"]),
                {
                    "fid": session_id,
                    "uid": user_id,
                    "reason": "session_revoked",
                    **_scope_params(realm, tenant_id),
                },
            )
        ).mappings().first()
        if row is None:
            # 跨用户/跨租或不存在：响应统一 404，但安全事件必须可追溯（test-plan A23-7）
            await append_audit(
                session,
                action="auth.session.revoke_denied",
                resource_type="auth",
                resource_id=str(session_id),
                actor_realm=realm,
                actor_id=user_id,
                tenant_id=tenant_id if realm == "tenant" else None,
                result="denied",
                metadata={
                    "attempted_session_id": str(session_id),
                    "reason": "not_found_or_not_owner",
                },
                request_id=request_id,
            )
            return False
        await append_audit(
            session,
            action="auth.session.revoked",
            resource_type="auth",
            resource_id=str(session_id),
            actor_realm=realm,
            actor_id=user_id,
            tenant_id=tenant_id if realm == "tenant" else None,
            metadata={
                "family_id": str(session_id),
                "device_label": str(row["device_label"]),
                "current": session_id == current_session_id,
            },
            request_id=request_id,
        )

    await revocation.delete_session(session_id, realm=realm)
    return True


async def revoke_others(
    *,
    realm: Realm,
    tenant_id: UUID | None,
    user_id: UUID,
    current_session_id: UUID,
    request_id: str | None = None,
) -> int:
    """下线除当前设备外的全部设备，返回实际撤销数量。

    空操作也写审计（用户主动发起的会话管理动作需要留痕）；计数以条件更新的
    `returning` 为准，避免并发撤销时虚报。
    """

    async with session_scope(realm=realm, tenant_id=tenant_id) as session:
        await acquire_user_lock(session, realm=realm, user_id=user_id)
        rows = await _active_family_rows(
            session, realm=realm, tenant_id=tenant_id, user_id=user_id
        )
        targets = [row for row in rows if row.family_id != current_session_id]
        revoked: list[UUID] = []
        for row in targets:
            updated = (
                await session.execute(
                    text(_SQL[realm]["revoke_family"]),
                    {
                        "fid": row.family_id,
                        "uid": user_id,
                        "reason": "session_revoked",
                        **_scope_params(realm, tenant_id),
                    },
                )
            ).first()
            if updated is not None:
                revoked.append(row.family_id)
        await append_audit(
            session,
            action="auth.session.revoked_others",
            resource_type="auth",
            resource_id=str(current_session_id),
            actor_realm=realm,
            actor_id=user_id,
            tenant_id=tenant_id if realm == "tenant" else None,
            metadata={
                "current_session_id": str(current_session_id),
                "revoked_count": len(revoked),
            },
            request_id=request_id,
        )

    await revocation.delete_sessions(revoked, realm=realm)
    return len(revoked)
