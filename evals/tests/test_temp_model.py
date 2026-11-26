from __future__ import annotations

from pathlib import Path

import httpx
import openai
import pytest

from campus_elf_evals.temp_model import (
    RotatingAsyncOpenAI,
    is_credential_or_quota_error,
    load_temp_model_settings,
)


class _StatusError(Exception):
    def __init__(self, status_code: int, message: str, *, body: object | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def test_loads_only_temp_model_settings_from_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n".join(
            [
                "LLM_API_KEY=must-not-be-used",
                "LLM_BASE_URL=https://wrong.invalid/v1",
                "LLM_MODEL=wrong-model",
                "TEMP_API_KEY_1=temp-key-1",
                "TEMP_API_KEY_2=",
                "TEMP_API_KEY_3=temp-key-3",
                "TEMP_BASE_URL=https://temp.invalid/v1",
                "TEMP_MODEL=temp-model",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_temp_model_settings(dotenv, environ={})

    assert settings.api_keys == ("temp-key-1", "temp-key-3")
    assert settings.base_url == "https://temp.invalid/v1"
    assert settings.model == "temp-model"


def test_process_environment_overrides_temp_values_in_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n".join(
            [
                "TEMP_API_KEY_1=file-key",
                "TEMP_BASE_URL=https://file.invalid/v1",
                "TEMP_MODEL=file-model",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_temp_model_settings(
        dotenv,
        environ={
            "TEMP_API_KEY_1": "process-key",
            "TEMP_BASE_URL": "https://process.invalid/v1",
            "TEMP_MODEL": "process-model",
        },
    )

    assert settings.api_keys == ("process-key",)
    assert settings.base_url == "https://process.invalid/v1"
    assert settings.model == "process-model"


def test_missing_temp_config_error_names_variables_without_values(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("LLM_MODEL=wrong-model\n", encoding="utf-8")

    with pytest.raises(ValueError, match="TEMP_MODEL") as exc:
        load_temp_model_settings(dotenv, environ={})

    assert "wrong-model" not in str(exc.value)


@pytest.mark.parametrize(
    ("status_code", "message", "expected"),
    [
        (401, "invalid api key", True),
        (402, "payment required", True),
        (403, "insufficient_user_quota", True),
        (429, "insufficient balance", True),
        (429, "requests per minute exceeded", False),
        (500, "provider unavailable", False),
        (None, "socket reset", False),
    ],
)
def test_credential_or_quota_error_classification(
    status_code: int | None, message: str, expected: bool
) -> None:
    assert is_credential_or_quota_error(_StatusError(status_code, message)) is expected


def test_credential_or_quota_error_reads_structured_provider_body() -> None:
    error = _StatusError(
        429,
        "provider rejected request",
        body={"error": {"type": "insufficient_quota", "code": "account_empty"}},
    )

    assert is_credential_or_quota_error(error)


def _completion_response(model: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        },
    )


@pytest.mark.asyncio
async def test_rotating_client_switches_once_on_insufficient_balance() -> None:
    seen_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers["authorization"]
        seen_keys.append(auth)
        if auth == "Bearer temp-key-1":
            return httpx.Response(
                429,
                json={"error": {"message": "insufficient_user_quota", "type": "quota"}},
            )
        return _completion_response("temp-model")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = RotatingAsyncOpenAI(
            api_keys=("temp-key-1", "temp-key-2"),
            base_url="https://temp.invalid/v1",
            http_client=http_client,
            max_retries=0,
        )
        response = await client.chat.completions.create(
            model="temp-model",
            messages=[{"role": "user", "content": "score this"}],
        )

    assert response.choices[0].message.content == "ok"
    assert seen_keys == ["Bearer temp-key-1", "Bearer temp-key-2"]
    assert client.active_key_slot == 2


@pytest.mark.asyncio
async def test_rotating_client_does_not_switch_on_ordinary_rate_limit() -> None:
    seen_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_keys.append(request.headers["authorization"])
        return httpx.Response(
            429,
            json={"error": {"message": "requests per minute exceeded", "type": "rate_limit"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = RotatingAsyncOpenAI(
            api_keys=("temp-key-1", "temp-key-2"),
            base_url="https://temp.invalid/v1",
            http_client=http_client,
            max_retries=0,
        )
        with pytest.raises(openai.RateLimitError):
            await client.chat.completions.create(
                model="temp-model",
                messages=[{"role": "user", "content": "score this"}],
            )

    assert seen_keys == ["Bearer temp-key-1"]
    assert client.active_key_slot == 1


@pytest.mark.asyncio
async def test_rotating_client_stops_after_all_temp_key_slots_fail() -> None:
    seen_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_keys.append(request.headers["authorization"])
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = RotatingAsyncOpenAI(
            api_keys=("temp-key-1", "temp-key-2"),
            base_url="https://temp.invalid/v1",
            http_client=http_client,
            max_retries=0,
        )
        with pytest.raises(openai.AuthenticationError):
            await client.chat.completions.create(
                model="temp-model",
                messages=[{"role": "user", "content": "score this"}],
            )

    assert seen_keys == ["Bearer temp-key-1", "Bearer temp-key-2"]
    assert client.active_key_slot == 2


@pytest.mark.asyncio
async def test_rotating_client_repr_redacts_keys() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None)) as http_client:
        client = RotatingAsyncOpenAI(
            api_keys=("client-secret-slot",),
            base_url="https://temp.invalid/v1",
            http_client=http_client,
            max_retries=0,
        )

    assert "client-secret-slot" not in repr(client)
    assert repr(client) == "RotatingAsyncOpenAI(key_slot_count=1)"


def test_temp_settings_never_expose_keys_in_repr(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "TEMP_API_KEY_1=secret-slot-value\nTEMP_BASE_URL=https://temp.invalid/v1\nTEMP_MODEL=temp-model\n",
        encoding="utf-8",
    )

    settings = load_temp_model_settings(dotenv, environ={})
    rendered = repr(settings) + repr(settings.api_keys)

    assert "secret-slot-value" not in rendered
    assert "temp.invalid" not in rendered
    assert len(settings.api_keys) == 1
    assert settings.model == "temp-model"
