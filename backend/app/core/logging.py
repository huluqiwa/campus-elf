"""结构化日志配置。

应用日志使用 JSON；uvicorn/httpx 等标准库日志暂未统一为 JSON（M0-B 再接入）。
后续在 Harness/审计中补充 request_id/tenant_id，并确保不输出敏感信息。
"""

import logging
import sys

import structlog

_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def configure_logging(level: str = "INFO") -> None:
    """配置 structlog 与标准库日志。

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

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
        force=True,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
