"""业务场景 BS-M2UI3-D（替换路径）：自扩权必须显式确认（TD-191 收口，M2-UI3 简报 §6 决策 #4）。

Given 校方超管、一名绑定 `school_admin` 且已被授予 `school.role.manage` 的管理员，
When 该管理员把**自己当前没有**的敏感码写进**自己所在**的角色，
Then 未确认 422 / 确认但没理由 422 / 确认且给理由 200；
    前两次都不前进版本、不改映射，并**同时**留下通用 `*.denied` 与专项 `school.role.self_grant`
    （result=denied + 触发的码清单）——打断也是安全事件；放行那次除 `*.updated` 外再多一条
    专项成功审计，且管理员**同一条 token** 下一请求即拿到该能力。

同时钉住三条不打扰/不重复计算的反向与旁路：
- 改别人所在的角色（教师 B 端）加同一个敏感码 → 无需 ack；
- 自己本就持有该敏感码时补进自己所在角色（净增为零）→ 无需 ack、也不产生专项审计；
- 超管（目录落后态）自扩权 → 不打断，但审计记 `confirmed_by=super_admin`（免打断≠免留痕）。

判据写宽了会把日常配置全挡掉，写窄了等于没有守卫——这两条就是"宽"的边界证明。
"""

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth import revocation
from app.config import get_settings
from app.db.session import db_session, dispose_engine
from app.main import create_app
from tests.support.auth import login_tenant, seed_tenant_user
from tests.support.db import clean_test_data, database_url, make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.business, pytest.mark.integration]

SUPER_LOGIN = "selfgr-super"
SUPER_PASSWORD = "SelfGr-Super-Pass-1"
ADMIN_LOGIN = "selfgr-admin"
ADMIN_PASSWORD = "SelfGr-Admin-Pass-1"

SCHOOL_ADMIN_DEFAULTS = {
    "school.dashboard.view",
    "school.personnel.view",
    "school.kb.view",
    "school.message.view",
    "school.message.send",
    "school.message.read",
}
# 敏感清单里的码（`app/rbac/self_grant.py`）：管理员默认不持有，写进自己所在角色即"净增"。
SENSITIVE_CODE = "school.personnel.sensitive.read"
# 受控查看接口只需要"权限位"这一个判据命中；人员 id 用随机值 → 必然 404，
# 于是 403/404 的翻转正好只反映权限本身的变化（不依赖身份证数据与密钥配置）。
MISSING_PERSONNEL_ID = str(uuid4())
READ_REASON = "核对教职工身份（业务场景 BS-M2UI3-D）"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def client(migrated_database: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
    finally:
        await admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    get_settings.cache_clear()
    await revocation.reset_client()

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    await revocation.reset_client()
    await dispose_engine()
    get_settings.cache_clear()


async def _seed_school_with_delegated_manager(school_code: str) -> dict[str, Any]:
    """建租户 + 内置角色；超管绑 `school_super_admin`，管理员绑 `school_admin`。

    `role.manage` 不在出厂默认里，必须由超管授出——这一步本身不构成自扩权
    （操作者没有绑在目标角色上），所以不需要 ack。
    """

    engine = make_test_engine("admin")
    try:
        tenant_id, super_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=SUPER_LOGIN,
            password=SUPER_PASSWORD,
            user_type="school_admin",
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text("select seed_builtin_roles(:tid)"), {"tid": str(tenant_id)}
            )
        _, admin_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=ADMIN_LOGIN,
            password=ADMIN_PASSWORD,
            user_type="school_admin",
        )
        async with factory() as session, session.begin():
            for user_id, role_code in ((super_id, "school_super_admin"), (admin_id, "school_admin")):
                await session.execute(
                    text(
                        "insert into user_roles (tenant_id, user_id, role_id) "
                        "select :tid, :uid, r.id from roles r "
                        "where r.tenant_id = :tid and r.code = :code"
                    ),
                    {"tid": str(tenant_id), "uid": str(user_id), "code": role_code},
                )
        return {"tenant_id": tenant_id, "super_id": super_id, "admin_id": admin_id}
    finally:
        await engine.dispose()


async def _role_id(tenant_id: UUID, code: str) -> UUID:
    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            value = await session.scalar(
                text("select id from roles where tenant_id = :tid and code = :code"),
                {"tid": str(tenant_id), "code": code},
            )
    finally:
        await engine.dispose()
    assert value is not None
    return UUID(str(value))


