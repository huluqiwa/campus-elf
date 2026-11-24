"""qa_seed 集成测试（fake API，CI 可跑）：全链种子 / 幂等 / 失败态 / 超时。"""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest

from campus_elf_evals.qa_cases import KbDoc
from campus_elf_evals.qa_seed import SeedError, TokenRefresher, fetch_chunk_text, seed_kb

ACCOUNTS = {
    name: {"school_code": "S", "login_id": name, "password": "p"}
    for name in ("kb_uploader", "kb_approver", "kb_publisher", "kb_admin", "campus_chat")
}


class FakeKbApi:
    """有状态假 API：上传→审批→ACL→构建→测试→发布；构建后即时 READY。"""

    def __init__(self, *, stuck_build: bool = False) -> None:
        self.docs: dict[str, dict] = {}
        self.uploads = 0
        self.stuck_build = stuck_build

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "POST" and path == "/api/v1/auth/login":
            who = json.loads(request.content)["login_id"]
            return httpx.Response(200, json={"access_token": f"tok-{who}"})
        if method == "GET" and path == "/api/v1/school/kb/documents":
            items = [
                {"id": d["id"], "title": d["title"], "status": d["status"],
                 "current_version": {"id": d["version_id"]}}
                for d in self.docs.values()
            ]
            return httpx.Response(200, json={"items": items, "next_cursor": None})
        if method == "POST" and path == "/api/v1/school/kb/documents":
            self.uploads += 1
            match = re.search(rb'"title":\s*"([^"]+)"', request.content)
            title = match.group(1).decode("utf-8") if match else f"doc{self.uploads}"
            doc_id, version_id = f"d{self.uploads}", f"v{self.uploads}"
            self.docs[doc_id] = {
                "id": doc_id, "version_id": version_id, "title": title, "status": "UPLOADED",
            }
            return httpx.Response(201, json={
                "documents": [{"id": doc_id, "title": title, "current_version": {"id": version_id}}]
            })
        if method == "GET" and path.startswith("/api/v1/school/kb/documents/"):
            doc = self.docs[path.rsplit("/", 1)[1]]
            return httpx.Response(200, json={
                "id": doc["id"], "status": doc["status"], "active_version_id": doc["version_id"],
            })
        if method == "POST" and path.endswith("/submit-approval"):
            return httpx.Response(201, json={"request_id": "r1"})
        if method == "POST" and "/approvals/" in path:
            return httpx.Response(200, json={})
        if path.endswith("/audiences") or path.endswith("/audiences/confirm"):
            return httpx.Response(200, json={})
        if method == "POST" and path.endswith("/build"):
            if not self.stuck_build:
                self._set_status_by_version(path, "READY")
            return httpx.Response(201, json={})
        if method == "POST" and path.endswith("/test-sets"):
            return httpx.Response(201, json={"test_set_id": "ts1"})
        if method == "POST" and path.endswith("/tests"):
            self._set_status_by_version(path, "TEST_PASSED")
            return httpx.Response(202, json={})
        if method == "POST" and path.endswith("/publish"):
            self._set_status_by_version(path, "PUBLISHED")
            return httpx.Response(200, json={})
        if method == "GET" and path.startswith("/api/v1/school/kb/chunks/"):
            return httpx.Response(200, json={"text_markdown": "块正文"})
        return httpx.Response(404, json={"detail": f"unhandled {method} {path}"})

    def _set_status_by_version(self, path: str, status: str) -> None:
        version_id = path.split("versions/")[1].split("/")[0]
        for doc in self.docs.values():
            if doc["version_id"] == version_id:
                doc["status"] = status


@pytest.mark.asyncio
async def test_seed_kb_full_chain_and_idempotent() -> None:
    api = FakeKbApi()
    docs = [KbDoc(title="甲办法", markdown="## 甲办法\n\n正文。\n"),
            KbDoc(title="乙办法", markdown="## 乙办法\n\n正文。\n")]
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.handler)) as client:
        seeded = await seed_kb(client, "http://x", ACCOUNTS, docs, poll_interval_s=0.01)
        assert [s.title for s in seeded] == ["甲办法", "乙办法"]
        assert api.uploads == 2
        assert all(d["status"] == "PUBLISHED" for d in api.docs.values())
        # 幂等重跑：不再上传
        again = await seed_kb(client, "http://x", ACCOUNTS, docs, poll_interval_s=0.01)
        assert api.uploads == 2
        assert [s.document_id for s in again] == [s.document_id for s in seeded]


@pytest.mark.asyncio
async def test_seed_kb_existing_unpublished_raises() -> None:
    api = FakeKbApi()
    api.docs["d0"] = {"id": "d0", "version_id": "v0", "title": "甲办法", "status": "READY"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.handler)) as client:
        with pytest.raises(SeedError, match="非 PUBLISHED"):
            await seed_kb(
                client, "http://x", ACCOUNTS,
                [KbDoc(title="甲办法", markdown="## 甲办法\n\n正文。\n")],
                poll_interval_s=0.01,
            )


@pytest.mark.asyncio
async def test_seed_kb_build_timeout_raises() -> None:
    api = FakeKbApi(stuck_build=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.handler)) as client:
        with pytest.raises(SeedError, match="超时"):
            await seed_kb(
                client, "http://x", ACCOUNTS,
                [KbDoc(title="甲办法", markdown="## 甲办法\n\n正文。\n")],
                poll_interval_s=0.01, build_timeout_s=0.05,
            )


@pytest.mark.asyncio
async def test_fetch_chunk_text() -> None:
    api = FakeKbApi()
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.handler)) as client:
        text = await fetch_chunk_text(client, "http://x", "tok", "ch1")
    assert text == "块正文"


class _CountingLogin:
    def __init__(self) -> None:
        self.logins = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            self.logins += 1
            return httpx.Response(200, json={"access_token": f"tok-{self.logins}"})
        return httpx.Response(404)


@pytest.mark.asyncio
async def test_token_refresher_caches_until_ttl() -> None:
    counter = _CountingLogin()
    async with httpx.AsyncClient(transport=httpx.MockTransport(counter.handler)) as client:
        ref = TokenRefresher(client, "http://x", ACCOUNTS["campus_chat"], ttl_s=3600)
        first = await ref.get()
        second = await ref.get()
    assert first == second == "tok-1"
    assert counter.logins == 1


@pytest.mark.asyncio
async def test_token_refresher_refreshes_after_ttl() -> None:
    counter = _CountingLogin()
    async with httpx.AsyncClient(transport=httpx.MockTransport(counter.handler)) as client:
        ref = TokenRefresher(client, "http://x", ACCOUNTS["campus_chat"], ttl_s=0)
        first = await ref.get()
        second = await ref.get()
    assert first == "tok-1"
    assert second == "tok-2"
    assert counter.logins == 2


@pytest.mark.asyncio
async def test_token_refresher_concurrent_single_login() -> None:
    counter = _CountingLogin()
    async with httpx.AsyncClient(transport=httpx.MockTransport(counter.handler)) as client:
        ref = TokenRefresher(client, "http://x", ACCOUNTS["campus_chat"], ttl_s=3600)
        tokens = await asyncio.gather(*(ref.get() for _ in range(5)))
    assert set(tokens) == {"tok-1"}
    assert counter.logins == 1