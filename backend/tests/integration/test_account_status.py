"""权限账号停用/启用的服务层不变量（M2-UI3 TD-187e）。

业务场景（`tests/business/test_bs_m2ui3_account_status.py`）走 HTTP，请求天然串行；这里补八类
**只有服务层/并发才成立**的性质：

1. 停用的撤会话是「一步到位但只断活着的」：该账号**未撤销且未过期**的刷新家族置
   `admin_revoke`、`token_version+1`、Redis 会话标记与 MFA 待激活 secret 一并删除——少一步
   就等于没停用；已过期家族不撤销也不算进 `sessions_revoked`（设备列表本来就看不见它）；
2. 状态矩阵与目标保护：`active/locked → disabled`、`disabled → active`，重复动作 409、
   超管账号一律 403、跨租/已删除同形 404、学生等 C 端账号 422（不属于权限账号）、
   已删除且仍持有超管绑定的同形 404（删除判据先于角色，不泄露"曾为超管"）；
   每条拒绝都留 `school.user.status_denied`，且被拒目标的库内状态与会话一个字节都没变；
   成功与拒绝两侧的 `actor_id` 都是操作者本人（"谁停用的/谁在试探"要查得出来）；
3. 自停用先于**目标账号的任何读取与状态写入**：操作者 id 在库里不存在时也应得到 409 而不是
   404（拒绝审计是另一条独立事务，不在此列）；
4. 并发停用恰好一路成功：账号级 advisory lock + 行锁串行化第二个请求，`token_version` 只前进一次；
5. 跨租 id 不进那把账号锁：advisory lock 的 key 只有 `realm + user_id`、不含租户，所以归属
   探针必须先于取锁，否则本租管理员能用别租账号的 id 排进对方登录链路的锁队列（第六轮 M1 → TD-230）；
6. 只持账号锁、不写任何行的并发事务也会把停用挡在外面——这条钉的是 advisory lock **本身**承重，
   不是它前面那层 `users` 行锁的副产品（原 TD-227 的疑问在此收口）；
7. 归属探针的 404 路径**一次只占一条数据库连接**：探针跑在自己的短事务里，拒绝审计另起一条，
   两者不叠加。这条同时覆盖 `account_admin`（停用）与 `mfa_admin`（管理员 MFA 重置）两个模块——
   探针是同一批（TD-230 / 第七轮 m3）以同一形状加进去的，只测一边等于另一边没测；
8. **MFA 重置侧**的别租 id 同样不进对方的账号锁队列：第 5 条只测了停用侧，`mfa_admin` 的探针
   是同批同形加的却没有独立回归网（TD-230 → 187f-A T5）。谁以后把 MFA 的归属判定挪回取锁
   之后，第 5 条仍然绿、这条会转红。

`_SessionLedger` 计的是 `db_session` 作用域的嵌套层数；一次作用域恰好借还一条池连接
（`app/db/session.py` 的 `factory() as session, session.begin()`），所以"峰值 = 1"就是"任一时刻只占一条连接"。

不测 RLS 兜底：那由迁移与 `test_rls_policy_coverage.py` 覆盖，这里再造一遍只会断言"由库拒绝"。
"""

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.auth import revocation
from app.auth.principal import Principal
from app.auth.sessions import acquire_user_lock
from app.config import get_settings
from app.db.session import db_session, dispose_engine
from app.personnel import account_admin, mfa_admin
from app.personnel.account_admin import (
    AccountAdminError,
    StatusChangeResult,
    activate_user,
    deactivate_user,
)
from app.personnel.mfa_admin import MfaAdminError, MfaResetResult, reset_user_mfa
from app.security.encryption import decode_kek, encrypt_secret, mfa_aad
from app.security.totp import generate_totp_secret
from tests.support.auth import seed_tenant_user
from tests.support.db import clean_test_data, database_url, make_test_engine, upgrade_test_database
from tests.support.dsn import assert_test_database_dsn

pytestmark = pytest.mark.integration

PASSWORD = "Account-Status-Pass-1"
REASON = "教师离职，账号立即停用并撤销全部会话"


class _SessionLedger:
    """给某个服务模块的 `db_session` 包一层借还计数（一次作用域 == 一次池连接借还）。

    用法：`monkeypatch.setattr(<module>, "db_session", ledger)` 后读 `peak`/`opens`。
    `peak == 1` 断言的是"这条路径不会同时持有两个事务作用域"——探针挂在主事务里、或把拒绝
    审计并回主事务，都会让峰值变 2（第七轮 m3 的反证已实测 `open/open/close/close`）。
    """

    def __init__(self) -> None:
        self.opens: list[str] = []
        self.peak = 0
        self._active = 0

    def reset(self) -> None:
        self.opens.clear()
        self.peak = 0
        self._active = 0

    @contextlib.asynccontextmanager
    async def __call__(
        self, *, tenant_id: UUID | str | None = None, platform: bool = False
    ) -> AsyncIterator[Any]:
        self._active += 1
        self.peak = max(self.peak, self._active)
        self.opens.append("open")
        try:
            async with db_session(tenant_id=tenant_id, platform=platform) as session:
                yield session
        finally:
            self._active -= 1
            self.opens.append("close")


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
    await revocation.reset_client()
    yield
    await revocation.reset_client()
    await dispose_engine()
    get_settings.cache_clear()


