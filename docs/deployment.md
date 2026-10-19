# 校园陪伴精灵 — 部署方案（deployment.md）

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-10-19 |
| 状态 | 待用户确认；M0 实施 |
| 目标 | Linux + Docker Compose + Nginx + HTTPS；无 K8s |
| 观测 | Langfuse Server 4.63.0 + Python SDK 5.0.2，独立主机自托管 |

---

## 1. 环境

| 环境 | 用途 | 配置/数据 | 密钥 |
|---|---|---|---|
| development | 本机开发 | 本地 Compose；MailHog；Langfuse optional | `.env` 本地，不入库 |
| test | 自动化/集成 | 独立 Compose project；独立 DB/Redis/MinIO/Qdrant | 独立测试密钥 |
| production | 交付 | Linux + Compose + Nginx + HTTPS；Langfuse 独立主机 | 密钥管理/环境变量 |

- 三套环境配置、数据库、密钥严格隔离。
- 生产 `DEV_SKIP_SCHOOL_REVIEW=false`、`DEV_SKIP_FILE_SCAN=false`，启动时校验。
- 测试数据用后即清。

---

## 2. Compose 分层

| Profile | 服务 | 说明 |
|---|---|---|
| `core` | postgres、redis、minio、mailhog（dev）、clamav、backend、worker、frontend/nginx | 应用基础 |
| `rag` | qdrant、tei-embed、tei-rerank | 向量/嵌入/重排 |
| `parser` | mineru（GPU） | MinerU 4.0.12 V1 API |
| `observability` | langfuse-web、langfuse-worker、langfuse-postgres、clickhouse、langfuse-redis、langfuse-minio | 开发 optional；生产独立主机 |
| `tools` | k6、playwright（可选） | 压测/E2E |

> 开发机 16GB：默认 `core+rag+parser`；`observability` 默认关闭。生产 Langfuse 在独立主机。

---

## 3. 服务与端口（默认）

| 服务 | 端口 | 说明 |
|---|---|---|
| Nginx | 80/443 | 反代 admin/campus/API/SSE |
| backend | 8000 | FastAPI |
| worker | — | Arq Worker |
| postgres | 5432 | 仅内网/绑定 127.0.0.1 |
| redis | 6379 | 仅内网 |
| minio | 9000/9001 | 对象存储/控制台 |
| qdrant | 6333/6334 | REST/gRPC |
| tei-embed | 8081 | 嵌入 |
| tei-rerank | 8082 | 重排 |
| mineru | 8100 | V1 API |
| clamav | 3310 | clamd |
| mailhog | 1025/8025 | 开发邮件 |
| langfuse | 3000 | 独立主机/开发 optional |

- 所有依赖服务绑定 `127.0.0.1` 或 Docker 内网；不直接暴露公网。
- 仅 Nginx 暴露 80/443。

---

## 4. 资源限额（建议）

| 服务 | 内存限额 | 说明 |
|---|---|---|
| postgres | 2GB | shared_buffers 调优 |
| redis | 512MB | maxmemory 策略 |
| minio | 512MB | |
| qdrant | 2GB | 向量内存/磁盘 |
| backend | 1GB | 2–4 workers |
| worker | 1GB | 并发受 MinerU/CPU 限制 |
| clamav | 1GB | 病毒库 |
| tei-embed | 1.5GB | CPU |
| tei-rerank | 1.5GB | CPU |
| mineru | 4GB + GPU | GPU 单并发 |
| nginx | 256MB | |
| langfuse（独立主机） | 按官方 | 不与应用共用 |

- 总内存按 16GB 开发机校准；生产按服务器规格调整。
- GPU：MinerU 独占 6GB；TEI CPU，不争显存。

---

## 5. Nginx 关键配置

```nginx
# SSE 不缓冲
proxy_buffering off;
proxy_cache off;
proxy_read_timeout 300s;
add_header X-Accel-Buffering no;

location /api/v1/campus/chat/ {
    proxy_pass http://backend:8000;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_read_timeout 300s;
}

location /api/v1/school/kb/build-jobs/ {
    proxy_pass http://backend:8000;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 300s;
}
```

- HTTPS：Let's Encrypt 或自有证书；HTTP→HTTPS 跳转；HSTS。
- 上传大小：`client_max_body_size 300m`（按批）；个人库二期 30m。
- 限流：登录/API/SSE 分级；Redis 令牌桶为主。
- 安全头：CSP、X-Content-Type-Options、Referrer-Policy、X-Frame-Options。
- 访问日志脱敏；不记录 Authorization/Cookie。

---

