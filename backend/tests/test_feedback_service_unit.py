"""M4-AI05 单元测试：反馈服务纯函数（快照加密/PII 脱敏/载荷校验）。

覆盖：快照加密往返 + AAD 域绑定（跨 AAD 不可解）；comment PII 脱敏；
rating/tags/comment 校验；错误码。
"""

from uuid import uuid4

import pytest

from app.feedback.service import (
    COMMENT_MAX_CHARS,
    FeedbackError,
    decrypt_snapshot,
    encrypt_snapshot,
    feedback_aad,
    redact_comment,
)
from app.security.encryption import SecretDecryptError

_TENANT = uuid4()
_FEEDBACK = uuid4()
_KEK = bytes(range(32))


def test_feedback_aad_binds_tenant_and_feedback() -> None:
    assert feedback_aad(_TENANT, _FEEDBACK) == f"ai_feedback:{_TENANT}:{_FEEDBACK}".encode()


def test_snapshot_encrypt_decrypt_roundtrip() -> None:
    payload = {"question": "差旅报销怎么办理？", "answer": "见 [1]。", "citations": [{"index": 1}]}
    token = encrypt_snapshot(payload, kek=_KEK, tenant_id=_TENANT, feedback_id=_FEEDBACK)
    assert token != str(payload)  # 密文非明文
    assert decrypt_snapshot(token, kek=_KEK, tenant_id=_TENANT, feedback_id=_FEEDBACK) == payload


def test_snapshot_wrong_aad_cannot_decrypt() -> None:
    """AAD 域绑定（D2）：换 tenant/feedback 任一维 → 解密失败（跨域密文不可用）。"""

    payload = {"question": "q", "answer": "a", "citations": []}
    token = encrypt_snapshot(payload, kek=_KEK, tenant_id=_TENANT, feedback_id=_FEEDBACK)
    with pytest.raises(SecretDecryptError):
        decrypt_snapshot(token, kek=_KEK, tenant_id=uuid4(), feedback_id=_FEEDBACK)
    with pytest.raises(SecretDecryptError):
        decrypt_snapshot(token, kek=_KEK, tenant_id=_TENANT, feedback_id=uuid4())


def test_redact_comment_removes_idcard() -> None:
    """guards 输出红线仅身份证模式（手机号属自身 PII 放行——AI-09 卡 4-A 口径）。"""
    redacted = redact_comment("回答不错，我的身份证 11010119900307777X 请记录")
    assert "11010119900307777X" not in redacted
    assert "回答不错" in redacted  # 干净内容保留


def test_redact_comment_clean_text_unchanged() -> None:
    assert redact_comment("回答准确，引用清楚。") == "回答准确，引用清楚。"


def test_validate_rating_invalid() -> None:
    from app.feedback.service import _validate_payload

    with pytest.raises(FeedbackError) as exc:
        _validate_payload("great", [], None)
    assert exc.value.code == "FEEDBACK_RATING_INVALID"


def test_validate_tags_unknown_rejected() -> None:
    from app.feedback.service import _validate_payload

    with pytest.raises(FeedbackError) as exc:
        _validate_payload("helpful", ["inaccurate", "自定义标签"], None)
    assert exc.value.code == "FEEDBACK_TAGS_INVALID"


def test_validate_comment_too_long() -> None:
    from app.feedback.service import _validate_payload

    with pytest.raises(FeedbackError) as exc:
        _validate_payload("helpful", [], "长" * (COMMENT_MAX_CHARS + 1))
    assert exc.value.code == "FEEDBACK_COMMENT_TOO_LONG"


def test_validate_comment_pii_redacted_in_payload() -> None:
    """载荷校验同步完成 PII 脱敏：返回的 comment 已清除身份证号。"""

    from app.feedback.service import _validate_payload

    cleaned = _validate_payload("unhelpful", ["irrelevant"], "答非所问，证件号 11010119900307777X")
    assert "11010119900307777X" not in cleaned

def test_redact_comment_preserves_refs_and_non_pii() -> None:
    """专用脱敏（第 1 轮审查 Minor-2）：引用标记/普通内容保留，仅身份证/密钥/内网清除。"""

    text_in = "第[1]条引用对不上，密钥 sk-abcdef1234567890abcdef1234 泄露在回答里。"
    redacted = redact_comment(text_in)
    assert "[1]" in redacted  # REF_MARKER 保留（不再误改写用户原文）
    assert "sk-abcdef" not in redacted
    assert "泄露在回答里" in redacted


def test_redact_comment_preserves_xss_like_text() -> None:
    """XSS 形态文本不脱敏不拦截（输出红线不适用用户输入通道）。"""

    text_in = "回答里出现了 <script>alert(1)</script> 这段代码"
    assert redact_comment(text_in) == text_in
