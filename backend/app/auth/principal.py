"""认证依赖：把访问令牌解析为请求主体（Principal），并强制 realm↔连接绑定。

关键约束（TD-074）：
- **租户 realm** 的请求只使用租户连接（`db_session(tenant_id=...)`），身份与权限全部在
  RLS 上下文内读取；
- **平台 realm** 的请求只使用平台连接（`db_session(platform=True)`）；
- 两种令牌的 `aud` 不同，`decode_access_token` 会拒绝跨 realm 混用；
- 身份/权限/状态**每次请求从数据库读取**（不放进令牌快照），因此 `token_version` 自增、
  账号停用、权限变更都能立即生效；`must_change_password` 门禁只用令牌 claim（改密接口
  会自增 `token_version`，不会出现"改完还被挡"）。
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import revocation
from app.auth.tokens import Realm, TokenError, decode_access_token
from app.config import get_settings
from app.db.session import db_session
from app.tenant import context as tenant_context

logger = logging.getLogger(__name__)

# 首登改密期间仍然允许访问的接口（**精确路径**，不做后缀匹配，避免未来路由误放行）
_GATE_ALLOWED_SUFFIXES = (
    "/auth/change-password",
    "/auth/logout",
    "/auth/me",
)


def _is_gate_allowed(path: str) -> bool:
    """路径是否在首登改密白名单内（去尾斜杠后精确比较，前缀取配置的 api_prefix）。"""

    prefix = get_settings().api_prefix.rstrip("/")
    normalized = path.rstrip("/") or "/"
    return normalized in {f"{prefix}{suffix}" for suffix in _GATE_ALLOWED_SUFFIXES}


@dataclass(frozen=True, slots=True)
class Principal:
    """已认证主体（每次请求从数据库解析，不缓存权限快照）。"""

    user_id: UUID
    realm: Realm
    tenant_id: UUID | None
    jti: UUID
    session_id: UUID
    expires_at: datetime
    must_change_password: bool
    roles: frozenset[str]
    permissions: frozenset[str]
    # 当前会话是否完成过 MFA（来自访问令牌 amr；改密重签时原样保留）
    mfa_verified: bool = False

    def has_permission(self, code: str) -> bool:
        """是否具备某权限码。"""

        return code in self.permissions


async def _must_change_password(
    session: AsyncSession, user_id: UUID, *, tenant: bool
) -> bool:
    """以数据库为准判断是否处于首登改密状态（claim 可能滞后 ≤15 分钟）。"""

    table = "users" if tenant else "platform_users"
    value = await session.scalar(
        text(f"select must_change_password from {table} where id = :uid"),  # nosec B608 - 表名来自布尔分支
        {"uid": user_id},
    )
    return bool(value)


def _unauthorized(message: str) -> HTTPException:
    """统一的 401 响应（不回显令牌内容）。"""

    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=message,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _parse_bearer(authorization: str | None) -> str:
    """从 Authorization 头提取 Bearer 令牌。"""

    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized("缺少访问令牌")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise _unauthorized("缺少访问令牌")
    return token


async def _tenant_identity(
    session: AsyncSession, user_id: UUID
) -> tuple[bool, int, frozenset[str], frozenset[str]]:
    """在租户连接内读取用户状态、token_version、角色与权限码。"""

    row = (
        await session.execute(
            text(
                "select status, token_version from users where id = :uid"
            ),
            {"uid": user_id},
        )
    ).mappings().one_or_none()
    if row is None:
        raise _unauthorized("令牌无效")

    role_rows: Sequence[str] = (
        await session.execute(
            text(
                "select r.code from user_roles ur "
                "join roles r on r.id = ur.role_id and r.tenant_id = ur.tenant_id "
                "where ur.user_id = :uid"
            ),
            {"uid": user_id},
        )
    ).scalars().all()
    permission_rows: Sequence[str] = (
        await session.execute(
            text(
                "select distinct p.code from user_roles ur "
                "join role_permissions rp on rp.role_id = ur.role_id "
                "and rp.tenant_id = ur.tenant_id "
                "join permissions p on p.id = rp.permission_id "
                "where ur.user_id = :uid"
            ),
            {"uid": user_id},
        )
    ).scalars().all()
    return (
        row["status"] == "active",
        int(row["token_version"]),
        frozenset(str(code) for code in role_rows),
        frozenset(str(code) for code in permission_rows),
    )


async def _platform_identity(
    session: AsyncSession, user_id: UUID
) -> tuple[bool, int, frozenset[str], frozenset[str]]:
    """在平台连接内读取平台账号状态、token_version、角色与权限码。"""

    row = (
        await session.execute(
            text("select status, token_version from platform_users where id = :uid"),
            {"uid": user_id},
        )
    ).mappings().one_or_none()
    if row is None:
        raise _unauthorized("令牌无效")

    role_rows: Sequence[str] = (
        await session.execute(
            text(
                "select r.code from platform_user_roles ur "
                "join platform_roles r on r.id = ur.platform_role_id "
                "where ur.platform_user_id = :uid"
            ),
            {"uid": user_id},
        )
    ).scalars().all()
    permission_rows: Sequence[str] = (
        await session.execute(
            text(
                "select distinct p.code from platform_user_roles ur "
                "join platform_role_permissions rp on rp.platform_role_id = ur.platform_role_id "
                "join permissions p on p.id = rp.permission_id "
                "where ur.platform_user_id = :uid"
            ),
            {"uid": user_id},
        )
    ).scalars().all()
    return (
        row["status"] == "active",
        int(row["token_version"]),
        frozenset(str(code) for code in role_rows),
        frozenset(str(code) for code in permission_rows),
    )


async def get_current_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """解析并校验访问令牌，返回请求主体。

    异常:
        HTTPException: 401（令牌缺失/无效/过期/账号停用/版本不匹配/已登出）、
            403（首登未改密访问受限接口）。
    """

    settings = get_settings()
    token = _parse_bearer(authorization)
    try:
        claims = decode_access_token(settings, token)
    except TokenError as exc:
        raise _unauthorized(exc.message) from exc

    if not await revocation.is_active(
        str(claims.jti), claims.session_id, realm=claims.realm
    ):
        raise _unauthorized("该设备已被退出登录")

    must_change_password = claims.must_change_password
    if claims.realm == "tenant":
        if claims.tenant_id is None:
            raise _unauthorized("令牌无效")
        async with db_session(tenant_id=claims.tenant_id) as session:
            active, token_version, roles, permissions = await _tenant_identity(
                session, claims.subject
            )
            must_change_password = await _must_change_password(session, claims.subject, tenant=True)
    else:
        async with db_session(platform=True) as session:
            active, token_version, roles, permissions = await _platform_identity(
                session, claims.subject
            )
            must_change_password = await _must_change_password(
                session, claims.subject, tenant=False
            )

    if not active:
        raise _unauthorized("账号不可用，请联系管理员")
    if token_version != claims.token_version:
        raise _unauthorized("登录已失效，请重新登录")

    tenant_context.bind_context(
        tenant_context.RequestContext(
            realm=claims.realm,
            user_id=claims.subject,
            tenant_id=claims.tenant_id,
            roles=roles,
            permissions=permissions,
            request_id=getattr(request.state, "request_id", None),
        )
    )

    if must_change_password and not _is_gate_allowed(request.url.path):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="首次登录必须修改密码后才能使用其他功能",
            headers={"X-Auth-Error": "PASSWORD_CHANGE_REQUIRED"},
        )

    return Principal(
        user_id=claims.subject,
        realm=claims.realm,
        tenant_id=claims.tenant_id,
        must_change_password=must_change_password,
        jti=claims.jti,
        session_id=claims.session_id,
        expires_at=claims.expires_at,
        roles=roles,
        permissions=permissions,
        mfa_verified=claims.mfa_verified,
    )


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


async def get_optional_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal | None:
    """可选主体：无/无效令牌时返回 None（供"仅凭 HttpOnly Cookie 登出"这类接口使用）。

    注意：这类接口**不得**依赖主体做安全判定——真正凭证是刷新令牌（哈希比对后才撤销）。
    """

    if not authorization:
        return None
    try:
        return await get_current_principal(request, authorization)
    except HTTPException:
        return None


OptionalPrincipal = Annotated[Principal | None, Depends(get_optional_principal)]


def require_permission(code: str, *, realm: Realm) -> object:
    """构造"需要某权限码且限定 realm"的依赖。

    **realm 必传**：权限目录是全局的（platform.* 与校方/C 端码同表），数据库层也不能保证
    租户角色不会引用 platform.* 码；因此平台路由必须传 `realm="platform"`，租户/C 端路由必须
    传 `realm="tenant"`，仅靠权限码不足以构成路由级边界（审查 B1、M1-E1 简报 §3.3）。

    校验失败时写 `auth.permission.denied` 审计（含真实主体与拒绝原因），再返回 403；
    审计写入失败不影响拒绝结果（权限校验绝不因审计故障放行）。

    依赖函数上带 `__rbac_permission__`/`__rbac_realm__` 标记，供路由鉴权清单测试
    （`tests/test_route_permission_manifest_unit.py`）从依赖树读回声明，避免装饰器双写漂移。
    """

    async def _dependency(request: Request, principal: CurrentPrincipal) -> Principal:
        if principal.realm != realm:
            await _audit_denied(principal, code, request, reason="realm_mismatch")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="当前身份不允许访问该接口",
            )
        if not principal.has_permission(code):
            await _audit_denied(principal, code, request, reason="missing_permission")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"缺少权限：{code}",
            )
        return principal

    _dependency.__rbac_permission__ = code  # type: ignore[attr-defined]
    _dependency.__rbac_realm__ = realm  # type: ignore[attr-defined]
    return Depends(_dependency)


async def _audit_denied(
    principal: "Principal", code: str, request: Request, *, reason: str = "missing_permission"
) -> None:
    """记录一次权限拒绝（best-effort）。"""

    from app.audit.writer import append_audit

    try:
        if principal.realm == "platform":
            async with db_session(platform=True) as session:
                await append_audit(
                    session,
                    action="auth.permission.denied",
                    resource_type="permission",
                    resource_id=code,
                    actor_realm="platform",
                    actor_id=principal.user_id,
                    result="failure",
                    metadata={"path": request.url.path, "reason": reason},
                    request_id=getattr(request.state, "request_id", None),
                )
        elif principal.tenant_id is not None:
            async with db_session(tenant_id=principal.tenant_id) as session:
                await append_audit(
                    session,
                    action="auth.permission.denied",
                    resource_type="permission",
                    resource_id=code,
                    actor_realm="tenant",
                    actor_id=principal.user_id,
                    tenant_id=principal.tenant_id,
                    result="failure",
                    metadata={"path": request.url.path, "reason": reason},
                    request_id=getattr(request.state, "request_id", None),
                )
    except Exception as exc:  # 审计故障不得放行或阻断权限拒绝
        # 拒绝仍然生效，但"谁在什么时候被挡了"必须留下痕迹供运维排查——静默吞掉
        # 等于安全事件失去可观测性。
        # 只记异常类型，不用 exc_info：SQLAlchemy 会把 `[SQL: ...] [parameters: ...]` 拼进
        # 异常字符串，打开 traceback 就等于把 SQL 参数（路径、主体 id）写进日志（红线）。
        logger.warning(
            "权限拒绝审计写入失败：%s.%s",
            type(exc).__module__,
            type(exc).__name__,
            extra={"permission": code},
        )
        return
