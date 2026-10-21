"""客户端 IP 解析（反向代理后仍能按真实来源限流/审计）。

只有直连对端在 `TRUSTED_PROXY_IPS` 白名单内时才读取 `X-Forwarded-For`，
且要求反代**覆盖**该头（Nginx `proxy_set_header X-Forwarded-For $remote_addr`），
否则客户端可伪造来源绕过限流。
"""

import ipaddress

from fastapi import Request

from app.config import Settings, get_settings


def resolve_client_ip(request: Request, settings: Settings | None = None) -> str | None:
    """返回可信客户端 IP；无可信来源时返回直连地址。"""

    direct = request.client.host if request.client else None
    if direct is None:
        return None

    current = settings or get_settings()
    trusted = {item.strip() for item in current.trusted_proxy_ips.split(",") if item.strip()}
    if direct not in trusted:
        return direct

    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return direct
    candidate = forwarded.split(",", 1)[0].strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return direct
