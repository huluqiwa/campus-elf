"""密码哈希、策略校验、历史比对与初始密码生成（bcrypt cost 12）。

策略（spec AUTH-03）：10~128 字符、≤72 UTF-8 字节（bcrypt 输入上限）、
至少 3 类字符、不得包含登录名；历史复用比对由 `is_password_reused` 提供。
"""

import secrets
import string

from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher

# 与 spec 决策一致：bcrypt cost 12
BCRYPT_ROUNDS = 12
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128
# bcrypt 输入上限 72 字节（多字节按 UTF-8 计）：超长会抛 ValueError（500）。
# 入口按字节拦截；改哈希方案（argon2/预哈希）见 TD-107。
MAX_PASSWORD_BYTES = 72
_INITIAL_PASSWORD_LENGTH = 12
# 过短登录名（如单个字母）不做子串比较，否则几乎无法选密码
_MIN_LOGIN_ID_FOR_CONTAINMENT = 3


class PasswordPolicyError(ValueError):
    """密码不符合策略（消息可直接展示给用户）。"""


_hasher = PasswordHash((BcryptHasher(rounds=BCRYPT_ROUNDS),))

_UPPER = string.ascii_uppercase.replace("O", "").replace("I", "")
_LOWER = string.ascii_lowercase.replace("o", "").replace("l", "")
_DIGITS = "23456789"
_SYMBOLS = "!@#$%^&*"
_ALPHABET = _UPPER + _LOWER + _DIGITS + _SYMBOLS


def hash_password(password: str) -> str:
    """返回 bcrypt 哈希（cost 12）。"""

    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码与哈希是否匹配（格式非法时返回 False，不抛错）。"""

    try:
        return _hasher.verify(password, password_hash)
    except Exception:
        return False


def validate_password_strength(password: str, *, login_id: str | None = None) -> None:
    """校验密码策略。

    参数:
        password: 待校验的新密码。
        login_id: 学号/工号或平台账号；提供时禁止密码包含该值（大小写不敏感）。

    异常:
        PasswordPolicyError: 长度/字节数/字符类/包含登录名任一不满足。
    """

    if not (MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH):
        raise PasswordPolicyError(
            f"密码长度需在 {MIN_PASSWORD_LENGTH}~{MAX_PASSWORD_LENGTH} 个字符之间"
        )
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(f"密码不能超过 {MAX_PASSWORD_BYTES} 字节（一个汉字约 3 字节）")

    classes = sum(
        (
            any(ch.isupper() for ch in password),
            any(ch.islower() for ch in password),
            any(ch.isdigit() for ch in password),
            any(not ch.isalnum() and not ch.isspace() for ch in password),
        )
    )
    if classes < 3:
        raise PasswordPolicyError("密码需至少包含大写字母、小写字母、数字、符号中的 3 类")

    normalized_login = (login_id or "").strip()
    if (
        len(normalized_login) >= _MIN_LOGIN_ID_FOR_CONTAINMENT
        and normalized_login.casefold() in password.casefold()
    ):
        raise PasswordPolicyError("密码不能包含学号/工号或账号")


def is_password_reused(password: str, password_hashes: list[str]) -> bool:
    """密码是否与给定历史哈希之一匹配（历史哈希只存 bcrypt 摘要）。"""

    return any(verify_password(password, item) for item in password_hashes)


def generate_initial_password(
    length: int = _INITIAL_PASSWORD_LENGTH, *, avoid: str | None = None
) -> str:
    """生成初始密码：默认 12 位，至少包含大写/小写/数字/符号中的 3 类。

    排除易混淆字符（0/O、1/I/l），保证可邮件抄读。`avoid` 用于避免包含登录名。

    异常:
        ValueError: 长度小于 8，或 `avoid` 长到无法在给定长度内避开。
    """

    if length < 8:
        raise ValueError("initial password length must be >= 8")
    forbidden = (avoid or "").strip().casefold()
    if len(forbidden) > length:
        raise ValueError("avoid substring is longer than the generated password")

    for _ in range(32):
        # 先保证每类各一个（共 4 类，满足"至少 3 类"）
        chars = [
            secrets.choice(_UPPER),
            secrets.choice(_LOWER),
            secrets.choice(_DIGITS),
            secrets.choice(_SYMBOLS),
        ]
        chars.extend(secrets.choice(_ALPHABET) for _ in range(length - len(chars)))
        secrets.SystemRandom().shuffle(chars)
        candidate = "".join(chars)
        if not forbidden or forbidden not in candidate.casefold():
            return candidate
    raise ValueError("failed to generate an initial password avoiding the login id")
