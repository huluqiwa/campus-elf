"""FastAPI 应用入口与工厂。

`create_app()` 只做装配（含可注入 trace sink），不产生日志配置等副作用；
进程入口在模块层配置日志。进程级 `app` 使用内存 trace sink
（M1 起替换为 PostgreSQL + JSONL 实现）。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.auth import router as auth_router
from app.api.body_limit import BodySizeLimitMiddleware
from app.api.campus_chat import router as campus_chat_router
from app.api.health import router as health_router
from app.api.platform_audit import router as platform_audit_router
from app.api.platform_rbac import router as platform_rbac_router
from app.api.platform_tenants import router as platform_tenants_router
from app.api.school_imports import router as school_imports_router
from app.api.school_org import router as school_org_router
from app.api.school_personnel import router as school_personnel_router
from app.api.school_rbac import router as school_rbac_router
from app.config import get_settings
from app.core.logging import configure_logging
from app.db.selfcheck import verify_runtime_database
from app.harness.trace import InMemoryTraceSink, TraceSink
from app.tenant.middleware import RequestContextMiddleware

# 10MB 资质文件 + multipart 边界/表单字段余量（仅注册路由）
MAX_REGISTER_BODY_BYTES = 11 * 1024 * 1024
# 导入文件 ≤10MB（解析器 MAX_FILE_SIZE 同口径）+ multipart 表单余量（M2-ORG02）
MAX_IMPORT_BODY_BYTES = 11 * 1024 * 1024


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    """启动时对预发/生产执行数据库安全自检（TD-073）。"""

    if get_settings().app_env in {"staging", "production"}:
        await verify_runtime_database()
    yield


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
        lifespan=_lifespan,
    )
    application.state.trace_sink = trace_sink if trace_sink is not None else InMemoryTraceSink()

    @application.exception_handler(RequestValidationError)
    async def _sanitized_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        """422 只回字段位置/原因/类型，不回显 `input`（避免密码/令牌进响应/日志）。"""

        details = [
            {
                "loc": list(error.get("loc", ())),
                "msg": str(error.get("msg", "")),
                "type": str(error.get("type", "")),
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": details})

    # 请求级：生成/校验 request_id，并在请求结束时清空身份上下文（防协程复用泄漏）
    # 后添加的中间件在最外层：RequestContext 必须包住 BodySize，413 才带 X-Request-ID。
    # 体积限制只作用于登记路径，避免给后续 KB 大上传引入全局上限（M27）
    application.add_middleware(
        BodySizeLimitMiddleware,
        limits={
            f"{settings.api_prefix}/platform/tenants/register": MAX_REGISTER_BODY_BYTES,
            # M2-ORG02：导入文件体积限制（10MB 文件 + 表单余量）
            f"{settings.api_prefix}/school/imports": MAX_IMPORT_BODY_BYTES,
        },
    )
    application.add_middleware(RequestContextMiddleware)

    # 健康检查固定在根路径，供 Compose/Nginx/探针使用；业务路由挂 api_prefix。
    application.include_router(health_router)
    application.include_router(auth_router, prefix=settings.api_prefix)
    application.include_router(campus_chat_router, prefix=settings.api_prefix)
    application.include_router(platform_audit_router, prefix=settings.api_prefix)
    application.include_router(platform_tenants_router, prefix=settings.api_prefix)
    application.include_router(school_personnel_router, prefix=settings.api_prefix)
    application.include_router(school_org_router, prefix=settings.api_prefix)
    application.include_router(school_imports_router, prefix=settings.api_prefix)
    application.include_router(school_rbac_router, prefix=settings.api_prefix)
    application.include_router(platform_rbac_router, prefix=settings.api_prefix)
    return application


settings = get_settings()
configure_logging(settings.log_level)

app = create_app()
