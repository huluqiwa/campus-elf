"""M2-ORG01-03 单元测试：class 范围行的派生与对账、cursor 编解码（纯函数，不依赖数据库）。"""

import base64
import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.db.repository import CursorError, decode_cursor, encode_cursor
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


def test_decode_cursor_rejects_timestamp_without_timezone() -> None:
    """无时区的时间戳必须报错（400），不能放行。

    PG 会按**会话时区**解释无时区时间戳：手造一个"未来时刻"的游标，比较条件恒真，
    翻页会**悄悄回到第一页**——正是 `decode_cursor` 文档串里"绝不能静默回到开头"
    要杜绝的那种。真实游标一律带时区（`created_at` 取自 TIMESTAMPTZ 列），所以这里
    只挡手造的。
    """

    payload = json.dumps({"t": datetime(2999, 1, 1, 0, 0, 0).isoformat(), "id": str(uuid4())})
    cursor = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")

    with pytest.raises(CursorError):
        decode_cursor(cursor)
