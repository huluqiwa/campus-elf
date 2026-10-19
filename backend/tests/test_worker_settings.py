"""Arq WorkerSettings 单元测试（不依赖真实 Redis）。"""

import pytest
from arq.worker import create_worker
from pydantic import ValidationError

from app.config import Settings
from app.workers.settings import WorkerSettings, build_redis_settings, queue_name
from app.workers.tasks import echo


def test_worker_settings_registers_echo() -> None:
    """Worker 入口挂载 echo 任务。"""

    assert WorkerSettings.functions == [echo]


def test_arq_cli_wires_namespaced_queue() -> None:
    """arq CLI 读取 `WorkerSettings.queue_name` 并传给 Worker（0.28 的真实契约）。"""

    worker = create_worker(WorkerSettings)

    assert worker.queue_name == "campus_elf:queue"


def test_build_redis_settings_maps_dsn() -> None:
    """DSN 各段映射到 RedisSettings（队列命名不在其中）。"""

    settings = Settings(redis_url="redis://:pw@127.0.0.1:6379/3")

    redis_settings = build_redis_settings(settings)

    assert (redis_settings.host, redis_settings.port) == ("127.0.0.1", 6379)
    assert redis_settings.database == 3
    assert redis_settings.password == "pw"


def test_queue_name_uses_prefix() -> None:
    """队列名按 REDIS_PREFIX 命名空间化。"""

    settings = Settings(redis_url="redis://:pw@127.0.0.1:6379/3", redis_prefix="campus_elf_test")

    assert queue_name(settings) == "campus_elf_test:queue"


def test_missing_redis_url_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """REDIS_URL 缺失时校验失败，不回退到本机 DB 0。"""

    monkeypatch.delenv("REDIS_URL", raising=False)

    with pytest.raises(ValidationError, match="redis_url"):
        Settings(_env_file=None)
