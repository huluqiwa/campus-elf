"""运行时数据库自检（TD-073）。

生产/预发启动时验证：
1. **租户与平台两个连接**的角色都不是超级用户、没有 BYPASSRLS（否则 RLS 形同虚设）；
2. 所有租户级表逐表启用 FORCE RLS，且至少一条策略引用租户/平台上下文；
3. 审核函数 owner 具备超级用户或 BYPASSRLS（SECURITY DEFINER 在 FORCE RLS 下需要）；
4. 未设置租户上下文时租户数据不可见（fail-closed 探针）。

失败即拒绝启动，避免"带病上线"。
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import db_session
from app.rbac.catalog import TENANT_MODULES

# 租户级表（含 tenants 自身）；audit_logs 允许 tenant_id 为空但有租户/平台策略
REQUIRED_RLS_TABLES: tuple[str, ...] = (
    "tenants",
    "users",
    "roles",
    "role_permissions",
    "user_roles",
    "data_scopes",
    "refresh_tokens",
    "password_history",
    "password_reset_tokens",
    "mfa_recovery_codes",
    "audit_logs",
)


class DatabaseSelfCheckError(RuntimeError):
    """数据库安全基线不满足（拒绝启动）。"""


async def _check_role(session: AsyncSession, label: str) -> None:
    """校验当前连接角色不可绕过 RLS。"""

    role = (
        (
            await session.execute(
                text(
                    "select current_user as role_name, r.rolsuper, r.rolbypassrls "
                    "from pg_roles r where r.rolname = current_user"
                )
            )
        )
        .mappings()
        .one()
    )
    if role["rolsuper"] or role["rolbypassrls"]:
        raise DatabaseSelfCheckError(
            f"{label}连接角色 {role['role_name']} 具有超级用户/BYPASSRLS，禁止启动"
        )


async def _check_forced_rls(session: AsyncSession) -> None:
    """逐表校验 FORCE RLS、策略数量、策略表达式与策略角色（禁止宽松策略放行）。

    只用"存在一条合规策略"判定会被 `CREATE POLICY ... USING (true)` 绕过（M24）：
    因此要求该表**所有**策略都引用租户/平台上下文，且只能授予两个应用组角色。
    """

    rows = (
        (
            await session.execute(
                text(
                    """
                select c.relname,
                       c.relrowsecurity,
                       c.relforcerowsecurity,
                       count(p.policyname) as policy_count,
                       count(*) filter (
                           where p.policyname is not null
                             and not (
                                 coalesce(p.qual, '') || ' ' || coalesce(p.with_check, '')
                                 ~ '(app\.tenant_id|app\.platform_context)'
                             )
                       ) as policy_without_context,
                       count(*) filter (
                           where p.policyname is not null
                             and exists (
                                 select 1 from unnest(p.roles) as policy_role
                                 where policy_role not in
                                     ('campus_elf_tenant', 'campus_elf_platform')
                             )
                       ) as policy_with_other_role
                from pg_class c
                join pg_namespace n on n.oid = c.relnamespace
                left join pg_policies p
                    on p.schemaname = 'public' and p.tablename = c.relname
                where n.nspname = 'public' and c.relkind = 'r'
                  and c.relname = any(:tables)
                group by c.relname, c.relrowsecurity, c.relforcerowsecurity
                """
                ),
                {"tables": list(REQUIRED_RLS_TABLES)},
            )
        )
        .mappings()
        .all()
    )

    found = {row["relname"] for row in rows}
    missing = set(REQUIRED_RLS_TABLES) - found
    if missing:
        raise DatabaseSelfCheckError(f"缺少租户级表: {', '.join(sorted(missing))}")

    problems: list[str] = []
    for row in rows:
        if not row["relrowsecurity"] or not row["relforcerowsecurity"]:
            problems.append(f"{row['relname']} 未启用 FORCE RLS")
        if row["policy_count"] == 0:
            problems.append(f"{row['relname']} 没有 RLS 策略")
            continue
        if row["policy_without_context"]:
            problems.append(
                f"{row['relname']} 有 {row['policy_without_context']} 条策略未引用租户/平台上下文"
            )
        if row["policy_with_other_role"]:
            problems.append(
                f"{row['relname']} 有 {row['policy_with_other_role']} 条策略授予了应用角色之外的"
                "角色（如 PUBLIC）"
            )
    if problems:
        raise DatabaseSelfCheckError("RLS 覆盖不完整：" + "；".join(problems))


async def _check_review_function_owner(session: AsyncSession) -> None:
    """SECURITY DEFINER 审核函数 owner 必须能绕过 FORCE RLS。"""

    row = (
        (
            await session.execute(
                text(
                    """
                select r.rolname, r.rolsuper, r.rolbypassrls
                from pg_proc p
                join pg_roles r on r.oid = p.proowner
                where p.proname = 'review_registration_request'
                limit 1
                """
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise DatabaseSelfCheckError("review_registration_request() 不存在（迁移未执行）")
    if not (row["rolsuper"] or row["rolbypassrls"]):
        raise DatabaseSelfCheckError(
            f"审核函数 owner {row['rolname']} 不具备超级用户/BYPASSRLS，开通会被 RLS 拒绝"
        )


async def _check_realm_permission_matrix() -> None:
    """逐租户校验：租户角色不得持有租户域（`school`/`campus`）外的权限（B1 第二道防线）。

    `role_permissions` 是 FORCE RLS 表，**没有租户上下文时一行都看不到**——因此不能用
    "无上下文的 count(*)" 取证（那恒为 0，等于空实现）。这里先经平台连接列出租户，
    再逐个在租户上下文内统计；平台侧不限制模块（平台超管按设计拥有全量权限）。

    判据用 `catalog.TENANT_MODULES` 而不是写死 `module = 'platform'`：`observability`/`eval`
    也属平台运维域（矩阵 §3.4 + 2026-10-23 定稿"不下放租户角色"），只按 `platform` 判分会让
    这两类码在被手工 INSERT 进租户角色后连启动自检都放行——而 DB 触发器同样只挡 `platform`
    （收紧见 TD-149），代码侧这三处（种子自检、同步守卫、本检查）是当前的全部防线。

    异常:
        DatabaseSelfCheckError: 发现租户角色映射了租户域外的权限。
    """

    async with db_session(platform=True) as platform_session:
        tenant_ids: list[UUID] = list(
            (await platform_session.execute(text("select id from tenants"))).scalars().all()
        )

    for tenant_id in tenant_ids:
        async with db_session(tenant_id=tenant_id) as scoped:
            violations = await scoped.scalar(
                text(
                    "select count(*) from role_permissions rp "
                    "join permissions p on p.id = rp.permission_id "
                    "where p.module <> all(cast(:tenant_modules as text[]))"
                ),
                {"tenant_modules": sorted(TENANT_MODULES)},
            )
        if violations:
            raise DatabaseSelfCheckError(
                f"租户 {tenant_id} 有 {violations} 条角色映射到租户域（school/campus）外的权限（realm 越界）"
            )


async def _check_registration_function_owner(session: AsyncSession) -> None:
    """注册函数同样在 FORCE RLS 下的 audit_logs/申请单内写入，owner 必须可绕过。"""

    row = (
        (
            await session.execute(
                text(
                    """
                select r.rolname, r.rolsuper, r.rolbypassrls
                from pg_proc p
                join pg_roles r on r.oid = p.proowner
                where p.proname = 'submit_tenant_registration'
                limit 1
                """
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise DatabaseSelfCheckError("submit_tenant_registration() 不存在（迁移未执行）")
    if not (row["rolsuper"] or row["rolbypassrls"]):
        raise DatabaseSelfCheckError(
            f"注册函数 owner {row['rolname']} 不具备超级用户/BYPASSRLS，注册会被 RLS 拒绝"
        )


async def _check_fail_closed(session: AsyncSession) -> None:
    """无租户上下文时，租户数据必须不可见（对非空表也有意义）。"""

    for table in ("users", "roles", "refresh_tokens"):
        visible = await session.scalar(
            text(f"select count(*) from {table}")  # nosec B608 - 表名来自常量元组
        )
        if visible:
            raise DatabaseSelfCheckError(f"未设置租户上下文却可读取 {table}，RLS 隔离失效")


async def verify_runtime_database() -> None:
    """执行运行时数据库自检（租户 + 平台两个连接）。

    异常:
        DatabaseSelfCheckError: 连接角色可绕过 RLS、FORCE RLS/策略缺失、
            审核函数 owner 不可绕过 RLS，或隔离失效。
    """

    async with db_session() as tenant_session:
        await _check_role(tenant_session, "租户")
        await _check_forced_rls(tenant_session)
        await _check_review_function_owner(tenant_session)
        await _check_registration_function_owner(tenant_session)
        await _check_realm_permission_matrix()
        await _check_fail_closed(tenant_session)

    async with db_session(platform=True) as platform_session:
        await _check_role(platform_session, "平台")
