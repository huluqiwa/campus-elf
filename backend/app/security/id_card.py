"""身份证号安全原语（M2 ORG-04）：AES-256-GCM 加密、HMAC 查重指纹、掩码、格式校验。

密钥来自独立 `ID_CARD_KEK`（base64 32 字节），**与 ENCRYPTION_KEK 分离**（D1 决策）。
明文**不落日志/审计/响应**；落库只存三列：`id_card_enc`（密文）、`id_card_hmac`（查重指纹）、
`id_card_mask`（展示掩码）。

校验码：GB 11643-1999 加权因子算法；15 位旧号不支持（提示升位）。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from base64 import urlsafe_b64decode as _b64d
from base64 import urlsafe_b64encode as _b64e

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES = 12
_CIPHER_PREFIX = "ic1"
_HKDF_INFO = b"id-card-hmac-v1"

# 18 位身份证：6 位地址 + 8 位出生日期 + 3 位顺序 + 1 位校验码
_ID_CARD_RE = re.compile(r"^\d{17}[\dXx]$")
_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_CHECK_MAP = "10X98765432"


class IdCardValidationError(ValueError):
    """身份证号格式或校验码非法。"""

    def __init__(self, message: str, *, field: str = "id_card_number") -> None:
        super().__init__(message)
        self.field = field


class IdCardCryptoError(RuntimeError):
    """ID_CARD_KEK 缺失或非法（功能不可用）。"""


class IdCardDecryptError(ValueError):
    """密文损坏或密钥不匹配（不泄露细节）。"""


def validate_id_card(number: str) -> str:
    """校验 18 位身份证号（GB 11643-1999 校验码）；返回归一化（尾 x → X）。

    异常:
        IdCardValidationError: 格式/长度/校验码不合法；15 位旧号拒绝并提示升位。
    """

    normalized = number.strip().upper()
    if len(normalized) == 15:
        raise IdCardValidationError("15 位旧身份证号不支持，请使用 18 位新号（需升位）")
    if not _ID_CARD_RE.match(normalized):
        raise IdCardValidationError("身份证号须为 18 位（17 位数字 + 1 位数字或 X）")
    checksum = _checksum(int(normalized[:17]), normalized[17])
    if checksum != normalized[17]:
        raise IdCardValidationError("身份证校验码不合法")
    return normalized


def _checksum(seventeen_digits: int, provided_check: str) -> str:
    raw = f"{seventeen_digits:017d}"
    weighted = sum(int(ch) * w for ch, w in zip(raw, _WEIGHTS, strict=True))
    return _CHECK_MAP[weighted % 11]


def mask_id_card(normalized: str) -> str:
    """掩码：前 3 后 4，中间 11 个星（输入须已通过 validate_id_card）。"""

    if len(normalized) != 18:
        raise IdCardValidationError("掩码输入须为 18 位归一化号码")
    return f"{normalized[:3]}{'*' * 11}{normalized[-4:]}"


def id_card_hmac(normalized: str, *, hmac_key: bytes, tenant_id: str) -> str:
    """查重指纹：`HMAC-SHA256(hmac_key, f"{tenant_id}:{normalized}")` 的十六进制摘要。

    HMAC 密钥与加密密钥分离（HKDF 域分离）；指纹允许同租等值查询而不泄露明文。
    """

    message = f"{tenant_id}:{normalized}".encode()
    return hmac.new(hmac_key, message, hashlib.sha256).hexdigest()


def encrypt_id_card(
    normalized: str, *, kek: bytes, tenant_id: str, personnel_id: str
) -> str:
    """AES-256-GCM 加密；AAD 绑定 `idcard:{tenant_id}:{personnel_id}`（防跨行移植）。"""

    nonce = os.urandom(_NONCE_BYTES)
    aad = f"idcard:{tenant_id}:{personnel_id}".encode()
    ciphertext = AESGCM(kek).encrypt(nonce, normalized.encode(), aad)
    nonce_b64 = _b64e(nonce).decode().rstrip("=")
    ct_b64 = _b64e(ciphertext).decode().rstrip("=")
    return f"{_CIPHER_PREFIX}.{nonce_b64}.{ct_b64}"


def decrypt_id_card(
    token: str, *, kek: bytes, tenant_id: str, personnel_id: str
) -> str:
    """解密 `encrypt_id_card` 的输出；AAD 校验失败/密钥不匹配 → IdCardDecryptError。"""

    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _CIPHER_PREFIX:
        raise IdCardDecryptError("密文格式不受支持")
    padding = "=" * (-len(parts[1]) % 4)
    nonce = _b64d(parts[1] + padding)
    padding2 = "=" * (-len(parts[2]) % 4)
    ciphertext = _b64d(parts[2] + padding2)
    aad = f"idcard:{tenant_id}:{personnel_id}".encode()
    try:
        return AESGCM(kek).decrypt(nonce, ciphertext, aad).decode()
    except (InvalidTag, UnicodeDecodeError) as exc:
        raise IdCardDecryptError("密文校验失败") from exc


def hmac_subkey(kek: bytes) -> bytes:
    """由 ID_CARD_KEK 派生 HMAC 子密钥（HKDF-SHA256 域分离）。"""

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO
    ).derive(kek)
