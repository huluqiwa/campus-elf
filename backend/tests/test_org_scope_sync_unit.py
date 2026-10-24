"""M2-ORG01-03 单元测试：class 范围行的派生与对账、cursor 编解码（纯函数，不依赖数据库）。"""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.db.repository import decode_cursor, encode_cursor
from app.org.scopes_sync import scope_row_diff


def test_scope_row_diff_adds_missing_and_removes_stale() -> None:
    """对账：期望有而实际无 → 增；实际有而期望无 → 删；交集不动（幂等）。"""

    expected = {("u1", "c1"), ("u2", "c2")}
    actual = {("u1", "c1"), ("u3", "c3")}  # 一条命中、一条过期

    to_add, to_remove = scope_row_diff(expected, actual)

    assert to_add == {("u2", "c2")}
    assert to_remove == {("u3", "c3")}


def test_scope_row_diff_idempotent_when_equal() -> None:
    """期望与实际完全一致 → 不增不删（重复调用同步零副作用）。"""

    expected = {("u1", "c1")}
    to_add, to_remove = scope_row_diff(expected, set(expected))
    assert to_add == set() and to_remove == set()


def test_scope_row_diff_empty_on_both_sides() -> None:
    to_add, to_remove = scope_row_diff(set(), set())
    assert to_add == set() and to_remove == set()


# ---------------------------------------------------------------------------
# cursor 编解码（TD-070）
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)


def test_cursor_roundtrip() -> None:
    marker_id = uuid4()
    created_at, object_id = decode_cursor(encode_cursor(_NOW, marker_id))
    assert created_at == _NOW
    assert object_id == marker_id


def test_cursor_roundtrip_survives_timezone_and_micros() -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone(timedelta(hours=8)))
    created_at, _ = decode_cursor(encode_cursor(moment, uuid4()))
    assert created_at == moment


@pytest.mark.parametrize("bad", ["", "not-base64!!", "eyJ0IjogIjEifQ=="])
def test_decode_cursor_rejects_garbage(bad: str) -> None:
    """坏游标必须报错（由路由映射 400），绝不能静默回到第一页。"""

    with pytest.raises(ValueError):
        decode_cursor(bad)
