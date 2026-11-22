"""反馈端点共享错误映射与 KEK 获取（M4-AI05；第 1 轮审查 Minor-9 收敛重复）。"""

from fastapi import HTTPException

from app.config import get_settings
from app.feedback.service import FeedbackError
from app.security.encryption import decode_kek

ERROR_STATUS: dict[str, int] = {
    "FEEDBACK_TARGET_NOT_FOUND": 404,
    "FEEDBACK_ALREADY_EXISTS": 409,
    "FEEDBACK_ANONYMIZED": 409,
    "FEEDBACK_SNAPSHOT_UNAVAILABLE": 503,
    "FEEDBACK_RATING_INVALID": 422,
    "FEEDBACK_TAGS_INVALID": 422,
    "FEEDBACK_COMMENT_TOO_LONG": 422,
    "FEEDBACK_REPLY_INVALID": 422,
    "FEEDBACK_CURSOR_INVALID": 400,
}


def http_from(exc: FeedbackError) -> HTTPException:
    """领域错误码 → HTTP 状态（端点层统一映射）。"""

    return HTTPException(status_code=ERROR_STATUS.get(exc.code, 400), detail=exc.code)


def get_kek() -> bytes:
    """ENCRYPTION_KEK（复用既有主密钥，AAD 域隔离——简报 D2）。"""

    return decode_kek(get_settings().encryption_kek)
