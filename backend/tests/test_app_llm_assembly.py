"""create_app 的 LLM 端口装配（M4-AI01a 步 8）。

显式注入优先（测试/未来 Loop 点）；缺省按 Settings 构建：未配置 → None（调用期
fail-closed），已配置 → OpenAICompatLlm。``get_settings`` 的 lru_cache 在用例
前后各清一次（对齐 test_skeleton_loop 的防泄漏口径）。

注意：项目根 .env 可能已带 LLM_* 值——monkeypatch **置空**（而非 delenv）才能
确定性压过 dotenv（env 覆盖优先，删除不改变 .env 提供的值）。
"""

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from app.config import Settings, get_settings
from app.harness.llm import LlmPort, OpenAICompatLlm, build_llm_port
from app.main import create_app
from tests.support.llm import FAKE_LLM_API_KEY, FakeLlm

_BASE_URL = "https://llm.test/v1"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """环境变量只在用例内生效：前后各清一次设置缓存。"""

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_explicit_llm_port_takes_precedence() -> None:
    """显式注入的端口直接进入 app.state（测试/Loop 点断言用）。"""

    fake = FakeLlm.answer()
    app = create_app(llm_port=fake)

    assert app.state.llm_port is fake
    assert isinstance(app.state.llm_port, LlmPort)


def test_build_llm_port_unconfigured_returns_none() -> None:
    """未配置（key 缺省）：build 返回 None，由对话链路调用期 fail-closed。"""

    assert build_llm_port(Settings(_env_file=None)) is None


def test_build_llm_port_configured_applies_a8_params() -> None:
    """已配置：适配器拿到 A8 默认超时与熔断参数（Settings → 构造参数接线）。"""

    port = build_llm_port(
        Settings(
            _env_file=None,
            llm_api_key=FAKE_LLM_API_KEY,
            llm_base_url="https://llm.test/v1",
            llm_model="test-model",
        )
    )

    assert isinstance(port, OpenAICompatLlm)
    assert isinstance(port, LlmPort)
    assert port._connect_timeout_s == 5.0
    assert port._first_token_timeout_s == 15.0
    assert port._max_attempts == 3
    assert port._breaker._failure_threshold == 5
    assert port._breaker._open_seconds == 30.0


def test_unconfigured_settings_build_none_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未配置 LLM：缺省装配为 None（env 置空压过本地 .env 的既有值）。"""

    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")

    app = create_app()

    assert app.state.llm_port is None


def test_configured_settings_build_openai_compat_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已配置 LLM：缺省装配为 OpenAICompatLlm（key 只在端口内部，不落别处）。"""

    monkeypatch.setenv("LLM_API_KEY", FAKE_LLM_API_KEY)
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    app = create_app()
    port: Any = app.state.llm_port

    assert isinstance(port, OpenAICompatLlm)
    assert isinstance(port, LlmPort)


# ---------- lifespan shutdown 钩子（复审 m2） ----------


def _noop_handler(request: httpx.Request) -> httpx.Response:
    """MockTransport 空处理器（本用例不发真实请求）。"""

    return httpx.Response(200, json={})


async def test_lifespan_shutdown_closes_llm_port() -> None:
    """shutdown 关闭 OpenAICompatLlm 持有的 SDK/httpx 客户端（复审 m2）。"""

    http = httpx.AsyncClient(transport=httpx.MockTransport(_noop_handler))
    client = AsyncOpenAI(
        api_key=FAKE_LLM_API_KEY, base_url=_BASE_URL, http_client=http, max_retries=0
    )
    adapter = OpenAICompatLlm(
        api_key=SecretStr(FAKE_LLM_API_KEY), base_url=_BASE_URL, model="test-model", client=client
    )
    app = create_app(llm_port=adapter)

    async with app.router.lifespan_context(app):
        assert app.state.llm_port is adapter

    assert http.is_closed


async def test_lifespan_shutdown_tolerates_port_without_aclose() -> None:
    """FakeLlm（无 aclose 属性）与 None 端口：shutdown 鸭子跳过、不炸（复审 m2）。"""

    app = create_app(llm_port=FakeLlm.answer())

    async with app.router.lifespan_context(app):
        assert app.state.llm_port is not None

    assert not hasattr(app.state.llm_port, "aclose")
