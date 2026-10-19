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

M0-A 只包含 FastAPI 最小骨架与健康检查；DB/Redis/Qdrant、Harness、Worker 在后续 M0 单元接入。
