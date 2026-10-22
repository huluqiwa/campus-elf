"""RBAC 定义同步（M1-E1）。

两层口径的落地点（`docs/rbac-matrix.md` v1.2 + `docs/design-briefs/M1-E1-rbac-core.md` §3.1/§5.1）：

1. **权限项目录**（`catalog.py`）跟代码走：`permissions` 按码 upsert，描述与代码同步更新；
2. **角色↔权限映射**（`seeds.py` 出厂默认）是运营配置：同步**绝不覆盖、也绝不删除**已有映射，
   人工撤销的权限不会被悄悄加回来。唯一的补授差异见 `seeds.py:FULL_SCOPE_ROLES` ——
   两个超管在矩阵里被*定义*成「范围内全量」，所以按「只补不撤」对齐；其余内置角色的默认
   只在「角色还没有任何映射」时授一次（新建租户由 `seed_builtin_roles()` 授）。

出厂默认另外写进两张**代码所有**的定义表：

- `rbac_builtin_roles(realm, code, name)`：内置角色清单；
- `rbac_builtin_role_defaults(realm, role_code, permission_code)`：内置角色的出厂默认映射。

为什么要这两张表：`provision_tenant()` 是 SQL 函数，开通新租户时要建角色并给默认权限。
把码清单抄进 SQL 会出现"迁移里一份、Python 里一份"的两份真相（新增一个权限码却忘了改函数，
新租户就少一条权限，而且没人会报错）。现在函数只读定义表，同步每次按 `seeds.py` **整表重写**
定义表（这两张表是代码的副本，不是运营配置，所以允许全量覆盖）：新租户的默认权限因此
自动跟上目录，存量租户按上面第 2 条的补授口径生效。

同步是幂等的：跑两次结果相同。

SQL 以「语句 + 参数」列表暴露（`build_sync_statements`），因为 Alembic 迁移里拿到的是同步连接，
而集成测试/将来 M2 的运维入口用的是 AsyncSession —— 两边执行同一份语句，不存在第二份真相。
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from app.rbac.catalog import (
    PERMISSION_CATALOG,
    TENANT_MODULES,
    catalog_codes,
    code_realm,
    codes_with_module,
    legacy_codes_removed_by_m1e1,
)
from app.rbac.seeds import (
    FULL_SCOPE_ROLES,
    PLATFORM_BUILTIN_ROLES,
    TENANT_BUILTIN_ROLES,
    BuiltinRole,
    Realm,
)

# 内置角色码（同步前用于校验库里没有同名但非系统的角色，避免把运营自建角色当成内置角色）
BUILTIN_ROLE_CODES: tuple[str, ...] = tuple(
    sorted({role.code for role in (*PLATFORM_BUILTIN_ROLES, *TENANT_BUILTIN_ROLES)})
)

# 越权守卫（口径见 `catalog.code_realm()` 的租户模块白名单）：
# 租户角色只能持有 `school`/`campus` 模块的码——不只 `platform`：`observability`/`eval`
# 同属平台运维域（矩阵 §3.4），只挡 `platform` 会给这两类留空档。
# 同步写完后再查一次，防止目录/种子改动引入越权映射。
# 模块集合从 `catalog.TENANT_MODULES` 绑定传入，不在 SQL 里抄第二份字面量：白名单若调整，
# 这里与启动自检（`app/db/selfcheck.py`）必须同步变化，否则防线静默漂移。
# 注意 DB 触发器 `role_permissions_realm_guard` 目前仍只挡 `module='platform'`（收紧为同一
# 白名单见 TD-149 → M1-E2）；在那之前这道检查是 `observability`/`eval` 的唯一防线。
TENANT_PLATFORM_LEAK_CHECK_SQL: str = """
    select r.code, p.code
    from public.roles r
    join public.role_permissions rp on rp.role_id = r.id
    join public.permissions p on p.id = rp.permission_id
    where p.module <> all(cast(:tenant_modules as text[]))
