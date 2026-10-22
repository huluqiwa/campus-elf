"""认证接口（M1-C/M1-D2）：三端登录、刷新、登出、首登改密、MFA、当前身份。

- `POST /auth/login`：`realm=tenant` 用"学校编号 + 学号/工号 + 密码"；`realm=platform` 用"账号 + 密码"。
- `POST /auth/mfa/verify`：登录第二步（TOTP/恢复码）。
- `POST /auth/mfa/enroll|activate|disable`：绑定、激活、关闭 TOTP；绑定/激活/关闭都会轮换会话。
- `POST /auth/refresh`：刷新令牌轮换；检测到重用 → 401 并撤销整个家族。
- `POST /auth/logout`：撤销刷新令牌家族 + 把当前访问令牌 jti 加入撤销集合（立即失效）。
- `POST /auth/change-password`：首登改密也走这里；成功后返回新令牌（旧令牌立即失效）。
- `GET /auth/me`：返回主体与权限（前端据此渲染菜单；服务端仍逐请求校验）。

令牌明文只在响应体出现，不写日志/审计（`AGENTS.md` 安全红线）。
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, Field, model_validator

from app.api.client_ip import resolve_client_ip
from app.auth import revocation, service, sessions, tokens
from app.auth.mfa import decode_mfa_token
from app.auth.principal import CurrentPrincipal, OptionalPrincipal, get_current_principal
from app.auth.service import AuthError
from app.auth.tokens import TokenError
from app.config import get_settings

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
    # 仅开发/测试回显（AUTH_REFRESH_TOKEN_IN_BODY）；staging/production 只走 HttpOnly Cookie
    refresh_token: str | None = None
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    realm: Literal["tenant", "platform"]
    must_change_password: bool
    tenant_id: str | None = None
    roles: list[str] = []


class LoginAuthenticatedResponse(TokenResponse):
    """登录完成（未开启 MFA 或已完成第二步）。"""

    status: Literal["authenticated"] = "authenticated"


class MfaRequiredResponse(BaseModel):
    """已绑定 MFA：进入第二步（不签发会话、不设 Cookie）。"""

    status: Literal["mfa_required"] = "mfa_required"
    mfa_token: str
    expires_in: int
    methods: list[Literal["totp"]] = ["totp"]


class MfaEnrollmentRequiredResponse(BaseModel):
    """平台强制 MFA 且未绑定：返回受限绑定令牌（不签发会话、不设 Cookie）。"""

    status: Literal["mfa_enrollment_required"] = "mfa_enrollment_required"
    enroll_token: str
    expires_in: int


LoginResponse = Annotated[
    LoginAuthenticatedResponse | MfaRequiredResponse | MfaEnrollmentRequiredResponse,
    Field(discriminator="status"),
]


class MfaVerifyRequest(BaseModel):
    """MFA 第二步校验（TOTP 6 位数字或一次性恢复码）。"""

    mfa_token: str = Field(min_length=8, max_length=4096)
    code: str = Field(min_length=1, max_length=32)


class MfaEnrollRequest(BaseModel):
    """开始绑定 TOTP；使用访问令牌时必须提供当前密码。"""

    current_password: str | None = Field(default=None, min_length=1, max_length=128)


class MfaEnrollResponse(BaseModel):
    """绑定信息（secret 只在此响应出现，不写日志/审计）。"""

    secret: str
    otpauth_uri: str
    expires_in: int


class MfaActivateRequest(BaseModel):
    """激活 TOTP：提交 Authenticator 生成的验证码。"""

    code: str = Field(min_length=1, max_length=32)


class MfaActivateResponse(TokenResponse):
    """激活结果：新会话 + 一次性恢复码（仅此响应可见）。"""

    recovery_codes: list[str]


class MfaDisableRequest(BaseModel):
    """关闭 MFA：需要当前密码 + TOTP/恢复码。"""

    current_password: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=32)


class RefreshRequest(BaseModel):
    """刷新请求。

    `refresh_token` 可省略：浏览器端使用 HttpOnly Cookie，仅非浏览器客户端在 body 传值。
    """

    refresh_token: str | None = Field(default=None, min_length=16, max_length=512)


class LogoutRequest(BaseModel):
    """登出请求（可选携带刷新令牌以撤销整个家族；浏览器端从 Cookie 读取）。"""

    refresh_token: str | None = Field(default=None, max_length=512)


class ChangePasswordRequest(BaseModel):
    """改密请求。"""

    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class PasswordResetRequest(BaseModel):
    """重置申请：租户填学校编号 + 登录名，平台填账号。"""

    realm: Literal["tenant", "platform"]
    school_code: str | None = Field(default=None, max_length=32)
    login_id: str | None = Field(default=None, max_length=254)
    account: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _check_fields(self) -> "PasswordResetRequest":
        """按 realm 校验必填字段。"""

        if self.realm == "tenant":
            if not (self.school_code or "").strip() or not (self.login_id or "").strip():
                raise ValueError("租户重置需要 school_code 与 login_id")
        elif not (self.account or "").strip():
            raise ValueError("平台重置需要 account")
        return self


class PasswordResetConfirmRequest(BaseModel):
    """重置确认请求（令牌来自邮件链接）。"""

    token: str = Field(max_length=512)
    new_password: str = Field(min_length=1, max_length=128)


class AcceptedResponse(BaseModel):
    """已受理响应（不透露账号是否存在/邮件是否发送）。"""

    status: Literal["accepted"] = "accepted"


class MeResponse(BaseModel):
    """当前主体。"""

    user_id: str
    realm: Literal["tenant", "platform"]
    tenant_id: str | None
    must_change_password: bool
    roles: list[str]
    permissions: list[str]
    mfa_enabled: bool = False
    mfa_required: bool = False


class OkResponse(BaseModel):
    """通用成功响应。"""

    status: Literal["ok"] = "ok"


class SessionItem(BaseModel):
    """登录设备（不含明文 IP/UA）。"""

    session_id: UUID
    device_label: str
    last_seen_at: datetime
    current: bool


class SessionListResponse(BaseModel):
    """当前账号的登录设备列表。"""

    sessions: list[SessionItem]


class RevokeOthersResponse(BaseModel):
    """退出其它设备的结果。"""

    status: Literal["ok"] = "ok"
    revoked_count: int


def _http_error(exc: AuthError) -> HTTPException:
    """业务错误 → HTTP 错误（401 带 WWW-Authenticate，429 带 Retry-After）。"""

    headers = None
    if exc.http_status == 401:
        headers = {"WWW-Authenticate": "Bearer"}
    elif exc.http_status == 429:
        headers = {"Retry-After": str(get_settings().password_reset_ip_window_seconds)}
    return HTTPException(status_code=exc.http_status, detail=exc.message, headers=headers)


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    """下发 HttpOnly 刷新令牌 Cookie（JS 不可读；路径限定在认证接口）。"""

    settings = get_settings()
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=refresh_token,
        max_age=settings.refresh_token_ttl_seconds,
        path=settings.auth_cookie_path,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
    )


def _clear_refresh_cookie(response: Response) -> None:
    """清除刷新令牌 Cookie（登出）。"""

    settings = get_settings()
    response.delete_cookie(
        key=settings.auth_cookie_name,
        path=settings.auth_cookie_path,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
    )


def _cookie_refresh_token(request: Request) -> str | None:
    """从 Cookie 读取刷新令牌（浏览器端）。"""

    value = request.cookies.get(get_settings().auth_cookie_name)
    return value or None


def _token_response(pair: service.TokenPair) -> TokenResponse:
    """TokenPair → 响应模型。"""

    settings = get_settings()
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token if settings.auth_refresh_token_in_body else None,
        expires_in=pair.expires_in,
        realm=pair.realm,
        must_change_password=pair.must_change_password,
        tenant_id=str(pair.tenant_id) if pair.tenant_id else None,
        roles=sorted(pair.roles),
    )


def _bearer_token(authorization: str | None) -> str:
    """从 Authorization 头提取 Bearer 令牌（MFA 绑定接口用）。"""

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="缺少访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail="缺少访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


async def _resolve_mfa_actor(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> service.MfaActor:
    """MFA 绑定/激活主体：普通访问令牌，或强制绑定阶段的受限 enroll 令牌。

    访问令牌走完整的每请求校验（撤销、账号状态、token_version、首登门禁）；
    enroll 令牌只能用于绑定/激活，且仍由服务层校验账号状态与 token_version。
    """

    try:
        principal = await get_current_principal(request, authorization)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_401_UNAUTHORIZED:
            raise
    else:
        return service.MfaActor(
            user_id=principal.user_id,
            realm=principal.realm,
            tenant_id=principal.tenant_id,
            from_enroll_token=False,
        )

    token = _bearer_token(authorization)
    try:
        claims = decode_mfa_token(get_settings(), token, expected_purpose="mfa_enroll")
    except TokenError as exc:
        raise HTTPException(
            status_code=401,
            detail=exc.message,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return service.MfaActor(
        user_id=claims.subject,
        realm=claims.realm,
        tenant_id=claims.tenant_id,
        token_version=claims.token_version,
        from_enroll_token=True,
    )


MfaActorDep = Annotated[service.MfaActor, Depends(_resolve_mfa_actor)]


@router.post("/login", response_model=LoginResponse, response_model_exclude_none=True)
async def login(payload: LoginRequest, request: Request, response: Response) -> LoginResponse:
    """三端登录。

    - 未开启 MFA：直接返回令牌并下发刷新 Cookie；
    - 已绑定 MFA：返回 `mfa_required` 与短时挑战令牌，**不签发会话/Cookie**；
    - 平台强制 MFA 且未绑定：返回 `mfa_enrollment_required` 与绑定令牌。

    异常:
        HTTPException: 401 凭证错误（文案统一，不区分账号/学校是否存在）。
    """

    request_id = getattr(request.state, "request_id", None)
    client_ip = resolve_client_ip(request)
    user_agent = request.headers.get("user-agent")
    try:
        if payload.realm == "tenant":
            outcome = await service.login_tenant(
                school_code=payload.school_code or "",
                login_id=payload.login_id or "",
                password=payload.password,
                ip=client_ip,
                user_agent=user_agent,
                request_id=request_id,
            )
        else:
            outcome = await service.login_platform(
                account=payload.account or "",
                password=payload.password,
                ip=client_ip,
                user_agent=user_agent,
                request_id=request_id,
            )
    except AuthError as exc:
        raise _http_error(exc) from exc

    if isinstance(outcome, service.MfaChallenge):
        return MfaRequiredResponse(mfa_token=outcome.mfa_token, expires_in=outcome.expires_in)
    if isinstance(outcome, service.MfaEnrollmentRequired):
        return MfaEnrollmentRequiredResponse(
            enroll_token=outcome.enroll_token, expires_in=outcome.expires_in
        )
    _set_refresh_cookie(response, outcome.refresh_token)
    return LoginAuthenticatedResponse(**_token_response(outcome).model_dump())


@router.post("/mfa/verify", response_model=TokenResponse, response_model_exclude_none=True)
async def verify_mfa(
    payload: MfaVerifyRequest, request: Request, response: Response
) -> TokenResponse:
    """登录第二步：TOTP 或一次性恢复码；成功后签发会话并下发刷新 Cookie。"""

    try:
        pair = await service.verify_mfa(
            mfa_token=payload.mfa_token,
            code=payload.code,
            ip=resolve_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
    except AuthError as exc:
        raise _http_error(exc) from exc
    _set_refresh_cookie(response, pair.refresh_token)
    return _token_response(pair)


@router.post("/mfa/enroll", response_model=MfaEnrollResponse)
async def enroll_mfa(
    request: Request,
    response: Response,
    actor: MfaActorDep,
    payload: MfaEnrollRequest | None = None,
) -> MfaEnrollResponse:
    """开始绑定 TOTP：返回 secret 与 otpauth URI，10 分钟内完成激活。

    使用访问令牌时必须重新输入当前密码；强制绑定阶段的 enroll 令牌已在登录时
    校验过密码，可省略。
    """

    try:
        enrollment = await service.enroll_mfa(
            actor=actor,
            current_password=payload.current_password if payload else None,
            request_id=getattr(request.state, "request_id", None),
        )
    except AuthError as exc:
        raise _http_error(exc) from exc
    # 响应含 TOTP secret：禁止任何中间层缓存
    response.headers["Cache-Control"] = "no-store"
    return MfaEnrollResponse(
        secret=enrollment.secret,
        otpauth_uri=enrollment.otpauth_uri,
        expires_in=enrollment.expires_in,
    )


@router.post("/mfa/activate", response_model=MfaActivateResponse, response_model_exclude_none=True)
async def activate_mfa(
    payload: MfaActivateRequest,
    actor: MfaActorDep,
    request: Request,
    response: Response,
) -> MfaActivateResponse:
    """激活 TOTP：生成 10 个一次性恢复码，轮换全部会话并下发新刷新 Cookie。"""

    try:
        activation = await service.activate_mfa(
            actor=actor,
            code=payload.code,
            ip=resolve_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
    except AuthError as exc:
        raise _http_error(exc) from exc
    _set_refresh_cookie(response, activation.pair.refresh_token)
    response.headers["Cache-Control"] = "no-store"
    return MfaActivateResponse(
        **_token_response(activation.pair).model_dump(),
        recovery_codes=activation.recovery_codes,
    )


@router.post("/mfa/disable", response_model=TokenResponse, response_model_exclude_none=True)
async def disable_mfa(
    payload: MfaDisableRequest,
    principal: CurrentPrincipal,
    request: Request,
    response: Response,
) -> TokenResponse:
    """关闭 MFA：校验密码 + TOTP/恢复码，清空绑定并轮换全部会话。"""

    try:
        pair = await service.disable_mfa(
            principal_user_id=principal.user_id,
            realm=principal.realm,
            tenant_id=principal.tenant_id,
            current_password=payload.current_password,
            code=payload.code,
            ip=resolve_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
    except AuthError as exc:
        raise _http_error(exc) from exc
    _set_refresh_cookie(response, pair.refresh_token)
    return _token_response(pair)


@router.post("/refresh", response_model=TokenResponse, response_model_exclude_none=True)
async def refresh(payload: RefreshRequest, request: Request, response: Response) -> TokenResponse:
    """刷新访问令牌（轮换；重用检测）。

    刷新令牌优先取 body，其次取 HttpOnly Cookie；两者都没有 → 401。
    """

    refresh_token = payload.refresh_token or _cookie_refresh_token(request)
    if not refresh_token:
        raise HTTPException(
            status_code=401,
            detail="缺少刷新令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        pair = await service.refresh_tokens(
            refresh_token=refresh_token,
            ip=resolve_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
    except AuthError as exc:
        raise _http_error(exc) from exc
    _set_refresh_cookie(response, pair.refresh_token)
    return _token_response(pair)


@router.post("/logout", response_model=OkResponse)
async def logout(
    request: Request,
    response: Response,
    principal: OptionalPrincipal,
    payload: LogoutRequest | None = None,
) -> OkResponse:
    """登出：撤销刷新令牌家族、立即失效当前访问令牌并清除 Cookie。

    主体**可选**：access 过期（停留超 15 分钟）或仅剩 Cookie 时同样必须能登出；
    安全性由"刷新令牌哈希比对"保证——不解码、不匹配就不会撤销任何人（审查 MA2）。
    无论成功与否都清除 Cookie；主体缺失时审计由撤销路径写入（metadata.self_revoked=true）。
    """

    request_id = getattr(request.state, "request_id", None)
    try:
        provided_refresh = (payload.refresh_token if payload else None) or _cookie_refresh_token(
            request
        )
        refresh_revocation: service.FamilyRevocation | None = None
        if provided_refresh:
            # 校验刷新令牌哈希后撤销家族；令牌不匹配按未知处理（不泄露、不误撤销）
            refresh_revocation = await service.revoke_refresh_family(
                refresh_token=provided_refresh,
                actor_user_id=principal.user_id if principal else None,
                actor_realm=principal.realm if principal else None,
                request_id=request_id,
                audit=principal is None,
            )
        if principal is not None:
            # 先撤销数据库家族（Redis 故障也不得让登出失败），再 best-effort 写 jti 撤销
            family_revoked = await service.revoke_session_by_id(
                realm=principal.realm,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                session_id=principal.session_id,
            )
            access_revoked = await revocation.revoke(
                str(principal.jti), principal.expires_at, realm=principal.realm
            )
            # 刷新令牌与 Bearer 属同一 family 时只写一条（family 已由刷新令牌撤销）；
            # 不同 family（如多标签）则每个 family 各记一条，避免漏审（复审 m1）。
            await service.record_logout(
                actor_user_id=principal.user_id,
                actor_realm=principal.realm,
                tenant_id=principal.tenant_id,
                family_id=principal.session_id,
                family_revoked=(
                    family_revoked
                    or (
                        refresh_revocation is not None
                        and refresh_revocation.family_id == principal.session_id
                        and refresh_revocation.revoked
                    )
                ),
                access_revoked=access_revoked,
                subject_user_id=principal.user_id,
                request_id=request_id,
                ip_hash=tokens.hash_client_fingerprint(resolve_client_ip(request)),
                ua_hash=tokens.hash_client_fingerprint(request.headers.get("user-agent")),
            )
            if (
                refresh_revocation is not None
                and refresh_revocation.family_id != principal.session_id
            ):
                await service.record_logout(
                    actor_user_id=principal.user_id,
                    actor_realm=principal.realm,
                    tenant_id=refresh_revocation.tenant_id,
                    scope_realm=refresh_revocation.realm,
                    family_id=refresh_revocation.family_id,
                    family_revoked=refresh_revocation.revoked,
                    access_revoked=False,
                    subject_user_id=refresh_revocation.subject_user_id,
                    request_id=request_id,
                    ip_hash=tokens.hash_client_fingerprint(resolve_client_ip(request)),
                    ua_hash=tokens.hash_client_fingerprint(request.headers.get("user-agent")),
                )
    finally:
        _clear_refresh_cookie(response)
    return OkResponse()


@router.post("/change-password", response_model=TokenResponse, response_model_exclude_none=True)
async def change_password(
    payload: ChangePasswordRequest,
    principal: CurrentPrincipal,
    request: Request,
    response: Response,
) -> TokenResponse:
    """修改密码（首登改密也走这里）；成功后返回新令牌。"""

    try:
        pair = await service.change_password(
            principal_user_id=principal.user_id,
            realm=principal.realm,
            tenant_id=principal.tenant_id,
            current_password=payload.current_password,
            new_password=payload.new_password,
            # 保留当前会话的 MFA 完成状态（强制策略下改密不得降级为仅密码会话）
            mfa_verified=principal.mfa_verified,
            ip=resolve_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
    except AuthError as exc:
        raise _http_error(exc) from exc
    # 改密会撤销全部旧家族：必须同时下发新刷新 Cookie，否则浏览器刷新即掉线（审查 MA1）
    _set_refresh_cookie(response, pair.refresh_token)
    return _token_response(pair)


@router.post(
    "/password-reset/request",
    response_model=AcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_password_reset(
    payload: PasswordResetRequest, request: Request, background_tasks: BackgroundTasks
) -> AcceptedResponse:
    """申请密码重置：恒返回 202（不暴露账号是否存在、是否有邮箱）。

    邮件发送放到响应之后的背景任务，避免用响应耗时枚举账号是否存在；
    202 在任务执行前返回，失败只留审计。
    """

    background_tasks.add_task(
        service.request_password_reset,
        realm=payload.realm,
        school_code=payload.school_code,
        login_id=payload.login_id,
        account=payload.account,
        ip=resolve_client_ip(request),
        request_id=getattr(request.state, "request_id", None),
    )
    return AcceptedResponse()


@router.post(
    "/password-reset/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def confirm_password_reset(
    payload: PasswordResetConfirmRequest, request: Request
) -> Response:
    """用邮件令牌设置新密码；成功后不签发令牌，客户端需重新登录。

    异常:
        HTTPException: 400 令牌无效/过期/已使用；422 新密码不合规。
    """

    try:
        await service.confirm_password_reset(
            token=payload.token,
            new_password=payload.new_password,
            ip=resolve_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
    except AuthError as exc:
        raise _http_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions", response_model=SessionListResponse)
async def list_my_sessions(principal: CurrentPrincipal) -> SessionListResponse:
    """列出当前账号仍有效的登录设备（按最近使用倒序）。"""

    items = await sessions.list_sessions(
        realm=principal.realm,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        current_session_id=principal.session_id,
    )
    return SessionListResponse(
        sessions=[
            SessionItem(
                session_id=item.session_id,
                device_label=item.device_label,
                last_seen_at=item.last_seen_at,
                current=item.current,
            )
            for item in items
        ]
    )


@router.post("/sessions/revoke-others", response_model=RevokeOthersResponse)
async def revoke_other_sessions(
    principal: CurrentPrincipal, request: Request
) -> RevokeOthersResponse:
    """退出除当前设备外的全部登录设备。"""

    revoked_count = await sessions.revoke_others(
        realm=principal.realm,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        current_session_id=principal.session_id,
        request_id=getattr(request.state, "request_id", None),
    )
    return RevokeOthersResponse(revoked_count=revoked_count)


@router.delete("/sessions/{session_id}", response_model=OkResponse)
async def revoke_my_session(
    session_id: UUID,
    principal: CurrentPrincipal,
    request: Request,
    response: Response,
) -> OkResponse:
    """下线本人的一台设备；不存在/非本人统一 404（不泄露会话归属）。"""

    removed = await sessions.revoke_session(
        realm=principal.realm,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        session_id=session_id,
        current_session_id=principal.session_id,
        request_id=getattr(request.state, "request_id", None),
    )
    if not removed:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session_id == principal.session_id:
        _clear_refresh_cookie(response)
    return OkResponse()


@router.get("/me", response_model=MeResponse)
async def me(principal: CurrentPrincipal) -> MeResponse:
    """返回当前主体与权限集合。"""

    mfa_enabled = await service.is_mfa_enabled(
        realm=principal.realm,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
    )
    return MeResponse(
        user_id=str(principal.user_id),
        realm=principal.realm,
        tenant_id=str(principal.tenant_id) if principal.tenant_id else None,
        must_change_password=principal.must_change_password,
        roles=sorted(principal.roles),
        permissions=sorted(principal.permissions),
        mfa_enabled=mfa_enabled,
        mfa_required=principal.realm == "platform" and get_settings().mfa_require_platform,
    )
