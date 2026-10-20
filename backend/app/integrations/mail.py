"""邮件发送（开发用 MailHog，生产用 SMTP）。

安全约束：邮件正文包含初始密码，**不得**写入日志/审计/接口响应。
"""

from email.message import EmailMessage
from typing import Protocol

import aiosmtplib

from app.config import Settings


class Mailer(Protocol):
    """邮件发送端口。"""

    async def send(self, *, to: str, subject: str, body: str) -> None:
        """发送纯文本邮件。"""


class SmtpMailer:
    """基于 aiosmtplib 的 SMTP 实现（MailHog / 生产 SMTP 通用）。"""

    def __init__(self, settings: Settings) -> None:
        """绑定 SMTP 连接参数。"""

        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._sender = settings.smtp_from
        self._use_tls = settings.smtp_use_tls
        self._start_tls = settings.smtp_start_tls
        self._username = settings.smtp_username
        self._password = (
            settings.smtp_password.get_secret_value()
            if settings.smtp_password
            else None
        )

    async def send(self, *, to: str, subject: str, body: str) -> None:
        """发送邮件。

        异常:
            aiosmtplib 抛出连接或投递错误（调用方负责审计/提示，不重试本函数）。
        """

        # message 参数是位置参数（positional-only），不能作为关键字传递
        await aiosmtplib.send(
            _build_message(sender=self._sender, to=to, subject=subject, body=body),
            hostname=self._host,
            port=self._port,
            use_tls=self._use_tls,
            start_tls=self._start_tls,
            username=self._username,
            password=self._password,
            timeout=10,
        )


def _build_message(*, sender: str, to: str, subject: str, body: str) -> EmailMessage:
    """构造 RFC 5322 纯文本邮件（UTF-8，中文主题正确编码）。

    返回 EmailMessage 对象（不是字符串）：aiosmtplib 对字符串消息要求显式
    sender/recipients，否则报 "Recipients must be provided with raw messages"。
    """

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    return message


def build_mailer(settings: Settings) -> Mailer:
    """按配置构造邮件实现。"""

    return SmtpMailer(settings)


def render_tenant_approved_email(
    *, school_name: str, school_code: str, login_id: str, initial_password: str
) -> tuple[str, str]:
    """渲染"租户开通"邮件（主题, 正文）。

    正文含初始密码，仅供邮件投递使用；调用方不得记录正文。
    """

    subject = f"【校园陪伴精灵】{school_name} 已开通，请查收管理员账号"
    body = (
        f"{school_name} 的负责人：\n\n"
        "贵校在校园陪伴精灵的注册申请已审核通过，管理员账号已创建：\n\n"
        f"  学校编号：{school_code}\n"
        f"  登录账号：{login_id}\n"
        f"  初始密码：{initial_password}\n\n"
        "首次登录后系统会要求修改密码；请勿将本邮件转发他人。\n"
        "如非贵校提交，请立即联系平台运营。\n\n"
        "—— 校园陪伴精灵平台"
    )
    return subject, body