async def _seed(school_code: str) -> dict[str, Any]:
    """租户 + 两名操作者/超管/普通教师/已锁定/已删除/已删除超管/学生/他租用户。

    教师与已锁定账号绑非超管角色（用于钉住"启停不动绑定"）；已删除超管故意保留超管绑定，
    用于验证"删除判据先于角色判据"。
    """

    engine = make_test_engine("admin")
    try:
        tenant_id, operator_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"status-operator-{school_code}",
            password=PASSWORD,
            user_type="school_admin",
            permissions=("school.permission_account.manage",),
            scope_types=("school",),
        )
        _, super_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"status-super-{school_code}",
            password=PASSWORD,
            user_type="school_admin",
        )
        _, teacher_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"status-teacher-{school_code}",
            password=PASSWORD,
            user_type="teacher",
        )
        # 第二名持码管理员：并发用例要的是"两个不同主体抢同一账号"，
        # 用同一个 id 复制两份 Principal 只能测到同一主体的两路请求。
        _, second_operator_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"status-operator2-{school_code}",
            password=PASSWORD,
            user_type="school_admin",
            permissions=("school.permission_account.manage",),
            scope_types=("school",),
        )
        _, locked_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"status-locked-{school_code}",
            password=PASSWORD,
            user_type="teacher",
            status="locked",
        )
        _, deleted_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"status-deleted-{school_code}",
            password=PASSWORD,
            user_type="teacher",
            status="deleted",
        )
        _, deleted_super_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"status-deleted-super-{school_code}",
            password=PASSWORD,
            user_type="school_admin",
            status="deleted",
        )
        _, student_id = await seed_tenant_user(
            engine,
            school_code=school_code,
            login_id=f"status-student-{school_code}",
            password=PASSWORD,
            user_type="student",
        )
        foreign_tenant_id, foreign_user_id = await seed_tenant_user(
            engine,
            school_code=f"{school_code}X",
            login_id=f"status-foreign-{school_code}",
            password=PASSWORD,
            user_type="teacher",
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text("select seed_builtin_roles(:tid)"), {"tid": str(tenant_id)}
            )
            await session.execute(
                text("select seed_builtin_roles(:tid)"), {"tid": str(foreign_tenant_id)}
            )
            await session.execute(
                text(
                    "insert into public.user_roles (tenant_id, user_id, role_id) "
                    "select :tid, :uid, r.id from public.roles r "
                    "where r.tenant_id = :tid and r.code = 'school_super_admin'"
                ),
                {"tid": str(tenant_id), "uid": str(super_id)},
            )
            # 已删除但仍挂着超管绑定：删除判据必须先于角色判据（404 而不是 403）
            await session.execute(
                text(
                    "insert into public.user_roles (tenant_id, user_id, role_id) "
                    "select :tid, :uid, r.id from public.roles r "
                    "where r.tenant_id = :tid and r.code = 'school_super_admin'"
                ),
                {"tid": str(tenant_id), "uid": str(deleted_super_id)},
            )
            # 非超管角色：停用/启用都不该动绑定，用它来钉住"只改状态"的边界。
            for holder in (teacher_id, locked_id):
                await session.execute(
                    text(
                        "insert into public.user_roles (tenant_id, user_id, role_id) "
                        "select :tid, :uid, r.id from public.roles r "
                        "where r.tenant_id = :tid and r.code = 'teacher_b_end'"
                    ),
                    {"tid": str(tenant_id), "uid": str(holder)},
                )
            await session.execute(
                text(
                    "update public.users set failed_login_attempts = 3, "
                    "failed_login_window_started_at = now(), "
                    "locked_until = now() + interval '10 minutes' "
                    "where tenant_id = :tid and id = :uid"
                ),
                {"tid": str(tenant_id), "uid": str(locked_id)},
            )
    finally:
        await engine.dispose()

    return {
        "tenant_id": tenant_id,
        "operator_id": operator_id,
        "second_operator_id": second_operator_id,
        "super_id": super_id,
        "teacher_id": teacher_id,
        "locked_id": locked_id,
        "deleted_id": deleted_id,
        "deleted_super_id": deleted_super_id,
        "student_id": student_id,
        "foreign_tenant_id": foreign_tenant_id,
        "foreign_user_id": foreign_user_id,
    }


def _principal(
    user_id: UUID,
    tenant_id: UUID | None,
    *,
    realm: str = "tenant",
    roles: frozenset[str] = frozenset(),
) -> Principal:
    return Principal(
        user_id=user_id,
        realm=realm,  # type: ignore[arg-type]
        tenant_id=tenant_id,
        jti=uuid4(),
        session_id=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        must_change_password=False,
        roles=roles,
        permissions=frozenset({"school.permission_account.manage"}),
    )


async def _seed_session(
    tenant_id: UUID, user_id: UUID, *, ttl: timedelta = timedelta(days=1)
) -> UUID:
    """插一条刷新令牌并登记 Redis 会话标记，返回 family_id（= session_id 口径）。

    `ttl` 传负数（如 `timedelta(hours=-1)`）得到**已过期**家族：它未撤销但换不出新令牌，用来钉住
    "停用只撤还活着的那批"。过期家族不登记 Redis 标记——标记 TTL 就是刷新令牌有效期，
    过期行在生产里本来就没有活着的标记，硬登记会造出假状态。
    """

    family_id = uuid4()
    async with db_session(tenant_id=tenant_id) as session:
        await session.execute(
            text(
                "insert into public.refresh_tokens "
                "(tenant_id, jti, family_id, user_id, token_hash, expires_at, last_seen_at) "
                "values (:tid, :jti, :fid, :uid, :hash, now() + :ttl, now())"
            ),
            {
                "tid": tenant_id,
                "jti": uuid4(),
                "fid": family_id,
                "uid": user_id,
                "hash": uuid4().hex + uuid4().hex,
                "ttl": ttl,
            },
        )
    if ttl > timedelta(0):
        await revocation.register_session(
            family_id, realm="tenant", ttl_seconds=int(ttl.total_seconds())
        )
    return family_id


async def _enable_mfa(tenant_id: UUID, user_id: UUID) -> None:
    """直接写加密 secret 并启用（绑定流程由 M1-D2 覆盖，这里只要目标状态）。"""

    encrypted = encrypt_secret(
        generate_totp_secret(),
        kek=decode_kek(get_settings().encryption_kek),
        aad=mfa_aad("tenant", user_id),
    )
    async with db_session(tenant_id=tenant_id) as session:
        await session.execute(
            text(
                "update public.users set mfa_enabled = true, mfa_secret_enc = :enc "
                "where tenant_id = :tid and id = :uid"
            ),
            {"enc": encrypted, "tid": tenant_id, "uid": user_id},
        )


async def _user_row(tenant_id: UUID, user_id: UUID) -> dict[str, Any]:
    async with db_session(tenant_id=tenant_id) as session:
        row = (
            (
                await session.execute(
                    text(
                        "select status, token_version, roles_version, mfa_enabled, "
                        "failed_login_attempts, "
                        "failed_login_window_started_at, locked_until from public.users "
                        "where tenant_id = :tid and id = :uid"
                    ),
                    {"tid": tenant_id, "uid": user_id},
                )
            )
            .mappings()
            .one_or_none()
        )
    return {} if row is None else dict(row)


