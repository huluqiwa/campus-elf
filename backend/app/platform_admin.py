"""平台账号初始化、邮箱维护与 MFA 重置（无 UI 注册；M1-C/M1-D2 运维脚本）。

用法（在 backend 目录）::

    uv run python -m app.platform_admin --account ops --email ops@example.com
    uv run python -m app.platform_admin --account ops --email new@example.com --update-email-only
    uv run python -m app.platform_admin --account ops --reset-mfa
    uv run python -m app.platform_admin --account ops --email new@example.com --reset-mfa

初始密码只打印一次，不写日志/审计；账号默认 `must_change_password=true`。
`--reset-mfa` 清空 TOTP 绑定与恢复码、解除锁定、轮换全部会话并自增 token_version，
用于运营账号丢失 Authenticator 后的恢复；不会重置密码、不会打印任何密钥。
"""

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from app.audit.writer import append_audit
from app.auth import revocation
from app.auth import sessions as session_store
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


@dataclass(frozen=True, slots=True)
class ResetPlatformMfaResult:
    """MFA 重置结果（不包含任何密钥/验证码）。"""

    account: str
    sessions_revoked: int
    email_updated: bool


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


async def reset_platform_mfa(*, account: str, email: str | None = None) -> ResetPlatformMfaResult:
    """重置平台账号 MFA：清空绑定/恢复码、解除锁定、轮换全部会话。

    用于运营人员丢失 Authenticator 且恢复码也不可用的情况；密码不变。
    `email` 非空时同时更新密码重置邮箱（并作废旧重置令牌）。

    异常:
        PlatformAccountProvisionError: 账号不存在或邮箱非法。
    """

    normalized_account = account.strip()
    if not normalized_account:
        raise PlatformAccountProvisionError("平台账号不能为空")
    normalized_email = email.strip() if email is not None else None
    if normalized_email is not None and (
        len(normalized_email) > 254 or not _EMAIL_PATTERN.fullmatch(normalized_email)
    ):
        raise PlatformAccountProvisionError("邮箱格式不正确")

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
        if existing is None:
            raise PlatformAccountProvisionError("平台账号不存在")

        user_id = existing["id"]
        # 与登录/刷新/改密/MFA 变更共用同一把账号级锁，避免破窗重置与在线写操作交错
        await session_store.acquire_user_lock(session, realm="platform", user_id=user_id)
        families: list[UUID] = list(
            (
                await session.execute(
                    text(
                        "select distinct family_id from platform_refresh_tokens "
                        "where platform_user_id = :uid and revoked_at is null"
                    ),
                    {"uid": user_id},
                )
            )
            .scalars()
            .all()
        )
        await session.execute(
            text(
                "update platform_refresh_tokens set revoked_at = now(), "
                "revoked_reason = 'admin_revoke' "
                "where platform_user_id = :uid and revoked_at is null"
            ),
            {"uid": user_id},
        )
        await session.execute(
            text("delete from platform_mfa_recovery_codes where platform_user_id = :uid"),
            {"uid": user_id},
        )
        await session.execute(
            text(
                "update platform_users set mfa_enabled = false, mfa_secret_enc = null, "
                "mfa_last_used_step = null, failed_login_attempts = 0, "
                "failed_login_window_started_at = null, locked_until = null, "
                "token_version = token_version + 1, updated_at = now() "
                "where id = :uid"
            ),
            {"uid": user_id},
        )
        if normalized_email is not None:
            await session.execute(
                text("update platform_users set email = :email where id = :uid"),
                {"email": normalized_email, "uid": user_id},
            )
            await session.execute(
                text(
                    "update platform_password_reset_tokens set used_at = now() "
                    "where platform_user_id = :uid and used_at is null"
                ),
                {"uid": user_id},
            )
        await append_audit(
            session,
            action="platform.user.mfa_reset",
            resource_type="platform_user",
            resource_id=str(user_id),
            actor_realm="system",
            metadata={
                "account_masked": _mask_account(normalized_account),
                "sessions_revoked": len(families),
                "email_updated": normalized_email is not None,
            },
        )

    # 会话标记删除是"立即失效访问令牌"的必要步骤；失败只影响 ≤15 分钟窗口
    await revocation.delete_sessions(families, realm="platform")
    await revocation.delete_mfa_pending_secret(realm="platform", user_id=user_id)
    return ResetPlatformMfaResult(
        account=normalized_account,
        sessions_revoked=len(families),
        email_updated=normalized_email is not None,
    )


def _build_parser() -> argparse.ArgumentParser:
    """构造命令行参数。"""

    parser = argparse.ArgumentParser(description="创建平台账号、更新邮箱或重置 MFA")
    parser.add_argument("--account", required=True, help="平台登录账号")
    parser.add_argument("--email", help="密码重置收件邮箱（重置 MFA 时可省略）")
    parser.add_argument("--role", default=DEFAULT_PLATFORM_ROLE, help="平台角色 code")
    parser.add_argument(
        "--update-email-only",
        action="store_true",
        help="仅更新已有账号的邮箱（不重置密码）",
    )
    parser.add_argument(
        "--reset-mfa",
        action="store_true",
        help="重置 MFA（清空绑定/恢复码、解除锁定、撤销全部会话；不重置密码）",
    )
    return parser


async def run(argv: list[str] | None = None) -> int:
    """CLI 主体（可 await；集成测试在既有事件循环内直接调用）。"""

    args = _build_parser().parse_args(argv)
    if args.reset_mfa and args.update_email_only:
        print("失败：--reset-mfa 与 --update-email-only 不能同时使用", file=sys.stderr)
        return 1
    if not args.reset_mfa and not args.email:
        print("失败：创建账号或仅更新邮箱时必须提供 --email", file=sys.stderr)
        return 1

    try:
        if args.reset_mfa:
            reset = await reset_platform_mfa(account=args.account, email=args.email)
            print(
                f"已重置平台账号 {reset.account} 的 MFA"
                f"（撤销 {reset.sessions_revoked} 个会话"
                f"{'，邮箱已更新' if reset.email_updated else ''}）"
            )
            print("请重新登录并按强制策略绑定新的 Authenticator。")
            return 0
        result = await provision_platform_account(
            account=args.account,
            email=args.email or "",
            role_code=args.role,
            update_email_only=args.update_email_only,
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


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：成功打印结果，失败返回非零；不打印任何密钥/验证码。"""

    return asyncio.run(run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
