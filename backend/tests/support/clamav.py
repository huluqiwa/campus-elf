"""测试用假 clamd（asyncio 简单服务器）与端口工具。

ClamAV 客户端单测与 KB 扫描 worker 集成测试共用：前者验证协议细节，
后者让扫描任务真的走一遍 TCP INSTREAM。
"""

import asyncio
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def read_instream(reader: asyncio.StreamReader) -> bytes:
    """读取客户端的 INSTREAM 报文并返回拼接后的文件内容。"""

    assert await reader.readexactly(10) == b"zINSTREAM\0"
    chunks: list[bytes] = []
    while True:
        size = int.from_bytes(await reader.readexactly(4), "big")
        if size == 0:
            return b"".join(chunks)
        chunks.append(await reader.readexactly(size))


@asynccontextmanager
async def clamd(handler: Handler) -> AsyncIterator[int]:
    """启动一个只监听本机的假 clamd，返回端口。"""

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    assert server.sockets is not None
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()


def closed_port() -> int:
    """返回一个当前无人监听的本地端口（用于连接失败场景）。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def reply_handler(reply: bytes, *, expected: bytes | None = None) -> Handler:
    """固定应答的假 clamd 处理器（可断言收到的文件字节）。"""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await read_instream(reader)
        if expected is not None:
            assert data == expected
        writer.write(reply)
        await writer.drain()
        writer.close()

    return handler
