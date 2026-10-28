"""用户↔多角色绑定的服务层不变量（M2-UI3 TD-187d）。

业务场景（`tests/business/test_bs_m2ui3_custom_role.py`）走 HTTP，请求天然串行；这里补四类
**只有服务层/并发才成立**的性质：

1. 落库形状：全量替换写成 `user_roles` 的精确行集、`users.roles_version` 每次 +1，
   返回的 diff 与审计 diff 都是"同一份数据"（不是让调用方拿两个时刻的快照去猜）；
2. 自绑禁令在任何数据库读写之前生效：它不依赖目标存在，因此连"用户不存在"的 404 都不该覆盖它；
3. 目标保护分层：学生 422、超管账号 403、超管角色 422、跨租 user/role 404、`deleted` 404，
   而 `locked`/`disabled` 仍可绑（绑定 ≠ 允许登录）——每类拒绝都留痕，且锁文件之外一个字节都没变；
4. 版本 CAS：两个人拿同一个 `roles_version` 并发改绑，恰好一个成功、另一个 409 并拿到最新快照，
   库里不会出现"两边的写入各生效一半"（`FOR UPDATE` 行锁 + 版本比较在同一事务内）；
5. 与角色删除的并发：绑定在角色行上持 `FOR KEY SHARE`，删除先提交时绑定方读到 0 行 →
   404 + 拒绝审计，而不是在插入处撞外键变成 500。

不测 `user_roles` 的 FK/RLS 兜底：那些由迁移与 `test_rls_policy_coverage.py` 覆盖，
这里再造一遍只会得到"由库拒绝"而不是"由服务层拒绝"的断言，错误码会分叉。
"""

import asyncio
import os
import time
from collections.abc import AsyncIterator, Coroutine, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth.principal import Principal
from app.config import get_settings
from app.db.session import db_session, dispose_engine
from app.rbac import admin
from app.rbac.admin import RoleSummary
from app.rbac.binding import (
    BindingAdminError,
    BindingUpdate,
    get_user_bindings,
    replace_user_bindings,
)
from tests.support.auth import seed_tenant_user
from tests.support.db import clean_test_data, database_url, make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn

pytestmark = pytest.mark.integration

PASSWORD = "Binding-Wiring-Pass-1"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def stack(migrated_database: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
    finally:
        await admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    get_settings.cache_clear()
    yield
    await dispose_engine()
    get_settings.cache_clear()


async def _seed(school_code: str) -> dict[str, Any]:
    """租户 + 内置角色 + 超管/馆员（school_admin 类型）/学生，并建一把自定义角色。"""

    engine = make_test_engine("admin")
    try:
        tenant_id, super_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"bind-super-{school_code}",
            password=PASSWORD,
            user_type="school_admin",
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text("select seed_builtin_roles(:tid)"), {"tid": str(tenant_id)}
            )
            await session.execute(
                text(
                    "insert into public.user_roles (tenant_id, user_id, role_id) "
                    "select :tid, :uid, r.id from public.roles r "
                    "where r.tenant_id = :tid and r.code = 'school_super_admin'"
                ),
                {"tid": str(tenant_id), "uid": str(super_id)},
            )
        _, keeper_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"bind-keeper-{school_code}",
            password=PASSWORD,
            user_type="school_admin",
        )
        _, student_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"bind-student-{school_code}",
            password=PASSWORD,
            user_type="student",
        )
        # 非超管的操作者：用于"目标为超管"这类必须由**别人**发起的拒绝
        # （超管改自己的绑定会先撞自绑禁令，测不到目标保护）。
        _, operator_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"bind-operator-{school_code}",
            password=PASSWORD,
            user_type="school_admin",
        )
        foreign_tenant_id, foreign_user_id = await seed_tenant_user(
            engine,
            school_code=f"{school_code}X",
            login_id=f"bind-foreign-{school_code}",
            password=PASSWORD,
            user_type="school_admin",
        )
        async with factory() as session, session.begin():
            await session.execute(
                text("select seed_builtin_roles(:tid)"), {"tid": str(foreign_tenant_id)}
            )
    finally:
        await engine.dispose()

    principal = Principal(
        user_id=super_id,
        realm="tenant",
        tenant_id=tenant_id,
        jti=uuid4(),
        session_id=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        must_change_password=False,
        roles=frozenset({"school_super_admin"}),
        permissions=frozenset({"school.permission_account.manage", "school.role.manage"}),
    )
    role = await admin.create_role(
        principal,
        code="library_keeper",
        name="图书馆管理员",
        permission_codes=["school.kb.view"],
        copy_from_role_id=None,
        reason=None,
        request_id=None,
    )
    operator = Principal(
        user_id=operator_id,
        realm="tenant",
        tenant_id=tenant_id,
        jti=uuid4(),
        session_id=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        must_change_password=False,
        roles=frozenset(),
        permissions=frozenset({"school.permission_account.manage"}),
    )
    return {
        "tenant_id": tenant_id,
        "super_id": super_id,
        "keeper_id": keeper_id,
        "student_id": student_id,
        "operator_id": operator_id,
        "operator": operator,
        "foreign_tenant_id": foreign_tenant_id,
        "foreign_user_id": foreign_user_id,
        "principal": principal,
        "role": role,
    }


