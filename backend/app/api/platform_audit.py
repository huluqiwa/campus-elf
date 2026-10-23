"""平台审计查询接口（M1-F，TD-068）。

只读：平台 realm + `platform.audit.view` 权限。
查询参数全部可选；cursor 分页（`before_sequence` 向前翻页）。
跨租审计查询**必须**走平台 realm——租户 realm 无此接口，尝试访问 403 并审计。
"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import text

from app.auth.principal import Principal, require_permission
from app.db.session import db_session

router = APIRouter(prefix="/platform", tags=["platform"])

ViewerDep = require_permission("platform.audit.view", realm="platform")

# 查询结果字段（与 audit_logs 列一一对应；metadata_json 已在写入时脱敏）
_AUDIT_FIELDS = """
    sequence, event_id::text, tenant_id::text, actor_realm, actor_id::text,
    action, resource_type, resource_id, result, ip, user_agent, request_id,
    prev_hash, hash, hash_version, anchor_ref, created_at::text,
    metadata_json::text
"""

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


class AuditLogItem(BaseModel):
    """审计记录项。"""

    sequence: int
    event_id: str
    tenant_id: str | None
    actor_realm: str
    actor_id: str | None
    action: str
    resource_type: str
    resource_id: str | None
    result: str
    ip: str | None
    user_agent: str | None
    request_id: str | None
    prev_hash: str | None
    hash: str | None
    hash_version: int
    anchor_ref: str | None
    created_at: str
    metadata_json: str | None


class AuditLogListResponse(BaseModel):
    """审计查询响应。"""

    items: list[AuditLogItem]
    total: int
    next_cursor: int | None


def _parse_row(row: Any) -> AuditLogItem:
    """把 RowMapping 映射为响应模型（.mappings().all() 的结果是 dict-like）。"""

    return AuditLogItem(
        sequence=row["sequence"],
        event_id=row["event_id"],
        tenant_id=row["tenant_id"],
        actor_realm=row["actor_realm"],
        actor_id=row["actor_id"],
        action=row["action"],
        resource_type=row["resource_type"],
        resource_id=row["resource_id"],
        result=row["result"],
        ip=row["ip"],
        user_agent=row["user_agent"],
        request_id=row["request_id"],
        prev_hash=row["prev_hash"],
        hash=row["hash"],
        hash_version=row["hash_version"],
        anchor_ref=row["anchor_ref"],
        created_at=row["created_at"],
        metadata_json=row["metadata_json"],
    )


@router.get("/audit-logs", response_model=AuditLogListResponse)
async def list_audit_logs(
    viewer: Annotated[Principal, ViewerDep],
    action: Annotated[str | None, Query(max_length=64)] = None,
    actor_id: UUID | None = None,
    tenant_id: UUID | None = None,
    result: Literal["success", "failure", "denied"] | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    before_sequence: int | None = Query(default=None, ge=1),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> AuditLogListResponse:
    """平台审计查询（只读；cursor 分页，新→旧）。"""

    conditions: list[str] = []
    params: dict[str, object] = {"limit": limit + 1}

    if action:
        conditions.append("action = :action")
        params["action"] = action
    if actor_id:
        conditions.append("actor_id = :actor_id")
        params["actor_id"] = actor_id
    if tenant_id:
        conditions.append("tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
    if result:
        conditions.append("result = :result")
        params["result"] = result
    if start_time:
        conditions.append("created_at >= :start_time")
        params["start_time"] = start_time
    if end_time:
        conditions.append("created_at <= :end_time")
        params["end_time"] = end_time
    if before_sequence:
        conditions.append("sequence < :before_sequence")
        params["before_sequence"] = before_sequence

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    async with db_session(platform=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        f"SELECT {_AUDIT_FIELDS} FROM audit_logs "
                        f"{where_clause} ORDER BY sequence DESC LIMIT :limit"  # nosec B608 - 条件名硬编码，值全走绑定参数
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        total = await session.scalar(
            text(
                f"SELECT count(*) FROM audit_logs {where_clause}"  # nosec B608 - 条件名硬编码，值全走绑定参数
            ),
            params,
        )

    items = []
    for row in rows[:limit]:
        # mappings().all() 返回 RowMapping，需要包装成有 _mapping 属性的对象
        items.append(_parse_row(row))

    next_cursor = rows[limit]["sequence"] if len(rows) > limit else None

    return AuditLogListResponse(
        items=items,
        total=total or 0,
        next_cursor=next_cursor,
    )
