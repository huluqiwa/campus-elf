"""Development-only temporary model configuration for offline evaluations.

The evaluation runner must use the explicit ``TEMP_*`` credentials configured in
the repository ``.env``.  It deliberately never falls back to ``LLM_*`` or
``VLM_*``: an accidental fallback would make a report claim the temporary model
while calling a different provider configuration.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from openai import AsyncOpenAI

PROJECT_ROOT = Path(__file__).resolve().parents[3]
_TEMP_KEY_NAMES = tuple(f"TEMP_API_KEY_{slot}" for slot in range(1, 11))
_QUOTA_MARKERS = (
    "insufficient_quota",
    "insufficient quota",
    "insufficient_user_quota",
    "insufficient balance",
    "insufficient_credit",
    "no credit",
    "billing",
    "payment required",
    "额度不足",
    "余额不足",
    "配额不足",
    "欠费",
    "额度已用尽",
)


def _parse_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        name = name.strip()
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values


def _merged_value(
    name: str, file_values: Mapping[str, str], environ: Mapping[str, str]
) -> str:
    return environ.get(name, file_values.get(name, "")).strip()


class SecretKeyPool(tuple[str, ...]):
    """Ordered API key pool whose diagnostic representations are always redacted."""

    def __new__(cls, values: Sequence[str]) -> SecretKeyPool:
        return super().__new__(cls, values)

    def __repr__(self) -> str:
        return f"<redacted temporary API keys: {len(self)}>"

    __str__ = __repr__


@dataclass(frozen=True)
class TempModelSettings:
    api_keys: Sequence[str] = field(repr=False)
    base_url: str
    model: str

    def __repr__(self) -> str:
        return (
            f"TempModelSettings(api_key_count={len(self.api_keys)}, "
            f"model={self.model!r})"
        )


def load_temp_model_settings(
    dotenv_path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> TempModelSettings:
    """Load—and validate—the temporary model pool without exposing secret values."""

    path = dotenv_path or PROJECT_ROOT / ".env"
    file_values = _parse_dotenv(path)
    process_values = os.environ if environ is None else environ
    api_keys = SecretKeyPool(
        tuple(
            dict.fromkeys(
                value
                for name in _TEMP_KEY_NAMES
                if (value := _merged_value(name, file_values, process_values))
            )
        )
    )
    base_url = _merged_value("TEMP_BASE_URL", file_values, process_values)
    model = _merged_value("TEMP_MODEL", file_values, process_values)

    missing = []
    if not api_keys:
        missing.append("TEMP_API_KEY_1..10")
    if not base_url:
        missing.append("TEMP_BASE_URL")
    if not model:
        missing.append("TEMP_MODEL")
    if missing:
        raise ValueError(f"缺少临时模型配置：{', '.join(missing)}")

    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "TEMP_BASE_URL 必须是 http(s) 绝对地址，且不能包含凭证/查询串/fragment"
        )

    return TempModelSettings(api_keys=api_keys, base_url=base_url, model=model)


def is_credential_or_quota_error(exc: BaseException) -> bool:
    """Whether a provider error means this key cannot be used anymore.

    Ordinary rate limits, timeouts and provider 5xx responses remain on the
    current key so quota rotation cannot silently turn into application retries.
    """

    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    if status in {401, 402}:
        return True

    code = getattr(exc, "code", "")
    error_type = getattr(exc, "type", "")
    body = getattr(exc, "body", None)
    try:
        body_text = json.dumps(body, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        body_text = repr(body)
    text = f"{exc} {code} {error_type} {body_text}".casefold()
    if status in {403, 429}:
        return any(marker in text for marker in _QUOTA_MARKERS)
    return False


class RotatingAsyncOpenAI(AsyncOpenAI):
    """AsyncOpenAI client that advances through temporary key slots on exhaustion.

    Rotation is monotonic: slots are tried in the order supplied by
    ``TEMP_API_KEY_1..10`` and an exhausted slot is never revisited in this run.
    Multiple concurrent requests share the same client instance, so the active
    slot is protected by an asyncio lock.
    """

    def __init__(
        self,
        *,
        api_keys: Sequence[str],
        base_url: str,
        http_client: Any,
        max_retries: int = 0,
        **kwargs: Any,
    ) -> None:
        keys = tuple(dict.fromkeys(key.strip() for key in api_keys if key.strip()))
        if not keys:
            raise ValueError("临时模型 key 池不能为空")
        self._temp_api_keys = keys
        self._active_key_index = 0
        self._rotation_lock = asyncio.Lock()
        super().__init__(
            api_key=keys[0],
            base_url=base_url,
            http_client=http_client,
            max_retries=max_retries,
            **kwargs,
        )

    def __repr__(self) -> str:
        return f"RotatingAsyncOpenAI(key_slot_count={self.key_slot_count})"

    @property
    def active_key_slot(self) -> int:
        return self._active_key_index + 1

    @property
    def key_slot_count(self) -> int:
        return len(self._temp_api_keys)

    async def _advance_after_failure(self, failed_index: int) -> bool:
        async with self._rotation_lock:
            if failed_index != self._active_key_index:
                return True
            next_index = self._active_key_index + 1
            if next_index >= len(self._temp_api_keys):
                return False
            self._active_key_index = next_index
            self.api_key = self._temp_api_keys[next_index]
            return True

    async def post(self, path: str, **kwargs: Any) -> Any:
        while True:
            failed_index = self._active_key_index
            try:
                return await super().post(path, **kwargs)
            except Exception as exc:
                if not is_credential_or_quota_error(exc):
                    raise
                if not await self._advance_after_failure(failed_index):
                    raise
