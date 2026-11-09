"""KB 集成测试共享环境：两所学校账号、临时对象目录与测试栈指向。

上传接口测试（`test_kb_upload.py`）与扫描 worker 测试（`test_kb_scan_worker.py`）
共用：同一套种子账号与对象 key 口径，避免两处漂移。
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.auth import revocation
from app.config import get_settings
from app.db.session import dispose_engine
from app.main import create_app
from tests.support.auth import login_tenant, seed_tenant_user
from tests.support.db import clean_test_data, database_url, make_test_engine

SCHOOL_A = "SCH-KB-A"
SCHOOL_B = "SCH-KB-B"
PASSWORD = "Kb-Upload-Pass-1"

UPLOADER = "kb-uploader"
UPLOADER_2 = "kb-uploader-2"
VIEWER_SELF = "kb-self"
VIEWER_SCHOOL = "kb-school"
UPLOAD_ONLY = "kb-upload-only"
NO_PERM = "kb-no-perm"
B_UPLOADER = "kb-b-uploader"
B_BUILDER = "kb-b-builder"
KB_APPROVER = "kb-approver"
KB_BUILDER = "kb-builder"
KB_SELF_APPROVER = "kb-self-approver"
KB_PUBLISHER = "kb-publisher"
KB_B_PUBLISHER = "kb-b-publisher"
KB_CHUNK_VIEWER = "kb-chunk-viewer"
KB_CHUNK_SELF = "kb-chunk-self"
KB_B_CHUNK_VIEWER = "kb-b-chunk-viewer"

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
MD = "# \u6821\u89c4\n\n\u6309\u65f6\u5230\u6821\u3002\n".encode()
TXT = "\u7b2c\u4e00\u6761\u3002".encode()


@dataclass(frozen=True)
class KbEnv:
    """测试环境：两所学校的 id 与本地对象目录。"""

    tenant_a: UUID
    tenant_b: UUID
    object_dir: Path


async def _seed_data(engine: AsyncEngine) -> tuple[UUID, UUID]:
    upload = ("school.kb.upload", "school.kb.view")
    tenant_a, _ = await seed_tenant_user(
        engine,
        school_code=SCHOOL_A,
        login_id=UPLOADER,
        password=PASSWORD,
        permissions=upload,
        scope_types=("self",),
    )
    await seed_tenant_user(
        engine,
        school_code=SCHOOL_A,
        login_id=UPLOADER_2,
        password=PASSWORD,
        permissions=upload,
        scope_types=("school",),
    )
    await seed_tenant_user(
        engine,
        school_code=SCHOOL_A,
        login_id=VIEWER_SELF,
        password=PASSWORD,
        permissions=("school.kb.view",),
        scope_types=("self",),
    )
    await seed_tenant_user(
        engine,
        school_code=SCHOOL_A,
        login_id=VIEWER_SCHOOL,
        password=PASSWORD,
        permissions=("school.kb.view",),
        scope_types=("school",),
    )
    await seed_tenant_user(
        engine,
        school_code=SCHOOL_A,
        login_id=UPLOAD_ONLY,
        password=PASSWORD,
        permissions=("school.kb.upload",),
        scope_types=("school",),
    )
    await seed_tenant_user(
        engine,
        school_code=SCHOOL_A,
        login_id=NO_PERM,
        password=PASSWORD,
        permissions=(),
        scope_types=("school",),
    )
    tenant_b, _ = await seed_tenant_user(
        engine,
        school_code=SCHOOL_B,
        login_id=B_UPLOADER,
        password=PASSWORD,
        permissions=upload,
        scope_types=("school",),
    )
    await seed_tenant_user(
        engine,
        school_code=SCHOOL_B,
        login_id=B_BUILDER,
        password=PASSWORD,
        permissions=("school.kb.build", "school.kb.view"),
        scope_types=("school",),
    )
    # KB-02：审批人（待办+决定+可见）、构建人、可自批账号（upload+approval 同持，验证自批禁令）
    await seed_tenant_user(
        engine,
        school_code=SCHOOL_A,
        login_id=KB_APPROVER,
        password=PASSWORD,
        permissions=("school.approval.todo", "school.kb.approval", "school.kb.view"),
        scope_types=("school",),
    )
    await seed_tenant_user(
        engine,
        school_code=SCHOOL_A,
        login_id=KB_BUILDER,
        password=PASSWORD,
        permissions=("school.kb.build", "school.kb.view"),
        scope_types=("school",),
    )
    # KB-05：发布人（发布/回滚/下线/删除）与租户 B 发布人（跨租 404 探测）
    await seed_tenant_user(
        engine,
        school_code=SCHOOL_A,
        login_id=KB_PUBLISHER,
        password=PASSWORD,
        permissions=("school.kb.publish", "school.kb.delete", "school.kb.view"),
        scope_types=("school",),
    )
    await seed_tenant_user(
        engine,
        school_code=SCHOOL_B,
        login_id=KB_B_PUBLISHER,
        password=PASSWORD,
        permissions=("school.kb.publish", "school.kb.delete", "school.kb.view"),
        scope_types=("school",),
    )
    # KB-06：分块预览（端点只挂 school.kb.view_chunks；self 范围验证范围收窄、租户 B 验证跨租 404）
    await seed_tenant_user(
        engine,
        school_code=SCHOOL_A,
        login_id=KB_CHUNK_VIEWER,
        password=PASSWORD,
        permissions=("school.kb.view_chunks",),
        scope_types=("school",),
    )
    await seed_tenant_user(
        engine,
        school_code=SCHOOL_A,
        login_id=KB_CHUNK_SELF,
        password=PASSWORD,
        permissions=("school.kb.view_chunks",),
        scope_types=("self",),
    )
    await seed_tenant_user(
        engine,
        school_code=SCHOOL_B,
        login_id=KB_B_CHUNK_VIEWER,
        password=PASSWORD,
        permissions=("school.kb.view_chunks",),
        scope_types=("school",),
    )
    await seed_tenant_user(
        engine,
        school_code=SCHOOL_A,
        login_id=KB_SELF_APPROVER,
        password=PASSWORD,
        permissions=(
            "school.kb.upload",
            "school.kb.approval",
            "school.approval.todo",
            "school.kb.view",
        ),
        scope_types=("school",),
    )
    return tenant_a, tenant_b


@asynccontextmanager
async def kb_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    overrides: dict[str, str] | None = None,
) -> AsyncIterator[KbEnv]:
    """清空业务数据、种两所学校账号，并把应用指向测试栈与临时对象目录。

    参数:
        monkeypatch: pytest 的环境变量补丁器（用例结束自动还原）。
        tmp_path: 临时目录（对象存储根）。
        overrides: 额外的环境变量覆盖（如 `CLAMAV_FAIL_CLOSED=false`）。
    """

    admin_engine = make_test_engine("admin")
    try:
        await clean_test_data(admin_engine)
        tenant_a, tenant_b = await _seed_data(admin_engine)
    finally:
        await admin_engine.dispose()

    object_dir = tmp_path / "objects"
    monkeypatch.setenv("OBJECT_STORE_BACKEND", "local")
    monkeypatch.setenv("OBJECT_STORE_DIR", str(object_dir))
    monkeypatch.setenv("DATABASE_URL", database_url("tenant"))
    monkeypatch.setenv("PLATFORM_DATABASE_URL", database_url("platform"))
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    # 每次用例独立队列命名空间：入队的扫描任务不会污染其他用例/worker 的默认队列
    monkeypatch.setenv("REDIS_PREFIX", f"kb-test-{uuid4().hex[:8]}")
    monkeypatch.setenv("DEV_SKIP_FILE_SCAN", "false")
    monkeypatch.setenv("CLAMAV_FAIL_CLOSED", "true")
    for key, value in (overrides or {}).items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    await revocation.reset_client()

    yield KbEnv(tenant_a=tenant_a, tenant_b=tenant_b, object_dir=object_dir)

    await revocation.reset_client()
    await dispose_engine()
    get_settings.cache_clear()


def make_client() -> AsyncClient:
    """构造指向 ASGI 应用的测试客户端。"""

    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


async def login(
    client: AsyncClient, login_id: str, school_code: str = SCHOOL_A
) -> dict[str, str]:
    return await login_tenant(
        client, school_code=school_code, login_id=login_id, password=PASSWORD
    )


async def rows(sql: str, **params: Any) -> list[Any]:
    """用管理员连接执行只读 SQL（绕过 RLS 的断言通道）。"""

    engine = make_test_engine("admin")
    try:
        async with engine.connect() as conn:
            return list((await conn.execute(text(sql), params)).mappings().all())
    finally:
        await engine.dispose()


async def execute(sql: str, **params: Any) -> None:
    """用管理员连接执行写语句并提交（构造/篡改测试数据）。"""

    engine = make_test_engine("admin")
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


async def upload(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    key: str,
    files: list[tuple[str, bytes, str]],
    metadata: str | None = None,
) -> Response:
    data = {"metadata": metadata} if metadata is not None else None
    return await client.post(
        "/api/v1/school/kb/documents",
        headers={**headers, "Idempotency-Key": key},
        files=[("files", (filename, content, mime)) for filename, content, mime in files],
        data=data,
    )


def object_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file()) if root.exists() else []
