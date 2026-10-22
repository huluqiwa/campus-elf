"""安全原语：字段加密、TOTP、恢复码（M1-D2）。

密钥只来自 `ENCRYPTION_KEK`（base64 编码的 32 字节），**不得入库**。
TOTP secret 使用 AES-256-GCM 加密并以 AAD 绑定 `realm:user_id`，防止密文在账号间
被移植；恢复码只存 HMAC-SHA256 摘要，摘要密钥由 KEK 经 HKDF 派生（域分离）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr

# 与设计简报一致的恢复码字母表：Base32 去掉 I/O/0/1，长度 32
RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
RECOVERY_CODE_LENGTH = 10
RECOVERY_HKDF_INFO = b"mfa-recovery-codes-v1"
_CIPHER_PREFIX = "v1"
_NONCE_BYTES = 12


class SecretEncryptionError(ValueError):
    """KEK 非法（缺失/长度错误/编码错误）。"""


class SecretDecryptError(ValueError):
    """密文损坏、AAD 不匹配或密钥不匹配。"""


def decode_kek(value: str | SecretStr) -> bytes:
    """把 base64 文本 KEK 解码为 32 字节；非法输入统一报错（不回显内容）。"""

    raw = value.get_secret_value() if isinstance(value, SecretStr) else value
    try:
        decoded = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SecretEncryptionError("ENCRYPTION_KEK 必须是 base64 编码的 32 字节密钥") from exc
    if len(decoded) != 32:
        raise SecretEncryptionError("ENCRYPTION_KEK 必须是 base64 编码的 32 字节密钥")
    return decoded


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def mfa_aad(realm: str, user_id: UUID | str) -> bytes:
    """TOTP 密文的附加认证数据：把密文绑定到特定 realm + 用户。"""

    return f"mfa:{realm}:{user_id}".encode()


def encrypt_secret(plaintext: str, *, kek: bytes, aad: bytes) -> str:
    """AES-256-GCM 加密，输出 `v1.<nonce>.<ciphertext>`（均 base64url）。"""

    if len(kek) != 32:
        raise SecretEncryptionError("KEK 必须是 32 字节")
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(kek).encrypt(nonce, plaintext.encode(), aad)
    return f"{_CIPHER_PREFIX}.{_b64url_encode(nonce)}.{_b64url_encode(ciphertext)}"


def decrypt_secret(token: str, *, kek: bytes, aad: bytes) -> str:
    """解密 `encrypt_secret` 的输出；任何失败都抛 `SecretDecryptError`（不泄露细节）。"""

    if len(kek) != 32:
        raise SecretEncryptionError("KEK 必须是 32 字节")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _CIPHER_PREFIX:
        raise SecretDecryptError("密文格式不受支持")
    try:
        nonce = _b64url_decode(parts[1])
        ciphertext = _b64url_decode(parts[2])
    except (binascii.Error, ValueError) as exc:
        raise SecretDecryptError("密文编码非法") from exc
    if len(nonce) != _NONCE_BYTES or not ciphertext:
        raise SecretDecryptError("密文格式非法")
    try:
        return AESGCM(kek).decrypt(nonce, ciphertext, aad).decode()
    except (InvalidTag, UnicodeDecodeError) as exc:
        raise SecretDecryptError("密文校验失败") from exc


def recovery_code_subkey(kek: bytes) -> bytes:
    """由 KEK 派生恢复码 HMAC 子密钥（HKDF-SHA256 域分离）。"""

    if len(kek) != 32:
        raise SecretEncryptionError("KEK 必须是 32 字节")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=RECOVERY_HKDF_INFO,
    ).derive(kek)


def normalize_recovery_code(code: str) -> str:
    """归一化用户输入：大写并去掉分隔符/空白；长度或字符非法时抛 ValueError。"""

    normalized = "".join(ch for ch in code.upper() if ch.isalnum())
    if len(normalized) != RECOVERY_CODE_LENGTH:
        raise ValueError("恢复码长度非法")
    if any(ch not in RECOVERY_ALPHABET for ch in normalized):
        raise ValueError("恢复码包含非法字符")
    return normalized


def hash_recovery_code(code: str, *, subkey: bytes, realm: str, user_id: UUID | str) -> str:
    """`HMAC-SHA256(subkey, f"{realm}:{user_id}:{normalized}")` 的十六进制摘要。"""

    normalized = normalize_recovery_code(code)
    message = f"{realm}:{user_id}:{normalized}".encode()
    return hmac.new(subkey, message, hashlib.sha256).hexdigest()


def generate_recovery_codes(
    count: int,
    *,
    subkey: bytes,
    realm: str,
    user_id: UUID | str,
) -> tuple[list[str], list[str]]:
    """生成一次性明文码（`XXXXX-XXXXX`）与对应摘要；明文只用于本次响应。"""

    plaintext_codes: list[str] = []
    hashes: list[str] = []
    for _ in range(count):
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
        display = f"{raw[:5]}-{raw[5:]}"
        plaintext_codes.append(display)
        hashes.append(hash_recovery_code(raw, subkey=subkey, realm=realm, user_id=user_id))
    return plaintext_codes, hashes