async def _role_id_by_code(tenant_id: UUID, code: str) -> UUID:
    async with db_session(tenant_id=tenant_id) as session:
        value = await session.scalar(
            text("select id from public.roles where tenant_id = :tid and code = :code"),
            {"tid": tenant_id, "code": code},
        )
    assert value is not None, code
    return UUID(str(value))


async def _user_roles(tenant_id: UUID, user_id: UUID) -> list[str]:
    """按码集返回该账号的绑定（顺序无关），并顺带检查没有跨租残留。"""

    async with db_session(tenant_id=tenant_id) as session:
        rows: list[Any] = list(
            (
                await session.execute(
                    text(
                        "select r.code from public.user_roles ur "
                        "join public.roles r on r.id = ur.role_id and r.tenant_id = ur.tenant_id "
                        "where ur.tenant_id = :tid and ur.user_id = :uid"
                    ),
                    {"tid": tenant_id, "uid": user_id},
                )
            )
            .scalars()
            .all()
        )
    return sorted(str(item) for item in rows)


async def _roles_version(tenant_id: UUID, user_id: UUID) -> int:
    async with db_session(tenant_id=tenant_id) as session:
        value = await session.scalar(
            text("select roles_version from public.users where tenant_id = :tid and id = :uid"),
            {"tid": tenant_id, "uid": user_id},
        )
    assert value is not None
    return int(value)


async def _audits(action: str) -> list[dict[str, Any]]:
    async with db_session(platform=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select result, resource_id, metadata_json from public.audit_logs "
                        "where action = :action order by sequence"
                    ),
                    {"action": action},
                )
            )
            .mappings()
            .all()
        )
    return [
        {
            "result": str(row["result"]),
            "resource_id": None if row["resource_id"] is None else str(row["resource_id"]),
            "metadata": dict(row["metadata_json"] or {}),
        }
        for row in rows
    ]


async def _replace(
    seeded: dict[str, Any],
    user_id: UUID,
    role_ids: list[UUID],
    *,
    version: int,
    reason: str = "绑定回归（集成用例）",
    principal: Principal | None = None,
) -> BindingUpdate:
    return await replace_user_bindings(
        principal or seeded["principal"],
        user_id,
        role_ids=role_ids,
        version=version,
        reason=reason,
        request_id=None,
    )


