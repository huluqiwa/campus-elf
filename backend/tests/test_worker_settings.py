"""Arq WorkerSettings 单元测试（不依赖真实 Redis）。"""

import pytest
from arq.worker import create_worker
from pydantic import ValidationError

from app.config import Settings
from app.kb.scan_task import scan_kb_version
from app.org.import_task import run_import_job
from app.workers.settings import WorkerSettings, build_redis_settings, queue_name
from app.workers.tasks import echo


def test_worker_settings_registers_tasks() -> None:
    """Worker 入口挂载 echo、导入任务（M2-ORG02）、知识库扫描（KB-01）与构建（KB-03A）。"""

    assert len(WorkerSettings.functions) == 4
    names = [getattr(item, 'name', None) for item in WorkerSettings.functions]
    assert run_import_job in WorkerSettings.functions
    assert scan_kb_version in WorkerSettings.functions
    assert echo in WorkerSettings.functions
    # build_kb_version 经 Function 包装（timeout_s=1800），按 name 断言
    assert 'build_kb_version' in names


def test_worker_settings_build_function_has_explicit_timeout() -> None:
    """审查 B2：arq 默认 job_timeout=300s 会腰斩合法长解析（MinerU 上限 600s）——
    构建任务必须显式覆盖（Function 包装 timeout_s=1800）。"""

    from arq.worker import Function

    functions = [item for item in WorkerSettings.functions if isinstance(item, Function)]
    build = next(item for item in functions if item.name == "build_kb_version")
    assert build.timeout_s == 1800


def test_worker_settings_registers_kb_scan_watchdog_cron() -> None:
    """知识库扫描看门狗每 5 分钟补投滞留版本（12 条）。"""

    cron = [
        job for job in (WorkerSettings.cron_jobs or []) if job.name == "retry_pending_kb_scans"
    ]
    assert len(cron) == 12
    assert {job.minute for job in cron} == set(range(0, 60, 5))


def test_worker_settings_registers_import_watchdog_cron() -> None:
    """看门狗 cron 每 15 分钟（:00/:15/:30/:45 四条）注册。"""

    cron = [job for job in (WorkerSettings.cron_jobs or []) if "import" in job.name]
    assert len(cron) == 4
    assert {job.minute for job in cron} == {0, 15, 30, 45}


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
