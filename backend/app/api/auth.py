"""认证接口（M1-C）：三端登录、刷新、登出、首登改密、当前身份。

- `POST /auth/login`：`realm=tenant` 用"学校编号 + 学号/工号 + 密码"；`realm=platform` 用"账号 + 密码"。
- `POST /auth/refresh`：刷新令牌轮换；检测到重用 → 401 并撤销整个家族。
- `POST /auth/logout`：撤销刷新令牌家族 + 把当前访问令牌 jti 加入撤销集合（立即失效）。
- `POST /auth/change-password`：首登改密也走这里；成功后返回新令牌（旧令牌立即失效）。
- `GET /auth/me`：返回主体与权限（前端据此渲染菜单；服务端仍逐请求校验）。

令牌明文只在响应体出现，不写日志/审计（`docs/AGENTS.md` 安全红线）。
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from app.auth import revocation, service, tokens
from app.auth.principal import CurrentPrincipal
from app.auth.service import AuthError

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """登录请求。"""

    realm: Literal["tenant", "platform"]
    # 租户 realm：学校编号 + 学号/工号
    school_code: str | None = Field(default=None, max_length=32)
    login_id: str | None = Field(default=None, max_length=254)
    # 平台 realm：平台账号
    account: str | None = Field(default=None, max_length=64)
    password: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _check_fields(self) -> "LoginRequest":
        """按 realm 校验必填字段。"""

        if self.realm == "tenant":
            if not (self.school_code or "").strip() or not (self.login_id or "").strip():
                raise ValueError("租户登录需要 school_code 与 login_id")
        elif not (self.account or "").strip():
            raise ValueError("平台登录需要 account")
        return self


class TokenResponse(BaseModel):
    """令牌响应（明文令牌只在此处出现）。

    含 `tenant_id`/`roles`：spec AUTH-01 要求登录返回租户与角色（前端据此渲染，
    服务端仍逐请求校验权限）。
    """

    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    realm: Literal["tenant", "platform"]
    must_change_password: bool
    tenant_id: str | None = None
    roles: list[str] = []


class RefreshRequest(BaseModel):
    """刷新请求。"""

    refresh_token: str = Field(min_length=16, max_length=512)


class LogoutRequest(BaseModel):
    """登出请求（可选携带刷新令牌以撤销整个家族）。"""

    refresh_token: str | None = Field(default=None, max_length=512)


class ChangePasswordRequest(BaseModel):
    """改密请求。"""

    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class MeResponse(BaseModel):
    """当前主体。"""

    user_id: str
    realm: Literal["tenant", "platform"]
    tenant_id: str | None
    must_change_password: bool
    roles: list[str]
    permissions: list[str]


class OkResponse(BaseModel):
    """通用成功响应。"""

    status: Literal["ok"] = "ok"


def _http_error(exc: AuthError) -> HTTPException:
    """业务错误 → HTTP 错误（401 带 WWW-Authenticate）。"""

    headers = {"WWW-Authenticate": "Bearer"} if exc.http_status == 401 else None
    return HTTPException(status_code=exc.http_status, detail=exc.message, headers=headers)


def _token_response(pair: service.TokenPair) -> TokenResponse:
    """TokenPair → 响应模型。"""

    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
        realm=pair.realm,
        must_change_password=pair.must_change_password,
        tenant_id=str(pair.tenant_id) if pair.tenant_id else None,
        roles=sorted(pair.roles),
    )


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, request: Request) -> TokenResponse:
    """三端登录。

    异常:
        HTTPException: 401 凭证错误（文案统一，不区分账号/学校是否存在）。
    """

    request_id = getattr(request.state, "request_id", None)
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    try:
        if payload.realm == "tenant":
            pair = await service.login_tenant(
                school_code=payload.school_code or "",
                login_id=payload.login_id or "",
                password=payload.password,
                ip=client_ip,
                user_agent=user_agent,
                request_id=request_id,
            )
        else:
            pair = await service.login_platform(
                account=payload.account or "",
                password=payload.password,
                ip=client_ip,
                user_agent=user_agent,
                request_id=request_id,
            )
    except AuthError as exc:
        raise _http_error(exc) from exc
    return _token_response(pair)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, request: Request) -> TokenResponse:
    """刷新访问令牌（轮换；重用检测）。"""

    try:
        pair = await service.refresh_tokens(
            refresh_token=payload.refresh_token,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
    except AuthError as exc:
        raise _http_error(exc) from exc
    return _token_response(pair)


@router.post("/logout", response_model=OkResponse)
async def logout(
    principal: CurrentPrincipal,
    request: Request,
    payload: LogoutRequest | None = None,
) -> OkResponse:
    """登出：撤销刷新令牌家族并立即失效当前访问令牌。"""

    request_id = getattr(request.state, "request_id", None)
    if payload is not None and payload.refresh_token:
        # 校验刷新令牌哈希后撤销家族；令牌不匹配按未知处理（不泄露、不误撤销）
        await service.revoke_refresh_family(
            refresh_token=payload.refresh_token,
            actor_user_id=principal.user_id,
            actor_realm=principal.realm,
            request_id=request_id,
        )
    await revocation.revoke(
        str(principal.jti), principal.expires_at, realm=principal.realm
    )
    await service.record_logout(
        actor_user_id=principal.user_id,
        actor_realm=principal.realm,
        tenant_id=principal.tenant_id,
        request_id=request_id,
        ip_hash=tokens.hash_client_fingerprint(
            request.client.host if request.client else None
        ),
        ua_hash=tokens.hash_client_fingerprint(request.headers.get("user-agent")),
    )
    return OkResponse()


@router.post("/change-password", response_model=TokenResponse)
async def change_password(
    payload: ChangePasswordRequest, principal: CurrentPrincipal, request: Request
) -> TokenResponse:
    """修改密码（首登改密也走这里）；成功后返回新令牌。"""

    try:
        pair = await service.change_password(
            principal_user_id=principal.user_id,
            realm=principal.realm,
            tenant_id=principal.tenant_id,
            current_password=payload.current_password,
            new_password=payload.new_password,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
    except AuthError as exc:
        raise _http_error(exc) from exc
    return _token_response(pair)


@router.get("/me", response_model=MeResponse)
async def me(principal: CurrentPrincipal) -> MeResponse:
    """返回当前主体与权限集合。"""

    return MeResponse(
        user_id=str(principal.user_id),
        realm=principal.realm,
        tenant_id=str(principal.tenant_id) if principal.tenant_id else None,
        must_change_password=principal.must_change_password,
        roles=sorted(principal.roles),
        permissions=sorted(principal.permissions),
    )
