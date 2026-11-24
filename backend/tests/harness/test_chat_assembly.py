"""chat 装配接线测试（TD-354）：回合首字硬超时从 Settings 传入两条路径。"""

from __future__ import annotations

from app.config import Settings
from app.harness.chat_assembly import build_chat_loop
from app.harness.trace import InMemoryTraceSink


def test_build_chat_loop_wires_turn_timeout_from_settings() -> None:
    settings = Settings(_env_file=None, llm_turn_timeout_seconds=120.0)
    loop = build_chat_loop(settings, trace_sink=InMemoryTraceSink(), llm_port=None)
    assert loop._fast_loop._turn_timeout_s == 120.0
    assert loop._react_loop._turn_timeout_s == 120.0


def test_build_chat_loop_turn_timeout_defaults_30() -> None:
    settings = Settings(_env_file=None)
    loop = build_chat_loop(settings, trace_sink=InMemoryTraceSink(), llm_port=None)
    assert loop._fast_loop._turn_timeout_s == 30.0
    assert loop._react_loop._turn_timeout_s == 30.0
