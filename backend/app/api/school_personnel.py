"""校方侧人员档案只读接口（M1-E2 RBAC-02）。

范围过滤发生在服务/仓储层（`app/personnel/service.py`），本模块只做鉴权、参数与响应形状：

- 列表：看不到的人不出现（静默过滤）；
- 详情：越权与不存在统一 404，越权在服务层补一条 `data_scope.denied` 审计。
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.auth.principal import Principal, require_permission
from app.personnel import service
from app.personnel.service import PersonnelError, PersonnelItem

router = APIRouter(prefix="/school", tags=["school"])

ViewerDep = require_permission("school.personnel.view", realm="tenant")


class PersonResponse(BaseModel):
    """人员档案项（简报 §2-C/口径 ⑥ 固定形状：掩码邮箱，不含 `login_id`/明文邮箱/哈希/MFA 字段）。"""

    id: UUID
    user_type: str
    display_name: str | None
    status: str
    last_login_at: datetime | None
    email_masked: str | None
    id_card_mask: str | None = None


def _to_response(item: PersonnelItem) -> PersonResponse:
    return PersonResponse(
        id=item.id,
        user_type=item.user_type,
        display_name=item.display_name,
        status=item.status,
        last_login_at=item.last_login_at,
        email_masked=item.email_masked,
        id_card_mask=item.id_card_mask,
    )


def _http_error(exc: PersonnelError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=exc.message)


@router.get("/personnel", response_model=list[PersonResponse])
async def list_personnel(
    viewer: Annotated[Principal, ViewerDep],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    user_type: Annotated[str | None, Query(max_length=16)] = None,
) -> list[PersonResponse]:
    """本校人员档案列表（按调用者的数据范围过滤）。

    异常:
        HTTPException: 403（主体 realm 不符）。
    """

    try:
        items = await service.list_personnel(
            viewer, limit=limit, offset=offset, user_type=user_type
        )
    except PersonnelError as exc:
        raise _http_error(exc) from exc
    return [_to_response(item) for item in items]


@router.get("/personnel/{personnel_id}", response_model=PersonResponse)
async def get_personnel(
    personnel_id: UUID,
    viewer: Annotated[Principal, ViewerDep],
    request: Request,
) -> PersonResponse:
    """单个人员档案；不在可见范围内与不存在都是 404。

    异常:
        HTTPException: 404（不可见或不存在）、403（主体 realm 不符）。
    """

    try:
        item = await service.get_personnel(
            viewer, personnel_id, request_id=getattr(request.state, "request_id", None)
        )
    except PersonnelError as exc:
        raise _http_error(exc) from exc
    return _to_response(item)
