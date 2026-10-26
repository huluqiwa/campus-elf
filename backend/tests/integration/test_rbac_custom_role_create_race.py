"""自定义角色创建/删除的服务层并发与跨租不变量（M2-UI3 TD-187b）。

业务场景文件（`tests/business/test_bs_m2ui3_custom_role.py`）走 HTTP，请求天然串行；
这里补两类**只有并发/多主体才成立**的性质，直接调服务层：

1. 同码并发建角色：接口层预检与 `INSERT` 之间有窗口，真正的兜底是 `uq_roles_tenant_id_code`。
   READ COMMITTED 下并发事务的预检都看不到对方未提交的行，所以必有一方走到 `INSERT` 撞唯一索引；
   要求是**恰好一把角色落地**、其余全部翻成人读 409 `ROLE_CODE_TAKEN`（不是 500），
   并且撞库那次的拒绝审计留得下——外层事务已中止，同事务写审计会一起消失。
2. 角色码唯一性是**租户内**的：两所学校各自建 `library_keeper` 都必须成功，
   谁也不因别人的角色码被挡、也不因别人的角色码被删（跨租读不到也撞不掉）。
3. 只有**同码唯一约束**才翻成人读 409：绕过接口层撞到其他完整性约束（格式 CHECK 等）时
   原样抛出，不把真故障伪装成"业务冲突"。
4. 删除影响面的 `affected_users` **不在版本 CAS 的保护范围内**：绑定关系不推进 `roles.version`，
   预览与确认之间同事再绑一个人，确认就是打在陈旧数字上（TD-199 的现状钉桩）。

不测 `PERMISSION_CATALOG_OUT_OF_SYNC` 那条条数重核：它与前置的 `_assert_codes_in_catalog`
在同一事务同一快照里，正常流程走不到，留作 FK 语义变更（`RESTRICT`）时的兜底
——与 `reset_role` 的条数重核同口径，都是"预览承诺 N 项就实种 N 项"的守卫。
`deleted.rowcount != 1`（RLS 的 `USING` 挡下 DELETE）那条同样不测，但理由是**造不出真场景**：
读侧 `SELECT … FOR UPDATE` 与写侧 `DELETE` 走同一策略的同一个 `USING` 表达式，一致策略下不可
能"看得见却删不动"，要命中只能 monkeypatch rowcount——那是测替身。这里改测第 4 条：影响面
数字的过期窗口，它是删除路径上唯一真实存在、且现有用例没覆盖的并发面。
"""

import asyncio
import os
from collections.abc import AsyncIterator, Coroutine, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.principal import Principal
from app.config import get_settings
from app.db.session import db_session, dispose_engine
from app.rbac import admin
from app.rbac.admin import RoleAdminError, RoleSummary
from tests.support.auth import seed_tenant_user
from tests.support.db import clean_test_data, database_url, make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn

pytestmark = pytest.mark.integration

ROLE_CODE = "library_keeper"
PASSWORD = "CustRole-Wiring-Pass-1"


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
    """把 `db_session` 指向测试租户连接（服务层自己开事务，与运行时同一条路径）。"""

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


async def _seed_tenant(school_code: str) -> dict[str, Any]:
    """租户 + 内置角色 + 一名持有 `school.role.manage`/`delete` 的超管主体（服务层直调，不登录）。"""

    engine = make_test_engine("admin")
    try:
        tenant_id, user_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"race-{school_code}",
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
                {"tid": str(tenant_id), "uid": str(user_id)},
            )
    finally:
        await engine.dispose()

    principal = Principal(
        user_id=user_id,
        realm="tenant",
        tenant_id=tenant_id,
        jti=uuid4(),
        session_id=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        must_change_password=False,
        roles=frozenset({"school_super_admin"}),
        permissions=frozenset({"school.role.manage", "school.role.delete"}),
    )
    return {"tenant_id": tenant_id, "user_id": user_id, "principal": principal}


async def _seed_extra_user(school_code: str, label: str, expected_tenant_id: UUID) -> UUID:
    """同租户再种一名用户（影响面用例需要"第二个人"）；`seed_tenant_user` 按 school_code 复用租户。"""

    engine = make_test_engine("admin")
    try:
        tenant_id, user_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"bind-{label}-{school_code}",
            password=PASSWORD,
            user_type="school_admin",
        )
    finally:
        await engine.dispose()
    assert tenant_id == expected_tenant_id, "没复用同校租户：绑定会种到别的学校，用例前提不成立"
    return user_id