"""

# 口径 ⑧ 的前置闸门（简报 §5.1-①）：9 个旧码**只允许**在"删掉也不丢能力"的角色上被引用。
# 全量型角色（`seeds.FULL_SCOPE_ROLES`，即两个超管）满足条件——它们的出厂默认按模块全量补，
# 旧码语义已被新码覆盖；其他角色（含运营自建）一旦被引用就 fail-fast，列出待人工改授的清单
# ——而不是静默删映射，让人第二天才发现权限凭空少了。
# 角色码从 `FULL_SCOPE_ROLES` 绑定传入，不在 SQL 里写字面量：新增全量型角色时只改一处。
LEGACY_GRANT_GUARD_SQL: str = """
    select r.tenant_id::text as tenant_id, r.code as role_code, p.code as permission_code
    from public.role_permissions rp
    join public.roles r on r.id = rp.role_id
    join public.permissions p on p.id = rp.permission_id
    where p.code = any(cast(:codes as text[]))
      and not (r.is_system is true and r.code = any(cast(:full_codes as text[])))
    union all
    select 'platform', pr.code, p.code
    from public.platform_role_permissions prp
    join public.platform_roles pr on pr.id = prp.platform_role_id
    join public.permissions p on p.id = prp.permission_id
    where p.code = any(cast(:codes as text[]))
      and not (pr.is_system is true and pr.code = any(cast(:full_codes as text[])))
    order by 1, 2, 3
"""

# 内置角色码撞上运营自建（非系统）角色时不能同步：两侧表都要查，
# 否则平台侧同名非系统角色会被当成内置角色补授全量目录。
CONFLICTING_ROLE_CHECK_SQL: str = """
    select tenant_id::text as tenant_id, code
    from public.roles
    where code = any(cast(:codes as text[]))
      and is_system is not true
    union all
    select 'platform', pr.code
    from public.platform_roles pr
    where pr.code = any(cast(:codes as text[]))
      and pr.is_system is not true
    order by 2