## 6. MinerU GPU

- 镜像/依赖固定 4.0.12；`mineru[torch]`。
- GPU 单并发；队列由 Worker 控制；进度写入 DB。
- 模型缓存卷 `mineru-data/`；首次启动下载模型需预置/离线包。
- 健康检查 `/v1/health`；失败重试/死信；tier 降级记录。
- 6GB 显存：Standard→Basic→Flash；若 Standard 降级率过高，默认 Basic 并报告质量差异。

---

## 7. Langfuse 独立主机

- 独立主机/VM；官方 self-hosting：Langfuse Web/Worker + PostgreSQL + ClickHouse + Redis + S3/MinIO。
- 版本锁定：**Server 4.63.0 + Python SDK 5.0.2**；镜像 `langfuse/langfuse:4.63.0`、`langfuse/worker:4.63.0`；不使用 `latest`。
- Server ≥4.59.0 才支持 Python SDK v5；v4 使用 OTel 接入，读取用 Observations API v2 / Metrics API v2 / Scores API v3。
- Compose 定位为单 VM/测试；校内单机可用但无 HA；高可用改用 Helm/K8s 或托管。
- 实时入库可选：Langfuse 容器环境变量 `LANGFUSE_MIGRATION_V4_NATIVE_OTEL_BEHAVIOUR=direct`；M0 验证延迟/吞吐。
- 备份；监控；仅平台/运维访问。
- 应用主机通过内网/HTTPS 上报；默认元数据+哈希；场景内容采集白名单。
- 开发 optional profile 默认关闭；不影响核心启动。

---

## 8. 健康检查与启动顺序

| 服务 | 检查 |
|---|---|
| postgres | `pg_isready` |
| redis | `redis-cli ping` |
| minio | `/minio/health/live` |
| qdrant | `/healthz` |
| mineru | `/v1/health` |
| tei | `/health` |
| backend | `/health/live`、`/health/ready` |
| nginx | `/health` |

- `depends_on: condition: service_healthy`；启动顺序：DB/Redis/MinIO → Qdrant/TEI/MinerU → backend/worker → nginx。
- 失败重启策略 `unless-stopped`；日志轮转。

---

## 9. 备份与恢复

- PostgreSQL：每日全量 + WAL 归档（可行时）；保留 180 天；加密；异地。
- MinIO：版本/对象备份；Qdrant：快照；配置：Git + 加密备份。
- 恢复演练月度；RPO≤24h（WAL≤15min）、RTO≤4h。
- 账号删除后备份 180 天内到期；除灾难恢复不得回灌；恢复后重新执行删除/匿名化。
- 删除证明注明备份到期。

---

## 10. CI/CD

- GitHub Actions：lint、security、unit、integration、scenario、e2e、build。
- 镜像固定 tag/digest；构建后扫描。
- 部署：手动审批 → 拉取镜像 → `docker compose up -d` → 健康检查 → 冒烟。
- 回滚：上一镜像 tag + 数据库迁移兼容；不自动回滚数据。
- 生产密钥通过环境变量/密钥管理注入；不入镜像/仓库。

---

## 11. 本地开发（M0 后）

```bash
# 1. 启动 Docker Desktop
# 2. 复制配置
cp .env.example .env
# 3. 启动 core + rag + parser
docker compose --profile core --profile rag --profile parser up -d
# 4. 后端
cd backend && uv sync && uv run alembic upgrade head
uv run uvicorn app.main:app --reload
# 5. 前端
cd frontend && pnpm install && pnpm --filter admin dev
pnpm --filter campus dev
```

> 以上命令在 M0 落地后生效；当前仅文档阶段。

---

## 12. 生产检查清单

- [ ] 生产 `DEV_SKIP_*` 全 false；启动校验通过。
- [ ] 密钥不入镜像/仓库/日志；轮换流程可用。
- [ ] PostgreSQL/Redis/MinIO/Qdrant 不暴露公网；仅 Nginx 443。
- [ ] HTTPS 证书有效；HSTS；安全头。
- [ ] SSE 不缓冲；心跳/超时正确。
- [ ] MinerU GPU 健康；tier 降级可观测。
- [ ] Langfuse 独立主机/访问控制/脱敏/留存。
- [ ] 备份/恢复演练通过；RPO/RTO 达标。
- [ ] 审计 WORM/哈希链校验。
- [ ] 资源限额/日志轮转/监控告警。
- [ ] 跨租隔离测试全量通过。
- [ ] 压测报告入库；发布/回滚零 5xx。
- [ ] 生产开关/功能开关默认关闭。