async def test_replace_bindings_writes_exact_rows_and_a_restorable_diff(stack: None) -> None:
    """全量替换：行集精确、版本 +1、diff 与审计可还原"之前是什么、之后是什么"。"""

    del stack
    seeded = await _seed(f"SCH-BIND{uuid4().hex[:6].upper()}")
    keeper = seeded["keeper_id"]
    role_id = seeded["role"].id
    tenant_id = seeded["tenant_id"]

    first = await _replace(seeded, keeper, [role_id], version=0)
    assert [item.code for item in first.bindings.roles] == ["library_keeper"]
    assert first.bindings.permission_codes == ("school.kb.view",)
    assert first.bindings.roles_version == 1
    assert first.added_role_codes == ("library_keeper",)
    assert first.removed_role_codes == ()
    assert first.added_permission_codes == ("school.kb.view",)
    assert first.removed_permission_codes == ()
    assert await _user_roles(tenant_id, keeper) == ["library_keeper"]
    assert await _roles_version(tenant_id, keeper) == 1

    # 重复提交同一集合：合法（幂等语义由前端保证），版本仍 +1 —— 版本是"写入次数"不是"内容版本"
    same = await _replace(seeded, keeper, [role_id, role_id], version=1)
    assert same.added_role_codes == () and same.removed_role_codes == ()
    assert same.bindings.roles_version == 2
    assert await _user_roles(tenant_id, keeper) == ["library_keeper"]

    cleared = await _replace(seeded, keeper, [], version=2)
    assert cleared.bindings.roles == ()
    assert cleared.bindings.permission_codes == ()
    assert cleared.removed_role_codes == ("library_keeper",)
    assert cleared.removed_permission_codes == ("school.kb.view",)
    assert await _user_roles(tenant_id, keeper) == []
    assert await _roles_version(tenant_id, keeper) == 3

    audits = await _audits("school.user.roles.updated")
    assert len(audits) == 3, audits
    assert [entry["resource_id"] for entry in audits] == [str(keeper)] * 3
    metadata = audits[2]["metadata"]
    # 审计里的"之前/之后"必须能还原这次变更：绑定行已删，审计是唯一来源
    assert metadata["role_codes_before"] == ["library_keeper"]
    assert metadata["role_codes_after"] == []
    assert metadata["removed_permission_codes"] == ["school.kb.view"]
    assert metadata["reason"] == "绑定回归（集成用例）"


async def test_self_ban_precedes_every_database_lookup(stack: None) -> None:
    """自绑 403 先于目标存在性判断：连"这条 user_id 根本不存在"也不该返回 404。

    若把自绑检查放到查询之后，攻击者可以用"404 vs 403"区分"这个 id 是不是我自己"，
    更糟的是：把检查挂在"目标已存在"分支上，未来任何一次重构都可能让它随分支一起消失。
    """

    del stack
    seeded = await _seed(f"SCH-BIND{uuid4().hex[:6].upper()}")
    with pytest.raises(BindingAdminError) as caught:
        await _replace(seeded, seeded["super_id"], [], version=0)
    assert (caught.value.code, caught.value.http_status) == ("BINDING_SELF_FORBIDDEN", 403)

    # 不存在的 id 与自己是同一个值？不可能；这里额外证明"自绑判定不看库"：
    # 用一个库里不存在的 id 冒充自己（构造一个新的 Principal）也能拿到同一个 403。
    ghost_principal = Principal(
        user_id=uuid4(),
        realm="tenant",
        tenant_id=seeded["tenant_id"],
        jti=uuid4(),
        session_id=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        must_change_password=False,
        roles=frozenset(),
        permissions=frozenset({"school.permission_account.manage"}),
    )
    with pytest.raises(BindingAdminError) as caught:
        await replace_user_bindings(
            ghost_principal,
            ghost_principal.user_id,
            role_ids=[],
            version=0,
            reason=None,
            request_id=None,
        )
    assert caught.value.code == "BINDING_SELF_FORBIDDEN"

    denied = await _audits("school.user.roles.denied")
    assert [(entry["result"], entry["metadata"].get("reason")) for entry in denied] == [
        ("denied", "BINDING_SELF_FORBIDDEN"),
        ("denied", "BINDING_SELF_FORBIDDEN"),
    ]
    assert await _audits("school.user.roles.updated") == []


