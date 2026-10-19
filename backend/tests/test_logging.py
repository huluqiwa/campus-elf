"""结构化日志测试（TD-002）。"""

import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from app.core.logging import configure_logging


@pytest.fixture
def _restore_logging() -> Iterator[None]:
    """用例结束后恢复 root logger 与 structlog 配置，避免影响其他用例。"""

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    uvicorn_saved = {
        name: (list(logging.getLogger(name).handlers), logging.getLogger(name).propagate)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    }
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)
    for name, (handlers, propagate) in uvicorn_saved.items():
        logging.getLogger(name).handlers = handlers
        logging.getLogger(name).propagate = propagate
    structlog.reset_defaults()


def test_app_and_uvicorn_logs_are_single_line_json(
    capsys: pytest.CaptureFixture[str], _restore_logging: None
) -> None:
    """应用与 uvicorn 日志都输出单行 JSON，字段形状一致。"""

    configure_logging("INFO")

    structlog.get_logger("app.test").info("hello_event", answer="值")
    logging.getLogger("uvicorn.error").info("uvicorn started")

    lines = [line for line in capsys.readouterr().out.strip().splitlines() if line]
    assert len(lines) == 2

    app_record, uvicorn_record = (json.loads(line) for line in lines)
    assert app_record["event"] == "hello_event"
    assert app_record["answer"] == "值"
    assert app_record["level"] == "info"
    assert app_record["logger"] == "app.test"
    assert uvicorn_record["event"] == "uvicorn started"
    assert uvicorn_record["logger"] == "uvicorn.error"
    for record in (app_record, uvicorn_record):
        assert "timestamp" in record


def test_invalid_level_raises() -> None:
    """非法日志级别直接报错，避免静默使用默认级别。"""

    with pytest.raises(ValueError, match="unsupported log level"):
        configure_logging("VERBOSE")
