"""审计/日志脱敏工具（M1-B）。"""


def mask_email(email: str | None) -> str | None:
    """掩码邮箱（保留首字符与域名，用于审计可追溯但不泄露完整地址）。

    参数:
        email: 原始邮箱；None 原样返回。

    返回值:
        形如 `a***@example.com`；无 @ 时仅保留首字符。
    """

    if not email:
        return email
    if "@" not in email:
        return email[:1] + "***"
    local, _, domain = email.partition("@")
    head = local[:1] if local else ""
    return f"{head}***@{domain}"


def truncate_text(value: str | None, maximum: int) -> str | None:
    """截断自由文本（审计元数据/理由落库前）。"""

    if value is None:
        return None
    clean = value.strip()
    return clean[:maximum] if len(clean) > maximum else clean
