"""账号启停的状态机与理由校验（TD-187e，纯函数，不碰数据库）。

服务层的两条判据与数据库状态无关：状态迁移只由「当前 status × 操作」决定，
理由只由「去空白后的长度」决定。把它们钉在单元层，集成用例才能专注在
锁、撤会话与审计这些真正需要真实基础设施的性质上。
"""

import pytest

from app.personnel.account_admin import (
    DEACTIVATE_REASON_MIN_LENGTH,
    AccountAdminError,
    ensure_transition_allowed,
    normalize_deactivate_reason,
)


@pytest.mark.parametrize(
    ("status", "operation", "expected"),
    [
        ("active", "deactivate", "disabled"),
        ("locked", "deactivate", "disabled"),
        ("disabled", "activate", "active"),
    ],
)
def test_allowed_transitions_return_the_next_status(
    status: str, operation: str, expected: str
) -> None:
    """合法迁移：返回目标状态，服务层据此写库（不把状态字符串散在 SQL 里）。"""

    assert ensure_transition_allowed(status, operation=operation) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "operation", "code", "http_status"),
    [
        ("disabled", "deactivate", "ACCOUNT_ALREADY_DISABLED", 409),
        ("active", "activate", "ACCOUNT_ALREADY_ACTIVE", 409),
        ("locked", "activate", "ACCOUNT_NOT_DISABLED", 409),
        ("deleted", "deactivate", "USER_NOT_FOUND", 404),
        ("deleted", "activate", "USER_NOT_FOUND", 404),
    ],
)
def test_rejected_transitions_carry_the_business_code(
    status: str, operation: str, code: str, http_status: int
) -> None:
    """非法迁移：错误码与状态码固定，拒绝审计按 `code` 落判据。"""

    with pytest.raises(AccountAdminError) as excinfo:
        ensure_transition_allowed(status, operation=operation)  # type: ignore[arg-type]
    assert excinfo.value.code == code
    assert excinfo.value.http_status == http_status


def test_reason_is_trimmed_and_must_reach_the_minimum_length() -> None:
    """理由去空白后计长：前后空格不能把 2 个字符凑成 10 个。"""

    assert DEACTIVATE_REASON_MIN_LENGTH == 10
    assert normalize_deactivate_reason("  教师离职，账号即时停用  ") == "教师离职，账号即时停用"

    for reason in ["", "   ", "换手机", "      短      "]:
        with pytest.raises(AccountAdminError) as excinfo:
            normalize_deactivate_reason(reason)
        assert excinfo.value.code == "DEACTIVATE_REASON_REQUIRED"
        assert excinfo.value.http_status == 422
