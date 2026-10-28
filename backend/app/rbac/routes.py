"""路由鉴权元数据的读取入口与免鉴权白名单（M1-E1 简报 §3.3-A）。

为什么要这个模块：`require_permission(码, realm=…)` 是**函数内**声明的依赖，
新增接口忘挂鉴权靠人眼审查看不出来。把"这条路由声明了哪些权限"做成可读取的元数据
（读 FastAPI 依赖树上被打的标记，不需要装饰器双写），路由清单测试就能在 CI 上强制：

- 每一条路由要么声明权限码 + realm，要么出现在 `PERMISSION_FREE_ROUTES` 里并写清理由；
- 白名单只能落在 `PERMISSION_FREE_PREFIX_ALLOWLIST` / `PERMISSION_FREE_EXACT_ALLOWLIST`
  允许的范围内，防止有人把新接口随手加进白名单绕过门禁。
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant  # 内部模块，但 FastAPI 0.1xx 稳定；仅读元数据
from fastapi.routing import APIRoute

from app.config import get_settings

# (权限码, realm)；realm 为 None 表示依赖没有限定 realm（清单测试会拒绝这种情况）
PermissionRequirement = tuple[str, str | None]

API_PREFIX = get_settings().api_prefix.rstrip("/")

PERMISSION_FREE_ROUTES: dict[tuple[str, str], str] = {
    # --- 公开：还没有账号，无从鉴权 ---
    ("POST", f"{API_PREFIX}/platform/tenants/register"): (
        "校方提交开通申请：此时还没有任何账号可鉴权；靠字段/资质文件/体积校验、"
        "学校名重复提示与服务端审计收口"
    ),
    ("POST", f"{API_PREFIX}/auth/login"): "认证入口本身：无身份可鉴权；凭据校验、失败锁定与 MFA 挑战在服务层",
    ("POST", f"{API_PREFIX}/auth/mfa/verify"): (
        "登录第二步：只接受登录挑战签发的 `aud=campus-elf-mfa` 临时令牌，"
        "不接收普通访问令牌；验证通过才换正式会话"
    ),
    ("POST", f"{API_PREFIX}/auth/password-reset/request"): (
        "公开找回：账号是否存在一律 202（防枚举）；靠邮件令牌与限流收口"
    ),
    ("POST", f"{API_PREFIX}/auth/password-reset/confirm"): (
        "公开找回第二步：凭一次性邮件令牌（Redis 单次消费）设新密码；成功不签发令牌，需重新登录"
    ),
    # --- 本人自助：只作用于调用者自己的账号/会话，已在服务层校验归属 ---
    ("POST", f"{API_PREFIX}/auth/mfa/enroll"): "本人开启两步验证的第一步：需已认证，只写自己的绑定",
    ("POST", f"{API_PREFIX}/auth/mfa/activate"): "本人激活 MFA：需已认证 + 校验当期动态码",
    ("POST", f"{API_PREFIX}/auth/mfa/disable"): "本人关闭 MFA：需已认证并通过第二步验证（服务层校验）",
    ("POST", f"{API_PREFIX}/auth/change-password"): (
        "本人改密：需旧密码；属于账号级安全事件，改密会轮换会话（不同于权限热更新）"
    ),
    ("GET", f"{API_PREFIX}/auth/me"): "本人身份回显（含 roles/permissions，供前端渲染；服务端仍逐请求校验）",
    ("POST", f"{API_PREFIX}/auth/refresh"): "会话内换发访问令牌：凭 HttpOnly 刷新 Cookie + Redis 会话标记",
    ("POST", f"{API_PREFIX}/auth/logout"): "只结束调用者自己的会话",
    ("GET", f"{API_PREFIX}/auth/sessions"): "只列自己的会话",
    ("POST", f"{API_PREFIX}/auth/sessions/revoke-others"): "只下线自己的其他设备",
    ("DELETE", f"{API_PREFIX}/auth/sessions/{{session_id}}"): "只下线本人会话（按 user_id 过滤，非本人统一 404）",
    # --- 探针：无业务数据 ---
    ("GET", "/health/live"): "存活探针：Compose/Nginx 无凭据探测，只回进程存活",
    ("GET", "/health/ready"): "就绪探针：只回依赖（PG/Redis 等）就绪状态，不含业务数据",
    # --- 运维抓取：无业务数据，默认关闭；开启后必须由网络策略限制到内网抓取端 ---
    ("GET", "/metrics"): (
        "Prometheus 抓取端点：只输出有界标签的计数器（方法/路由模板/状态码/原因码），"
        "不含租户/用户/登录名；METRICS_ENABLED=false 时返回 404。端点本身无鉴权"
        "（Prometheus 不带业务令牌），开启后必须由网络策略限制到内网抓取端"
    ),
}

# 免鉴权条目允许落的位置（超出这个范围的白名单条目视为越权放行，测试直接红）
PERMISSION_FREE_PREFIX_ALLOWLIST: tuple[str, ...] = (f"{API_PREFIX}/auth/", "/health")
PERMISSION_FREE_EXACT_ALLOWLIST: tuple[str, ...] = (
    f"{API_PREFIX}/platform/tenants/register",
    "/metrics",
)


def _walk(dependant: Dependant) -> Iterator[PermissionRequirement]:
    """深度优先遍历依赖树，取出 `require_permission` 打在依赖函数上的标记。"""

    call: Any = getattr(dependant, "call", None)
    if call is not None:
        code = getattr(call, "__rbac_permission__", None)
        if isinstance(code, str):
            realm = getattr(call, "__rbac_realm__", None)
            yield (code, realm if isinstance(realm, str) else None)
    for sub in getattr(dependant, "dependencies", ()) or ():
        yield from _walk(sub)


def declared_permissions(route: APIRoute) -> tuple[PermissionRequirement, ...]:
    """这条路由声明的权限要求（按 (码, realm) 去重，保持声明顺序）。

    去重键含 realm：同一码被两条依赖以不同 realm 声明时（改造时的真实失误），
    只按码去重会让后到的那条静默消失，清单测试就看不到 realm 漂移。
    """

    seen: dict[PermissionRequirement, PermissionRequirement] = {}
    for requirement in _walk(route.dependant):
        seen.setdefault(requirement, requirement)
    return tuple(seen.values())


# FastAPI/Starlette 自己挂的非业务路由（文档与静态资源）：没有业务处理器，也不读数据。
FRAMEWORK_ROUTE_PATHS: frozenset[str] = frozenset(
    {"/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}
)


def framework_routes(application: FastAPI) -> tuple[str, ...]:
    """应用里不属于业务路由、因而清单无法判定其鉴权的对象（按路径排序）。

    为什么要单独暴露：`_flatten` 只认 `APIRoute` 与 `include_router` 的包装对象。
    将来有人加 `@app.websocket(...)` 或 `app.mount(...)`，它会被静默跳过——
    「每条接口都要显式鉴权」这条门禁就对它失效了。清单测试断言这里的集合只包含
    框架自带的文档路由，新增任何不可识别的路由形态都会让测试变红。
    """

    skipped: list[str] = []
    list(_flatten(application.routes, "", skipped=skipped))
    return tuple(sorted(set(skipped)))


def is_permission_free_allowed(path: str) -> bool:
    """该路径是否属于允许免鉴权的类别（认证自助 / 健康探针 / 公开登记 / 指标端点）。"""

    return path.startswith(PERMISSION_FREE_PREFIX_ALLOWLIST) or path in PERMISSION_FREE_EXACT_ALLOWLIST


@dataclass(frozen=True, slots=True)
class RouteEntry:
    """一条可鉴权的路由：HTTP 方法、完整路径模板、声明的权限要求。"""

    method: str
    path: str
    permissions: tuple[PermissionRequirement, ...]


def _flatten(
    routes: Any, prefix: str, *, skipped: list[str] | None = None
) -> Iterator[tuple[str, APIRoute]]:
    """展开 `include_router` 的延迟装配，得到 (完整路径模板, APIRoute)。

    FastAPI 0.146 起 `app.routes` 里放的是 `_IncludedRouter` 包装对象，路由要按
    `original_router` + `include_context.prefix` 递归展开；本项目不在 include 层
    挂全局依赖（若将来挂了，需在此处一并读入）。

    `skipped`：既不是 `APIRoute`、也不是 `include_router` 包装的对象（框架文档路由、
    将来的 websocket/Mount 等）按 "类名(路径)" 记在这里，交给清单测试裁决。
    """

    for route in routes or ():
        if isinstance(route, APIRoute):
            yield f"{prefix}{route.path}", route
            continue
        original = getattr(route, "original_router", None)
        if original is None:
            if skipped is not None:
                skipped.append(f"{type(route).__name__}({getattr(route, 'path', '?')})")
            continue  # /docs、/openapi.json 等框架自带 Route：无业务处理函数
        context = getattr(route, "include_context", None)
        extra = getattr(context, "prefix", "") or ""
        yield from _flatten(getattr(original, "routes", ()), f"{prefix}{extra}", skipped=skipped)


def route_entries(application: FastAPI) -> tuple[RouteEntry, ...]:
    """应用里每条业务路由的鉴权声明清单（排除同处理器的 HEAD 与 CORS 预检）。"""

    entries: list[RouteEntry] = []
    for path, route in _flatten(application.routes, ""):
        permissions = declared_permissions(route)
        for method in sorted(route.methods or ()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            entries.append(RouteEntry(method=method, path=path, permissions=permissions))
    return tuple(entries)
