"""测试全局配置与共享夹具。

- 单元测试不依赖开发者本机 .env：REDIS_URL 未设置时给一次性 DB 的默认值
  （模块导入期应用的 Settings 校验需要它）。集成用例仍以 TEST_REDIS_URL 为准。
- 假密钥集中在 `tests/support/fakes.py`（TD-052），避免用例里散落形似凭证的字符串。
"""

import os

os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/15")