"""


def _full_scope_codes(realm: Realm) -> tuple[str, ...]:
    """某 realm 的全量型角色码（补授口径与旧码闸门共用同一份真相）。"""

    return tuple(sorted(code for role_realm, code in FULL_SCOPE_ROLES if role_realm == realm))


def _self_check_seeds() -> None:
    """种子自洽校验（不依赖数据库）：幽灵码、租户角色越权、超管范围漂移。

    异常:
        RuntimeError：上述任一不一致。
    """

    known = set(catalog_codes())
    for role in (*PLATFORM_BUILTIN_ROLES, *TENANT_BUILTIN_ROLES):
        ghosts = sorted(role.permissions - known)
        if ghosts:
            raise RuntimeError(f"{role.realm}/{role.code} 引用了目录外的权限码：{ghosts}")
        # realm 口径见 `catalog.code_realm`：`observability`/`eval` 也属平台运维域，
        # 用码前缀判分会漏掉这两类。方向只约束租户角色：矩阵 §5-11 把平台超管定义成
        # 「全量运维超集」，它按设计持有目录内全部码（含 school/campus 域），不得反向判违规。
        leaked = sorted(code for code in role.permissions if code_realm(code) != "tenant")
        if role.realm == "tenant" and leaked:
            raise RuntimeError(f"租户角色 {role.code} 引用了租户域（school/campus）外的权限：{leaked}")

    super_admin = next(role for role in TENANT_BUILTIN_ROLES if role.code == "school_super_admin")
    if super_admin.permissions != codes_with_module("school") | codes_with_module("campus"):
        raise RuntimeError("校方超管默认权限与「本校全量（school+campus 模块）」语义不一致")


def _delete_legacy_permission_statements() -> list[tuple[str, dict[str, Any]]]:
    """删除 M1-A 目录里与矩阵重复的 9 个旧码（口径 ⑧）：先删映射，再删码。"""

    codes = list(legacy_codes_removed_by_m1e1())
    return [
        (
            """
            delete from public.role_permissions
            where permission_id in (
                select id from public.permissions where code = any(cast(:codes as text[]))
            )
            """,
            {"codes": codes},
        ),
        (
            """
            delete from public.platform_role_permissions
            where permission_id in (
                select id from public.permissions where code = any(cast(:codes as text[]))
            )
            """,
            {"codes": codes},
        ),
        (
            "delete from public.permissions where code = any(cast(:codes as text[]))",
            {"codes": codes},
        ),
    ]


def _upsert_permission_statements() -> list[tuple[str, dict[str, Any]]]:
    """权限项目录 upsert：码为自然键，module/action/description 以代码为准。"""

    return [
        (
            """
            insert into public.permissions (id, code, module, action, description)
            values (gen_random_uuid(), :code, :module, :action, :description)
            on conflict (code) do update
              set module = excluded.module,
                  action = excluded.action,
                  description = excluded.description
            """,
            {
                "code": entry.code,
                "module": entry.module,
                "action": entry.action,
                "description": entry.description,
            },
        )
        for entry in PERMISSION_CATALOG
    ]


def _rewrite_definition_statements() -> list[tuple[str, dict[str, Any]]]:
    """整表重写两张定义表（代码的副本，允许覆盖）。"""

    statements: list[tuple[str, dict[str, Any]]] = [
        ("delete from public.rbac_builtin_role_defaults", {}),
        ("delete from public.rbac_builtin_roles", {}),
    ]
    for role in (*TENANT_BUILTIN_ROLES, *PLATFORM_BUILTIN_ROLES):
        statements.append(
            (
                """
                insert into public.rbac_builtin_roles (realm, code, name)
                values (:realm, :code, :name)
                """,
                {"realm": role.realm, "code": role.code, "name": role.name},
            )
        )
        for permission_code in sorted(role.permissions):
            statements.append(
                (
                    """
                    insert into public.rbac_builtin_role_defaults (realm, role_code, permission_code)
                    values (:realm, :role_code, :permission_code)
                    """,
                    {
                        "realm": role.realm,
                        "role_code": role.code,
                        "permission_code": permission_code,
                    },
                )
            )
    return statements


def _backfill_role_statements() -> list[tuple[str, dict[str, Any]]]:
    """按定义表补齐存量租户/平台侧的内置角色与其出厂默认。

    默认映射分两种口径（原因见 `seeds.py:FULL_SCOPE_ROLES`）：

    - **全量型角色**（两个超管）：矩阵把它们定义成「范围内全部权限」，按「只补不撤」对齐，
      新增权限码会自动落到它们身上；
    - **其余内置角色**：只在「角色还没有任何映射」时授一次出厂默认。映射入库后属于运营配置，
      人工撤销的权限绝不能被同步悄悄加回来。
    """

    tenant_full_codes = list(_full_scope_codes("tenant"))
    tenant_other_codes = sorted(
        role.code for role in TENANT_BUILTIN_ROLES if role.code not in tenant_full_codes
    )
    platform_full_codes = list(_full_scope_codes("platform"))
    platform_other_codes = sorted(
        role.code for role in PLATFORM_BUILTIN_ROLES if role.code not in platform_full_codes
    )

    return [
        # 租户内置角色：每个租户都要有
        (
            """
            insert into public.roles (id, tenant_id, code, name, is_system)
            select gen_random_uuid(), t.id, b.code, b.name, true
            from public.tenants t
            cross join public.rbac_builtin_roles b
            where b.realm = 'tenant'
              and not exists (
                  select 1 from public.roles r where r.tenant_id = t.id and r.code = b.code
              )
            """,
            {},
        ),
        # 平台内置角色
        (
            """
            insert into public.platform_roles (id, code, name, is_system)
            select gen_random_uuid(), b.code, b.name, true
            from public.rbac_builtin_roles b
            where b.realm = 'platform'
              and not exists (
                  select 1 from public.platform_roles pr where pr.code = b.code
              )
            """,
            {},
        ),
        # 租户侧：全量型角色按「只补不撤」对齐
        (
            """
            insert into public.role_permissions (id, tenant_id, role_id, permission_id)
            select gen_random_uuid(), r.tenant_id, r.id, p.id
            from public.roles r
            join public.rbac_builtin_role_defaults d
              on d.realm = 'tenant' and d.role_code = r.code
            join public.permissions p on p.code = d.permission_code
            where r.is_system
              and r.code = any(cast(:codes as text[]))
              and not exists (
                  select 1 from public.role_permissions rp
                  where rp.role_id = r.id and rp.permission_id = p.id
              )
            """,
            {"codes": tenant_full_codes},
        ),
        # 租户侧：其余内置角色只在还没有任何映射时授出厂默认
        (
            """
            insert into public.role_permissions (id, tenant_id, role_id, permission_id)
            select gen_random_uuid(), r.tenant_id, r.id, p.id
            from public.roles r
            join public.rbac_builtin_role_defaults d
              on d.realm = 'tenant' and d.role_code = r.code
            join public.permissions p on p.code = d.permission_code
            where r.is_system
              and r.code = any(cast(:codes as text[]))
              and not exists (
                  select 1 from public.role_permissions rp where rp.role_id = r.id
              )
            """,
            {"codes": tenant_other_codes},
        ),
        # 平台侧：同上两条口径
        (
            """
            insert into public.platform_role_permissions (id, platform_role_id, permission_id)
            select gen_random_uuid(), pr.id, p.id
            from public.platform_roles pr
            join public.rbac_builtin_role_defaults d
              on d.realm = 'platform' and d.role_code = pr.code
            join public.permissions p on p.code = d.permission_code
            where pr.code = any(cast(:codes as text[]))
              and not exists (
                  select 1 from public.platform_role_permissions prp
                  where prp.platform_role_id = pr.id and prp.permission_id = p.id
              )
            """,
            {"codes": platform_full_codes},
        ),
        (
            """
            insert into public.platform_role_permissions (id, platform_role_id, permission_id)
            select gen_random_uuid(), pr.id, p.id
            from public.platform_roles pr
            join public.rbac_builtin_role_defaults d
              on d.realm = 'platform' and d.role_code = pr.code
            join public.permissions p on p.code = d.permission_code
            where pr.code = any(cast(:codes as text[]))
              and not exists (
                  select 1 from public.platform_role_permissions prp
                  where prp.platform_role_id = pr.id
              )
            """,
            {"codes": platform_other_codes},
        ),
    ]


def build_sync_statements() -> list[tuple[str, dict[str, Any]]]:
    """完整的幂等同步语句（顺序：清旧码 → 补目录 → 重写定义表 → 补缺角色与映射）。

    异常:
        RuntimeError：种子与目录不自洽（`_self_check_seeds`）。
    """

    _self_check_seeds()
    return [
        *_delete_legacy_permission_statements(),
        *_upsert_permission_statements(),
        *_rewrite_definition_statements(),
        *_backfill_role_statements(),
    ]


def conflicting_roles(rows: list[dict[str, Any]]) -> list[str]:
    """把前置检查的结果整理成可读冲突项（空列表=可以安全同步）。"""

    return [f"{row['code']}@{row['tenant_id']}" for row in rows]


def legacy_grant_conflicts(rows: Sequence[dict[str, Any]]) -> list[str]:
    """仍被"非全量型角色"引用的旧码清单（空列表=可以安全删除口径 ⑧ 的 9 个旧码）。"""

    return [f"{row['permission_code']} -> {row['role_code']}@{row['tenant_id']}" for row in rows]


def assert_no_legacy_grants(rows: Sequence[dict[str, Any]]) -> None:
    """旧码仍被运营配置引用时 fail-fast，不静默删映射（口径 ⑧ 的删除前提）。

    异常:
        RuntimeError：存在需要人工改授的旧码引用。
    """

    conflicts = legacy_grant_conflicts(rows)
    if conflicts:
        raise RuntimeError(
            "口径 ⑧ 的旧码仍被非全量型角色引用，删除会让这些角色凭空掉权限；"
            "请先按 `docs/rbac-matrix.md` 人工改授再同步：" + ", ".join(conflicts)
        )


def assert_no_conflicting_roles(rows: list[dict[str, Any]]) -> None:
    """内置角色码撞上运营自建（非系统）角色时 fail-fast，不动人工数据。

    异常:
        RuntimeError：存在同名非系统角色。
    """

    conflicts = conflicting_roles(rows)
    if conflicts:
        raise RuntimeError(
            "存在与内置角色同名的非系统角色，同步会覆盖运营配置，请先人工处理："
            + ", ".join(conflicts)
        )


def _leaked_tenant_platform_grants(rows: Sequence[Any]) -> list[str]:
    return [f"{row[0]} -> {row[1]}" for row in rows]


def sync_rbac_definitions_sync(bind: Connection) -> int:
    """同步版本（Alembic 迁移用）：与异步版执行同一份语句、同一套守卫。

    返回值：
        实际执行的语句数。

    异常:
        RuntimeError：存在同名非系统角色、旧码仍被非全量型角色引用，
            或同步后租户角色仍引用 `school`/`campus` 域外的码。
    """

    rows = bind.execute(text(CONFLICTING_ROLE_CHECK_SQL), {"codes": list(BUILTIN_ROLE_CODES)})
    assert_no_conflicting_roles([dict(row) for row in rows.mappings()])
    assert_no_legacy_grants(_legacy_grant_rows_sync(bind))

    statements = build_sync_statements()
    for sql, params in statements:
        bind.execute(text(sql), params)

    leaked = bind.execute(
        text(TENANT_PLATFORM_LEAK_CHECK_SQL), _tenant_leak_check_params()
    ).all()
    leaks = _leaked_tenant_platform_grants(leaked)
    if leaks:
        raise RuntimeError("同步后租户角色仍引用平台域权限：" + ", ".join(leaks))
    return len(statements)


def _tenant_leak_check_params() -> dict[str, Any]:
    """越权守卫的绑定参数：租户域模块白名单直接取自 `catalog.TENANT_MODULES`。"""

    return {"tenant_modules": sorted(TENANT_MODULES)}


def _legacy_grant_guard_params() -> dict[str, Any]:
    """口径 ⑧ 闸门的绑定参数：待删旧码 + 允许引用它们的全量型角色码（来自 `FULL_SCOPE_ROLES`）。"""

    return {
        "codes": list(legacy_codes_removed_by_m1e1()),
        "full_codes": [*_full_scope_codes("tenant"), *_full_scope_codes("platform")],
    }


def _legacy_grant_rows_sync(bind: Connection) -> list[dict[str, Any]]:
    """仍引用口径 ⑧ 旧码的角色清单（排除删了也不丢能力的全量型角色）。"""

    result = bind.execute(text(LEGACY_GRANT_GUARD_SQL), _legacy_grant_guard_params())
    return [dict(row) for row in result.mappings()]


async def sync_rbac_definitions(session: AsyncSession) -> int:
    """执行幂等同步（权限目录 upsert + 旧码删除 + 内置角色与出厂默认补齐）。

    返回值：
        实际执行的语句数（供调用方日志与测试断言"确实跑过一次"）。

    异常:
        RuntimeError：种子与目录不自洽、存在同名的运营自建角色、旧码仍被非全量型角色引用，
            或同步后租户角色仍引用 `school`/`campus` 域外的码（守卫，不静默放行）。
    """

    rows = (
        await session.execute(text(CONFLICTING_ROLE_CHECK_SQL), {"codes": list(BUILTIN_ROLE_CODES)})
    ).mappings()
    assert_no_conflicting_roles([dict(row) for row in rows])
    legacy_rows = await session.execute(text(LEGACY_GRANT_GUARD_SQL), _legacy_grant_guard_params())
    assert_no_legacy_grants([dict(row) for row in legacy_rows.mappings()])

    statements = build_sync_statements()
    for sql, params in statements:
        await session.execute(text(sql), params)

    leaked_rows = (
        await session.execute(text(TENANT_PLATFORM_LEAK_CHECK_SQL), _tenant_leak_check_params())
    ).all()
    leaks = _leaked_tenant_platform_grants(leaked_rows)
    if leaks:
        raise RuntimeError("同步后租户角色仍引用平台域权限：" + ", ".join(leaks))
    return len(statements)


__all__ = [
    "BUILTIN_ROLE_CODES",
    "CONFLICTING_ROLE_CHECK_SQL",
    "LEGACY_GRANT_GUARD_SQL",
    "PLATFORM_BUILTIN_ROLES",
    "TENANT_BUILTIN_ROLES",
    "TENANT_PLATFORM_LEAK_CHECK_SQL",
    "BuiltinRole",
    "assert_no_conflicting_roles",
    "assert_no_legacy_grants",
    "build_sync_statements",
    "conflicting_roles",
    "legacy_grant_conflicts",
    "sync_rbac_definitions",
    "sync_rbac_definitions_sync",
]
