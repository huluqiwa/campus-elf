"""身份证号安全原语单元测试（M2 ORG-04）。"""

import pytest

from app.security.id_card import (
    IdCardValidationError,
    decrypt_id_card,
    encrypt_id_card,
    hmac_subkey,
    id_card_hmac,
    mask_id_card,
    validate_id_card,
)

KEK = bytes(range(32))


class TestValidate:
    def test_valid_18_digit(self) -> None:
        # 北京市 110101，19900307，男，序列 1234 → 校验码按 GB 11643 计算
        result = validate_id_card("110101199003077758")
        assert result == "110101199003077758"

    def test_trailing_x_normalized(self) -> None:
        """尾号为 X 的合法号码：归一化为大写 X。"""

        result = validate_id_card("11010119900307002x")
        assert result == "11010119900307002X"

    def test_15_digit_rejected(self) -> None:
        with pytest.raises(IdCardValidationError, match="升位"):
            validate_id_card("110101900307123")

    def test_bad_checksum_rejected(self) -> None:
        with pytest.raises(IdCardValidationError, match="校验码"):
            validate_id_card("110101199003071234")

    def test_wrong_length_rejected(self) -> None:
        with pytest.raises(IdCardValidationError):
            validate_id_card("1101011990030712")

    def test_non_digit_rejected(self) -> None:
        with pytest.raises(IdCardValidationError):
            validate_id_card("11010119900307abcX")


class TestMask:
    def test_mask_format(self) -> None:
        result = mask_id_card("110101199003077758")
        assert result == "110***********7758"
        assert len(result) == 18

    def test_mask_has_no_plaintext_leak(self) -> None:
        normalized = "110101199003077758"
        result = mask_id_card(normalized)
        assert normalized[3:14] not in result


class TestEncryptRoundTrip:
    def test_round_trip(self) -> None:
        token = encrypt_id_card(
            "110101199003077758", kek=KEK,
            tenant_id="t1", personnel_id="p1",
        )
        result = decrypt_id_card(token, kek=KEK, tenant_id="t1", personnel_id="p1")
        assert result == "110101199003077758"

    def test_nonce_random_same_input_different_ciphertext(self) -> None:
        t1 = encrypt_id_card("110101199003077758", kek=KEK, tenant_id="t1", personnel_id="p1")
        t2 = encrypt_id_card("110101199003077758", kek=KEK, tenant_id="t1", personnel_id="p1")
        assert t1 != t2  # nonce 随机 → 同号两次密文不同

    def test_aad_binds_tenant_and_personnel(self) -> None:
        token = encrypt_id_card("110101199003077758", kek=KEK, tenant_id="t1", personnel_id="p1")
        with pytest.raises(ValueError):
            decrypt_id_card(token, kek=KEK, tenant_id="t2", personnel_id="p1")
        with pytest.raises(ValueError):
            decrypt_id_card(token, kek=KEK, tenant_id="t1", personnel_id="p2")

    def test_wrong_kek_fails(self) -> None:
        token = encrypt_id_card("110101199003077758", kek=KEK, tenant_id="t1", personnel_id="p1")
        with pytest.raises(ValueError):
            decrypt_id_card(token, kek=bytes(b ^ 0xFF for b in KEK), tenant_id="t1", personnel_id="p1")


class TestHmac:
    def test_deterministic_same_input(self) -> None:
        subkey = hmac_subkey(KEK)
        h1 = id_card_hmac("110101199003077758", hmac_key=subkey, tenant_id="t1")
        h2 = id_card_hmac("110101199003077758", hmac_key=subkey, tenant_id="t1")
        assert h1 == h2

    def test_different_tenant_different_hmac(self) -> None:
        subkey = hmac_subkey(KEK)
        h1 = id_card_hmac("110101199003077758", hmac_key=subkey, tenant_id="t1")
        h2 = id_card_hmac("110101199003077758", hmac_key=subkey, tenant_id="t2")
        assert h1 != h2

    def test_hmac_does_not_leak_plaintext(self) -> None:
        subkey = hmac_subkey(KEK)
        h = id_card_hmac("110101199003077758", hmac_key=subkey, tenant_id="t1")
        assert "110101" not in h
        assert "7758" not in h
