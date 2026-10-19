# 后端（M0 骨架）

## 环境

- Python 3.13.x（uv 管理）
- 依赖见 `pyproject.toml`；`uv.lock` 入库

## 本地运行

```bash
cd backend
uv sync
uv run uvicorn app.main:app --reload
```

健康检查（根路径，供 Compose/Nginx/探针使用）：

- `GET /health/live`
- `GET /health/ready`

配置从项目根目录 `.env` 读取（与 `.env.example` 同目录）；不要提交 `.env`。

## 测试与检查

```bash
uv run pytest
uv run ruff check .
uv run mypy app tests
```

## 当前范围

- M0-A：FastAPI 最小骨架、配置、JSON 日志、根路径健康检查。
- M0-C：Harness 契约/能力与工具注册表/工具执行器/trace/审计 HMAC 哈希链/Prompt 注册表。
- 尚未接入：DB/Redis/Qdrant、LangGraph 图、真实 KB 工具、Worker、业务路由。

## Harness

```text
app/harness/
├─ contracts.py   AgentRequest/TenantContext/ToolSpec/StopReason/TraceRecord 等
├─ registry.py    CapabilityRegistry / ToolRegistry
├─ tools.py       ToolExecutor：租户/权限/只读/可用性/数据范围/schema/超时/审计
├─ trace.py       本地 trace sink（测试/开发用内存实现）
├─ audit.py       审计事件 + HMAC 哈希链 + 脱敏（内存实现）
└─ prompts.py     版本化 Prompt 注册/严格渲染
```

测试：

```bash
uv run pytest tests/harness
```

## 日志与入口约束

- `app/main.py` 在模块导入时调用 `configure_logging`，把 structlog/标准库/uvicorn 统一为单行 JSON。
- 该顺序依赖 uvicorn 的自带 handler 被清空，因此：
  - 推荐入口：`uv run uvicorn app.main:app`（uvicorn 在导入后初始化日志，JSON 生效）；
  - 程序化入口：若先 `from app.main import app` 再 `uvicorn.run(app, ...)`，必须传 `log_config=None`，
    否则 uvicorn 会重新挂上自己的文本 handler（唯一已知退化路径）。
