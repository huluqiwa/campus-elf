"""密码哈希与初始密码生成（bcrypt cost 12，对齐 A2/A3 决策）。

M1-D 会在此基础上加策略校验/历史/锁定；本模块只提供最小、可测试的原语。
"""

import secrets
import string

from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher

# 与 spec 决策一致：bcrypt cost 12
BCRYPT_ROUNDS = 12
_INITIAL_PASSWORD_LENGTH = 12

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


def generate_initial_password(length: int = _INITIAL_PASSWORD_LENGTH) -> str:
    """生成初始密码：默认 12 位，至少包含大写/小写/数字/符号中的 3 类。

    排除易混淆字符（0/O、1/I/l），保证可邮件抄读。
    """

    if length < 8:
        raise ValueError("initial password length must be >= 8")
    # 先保证每类各一个（共 4 类，满足"至少 3 类"）
    chars = [
        secrets.choice(_UPPER),
        secrets.choice(_LOWER),
        secrets.choice(_DIGITS),
        secrets.choice(_SYMBOLS),
    ]
    chars.extend(secrets.choice(_ALPHABET) for _ in range(length - len(chars)))
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)
