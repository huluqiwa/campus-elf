"""测试用假值集中定义（TD-052）。

统一 `FAKE_*` 前缀，便于 secret 扫描白名单；这些值不用于任何真实服务。
"""

FAKE_AUDIT_SECRET = "fake-audit-secret-0123456789"
FAKE_REDIS_PASSWORD = "fake-redis-password"
FAKE_ENCRYPTION_KEY = "fake-encryption-key-0123456789abcdef"
