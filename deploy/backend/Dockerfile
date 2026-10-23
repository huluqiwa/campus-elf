# 后端应用镜像（M1 验收 TD-110 全栈 E2E 首次引入；生产硬化为 M6 范围，见 TD 台账）。
# 与开发栈一致：Python 3.13 + uv --frozen 同步依赖；非 root 运行。
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# 先只拷依赖清单，利用层缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 再拷应用代码（app 为顶层包，不需要安装项目本身）
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts

RUN useradd --create-home appuser \
    && chown -R appuser:appuser /app
USER appuser
ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
