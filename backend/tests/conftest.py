"""测试全局配置与共享门禁。

1. 单元测试不依赖开发者本机 .env：REDIS_URL 未设置时给一次性 DB 的默认值。
2. **CI 硬门禁**：设置 `REQUIRE_TEST_SERVICES=1` 时，缺少 TEST_* 环境变量会直接
   终止整个测试会话（而不是静默 skip，让租户隔离用例在流水线里"消失"）。
"""

import os

import pytest

os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/15")

REQUIRED_SERVICE_ENV = (
    "TEST_DATABASE_URL",
    "TEST_APP_DATABASE_URL",
    "TEST_PLATFORM_DATABASE_URL",
    "TEST_REDIS_URL",
    "TEST_QDRANT_URL",
)


def pytest_configure(config: pytest.Config) -> None:
    """在 CI（REQUIRE_TEST_SERVICES=1）下强制要求测试服务环境变量齐备。"""

    if os.environ.get("REQUIRE_TEST_SERVICES") != "1":
        return
    missing = [name for name in REQUIRED_SERVICE_ENV if not os.environ.get(name)]
    if missing:
        pytest.exit(
            "REQUIRE_TEST_SERVICES=1 但缺少环境变量："
            + ", ".join(missing)
            + "（集成/业务场景/RLS 用例不允许静默跳过）",
            returncode=1,
        )
