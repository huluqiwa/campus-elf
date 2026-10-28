"""测试全局配置与共享门禁。

1. 单元测试不依赖开发者本机 .env：REDIS_URL 未设置时给一次性 DB 的默认值。
2. **CI 硬门禁**：设置 `REQUIRE_TEST_SERVICES=1` 时，缺少 TEST_* 环境变量会直接
   终止整个测试会话（而不是静默 skip，让租户隔离用例在流水线里"消失"）。
"""

import base64
import os

import pytest

os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/15")
# 认证密钥：单元/集成测试不作为密钥强度验证对象（生产由配置校验强制 ≥32 字节且非占位值）
os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-0123456789abcdef")
# 字段加密测试密钥：固定 32 字节 base64，仅用于测试库中的 MFA secret 往返
os.environ.setdefault("ENCRYPTION_KEK", base64.b64encode(b"0" * 32).decode())
# 指标端点默认关闭（生产默认不暴露）；测试里**强制**开启，便于断言计数器与 /metrics 输出。
# 用直接赋值而不是 setdefault：开发者 shell 里的 METRICS_ENABLED=false 会让所有指标断言悄悄失效。
os.environ["METRICS_ENABLED"] = "true"
# 本机服务（PG/Redis/Qdrant/MailHog）不得走系统代理：httpx 在 Windows 会读注册表代理设置，
# 若不绕过，127.0.0.1 的请求会被发到代理端口（表现为连接被拒）。
for _proxy_key in ("NO_PROXY", "no_proxy"):
    os.environ.setdefault(_proxy_key, "127.0.0.1,localhost,::1")

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
