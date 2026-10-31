"""ClamAV INSTREAM 客户端（仅上传文件扫描所需的最小协议面）。

协议：连接 clamd → 发送 `zINSTREAM\\0` → 若干「4 字节大端长度 + 数据」分块 →
4 字节零长度结束 → 读取 NUL 结尾的结果行（`stream: OK` / `stream: <签名> FOUND`）。

只处理单个文件的内存字节；错误分类为不可用 / 超时 / 协议异常，由调用方决定
fail-closed 重试或置失败，本模块不写日志、不落盘、不接触对象存储。
"""

import asyncio
import contextlib
from dataclasses import dataclass

_COMMAND = b"zINSTREAM\0"
_CHUNK_SIZE = 64 * 1024
_RESPONSE_LIMIT = 8 * 1024
_SIGNATURE_MAX_LENGTH = 200


class ClamAVError(Exception):
    """扫描失败的基类；`code` 用于落库 `scan_error_code` 与告警。"""

    code = "SCAN_FAILED"


class ClamAVUnavailableError(ClamAVError):
    """连接失败/连接被重置：扫描服务暂不可用（瞬时故障）。"""

    code = "SCAN_UNAVAILABLE"


class ClamAVTimeoutError(ClamAVError):
    """连接、发送或读取超过 `CLAMAV_TIMEOUT_SECONDS`（瞬时故障）。"""

    code = "SCAN_TIMEOUT"


class ClamAVProtocolError(ClamAVError):
    """响应不可解析（如 `... ERROR`、响应超长、连接提前关闭）。"""

    code = "SCAN_PROTOCOL_ERROR"


@dataclass(frozen=True)
class ScanResult:
    """扫描结论。

    参数:
        infected: 是否检出病毒。
        signature: 检出时的签名名（已去控制字符并截断）；干净时为 None。
    """

    infected: bool
    signature: str | None


def _sanitize_signature(raw: str) -> str:
    """签名名可能含非常规字符：只保留可打印 ASCII，并截断。"""

    cleaned = "".join(ch for ch in raw if 32 <= ord(ch) < 127).strip()
    return cleaned[:_SIGNATURE_MAX_LENGTH] or "unknown"


def _parse_response(payload: bytes) -> ScanResult:
    """解析 clamd 的结果行。"""

    text = payload.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()
    if text.endswith(" OK"):
        return ScanResult(infected=False, signature=None)
    if " FOUND" in text:
        head, _, _ = text.partition(" FOUND")
        signature = head.removeprefix("stream:").strip()
        return ScanResult(infected=True, signature=_sanitize_signature(signature))
    raise ClamAVProtocolError(f"ClamAV 响应无法识别: {text[:200]}")


async def scan_bytes(
    data: bytes,
    *,
    host: str,
    port: int,
    timeout_seconds: float,
) -> ScanResult:
    """以 INSTREAM 方式扫描一段字节。

    参数:
        data: 文件内容（调用方负责大小上限）。
        host: clamd 主机。
        port: clamd 端口。
        timeout_seconds: 整个会话（连接+发送+读取）的总超时。

    返回值:
        ScanResult（`infected` + 签名）。

    异常:
        ClamAVTimeoutError: 超时。
        ClamAVUnavailableError: 无法连接或连接被重置。
        ClamAVProtocolError: 响应不可解析。
    """

    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout(timeout_seconds):
            reader, writer = await asyncio.open_connection(host, port, limit=_RESPONSE_LIMIT)
            writer.write(_COMMAND)
            for offset in range(0, len(data), _CHUNK_SIZE):
                chunk = data[offset : offset + _CHUNK_SIZE]
                writer.write(len(chunk).to_bytes(4, "big"))
                writer.write(chunk)
            writer.write(b"\x00\x00\x00\x00")
            await writer.drain()
            payload = await reader.readuntil(b"\0")
    except TimeoutError as exc:
        raise ClamAVTimeoutError(f"ClamAV 扫描超时（{timeout_seconds:g}s）") from exc
    except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        # IncompleteReadError/LimitOverrunError 属于协议层异常，不重试即可恢复
        if isinstance(exc, (asyncio.IncompleteReadError, asyncio.LimitOverrunError)):
            raise ClamAVProtocolError("ClamAV 响应不完整或超长") from exc
        raise ClamAVUnavailableError("ClamAV 连接失败") from exc
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    return _parse_response(payload)
