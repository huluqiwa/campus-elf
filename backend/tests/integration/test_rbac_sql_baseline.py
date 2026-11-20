"""M1-E1 性能基线：一次带鉴权请求的 SQL 次数上界（TD-100 的复查依据）。

口径（简报 §3.4/§9-5）：本步不做权限缓存，权限每请求从库读。这条测试不追求"快"，
而是把**成本钉死**——将来有人加缓存、加 join、或在依赖里再查一次库，次数就会越界报警，
那时再对着 TD-100 决定是否引入短 TTL 缓存 + 变更失效。

第二条断言是这套方案成立的前提：`require_permission` 只在已解析的 `Principal` 上判定，
**权限声明本身不多一次查询**（实测：这个受保护的空壳接口一次请求共 6 条 SQL，权限判定 0 条；
M1-E2 起其中 1 条是数据范围解析，见 `app/auth/principal.py:DATA_SCOPE_SELECT_SQL`）。
"""

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from starlette.requests import Request

from app.api.campus_chat import ChatTurnDep
from app.auth import revocation
from app.auth.principal import Principal, get_current_principal
from app.config import get_settings
from app.db.session import dispose_engine, get_engine
from app.main import create_app
from tests.support.auth import login_tenant, seed_tenant_user
from tests.support.db import (
    clean_test_data,
    database_url,
    make_test_engine,
    upgrade_test_database,
)
from tests.support.dsn import assert_test_database_dsn

pytestmark = [pytest.mark.integration]

CHAT_PERMISSION = "campus.chat.use"
CHAT_ENDPOINT = "/api/v1/campus/chat/turns"
SCHOOL_CODE = "SCH-E1SQL"
LOGIN_ID = "S-8001"
PASSWORD = "Sql-Baseline-Pass-1"

# 上界：实测本接口一次请求 6 条（认证解析 4 + 数据范围解析 1 + 空壳自身 1），留 2 条余量给业务表查询。
# M1-E2 简报口径 ⑤：加了范围解析后仍在 ≤8 的预算内；越界不代表变慢，只代表"鉴权模型变了"，
# 需要回到 TD-100 重新决策。
# M4-AI01b 真快路径实测 13 条（版本发现 2 + 人群解析 4-5 + 证据回查 1 + 工具审计 2
# + 既有鉴权面）：基线从 8 重校准；上限仍守住「单请求 SQL 预算」防退化（TD-100 口径）。
# M4-AI06/07 实测 20 条（AI-01b 基线 16 + 会话查/创 2 + 消息历史 1 + trace 落库 1）
MAX_STATEMENTS_PER_REQUEST = 24
# 下界：认证解析本身就要查用户/角色/权限（矩阵 §5-8 的"每请求从库读"）。条数掉到下界
# 以下说明鉴权改成了缓存或干脆不查（fail-open），热更新口径失效 —— 那时必须显式改这里，
# 而不是让"更快"静默通过（第三轮审查 Minor 5）。
MIN_STATEMENTS_PER_REQUEST = 4


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[None]:
    """确保测试库结构最新。"""

    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL 未设置；先运行 scripts/test-stack.sh up 并导出 TEST_*")
    assert_test_database_dsn(admin_url)
    upgrade_test_database()
    yield


@pytest.fixture
async def client(
    migrated_database: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    """指向测试栈、开关打开的应用客户端，并备好一个有对话权限的学生。"""

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
        await seed_tenant_user(
            admin_engine,
            school_code=SCHOOL_CODE,
            login_id=LOGIN_ID,
            password=PASSWORD,
            user_type="student",
            permissions=(CHAT_PERMISSION,),
        )
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


class _Counter:
    """统计两台引擎上实际执行的 SQL 条数（SELECT 与 DML 都算）。"""

    def __init__(self) -> None:
        self.count = 0
        self._engines: list[Any] = []

    def attach(self) -> None:
        for platform in (False, True):
            sync_engine = get_engine(platform=platform).sync_engine
            event.listen(sync_engine, "before_cursor_execute", self._increase)
            self._engines.append(sync_engine)

    def _increase(self, *_args: object) -> None:
        self.count += 1

    def detach(self) -> None:
        for sync_engine in self._engines:
            event.remove(sync_engine, "before_cursor_execute", self._increase)
        self._engines.clear()


@pytest.mark.asyncio
async def test_authenticated_request_stays_within_sql_budget(client: AsyncClient) -> None:
    """一次带鉴权的对话请求：SQL 次数在上界内（权限缓存引入前的成本基线）。"""

    headers = await login_tenant(
        client, school_code=SCHOOL_CODE, login_id=LOGIN_ID, password=PASSWORD
    )
    counter = _Counter()
    counter.attach()
    try:
        response = await client.post(
            CHAT_ENDPOINT, json={"message": "基线"}, headers=headers
        )
    finally:
        counter.detach()

    assert response.status_code == 200, response.text
    assert counter.count >= MIN_STATEMENTS_PER_REQUEST, (
        f"一次带鉴权请求只发了 {counter.count} 条 SQL（下限 {MIN_STATEMENTS_PER_REQUEST} 条）："
        "鉴权不再每请求从库读角色与权限，权限热更新（矩阵 §5-8）已失效，请显式复核而不是放行"
    )
    assert counter.count <= MAX_STATEMENTS_PER_REQUEST, (
        f"一次带鉴权请求发了 {counter.count} 条 SQL，超过基线 "
        f"{MAX_STATEMENTS_PER_REQUEST} 条（TD-100：先评估短 TTL 缓存 + 变更失效，再放行）"
    )


@pytest.mark.asyncio
async def test_permission_declaration_adds_no_extra_query(client: AsyncClient) -> None:
    """鉴权声明零成本：权限判定只在已解析好的 `Principal` 上做，不再查库。

    这是「每请求查权限」方案（简报 §3.5-A）能成立的前提——多一道声明不多一次查询。
    """

    headers = await login_tenant(
        client, school_code=SCHOOL_CODE, login_id=LOGIN_ID, password=PASSWORD
    )
    request = _chat_request()
    principal = await get_current_principal(request, headers["Authorization"])

    # `require_permission()` 返回 `Depends(...)`；这里取出它包住的依赖函数本体直接调用。
    # getattr 而非属性访问：mypy 不接受把 `Depends` 当类型注解，属性访问会报 attr-defined。
    inner: object = getattr(ChatTurnDep, "dependency")  # noqa: B009
    dependency = cast("Callable[[Any, Principal], Awaitable[Principal]]", inner)
    counter = _Counter()
    counter.attach()
    try:
        checked = await dependency(request, principal)
    finally:
        counter.detach()

    assert checked is principal
    assert counter.count == 0, f"权限校验发了 {counter.count} 条 SQL；应只在 Principal 上判定"


def _chat_request() -> Request:
    """构造一个仅供依赖函数使用的最小 Request。"""

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": CHAT_ENDPOINT,
            "headers": [],
            "query_string": b"",
            "state": {},
        }
    )
