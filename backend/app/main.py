"""FastAPI 应用入口与工厂。

`create_app()` 只做装配（含可注入 trace sink），不产生日志配置等副作用；
进程入口在模块层配置日志。进程级 `app` 使用内存 trace sink
（M1 起替换为 PostgreSQL + JSONL 实现）。
"""

from fastapi import FastAPI

from app.api.campus_chat import router as campus_chat_router
from app.api.health import router as health_router
from app.config import get_settings
from app.core.logging import configure_logging
from app.harness.trace import InMemoryTraceSink, TraceSink
from app.tenant.middleware import RequestContextMiddleware


def create_app(*, trace_sink: TraceSink | None = None) -> FastAPI:
    """构造 FastAPI 应用。

    参数:
        trace_sink: 可选 trace sink（测试注入内存实现以断言 trace 落点）。

    返回值:
        FastAPI 实例。

    异常:
        pydantic.ValidationError: 配置缺失/非法（含生产必需项校验）。
    """

    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
    )
    application.state.trace_sink = trace_sink if trace_sink is not None else InMemoryTraceSink()

    # 请求级：生成/校验 request_id，并在请求结束时清空身份上下文（防协程复用泄漏）
    application.add_middleware(RequestContextMiddleware)

    # 健康检查固定在根路径，供 Compose/Nginx/探针使用；业务路由挂 api_prefix。
    application.include_router(health_router)
    application.include_router(campus_chat_router, prefix=settings.api_prefix)
    return application


settings = get_settings()
configure_logging(settings.log_level)

app = create_app()
