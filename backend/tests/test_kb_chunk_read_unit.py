"""KB-06A 分块预览读侧纯函数单元测试（LIKE 转义 / 搜索词规范化 / 游标编解码）。

覆盖简报 `docs/design-briefs/M3-KB-06.md` §5 KB-06A 第 1 步的 TDD 红→绿清单；
查询、端点与权限行为在集成/业务场景层覆盖（真实 PG）。
"""

import base64
import json
from uuid import uuid4

import pytest

from app.db.repository import CursorError
from app.kb.chunk_read import (
    decode_chunk_cursor,
    encode_chunk_cursor,
    escape_like,
    normalize_q,
)

# ---------------------------------------------------------------------------
# escape_like：ILIKE 通配符转义（顺序敏感：反斜杠必须最先翻倍）
# ---------------------------------------------------------------------------


def test_escape_like_passes_plain_text_through() -> None:
    """普通文本（含中文与空格）原样保留。"""

    assert escape_like("第一章 安装指南") == "第一章 安装指南"


def test_escape_like_escapes_percent_and_underscore() -> None:
    """`%`/`_` 是 LIKE 通配符，必须转义，否则 `q=_%` 等价于全匹配。"""

    assert escape_like("100%_done") == "100\\%\\_done"


def test_escape_like_escapes_backslash_first() -> None:
    """反斜杠先翻倍再转义 %/_；顺序错了用户输入的 \\ 会变成转义符。"""

    assert escape_like("a\\b%c_") == "a\\\\b\\%c\\_"


def test_escape_like_empty_string() -> None:
    assert escape_like("") == ""


# ---------------------------------------------------------------------------
# normalize_q：搜索词规范化（strip 后空 → None，按「未提供」处理）
# ---------------------------------------------------------------------------


def test_normalize_q_strips_and_collapses_blank_to_none() -> None:
    assert normalize_q(None) is None
    assert normalize_q("") is None
    assert normalize_q("   ") is None
    assert normalize_q("  关键词  ") == "关键词"


def test_normalize_q_keeps_inner_whitespace() -> None:
    """词内空白是有效搜索语义，只去两端。"""

    assert normalize_q("表格 冻结") == "表格 冻结"


# ---------------------------------------------------------------------------
# 游标编解码：(chunk_index, id) → base64url(JSON)；非法游标必须显式拒绝
# （TD-243 口径：绝不能静默当第一页，否则翻页悄悄回到开头）
# ---------------------------------------------------------------------------


def test_chunk_cursor_roundtrip() -> None:
    chunk_id = uuid4()
    cursor = encode_chunk_cursor(42, chunk_id)
    assert decode_chunk_cursor(cursor) == (42, chunk_id)


def test_chunk_cursor_zero_index_roundtrip() -> None:
    chunk_id = uuid4()
    assert decode_chunk_cursor(encode_chunk_cursor(0, chunk_id)) == (0, chunk_id)


def test_chunk_cursor_rejects_garbage() -> None:
    with pytest.raises(CursorError):
        decode_chunk_cursor("not-a-cursor")


def test_chunk_cursor_rejects_wrong_shape() -> None:
    """合法 base64+JSON 但字段不是分块游标形状（如时间戳文档游标）→ 拒绝。"""

    payload = base64.urlsafe_b64encode(json.dumps({"t": "2026-01-01T00:00:00+00:00"}).encode()).decode()
    with pytest.raises(CursorError):
        decode_chunk_cursor(payload)


def test_chunk_cursor_rejects_missing_fields() -> None:
    payload = base64.urlsafe_b64encode(json.dumps({"i": 1}).encode()).decode()
    with pytest.raises(CursorError):
        decode_chunk_cursor(payload)


def test_chunk_cursor_rejects_bool_index() -> None:
    """bool 是 int 子类：True == 1 会蒙混等值断言（TD-231 教训），按类型拒绝。"""

    payload = base64.urlsafe_b64encode(
        json.dumps({"i": True, "d": str(uuid4())}).encode()
    ).decode()
    with pytest.raises(CursorError):
        decode_chunk_cursor(payload)


def test_chunk_cursor_rejects_string_index() -> None:
    payload = base64.urlsafe_b64encode(
        json.dumps({"i": "3", "d": str(uuid4())}).encode()
    ).decode()
    with pytest.raises(CursorError):
        decode_chunk_cursor(payload)


def test_chunk_cursor_rejects_negative_index() -> None:
    """chunk_index 有库层 CHECK >= 0；负数游标会让 keyset 恒真。"""

    with pytest.raises(CursorError):
        decode_chunk_cursor(encode_chunk_cursor(-1, uuid4()))


def test_chunk_cursor_rejects_bad_uuid() -> None:
    payload = base64.urlsafe_b64encode(json.dumps({"i": 1, "d": "nope"}).encode()).decode()
    with pytest.raises(CursorError):
        decode_chunk_cursor(payload)
