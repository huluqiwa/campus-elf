"""M1-D2 安全原语：字段加密与恢复码（单元）。"""

import base64

import pytest

from app.security.encryption import (
    RECOVERY_ALPHABET,
    SecretDecryptError,
    SecretEncryptionError,
    decode_kek,
    decrypt_secret,
    encrypt_secret,
    generate_recovery_codes,
    hash_recovery_code,
    mfa_aad,
    normalize_recovery_code,
    recovery_code_subkey,
)

KEK = bytes(range(32))
OTHER_KEK = bytes(range(1, 33))


def test_encrypt_decrypt_roundtrip() -> None:
    """密文可还原明文，且格式为 v1.<nonce>.<ct>。"""

    aad = mfa_aad("platform", "11111111-1111-1111-1111-111111111111")
    token = encrypt_secret("JBSWY3DPEHPK3PXP", kek=KEK, aad=aad)

    assert token.startswith("v1.")
    assert decrypt_secret(token, kek=KEK, aad=aad) == "JBSWY3DPEHPK3PXP"


def test_ciphertext_is_randomized() -> None:
    """同一明文每次加密的 nonce/密文不同（避免可比较）。"""

    aad = mfa_aad("tenant", "22222222-2222-2222-2222-222222222222")
    first = encrypt_secret("secret", kek=KEK, aad=aad)
    second = encrypt_secret("secret", kek=KEK, aad=aad)

    assert first != second


def test_decrypt_rejects_aad_mismatch() -> None:
    """密文绑定 realm+user_id：换账号/换 realm 无法解密。"""

    token = encrypt_secret(
        "secret",
        kek=KEK,
        aad=mfa_aad("tenant", "33333333-3333-3333-3333-333333333333"),
    )

    with pytest.raises(SecretDecryptError):
        decrypt_secret(
            token,
            kek=KEK,
            aad=mfa_aad("tenant", "44444444-4444-4444-4444-444444444444"),
        )
    with pytest.raises(SecretDecryptError):
        decrypt_secret(
            token,
            kek=KEK,
            aad=mfa_aad("platform", "33333333-3333-3333-3333-333333333333"),
        )


def test_decrypt_rejects_tampering_and_wrong_key() -> None:
    """篡改密文或使用错误 KEK 必须失败，不得返回部分明文。"""

    aad = mfa_aad("platform", "55555555-5555-5555-5555-555555555555")
    token = encrypt_secret("secret", kek=KEK, aad=aad)
    prefix, nonce, ciphertext = token.split(".")
    tampered = f"{prefix}.{nonce}.{ciphertext[:-2]}AA"

    with pytest.raises(SecretDecryptError):
        decrypt_secret(tampered, kek=KEK, aad=aad)
    with pytest.raises(SecretDecryptError):
        decrypt_secret(token, kek=OTHER_KEK, aad=aad)


@pytest.mark.parametrize("token", ["", "v1", "v2.abc.def", "v1.!!!.???", "v1.."])
def test_decrypt_rejects_malformed_tokens(token: str) -> None:
    """格式不支持/编码非法时统一报 SecretDecryptError（不泄露内部细节）。"""

    with pytest.raises(SecretDecryptError):
        decrypt_secret(token, kek=KEK, aad=b"aad")


def test_decode_kek_accepts_base64_32_bytes() -> None:
    """标准 base64 的 32 字节密钥可通过。"""

    encoded = base64.b64encode(KEK).decode()

    assert decode_kek(encoded) == KEK


@pytest.mark.parametrize(
    "value",
    [
        "not-base64!!",
        base64.b64encode(b"short").decode(),
        base64.b64encode(b"x" * 31).decode(),
        base64.b64encode(b"x" * 33).decode(),
    ],
)
def test_decode_kek_rejects_invalid_values(value: str) -> None:
    """非 base64 或长度不足 32 字节拒绝启动。"""

    with pytest.raises(SecretEncryptionError):
        decode_kek(value)


def test_recovery_codes_format_and_hashes() -> None:
    """生成 10 个 XXXXX-XXXXX、字符集合法、摘要唯一。"""

    subkey = recovery_code_subkey(KEK)
    codes, hashes = generate_recovery_codes(
        10, subkey=subkey, realm="platform", user_id="66666666-6666-6666-6666-666666666666"
    )

    assert len(codes) == 10
    assert len(set(hashes)) == 10
    for code, digest in zip(codes, hashes, strict=True):
        assert len(code) == 11 and code[5] == "-"
        assert all(ch in RECOVERY_ALPHABET for ch in code.replace("-", ""))
        assert len(digest) == 64
        assert digest == hash_recovery_code(
            code,
            subkey=subkey,
            realm="platform",
            user_id="66666666-6666-6666-6666-666666666666",
        )


def test_recovery_code_hash_is_scoped_and_normalized() -> None:
    """哈希绑定 realm+user，且忽略大小写与分隔符。"""

    subkey = recovery_code_subkey(KEK)
    user_id = "77777777-7777-7777-7777-777777777777"

    base = hash_recovery_code("ABCDE-FGHJK", subkey=subkey, realm="tenant", user_id=user_id)

    assert hash_recovery_code("abcde fghjk", subkey=subkey, realm="tenant", user_id=user_id) == base
    assert (
        hash_recovery_code("ABCDE-FGHJK", subkey=subkey, realm="platform", user_id=user_id) != base
    )
    assert (
        hash_recovery_code(
            "ABCDE-FGHJK",
            subkey=subkey,
            realm="tenant",
            user_id="88888888-8888-8888-8888-888888888888",
        )
        != base
    )


@pytest.mark.parametrize("code", ["", "ABC", "ABCDE-FGHJ0", "ABCDE-FGHJI", "ABCDE-FGHJKX"])
def test_normalize_recovery_code_rejects_invalid(code: str) -> None:
    """长度不对或含易混淆字符（0/1/I/O）均拒绝。"""

    with pytest.raises(ValueError):
        normalize_recovery_code(code)


def test_recovery_subkey_differs_from_kek_and_other_kek() -> None:
    """子密钥与 KEK/其它 KEK 派生结果不同（域分离生效）。"""

    subkey = recovery_code_subkey(KEK)

    assert subkey != KEK
    assert subkey != recovery_code_subkey(OTHER_KEK)
