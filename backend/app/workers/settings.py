"""Arq Worker 设置。

arq 0.28 的队列名**不是** `RedisSettings` 的属性：Worker 由 arq CLI 读取
`WorkerSettings.queue_name` 传入 `Worker(queue_name=...)`，生产者必须在
`create_pool(..., default_queue_name=...)` 指定（否则落到常量 `arq:queue`）。
本模块把这两端收敛到同一处，避免命名空间分叉。
"""

from collections.abc import Sequence
from typing import cast

from arq.connections import ArqRedis, RedisSettings, create_pool
from arq.cron import CronJob
from arq.typing import StartupShutdown, WorkerCoroutine
from arq.worker import Function

from app.config import Settings, get_settings
from app.workers.tasks import cleanup_refresh_tokens, echo


def build_redis_settings(settings: Settings) -> RedisSettings:
    """由应用配置构造 arq 连接参数（只负责连接，不含队列命名）。

    参数:
        settings: 已校验的应用配置（含必填 redis_url）。

    返回值:
        RedisSettings 实例。

    异常:
        无。DSN 非法时由 arq 抛出 ValueError，Worker 启动即失败。
    """

    return RedisSettings.from_dsn(settings.redis_url)


def queue_name(settings: Settings) -> str:
    """返回命名空间化的队列名 `<redis_prefix>:queue`。"""

    return f"{settings.redis_prefix}:queue"


async def create_producer_pool(settings: Settings | None = None) -> ArqRedis:
    """创建使用命名空间队列的生产者连接池。

    参数:
        settings: 可选配置覆盖；默认取进程缓存的配置。

    返回值:
        ArqRedis 连接池，其 default_queue_name 已指向 `queue_name(settings)`。
    """

    resolved = settings if settings is not None else get_settings()
    return await create_pool(
        build_redis_settings(resolved),
        default_queue_name=queue_name(resolved),
    )


def _redis_settings() -> RedisSettings:
    """进程内读取缓存的配置构造 RedisSettings。"""

    return build_redis_settings(get_settings())


class WorkerSettings:
    """Arq worker 入口（`arq app.workers.settings.WorkerSettings`）。

    REDIS_URL 缺失时 `get_settings()` 抛 ValidationError，Worker 启动即失败，
    不会回退到本机无凭证 DB 0。

    前四个属性按 arq 的 `WorkerSettingsBase` 协议显式声明（协议要求成员齐全，
    mypy 才能校验 `arq.worker.create_worker` 的入参）；`queue_name` 是 arq CLI
    传给 `Worker` 的队列名，缺省会用常量 `arq:queue`。
    """

    # echo 的签名比 WorkerCoroutine 协议更窄（协议要求 *args/**kwargs），故 cast。
    functions: Sequence[WorkerCoroutine | Function] = [cast(WorkerCoroutine, echo)]
    # 每天 03:30（UTC）清理过期/已撤销刷新令牌（TD-067）
    cron_jobs: Sequence[CronJob] | None = [
        CronJob(
            "cleanup_refresh_tokens",
            cast(WorkerCoroutine, cleanup_refresh_tokens),
            month=None,
            day=None,
            weekday=None,
            hour=3,
            minute=30,
            second=0,
            microsecond=0,
            run_at_startup=False,
            unique=True,
            job_id=None,
            timeout_s=None,
            keep_result_s=None,
            keep_result_forever=None,
            max_tries=None,
        )
    ]
    on_startup: StartupShutdown | None = None
    on_shutdown: StartupShutdown | None = None
    redis_settings: RedisSettings = _redis_settings()
    queue_name: str = queue_name(get_settings())