async def test_target_and_role_protections_are_layered_and_audited(stack: None) -> None:
    """四类保护各自的错误码与落点；跨租一律 404（不泄露存在性）。"""

    del stack
    seeded = await _seed(f"SCH-BIND{uuid4().hex[:6].upper()}")
    tenant_id = seeded["tenant_id"]
    super_role_id = await _role_id_by_code(tenant_id, "school_super_admin")
    admin_role_id = await _role_id_by_code(tenant_id, "school_admin")
    foreign_role_id = await _role_id_by_code(seeded["foreign_tenant_id"], "school_admin")

    # 第 2 例必须由非超管发起：超管改自己的绑定会先撞自绑禁令，目标保护就测不到了。
    cases = [
        (seeded["student_id"], [admin_role_id], 0, "BINDING_TARGET_NOT_ALLOWED", 422, None),
        (
            seeded["super_id"],
            [admin_role_id],
            0,
            "BINDING_SUPER_ADMIN_FORBIDDEN",
            403,
            seeded["operator"],
        ),
        (seeded["keeper_id"], [super_role_id], 0, "ROLE_NOT_BINDABLE", 422, None),
        (uuid4(), [admin_role_id], 0, "USER_NOT_FOUND", 404, None),
        (seeded["foreign_user_id"], [admin_role_id], 0, "USER_NOT_FOUND", 404, None),
        (seeded["keeper_id"], [foreign_role_id], 0, "ROLE_NOT_FOUND", 404, None),
        (seeded["keeper_id"], [uuid4()], 0, "ROLE_NOT_FOUND", 404, None),
    ]
    for user_id, role_ids, version, code, status_code, actor in cases:
        with pytest.raises(BindingAdminError) as caught:
            await _replace(seeded, user_id, role_ids, version=version, principal=actor)
        assert (caught.value.code, caught.value.http_status) == (code, status_code), code

    # 一个绑定都没落地，也没推版本
    assert await _user_roles(tenant_id, seeded["keeper_id"]) == []
    assert await _user_roles(tenant_id, seeded["super_id"]) == ["school_super_admin"]
    assert await _roles_version(tenant_id, seeded["keeper_id"]) == 0
    assert await _roles_version(tenant_id, seeded["super_id"]) == 0
    assert await _audits("school.user.roles.updated") == []

    denied = await _audits("school.user.roles.denied")
    assert [entry["metadata"].get("reason") for entry in denied] == [case[3] for case in cases]
    # 目标类型与提交的角色 id 各带一份上下文：排障时不必再猜"当时提交了什么"
    assert denied[0]["metadata"]["target_user_type"] == "student"
    assert denied[5]["metadata"]["submitted_role_ids"] == [str(foreign_role_id)]
    assert denied[6]["metadata"]["submitted_role_ids"] == [str(cases[6][1][0])]


async def test_deleted_target_is_404_and_a_locked_target_stays_bindable(stack: None) -> None:
    """决策 D#2：`deleted` 与不存在同形 404；`locked`/`disabled` 照常可绑。

    「停用账号先配好角色再启用」是正常流程，`locked` 只是临时状态——绑定表达的是"该账号
    应该有哪些能力"，不是"现在允许登录"。把非 active 一律拒掉会把这条运维路径堵死，
    而真正该拦的是 `deleted`（账号已进入删除流程，再写绑定只会留下孤儿配置）。
    """

    del stack
    seeded = await _seed(f"SCH-BIND{uuid4().hex[:6].upper()}")
    tenant_id = seeded["tenant_id"]
    keeper = seeded["keeper_id"]
    role_id = seeded["role"].id

    async with db_session(tenant_id=tenant_id) as session:
        await session.execute(
            text("update public.users set status = 'locked' where id = :uid and tenant_id = :tid"),
            {"uid": keeper, "tid": tenant_id},
        )
    locked = await _replace(seeded, keeper, [role_id], version=0)
    assert locked.bindings.status == "locked"
    assert [item.code for item in locked.bindings.roles] == ["library_keeper"]

    async with db_session(tenant_id=tenant_id) as session:
        await session.execute(
            text("update public.users set status = 'deleted' where id = :uid and tenant_id = :tid"),
            {"uid": keeper, "tid": tenant_id},
        )
    with pytest.raises(BindingAdminError) as caught:
        await get_user_bindings(seeded["principal"], keeper)
    assert (caught.value.code, caught.value.http_status) == ("USER_NOT_FOUND", 404)
    with pytest.raises(BindingAdminError) as caught:
        await _replace(seeded, keeper, [], version=1)
    assert (caught.value.code, caught.value.http_status) == ("USER_NOT_FOUND", 404)

    assert await _user_roles(tenant_id, keeper) == ["library_keeper"], "已删除账号的绑定不该被 404 路径改动"
    assert await _roles_version(tenant_id, keeper) == 1
    assert [entry["metadata"]["reason"] for entry in await _audits("school.user.roles.denied")] == [
        "USER_NOT_FOUND"
    ]


