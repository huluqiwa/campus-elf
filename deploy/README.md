# 部署依赖栈（M0-B）

## 启动

```bash
# 1. 生成 Compose 密钥（deploy/.env 已被 git 忽略）
cd deploy
cp .env.example .env
# 不要用 change-me 启动；生成强随机值并写入 .env（不要打印到日志）
POSTGRES_PASSWORD=$(openssl rand -hex 24)
REDIS_PASSWORD=$(openssl rand -hex 24)
MINIO_ROOT_PASSWORD=$(openssl rand -hex 24)
# 将上述值填入 deploy/.env 的对应变量；或使用你自己的密钥管理方式

# 2. core：PostgreSQL / Redis / MinIO / MailHog / ClamAV
#    首次会从官方源码构建 MinIO（固定 RELEASE），需要几分钟
docker compose up -d

# 3. 加 RAG：Qdrant / TEI embedding / TEI reranker
docker compose --profile rag up -d

# 4. 加 MinerU（M0-E 才构建/验证；当前 parser profile 未验证）
docker compose --profile parser up -d
```

## 端口（仅 127.0.0.1）

| 服务 | 端口 |
|---|---|
| PostgreSQL | 5432 |
| Redis | 6379 |
| MinIO API/Console | 9000 / 9001 |
| MailHog SMTP/UI | 1025 / 8025 |
| ClamAV | 3310 |
| Qdrant REST/gRPC | 6333 / 6334 |
| TEI embed | 8081 |
| TEI rerank | 8082 |
| MinerU V1 API | 8100（容器 8000） |

## 常用命令

```bash
docker compose ps
docker compose logs -f postgres
docker compose --profile rag --profile parser ps
docker compose down
```

## 注意

- `.env` 已被 `.gitignore` 忽略；不要提交。
- 生产必须替换所有 dev 密码；建议使用密钥管理，不要把生产密码写入仓库。
- ClamAV 首次启动会更新病毒库，加载病毒库约需 1GB+ 内存；`mem_limit` 已设为 1536m，768m 会 OOMKilled。健康检查未通过前不要上传文件。
- TEI 首次启动会直连 HuggingFace 下载 BGE-M3 / reranker 模型（约 2.2GB+），耗时取决于网络；模型缓存在 `tei_models` 卷。`hf-mirror.com` 不返回 ETag，会导致 TEI 报 `Header etag is missing`，因此当前使用直连。
- MinIO 官方 Docker Hub/Quay/dl.min.io 当前不可匿名获取；`deploy/minio/Dockerfile` 从官方源码构建固定 `RELEASE.2025-10-15T17-29-55Z`。详见 `deploy/minio/README.md`。
- MinerU 镜像需要本地构建；在构建前 `parser` profile 不可用。详见 `deploy/mineru/README.md`。
- Langfuse 不在本文件；生产独立观测主机，开发 optional 在 M0-E 验证。
- 16GB 开发机不要同时启动全部 profile；按需 `--profile rag`、`--profile parser` 分开验证。
- 已验证：PostgreSQL 查询、Redis PING、MinIO 健康/未授权 403、Qdrant 建删集合、ClamAV 扫描、MailHog SMTP 投递、TEI embed 1024 维、TEI rerank 分数。
- TEI 冒烟：
  ```bash
  curl -fsS http://127.0.0.1:8081/health
  curl -fsS http://127.0.0.1:8082/health
  curl -fsS -X POST http://127.0.0.1:8081/embed -H 'Content-Type: application/json' -d '{"inputs":"奖学金申请条件"}'
  curl -fsS -X POST http://127.0.0.1:8082/rerank -H 'Content-Type: application/json' -d '{"query":"奖学金申请条件","texts":["奖学金申请条件包括成绩和综合素质测评。","图书馆本科生可借10册。"]}'
  ```
