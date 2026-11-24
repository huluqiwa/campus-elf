"""API 驱动 KB 种子（M4-EVAL 简报步 5 / D1-A）：移植全栈链⑤发布管线。

上传→审批→ACL(all)→确认→构建 READY→测试集→TEST_PASSED→发布。
幂等：同标题文档已 PUBLISHED 则跳过；存在但未发布 → 显式报错（重置栈后重跑）。
账号来自 `deploy/.e2e-accounts.json`（git 忽略）；口令不落日志。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from campus_elf_evals.qa_cases import KbDoc

_API = "/api/v1"


class SeedError(RuntimeError):
    """种子链路某步失败（带步骤与状态码上下文）。"""


@dataclass(frozen=True)
class SeededDoc:
    title: str
    document_id: str
    version_id: str


async def login(client: httpx.AsyncClient, base_url: str, account: dict[str, str]) -> str:
    response = await client.post(
        f"{base_url}{_API}/auth/login",
        json={
            "realm": "tenant",
            "school_code": account["school_code"],
            "login_id": account["login_id"],
            "password": account["password"],
        },
    )
    if response.status_code != 200:
        raise SeedError(f"登录失败 login_id={account['login_id']} HTTP {response.status_code}")
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TokenRefresher:
    """长跑批的访问令牌保鲜：access_token TTL=900s，超 ttl_s 自动重登。

    并发安全（asyncio.Lock）；ttl_s 默认 700s 留出单回合（≤60s）余量，
    避免回合中途令牌过期触发 401。首次 get() 才登录。
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        account: dict[str, str],
        *,
        ttl_s: float = 700.0,
    ) -> None:
        self._client = client
        self._base_url = base_url
        self._account = account
        self._ttl_s = ttl_s
        self._token: str | None = None
        self._issued = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        async with self._lock:
            if self._token is None or time.monotonic() - self._issued >= self._ttl_s:
                self._token = await login(self._client, self._base_url, self._account)
                self._issued = time.monotonic()
            return self._token


async def _call(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    method: str,
    path: str,
    *,
    step: str,
    expect: tuple[int, ...],
    json_body: dict | None = None,
) -> dict[str, Any]:
    response = await client.request(
        method, f"{base_url}{_API}{path}", headers=_auth(token), json=json_body
    )
    if response.status_code not in expect:
        raise SeedError(f"{step}: HTTP {response.status_code} {response.text[:200]}")
    return dict(response.json()) if response.text else {}


async def _find_published(
    client: httpx.AsyncClient, base_url: str, token: str, title: str
) -> SeededDoc | None:
    cursor: str | None = None
    for _page in range(10):
        params: dict[str, Any] = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        response = await client.get(
            f"{base_url}{_API}/school/kb/documents", headers=_auth(token), params=params
        )
        if response.status_code != 200:
            raise SeedError(f"文档列表: HTTP {response.status_code}")
        page = response.json()
        for item in page["items"]:
            if item["title"] == title:
                if item["status"] != "PUBLISHED":
                    raise SeedError(
                        f"文档「{title}」已存在但状态 {item['status']}（非 PUBLISHED）；"
                        "请重置 e2e 栈后重跑种子"
                    )
                detail = await _call(
                    client, base_url, token, "GET",
                    f"/school/kb/documents/{item['id']}",
                    step="文档详情", expect=(200,),
                )
                version_id = detail.get("active_version_id") or item["current_version"]["id"]
                return SeededDoc(title=title, document_id=item["id"], version_id=version_id)
        cursor = page.get("next_cursor")
        if not cursor:
            return None
    return None


