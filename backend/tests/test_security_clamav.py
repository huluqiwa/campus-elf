"""ClamAV INSTREAM 客户端单测（假 clamd，不依赖真实服务）。"""

import asyncio

import pytest

from app.security.clamav import (
    ClamAVProtocolError,
    ClamAVTimeoutError,
    ClamAVUnavailableError,
    ScanResult,
    scan_bytes,
)
from tests.support.clamav import clamd, closed_port, read_instream


async def test_clean_scan_streams_chunks_and_returns_ok() -> None:
    """多分块数据按协议完整送达；`stream: OK` 判定为干净。"""

    payload = b"%PDF-1.4 test-bytes" * 20_000  # 约 360KB，跨多个 64KB 分块
    received: list[bytes] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await read_instream(reader))
        writer.write(b"stream: OK\0")
        await writer.drain()
        writer.close()

    async with clamd(handler) as port:
        result = await scan_bytes(payload, host="127.0.0.1", port=port, timeout_seconds=5)

    assert result == ScanResult(infected=False, signature=None)
    assert received == [payload]


async def test_infected_scan_returns_signature() -> None:
    """`stream: <签名> FOUND` 判定为感染并提取签名名。"""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_instream(reader)
        writer.write(b"stream: Win.Test.EICAR_HDB-1 FOUND\0")
        await writer.drain()
        writer.close()

    async with clamd(handler) as port:
        result = await scan_bytes(b"eicar", host="127.0.0.1", port=port, timeout_seconds=5)

    assert result == ScanResult(infected=True, signature="Win.Test.EICAR_HDB-1")


async def test_signature_is_sanitized_and_truncated() -> None:
    """签名名去控制字符并截断，避免把异常字节写进库/日志。"""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_instream(reader)
        writer.write(b"stream: Bad\x01\x1bName " + b"A" * 300 + b" FOUND\0")
        await writer.drain()
        writer.close()

    async with clamd(handler) as port:
        result = await scan_bytes(b"x", host="127.0.0.1", port=port, timeout_seconds=5)

    assert result.infected is True
    assert result.signature is not None
    assert "\x00" not in result.signature
    assert "\x1b" not in result.signature
    assert len(result.signature) <= 200


async def test_error_response_is_protocol_error() -> None:
    """clamd 返回 ERROR 等非 OK/FOUND 结果时归为协议异常（不误判为干净）。"""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_instream(reader)
        writer.write(b"INSTREAM size limit exceeded. ERROR\0")
        await writer.drain()
        writer.close()

    async with clamd(handler) as port:
        with pytest.raises(ClamAVProtocolError):
            await scan_bytes(b"x", host="127.0.0.1", port=port, timeout_seconds=5)


async def test_connection_closed_before_nul_is_protocol_error() -> None:
    """响应缺少 NUL 结束符（连接提前关闭）不得当作干净。"""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_instream(reader)
        writer.close()

    async with clamd(handler) as port:
        with pytest.raises(ClamAVProtocolError):
            await scan_bytes(b"x", host="127.0.0.1", port=port, timeout_seconds=5)


async def test_oversized_response_is_protocol_error() -> None:
    """响应超过读取上限（无 NUL）归为协议异常，避免无限占用内存。"""

    stop = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await read_instream(reader)
            writer.write(b"x" * 16_384)
            await writer.drain()
            await stop.wait()
        finally:
            writer.close()

    async with clamd(handler) as port:
        with pytest.raises(ClamAVProtocolError):
            await scan_bytes(b"x", host="127.0.0.1", port=port, timeout_seconds=5)
        stop.set()
        await asyncio.sleep(0)


async def test_connection_refused_is_unavailable() -> None:
    """端口无人监听 → 不可用（瞬时故障，交给 fail-closed 重试）。"""

    with pytest.raises(ClamAVUnavailableError):
        await scan_bytes(b"x", host="127.0.0.1", port=closed_port(), timeout_seconds=5)


async def test_no_response_times_out() -> None:
    """服务端不响应时按总超时终止，不得无限等待。"""

    stop = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await read_instream(reader)
            await stop.wait()
        finally:
            writer.close()

    async with clamd(handler) as port:
        with pytest.raises(ClamAVTimeoutError):
            await scan_bytes(b"x", host="127.0.0.1", port=port, timeout_seconds=0.05)
        stop.set()
        await asyncio.sleep(0)
