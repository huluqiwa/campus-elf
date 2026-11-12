"""人群 ACL 纯函数单测（M3 KB-09A）：payload 校验 / token 生成 / fail-closed 门禁判据。"""

from datetime import UTC, datetime

import pytest

from app.kb.audience import (
    ACL_GATE_ALLOW,
    AudienceValidationError,
    acl_gate,
    audience_tokens,
    parse_audience_items,
)

UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"


def _code(exc: AudienceValidationError) -> str:
    return exc.code


def test_parse_accepts_each_type_in_own_payload() -> None:
    """七种类型逐个独立载荷可解析（all 单独成档——AUDIENCE_ALL_MIXED 拒混配）。"""

    for audience_type in ("department", "role", "class", "grade", "course", "user"):
        parsed = parse_audience_items([{"audience_type": audience_type, "audience_id": UUID_A}])
        assert parsed == [(audience_type, UUID_A)]
    assert parse_audience_items([{"audience_type": "all", "audience_id": None}]) == [("all", None)]


def test_parse_grade_takes_arbitrary_string_value() -> None:
    """grade 的 audience_id 是年级字符串（非 UUID；classes.grade 为 String(16)）。"""

    parsed = parse_audience_items([{"audience_type": "grade", "audience_id": "2026"}])
    assert parsed == [("grade", "2026")]


def test_parse_rejects_unknown_type() -> None:
    with pytest.raises(AudienceValidationError) as exc:
        parse_audience_items([{"audience_type": "group", "audience_id": UUID_A}])
    assert _code(exc.value) == "AUDIENCE_TYPE_INVALID"


def test_parse_rejects_all_mixed() -> None:
    """all 与其他类型混配 → 422 AUDIENCE_ALL_MIXED（简报 §3.1 细化决策）。"""

    with pytest.raises(AudienceValidationError) as exc:
        parse_audience_items(
            [
                {"audience_type": "all", "audience_id": None},
                {"audience_type": "department", "audience_id": UUID_A},
            ]
        )
    assert _code(exc.value) == "AUDIENCE_ALL_MIXED"


def test_parse_rejects_all_with_audience_id() -> None:
    with pytest.raises(AudienceValidationError) as exc:
        parse_audience_items([{"audience_type": "all", "audience_id": UUID_A}])
    assert _code(exc.value) == "AUDIENCE_ID_FORBIDDEN"


def test_parse_rejects_missing_audience_id() -> None:
    """非 all 类型必须有非空白 audience_id。"""

    for blank in (None, "", "   "):
        with pytest.raises(AudienceValidationError) as exc:
            parse_audience_items([{"audience_type": "department", "audience_id": blank}])
        assert _code(exc.value) == "AUDIENCE_ID_REQUIRED"


def test_parse_rejects_duplicate_entry() -> None:
    with pytest.raises(AudienceValidationError) as exc:
        parse_audience_items(
            [
                {"audience_type": "department", "audience_id": UUID_A},
                {"audience_type": "department", "audience_id": UUID_A},
            ]
        )
    assert _code(exc.value) == "AUDIENCE_DUPLICATE"


def test_parse_rejects_non_list_payload() -> None:
    with pytest.raises(AudienceValidationError):
        parse_audience_items({"audience_type": "all"})  # type: ignore[arg-type]


def test_audience_tokens_mapping() -> None:
    """token 形如 `department:<uuid>`；all → `all`（spec :310）。"""

    tokens = audience_tokens([("department", UUID_A), ("grade", "2026"), ("all", None)])
    assert tokens == [f"department:{UUID_A}", "grade:2026", "all"]


def test_acl_gate_required_when_empty() -> None:
    """0 条 → ACL_REQUIRED（无论确认态；spec :307）。"""

    confirmed_at = datetime(2026, 11, 13, tzinfo=UTC)
    assert acl_gate(0, None) == "ACL_REQUIRED"
    assert acl_gate(0, confirmed_at) == "ACL_REQUIRED"


def test_acl_gate_unconfirmed() -> None:
    assert acl_gate(2, None) == "ACL_UNCONFIRMED"


def test_acl_gate_pass() -> None:
    assert acl_gate(2, datetime(2026, 11, 13, tzinfo=UTC)) == ACL_GATE_ALLOW
