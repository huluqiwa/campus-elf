"""MinerU 4.0.12 V1 `/v1/file_parse` 适配器（M3 KB-03A）。

契约（2026-11-07 对 dev 容器 openapi.json 实测，TD-046 同源）：
- `POST /v1/file_parse?wait_timeout=<s>&response_format=json`，
  请求体 `{"tier": "<tier>", "output_formats": ["markdown"],
  "files": [{"source": {"type": "inline", "name": "<filename>", "data": "<base64>"}}]}`；
  **tier/ocr_mode/output_formats 在请求体**（query 传参会 400 `invalid_request`）；
- 响应 `{status: completed|partial|failed|..., tier, files: [{name, status,
  content: {markdown}, error}]}`；
- 同步等价端点：`wait_timeout` 内阻塞等待结果（HTTP 层一次往返，无需轮询 job）；
- `/v1/health` 只声明能力不能作为可用性证据（TD-046）——本适配器不做健康预检，
  直接 file_parse，失败即 `ParserError`（降级链接管）。

只报错不重试：同档重试不在适配器层（编排层的降级链负责跨档）。
"""

import base64
import logging
from typing import Any

import httpx

from app.kb.parser.ports import ParsedDocument, ParserError

logger = logging.getLogger(__name__)


class MinerUParser:
    """单档 MinerU 适配器（一个实例对应一个 tier）。"""

    def __init__(
        self,
        *,
        tier: str,
        base_url: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._tier = tier
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._client = client

    async def parse(self, *, data: bytes, filename: str) -> ParsedDocument:
        """把字节流交给 MinerU 解析并取回 markdown。"""

        if not data:
            raise ParserError("MINERU_EMPTY_INPUT", "解析输入为空")
        payload = {
            "tier": self._tier,
            "output_formats": ["markdown"],
            "files": [
                {
                    "source": {
                        "type": "inline",
                        "name": filename,
                        "data": base64.b64encode(data).decode("ascii"),
                    }
                }
            ],
        }
        try:
            response = await self._client_or_default().post(
                f"{self._base_url}/v1/file_parse",
                params={"wait_timeout": int(self._timeout_seconds), "response_format": "json"},
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise ParserError(
                "MINERU_HTTP_ERROR",
                f"MinerU（{self._tier}）网络错误：{type(exc).__name__}",
            ) from exc
        if response.status_code != 200:
            raise ParserError(
                "MINERU_HTTP_ERROR",
                f"MinerU（{self._tier}）返回 HTTP {response.status_code}",
            )
        body = _safe_json(response)
        task_status = str(body.get("status", ""))
        if task_status not in ("completed", "partial"):
            raise ParserError(
                "MINERU_PARSE_FAILED",
                f"MinerU（{self._tier}）任务状态 {task_status or '未知'}",
            )
        files = body.get("files") or []
        entry = files[0] if files else None
        file_status = str((entry or {}).get("status", ""))
        if file_status != "completed":
            detail = (entry or {}).get("error") or {}
            message = str(detail.get("message", "")) if isinstance(detail, dict) else ""
            raise ParserError(
                "MINERU_PARSE_FAILED",
                f"MinerU（{self._tier}）文件状态 {file_status or '未知'}{('：' + message) if message else ''}",
            )
        content = (entry or {}).get("content") or {}
        markdown = content.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ParserError("MINERU_EMPTY_OUTPUT", f"MinerU（{self._tier}）返回空 markdown")
        return ParsedDocument(markdown=markdown)

    def _client_or_default(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        # 长超时覆盖标准档冷启动（M0-E：首次含模型下载 248s；热态秒级）。
        return httpx.AsyncClient(timeout=self._timeout_seconds)


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise ParserError("MINERU_HTTP_ERROR", "MinerU 返回非 JSON 响应") from exc
    if not isinstance(body, dict):
        raise ParserError("MINERU_HTTP_ERROR", "MinerU 响应结构异常")
    return body
