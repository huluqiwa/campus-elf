"""Arq + Redis 真实任务冒烟。

需要 TEST_REDIS_URL 显式指向一次性 DB（DB 号 >= 10，例如 `.../15`）；未设置时跳过。
本用例不执行 flushdb：使用随机 `REDIS_PREFIX` 派生独立队列，避免误清其他数据。
生产者与消费者都走 `app.workers.settings` 的队列命名契约（arq 0.28 下二者必须显式对齐）。
"""

import asyncio
import contextlib
import os
from typing import Final
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from arq.worker import Worker

from app.config import Settings
from app.workers.settings import build_redis_settings, create_producer_pool, queue_name
from app.workers.tasks import echo

# 低于该 DB 号视为项目业务库（含默认 DB 0），禁止用于集成测试。
_MIN_DISPOSABLE_DB: Final = 10


def _disposable_dsn(dsn: str) -> str:
    """校验 DSN 指向一次性 DB。

    参数:
        dsn: TEST_REDIS_URL 的值。

    返回值:
        原样返回的 DSN。

    异常:
        pytest.fail: DSN 未显式指定一次性 DB（防止误连业务库）。
    """

    database_segment = urlparse(dsn).path.strip("/")
    if not database_segment.isdigit() or int(database_segment) < _MIN_DISPOSABLE_DB:
        pytest.fail(
            "TEST_REDIS_URL 必须显式指向一次性 DB（如 redis://:pw@127.0.0.1:6379/15）；"
            f"当前 DB 段为 {database_segment!r}，禁止对业务库/默认 DB 0 运行集成测试。"
        )
    return dsn


@pytest.mark.integration
@pytest.mark.asyncio
async def test_arq_echo_task_on_namespaced_queue() -> None:
    """生产者按命名空间入队 → burst worker 消费 → 取回结果并清空队列。"""

    raw_dsn = os.environ.get("TEST_REDIS_URL")
    if not raw_dsn:
        pytest.skip("TEST_REDIS_URL not set; run against a disposable Redis DB")

    settings = Settings(
        redis_url=_disposable_dsn(raw_dsn),
        redis_prefix=f"campus_elf_test_{uuid4().hex[:8]}",
    )
    expected_queue = queue_name(settings)
    redis = await create_producer_pool(settings)
    worker = Worker(
        functions=[echo],
        redis_settings=build_redis_settings(settings),
        queue_name=expected_queue,
        burst=True,
        poll_delay=0.05,
        max_burst_jobs=1,
        handle_signals=False,
    )
    worker_task = asyncio.create_task(worker.async_run())

    try:
        # 不传 _queue_name：验证生产者连接池的默认队列名已与 worker 对齐。
        job = await redis.enqueue_job("echo", "hello-arq")
        assert job is not None

        result = await job.result(timeout=10)
        await asyncio.wait_for(worker_task, timeout=30)

        assert result == "hello-arq"
        assert await redis.zcard(expected_queue) == 0
    finally:
        if not worker_task.done():
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
        await redis.aclose()
