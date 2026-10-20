"""M1-B 单元测试：密码、学校编号、邮箱规范化、资质校验。"""

import re

import pytest

from app.auth.passwords import generate_initial_password, hash_password, verify_password
from app.onboarding.codes import CODE_ALPHABET, generate_school_code
from app.onboarding.service import (
    MAX_QUALIFICATION_BYTES,
    OnboardingError,
    _validate_text,
    normalize_email,
    validate_qualification,
)


class TestSchoolCode:
    """学校编号格式（用户确认：SCH- + 8 位随机）。"""

    def test_format_and_charset(self) -> None:
        """形如 SCH-XXXXXXXX，字符集排除易混淆字符。"""

        code = generate_school_code()

        assert re.fullmatch(r"SCH-[A-Z2-9]{8}", code), code
        assert not set(code[4:]) & set("0O1IL")

    def test_alphabet_excludes_confusing_characters(self) -> None:
        """字符集不含 0/O/1/I/L。"""

        assert not set(CODE_ALPHABET) & set("0O1IL")

    def test_codes_are_not_trivially_repeating(self) -> None:
        """连续生成不应重复（唯一性最终由数据库约束兜底）。"""

        codes = {generate_school_code() for _ in range(200)}
        assert len(codes) == 200


class TestPassword:
    """初始密码与 bcrypt 哈希。"""

    def test_initial_password_length_and_classes(self) -> None:
        """12 位，至少包含大写/小写/数字/符号中的 3 类。"""

        password = generate_initial_password()

        assert len(password) == 12
        classes = sum(
            [
                any(ch.isupper() for ch in password),
                any(ch.islower() for ch in password),
                any(ch.isdigit() for ch in password),
                any(not ch.isalnum() for ch in password),
            ]
        )
        assert classes >= 3

    def test_hash_roundtrip_and_wrong_password(self) -> None:
        """bcrypt 哈希可校验；错误密码返回 False。"""

        password = generate_initial_password()
        hashed = hash_password(password)

        assert hashed.startswith("$2b$12$")
        assert verify_password(password, hashed) is True
        assert verify_password(password + "x", hashed) is False
        assert verify_password(password, "not-a-hash") is False

    def test_generated_passwords_differ(self) -> None:
        """随机性：连续生成不相同。"""

        assert generate_initial_password() != generate_initial_password()

    def test_reject_short_length(self) -> None:
        """过短长度直接报错。"""

        with pytest.raises(ValueError, match="length"):
            generate_initial_password(length=4)


class TestEmailNormalization:
    """邮箱规范化与校验。"""

    def test_lowercase_and_strip(self) -> None:
        """去空白并转小写（避免大小写重复注册）。"""

        assert normalize_email("  Admin@School.EDU  ") == "admin@school.edu"

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "no-at-sign",
            "a@b",
            "x" * 250 + "@b.cn",
            "a@@school.edu",
            "a@school@edu",
            "bad\r\nBcc: evil@school.edu@school.edu",
            "a b@school.edu",
            "a@ school.edu",
            ("a" * 65) + "@school.edu",
        ],
    )
    def test_invalid(self, raw: str) -> None:
        """缺 @ / 过短 / 过长 / 双 @ / CRLF / 空白 / local 超 64 → 422。"""

        with pytest.raises(OnboardingError) as excinfo:
            normalize_email(raw)
        assert excinfo.value.http_status == 422


class TestTextFieldValidation:
    """校名/地址/校领导等文本字段：拒绝控制字符（防 CRLF 头注入与邮件构造失败）。"""

    def test_reject_control_characters(self) -> None:
        """CR/LF/TAB/NUL 一律 422。"""

        bad_name = "注入中学" + chr(13) + chr(10) + "Bcc: evil@example.com"
        for raw in (bad_name, "校" + chr(9) + "名", "校" + chr(0) + "名"):
            with pytest.raises(OnboardingError) as excinfo:
                _validate_text(raw, field="school_name")
            assert excinfo.value.code == "INVALID_FIELD"
            assert excinfo.value.http_status == 422

    def test_accept_normal_text(self) -> None:
        """正常中文/空格/标点可通过并去除首尾空白。"""

        assert _validate_text("  示范 一中  ", field="school_name") == "示范 一中"


class TestQualificationValidation:
    """资质凭证校验（MIME 白名单 + 大小上限）。"""

    def test_allowed_types_return_suffix(self) -> None:
        """PDF/PNG/JPG 允许，后缀以 MIME 为准。"""

        assert validate_qualification("application/pdf", "x.bin", b"%PDF-1.4") == ".pdf"
        assert (
            validate_qualification("image/png", "x.png", b"\x89PNG\r\n\x1a\nrest")
            == ".png"
        )
        assert validate_qualification("image/jpeg", "x.jpeg", b"\xff\xd8\xffrest") == ".jpg"

    def test_reject_unsupported_mime(self) -> None:
        """非白名单类型拒绝。"""

        with pytest.raises(OnboardingError) as excinfo:
            validate_qualification("text/html", "x.html", b"<html>")
        assert excinfo.value.code == "INVALID_FILE_TYPE"

    def test_reject_content_mismatch(self) -> None:
        """声明 PDF 但内容不是 PDF → 422（防止改名绕过类型校验）。"""

        with pytest.raises(OnboardingError) as excinfo:
            validate_qualification("application/pdf", "x.pdf", b"<html>not a pdf</html>")
        assert excinfo.value.code == "FILE_CONTENT_MISMATCH"
        assert excinfo.value.http_status == 422

    def test_reject_empty_and_oversized(self) -> None:
        """空文件 422；超过上限 413。"""

        with pytest.raises(OnboardingError) as empty:
            validate_qualification("application/pdf", "x.pdf", b"")
        assert empty.value.http_status == 422

        with pytest.raises(OnboardingError) as huge:
            validate_qualification("application/pdf", "x.pdf", b"x" * (MAX_QUALIFICATION_BYTES + 1))
        assert huge.value.http_status == 413
