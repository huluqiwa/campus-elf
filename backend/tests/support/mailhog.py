"""MailHog 辅助（集成/业务场景断言邮件投递）。"""

import email
import re
import time
from email.message import Message
from typing import Any

import httpx


def wait_for_message(api_base: str, *, to: str, timeout: float = 10.0) -> dict[str, Any]:
    """轮询 MailHog 直到收到收件人的最新邮件。

    参数:
        api_base: MailHog API 地址（如 http://127.0.0.1:58025）。
        to: 收件人地址。
        timeout: 最长等待秒数。

    返回值:
        MailHog 消息对象（含 Content.Headers.Subject / Content.Body）。

    异常:
        TimeoutError: 超时未收到邮件。
    """

    deadline = time.monotonic() + timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(
                f"{api_base.rstrip('/')}/api/v2/search",
                params={"kind": "to", "query": to},
                timeout=5,
            )
            response.raise_for_status()
            items = response.json().get("items") or []
            if items:
                # MailHog 返回按时间倒序，取第一条即最新
                newest: dict[str, Any] = items[0]
                return newest
        except Exception as exc:  # 服务刚启动时可能连接失败，继续重试
            last_error = str(exc)
        time.sleep(0.2)
    raise TimeoutError(f"等待邮件超时（to={to}, last_error={last_error}）")


def _content(message: dict[str, Any]) -> dict[str, Any]:
    """返回消息 Content 段（MailHog JSON 结构）。"""

    content = message.get("Content")
    return content if isinstance(content, dict) else {}


def _parse_raw(message: dict[str, Any]) -> Message | None:
    """用 MailHog 的 Raw.Data（完整 MIME）解析邮件；不可用时返回 None。"""

    raw = message.get("Raw")
    if not isinstance(raw, dict):
        return None
    data = raw.get("Data")
    if not isinstance(data, str) or not data:
        return None
    return email.message_from_string(data)


def message_subject(message: dict[str, Any]) -> str:
    """提取邮件主题（优先解析原始 MIME，回退到 Content.Headers）。"""

    parsed = _parse_raw(message)
    if parsed is not None:
        return str(parsed.get("Subject", ""))
    headers = _content(message).get("Headers") or {}
    if not isinstance(headers, dict):
        return ""
    subject = headers.get("Subject", [""])
    return subject[0] if isinstance(subject, list) and subject else str(subject)


def message_body(message: dict[str, Any]) -> str:
    """提取邮件正文（自动解码 base64/quoted-printable 与字符集）。"""

    parsed = _parse_raw(message)
    if parsed is not None:
        payload = parsed.get_payload(decode=True)
        if isinstance(payload, bytes):
            return payload.decode(parsed.get_content_charset() or "utf-8", errors="replace")
        if isinstance(payload, str):
            return payload
    return str(_content(message).get("Body", ""))


def extract_initial_password(body: str) -> str:
    """从邮件正文提取初始密码（仅测试使用）。

    异常:
        AssertionError: 正文中没有初始密码。
    """

    match = re.search(r"初始密码：(\S+)", body)
    assert match is not None, f"邮件正文缺少初始密码：{body[:200]}"
    return match.group(1)
