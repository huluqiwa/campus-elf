"""结构化日志配置。

所有日志（应用 structlog、标准库、uvicorn/httpx 等第三方）统一输出单行 JSON：
structlog 通过 `ProcessorFormatter` 接管 stdlib 日志，并清空 uvicorn 自带 handler
使其走同一个 root handler（TD-002）。
"""

import logging
import sys
from typing import Any

import structlog

_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# uvicorn 默认自带 handler，会绕过 root 处理器；清空后依赖向 root 传播。
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def configure_logging(level: str = "INFO") -> None:
    """配置 structlog 与标准库日志为统一 JSON 输出。

    参数:
        level: 日志级别字符串，如 INFO/DEBUG。

    返回值:
        None。

    异常:
        ValueError: 日志级别非法。
    """

    key = level.upper()
    if key not in _LEVELS:
        raise ValueError(f"unsupported log level: {level}")
    numeric_level = _LEVELS[key]

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    # structlog 与 stdlib 日志共用的前置处理器（保证两边字段形状一致）。
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(numeric_level)

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
