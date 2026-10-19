"""SSE 事件格式化与响应头。

契约见 `docs/design.md` §5：`Content-Type: text/event-stream`、
Nginx 关缓冲（`X-Accel-Buffering: no`）、事件形如 `event: <name>\\ndata: <json>\\n\\n`。
"""

import json
from collections.abc import Mapping
from typing import Any

SSE_MEDIA_TYPE = "text/event-stream"

# 客户端/代理不应缓存或缓冲流；心跳由调用方按需发送（生产 15s）。
# Connection: keep-alive 是 HTTP/1.1 语义（HTTP/2 由协议层保活、该头无意义），
# 保留是为了让 HTTP/1.1 客户端/中间代理不主动断开长连接。
SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def format_sse(event: str, data: Mapping[str, Any]) -> str:
    """把事件名与负载格式化为 SSE 帧。

    参数:
        event: 事件名（如 meta/step/done）。
        data: JSON 可序列化的负载。

    返回值:
        str: 形如 `event: step\\ndata: {...}\\n\\n` 的文本帧。

    异常:
        TypeError: data 含不可 JSON 序列化的值。
    """

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


def format_heartbeat() -> str:
    """SSE 心跳帧（注释行，不触发客户端事件）。"""

    return ": keep-alive\n\n"
