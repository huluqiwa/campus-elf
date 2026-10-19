"""Langfuse 观测栈连通性冒烟（M0-E4 可复现证据）。

用法（先启动 deploy/observability 栈）:

    cd evals
    uv run python scripts/langfuse_smoke.py

凭据来源优先级：环境变量 LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY，
否则读取 `deploy/observability/.env` 的 NEXTAUTH_URL 与 LANGFUSE_INIT_PROJECT_* 。

成功时输出 `OK trace=<id>`（退出码 0）；失败输出原因（退出码 1）。
"""

import os
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
OBSERVABILITY_ENV = REPO_ROOT / "deploy" / "observability" / ".env"
DEFAULT_HOST = "http://127.0.0.1:3000"
SMOKE_SPAN_NAME = "m0e-smoke"


def _read_env_file(path: Path) -> dict[str, str]:
    """读取 .env 为键值映射（忽略注释与空行）。"""

    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _resolve_credentials() -> tuple[str, str, str]:
    """返回 (host, public_key, secret_key)。

    异常:
        RuntimeError: 缺少必要凭据。
    """

    file_values = _read_env_file(OBSERVABILITY_ENV)
    host = os.environ.get("LANGFUSE_HOST") or file_values.get("NEXTAUTH_URL") or DEFAULT_HOST
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY") or file_values.get(
        "LANGFUSE_INIT_PROJECT_PUBLIC_KEY", ""
    )
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY") or file_values.get(
        "LANGFUSE_INIT_PROJECT_SECRET_KEY", ""
    )
    if not public_key or not secret_key:
        raise RuntimeError(
            "缺少 Langfuse 凭据：请设置 LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY，"
            f"或确认 {OBSERVABILITY_ENV} 已生成 LANGFUSE_INIT_PROJECT_* 。"
        )
    return host.rstrip("/"), public_key, secret_key


def main() -> int:
    """写入一条 trace 并通过 v2 observations API 复查。"""

    try:
        host, public_key, secret_key = _resolve_credentials()
    except RuntimeError as exc:
        print(f"FAIL {exc}")
        return 1

    from langfuse import Langfuse

    client = Langfuse(public_key=public_key, secret_key=secret_key, host=host)

    try:
        with client.start_as_current_observation(name=SMOKE_SPAN_NAME):
            client.update_current_span(
                output={"smoke": True, "source": "evals/scripts/langfuse_smoke.py"}
            )
            trace_id = client.get_current_trace_id()

        client.flush()

        response = httpx.get(
            f"{host}/api/public/v2/observations",
            params={"limit": 5},
            auth=(public_key, secret_key),
            timeout=15,
        )
        response.raise_for_status()
        items = response.json().get("data", [])
        matched = [item for item in items if item.get("name") == SMOKE_SPAN_NAME]
    except Exception as exc:  # 冒烟脚本需给出可读失败原因（栈未启动/凭据错误等）
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1

    if not matched:
        print(f"FAIL trace {trace_id} 未在 observations API 中出现（返回 {len(items)} 条）")
        return 1

    print(f"OK trace={trace_id} observation={matched[0]['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
