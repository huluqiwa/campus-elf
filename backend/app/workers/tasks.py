"""Worker 任务定义。"""

from typing import Any


async def echo(ctx: dict[str, Any], value: str) -> str:
    """回显任务，用于 Arq/Redis 连通性冒烟。

    参数:
        ctx: Arq 任务上下文。
        value: 要回显的字符串。

    返回值:
        原样返回 value。

    异常:
        无。
    """

    return value
