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
from app.kb.build_task import build_kb_version, retry_stale_build_jobs
from app.kb.scan_task import retry_pending_kb_scans, scan_kb_version
from app.kb.test_task import run_kb_test
from app.org.import_task import fail_stale_import_jobs, run_import_job
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
    # B2（审查）：arq worker 默认 job_timeout=300s 会腰斩合法长解析（MinerU 上限
    # 600s、冷启动 248s 实证）——构建任务显式给 30 分钟；取消未捕获会滞留 BUILDING 假 STALE。
    build_timeout_s = 1800
    # 测试运行任务的超时（KB-05B 补注册时定）：与 build 的显式超时同理由，覆盖大测试集的
    # 嵌入 + 逐条 Qdrant 检索；上限同 build 取整（arq 默认 300s 对 200 条用例不够）。
    test_timeout_s = 600
    functions: Sequence[WorkerCoroutine | Function] = [
        cast(WorkerCoroutine, echo),
        run_import_job,
        scan_kb_version,
        Function("build_kb_version", build_kb_version, timeout_s=build_timeout_s, keep_result_s=None, keep_result_forever=None, max_tries=None),
        # KB-04A 测试运行任务（KB-05B 全栈 E2E 发现的漏注册修复）：真实部署里测试运行
        # 经队列执行；不注册则 worker 报 function not found、运行永远停在 TESTING。
        # 显式 timeout 与 build 同型：200 条用例 × 逐条嵌入 + 检索在 CPU TEI 上可超 arq 默认 300s。
        Function("run_kb_test", run_kb_test, timeout_s=test_timeout_s, keep_result_s=None, keep_result_forever=None, max_tries=None),
    ]
    # 每天 03:30（UTC）清理过期/已撤销刷新令牌（TD-067）
    cron_jobs: Sequence[CronJob] | None = [
        # 构建看门狗（KB-03A）：与导入看门狗同 15 分钟节奏，错开分钟位避免同拍拥挤
        *[
            CronJob(
                "retry_stale_build_jobs",
                cast(WorkerCoroutine, retry_stale_build_jobs),
                month=None,
                day=None,
                weekday=None,
                hour=None,
                minute=(minute + 7) % 60,
                second=0,
                microsecond=0,
                run_at_startup=False,
                unique=True,
                job_id=None,
                timeout_s=None,
                keep_result_s=None,
                keep_result_forever=None,
                max_tries=None,
                next_run=None,
            )
            for minute in (0, 15, 30, 45)
        ],
        # 看门狗每 15 分钟（:00/:15/:30/:45 四条；arq cron 的 minute 语义是「每小时第 N 分」）
        *[
            CronJob(
                "fail_stale_import_jobs",
                cast(WorkerCoroutine, fail_stale_import_jobs),
                month=None,
                day=None,
                weekday=None,
                hour=None,
                minute=minute,
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
            for minute in (0, 15, 30, 45)
        ],
        # 知识库扫描看门狗每 5 分钟补投滞留 UPLOADED 版本（KB-01）
        *[
            CronJob(
                "retry_pending_kb_scans",
                cast(WorkerCoroutine, retry_pending_kb_scans),
                month=None,
                day=None,
                weekday=None,
                hour=None,
                minute=minute,
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
            for minute in range(0, 60, 5)
        ],
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