async def _poll_status(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    document_id: str,
    target: str,
    *,
    timeout_s: float,
    interval_s: float,
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        detail = await _call(
            client, base_url, token, "GET",
            f"/school/kb/documents/{document_id}",
            step="构建轮询", expect=(200,),
        )
        if detail["status"] == target:
            return
        if detail["status"] in ("FAILED", "TEST_FAILED"):
            raise SeedError(f"文档 {document_id} 到达失败态 {detail['status']}")
        await asyncio.sleep(interval_s)
    raise SeedError(f"文档 {document_id} 等待 {target} 超时（{timeout_s}s）")


async def seed_document(
    client: httpx.AsyncClient,
    base_url: str,
    *,
    uploader: str,
    approver: str,
    publisher: str,
    doc: KbDoc,
    build_timeout_s: float = 120.0,
    test_timeout_s: float = 120.0,
    poll_interval_s: float = 1.0,
) -> SeededDoc:
    existing = await _find_published(client, base_url, publisher, doc.title)
    if existing is not None:
        return existing

    filename = f"eval-{uuid.uuid4().hex[:8]}.md"
    upload = await client.post(
        f"{base_url}{_API}/school/kb/documents",
        headers={**_auth(uploader), "Idempotency-Key": str(uuid.uuid4())},
        files={"files": (filename, doc.markdown.encode("utf-8"), "text/markdown")},
        data={"metadata": json.dumps([{"title": doc.title, "tags": []}], ensure_ascii=False)},
    )
    if upload.status_code != 201:
        raise SeedError(f"上传「{doc.title}」: HTTP {upload.status_code} {upload.text[:200]}")
    created = upload.json()["documents"][0]
    document_id, version_id = created["id"], created["current_version"]["id"]

    submit = await _call(
        client, base_url, uploader, "POST",
        f"/school/kb/document-versions/{version_id}/submit-approval",
        step="提交审批", expect=(201,), json_body={},
    )
    await _call(
        client, base_url, approver, "POST",
        f"/school/approvals/{submit['request_id']}/approve",
        step="审批通过", expect=(200,), json_body={},
    )
    await _call(
        client, base_url, publisher, "PUT",
        f"/school/kb/document-versions/{version_id}/audiences",
        step="ACL 配置", expect=(200,),
        json_body={"audiences": [{"audience_type": "all", "audience_id": None}]},
    )
    await _call(
        client, base_url, publisher, "POST",
        f"/school/kb/document-versions/{version_id}/audiences/confirm",
        step="ACL 确认", expect=(200,), json_body={},
    )
    await _call(
        client, base_url, publisher, "POST",
        f"/school/kb/document-versions/{version_id}/build",
        step="发起构建", expect=(201,), json_body={},
    )
    await _poll_status(
        client, base_url, publisher, document_id, "READY",
        timeout_s=build_timeout_s, interval_s=poll_interval_s,
    )

    csv = f"question\n{doc.title}的主要内容是什么？\n"
    test_set = await client.post(
        f"{base_url}{_API}/school/kb/versions/{version_id}/test-sets",
        headers=_auth(publisher),
        files={"file": (f"eval-cases-{uuid.uuid4().hex[:8]}.csv", csv.encode("utf-8"), "text/csv")},
        data={"name": f"M4-EVAL 测试集 {doc.title}"},
    )
    if test_set.status_code != 201:
        raise SeedError(f"测试集「{doc.title}」: HTTP {test_set.status_code}")
    run = await client.post(
        f"{base_url}{_API}/school/kb/versions/{version_id}/tests",
        headers=_auth(publisher),
        data={"test_set_id": test_set.json()["test_set_id"]},
    )
    if run.status_code not in (201, 202):
        raise SeedError(f"测试运行「{doc.title}」: HTTP {run.status_code}")
    await _poll_status(
        client, base_url, publisher, document_id, "TEST_PASSED",
        timeout_s=test_timeout_s, interval_s=poll_interval_s,
    )
    await _call(
        client, base_url, publisher, "POST",
        f"/school/kb/versions/{version_id}/publish",
        step="发布", expect=(200,), json_body={},
    )
    return SeededDoc(title=doc.title, document_id=document_id, version_id=version_id)


async def seed_kb(
    client: httpx.AsyncClient,
    base_url: str,
    accounts: dict[str, Any],
    docs: list[KbDoc],
    **kwargs: Any,
) -> list[SeededDoc]:
    uploader = await login(client, base_url, accounts["kb_uploader"])
    approver = await login(client, base_url, accounts["kb_approver"])
    publisher = await login(client, base_url, accounts["kb_publisher"])
    seeded: list[SeededDoc] = []
    for doc in docs:
        seeded.append(
            await seed_document(
                client, base_url,
                uploader=uploader, approver=approver, publisher=publisher,
                doc=doc, **kwargs,
            )
        )
    return seeded


async def fetch_chunk_text(
    client: httpx.AsyncClient, base_url: str, token: str, chunk_id: str
) -> str:
    detail = await _call(
        client, base_url, token, "GET",
        f"/school/kb/chunks/{chunk_id}",
        step="分块详情", expect=(200,),
    )
    return str(detail["text_markdown"])