async def test_stale_version_returns_latest_snapshot_without_writing(stack: None) -> None:
    """版本落后 → 409 + 最新快照；不写绑定、不推版本、不写"已更新"审计。"""

    del stack
    seeded = await _seed(f"SCH-BIND{uuid4().hex[:6].upper()}")
    keeper = seeded["keeper_id"]
    tenant_id = seeded["tenant_id"]
    await _replace(seeded, keeper, [seeded["role"].id], version=0)

    with pytest.raises(BindingAdminError) as caught:
        await _replace(seeded, keeper, [], version=0)
    assert (caught.value.code, caught.value.http_status) == ("BINDING_VERSION_CONFLICT", 409)
    latest = caught.value.latest
    assert latest is not None
    assert latest.roles_version == 1
    assert [item.code for item in latest.roles] == ["library_keeper"]

    assert await _user_roles(tenant_id, keeper) == ["library_keeper"]
    assert await _roles_version(tenant_id, keeper) == 1
    assert len(await _audits("school.user.roles.updated")) == 1
    assert await _audits("school.user.roles.denied") == []


async def test_concurrent_replaces_with_the_same_version_land_exactly_one(
    stack: None,
) -> None:
    """同一 `roles_version` 的两路并发：一路成功、一路 409，最终行集只可能是其中一路的提交。

    `FOR UPDATE` 行锁让第二路在拿到锁时已经看到 version+1，于是走进版本分支而不是写入；
    若锁被去掉，两路都能通过版本比较，`delete + insert` 交错后可能出现"集合来自 A、版本来自 B"
    的半提交——那时 diff 审计也会与库不一致。
    """

    del stack
    seeded = await _seed(f"SCH-BIND{uuid4().hex[:6].upper()}")
    keeper = seeded["keeper_id"]
    tenant_id = seeded["tenant_id"]
    role_a = seeded["role"].id
    role_b = (await _create_second_role(seeded, "reading_room")).id

    async def _settle(coro: Coroutine[Any, Any, Any]) -> Any:
        try:
            return await coro
        except BindingAdminError as exc:
            return exc

    outcomes = list(
        await asyncio.gather(
            _settle(_replace(seeded, keeper, [role_a], version=0)),
            _settle(_replace(seeded, keeper, [role_b], version=0)),
        )
    )
    winners = [item for item in outcomes if isinstance(item, BindingUpdate)]
    losers = [item for item in outcomes if isinstance(item, BindingAdminError)]
    assert len(winners) == 1, outcomes
    assert [(item.code, item.http_status) for item in losers] == [
        ("BINDING_VERSION_CONFLICT", 409)
    ], losers

    winning_code = winners[0].bindings.roles[0].code
    assert await _user_roles(tenant_id, keeper) == [winning_code], "库里的行集必须只来自赢家"
    assert await _roles_version(tenant_id, keeper) == 1
    audits = await _audits("school.user.roles.updated")
    assert len(audits) == 1
    assert audits[0]["metadata"]["role_codes_after"] == [winning_code]


async def _create_second_role(seeded: dict[str, Any], code: str) -> RoleSummary:
    return await admin.create_role(
        seeded["principal"],
        code=code,
        name="阅览室管理员",
        permission_codes=["school.kb.view"],
        copy_from_role_id=None,
        reason=None,
        request_id=None,
    )


async def test_get_bindings_uses_the_same_read_path_as_the_principal(stack: None) -> None:
    """GET 的权限并集与 `Principal` 解析同源：绑定后并集里出现角色的码，解绑后消失。"""

    del stack
    seeded = await _seed(f"SCH-BIND{uuid4().hex[:6].upper()}")
    keeper = seeded["keeper_id"]

    empty = await get_user_bindings(seeded["principal"], keeper)
    assert empty.roles == () and empty.permission_codes == () and empty.roles_version == 0

    await _replace(seeded, keeper, [seeded["role"].id], version=0)
    filled = await get_user_bindings(seeded["principal"], keeper)
    assert filled.permission_codes == ("school.kb.view",)
    assert filled.user_type == "school_admin" and filled.status == "active"

    with pytest.raises(BindingAdminError) as caught:
        await get_user_bindings(seeded["principal"], seeded["foreign_user_id"])
    assert (caught.value.code, caught.value.http_status) == ("USER_NOT_FOUND", 404)


