"""平台侧策略开关与跨租只读接口（M1-E2 口径 ②⑧）。

- `GET/PUT /platform/settings`：`platform.config.view` / `platform.config.edit`；
  改值必须带理由，并与审计写在同一事务里（没有审计的改动=不知道是谁开的会）；
- `POST /platform/cross-tenant/personnel/read`：`platform.cross_tenant.read`；
  只读、必填理由、强制审计，一次只允许一个目标租户。
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.audit.writer import append_audit
from app.auth.principal import Principal, require_permission
from app.db.session import db_session
from app.personnel import cross_tenant
from app.personnel.cross_tenant import CrossTenantError
from app.settings import store

router = APIRouter(prefix="/platform", tags=["platform"])

ConfigViewerDep = require_permission("platform.config.view", realm="platform")
ConfigEditorDep = require_permission("platform.config.edit", realm="platform")
CrossTenantReadDep = require_permission("platform.cross_tenant.read", realm="platform")


class SettingResponse(BaseModel):
    """一个策略开关的当前生效值。"""

    key: str
    value: bool
    is_library_value: bool
    description: str
    default_value: bool


class SettingWritePayload(BaseModel):
    """改值请求：理由必填（空串在数据库 CHECK 之前就被 422 挡掉）。"""

    value: bool
    reason: str = Field(min_length=1, max_length=500)


class CrossTenantReadPayload(BaseModel):
    """跨租只读请求：目标租户 + 必填理由。"""

    tenant_id: UUID
    reason: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class CrossTenantPersonResponse(BaseModel):
    """跨租读取返回的人员项（与校方侧同一脱敏形状：不含 `login_id`/明文邮箱/哈希/MFA 字段）。"""

    id: UUID
    user_type: str
    display_name: str | None
    status: str
    last_login_at: datetime | None
    email_masked: str | None


class CrossTenantReadResponse(BaseModel):
    """跨租读取结果。"""

    tenant_id: UUID
    items: list[CrossTenantPersonResponse]


def _setting_response(snapshot: store.SettingSnapshot) -> SettingResponse:
    definition = store.SETTING_DEFS[snapshot.key]
    return SettingResponse(
        key=snapshot.key,
        value=snapshot.value,
        is_library_value=snapshot.is_library_value,
        description=definition.description,
        default_value=definition.default,
    )


@router.get("/settings", response_model=list[SettingResponse])
async def list_platform_settings(
    viewer: Annotated[Principal, ConfigViewerDep],
) -> list[SettingResponse]:
    """列出全部策略开关的当前生效值（库里没有的键回出厂默认）。"""

    async with db_session(platform=True) as session:
        snapshots = await store.list_settings(session)
    return [_setting_response(snapshot) for snapshot in snapshots]


@router.put("/settings/{key}", response_model=SettingResponse)
async def put_platform_setting(
    key: str,
    payload: SettingWritePayload,
    request: Request,
    editor: Annotated[Principal, ConfigEditorDep],
) -> SettingResponse:
    """改一个策略开关（理由必填 + 同事务审计；未知键 404）。

    异常:
        HTTPException: 404（键未声明）、422（值或理由非法）。
    """

    if not store.is_known_key(key):
        raise HTTPException(status_code=404, detail="未声明的系统设置键")
    try:
        async with db_session(platform=True) as session:
            snapshot = await store.put_setting(
                session, key=key, value=payload.value, updated_by=editor.user_id,
                reason=payload.reason,
            )
            await append_audit(
                session,
                action="platform.setting.update",
                resource_type="system_setting",
                resource_id=key,
                actor_realm="platform",
                actor_id=editor.user_id,
                result="success",
                metadata={"value": snapshot.value, "reason": payload.reason.strip()},
                request_id=getattr(request.state, "request_id", None),
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _setting_response(snapshot)


@router.post("/cross-tenant/personnel/read", response_model=CrossTenantReadResponse)
async def read_cross_tenant_personnel(
    payload: CrossTenantReadPayload,
    request: Request,
    reader: Annotated[Principal, CrossTenantReadDep],
) -> CrossTenantReadResponse:
    """跨租只读一个学校的人员档案（必填理由；只读事务；强制审计）。

    异常:
        HTTPException: 403（非平台主体）、404（目标租户不存在）、422（理由非法）。
    """

    try:
        result = await cross_tenant.read_tenant_personnel(
            reader,
            tenant_id=payload.tenant_id,
            reason=payload.reason,
            limit=payload.limit,
            offset=payload.offset,
            request_id=getattr(request.state, "request_id", None),
        )
    except CrossTenantError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
    return CrossTenantReadResponse(
        tenant_id=result.tenant_id,
        items=[
            CrossTenantPersonResponse(
                id=item.id,
                user_type=item.user_type,
                display_name=item.display_name,
                status=item.status,
                last_login_at=item.last_login_at,
                email_masked=item.email_masked,
            )
            for item in result.items
        ],
    )