async def _refresh_rows(tenant_id: UUID, user_id: UUID) -> list[dict[str, Any]]:
    async with db_session(tenant_id=tenant_id) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select family_id, revoked_at, revoked_reason from public.refresh_tokens "
                        "where tenant_id = :tid and user_id = :uid order by issued_at"
                    ),
                    {"tid": tenant_id, "uid": user_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _role_codes(tenant_id: UUID, user_id: UUID) -> list[str]:
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
    return sorted(str(code) for code in rows)


async def _audit_rows(action: str, tenant_id: UUID) -> list[dict[str, Any]]:
    """按 `action` + 本用例租户取审计。

    必须带租户过滤：用例断言的是**精确列表**（顺序与条数），别租户（=别的用例）残留的
    同名审计会把断言打成红且难以归因；平台连接能读全部审计，所以作用域要自己收紧。
    """

    async with db_session(platform=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select resource_id, actor_id::text as actor_id, result, metadata_json "
                        "from audit_logs "
                        "where action = :action and tenant_id = :tid order by sequence"
                    ),
                    {"action": action, "tid": tenant_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _audit_rows_all(action: str) -> list[dict[str, Any]]:
    """按 `action` 取审计（**全表口径**，不按租户收窄）。

    「这条动作一次都不该发生」类断言必须用它：按租户收窄会漏掉**写错租户**的那一行，
    而那恰恰是要抓的 bug。精确条数类断言才用带租户的 `_audit_rows`（TD-228）。
    """

    async with db_session(platform=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select resource_id, actor_id::text as actor_id, result, metadata_json "
                        "from audit_logs where action = :action order by sequence"
                    ),
                    {"action": action},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def test_deactivate_revokes_every_family_and_clears_redis_state(stack: None) -> None:
    """Given 教师两条活跃会话、一条已过期家族与待激活 MFA secret；When 停用；Then 只断掉活着的。"""

    school_code = f"SCH-STATUS{uuid4().hex[:6].upper()}"
    seeded = await _seed(school_code)
    tenant_id = seeded["tenant_id"]
    operator = _principal(seeded["operator_id"], tenant_id)
    first_family = await _seed_session(tenant_id, seeded["teacher_id"])
    second_family = await _seed_session(tenant_id, seeded["teacher_id"])
    # 已过期但未撤销：设备列表看不见它（`list_sessions` 过滤 `expires_at > now()`），
    # 停用也不该把它算进 `sessions_revoked`，否则"这次断了几台设备"是虚报。
    expired_family = await _seed_session(tenant_id, seeded["teacher_id"], ttl=timedelta(hours=-1))
    await revocation.store_mfa_pending_secret(
        "pending-ciphertext", realm="tenant", user_id=seeded["teacher_id"], ttl_seconds=600
    )
    assert await revocation.session_exists(first_family) is True
    before = await _user_row(tenant_id, seeded["teacher_id"])
    assert before["status"] == "active"

    result = await deactivate_user(operator, seeded["teacher_id"], reason=REASON)

    assert isinstance(result, StatusChangeResult)
    assert result.status == "disabled"
    assert result.status_before == "active"
    assert result.sessions_revoked == 2
    after = await _user_row(tenant_id, seeded["teacher_id"])
    assert after["status"] == "disabled"
    assert after["token_version"] == before["token_version"] + 1
    # MFA 绑定本身保留：停用不是重置，启用后原绑定还在（清待激活 secret 是另一回事）
    assert after["mfa_enabled"] == before["mfa_enabled"]

    families = {first_family, second_family}
    rows = await _refresh_rows(tenant_id, seeded["teacher_id"])
    assert {row["family_id"] for row in rows} == families | {expired_family}
    revoked = {row["family_id"] for row in rows if row["revoked_at"] is not None}
    assert revoked == families
    assert {row["revoked_reason"] for row in rows if row["revoked_at"] is not None} == {
        "admin_revoke"
    }
    # 过期家族原样不动：它本来就换不出令牌，动它只会污染台账
    expired_row = next(row for row in rows if row["family_id"] == expired_family)
    assert expired_row["revoked_at"] is None
    assert expired_row["revoked_reason"] is None
    for family_id in families:
        assert await revocation.session_exists(family_id) is False

    # 提交后删除待激活 secret：停用中的账号不能再完成 MFA 绑定
    assert (
        await revocation.get_mfa_pending_secret(realm="tenant", user_id=seeded["teacher_id"])
        is None
    )

    audits = await _audit_rows("school.user.deactivated", tenant_id)
    assert len(audits) == 1
    metadata = audits[0]["metadata_json"]
    assert set(metadata) == {"status_before", "sessions_revoked", "reason"}
    assert metadata["status_before"] == "active"
    assert metadata["sessions_revoked"] == 2
    assert REASON in metadata["reason"]
    assert audits[0]["result"] == "success"
    # "谁停用的"必须查得出来：actor 落的是操作者本人，不是目标账号
    assert audits[0]["actor_id"] == str(seeded["operator_id"])
    assert audits[0]["resource_id"] == str(seeded["teacher_id"])


async def test_mfa_reset_counts_only_live_families(stack: None) -> None:
    """Given 教师两条活跃会话与一条已过期家族；When 管理员重置其 MFA；Then 只数还活着的那些。

    停用侧已经这么算了（`test_deactivate_revokes_every_family_and_clears_redis_state`），
    MFA 重置侧此前按"全部未撤销"计数，同一个 `sessions_revoked` 键跨动作对比会误判（TD-231）。
    """

    school_code = f"SCH-STATUS{uuid4().hex[:6].upper()}"
    seeded = await _seed(school_code)
    tenant_id = seeded["tenant_id"]
    operator = _principal(seeded["operator_id"], tenant_id)
    teacher_id = seeded["teacher_id"]
    await _enable_mfa(tenant_id, teacher_id)
    first_family = await _seed_session(tenant_id, teacher_id)
    second_family = await _seed_session(tenant_id, teacher_id)
    expired_family = await _seed_session(tenant_id, teacher_id, ttl=timedelta(hours=-1))

    result = await reset_user_mfa(operator, teacher_id, reason=REASON)

    assert isinstance(result, MfaResetResult)
    assert result.sessions_revoked == 2
    families = {first_family, second_family}
    for family_id in families:
        assert await revocation.session_exists(family_id) is False

    # 撤销动作仍是全量（死行一并标上），但计数的只有活着的那些
    rows = await _refresh_rows(tenant_id, teacher_id)
    assert {row["family_id"] for row in rows} == families | {expired_family}
    assert all(row["revoked_at"] is not None for row in rows)

    audits = await _audit_rows("school.user.mfa_reset", tenant_id)
    assert len(audits) == 1
    metadata = audits[0]["metadata_json"]
    assert set(metadata) == {"sessions_revoked", "reason"}
    # 布尔是 int 的子类：`True == 2` 恒假，但 `True == 1` 会蒙混过关，类型也得钉住
    assert type(metadata["sessions_revoked"]) is int
    assert metadata["sessions_revoked"] == 2


async def test_status_matrix_guards_and_denied_audits(stack: None) -> None:
    """Given 活跃/锁定/已删除/他租/超管/学生六类目标；When 逐项停用或启用；Then 状态机与拒绝留痕一致。"""

    school_code = f"SCH-STATUS{uuid4().hex[:6].upper()}"
    seeded = await _seed(school_code)
    tenant_id = seeded["tenant_id"]
    operator = _principal(seeded["operator_id"], tenant_id)
    teacher_id = seeded["teacher_id"]

    # active → disabled → 再停用 409 → 启用 → 再启用 409
    await deactivate_user(operator, teacher_id, reason=REASON)
    with pytest.raises(AccountAdminError) as already_disabled:
        await deactivate_user(operator, teacher_id, reason=REASON)
    assert already_disabled.value.code == "ACCOUNT_ALREADY_DISABLED"
    assert already_disabled.value.http_status == 409

    await activate_user(operator, teacher_id)
    with pytest.raises(AccountAdminError) as already_active:
        await activate_user(operator, teacher_id)
    assert already_active.value.code == "ACCOUNT_ALREADY_ACTIVE"
    assert already_active.value.http_status == 409

    # locked：可停用；但启用只能从 disabled 走
    with pytest.raises(AccountAdminError) as not_disabled:
        await activate_user(operator, seeded["locked_id"])
    assert not_disabled.value.code == "ACCOUNT_NOT_DISABLED"
    assert not_disabled.value.http_status == 409
    locked_before = await _user_row(tenant_id, seeded["locked_id"])
    await deactivate_user(operator, seeded["locked_id"], reason=REASON)
    assert (await _user_row(tenant_id, seeded["locked_id"]))["status"] == "disabled"
    assert locked_before["status"] == "locked"

    # deleted / 跨租：同形 404，且目标行不变
    for target in (seeded["deleted_id"], seeded["foreign_user_id"]):
        with pytest.raises(AccountAdminError) as missing:
            await deactivate_user(operator, target, reason=REASON)
        assert missing.value.code == "USER_NOT_FOUND"
        assert missing.value.http_status == 404
    assert (await _user_row(tenant_id, seeded["deleted_id"]))["status"] == "deleted"
    foreign_row = await _user_row(seeded["foreign_tenant_id"], seeded["foreign_user_id"])
    assert foreign_row["status"] == "active"

    # 已删除但仍持有超管绑定：两个方向都按 404 收口，不泄露"这个 id 曾是超管"
    for target_operation in ("deactivate", "activate"):
        with pytest.raises(AccountAdminError) as deleted_super:
            if target_operation == "deactivate":
                await deactivate_user(operator, seeded["deleted_super_id"], reason=REASON)
            else:
                await activate_user(operator, seeded["deleted_super_id"])
        assert deleted_super.value.code == "USER_NOT_FOUND"
        assert deleted_super.value.http_status == 404
    assert (await _user_row(tenant_id, seeded["deleted_super_id"]))["status"] == "deleted"

    # 学生是 C 端账号，不属于"权限账号"：类型判据先于状态机，两个方向都 422
    student_family = await _seed_session(tenant_id, seeded["student_id"])
    for target_operation in ("deactivate", "activate"):
        with pytest.raises(AccountAdminError) as student:
            if target_operation == "deactivate":
                await deactivate_user(operator, seeded["student_id"], reason=REASON)
            else:
                await activate_user(operator, seeded["student_id"])
        assert student.value.code == "ACCOUNT_TARGET_NOT_ALLOWED"
        assert student.value.http_status == 422
    assert (await _user_row(tenant_id, seeded["student_id"]))["status"] == "active"
    assert await revocation.session_exists(student_family) is True
    assert all(
        row["revoked_at"] is None
        for row in await _refresh_rows(tenant_id, seeded["student_id"])
    )

    # 超管：停用与启用都 403（启用方向也先撞保护，而不是 409 ALREADY_ACTIVE）
    super_family = await _seed_session(tenant_id, seeded["super_id"])
    with pytest.raises(AccountAdminError) as forbidden:
        await deactivate_user(operator, seeded["super_id"], reason=REASON)
    assert forbidden.value.code == "ACCOUNT_SUPER_ADMIN_FORBIDDEN"
    assert forbidden.value.http_status == 403
    with pytest.raises(AccountAdminError) as forbidden_activate:
        await activate_user(operator, seeded["super_id"])
    assert forbidden_activate.value.code == "ACCOUNT_SUPER_ADMIN_FORBIDDEN"
    assert forbidden_activate.value.http_status == 403
    assert (await _user_row(tenant_id, seeded["super_id"]))["status"] == "active"
    # 被拒的停用不得撤销任何会话：超管的刷新家族与 Redis 标记都还在
    assert await revocation.session_exists(super_family) is True
    assert all(
        row["revoked_at"] is None for row in await _refresh_rows(tenant_id, seeded["super_id"])
    )

    # 主体 realm 不符：403，且不写任何用户状态
    platform_operator = _principal(seeded["operator_id"], None, realm="platform")
    with pytest.raises(AccountAdminError) as realm_mismatch:
        await deactivate_user(platform_operator, teacher_id, reason=REASON)
    assert realm_mismatch.value.code == "REALM_MISMATCH"
    assert realm_mismatch.value.http_status == 403

    # 理由过短：入参非法，422，不落安全审计（与 RESET_REASON_REQUIRED 同口径）
    with pytest.raises(AccountAdminError) as short_reason:
        await deactivate_user(operator, teacher_id, reason="  短  ")
    assert short_reason.value.code == "DEACTIVATE_REASON_REQUIRED"
    assert short_reason.value.http_status == 422
    assert (await _user_row(tenant_id, teacher_id))["status"] == "active"

    denied = await _audit_rows("school.user.status_denied", tenant_id)
    assert [row["metadata_json"]["reason"] for row in denied] == [
        "ACCOUNT_ALREADY_DISABLED",
        "ACCOUNT_ALREADY_ACTIVE",
        "ACCOUNT_NOT_DISABLED",
        "USER_NOT_FOUND",
        "USER_NOT_FOUND",
        "USER_NOT_FOUND",
        "USER_NOT_FOUND",
        "ACCOUNT_TARGET_NOT_ALLOWED",
        "ACCOUNT_TARGET_NOT_ALLOWED",
        "ACCOUNT_SUPER_ADMIN_FORBIDDEN",
        "ACCOUNT_SUPER_ADMIN_FORBIDDEN",
    ], denied
    assert {row["result"] for row in denied} == {"denied"}
    assert [row["metadata_json"]["operation"] for row in denied] == [
        "deactivate",
        "activate",
        "activate",
        "deactivate",
        "deactivate",
        "deactivate",
        "activate",
        "deactivate",
        "activate",
        "deactivate",
        "activate",
    ]
    assert denied[3]["resource_id"] == str(seeded["deleted_id"])
    assert denied[4]["resource_id"] == str(seeded["foreign_user_id"])
    assert denied[5]["resource_id"] == str(seeded["deleted_super_id"])
    assert denied[6]["resource_id"] == str(seeded["deleted_super_id"])
    assert denied[7]["resource_id"] == str(seeded["student_id"])
    assert denied[8]["resource_id"] == str(seeded["student_id"])
    for row in denied:
        assert set(row["metadata_json"]) == {"operation", "reason"}
        assert school_code not in str(row["metadata_json"])
        # realm 不符那条在 `_require_tenant` 就抛出，压根走不到审计；理由过短属入参非法族，
        # 同样不落痕——所以这里恰好 11 条，每条都记着"是谁在试探"。
        assert row["actor_id"] == str(seeded["operator_id"])

    success = await _audit_rows("school.user.deactivated", tenant_id)
    assert [row["metadata_json"]["status_before"] for row in success] == [
        "active",
        "locked",
    ]
    assert [row["actor_id"] for row in success] == [str(seeded["operator_id"])] * 2
    activated = await _audit_rows("school.user.activated", tenant_id)
    assert len(activated) == 1
    assert activated[0]["metadata_json"] == {"status_before": "disabled"}
    assert activated[0]["actor_id"] == str(seeded["operator_id"])


async def test_self_deactivate_is_rejected_before_touching_the_target(stack: None) -> None:
    """Given 操作者 id 在库里不存在；When 停用自己；Then 409 自停用（而不是 404）。

    这条钉的是判据顺序：自停用先看"是不是我自己"，因此一个库里根本不存在的 id 也不会被
    降级成 404（那等于告诉探测者"这个 id 不成立"）。它先于**对目标账号的任何读取与状态
    写入**；拒绝审计本身是另一条独立事务，不在此列。
    """

    school_code = f"SCH-STATUS{uuid4().hex[:6].upper()}"
    seeded = await _seed(school_code)
    tenant_id = seeded["tenant_id"]
    ghost_id = uuid4()
    operator = _principal(ghost_id, tenant_id)

    with pytest.raises(AccountAdminError) as self_deactivate:
        await deactivate_user(operator, ghost_id, reason=REASON)
    assert self_deactivate.value.code == "ACCOUNT_SELF_DEACTIVATE"
    assert self_deactivate.value.http_status == 409

    assert await _user_row(tenant_id, ghost_id) == {}
    denied = await _audit_rows("school.user.status_denied", tenant_id)
    assert len(denied) == 1
    assert denied[0]["resource_id"] == str(ghost_id)
    assert denied[0]["actor_id"] == str(ghost_id)
    assert denied[0]["metadata_json"] == {
        "operation": "deactivate",
        "reason": "ACCOUNT_SELF_DEACTIVATE",
    }


async def test_activate_clears_lockout_but_keeps_mfa_and_bindings(stack: None) -> None:
    """Given 已锁定且绑了角色/MFA 的教师；When 停用再启用；Then 只清锁定与状态，其余原样。"""

    school_code = f"SCH-STATUS{uuid4().hex[:6].upper()}"
    seeded = await _seed(school_code)
    tenant_id = seeded["tenant_id"]
    operator = _principal(seeded["operator_id"], tenant_id)
    locked_id = seeded["locked_id"]
    await _seed_session(tenant_id, locked_id)
    bindings_before = await _role_codes(tenant_id, locked_id)
    assert bindings_before == ["teacher_b_end"]
    await _enable_mfa(tenant_id, locked_id)

    locked_before = await _user_row(tenant_id, locked_id)
    assert locked_before["locked_until"] is not None
    deactivated = await deactivate_user(operator, locked_id, reason=REASON)
    assert deactivated.status_before == "locked"
    token_version_after_deactivate = (await _user_row(tenant_id, locked_id))["token_version"]
    assert token_version_after_deactivate == locked_before["token_version"] + 1

    result = await activate_user(operator, locked_id)

    assert result.status == "active"
    assert result.status_before == "disabled"
    assert result.sessions_revoked == 0
    after = await _user_row(tenant_id, locked_id)
    assert after["status"] == "active"
    assert after["failed_login_attempts"] == 0
    assert after["failed_login_window_started_at"] is None
    assert after["locked_until"] is None
    # 启用不是重置：MFA、角色绑定、刷新令牌、token 版本与绑定版本都不动
    assert after["mfa_enabled"] is True
    assert await _role_codes(tenant_id, locked_id) == bindings_before
    assert after["token_version"] == token_version_after_deactivate
    assert after["roles_version"] == locked_before["roles_version"]
    rows = await _refresh_rows(tenant_id, locked_id)
    assert all(row["revoked_reason"] == "admin_revoke" for row in rows)


async def test_concurrent_deactivate_has_exactly_one_winner(stack: None) -> None:
    """Given 两名管理员同时停用同一账号；When 并发执行；Then 恰好一路成功、版本只前进一次。

    这里钉的是"同一动作并发"：实测摘掉 `acquire_user_lock` 只剩 `users FOR UPDATE`，本用例
    仍然恰好一路成功（行锁足够）。跨动作的会话竞态见
    `test_deactivate_waits_for_an_inflight_session_write`（账号级锁区间要覆盖会话写入），
    "只持账号锁、不写任何行"的独立承重见 `test_deactivate_waits_for_a_lock_only_holder`。
    """

    school_code = f"SCH-STATUS{uuid4().hex[:6].upper()}"
    seeded = await _seed(school_code)
    tenant_id = seeded["tenant_id"]
    operator = _principal(seeded["operator_id"], tenant_id)
    second_operator = _principal(seeded["second_operator_id"], tenant_id)
    await _seed_session(tenant_id, seeded["teacher_id"])
    before = await _user_row(tenant_id, seeded["teacher_id"])

    outcomes = await asyncio.gather(
        deactivate_user(operator, seeded["teacher_id"], reason=REASON),
        deactivate_user(second_operator, seeded["teacher_id"], reason=REASON),
        return_exceptions=True,
    )

    results = [item for item in outcomes if isinstance(item, StatusChangeResult)]
    errors = [item for item in outcomes if isinstance(item, AccountAdminError)]
    assert len(results) == 1, outcomes
    assert len(errors) == 1, outcomes
    assert errors[0].code == "ACCOUNT_ALREADY_DISABLED"
    assert errors[0].http_status == 409

    after = await _user_row(tenant_id, seeded["teacher_id"])
    assert after["status"] == "disabled"
    assert after["token_version"] == before["token_version"] + 1
    success = await _audit_rows("school.user.deactivated", tenant_id)
    assert len(success) == 1
    denied = await _audit_rows("school.user.status_denied", tenant_id)
    assert [row["metadata_json"]["reason"] for row in denied] == ["ACCOUNT_ALREADY_DISABLED"]
    # 两个主体是不同的账号：赢的那条记赢家，撞状态机的那条记输家，且不是同一个人
    winner_actor = str(success[0]["actor_id"])
    loser_actor = str(denied[0]["actor_id"])
    assert {winner_actor, loser_actor} == {
        str(seeded["operator_id"]),
        str(seeded["second_operator_id"]),
    }
    assert winner_actor != loser_actor


async def _wait_until_backend_blocked(
    engine: AsyncEngine, holder_pid: int, *, timeout: float = 10.0
) -> None:
    """用 `pg_blocking_pids()` 观察到"确实有会话卡在账号锁上"，而不是靠固定 sleep 猜时序。

    探针走独立的**管理连接**：租户角色在 `pg_stat_activity` 里看不到别的会话的锁信息，
    只有超级用户视角能稳定拿到 `blockers`（`test_rbac_binding.py` 同一手法）。
    """

    probe = text(
        "select pid, state, wait_event_type, wait_event, pg_blocking_pids(pid) as blockers "
        "from pg_stat_activity "
        "where datname = current_database() and pid <> pg_backend_pid()"
    )
    rows: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    async with engine.connect() as conn:
        while time.monotonic() < deadline:
            rows = [dict(row) for row in (await conn.execute(probe)).mappings().all()]
            if any(holder_pid in row["blockers"] for row in rows):
                return
            await asyncio.sleep(0.02)
    raise AssertionError(f"没有会话在等本用例的账号锁；pg_stat_activity={rows}")


async def test_deactivate_waits_for_an_inflight_session_write(stack: None) -> None:
    """Given 刷新事务持着账号锁、新家族还没提交；When 停用抵达；Then 等它提交并把新家族一起撤销。

    这是"停用在读账号"的真实竞态：刷新正在写会话，停用若提前提交，读到的活跃家族是**旧集合**，
    那个刚签发的家族会带着有效刷新令牌活下来（撤销漏一个）。本用例钉的是"账号级锁区间覆盖
    会话写入"这个不变量，不是具体哪一层锁。

    摘锁实测（2026-10-29）：只摘 `acquire_user_lock` → 本用例仍通过（`users` 行锁与刷新侧 insert
    的 `FOR KEY SHARE` 冲突，兜住了）；只摘 `FOR UPDATE` → 也通过（advisory 兜住）；两层同摘 →
    转红，且是探针先失败（10s 内 `pg_stat_activity` 观察不到任何会话排在 holder 上），
    同时并发用例一起转红（两路都成功，`len(success) == 2`）。advisory lock 的"单独承重"
    由 `test_deactivate_waits_for_a_lock_only_holder` 单独钉住 → TD-227 已收口。
    """

    school_code = f"SCH-STATUS{uuid4().hex[:6].upper()}"
    seeded = await _seed(school_code)
    tenant_id = seeded["tenant_id"]
    teacher_id = seeded["teacher_id"]
    operator = _principal(seeded["operator_id"], tenant_id)
    family_id = uuid4()

    async def _settle() -> Any:
        try:
            return await deactivate_user(operator, teacher_id, reason=REASON)
        except AccountAdminError as exc:
            return exc

    holder_engine = make_test_engine("admin")
    probe_engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(holder_engine, expire_on_commit=False)
        async with factory() as holder, holder.begin():
            holder_pid = int(await holder.scalar(text("select pg_backend_pid()")))
            # 模拟刷新事务的次序：先取账号锁，再写新家族
            await acquire_user_lock(holder, realm="tenant", user_id=teacher_id)
            await holder.execute(
                text(
                    "insert into public.refresh_tokens "
                    "(tenant_id, jti, family_id, user_id, token_hash, expires_at, last_seen_at) "
                    "values (:tid, :jti, :fid, :uid, :hash, now() + interval '1 day', now())"
                ),
                {
                    "tid": tenant_id,
                    "jti": uuid4(),
                    "fid": family_id,
                    "uid": teacher_id,
                    "hash": uuid4().hex + uuid4().hex,
                },
            )
            task = asyncio.create_task(_settle())
            try:
                await _wait_until_backend_blocked(probe_engine, holder_pid)
                assert not task.done(), "停用应被账号锁挡在刷新事务之外"
                await revocation.register_session(family_id, realm="tenant")
            except BaseException:
                # 失败路径也要收掉还在等锁的停用：否则用例结束后它才提交，白占一条连接、
                # 返回值被静默丢弃。注意 cancel 只在"尚未提交"时有效——若停用已抢先提交，
                # 那条 `school.user.deactivated` 与 disabled 状态仍会落库（本用例用独立租户
                # uuid、且每条用例起始 TRUNCATE audit_logs，因此不会污染别条用例的断言）。
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                raise
            # holder 提交（退出 with）= 新家族对外可见；停用此时才继续，应把它一并撤销
        outcome = await asyncio.wait_for(task, timeout=20)

        assert isinstance(outcome, StatusChangeResult), outcome
        assert outcome.sessions_revoked == 1
        rows = await _refresh_rows(tenant_id, teacher_id)
        assert {row["family_id"] for row in rows} == {family_id}
        assert {row["revoked_reason"] for row in rows} == {"admin_revoke"}
        assert await revocation.session_exists(family_id) is False
    finally:
        await holder_engine.dispose()
        await probe_engine.dispose()


async def test_cross_tenant_user_id_does_not_take_their_account_lock(
    stack: None,
) -> None:
    """Given 他租账号正持着自己的会话写事务；When 本租管理员拿那个 id 来停用；Then 立刻 404，不排队。

    advisory lock 的 key 是 `realm + user_id`，**不含 tenant_id**。所以归属判定必须排在取锁
    之前：先取锁再查租户，本事务就会排进**另一个租户**那条账号的登录/刷新队列——404 的耗时
    泄露"这个 id 存在且正在写会话"，反复提交还能定向拖慢对方的登录链路（第六轮审查 M1 → TD-230）。
    holder 全程不提交，停用照样返回，就是"没进等待区间"的证据。
    """

    school_code = f"SCH-STATUS{uuid4().hex[:6].upper()}"
    seeded = await _seed(school_code)
    tenant_id = seeded["tenant_id"]
    foreign_id = seeded["foreign_user_id"]
    operator = _principal(seeded["operator_id"], tenant_id)

    holding = asyncio.Event()
    release = asyncio.Event()

    async def _hold_foreign_account_lock() -> None:
        holder_engine = make_test_engine("admin")
        try:
            factory = async_sessionmaker(holder_engine, expire_on_commit=False)
            async with factory() as holder, holder.begin():
                await acquire_user_lock(holder, realm="tenant", user_id=foreign_id)
                holding.set()
                await release.wait()
        finally:
            await holder_engine.dispose()

    holder_task = asyncio.create_task(_hold_foreign_account_lock())
    try:
        await asyncio.wait_for(holding.wait(), timeout=10)

        with pytest.raises(AccountAdminError) as cross:
            await deactivate_user(operator, foreign_id, reason=REASON)
        assert cross.value.code == "USER_NOT_FOUND"
        assert cross.value.http_status == 404
        # 走到这一行本身就是证据：`cross` 能拿到异常，说明停用**没有**排进那把别租账号的锁
        # （排进去的话会一直等到 holder 释放，直到 `backend/pyproject.toml` 的 `timeout = 120`
        # 由 pytest-timeout 打断——摘掉探针实测正是这个表现）。
        # 下面这条只是兜底断言——`release` 未置位时 holder 本来也不可能完成。
        assert not holder_task.done()
        denied = await _audit_rows("school.user.status_denied", tenant_id)
        assert [row["metadata_json"]["reason"] for row in denied] == ["USER_NOT_FOUND"]
        assert denied[0]["resource_id"] == str(foreign_id)
        assert denied[0]["actor_id"] == str(seeded["operator_id"])
        assert (await _user_row(seeded["foreign_tenant_id"], foreign_id))["status"] == "active"
    except BaseException:
        # 失败路径必须收掉 holder：它还卡在 `release.wait()` 上，会带着一条 PG 连接和那把
        # 别租账号的 advisory lock 拖进下一条用例（TD-228）。`cancel()` 对已完成任务是空操作。
        release.set()
        holder_task.cancel()
        with contextlib.suppress(BaseException):
            await holder_task
        raise
    else:
        release.set()
        await asyncio.wait_for(holder_task, timeout=10)


async def test_mfa_reset_cross_tenant_id_does_not_take_their_account_lock(
    stack: None,
) -> None:
    """Given 他租账号正持着自己的会话写事务；When 本租管理员拿那个 id 来重置 MFA；Then 立刻 404，不排队。

    与停用侧 `test_cross_tenant_user_id_does_not_take_their_account_lock` **同形**（TD-230 的
    187f-A T5）：`mfa_admin` 的归属探针是同批以同一形状加进去的，只给停用侧留用例，MFA 这侧
    就没有回归网——谁以后重排 MFA 的判据顺序（把归属判定挪回取锁之后），停用侧那条仍绿、
    这条转红。holder 全程不提交，重置照样返回，就是"没进等待区间"的证据。
    """

    school_code = f"SCH-STATUS{uuid4().hex[:6].upper()}"
    seeded = await _seed(school_code)
    tenant_id = seeded["tenant_id"]
    foreign_id = seeded["foreign_user_id"]
    foreign_tenant_id = seeded["foreign_tenant_id"]
    operator = _principal(seeded["operator_id"], tenant_id)
    before = await _user_row(foreign_tenant_id, foreign_id)

    holding = asyncio.Event()
    release = asyncio.Event()

    async def _hold_foreign_account_lock() -> None:
        holder_engine = make_test_engine("admin")
        try:
            factory = async_sessionmaker(holder_engine, expire_on_commit=False)
            async with factory() as holder, holder.begin():
                await acquire_user_lock(holder, realm="tenant", user_id=foreign_id)
                holding.set()
                await release.wait()
        finally:
            await holder_engine.dispose()

    holder_task = asyncio.create_task(_hold_foreign_account_lock())
    try:
        await asyncio.wait_for(holding.wait(), timeout=10)

        with pytest.raises(MfaAdminError) as cross:
            await reset_user_mfa(operator, foreign_id, reason=REASON)
        assert cross.value.code == "USER_NOT_FOUND"
        assert cross.value.http_status == 404
        # 能拿到异常本身就说明没排进那把别租账号的锁（排进去会一直等到 `timeout = 120` 被
        # pytest-timeout 打断）；下面这条只是兜底断言。
        assert not holder_task.done()
        denied = await _audit_rows("school.user.mfa_reset_denied", tenant_id)
        assert [row["metadata_json"]["reason"] for row in denied] == ["USER_NOT_FOUND"]
        assert denied[0]["resource_id"] == str(foreign_id)
        assert denied[0]["actor_id"] == str(seeded["operator_id"])
        # 成功动作一条都没落，且对方账号的 MFA/token 版本/状态一个字节都没变
        assert await _audit_rows_all("school.user.mfa_reset") == []
        assert await _user_row(foreign_tenant_id, foreign_id) == before
    except BaseException:
        # 同上：失败路径必须收掉 holder，别让它带着别租账号的锁拖进下一条用例（TD-228）。
        release.set()
        holder_task.cancel()
        with contextlib.suppress(BaseException):
            await holder_task
        raise
    else:
        release.set()
        await asyncio.wait_for(holder_task, timeout=10)


async def test_probe_denial_paths_hold_one_connection_at_a_time(
    stack: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given 管理员拿一个别租 id 来停用 / 重置 MFA；When 走探针 404；Then 全程只占一条数据库连接。

    第七轮 m3：探针原先挂在主事务里，未命中的 404 要再开一个独立事务写拒绝审计，一次注定
    失败的请求就同时占住两条连接（`app/db/session.py` 每引擎 `pool_size=10 / max_overflow=10`），
    而"猜 id 的洪水请求"打的正是这条路径。探针挪进自己的短事务后占用峰值必须是 1；把它挪回
    主事务，`opens` 立刻变 open/open/close/close、`peak` 变 2（2026-10-29 反证已实测）。
    两个模块都测：`mfa_admin` 的探针是同批同形加的，只测一边等于另一边没测（第八轮 m5）。
    """

    ledger = _SessionLedger()
    monkeypatch.setattr(account_admin, "db_session", ledger)
    monkeypatch.setattr(mfa_admin, "db_session", ledger)

    school_code = f"SCH-STATUS{uuid4().hex[:6].upper()}"
    seeded = await _seed(school_code)
    tenant_id = seeded["tenant_id"]
    operator = _principal(seeded["operator_id"], tenant_id)

    with pytest.raises(AccountAdminError) as exc:
        await deactivate_user(operator, seeded["foreign_user_id"], reason=REASON)
    assert exc.value.code == "USER_NOT_FOUND"
    assert ledger.opens == ["open", "close", "open", "close"]
    assert ledger.peak == 1

    ledger.reset()
    with pytest.raises(MfaAdminError) as mfa_exc:
        await reset_user_mfa(operator, seeded["foreign_user_id"], reason=REASON)
    assert mfa_exc.value.code == "USER_NOT_FOUND"
    assert mfa_exc.value.http_status == 404
    assert ledger.opens == ["open", "close", "open", "close"]
    assert ledger.peak == 1
    # 两边的拒绝审计各自落自己的动作名，都只有一条、且是同一个操作者试探同一个别租 id
    assert [
        (row["metadata_json"]["reason"], row["actor_id"])
        for row in await _audit_rows("school.user.status_denied", tenant_id)
    ] == [("USER_NOT_FOUND", str(seeded["operator_id"]))]
    assert [
        (row["metadata_json"]["reason"], row["actor_id"])
        for row in await _audit_rows("school.user.mfa_reset_denied", tenant_id)
    ] == [("USER_NOT_FOUND", str(seeded["operator_id"]))]


async def test_school_admin_target_can_be_deactivated_and_restored(stack: None) -> None:
    """Given 目标是另一名校方管理员账号（`school_admin`）；When 停用再启用；Then 两次都成立。

    `BINDABLE_USER_TYPES` 有两位成员，前八条用例的成功路径全是 `teacher`——校管账号才是这条
    权限日常要停的对象，却一次都没被成功停用过（第八轮 m6）。谁以后把判据写成
    `user_type == "teacher"`，这条路径会静默失效，所以单独钉一条往返。
    """

    school_code = f"SCH-STATUS{uuid4().hex[:6].upper()}"
    seeded = await _seed(school_code)
    tenant_id = seeded["tenant_id"]
    operator = _principal(seeded["operator_id"], tenant_id)
    target = seeded["second_operator_id"]

    result = await deactivate_user(operator, target, reason=REASON)
    assert result.status == "disabled"
    assert result.sessions_revoked == 0
    assert (await _user_row(tenant_id, target))["status"] == "disabled"
    assert (
        await _audit_rows("school.user.deactivated", tenant_id)
    )[0]["metadata_json"]["status_before"] == "active"

    restored = await activate_user(operator, target)
    assert restored.status == "active"
    assert (await _user_row(tenant_id, target))["status"] == "active"
    assert (
        await _audit_rows("school.user.activated", tenant_id)
    )[0]["resource_id"] == str(target)


async def test_deactivate_waits_for_a_lock_only_holder(stack: None) -> None:
    """Given 一个只取账号锁、不写任何行的并发事务；When 停用抵达；Then 停用必须排在它后面。

    这条专门回答 TD-227 的疑问：holder 不碰 `users`、也不插 `refresh_tokens`，所以它不会在
    `users` 行上留下任何锁——`FOR UPDATE` 与插入侧的 `FOR KEY SHARE` 都够不着它。停用如果仍然
    等它，等的只可能是 advisory lock 本身。摘锁实测（2026-10-29）：摘掉 `acquire_user_lock`
    后本用例转红——探针在 10s 内观察不到任何会话排在 holder 上（停用没排队就直接提交了），
    因此 advisory lock 在本链路**独立承重**，不是行锁的副产品；`app/auth/sessions.py` 的
    「同一账号的登录/刷新/撤销/改密共享事务级 advisory lock，避免撤销与轮换竞态」表述按实测保留。
    """

    school_code = f"SCH-STATUS{uuid4().hex[:6].upper()}"
    seeded = await _seed(school_code)
    tenant_id = seeded["tenant_id"]
    teacher_id = seeded["teacher_id"]
    operator = _principal(seeded["operator_id"], tenant_id)

    async def _settle() -> Any:
        try:
            return await deactivate_user(operator, teacher_id, reason=REASON)
        except AccountAdminError as exc:
            return exc

    holder_engine = make_test_engine("admin")
    probe_engine = make_test_engine("admin")
    try:
        factory = async_sessionmaker(holder_engine, expire_on_commit=False)
        async with factory() as holder, holder.begin():
            holder_pid = int(await holder.scalar(text("select pg_backend_pid()")))
            await acquire_user_lock(holder, realm="tenant", user_id=teacher_id)
            # 刻意不写任何行：这里要钉的就是"只有 advisory lock"这一种交错。
            task = asyncio.create_task(_settle())
            try:
                await _wait_until_backend_blocked(probe_engine, holder_pid)
                assert not task.done(), "停用应被纯 advisory lock 挡在 holder 之外"
            except BaseException:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                raise
        outcome = await asyncio.wait_for(task, timeout=20)
        assert isinstance(outcome, StatusChangeResult), outcome
        assert outcome.status == "disabled"
    finally:
        await holder_engine.dispose()
        await probe_engine.dispose()