async def _bind_user(tenant_id: UUID, user_id: UUID, role_id: UUID) -> None:
    """用 SQL 直接建绑定：本仓还没有绑定接口（TD-187d），这里模拟"同事在预览之后绑了人"。"""

    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "insert into public.user_roles (tenant_id, user_id, role_id) "
                    "values (:tid, :uid, :rid)"
                ),
                {"tid": str(tenant_id), "uid": str(user_id), "rid": str(role_id)},
            )
    finally:
        await engine.dispose()


async def _create(principal: Principal) -> RoleSummary:
    return await admin.create_role(
        principal,
        code=ROLE_CODE,
        name="图书馆管理员",
        permission_codes=["school.kb.view"],
        copy_from_role_id=None,
        reason="并发建角色（集成用例）",
        request_id=None,
    )


async def _gather(coroutines: list[Coroutine[Any, Any, Any]]) -> list[Any]:
    """并发跑并回收结果/异常：断言的是"最终库里只有一行"，不是"谁先抢到"。"""

    async def _settle(coro: Coroutine[Any, Any, Any]) -> Any:
        try:
            return await coro
        except RoleAdminError as exc:
            return exc

    return list(await asyncio.gather(*(_settle(coro) for coro in coroutines)))


async def _role_rows(tenant_id: UUID, code: str) -> list[dict[str, Any]]:
    """按租户上下文查同码角色：不带 tenant_id 过滤，让 RLS 自己证明"只看得见自己的"。"""

    async with db_session(tenant_id=tenant_id) as session:
        rows = (
            (
                await session.execute(
                    text("select id, tenant_id, is_system, version from public.roles where code = :code"),
                    {"code": code},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


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
            "resource_id": str(row["resource_id"]),
            "metadata": dict(row["metadata_json"] or {}),
        }
        for row in rows
    ]


async def _role_permissions(tenant_id: UUID, role_id: UUID) -> set[str]:
    async with db_session(tenant_id=tenant_id) as session:
        rows: list[Any] = list(
            (
                await session.execute(
                    text(
                        "select p.code from public.role_permissions rp "
                        "join public.permissions p on p.id = rp.permission_id "
                        "where rp.tenant_id = :tid and rp.role_id = :rid"
                    ),
                    {"tid": tenant_id, "rid": role_id},
                )
            )
            .scalars()
            .all()
        )
    return {str(code) for code in rows}


async def test_concurrent_same_code_creations_leave_exactly_one_role(stack: None) -> None:
    """三个并发建同名码的请求：恰好一把角色落地，其余全部 409 `ROLE_CODE_TAKEN` 且都留痕。"""

    del stack
    seeded = await _seed_tenant(f"SCH-RACE{uuid4().hex[:6].upper()}")
    principal: Principal = seeded["principal"]

    outcomes = await _gather([_create(principal) for _ in range(3)])
    succeeded = [item for item in outcomes if isinstance(item, RoleSummary)]
    failures = [item for item in outcomes if isinstance(item, RoleAdminError)]

    assert len(succeeded) == 1, f"并发下应恰好一个成功：{outcomes}"
    assert failures, "其余请求必须失败"
    assert {
        (item.code, item.http_status) for item in failures
    } == {("ROLE_CODE_TAKEN", 409)}, failures

    rows = await _role_rows(seeded["tenant_id"], ROLE_CODE)
    assert len(rows) == 1, f"库里同码角色应有 1 行，实际 {len(rows)}：唯一约束没兜住"
    assert rows[0]["is_system"] is False and int(rows[0]["version"]) == 0

    # 撞库那次的拒绝是在**已中止的事务**里发起的：审计走独立事务才留得下（外层回滚带不走它）
    denied = await _audits("school.role.create_denied")
    assert len(denied) == len(failures), f"每个拒绝都该留一条痕：{denied}"
    assert {entry["metadata"].get("reason") for entry in denied} == {"ROLE_CODE_TAKEN"}
    assert {entry["resource_id"] for entry in denied} == {ROLE_CODE}
    assert len(await _audits("school.role.created")) == 1


async def test_duplicate_committed_after_precheck_becomes_409_not_500(
    stack: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预检放行、`INSERT` 才撞唯一索引那条分支必须**稳定**覆盖，而不是靠并发赌一次。

    接口层的同码预检与 `INSERT` 之间有窗口：并发同事或一句手工 SQL 在窗口里提交同码行时，
    兜底是 `uq_roles_tenant_id_code`，而裸 `IntegrityError` 出到接口就是 500。
    上面那条并发用例能覆盖这条分支，但命中与否取决于机器快慢，CI 上会漂；这里把窗口显式打开：
    预检照常返回"没有"，返回后立刻用另一条连接提交一把同码角色——`INSERT` 必然撞库。
    顺带钉住"已中止事务里发起的拒绝审计仍然落库"（同事务写会跟着回滚一起消失）。
    """

    del stack
    seeded = await _seed_tenant(f"SCH-RACEW{uuid4().hex[:6].upper()}")
    principal: Principal = seeded["principal"]
    tenant_id = seeded["tenant_id"]

    real_scalar = AsyncSession.scalar
    slipped: dict[str, bool] = {"done": False}

    async def _insert_competing_role() -> None:
        engine = make_test_engine("admin")
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "insert into public.roles (tenant_id, code, name, is_system) "
                        "values (:tid, :code, '抢跑的同码角色', false)"
                    ),
                    {"tid": str(tenant_id), "code": ROLE_CODE},
                )
        finally:
            await engine.dispose()

    async def _patched_scalar(self: Any, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = await real_scalar(self, statement, *args, **kwargs)
        if not slipped["done"] and "select 1 from public.roles" in str(statement):
            slipped["done"] = True
            await _insert_competing_role()
        return result

    monkeypatch.setattr(AsyncSession, "scalar", _patched_scalar)

    with pytest.raises(RoleAdminError) as caught:
        await admin.create_role(
            principal,
            code=ROLE_CODE,
            name="图书馆管理员",
            permission_codes=["school.kb.view"],
            copy_from_role_id=None,
            reason="预检后撞库（集成用例）",
            request_id=None,
        )

    assert slipped["done"] is True, "预检补丁没生效：本用例的前提（打开窗口）没成立"
    assert (caught.value.code, caught.value.http_status) == ("ROLE_CODE_TAKEN", 409)
    assert "并发" in caught.value.message, "应走 INSERT 撞库分支，而不是预检分支"

    # 库里只有抢跑那一把角色：本次请求既没落角色行，也没落权限映射
    rows = await _role_rows(tenant_id, ROLE_CODE)
    assert len(rows) == 1, rows
    async with db_session(tenant_id=tenant_id) as session:
        name = await session.scalar(
            text("select name from public.roles where tenant_id = :tid and code = :code"),
            {"tid": tenant_id, "code": ROLE_CODE},
        )
        mapping_rows = await session.scalar(
            text(
                "select count(*) from public.role_permissions rp "
                "join public.roles r on r.id = rp.role_id "
                "where r.tenant_id = :tid and r.code = :code"
            ),
            {"tid": tenant_id, "code": ROLE_CODE},
        )
    assert str(name) == "抢跑的同码角色"
    assert int(mapping_rows or 0) == 0, "失败的创建不该留下任何权限映射"

    denied = await _audits("school.role.create_denied")
    assert [(e["result"], e["resource_id"], e["metadata"].get("reason")) for e in denied] == [
        ("denied", ROLE_CODE, "ROLE_CODE_TAKEN")
    ], denied
    assert await _audits("school.role.created") == []


async def test_role_code_is_unique_per_tenant_not_globally(stack: None) -> None:
    """两校各建 `library_keeper` 都成功；乙校既删不掉甲校的同名角色，也看不到它。"""

    del stack
    school_a = await _seed_tenant(f"SCH-RACU{uuid4().hex[:6].upper()}")
    school_b = await _seed_tenant(f"SCH-RACV{uuid4().hex[:6].upper()}")

    role_a = await _create(school_a["principal"])
    role_b = await _create(school_b["principal"])
    assert role_a.id != role_b.id

    # 每侧只查得到自己那一行：`where code = :code` 不带租户过滤，靠 RLS 兜住
    assert [str(row["tenant_id"]) for row in await _role_rows(school_a["tenant_id"], ROLE_CODE)] == [
        str(school_a["tenant_id"])
    ]
    assert [str(row["tenant_id"]) for row in await _role_rows(school_b["tenant_id"], ROLE_CODE)] == [
        str(school_b["tenant_id"])
    ]

    with pytest.raises(RoleAdminError) as caught:
        await admin.delete_role(
            school_b["principal"],
            role_a.id,
            confirm_role_code=ROLE_CODE,
            reason="别校同名角色删不掉（集成用例）",
            version=0,
            request_id=None,
        )
    assert (caught.value.code, caught.value.http_status) == ("ROLE_NOT_FOUND", 404)
    assert len(await _role_rows(school_a["tenant_id"], ROLE_CODE)) == 1

    await admin.delete_role(
        school_b["principal"],
        role_b.id,
        confirm_role_code=ROLE_CODE,
        reason="本校自建角色可删（集成用例）",
        version=0,
        request_id=None,
    )
    assert await _role_rows(school_b["tenant_id"], ROLE_CODE) == []
    # 甲校那行连同它的权限映射完全不受影响（CASCADE 只顺着 role_id 走）
    assert len(await _role_rows(school_a["tenant_id"], ROLE_CODE)) == 1
    assert await _role_permissions(school_a["tenant_id"], role_a.id) == {"school.kb.view"}


async def test_other_integrity_errors_are_not_relabelled_as_code_taken(stack: None) -> None:
    """INSERT 撞的不是同码唯一约束时原样抛出，不伪装成 409「同码已存在」。

    `ROLE_CODE_PATTERN` 只在接口层（Pydantic）校验，服务层直调 + 大写带连字符的码会撞库层
    `ck_roles_code_format_valid`。若那句也翻成 `ROLE_CODE_TAKEN`，排障的人会先去查这所学校
    有没有重名角色，而真故障在迁移/约束上——错误的对外形状必须跟着约束走。
    """

    del stack
    seeded = await _seed_tenant(f"SCH-RACW{uuid4().hex[:6].upper()}")

    with pytest.raises(IntegrityError) as caught:
        await admin.create_role(
            seeded["principal"],
            code="Library-Keeper",
            name="绕过接口校验的非法格式",
            permission_codes=[],
            copy_from_role_id=None,
            reason="库层格式 CHECK 必须原样暴露（集成用例）",
            request_id=None,
        )

    # 断到约束名本身：只 `raises(IntegrityError)` 的话，FK 或 NOT NULL 破了也算"过"，
    # 而这条用例存在的理由恰恰是「只有同码唯一约束才翻 409」。
    assert admin._integrity_constraint_name(caught.value) == "ck_roles_code_format_valid", str(
        caught.value
    )

    assert await _role_rows(seeded["tenant_id"], "Library-Keeper") == []
    # 不是越权、也不是业务冲突，是 500：不写拒绝审计（写了反而把故障混进安全事件流里）
    assert await _audits("school.role.create_denied") == []
    assert await _audits("school.role.created") == []


async def test_preview_affected_users_is_not_protected_by_the_version_cas(stack: None) -> None:
    """预览之后同事再绑一个人：`roles.version` 原地不动，CAS 放行，确认打在陈旧的影响面上。

    这是 TD-199 的**现状钉桩**，不是"这样对"。删除事务自己是一致的：拿到 `FOR UPDATE` 行锁后，
    并发 `insert into user_roles` 会因外键的 `FOR KEY SHARE` 阻塞到本事务结束，所以执行侧响应
    与审计里的 `affected_users` 是删除当时的真数——事前失守、事后可查。
    TD-187d 若采纳"绑定 bump 版本"，第 4 步会从成功变成 409：那时把这条改成断言
    `ROLE_VERSION_CONFLICT` 并把 TD-199 置为已完成，不要删掉它。
    """

    del stack
    school_code = f"SCH-RACB{uuid4().hex[:6].upper()}"
    seeded = await _seed_tenant(school_code)
    principal: Principal = seeded["principal"]
    tenant_id = seeded["tenant_id"]
    role = await _create(principal)

    first = await _seed_extra_user(school_code, "1", tenant_id)
    await _bind_user(tenant_id, first, role.id)
    preview = await admin.preview_role_deletion(principal, role.id)
    assert preview.affected_users == 1, preview

    # 预览与确认之间的第二次绑定：不加任何锁、不碰 `roles.version`，所以 CAS 看不见它
    second = await _seed_extra_user(school_code, "2", tenant_id)
    await _bind_user(tenant_id, second, role.id)
    rows = await _role_rows(tenant_id, ROLE_CODE)
    assert len(rows) == 1 and int(rows[0]["version"]) == 0, rows

    deleted = await admin.delete_role(
        principal,
        role.id,
        confirm_role_code=ROLE_CODE,
        reason="预览后又被绑走一个人，CAS 仍然放行（集成用例）",
        version=0,
        request_id=None,
    )
    assert deleted.affected_users == 2, "执行侧取的是删除当时的真实数，不是预览数"
    assert deleted.version == 0

    deleted_audit = await _audits("school.role.deleted")
    assert [(e["result"], e["metadata"].get("affected_users")) for e in deleted_audit] == [
        ("success", 2)
    ], deleted_audit

