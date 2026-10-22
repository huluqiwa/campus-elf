"""M1-E1 单元：路由鉴权清单。

规则（简报 §3.3-A）：应用里**每一个**接口要么显式挂 `require_permission(码, realm=…)`，
要么进入"免鉴权白名单"并写清理由。新增接口忘挂鉴权不会靠人眼审查发现，而是让这条测试
直接红——这是"前端隐藏按钮不算安全边界、服务端必须再次校验"（AGENTS.md §3）在 CI 上的落点。

纯单元测试：只构建应用并读取路由元数据，不发出请求、不连数据库。
"""

import re

from app.config import get_settings
from app.main import create_app
from app.rbac.catalog import catalog_codes, code_realm
from app.rbac.routes import (
    FRAMEWORK_ROUTE_PATHS,
    PERMISSION_FREE_ROUTES,
    RouteEntry,
    framework_routes,
    is_permission_free_allowed,
    route_entries,
)

API_PREFIX = get_settings().api_prefix.rstrip("/")

ENTRIES: tuple[RouteEntry, ...] = route_entries(create_app())
SKIPPED: tuple[str, ...] = framework_routes(create_app())


def _matches(entry_path: str, actual_path: str) -> bool:
    """模板匹配：`/auth/sessions/{session_id}` 匹配任意同段真实路径。"""

    if "{" in entry_path:
        pattern = re.sub(r"\{[^/]+\}", "[^/]+", entry_path)
        return re.fullmatch(pattern, actual_path) is not None
    return entry_path == actual_path


def test_manifest_sees_the_whole_app() -> None:
    """清单确实读到了路由（FastAPI 内部结构变化时先在这里报警，而不是让其它用例静默通过）。"""

    paths = {entry.path for entry in ENTRIES}

    assert len(ENTRIES) >= 20, f"只读到 {len(ENTRIES)} 条路由，请检查 route_entries 的展开逻辑"
    assert f"{API_PREFIX}/auth/login" in paths
    assert f"{API_PREFIX}/campus/chat/turns" in paths
    assert f"{API_PREFIX}/platform/tenant-requests/{{request_id}}/review" in paths
    assert "/health/live" in paths


def test_every_route_declares_permission_or_is_whitelisted() -> None:
    """非白名单接口必须声明权限码与 realm；白名单条目必须写理由。"""

    offenders: list[str] = []
    for entry in ENTRIES:
        if entry.permissions:
            continue
        reason = PERMISSION_FREE_ROUTES.get((entry.method, entry.path))
        if reason is None:
            offenders.append(f"{entry.method} {entry.path} 未声明权限码，也不在免鉴权白名单")
        elif not reason.strip():
            offenders.append(f"{entry.method} {entry.path} 白名单条目缺理由")

    assert offenders == [], "缺少服务端鉴权声明：\n" + "\n".join(sorted(offenders))


def test_whitelist_does_not_cover_protected_routes() -> None:
    """白名单里的接口若已挂权限，属于口径漂移（要么撤白名单，要么去掉权限声明）。"""

    conflicts = sorted(
        f"{entry.method} {entry.path}"
        for entry in ENTRIES
        if entry.permissions
        and any(
            method == entry.method and _matches(path, entry.path)
            for (method, path) in PERMISSION_FREE_ROUTES
        )
    )

    assert conflicts == [], f"白名单与权限声明同时存在：{conflicts}"


def test_whitelist_has_no_stale_entries() -> None:
    """白名单不得留下已删除/改名的路由（过期条目=无人复核的放行口子）。"""

    actual = {(entry.method, entry.path) for entry in ENTRIES}
    stale = sorted(
        f"{method} {path}"
        for (method, path) in PERMISSION_FREE_ROUTES
        if not any(m == method and _matches(path, found) for m, found in actual)
    )

    assert stale == [], f"白名单存在已不存在的路由：{stale}"


def test_whitelist_stays_within_allowed_categories() -> None:
    """免鉴权条目只能落在认证自助/健康探针/公开登记三类里（防止随手扩大白名单）。"""

    offenders = sorted(
        f"{method} {path}"
        for (method, path) in PERMISSION_FREE_ROUTES
        if not is_permission_free_allowed(path)
    )

    assert offenders == [], f"白名单出现不允许的类别：{offenders}"


def test_declared_permission_codes_exist_in_catalog() -> None:
    """接口声明的权限码必须在权限目录里（引用幽灵码=永远 403 的死接口）。"""

    known = set(catalog_codes())
    offenders = sorted(
        f"{entry.method} {entry.path} -> {code}"
        for entry in ENTRIES
        for code, _realm in entry.permissions
        if code not in known
    )

    assert offenders == [], f"接口引用了目录外的权限码：{offenders}"


def test_realm_matches_route_reach() -> None:
    """realm 与路径归属一致，且不得留空：平台前缀用 platform，其余一律 tenant。

    只靠权限码不足以构成边界（目录是全局的，校方角色可能碰到同名码）。
    """

    offenders: list[str] = []
    for entry in ENTRIES:
        for code, realm in entry.permissions:
            on_platform_path = entry.path.startswith(f"{API_PREFIX}/platform/")
            expected = "platform" if on_platform_path else "tenant"
            if realm != expected:
                offenders.append(
                    f"{entry.method} {entry.path} 声明 realm={realm!r}，"
                    f"应为 {expected!r}（码 {code}）"
                )

    assert offenders == [], "\n".join(offenders)


def test_platform_review_endpoint_keeps_review_permission() -> None:
    """已交付的平台接口按 M1-C 的码鉴权（回归：改造时不能把审核权限降成查看权限）。"""

    review = [
        entry
        for entry in ENTRIES
        if entry.method == "POST"
        and entry.path == f"{API_PREFIX}/platform/tenant-requests/{{request_id}}/review"
    ]

    assert len(review) == 1
    assert ("platform.tenant.review", "platform") in review[0].permissions


def test_unrecognized_route_kinds_are_only_framework_assets() -> None:
    """清单读不懂的路由形态只能是框架自带的文档资产（防止 websocket/Mount 静默绕过门禁）。

    `_flatten` 只认 `APIRoute` 与 `include_router` 的包装对象。将来加
    `@app.websocket(...)` 或 `app.mount(...)` 时，它不会出现在 `ENTRIES` 里，
    「每条接口都要显式鉴权」就悄悄失效了——这条测试把它变成一次红色的 CI 失败，
    要求新形态要么纳入清单，要么显式登记理由。
    """

    offenders = sorted(
        skipped
        for skipped in SKIPPED
        if not any(
            skipped.endswith(f"({path})") for path in FRAMEWORK_ROUTE_PATHS | {"/favicon.ico"}
        )
    )

    assert offenders == [], f"路由清单无法判定鉴权的对象：{offenders}"


def test_declared_realm_matches_the_permission_domain() -> None:
    """接口声明的 realm 必须等于权限码所属域（矩阵 §3.4：observability/eval 属平台运维）。

    路径前缀只保证「平台前缀用 platform realm」，不保证「这条码本身是平台域的」：
    用 `realm="tenant"` 挂一个 `eval.run` 会让租户角色（一旦被授予该码）打开平台能力，
    而 `school.*` 码挂成 `realm="platform"` 则是反向漂移。
    """

    offenders = sorted(
        f"{entry.method} {entry.path} -> {code} 声明 realm={realm!r}，"
        f"该码属于 {code_realm(code)!r} 域"
        for entry in ENTRIES
        for code, realm in entry.permissions
        if realm is not None and code_realm(code) != realm
    )

    assert offenders == [], "\n".join(offenders)