async def _wait_until_binding_waits_on_the_operator(
    engine: Any, holder_pid: int, *, timeout: float = 5.0
) -> None:
    """等到某个会话被**本用例的事务**（`holder_pid`）挡住。

    用"观察到等待"而不是固定 sleep 判"绑定已走到角色读取"：任务若在删除提交之后才开始，
    它会读到 0 行直接 404，用例就变成在验证另一条路径（假绿）。判据取 `pg_blocking_pids()`
    而不是匹配语句文本——被挡住的可能是 `FOR KEY SHARE`（有锁时）也可能是 INSERT 的外键检查
    （锁被移除时），两种都算"绑定卡在删除方这一侧"；配合下面 `assert not task.done()` 与最终的
    404 断言，被移除的 `FOR KEY SHARE` 会让任务以 `IntegrityError` 收场而如实转红。
    """

    probe = text(
        "select pid, state, wait_event_type, wait_event, "
        "pg_blocking_pids(pid) as blockers, query "
        "from pg_stat_activity "
        "where datname = current_database() and pid <> pg_backend_pid()"
    )
    deadline = time.monotonic() + timeout
    rows: list[dict[str, Any]] = []
    async with engine.connect() as conn:
        while time.monotonic() < deadline:
            rows = [dict(row) for row in (await conn.execute(probe)).mappings().all()]
            if any(holder_pid in row["blockers"] for row in rows):
                return
            await asyncio.sleep(0.02)
    raise AssertionError(f"没有会话在等本用例的角色行锁；pg_stat_activity={rows}")


async def test_role_deleted_mid_binding_yields_404_instead_of_a_foreign_key_error(
    stack: None,
) -> None:
    """并发删角色与绑定同一角色：角色的行锁先落到删除方，绑定必须拿到 404 而不是外键 500。

    `_roles_by_ids` 的 `FOR KEY SHARE` 与 `user_roles` 插入所需的外键锁同档，于是这个窗口
    被封住：删除先完成时绑定读到 0 行（404 + 拒绝审计）；删除晚到时它会被挡到绑定提交之后，
    由删除侧的影响面指纹兜住（见 `test_rbac_custom_role_create_race.py` 的两个指纹用例）。
    没有这把锁时，绑定会一路走到 INSERT 才被那条外键挡住，然后以 `IntegrityError` 收场——
    既不是 `{code,message}` 的 404，也没有拒绝审计。
    """

    del stack
    seeded = await _seed(f"SCH-BIND{uuid4().hex[:6].upper()}")
    tenant_id = seeded["tenant_id"]
    keeper = seeded["keeper_id"]
    role_id = seeded["role"].id

    async def _outcome() -> Any:
        try:
            return await _replace(seeded, keeper, [role_id], version=0)
        except BindingAdminError as exc:
            return exc

    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            holder_pid = int(await session.scalar(text("select pg_backend_pid()")))
            await session.execute(
                text(
                    "select id from public.roles "
                    "where id = :rid and tenant_id = :tid for update"
                ),
                {"rid": role_id, "tid": tenant_id},
            )
            task = asyncio.create_task(_outcome())
            await _wait_until_binding_waits_on_the_operator(engine, holder_pid)
            assert not task.done(), "绑定应被角色行锁挡住"
            await session.execute(
                text("delete from public.roles where id = :rid and tenant_id = :tid"),
                {"rid": role_id, "tid": tenant_id},
            )
        outcome = await asyncio.wait_for(task, timeout=10)
    finally:
        await engine.dispose()

    assert isinstance(outcome, BindingAdminError), outcome
    assert (outcome.code, outcome.http_status) == ("ROLE_NOT_FOUND", 404)
    assert await _user_roles(tenant_id, keeper) == []
    assert await _roles_version(tenant_id, keeper) == 0
    denied = await _audits("school.user.roles.denied")
    assert [entry["metadata"]["reason"] for entry in denied] == ["ROLE_NOT_FOUND"]
