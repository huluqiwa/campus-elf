"""角色级数据范围编辑的服务层不变量（M2-UI3 TD-187c，简报 §13）。

这里只测**必须直调服务层**才成立的性质（HTTP 侧的可见性端到端在
`tests/business/test_bs_m2ui3_role_scope.py`）：

1. 全量替换写的是差量、推进 `roles.version`，而**每一种被拒都不推高版本**（拒绝排在 CAS 之前）；
2. `class`/`course` 与 `bogus` 分属两档：前者留痕（试图写入一条本端点补不全 `scope_id` 的范围行），
   后者不留痕（入参非法族）——审计四类分档在本点的落点；
3. 删除只碰可编辑那两类：角色上真存在的 `class` 行必须**活过一次范围替换**，
   否则"全量替换"会在运营没点任何按钮的情况下，动一条本端点无权动的行；
4. 范围抬升触达本人时的 ack 分支：无 ack → 422 + `*.denied` + 专项打断留痕；
   带 ack + 理由 → 放行 + 成功留痕；本人已通过别的角色看得到全校时净增为零，不打断。
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth.principal import DATA_SCOPE_SELECT_SQL, Principal
from app.config import get_settings
from app.db.session import db_session, dispose_engine
from app.rbac import admin
from app.rbac.admin import RoleAdminError, RoleSummary
from tests.support.auth import seed_tenant_user
from tests.support.db import clean_test_data, database_url, make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn

pytestmark = pytest.mark.integration

ROLE_CODE = "scope_keeper"
PASSWORD = "ScopeEdit-Seed-Pass-1"


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


async def _seed_tenant(school_code: str) -> dict[str, Any]:
    """租户 + 内置角色 + 一名超管（服务层直调，不登录）。"""

    engine = make_test_engine("admin")
    try:
        tenant_id, user_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"scope-{school_code}",
            password=PASSWORD,
            user_type="school_admin",
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(text("select seed_builtin_roles(:tid)"), {"tid": str(tenant_id)})
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
        permissions=frozenset({"school.role.manage"}),
    )
    return {"tenant_id": tenant_id, "user_id": user_id, "principal": principal}


async def _role_id(tenant_id: UUID, code: str) -> UUID:
    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            value = await session.scalar(
                text("select id from public.roles where tenant_id = :tid and code = :code"),
                {"tid": str(tenant_id), "code": code},
            )
    finally:
        await engine.dispose()
    assert value is not None, f"角色 {code} 不存在"
    return UUID(str(value))


async def _seed_operator_bound_to(
    school_code: str, tenant_id: UUID, login: str, role_id: UUID
) -> Principal:
    """一名**非超管**操作者，绑在指定角色上（判据 ①「触达自己」需要真实绑定关系）。

    `school_code` 必须与主租户同一个：`seed_tenant_user` 按 school_code 复用租户，
    写死别的码会把操作者落到另一所学校，判据 ① 就永远不成立了。
    """

    engine = make_test_engine("admin")
    try:
        seeded_tenant, user_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=login,
            password=PASSWORD,
            user_type="school_admin",
        )
        assert seeded_tenant == tenant_id, "操作者必须落在同一个租户里"
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
    return Principal(
        user_id=user_id,
        realm="tenant",
        tenant_id=tenant_id,
        jti=uuid4(),
        session_id=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        must_change_password=False,
        roles=frozenset({"school_admin"}),
        permissions=frozenset({"school.role.manage"}),
    )


async def _bind_scope_rows(tenant_id: UUID, role_id: UUID, scope_type: str, *, with_id: bool) -> None:
    """手工种范围行。

    `class` 那一条是**人工 SQL** 造出来的：角色级 `class` 行今天没有合法写入方（`scopes_sync`
    只处理用户级行）。用例要钉的是"本端点的删除语句碰不到它"，所以只能自己种。
    """

    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "insert into public.data_scopes (id, tenant_id, role_id, scope_type, scope_id) "
                    "values (gen_random_uuid(), :tid, :rid, :stype, "
                    + ("gen_random_uuid()" if with_id else "null")
                    + ")"
                ),
                {"tid": str(tenant_id), "rid": str(role_id), "stype": scope_type},
            )
    finally:
        await engine.dispose()


async def _bind_user_scope(tenant_id: UUID, user_id: UUID, scope_type: str) -> None:
    """手工种一条**用户级**范围行（`scope_binding` CHECK 要求 role_id 与 user_id 二选一）。

    只为构造"角色级 ∪ 用户级"的两条腿：用户级行的合法写路径在关系同步/其他模块，不在本端点。
    """

    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "insert into public.data_scopes (id, tenant_id, user_id, scope_type, scope_id) "
                    "select gen_random_uuid(), :tid, :uid, cast(:stype as text), null "
                    "where not exists (select 1 from public.data_scopes "
                    "where tenant_id = :tid and user_id = :uid and scope_type = cast(:stype as text))"
                ),
                {"tid": str(tenant_id), "uid": str(user_id), "stype": scope_type},
            )
    finally:
        await engine.dispose()


async def _scope_rows(tenant_id: UUID, role_id: UUID) -> list[dict[str, Any]]:
    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "select scope_type, scope_id::text as scope_id from public.data_scopes "
                            "where tenant_id = :tid and role_id = :rid order by scope_type"
                        ),
                        {"tid": str(tenant_id), "rid": str(role_id)},
                    )
                )
                .mappings()
                .all()
            )
    finally:
        await engine.dispose()
    return [{"scope_type": str(r["scope_type"]), "scope_id": r["scope_id"]} for r in rows]


async def _permission_codes(tenant_id: UUID, role_id: UUID) -> list[str]:
    """角色当前映射的权限码（按码排序），用于跨路径并发时判定"权限这一侧到底被写了什么"。"""

    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rows: list[str] = list(
                (
                    await session.execute(
                        text(
                            "select p.code from public.role_permissions rp "
                            "join public.permissions p on p.id = rp.permission_id "
                            "where rp.tenant_id = :tid and rp.role_id = :rid order by p.code"
                        ),
                        {"tid": str(tenant_id), "rid": str(role_id)},
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()
    return [str(code) for code in rows]


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


async def _create_role(principal: Principal) -> UUID:
    summary = await admin.create_role(
        principal,
        code=ROLE_CODE,
        name="档案室",
        permission_codes=["school.personnel.view"],
        copy_from_role_id=None,
        reason="集成用例：建一把待配范围的角色",
        request_id=None,
    )
    return UUID(str(summary.id))


# --- 1. 全量替换与版本 CAS ---


async def test_replacement_writes_the_difference_and_bumps_version_once(stack: None) -> None:
    """提交 {'school'} 覆盖空集 → 库里正好一行、版本 +1、审计记 added/removed 两侧。"""

    seeded = await _seed_tenant("SCPA1")
    principal: Principal = seeded["principal"]
    tenant_id = seeded["tenant_id"]
    role_id = await _create_role(principal)

    summary = await admin.replace_role_scopes(
        principal,
        role_id,
        scope_types={"school"},
        version=0,
        reason="档案室需要查全校人员",
        request_id=None,
    )
    assert summary.version == 1, summary
    assert await _scope_rows(tenant_id, role_id) == [{"scope_type": "school", "scope_id": None}]

    updated = await _audits("school.role.scopes.updated")
    assert [(e["result"], e["metadata"]["added"], e["metadata"]["removed"]) for e in updated] == [
        ("success", ["school"], [])
    ], updated
    assert updated[-1]["metadata"]["reason"] == "档案室需要查全校人员"
    assert updated[-1]["metadata"]["resulting_scopes"] == ["school"]

    # 收窄到 self：差量两侧都记，且**超管本人不需要 ack**（净增为空，方向是收权）
    narrowed = await admin.replace_role_scopes(
        principal,
        role_id,
        scope_types={"self"},
        version=1,
        reason="档案室只看本人",
        request_id=None,
    )
    assert narrowed.version == 2
    assert await _scope_rows(tenant_id, role_id) == [{"scope_type": "self", "scope_id": None}]
    assert (await _audits("school.role.scopes.updated"))[-1]["metadata"] == {
        "role_code": ROLE_CODE,
        "added": ["self"],
        "removed": ["school"],
        "resulting_scopes": ["self"],
        "affected_users": 0,
        "reason": "档案室只看本人",
    }


async def test_empty_scope_list_is_allowed_and_shows_in_the_preview(stack: None) -> None:
    """空清单合法（撤范围不是危险动作，解析层 fail-closed 成"看不到数据"），预览要提前说清。"""

    seeded = await _seed_tenant("SCPA2")
    principal: Principal = seeded["principal"]
    tenant_id = seeded["tenant_id"]
    role_id = await _create_role(principal)
    await admin.replace_role_scopes(
        principal,
        role_id,
        scope_types={"school"},
        version=0,
        reason="先给全校",
        request_id=None,
    )

    preview = await admin.preview_role_scopes(principal, role_id, scope_types=set())
    assert preview.removed == ("school",)
    assert preview.added == ()
    assert preview.empties_scope is True, "清空后角色无任何范围行，必须在预览里点出来"
    assert preview.requires_confirmation is False

    summary = await admin.replace_role_scopes(
        principal,
        role_id,
        scope_types=set(),
        version=1,
        reason="撤掉范围，先按人临时授",
        request_id=None,
    )
    assert summary.version == 2
    assert await _scope_rows(tenant_id, role_id) == []


async def test_stale_version_is_rejected_and_returns_the_latest_snapshot(stack: None) -> None:
    """CAS 落败必须 409 并回带最新快照，且不写任何范围行（防并发覆盖）。"""

    seeded = await _seed_tenant("SCPA3")
    principal: Principal = seeded["principal"]
    tenant_id = seeded["tenant_id"]
    role_id = await _create_role(principal)
    await admin.replace_role_scopes(
        principal,
        role_id,
        scope_types={"school"},
        version=0,
        reason="先给全校",
        request_id=None,
    )

    with pytest.raises(RoleAdminError) as raised:
        await admin.replace_role_scopes(
            principal,
            role_id,
            scope_types={"self"},
            version=0,
            reason="同事已经改过一次",
            request_id=None,
        )
    assert raised.value.code == "ROLE_VERSION_CONFLICT"
    assert raised.value.http_status == 409
    assert raised.value.latest is not None and raised.value.latest.version == 1
    assert await _scope_rows(tenant_id, role_id) == [{"scope_type": "school", "scope_id": None}]


# --- 2. 两档拒绝：留痕 vs 不留痕 ---


@pytest.mark.parametrize(
    ("blocked", "reason_phrase"),
    [
        ("class", "班级范围必须绑定到具体班级 id"),
        ("course", "本期不贡献可见性"),
    ],
)
async def test_non_editable_scope_types_are_rejected_with_an_audit(
    stack: None, blocked: str, reason_phrase: str
) -> None:
    """手工授 `class`/`course` → 422 + `scopes.denied` 留痕 + 版本与数据都不动。

    留痕的理由（简报 §13.2 决策③）：拦的是"往库里写一条本端点补不全 `scope_id` 的行"的意图，
    与转授禁令同一档；报警要能回答"谁在反复试图手工授班级范围"。两类各自的**人读理由不同**，
    所以按类型逐个断言文案——文案与判据分家时，运营会照着一个不成立的理由去排查。
    """

    seeded = await _seed_tenant(f"SCPB{blocked[:2].upper()}")
    principal: Principal = seeded["principal"]
    tenant_id = seeded["tenant_id"]
    role_id = await _create_role(principal)

    with pytest.raises(RoleAdminError) as raised:
        await admin.replace_role_scopes(
            principal,
            role_id,
            scope_types={blocked},
            version=0,
            reason="想按班看",
            request_id=None,
        )
    assert raised.value.code == "SCOPE_TYPE_NOT_EDITABLE"
    assert blocked in raised.value.message
    assert reason_phrase in raised.value.message, raised.value.message
    assert await _scope_rows(tenant_id, role_id) == []

    denied = await _audits("school.role.scopes.denied")
    assert [(e["result"], e["metadata"].get("reason")) for e in denied] == [
        ("denied", "SCOPE_TYPE_NOT_EDITABLE")
    ], denied
    assert denied[-1]["metadata"]["requested_scope_types"] == [blocked]
    # 拒绝排在 CAS 之前：一次被拒不得把版本推高（否则别人下次提交假报"已被他人修改"）
    assert await _role_version(tenant_id, role_id) == 0, "拒绝排在 CAS 之前，不得推高版本"
    assert await _audits("school.role.scopes.updated") == []


async def test_unknown_scope_type_is_rejected_without_a_security_audit(stack: None) -> None:
    """`bogus` 属于入参非法族（同目录外权限码）：422 无痕，且**先于**可编辑性判定。"""

    seeded = await _seed_tenant("SCPC1")
    principal: Principal = seeded["principal"]
    tenant_id = seeded["tenant_id"]
    role_id = await _create_role(principal)

    with pytest.raises(RoleAdminError) as raised:
        await admin.replace_role_scopes(
            principal,
            role_id,
            scope_types={"bogus", "class"},
            version=0,
            reason="字面量都没对齐",
            request_id=None,
        )
    assert raised.value.code == "UNKNOWN_SCOPE_TYPE"
    assert await _audits("school.role.scopes.denied") == []
    assert await _scope_rows(tenant_id, role_id) == []


async def test_preview_rejects_the_same_two_kinds_in_the_same_order(stack: None) -> None:
    """类型族两档在预览里的**相对顺序**与判据码同 `PUT`：否则"预览说能过、PUT 被拒"，运营会当预览是装饰。

    不钉"整体顺序与 PUT 完全一致"：`PUT` 的"角色存在/可编辑"排在两档之间（要先 `FOR UPDATE`
    拿行），预览不锁行、把类型族一次性放前面。见 `admin.preview_role_scopes` 的文档串。
    """

    seeded = await _seed_tenant("SCPC2")
    principal: Principal = seeded["principal"]
    role_id = await _create_role(principal)

    for submitted, code in (
        ({"bogus"}, "UNKNOWN_SCOPE_TYPE"),
        ({"class"}, "SCOPE_TYPE_NOT_EDITABLE"),
        # 同时含两类非法项：只有"未知"先跑才可能出这个码。把服务层两档对调，这条会红——
        # 前两条单元素提交对调后仍然各自命中，钉不住相对顺序。
        ({"bogus", "class"}, "UNKNOWN_SCOPE_TYPE"),
    ):
        with pytest.raises(RoleAdminError) as raised:
            await admin.preview_role_scopes(principal, role_id, scope_types=submitted)
        assert raised.value.code == code, submitted
    assert await _audits("school.role.scopes.denied") == [], "预览阶段的拒绝不落安全审计"


# --- 2b. 判据顺序：realm 先于入参，预览先于取行 ---


async def test_platform_principal_is_refused_before_any_argument_validation(stack: None) -> None:
    """平台主体带着非法类型来 → 403 `REALM_MISMATCH`，不是 422 `UNKNOWN_SCOPE_TYPE`。

    realm 判据排在最前（`_require_tenant` 在 `_assert_known_scope_types` 之前）：平台主体根本不该
    走到租户角色的入参校验里，否则"这个端点对我可不可用"的答案会随提交内容变化，且被拒原因会
    诱导平台侧调参重试。三条入口都要有这条顺序——预览与 PUT 各自钉一次。
    """

    seeded = await _seed_tenant("SCPG1")
    principal: Principal = seeded["principal"]
    role_id = await _create_role(principal)
    platform = Principal(
        user_id=seeded["user_id"],
        realm="platform",
        tenant_id=None,
        jti=principal.jti,
        session_id=principal.session_id,
        expires_at=principal.expires_at,
        must_change_password=False,
        roles=frozenset({"platform_super_admin"}),
        permissions=frozenset({"school.role.manage"}),
    )

    with pytest.raises(RoleAdminError) as preview_raised:
        await admin.preview_role_scopes(platform, role_id, scope_types={"bogus"})
    assert preview_raised.value.code == "REALM_MISMATCH"
    assert preview_raised.value.http_status == 403

    with pytest.raises(RoleAdminError) as put_raised:
        await admin.replace_role_scopes(
            platform, role_id, scope_types={"bogus"}, version=0, reason="平台误入", request_id=None
        )
    assert put_raised.value.code == "REALM_MISMATCH"
    assert put_raised.value.http_status == 403


async def test_preview_reports_the_type_verdict_while_put_reports_the_missing_role(
    stack: None,
) -> None:
    """同一个不存在的角色 id + `class`：预览 422 `SCOPE_TYPE_NOT_EDITABLE`，PUT 404 + 留痕。

    design.md §5.12 把这条差异写成结构性事实（PUT 要先 `FOR UPDATE` 拿到行才判得了角色，预览
    不锁行、类型族一次性放前面），但此前没有用例，文档可以单方面漂移。它同时是"预览不是 PUT 的
    预演"这一口径的反例：类型族两档同码同序，角色族判据不同序。
    """

    seeded = await _seed_tenant("SCPH1")
    principal: Principal = seeded["principal"]
    missing = uuid4()

    with pytest.raises(RoleAdminError) as preview_raised:
        await admin.preview_role_scopes(principal, missing, scope_types={"class"})
    assert preview_raised.value.code == "SCOPE_TYPE_NOT_EDITABLE"
    assert await _audits("school.role.scopes.denied") == [], "预览被拒不落安全审计"

    with pytest.raises(RoleAdminError) as put_raised:
        await admin.replace_role_scopes(
            principal, missing, scope_types={"class"}, version=0, reason="给不存在的角色配范围",
            request_id=None,
        )
    assert put_raised.value.code == "ROLE_NOT_FOUND"
    assert put_raised.value.http_status == 404
    denied = await _audits("school.role.scopes.denied")
    assert [(e["metadata"].get("reason"), e["resource_id"]) for e in denied] == [
        ("ROLE_NOT_FOUND", str(missing))
    ], denied


# --- 3. 非可编辑行不由本端点写入，也不能被这次替换抹掉 ---


async def test_role_bound_class_row_survives_a_scope_replacement(stack: None) -> None:
    """角色上的 `class` 行必须活过一次范围替换（删除只碰 school/self）。

    今天 `scopes_sync` 写的是**用户级** class 行，角色级同型行只能由手工 SQL 落下；但删除语句
    一旦写成"删掉该角色全部范围行"，这行就会在运营没点任何按钮的情况下静默消失——而它带着
    `scope_id`，是解析层唯一读得懂 class 可见性的地方。这条用例钉住窄删除，并顺带钉住
    预览的 `retained_non_editable`：结果集里那些"本次不碰"的行必须列出来，否则 added/removed 说谎。
    """

    seeded = await _seed_tenant("SCPD1")
    principal: Principal = seeded["principal"]
    tenant_id = seeded["tenant_id"]
    role_id = await _create_role(principal)
    await _bind_scope_rows(tenant_id, role_id, "class", with_id=True)

    preview = await admin.preview_role_scopes(principal, role_id, scope_types={"self"})
    assert preview.retained_non_editable == ("class",), preview

    summary = await admin.replace_role_scopes(
        principal,
        role_id,
        scope_types={"self"},
        version=0,
        reason="只配 self，别碰别人的行",
        request_id=None,
    )
    rows = await _scope_rows(tenant_id, role_id)
    assert {row["scope_type"] for row in rows} == {"class", "self"}, rows
    assert summary.version == 1

    listed = await admin.list_role_scopes(principal, role_id)
    assert [(row.scope_type, row.editable) for row in listed] == [("class", False), ("self", True)]

    # 差量按"结果集 = 提交 ∪ 保留的非可编辑行"算：class 既不是 added 也不是 removed
    metadata = (await _audits("school.role.scopes.updated"))[-1]["metadata"]
    assert metadata["added"] == ["self"]
    assert metadata["removed"] == []
    assert metadata["resulting_scopes"] == ["class", "self"]


# --- 4. 范围抬升触达本人：ack 分支 ---


async def test_scope_upgrade_reaching_self_needs_ack_then_audits_both_sides(
    stack: None,
) -> None:
    """非超管把自己所在角色从 self 抬到 school：无 ack 422 + 打断留痕，带 ack 放行 + 成功留痕。"""

    seeded = await _seed_tenant("SCPE1")
    super_principal: Principal = seeded["principal"]
    tenant_id = seeded["tenant_id"]
    role_id = await _create_role(super_principal)
    operator = await _seed_operator_bound_to("SCPE1", tenant_id, "scope-op-1", role_id)
    await admin.replace_role_scopes(
        super_principal,
        role_id,
        scope_types={"self"},
        version=0,
        reason="出厂先按本人",
        request_id=None,
    )

    with pytest.raises(RoleAdminError) as raised:
        await admin.replace_role_scopes(
            operator,
            role_id,
            scope_types={"school"},
            version=1,
            reason="要查全校",
            request_id=None,
            self_grant_ack=False,
        )
    assert raised.value.code == "SELF_GRANT_CONFIRMATION_REQUIRED"
    assert "school" in raised.value.message
    assert await _scope_rows(tenant_id, role_id) == [{"scope_type": "self", "scope_id": None}]
    assert await _role_version(tenant_id, role_id) == 1, "打断不推高版本"

    interrupted = await _audits("school.role.self_grant")
    assert [(e["result"], e["metadata"].get("kind")) for e in interrupted] == [("denied", "scope")]
    assert interrupted[-1]["metadata"]["newly_reaching_self"] == ["school"]
    assert interrupted[-1]["metadata"]["role_code"] == ROLE_CODE

    acked = await admin.replace_role_scopes(
        operator,
        role_id,
        scope_types={"school"},
        version=1,
        reason="兼管档案室全量核对",
        request_id=None,
        self_grant_ack=True,
    )
    assert acked.version == 2
    grants = await _audits("school.role.self_grant")
    assert [(e["result"], e["metadata"].get("kind")) for e in grants] == [
        ("denied", "scope"),
        ("success", "scope"),
    ], grants
    assert grants[-1]["metadata"]["confirmed_by"] == "self_grant_ack"
    assert "兼管档案室全量核对" in grants[-1]["metadata"]["reason"]


async def test_upgrade_is_not_an_expansion_when_self_already_sees_school(
    stack: None,
) -> None:
    """操作者通过**另一条角色**已经看得到全校时，把他所在角色也抬成 school 不算扩权。"""

    seeded = await _seed_tenant("SCPE2")
    super_principal: Principal = seeded["principal"]
    tenant_id = seeded["tenant_id"]
    role_id = await _create_role(super_principal)
    operator = await _seed_operator_bound_to("SCPE2", tenant_id, "scope-op-2", role_id)
    # 再给这名操作者绑一把已经是 school 范围的角色（并集分母从此含 school）
    wide_role = await admin.create_role(
        super_principal,
        code="scope_wide",
        name="全校视图",
        permission_codes=[],
        copy_from_role_id=None,
        reason="给操作者的另一条角色",
        request_id=None,
    )
    await admin.replace_role_scopes(
        super_principal,
        UUID(str(wide_role.id)),
        scope_types={"school"},
        version=0,
        reason="全校视图",
        request_id=None,
    )
    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text("insert into public.user_roles (tenant_id, user_id, role_id) "
                     "values (:tid, :uid, :rid)"),
                {
                    "tid": str(tenant_id),
                    "uid": str(operator.user_id),
                    "rid": str(wide_role.id),
                },
            )
    finally:
        await engine.dispose()

    preview = await admin.preview_role_scopes(operator, role_id, scope_types={"school"})
    assert preview.requires_confirmation is False, preview
    assert preview.newly_reaching_self == ()

    summary = await admin.replace_role_scopes(
        operator,
        role_id,
        scope_types={"school"},
        version=0,
        reason="两把角色都按全校",
        request_id=None,
        self_grant_ack=False,
    )
    assert summary.version == 1
    assert await _audits("school.role.self_grant") == [], "净增为零不该产生任何自扩权留痕"


async def test_locked_builtin_role_refuses_scope_edits_with_an_audit(stack: None) -> None:
    """超管角色锁定：409 `ROLE_IMMUTABLE` + 留痕 + 出厂范围行一条没动。"""

    seeded = await _seed_tenant("SCPF1")
    principal: Principal = seeded["principal"]
    tenant_id = seeded["tenant_id"]
    super_role = await _role_id(tenant_id, "school_super_admin")

    before = await _scope_rows(tenant_id, super_role)
    assert {row["scope_type"] for row in before} == {"school"}, before

    with pytest.raises(RoleAdminError) as raised:
        await admin.replace_role_scopes(
            principal,
            super_role,
            scope_types={"self"},
            version=0,
            reason="试着改锁定角色的范围",
            request_id=None,
        )
    assert raised.value.code == "ROLE_IMMUTABLE"
    assert await _scope_rows(tenant_id, super_role) == before
    denied = await _audits("school.role.scopes.denied")
    assert [(e["result"], e["metadata"].get("reason")) for e in denied] == [
        ("denied", "ROLE_IMMUTABLE")
    ], denied


async def test_cross_tenant_role_id_is_404_and_leaves_no_row(stack: None) -> None:
    """跨租 role_id 与不存在同形 404，且留痕只记判据码、不落对方租户信息。"""

    seeded_a = await _seed_tenant("SCPG1")
    seeded_b = await _seed_tenant("SCPG2")
    principal_b: Principal = seeded_b["principal"]
    role_id_a = await _create_role(seeded_a["principal"])

    with pytest.raises(RoleAdminError) as raised:
        await admin.replace_role_scopes(
            principal_b,
            role_id_a,
            scope_types={"school"},
            version=0,
            reason="拿别人学校的 role id 试一把",
            request_id=None,
        )
    assert raised.value.code == "ROLE_NOT_FOUND"
    assert raised.value.http_status == 404
    denied = await _audits("school.role.scopes.denied")
    assert [(e["result"], e["metadata"].get("reason")) for e in denied] == [
        ("denied", "ROLE_NOT_FOUND")
    ], denied
    assert set(denied[-1]["metadata"]) == {"reason"}, "只记判据码，不落任何对方租户信息"
    # 用例名承诺的那一半：跨租拒绝不得在**自己**这边留下范围行（RLS 之外的显式 tenant 过滤）
    assert await _scope_rows(seeded_b["tenant_id"], role_id_a) == []
    assert await _role_version(seeded_a["tenant_id"], role_id_a) == 0, "不得替别人把版本推高"


async def test_read_and_preview_of_a_foreign_role_are_404(stack: None) -> None:
    """只读侧（列表/预览）跨租同样 404 同形，且不落安全审计——读路径的探测由写侧计数。"""

    seeded_a = await _seed_tenant("SCPH1")
    seeded_b = await _seed_tenant("SCPH2")
    principal_b: Principal = seeded_b["principal"]
    role_id_a = await _create_role(seeded_a["principal"])

    for call in (
        lambda: admin.list_role_scopes(principal_b, role_id_a),
        lambda: admin.preview_role_scopes(principal_b, role_id_a, scope_types={"school"}),
    ):
        with pytest.raises(RoleAdminError) as raised:
            await call()
        assert raised.value.code == "ROLE_NOT_FOUND"
        assert raised.value.http_status == 404
    assert await _audits("school.role.scopes.denied") == [], "只读路径不留安全审计（与重置预览同口径）"


async def test_scope_ack_without_a_reason_is_rejected_before_any_write(stack: None) -> None:
    """勾了确认框但没填理由 → `SELF_GRANT_REASON_REQUIRED`：与「没勾框」是两个判据码、两种留痕。

    为什么要分开钉：审计消费方靠 `reason` 区分"没勾框"与"勾了框没写理由"（design.md §5.12），
    只测前者就等于放过后者；两条分支在 `_assert_self_grant` 里是相邻的 `if`，漏一条不会让另一条失败。
    """

    seeded = await _seed_tenant("SCPE3")
    super_principal: Principal = seeded["principal"]
    tenant_id = seeded["tenant_id"]
    role_id = await _create_role(super_principal)
    operator = await _seed_operator_bound_to("SCPE3", tenant_id, "scope-op-3", role_id)
    await admin.replace_role_scopes(
        super_principal,
        role_id,
        scope_types={"self"},
        version=0,
        reason="出厂先按本人",
        request_id=None,
    )

    with pytest.raises(RoleAdminError) as raised:
        await admin.replace_role_scopes(
            operator,
            role_id,
            scope_types={"school"},
            version=1,
            reason="   ",
            request_id=None,
            self_grant_ack=True,
        )
    assert raised.value.code == "SELF_GRANT_REASON_REQUIRED"
    assert await _scope_rows(tenant_id, role_id) == [{"scope_type": "self", "scope_id": None}]
    assert await _role_version(tenant_id, role_id) == 1, "被拒不得推高版本"

    generic = await _audits("school.role.scopes.denied")
    assert [(e["result"], e["metadata"].get("reason"), e["resource_id"]) for e in generic] == [
        ("denied", "SELF_GRANT_REASON_REQUIRED", str(role_id))
    ], "通用拒绝审计与专项审计成对写：只写专项会丢掉'在本端点被拒'这一层"
    grants = await _audits("school.role.self_grant")
    assert [(e["result"], e["metadata"].get("reason")) for e in grants] == [
        ("denied", "SELF_GRANT_REASON_REQUIRED")
    ], grants
    assert grants[-1]["metadata"]["self_grant_ack"] is True, "要能区分'没勾框'与'勾了框没写理由'"
    assert grants[-1]["metadata"]["kind"] == "scope"


async def test_two_concurrent_replacements_of_the_same_version_yield_one_writer(
    stack: None,
) -> None:
    """两个会话同时按 `version=0` 提交不同范围：一个成功、一个 409，库里只剩赢家的差量。

    与 `test_stale_version_...` 的区别：那条是"先输一次再提交"的事后检查，两个事务都真的在
    `FOR UPDATE` 上排过队，输家醒来看到的是**已经推进过**的版本。本条钉住的性质是三件：恰好一个
    写者成功、`version` 只推进一格（不是两格）、赢家审计里的 `added` 与库里留下的行一致。

    它**钉不住**两件容易被误当成本条功劳的事：① `update ... and version = :version` 那条 SQL 级
    CAS 谓词——同函数更早的 `int(role["version"]) != version` 已经在行锁之后拦下落败者，去掉谓词
    本条仍然绿，谓词是纵深防御而不是本条的承重墙（已登记 TD-205）；② 权限与范围共享一列
    `roles.version`（TD-202）时的跨路径锁顺序——两个写者都走范围路径，权限路径没参与；跨路径那条
    由紧随其后的 `test_cross_path_concurrent_permission_and_scope_edit_yields_one_writer` 钉住
    （TD-187f-A T2）。
    """

    seeded = await _seed_tenant("SCPA4")
    principal: Principal = seeded["principal"]
    tenant_id = seeded["tenant_id"]
    role_id = await _create_role(principal)

    async def submit(scope_types: set[str], reason: str) -> RoleAdminError | int:
        try:
            summary = await admin.replace_role_scopes(
                principal,
                role_id,
                scope_types=scope_types,
                version=0,
                reason=reason,
                request_id=None,
            )
        except RoleAdminError as exc:
            return exc
        return summary.version

    outcomes = await asyncio.gather(submit({"school"}, "并发甲"), submit({"self"}, "并发乙"))
    codes = sorted(item.code if isinstance(item, RoleAdminError) else "OK" for item in outcomes)
    assert codes == ["OK", "ROLE_VERSION_CONFLICT"], outcomes
    assert await _role_version(tenant_id, role_id) == 1, "并发下版本只推进一格"

    rows = await _scope_rows(tenant_id, role_id)
    assert len(rows) == 1, f"赢家只能留下一类范围：{rows}"
    winner = (await _audits("school.role.scopes.updated"))[-1]["metadata"]
    assert winner["added"] == [rows[0]["scope_type"]], (winner, rows)
    assert await _audits("school.role.scopes.denied") == [], "版本落败属并发，不是越权探测（不入安全审计）"


# --- 4b. 跨路径：权限 PUT 与范围 PUT 抢同一列 `roles.version` ---


async def test_cross_path_concurrent_permission_and_scope_edit_yields_one_writer(
    stack: None,
) -> None:
    """同一个角色：甲在权限页提交、乙同时的范围页提交，两边都带 `version=0` → 恰好一路成功。

    业务形状是 M2-UI3 前台真实的两个标签页：权限与范围分家编辑，但 `roles.version` 只有一列
    （TD-202 的有意取舍——分列会出现"权限写进去了、范围失败"的半更新态）。今天这条链路**没有**
    任何机制阻止两路都落库：唯一的屏障是两边都在同一行 `roles` 上取 `FOR UPDATE`。所以本条要钉的
    是三件跨路径才成立的事：① 恰好一个写者成功、`version` 只推进一格（不是两格）；② 落败那一路
    拿到的 409 `latest` 快照必须已经是**赢家**的新值——前台据此提示"刷新后重试"，快照若是旧值等于
    让操作者基于过期数据再改一轮；③ 赢家只写自己那一侧，落败侧一行都不许留（半更新态的真实形状）。

    **本条预期"新增即绿"**（简报 §16.4 步骤 2 已写明）：它不是复现一个今天的 bug，而是把"两侧共享
    一列版本 + 同一行锁"这套隐式约定钉成回归网。谁以后改任一侧的取锁方式（TD-202 的候选解之一就是
    给范围单独开版本列；把 `for_update` 换成 advisory lock 也算），本条会转红而不是等到线上出现
    "两个都 200、版本只加一格"。

    它**钉不住**两件事：① SQL 级 CAS 谓词是否承重（同 §852 的①，落败者先被行锁后的版本比较拦住）；
    ② 死锁不存在——两侧取锁顺序相同（都是 `roles` 行锁），本条只验证"排上队的那一路不会既拿到锁又
    看不见对方已写的值"，不验证反序取锁的情形（那种情形需要新的写入方，今天没有）。
    """

    seeded = await _seed_tenant("SCPA5")
    principal: Principal = seeded["principal"]
    tenant_id = seeded["tenant_id"]
    role_id = await _create_role(principal)
    submitted = ["school.personnel.create", "school.personnel.edit"]

    async def submit_permissions() -> RoleAdminError | RoleSummary:
        try:
            return await admin.replace_role_permissions(
                principal,
                role_id,
                permission_codes=submitted,
                version=0,
                reason="并发跨路径甲",
                request_id=None,
            )
        except RoleAdminError as exc:
            return exc

    async def submit_scopes() -> RoleAdminError | RoleSummary:
        try:
            return await admin.replace_role_scopes(
                principal,
                role_id,
                scope_types={"self"},
                version=0,
                reason="并发跨路径乙",
                request_id=None,
            )
        except RoleAdminError as exc:
            return exc

    outcomes = await asyncio.gather(submit_permissions(), submit_scopes())
    winners = [item for item in outcomes if not isinstance(item, RoleAdminError)]
    losers = [item for item in outcomes if isinstance(item, RoleAdminError)]
    assert len(winners) == 1 and len(losers) == 1, outcomes
    assert await _role_version(tenant_id, role_id) == 1, "跨路径并发同样只推进一格"

    loser = losers[0]
    assert loser.code == "ROLE_VERSION_CONFLICT", loser
    assert loser.latest is not None, "落败必须带回刷新快照，否则前台只能盲改"
    assert loser.latest.version == 1

    permissions_won = isinstance(winners[0], RoleSummary) and sorted(
        winners[0].permission_codes
    ) == submitted
    if permissions_won:
        assert await _permission_codes(tenant_id, role_id) == submitted
        assert await _scope_rows(tenant_id, role_id) == [], "落败的范围写入不能留下半行"
        assert sorted(loser.latest.permission_codes) == submitted, "快照得是赢家的新权限集"
        metadata = (await _audits("school.role.permissions.updated"))[-1]["metadata"]
        assert metadata["added"] == submitted, metadata
        assert metadata["removed"] == ["school.personnel.view"], metadata
        assert await _audits("school.role.scopes.updated") == []
    else:
        rows = await _scope_rows(tenant_id, role_id)
        assert [row["scope_type"] for row in rows] == ["self"], rows
        assert await _permission_codes(tenant_id, role_id) == ["school.personnel.view"], (
            "落败的权限写入不能改动映射"
        )
        assert list(loser.latest.permission_codes) == ["school.personnel.view"], (
            "范围赢家不该动权限快照"
        )
        metadata = (await _audits("school.role.scopes.updated"))[-1]["metadata"]
        assert metadata["added"] == ["self"], metadata
        assert await _audits("school.role.permissions.updated") == []

    assert await _audits("school.role.permissions.denied") == []
    assert await _audits("school.role.scopes.denied") == [], "版本落败属并发，不入安全审计"


# --- 5. 守卫的"净增"分母与请求期解析必须同解 ---


async def test_principal_scope_types_agrees_with_the_request_time_resolver(stack: None) -> None:
    """`_principal_scope_types` 与 `DATA_SCOPE_SELECT_SQL` 对同一用户必须给出同一类型集合。

    两处是同一并集语义的两份 SQL（TD-195）：守卫在**写事务内**重读（不吃请求快照），认证依赖在
    **每次请求进入时**解析。改了一处的连接口径/谓词而另一处没改，守卫就会按一个运行时并不存在的
    能力面判"扩没扩权"——宽了会静默放行自扩权，窄了会把运营正常保存反复打断成 422。
    种子同时覆盖两条腿（角色级 `school` + 用户级 `self`），否则"两边都只读角色级"也能过。
    """

    seeded = await _seed_tenant("SCPI1")
    principal: Principal = seeded["principal"]
    tenant_id = seeded["tenant_id"]

    role_id = await _create_role(principal)
    operator = await _seed_operator_bound_to("SCPI1", tenant_id, "scope-op-i1", role_id)
    await admin.replace_role_scopes(
        principal,
        role_id,
        scope_types={"school"},
        version=0,
        reason="给自建角色配全校范围",
        request_id=None,
    )
    await _bind_user_scope(tenant_id, operator.user_id, "self")

    async with db_session(tenant_id=tenant_id) as session:
        guard_side = await admin._principal_scope_types(session, tenant_id, operator.user_id)
        request_rows: list[Any] = list(
            (await session.execute(text(DATA_SCOPE_SELECT_SQL), {"uid": operator.user_id}))
            .scalars()
            .all()
        )
        request_side = {str(value) for value in request_rows}

    assert guard_side == {"school", "self"}, guard_side
    assert guard_side == request_side, (guard_side, request_side)


async def _role_version(tenant_id: UUID, role_id: UUID) -> int:
    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            value = await session.scalar(
                text("select version from public.roles where id = :rid and tenant_id = :tid"),
                {"rid": str(role_id), "tid": str(tenant_id)},
            )
    finally:
        await engine.dispose()
    return int(value)

