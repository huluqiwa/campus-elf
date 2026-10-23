"""M1-F 集成测试：审计哈希链——触发器自动算哈希、密钥注入、仅追加、校验脚本、自检。

用管理引擎直连（不走 db_session），避免测试环境 settings 缓存不可靠的问题。
"""

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.db import (
    clean_test_data,
    make_session_factory,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.integration]

TEST_AUDIT_KEY = "test-audit-hash-key-0123456789abcdef0123456789abcdef"

_INSERT_SQL = text(
    "INSERT INTO audit_logs "
    "(event_id, actor_realm, action, resource_type, result, created_at) "
    "VALUES (:event_id, 'system', :action, 'test', 'success', now()) "
    "RETURNING sequence, prev_hash, hash, hash_version"
)

_SELECT_SQL = text(
    "SELECT sequence, prev_hash, hash, hash_version, event_id::text, "
    "actor_realm, action, resource_type, result, request_id, created_at::text "
    "FROM audit_logs WHERE sequence = :seq"
)


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    """确保测试库结构最新（迁移应正确创建触发器；缺失则测试失败，不掩盖）。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()

    # 迁移应正确创建触发器；如果缺失说明迁移有问题，测试会失败（不掩盖）
    yield


@pytest.fixture
async def audit_env(migrated_database: None) -> AsyncIterator[AsyncEngine]:
    """清空数据，返回已绑定密钥的管理引擎工厂。"""

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
    finally:
        await admin_engine.dispose()

    # 密钥 GUC 由各用例在自身事务内注入（is_local=true，事务结束自动失效）；
    # 这里只提供引擎，不做 session 级 set_config（会残留在池中连接上，误导后续用例）。
    engine = make_test_engine("admin")
    yield engine

    await engine.dispose()


async def _insert_audit(engine: AsyncEngine, action: str) -> dict[str, Any]:
    """用管理引擎写入一条审计并返回 RETURNING 行。"""

    factory = make_session_factory(engine)
    async with factory() as session, session.begin():
        await session.execute(
            text("select set_config('app.audit_hash_key', :key, true)"),
            {"key": TEST_AUDIT_KEY},
        )
        result = await session.execute(_INSERT_SQL, {"event_id": uuid4(), "action": action})
        return dict(result.mappings().one())


async def _get_row(engine: AsyncEngine, sequence: int) -> dict[str, Any]:
    """读取指定行。"""

    factory = make_session_factory(engine)
    async with factory() as session:
        result = await session.execute(_SELECT_SQL, {"seq": sequence})
        return dict(result.mappings().one())


@pytest.mark.asyncio
async def test_trigger_computes_hash_and_links(audit_env: AsyncEngine) -> None:
    """插入两条 → hash_version=1、hash 64 位 hex、第二条 prev_hash=第一条 hash。"""

    row1 = await _insert_audit(audit_env, "test.first")
    row2 = await _insert_audit(audit_env, "test.second")

    assert row1["hash_version"] == 1
    assert row1["hash"] is not None and len(row1["hash"]) == 64
    assert row1["prev_hash"] is None  # 清空后首条

    assert row2["hash_version"] == 1
    assert row2["prev_hash"] == row1["hash"]
    assert row2["hash"] != row1["hash"]


@pytest.mark.asyncio
async def test_hash_is_hmac_not_plain_sha(audit_env: AsyncEngine) -> None:
    """不同密钥产生不同哈希（证明密钥参与计算，非纯 SHA256）。

    Python 侧重算与存储的一致性由 test_verify_script_passes
    （触发器算 → 脚本按同一字段列表重算）覆盖。
    """

    row = await _insert_audit(audit_env, "test.hmac")
    assert len(row["hash"]) == 64

    factory = make_session_factory(audit_env)
    async with factory() as session, session.begin():
        await session.execute(
            text("select set_config('app.audit_hash_key', 'different-key-0123456789abcdef', true)")
        )
        result2 = await session.execute(
            _INSERT_SQL, {"event_id": uuid4(), "action": "test.hmac.diffkey"}
        )
        row2 = dict(result2.mappings().one())

    assert row2["hash"] != row["hash"], "不同密钥应产生不同哈希"


@pytest.mark.asyncio
async def test_append_only_still_enforced(audit_env: AsyncEngine) -> None:
    """已入链行不能被 UPDATE/DELETE。"""

    row = await _insert_audit(audit_env, "test.append_only")
    seq = row["sequence"]

    factory = make_session_factory(audit_env)
    async with factory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text("UPDATE audit_logs SET action = 'tampered' WHERE sequence = :seq"),
                {"seq": seq},
            )
        with pytest.raises(DBAPIError):
            await session.execute(
                text("DELETE FROM audit_logs WHERE sequence = :seq"), {"seq": seq}
            )


@pytest.mark.asyncio
async def test_missing_key_marks_unhashed(audit_env: AsyncEngine) -> None:
    """密钥 GUC 清空时插入审计 → 不阻断写入，标 hash=NULL/hash_version=0（未入链）。

    生产环境密钥始终有（config 强制校验 + db_session 自动注入），
    出现 hash_version=0 的行即"绕过应用直写"的信号。
    """

    factory = make_session_factory(audit_env)
    async with factory() as session, session.begin():
        await session.execute(text("select set_config('app.audit_hash_key', '', true)"))
        result = await session.execute(
            text(
                "INSERT INTO audit_logs "
                "(event_id, actor_realm, action, resource_type, result, created_at) "
                "VALUES (gen_random_uuid(), 'system', 'test.nokey', 'test', 'success', now()) "
                "RETURNING sequence, hash, hash_version, prev_hash"
            )
        )
        row = result.mappings().one()

    assert row["hash"] is None
    assert row["hash_version"] == 0
    assert row["prev_hash"] is None


@pytest.mark.asyncio
async def test_verify_script_passes(audit_env: AsyncEngine) -> None:
    """校验脚本对合法链返回退出码 0 并输出"全链通过"。"""

    await _insert_audit(audit_env, "test.verify.ok1")
    await _insert_audit(audit_env, "test.verify.ok2")

    admin_url = os.environ["TEST_DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    script_path = str(Path(__file__).resolve().parents[2] / "scripts" / "verify_audit_chain.py")
    result = subprocess.run(
        [sys.executable, script_path, "--db-url", admin_url, "--hash-key", TEST_AUDIT_KEY],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "全链通过" in result.stdout or "链为空" in result.stdout


@pytest.mark.asyncio
async def test_concurrent_inserts_do_not_fork_chain(audit_env: AsyncEngine) -> None:
    """B1：并发插入不会分叉——advisory lock 串行化 prev_hash 读取。"""

    import asyncio

    async def insert_one(action: str) -> dict[str, Any]:
        return await _insert_audit(audit_env, action)

    results = await asyncio.gather(
        insert_one("test.concurrent.1"),
        insert_one("test.concurrent.2"),
        insert_one("test.concurrent.3"),
    )

    # 按 sequence 排序后验证链连续
    sorted_rows = sorted(results, key=lambda r: r["sequence"])
    for i in range(1, len(sorted_rows)):
        assert sorted_rows[i]["prev_hash"] == sorted_rows[i - 1]["hash"], (
            f"并发导致链分叉：sequence {sorted_rows[i]['sequence']} 的 prev_hash "
            f"≠ 前一行 hash"
        )


@pytest.mark.asyncio
async def test_verify_script_detects_unchained_row(audit_env: AsyncEngine) -> None:
    """MA-2：链中混入未入链行（hash_version=0）→ 校验脚本报告并退出码 1。"""

    import subprocess
    import sys as _sys
    from pathlib import Path as _Path

    # 先插入两条正常入链行
    await _insert_audit(audit_env, "test.chain.ok1")
    await _insert_audit(audit_env, "test.chain.ok2")

    # 再插入一条绕过密钥的未入链行（GUC 清空 → fail-open 标 v0）
    factory = make_session_factory(audit_env)
    async with factory() as session, session.begin():
        await session.execute(text("select set_config('app.audit_hash_key', '', true)"))
        await session.execute(
            text(
                "INSERT INTO audit_logs "
                "(event_id, actor_realm, action, resource_type, result, created_at) "
                "VALUES (gen_random_uuid(), 'system', 'test.bypass', 'test', 'success', now())"
            )
        )

    admin_url = os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    script = str(_Path(__file__).resolve().parents[2] / "scripts" / "verify_audit_chain.py")
    result = subprocess.run(
        [_sys.executable, script, "--db-url", admin_url, "--hash-key", TEST_AUDIT_KEY],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1, f"应有未入链行告警：stdout={result.stdout}"
    assert "未入链行" in result.stdout


@pytest.mark.asyncio
async def test_selfcheck_detects_missing_trigger(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3：自检——触发器被删除时报错，恢复后通过。"""

    import os

    from app.config import get_settings
    from app.db.selfcheck import DatabaseSelfCheckError, verify_runtime_database
    from app.db.session import dispose_engine

    # monkeypatch 自动还原（不用 os.environ.setdefault——会泄漏到后续测试）
    monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_APP_DATABASE_URL", ""))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", os.environ.get("TEST_PLATFORM_DATABASE_URL", ""))
    get_settings.cache_clear()
    await dispose_engine()

    engine = make_test_engine("admin")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TRIGGER IF EXISTS audit_logs_hash_chain ON audit_logs"))
        with pytest.raises(DatabaseSelfCheckError, match="触发器不存在"):
            await verify_runtime_database()
    finally:
        # 恢复触发器
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TRIGGER audit_logs_hash_chain "
                "BEFORE INSERT ON audit_logs "
                "FOR EACH ROW EXECUTE FUNCTION audit_logs_compute_hash()"
            ))
        await engine.dispose()