async def _db_permission_codes(tenant_id: UUID, role_id: UUID) -> set[str]:
    async with db_session(tenant_id=tenant_id) as session:
        rows: list[Any] = list(
            (
                await session.execute(
                    text(
                        "select p.code from role_permissions rp "
                        "join permissions p on p.id = rp.permission_id "
                        "where rp.tenant_id = :tid and rp.role_id = :rid"
                    ),
                    {"tid": tenant_id, "rid": role_id},
                )
            )
            .scalars()
            .all()
        )
    return {str(code) for code in rows}


async def _role_row(tenant_id: UUID, role_id: UUID) -> dict[str, Any]:
    async with db_session(tenant_id=tenant_id) as session:
        row = (
            (
                await session.execute(
                    text("select version from roles where tenant_id = :tid and id = :rid"),
                    {"tid": tenant_id, "rid": role_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _audit_entries(action: str) -> list[dict[str, Any]]:
    """按写入顺序取回 `result / resource_id / metadata_json`，用于核对专项留痕的条数与内容。"""

    async with db_session(platform=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select result, resource_id, metadata_json from audit_logs "
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


async def _state(client: AsyncClient, headers: dict[str, str], code: str) -> dict[str, Any]:
    """从角色列表读回目标角色的当前版本与权限（不假设版本号从 0 起）。"""

    roles = (await client.get("/api/v1/school/roles", headers=headers)).json()
    assert roles, "角色列表为空"
    return next(role for role in roles if role["code"] == code)


@pytest.fixture
async def delegated(client: AsyncClient) -> dict[str, Any]:
    """超管把 `school.role.manage` 授给 school_admin；返回两端 headers 与目标角色 id。"""

    school_code = f"SCH-SG{uuid4().hex[:6].upper()}"
    seeded = await _seed_school_with_delegated_manager(school_code)
    super_headers = await login_tenant(
        client, school_code=school_code, login_id=SUPER_LOGIN, password=SUPER_PASSWORD
    )
    admin_headers = await login_tenant(
        client, school_code=school_code, login_id=ADMIN_LOGIN, password=ADMIN_PASSWORD
    )
    admin_role_id = await _role_id(seeded["tenant_id"], "school_admin")
    current = await _state(client, super_headers, "school_admin")
    granted = await client.put(
        f"/api/v1/school/roles/{admin_role_id}/permissions",
        json={
            "permission_codes": sorted(SCHOOL_ADMIN_DEFAULTS | {"school.role.manage"}),
            "version": int(current["version"]),
            "reason": "超管委托管理员代管角色权限（场景 BS-M2UI3-D 前置）",
        },
        headers=super_headers,
    )
    assert granted.status_code == 200, granted.text
    # 委托这一步不该留下自扩权痕迹：超管没有绑在 school_admin 上，能力面不流向他本人
    assert await _audit_entries("school.role.self_grant") == []
    return {
        "tenant_id": seeded["tenant_id"],
        "admin_role_id": admin_role_id,
        "teacher_role_id": await _role_id(seeded["tenant_id"], "teacher_b_end"),
        "super_headers": super_headers,
        "admin_headers": admin_headers,
        "version": int(granted.json()["version"]),
    }


async def _id_card_status(client: AsyncClient, headers: dict[str, str]) -> int:
    return (
        await client.post(
            f"/api/v1/school/personnel/{MISSING_PERSONNEL_ID}/id-card",
            json={"reason": READ_REASON},
            headers=headers,
        )
    ).status_code


async def test_self_expansion_requires_ack_then_leaves_a_dedicated_audit_trail(
    client: AsyncClient, delegated: dict[str, Any]
) -> None:
    """未确认 422 → 确认无理由 422 → 确认有理由 200；两次打断都不动库，放行后能力立即生效。

    提交集**不含** `school.role.manage`：携带它会先撞上 M2-UI2 的转授禁令（用例第一步就是钉这个
    顺序），根本到不了本守卫。也就是说"管理员自助换掉 role.manage 去拿别的敏感码"这条路，
    以前只有 M2-UI2 挡不住、现在由 TD-191 守卫接管——代价是这次配置会把 role.manage 一起交出。
    """

    tenant_id = delegated["tenant_id"]
    role_id = delegated["admin_role_id"]
    admin_headers = delegated["admin_headers"]
    version = delegated["version"]
    codes = sorted(SCHOOL_ADMIN_DEFAULTS | {SENSITIVE_CODE})

    # 权限位先确认是"缺"的：403 而不是 404，说明后面的翻转确实由本次写操作造成
    assert await _id_card_status(client, admin_headers) == 403

    # 第一道墙（M2-UI2）：非超管提交集里出现 role.manage → 转授禁令先于自扩权守卫
    with_manage = await client.put(
        f"/api/v1/school/roles/{role_id}/permissions",
        json={
            "permission_codes": sorted(set(codes) | {"school.role.manage"}),
            "version": version,
            "reason": "带着 role.manage 提交（BS-M2UI3-D 禁令顺序）",
        },
        headers=admin_headers,
    )
    assert with_manage.status_code == 422, with_manage.text
    assert with_manage.json()["detail"]["code"] == "ROLE_CODE_NOT_DELEGABLE"

    unacked = await client.put(
        f"/api/v1/school/roles/{role_id}/permissions",
        json={"permission_codes": codes, "version": version, "reason": "岗位调整（BS-M2UI3-D）"},
        headers=admin_headers,
    )
    assert unacked.status_code == 422, unacked.text
    detail = unacked.json()["detail"]
    assert detail["code"] == "SELF_GRANT_CONFIRMATION_REQUIRED"
    # 消息点名触发的码：前端不必再猜是哪一格被挡
    assert SENSITIVE_CODE in detail["message"]
    assert int((await _role_row(tenant_id, role_id))["version"]) == version
    assert await _db_permission_codes(tenant_id, role_id) == (
        SCHOOL_ADMIN_DEFAULTS | {"school.role.manage"}
    )

    no_reason = await client.put(
        f"/api/v1/school/roles/{role_id}/permissions",
        json={"permission_codes": codes, "version": version, "self_grant_ack": True},
        headers=admin_headers,
    )
    assert no_reason.status_code == 422, no_reason.text
    assert no_reason.json()["detail"]["code"] == "SELF_GRANT_REASON_REQUIRED"
    blank_reason = await client.put(
        f"/api/v1/school/roles/{role_id}/permissions",
        json={
            "permission_codes": codes,
            "version": version,
            "self_grant_ack": True,
            "reason": "   ",
        },
        headers=admin_headers,
    )
    assert blank_reason.status_code == 422, blank_reason.text
    assert blank_reason.json()["detail"]["code"] == "SELF_GRANT_REASON_REQUIRED"
    assert int((await _role_row(tenant_id, role_id))["version"]) == version

    # 三次打断都属"越权动作被挡"，与安全拒绝同一口径留痕（独立事务：外层已回滚）
    denied = await _audit_entries("school.role.permissions.denied")
    assert [(e["result"], e["metadata"].get("reason")) for e in denied] == [
        ("denied", "ROLE_CODE_NOT_DELEGABLE"),
        ("denied", "SELF_GRANT_CONFIRMATION_REQUIRED"),
        ("denied", "SELF_GRANT_REASON_REQUIRED"),
        ("denied", "SELF_GRANT_REASON_REQUIRED"),
    ], denied
    assert {e["resource_id"] for e in denied} == {str(role_id)}
    # 专项审计**放行与打断都写**（简报 §6 决策 #4）：只记成功案例的报警等于只看得到得手的人。
    # 第一条 `ROLE_CODE_NOT_DELEGABLE` 不在这里——转授禁令先于自扩权判定触发，那次请求
    # 根本没走到守卫（钉的正是两条守卫的先后顺序）。
    interrupted = await _audit_entries("school.role.self_grant")
    assert [(e["result"], e["metadata"].get("reason")) for e in interrupted] == [
        ("denied", "SELF_GRANT_CONFIRMATION_REQUIRED"),
        ("denied", "SELF_GRANT_REASON_REQUIRED"),
        ("denied", "SELF_GRANT_REASON_REQUIRED"),
    ], interrupted
    assert all(e["resource_id"] == str(role_id) for e in interrupted)
    assert all(e["metadata"]["newly_reaching_self"] == [SENSITIVE_CODE] for e in interrupted)
    # 与成功侧同一套可读字段：报警消费方不该因为被拒就拿不到角色码
    assert all(e["metadata"]["role_code"] == "school_admin" for e in interrupted)
    # `self_grant_ack` 落在审计里才能区分"没勾框"和"勾了框但懒得写理由"
    assert [e["metadata"]["self_grant_ack"] for e in interrupted] == [False, True, True]
    # 拒绝侧的 `reason` 是判据码（与所有 `*.denied` 一致），成功侧才是操作者填的理由
    assert all(e["metadata"]["reason"].startswith("SELF_GRANT_") for e in interrupted)

    acked = await client.put(
        f"/api/v1/school/roles/{role_id}/permissions",
        json={
            "permission_codes": codes,
            "version": version,
            "self_grant_ack": True,
            "reason": "兼管人事档案核对，需要完整查看身份证（BS-M2UI3-D）",
        },
        headers=admin_headers,
    )
    assert acked.status_code == 200, acked.text
    assert acked.json()["version"] == version + 1
    assert set(acked.json()["permission_codes"]) == set(codes)
    assert await _db_permission_codes(tenant_id, role_id) == set(codes)

    grants = await _audit_entries("school.role.self_grant")
    assert len(grants) == 4, grants
    assert [e["result"] for e in grants] == ["denied", "denied", "denied", "success"], grants
    entry = grants[-1]
    assert entry["resource_id"] == str(role_id)
    assert entry["metadata"]["role_code"] == "school_admin"
    assert entry["metadata"]["newly_reaching_self"] == [SENSITIVE_CODE]
    assert entry["metadata"]["confirmed_by"] == "self_grant_ack"
    assert "兼管人事档案核对" in entry["metadata"]["reason"]
    # 常规 diff 仍在 `*.updated` 里：专项审计不替代通用流水，两者可交叉核对
    # （`removed` 里的 role.manage 就是这次自扩权的显式代价——换进来一个敏感码，交出一个转授权）
    updated = (await _audit_entries("school.role.permissions.updated"))[-1]
    assert updated["metadata"]["added"] == [SENSITIVE_CODE]
    assert updated["metadata"]["removed"] == ["school.role.manage"]

    # Then：同一条 token 下一请求即拿到该能力（403 → 404：权限到位，人员不存在）
    assert await _id_card_status(client, admin_headers) == 404



async def test_editing_another_role_with_the_same_code_needs_no_ack(
    client: AsyncClient, delegated: dict[str, Any]
) -> None:
    """同一个敏感码写进**自己不所在**的角色 → 正常配置，不打断、不产生专项审计。

    这条是"判据写宽了会挡掉日常配置"的反证：教师岗需要敏感查看权时，运营不该被迫勾选确认。
    """

    tenant_id = delegated["tenant_id"]
    teacher_role_id = delegated["teacher_role_id"]
    current = await _state(client, delegated["admin_headers"], "teacher_b_end")
    teacher_codes = sorted(set(current["permission_codes"]) | {SENSITIVE_CODE})

    routine = await client.put(
        f"/api/v1/school/roles/{teacher_role_id}/permissions",
        json={
            "permission_codes": teacher_codes,
            "version": int(current["version"]),
            "reason": "为教师 B 端开敏感查看（BS-M2UI3-D 正常配置流）",
        },
        headers=delegated["admin_headers"],
    )
    assert routine.status_code == 200, routine.text
    assert set(routine.json()["permission_codes"]) == set(teacher_codes)
    assert await _db_permission_codes(tenant_id, teacher_role_id) == set(teacher_codes)
    assert await _audit_entries("school.role.self_grant") == []
    # 管理员自己的权限面没有因为这次"给别人配"而变大
    assert await _id_card_status(client, delegated["admin_headers"]) == 403


async def test_code_already_held_is_not_an_expansion(
    client: AsyncClient, delegated: dict[str, Any]
) -> None:
    """本就持有该敏感码时把它补进自己所在角色 = 净增为零 → 无需 ack，也不留专项审计。

    由超管先把该码授到管理员所在的另一个角色（模拟"多角色/另一条授权来源"），
    管理员随后把它写进 `school_admin`：能力面没变大，挡它只会逼人绕路。
    """

    tenant_id = delegated["tenant_id"]
    role_id = delegated["admin_role_id"]

    # 用 teacher_b_end 作为"第二条来源"：把该码配到教师角色再绑给管理员本人
    teacher_role_id = delegated["teacher_role_id"]
    teacher_state = await _state(client, delegated["super_headers"], "teacher_b_end")
    granted_by_super = await client.put(
        f"/api/v1/school/roles/{teacher_role_id}/permissions",
        json={
            "permission_codes": sorted(set(teacher_state["permission_codes"]) | {SENSITIVE_CODE}),
            "version": int(teacher_state["version"]),
            "reason": "超管经另一角色授予敏感查看（BS-M2UI3-D 净增为零前置）",
        },
        headers=delegated["super_headers"],
    )
    assert granted_by_super.status_code == 200, granted_by_super.text

    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "insert into user_roles (tenant_id, user_id, role_id) "
                    "select :tid, u.id, :rid from users u "
                    "where u.tenant_id = :tid and u.login_id = :login"
                ),
                {
                    "tid": str(tenant_id),
                    "rid": str(teacher_role_id),
                    "login": ADMIN_LOGIN,
                },
            )
    finally:
        await engine.dispose()

    # 管理员已持有该码（经 teacher_b_end），再写进自己所在的 school_admin → 不触发守卫。
    # 提交集去掉 role.manage：那条码由 M2-UI2 的转授禁令独占，与本守卫的判据无关。
    admin_state = await _state(client, delegated["admin_headers"], "school_admin")
    codes = sorted((set(admin_state["permission_codes"]) | {SENSITIVE_CODE}) - {"school.role.manage"})
    replaced = await client.put(
        f"/api/v1/school/roles/{role_id}/permissions",
        json={
            "permission_codes": codes,
            "version": int(admin_state["version"]),
            "reason": "把已有的敏感查看补进本角色（BS-M2UI3-D 净增为零）",
        },
        headers=delegated["admin_headers"],
    )
    assert replaced.status_code == 200, replaced.text
    assert set(replaced.json()["permission_codes"]) == set(codes)
    assert await _audit_entries("school.role.self_grant") == []


async def test_super_admin_self_expansion_skips_the_interrupt_but_still_leaves_the_trail(
    client: AsyncClient, delegated: dict[str, Any]
) -> None:
    """超管免的是打断，不是免留痕：不勾确认也 200，但专项审计记 `confirmed_by=super_admin`。

    这条分支在**一致种子下不可达**：`school_super_admin` 按矩阵 §4 持本校 school+campus 全量，
    而敏感清单里的码全在 school 域 → 超管的持有并集必然覆盖它们 → 净增恒为空。能让它成立的
    只有"超管角色的映射落后于代码目录"（存量库、同步未跑或被人工修剪过）。用例因此显式删掉
    该租户超管角色上的这一条映射来还原漂移态：钉的是**豁免分支本身**——目录落后时不能把最高
    权限账号锁在自己的角色配置页外（他会变成既不能确认、也不能改的唯一角色），
    而不是钉一条运营日常会走到的路径。留痕照常写，事后仍可复盘与撤销。
    """

    tenant_id = delegated["tenant_id"]
    role_id = delegated["admin_role_id"]
    super_headers = delegated["super_headers"]
    super_role_id = await _role_id(tenant_id, "school_super_admin")

    # 前置 1：超管也绑进 `school_admin`（"触达自己"必须真的成立，否则这条用例什么都不是）
    # 前置 2：从超管自己的角色映射里摘掉该敏感码，制造"持有并集落后于目录"的漂移态
    engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "insert into user_roles (tenant_id, user_id, role_id) "
                    "select :tid, u.id, :rid from users u "
                    "where u.tenant_id = :tid and u.login_id = :login"
                ),
                {
                    "tid": str(tenant_id),
                    "rid": str(role_id),
                    "login": SUPER_LOGIN,
                },
            )
            deleted: Any = await session.execute(
                text(
                    "delete from role_permissions rp "
                    "using permissions p "
                    "where rp.permission_id = p.id and rp.role_id = :rid and p.code = :code"
                ),
                {"rid": str(super_role_id), "code": SENSITIVE_CODE},
            )
            assert deleted.rowcount == 1, (
                "前置不成立：超管角色本来就没有这条映射，用例没有制造出漂移态"
            )
    finally:
        await engine.dispose()

    current = await _state(client, super_headers, "school_admin")
    codes = sorted(set(current["permission_codes"]) | {SENSITIVE_CODE})

    passed = await client.put(
        f"/api/v1/school/roles/{role_id}/permissions",
        json={
            "permission_codes": codes,
            "version": int(current["version"]),
            "reason": "超管补授敏感查看（BS-M2UI3-D 超管分支）",
        },
        headers=super_headers,
    )
    # 没有 self_grant_ack，仍是 200：打断只挡得住普通主体
    assert passed.status_code == 200, passed.text
    assert passed.json()["version"] == int(current["version"]) + 1
    assert await _db_permission_codes(tenant_id, role_id) == set(codes)
    assert await _audit_entries("school.role.permissions.denied") == []

    grants = await _audit_entries("school.role.self_grant")
    assert len(grants) == 1, grants
    entry = grants[0]
    assert entry["result"] == "success"
    assert entry["resource_id"] == str(role_id)
    assert entry["metadata"]["role_code"] == "school_admin"
    assert entry["metadata"]["newly_reaching_self"] == [SENSITIVE_CODE]
    assert entry["metadata"]["confirmed_by"] == "super_admin"
    assert "超管补授敏感查看" in entry["metadata"]["reason"]
