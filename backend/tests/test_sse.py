"""SSE 帧格式化测试。"""

import pytest

from app.core.sse import SSE_HEADERS, format_heartbeat, format_sse


def test_format_sse_frame_shape() -> None:
    """帧格式为 `event:` + 单行 JSON `data:`，保留非 ASCII。"""

    frame = format_sse("step", {"stage": "检索", "status": "running"})

    assert frame.startswith("event: step\n")
    assert frame.endswith("\n\n")
    assert 'data: {"stage":"检索","status":"running"}' in frame


def test_format_sse_compact_and_deterministic() -> None:
    """同一负载输出稳定（无多余空格，可做快照断言）。"""

    assert format_sse("meta", {"a": 1, "b": [1, 2]}) == 'event: meta\ndata: {"a":1,"b":[1,2]}\n\n'


def test_format_sse_rejects_unserializable_payload() -> None:
    """不可序列化负载必须报错，避免半截帧。"""

    with pytest.raises(TypeError):
        format_sse("step", {"bad": object()})


def test_heartbeat_is_comment_only() -> None:
    """心跳是注释行，不触发客户端事件。"""

    assert format_heartbeat() == ": keep-alive\n\n"


def test_sse_headers_disable_buffering_and_cache() -> None:
    """SSE 响应头关闭缓冲/缓存（Nginx 亦需 proxy_buffering off）。"""

    assert SSE_HEADERS["X-Accel-Buffering"] == "no"
    assert SSE_HEADERS["Cache-Control"] == "no-cache"
