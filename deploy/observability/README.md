# Langfuse 4.63.0 开发观测栈（optional）

> 生产观测使用**独立主机自托管**：由 SaaS 运营方部署运维、平台/运维专属访问；校方不部署、不直接访问（见 `docs/deployment.md` §7）。
> 本目录仅用于开发/测试按需验证，默认不随 core/rag 启动。
> 所有端口只绑定 `127.0.0.1`。敏感项在 compose 中为必填（`${VAR:?…}`）：缺少 `.env` 会直接报错，不会用可猜默认凭据启动。

## 组件

| 服务 | 镜像 | 端口（127.0.0.1） |
|---|---|---|
| langfuse-web | `docker.langfuse.com/langfuse/langfuse:4.63.0` | 3000 |
| langfuse-worker | `docker.langfuse.com/langfuse/langfuse-worker:4.63.0` | 3030 |
| Postgres | `postgres:17-alpine`（`POSTGRES_VERSION` 可覆盖） | 5433 |
| ClickHouse | `clickhouse/clickhouse-server:25.12` | 8123 / 9100 |
| Redis | `redis:7.4-alpine` | 6380 |
| MinIO | 本项目源码构建 `minio:RELEASE.2025-10-15T17-29-55Z` | 9090 / 9091 |

## 启动

```bash
# 1. 先构建 MinIO 镜像（compose 中 pull_policy: never，本地不存在会直接报错）
cd deploy/minio
docker build -t minio:RELEASE.2025-10-15T17-29-55Z .
cd ../observability

# 2. 生成配置并替换所有 change-me
cp .env.example .env
docker compose up -d
curl http://127.0.0.1:3000/api/public/health
```

`DATABASE_URL` 由 `POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB` 在 compose 中拼装，不需要（也不应）在 `.env` 单独维护，避免改密码漏改连接串导致 P1000 认证失败（TD-044）。敏感项为必填，缺少 `.env` 或仍是 `change-me` 会直接报错。

**首次写入 trace 前**必须存在 `langfuse` bucket（Langfuse 不会自动创建，缺少时 trace 上传静默失败、observations 查询为空）。用 backend 的 aioboto3 按 `.env` 的 MinIO 凭据创建：

```bash
cd backend && uv run python -c "
import asyncio
from pathlib import Path
import aioboto3
env = dict(l.split('=', 1) for l in Path('../deploy/observability/.env').read_text(encoding='utf-8').splitlines() if '=' in l and not l.startswith('#'))
async def main():
    async with aioboto3.Session().client('s3', endpoint_url='http://127.0.0.1:9090',
        aws_access_key_id=env['MINIO_ROOT_USER'].strip(), aws_secret_access_key=env['MINIO_ROOT_PASSWORD'].strip(),
        region_name='auto') as s3:
        await s3.create_bucket(Bucket='langfuse')
        print([b['Name'] for b in (await s3.list_buckets())['Buckets']])
asyncio.run(main())
"
```

## 可复现冒烟

```bash
cd evals
uv run python scripts/langfuse_smoke.py
```

脚本读取 `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`（默认取本目录 `.env` 的 `LANGFUSE_INIT_*`），写入一条 trace 后通过 v2 observations API 复查，输出 `OK trace=<id>`。

## 停止

```bash
docker compose down
```

## 注意

- 该栈内存占用约 5–6GB；与 core+rag 同时常驻会接近 16GB 主机上限，建议单独启动验证。
- `.env` 已被 `.gitignore` 忽略；不要提交。
- 默认 `LANGFUSE_ENABLED=false`；应用侧接入前无上报路径，接入后关闭该开关即不影响聊天。
- 生产必须使用独立观测主机或独立部署，不复用本目录的开发密钥。
- 容器镜像当前按 tag 固定（未 pin digest），与 core/rag 的 digest 策略不一致，已登记 TD-048。
