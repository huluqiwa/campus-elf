"""平台账号初始化与邮箱维护（无 UI 注册；M1-C 承诺的运维脚本）。

用法（在 backend 目录）::

    uv run python -m app.platform_admin --account ops --email ops@example.com
    uv run python -m app.platform_admin --account ops --email new@example.com --update-email-only

初始密码只打印一次，不写日志/审计；账号默认 `must_change_password=true`。
"""

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass

from sqlalchemy import text

from app.audit.writer import append_audit
from app.auth.passwords import generate_initial_password, hash_password
from app.db.session import db_session

DEFAULT_PLATFORM_ROLE = "platform_super_admin"
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+$")


class PlatformAccountProvisionError(ValueError):
    """平台账号初始化参数错误（消息可直接展示给运维）。"""


@dataclass(frozen=True, slots=True)
class ProvisionedPlatformAccount:
    """新建的平台账号；`initial_password` 仅用于一次性交付，不得持久化。"""

    account: str
    role_code: str
    initial_password: str


def _mask_account(account: str) -> str:
    """审计里只保留账号首字符。"""

    return f"{account[:1]}***"


def _validate_inputs(*, account: str, email: str, role_code: str) -> None:
    """校验运维输入（避免把畸形账号/邮箱写进平台账号表）。"""

    if not 3 <= len(account) <= 64 or any(ch.isspace() for ch in account):
        raise PlatformAccountProvisionError("平台账号需为 3~64 个非空白字符")
    if len(email) > 254 or not _EMAIL_PATTERN.fullmatch(email):
        raise PlatformAccountProvisionError("邮箱格式不正确")
    if not role_code.strip():
        raise PlatformAccountProvisionError("平台角色不能为空")


async def provision_platform_account(
    *,
    account: str,
    email: str,
    role_code: str = DEFAULT_PLATFORM_ROLE,
    update_email_only: bool = False,
) -> ProvisionedPlatformAccount | None:
    """创建平台账号，或仅为已有账号更新邮箱。

    返回值:
        新建账号时返回账号/角色/初始密码；仅更新邮箱时返回 None。

    异常:
        PlatformAccountProvisionError: 输入非法、账号已存在、账号不存在或角色不存在。
    """

    normalized_account = account.strip()
    normalized_email = email.strip()
    normalized_role = role_code.strip()
    _validate_inputs(account=normalized_account, email=normalized_email, role_code=normalized_role)

    async with db_session(platform=True) as session:
        existing = (
            (
                await session.execute(
                    text("select id from platform_users where account = :account"),
                    {"account": normalized_account},
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if not update_email_only:
                raise PlatformAccountProvisionError(
                    "平台账号已存在；仅更新邮箱请加 --update-email-only"
                )
            await session.execute(
                text(
                    "update platform_users set email = :email, updated_at = now() where id = :uid"
                ),
                {"email": normalized_email, "uid": existing["id"]},
            )
            # 邮箱变更后旧地址收到的重置链接必须立即失效
            await session.execute(
                text(
                    "update platform_password_reset_tokens set used_at = now() "
                    "where platform_user_id = :uid and used_at is null"
                ),
                {"uid": existing["id"]},
            )
            await append_audit(
                session,
                action="platform.user.email_updated",
                resource_type="platform_user",
                resource_id=str(existing["id"]),
                actor_realm="system",
                metadata={"account_masked": _mask_account(normalized_account)},
            )
            return None

        if update_email_only:
            raise PlatformAccountProvisionError("平台账号不存在，无法只更新邮箱")

        role_id = await session.scalar(
            text("select id from platform_roles where code = :code"),
            {"code": normalized_role},
        )
        if role_id is None:
            raise PlatformAccountProvisionError(f"平台角色不存在：{normalized_role}")

        try:
            # 初始密码必须比账号长，否则短账号之外的长账号可能永远无法避开
            initial_password = generate_initial_password(
                length=max(12, len(normalized_account) + 4), avoid=normalized_account
            )
        except ValueError as exc:
            raise PlatformAccountProvisionError("账号过长，无法生成不包含账号的初始密码") from exc

        user_id = await session.scalar(
            text(
                "insert into platform_users "
                "(account, email, password_hash, status, must_change_password, mfa_enabled) "
                "values (:account, :email, :hash, 'active', true, false) returning id"
            ),
            {
                "account": normalized_account,
                "email": normalized_email,
                "hash": hash_password(initial_password),
            },
        )
        await session.execute(
            text(
                "insert into platform_user_roles (platform_user_id, platform_role_id) "
                "values (:uid, :role_id)"
            ),
            {"uid": user_id, "role_id": role_id},
        )
        await append_audit(
            session,
            action="platform.user.provisioned",
            resource_type="platform_user",
            resource_id=str(user_id),
            actor_realm="system",
            metadata={
                "account_masked": _mask_account(normalized_account),
                "role_code": normalized_role,
            },
        )
        return ProvisionedPlatformAccount(
            account=normalized_account,
            role_code=normalized_role,
            initial_password=initial_password,
        )


def _build_parser() -> argparse.ArgumentParser:
    """构造命令行参数。"""

    parser = argparse.ArgumentParser(description="创建平台账号或更新其邮箱")
    parser.add_argument("--account", required=True, help="平台登录账号")
    parser.add_argument("--email", required=True, help="密码重置收件邮箱")
    parser.add_argument("--role", default=DEFAULT_PLATFORM_ROLE, help="平台角色 code")
    parser.add_argument(
        "--update-email-only",
        action="store_true",
        help="仅更新已有账号的邮箱（不重置密码）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：成功打印一次性初始密码，失败返回非零。"""

    args = _build_parser().parse_args(argv)
    try:
        result = asyncio.run(
            provision_platform_account(
                account=args.account,
                email=args.email,
                role_code=args.role,
                update_email_only=args.update_email_only,
            )
        )
    except PlatformAccountProvisionError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1

    if result is None:
        print(f"已更新平台账号 {args.account} 的邮箱")
    else:
        print(f"已创建平台账号 {result.account}（角色 {result.role_code}）")
        print(f"初始密码（仅显示一次）：{result.initial_password}")
        print("首次登录后必须修改密码；请通过安全渠道交付，勿写入日志/工单。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
