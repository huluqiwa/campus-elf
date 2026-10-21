"""客户端 IP 解析单元测试：只有可信反代才能提供 X-Forwarded-For。"""

from typing import Any, cast

from fastapi import Request

from app.api.client_ip import resolve_client_ip
from app.config import Settings

PEER = ("203.0.113.9", 1234)


def _request(forwarded: str | None = None, peer: tuple[str, int] | None = PEER) -> Request:
    headers = [] if forwarded is None else [(b"x-forwarded-for", forwarded.encode())]
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "client": peer,
        "http_version": "1.1",
    }
    return Request(cast(Any, scope))


def _settings(trusted: str = "") -> Settings:
    return Settings.model_construct(trusted_proxy_ips=trusted)


def test_untrusted_peer_cannot_spoof_forwarded_for() -> None:
    """直连对端不在白名单：忽略客户端自带的 X-Forwarded-For。"""

    assert resolve_client_ip(_request("1.2.3.4"), _settings()) == "203.0.113.9"


def test_trusted_proxy_uses_first_forwarded_ip() -> None:
    """可信反代覆盖写入的首个地址即真实客户端（链式代理取最左）。"""

    request = _request("198.18.0.7, 10.0.0.2", peer=("10.0.0.1", 1234))
    assert resolve_client_ip(request, _settings("10.0.0.1, 10.0.0.2")) == "198.18.0.7"


def test_trusted_proxy_trims_whitelist_entries() -> None:
    request = _request("198.18.0.7", peer=("10.0.0.1", 1234))
    assert resolve_client_ip(request, _settings(" 10.0.0.1 , ")) == "198.18.0.7"


def test_trusted_proxy_invalid_header_falls_back_to_peer() -> None:
    """伪造/畸形头部不能让请求绕过限流，按直连地址计数。"""

    assert resolve_client_ip(_request("not-an-ip"), _settings("203.0.113.9")) == "203.0.113.9"


def test_trusted_proxy_without_header_uses_peer() -> None:
    assert resolve_client_ip(_request(None), _settings("203.0.113.9")) == "203.0.113.9"


def test_missing_client_returns_none() -> None:
    assert resolve_client_ip(_request(None, peer=None), _settings()) is None
