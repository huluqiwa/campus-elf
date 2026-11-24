"""SSE 收集器（M4-EVAL 简报步 2）：真实 HTTP 调 `/campus/chat/turns` 收答案与引用。

- `SSEParser`：增量行解析（跨 chunk 边界安全），心跳注释忽略，坏 JSON 显式报错。
- `collect_turn`：每回合独立新会话（不带 conversation_id）；传输错误重试 1 次，
  HTTP 非 200 不重试；内容失败（error 事件）不重试。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

_TURN_PATH = "/api/v1/campus/chat/turns"


class SSEParser:
    """按行喂入的 SSE 帧解析器；空行触发一帧派发。"""

    def __init__(self) -> None:
        self._event: str | None = None
        self._data: str | None = None

    def feed(self, line: str) -> list[tuple[str, dict[str, Any]]]:
        if line.startswith(":"):
            return []
        if line == "":
            if self._event is None or self._data is None:
                self._event = self._data = None
                return []
            try:
                payload = json.loads(self._data)
            except json.JSONDecodeError as exc:
                raise ValueError(f"SSE data 不是合法 JSON: {self._data!r}") from exc
            event, self._event, self._data = self._event, None, None
            return [(event, payload)]
        if line.startswith("event:"):
            self._event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            self._data = line[len("data:"):].strip()
        return []


@dataclass
class TurnResult:
    answer: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    done: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None
    transport_error: str | None = None
    first_delta_ms: int | None = None
    latency_ms: int = 0


async def collect_turn(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    token: str,
    message: str,
    timeout: float = 150.0,
) -> TurnResult:
    """发起一个回合并消费 SSE 到终态（done/error）；传输错误最多重试 1 次。"""
    last_error: str | None = None
    for _attempt in range(2):
        try:
            return await _collect_once(
                client, base_url=base_url, token=token, message=message, timeout=timeout
            )
        except httpx.TransportError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    return TurnResult(answer="", transport_error=last_error)


async def _collect_once(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    token: str,
    message: str,
    timeout: float,
) -> TurnResult:
    result = TurnResult(answer="")
    parts: list[str] = []
    parser = SSEParser()
    started = time.monotonic()
    async with client.stream(
        "POST",
        f"{base_url}{_TURN_PATH}",
        json={"message": message},
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    ) as response:
        if response.status_code != 200:
            await response.aread()
            result.transport_error = f"HTTP {response.status_code}"
            result.latency_ms = int((time.monotonic() - started) * 1000)
            return result
        async for line in response.aiter_lines():
            for event, payload in parser.feed(line):
                if event == "meta":
                    result.meta = payload
                elif event == "delta":
                    if result.first_delta_ms is None:
                        result.first_delta_ms = int((time.monotonic() - started) * 1000)
                    parts.append(str(payload.get("text", "")))
                elif event == "citation":
                    result.citations.append(payload)
                elif event == "done":
                    result.done = payload
                elif event == "error":
                    result.error = payload
    result.answer = "".join(parts)
    result.latency_ms = int((time.monotonic() - started) * 1000)
    return result