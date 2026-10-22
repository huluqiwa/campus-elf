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
| production | SaaS 运营环境（由运营方部署与运维） | Linux + Compose + Nginx + HTTPS；Langfuse 独立主机 | 密钥管理/环境变量 |

- 定位：本系统是**多租户 SaaS**，由运营方（我们）统一部署一套平台（SaaS 平台端 + 校方 B 端 + 教师/学生 C 端），**校方不部署任何组件**，只通过平台注册成为租户后使用；租户边界靠 tenant_id + RLS + 仓储过滤 + 缓存/对象存储/向量库租户前缀保障。

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
| `observability` | langfuse-web、langfuse-worker、langfuse-postgres、clickhouse、langfuse-redis、langfuse-minio | 开发 optional，独立 compose（`deploy/observability/`）；生产独立主机 |
| `tools` | k6、playwright（可选） | 压测/E2E |

> 开发机 16GB：**默认只启动 core**；`rag` 与 `parser` 按需分开启动，不要同时开启全部 profile；`observability` 默认关闭。生产 Langfuse 在独立主机。

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
| postgres | 1.5GB | shared_buffers 调优 |
| redis | 384MB | maxmemory 策略 |
| minio | 384MB | |
| qdrant | 1.5GB | 向量内存/磁盘 |
| backend | 1GB | 2–4 workers |
| worker | 1GB | 并发受 MinerU/CPU 限制 |
| clamav | 1.5GB | 病毒库；768MB 实测会 OOMKilled |
| tei-embed | 2.5GB | CPU；float32 + 小批量 |
| tei-rerank | 2.5GB | CPU；float32 + 小批量 |
| mineru | 3584m + GPU | GPU 单并发；M0-E 实测模型加载后约 3.2GB，显存 <500MiB |
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
    # 覆盖写入真实来源（不要用 $proxy_add_x_forwarded_for，否则客户端可伪造）
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_buffering off;
    proxy_read_timeout 300s;
}

location /api/v1/school/kb/build-jobs/ {
    proxy_pass http://backend:8000;
    proxy_http_version 1.1;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_buffering off;
    proxy_read_timeout 300s;
}
```

- **客户端 IP**：认证接口按来源 IP 限流/审计。应用默认只信任直连对端；部署在 Nginx 后时，在**所有** `location`（或 server 级）设置 `proxy_set_header X-Forwarded-For $remote_addr;`，并把 Nginx 地址填入 `TRUSTED_PROXY_IPS`。若改用 `$proxy_add_x_forwarded_for`，必须确保最左值不被客户端控制；否则限流可被伪造头绕过，或所有用户共用代理 IP 导致误封。
- HTTPS：Let's Encrypt 或自有证书；HTTP→HTTPS 跳转；HSTS。
- 上传大小：`client_max_body_size 300m`（按批）；个人库二期 30m。**注册接口路径**（`/api/v1/platform/tenants/register`）单独设 `client_max_body_size 11m`（应用层同样限制 11MB，含 chunked）。
- 限流：登录/API/SSE 分级；Redis 令牌桶为主。
- 安全头：CSP、X-Content-Type-Options、Referrer-Policy、X-Frame-Options。
- 访问日志脱敏；不记录 Authorization/Cookie。

---

## 6. MinerU GPU

- 镜像/依赖固定 4.0.12；自定义镜像 `deploy/mineru/Dockerfile`（`mineru[torch]`，非 root，`WORKDIR /data`）。
- 启动命令：`mineru-kit api-server --host 0.0.0.0 --port 8000 --upload-dir /data/uploads --concurrency 1`（`api-server` 是 `mineru-kit` 子命令，无独立可执行文件）。
- GPU 单并发（`--concurrency 1`）；队列由 Worker 控制；进度写入 DB。
- 模型源 `MINERU_MODEL_SOURCE=auto`（huggingface/modelscope 自动解析）；模型落 `mineru_data` 卷的 `/data/mineru/models` + `/data/hf`。实测 standard 档模型合计约 1.4GB（pipeline 小模型 227MB + VLM GGUF 1.2GB：主模型 531MB + mmproj 709MB）。
- VLM 引擎 `MINERU_MODEL_VLM_ENGINE=auto` → 本机解析为 llama-cpp **CPU 推理**；实测显存占用恒为 421–485MiB（远低于 6GB），代价是冷启动慢。
- 健康检查 `/v1/health` 只声明能力（版本/输出格式/来源），**不代表模型可用**；上线验证必须带一次真实 `/v1/file_parse`（TD-046）。
- 6GB 显存实测：standard/basic/flash 三档均可用（热态 1.9s / 2.0s / 0.9s），未触发降级；降级链 Standard→Basic→Flash 由 M3 执行器实现并验证。

### 6.1 后端切换：自建（llama-cpp/CPU）↔ 官方 vLLM/GPU

两者是**同一软件同一模型**（官方 `mineru` 包 + 官方权重），只是镜像基座与 VLM 推理后端不同，API 契约一致。切换只需改部署配置，**不改应用代码**：

| 需要改 | 位置 | 说明 |
|---|---|---|
| 镜像与启动命令 | `deploy/docker-compose.yml` / `deploy/mineru/Dockerfile` | 官方路径：entrypoint `mineru-kit` + `command: api-server ...`；我们的路径：完整 argv |
| 环境变量 | compose `environment` | 官方用 `MINERU_MODEL_SOURCE=local`（模型已打进镜像）+ `MINERU_MODEL_VLM_ENGINE=vllm`；我们用 `auto` + `auto→llama-cpp` |
| 模型与卷 | compose `volumes` | 官方镜像模型在镜像内；我们挂在 `mineru_data:/data`（`MINERU_HOME`/`HF_HOME`） |
| 资源 | compose `deploy.resources` / `mem_limit` | 官方 vLLM 需 **8GB+ 显存**；并发可上调（我们固定 `--concurrency 1`） |
| 安全姿态 | Dockerfile | 官方镜像以 root 运行；若要保持非 root 需自行加用户层 |
| 文档 | TD-043/TD-055、本文件 | 记录偏离与质量对照结论 |

应用侧只需满足三条（M3 落地时的实现约束，避免切换时改代码）：

1. 解析客户端只依赖 V1 HTTP API（`/v1/file_parse`、`/v1/tiers`、`/v1/health`），**base URL 走配置**（AI Gateway/环境变量），不硬编码容器名或端口。
2. **tier 从 `/v1/tiers` 读取**（或至少兼容 `advanced`），不把可用档位写死为 flash/basic/standard。
3. 超时、并发、重试与降级顺序都是配置项；降级链（Standard→Basic→Flash）由执行器按"实际可用档位 + 失败原因"决定，不假设特定后端。

> 反向切换同理：若生产为 CPU-only 或 6GB 档，直接用当前已验证的自建镜像，无需改动任何应用代码。

---

## 6.5 MinIO 与 TEI 的实际处理

- **MinIO**：官方 Docker Hub/Quay 仓库与 dl.min.io 二进制当前不可匿名获取；`deploy/minio/Dockerfile` 从官方源码构建固定 `RELEASE.2025-10-15T17-29-55Z`，不依赖 `latest`。该 RELEASE 为官方安全修复版本。
- **TEI 模型下载**：首次启动直连 HuggingFace 下载 BGE-M3 / reranker 权重（约 2.2GB+）。`hf-mirror.com` 不返回 ETag，会导致 TEI 报 `Header etag is missing`，因此当前使用直连；下载完成后缓存到 `tei_models` 卷。
- **Qdrant healthcheck**：镜像不含 curl/wget，使用容器自带 bash `/dev/tcp` 做 TCP 探活。
- **ClamAV**：首次启动更新病毒库并加载，需约 1.5GB 内存；健康检查通过前不得上传文件。

## 6.6 数据库连接与角色（M1-A）

- **三连接分离**（`docs/design.md` §4.6）：`DATABASE_URL`（租户侧应用，属 `campus_elf_tenant`）、
  `PLATFORM_DATABASE_URL`（平台侧应用，属 `campus_elf_platform`）、`MIGRATION_DATABASE_URL`（DDL/迁移，库属主）。
- **运行时必须是超级用以外的角色**：超级用户连 `FORCE ROW LEVEL SECURITY` 也会绕过，RLS 形同虚设；
  部署脚本按约定命名创建登录角色（`campus_elf_tenant_login` / `campus_elf_platform_login`，口令由密钥管理注入），
  迁移会自动把它们加入对应组角色。测试栈（`scripts/test-stack.sh up`）已自动化同样流程并导出 `TEST_APP_DATABASE_URL` / `TEST_PLATFORM_DATABASE_URL`。
- **迁移/运维脚本访问租户表的语义（重要）**：策略已按角色限定（`TO campus_elf_tenant|platform`），因此在
  `FORCE RLS` 下，**非超级用户且非这两组成员**的连接（例如普通的"库属主"DDL 角色）**匹配不到任何策略**：
  查询会静默返回 0 行、写入直接报 RLS 错误。因此 `MIGRATION_DATABASE_URL` 必须使用超级用户或
  具备 `BYPASSRLS` 的运维角色；临时数据修复脚本应显式 `SET ROLE campus_elf_platform`（或临时 `ALTER TABLE … NO FORCE`，事后恢复）。
- 角色与授权**不随迁移回滚**（集群级对象）；需要清理时手工 `DROP ROLE`（见 `scripts/test-stack.sh::cleanup_legacy_roles` 的做法）。
- **启动自检（TD-073）**：`APP_ENV=staging|production` 时应用启动会执行 `verify_runtime_database()`——**租户与平台两个连接**角色都不得是超级用户或带 `BYPASSRLS`；逐表要求 FORCE RLS、每条策略都引用租户/平台上下文且只授予两个应用组角色；审核函数 owner 必须可绕过 FORCE RLS；未设置租户上下文时租户数据必须不可见。任一不满足直接拒绝启动。
- **staging/production 配置校验强制项**：`DATABASE_URL`、`PLATFORM_DATABASE_URL`、`QDRANT_URL`、真实 `SMTP_HOST/SMTP_FROM` + `SMTP_USERNAME/SMTP_PASSWORD` + `SMTP_USE_TLS|SMTP_START_TLS`（二选一，MailHog/明文 SMTP 一律拒绝）、`JWT_SECRET`（≥32 字节随机值）、`ENCRYPTION_KEK`（标准 base64 的 32 字节随机值，`openssl rand -base64 32`；占位值/长度不符即启动失败）、`PASSWORD_RESET_URL_BASE`（https）、`AUTH_COOKIE_SECURE=true`；生产另强制 S3 四项；`DEV_*` 开关一律禁止。任一缺失/非法即启动失败。`MIGRATION_DATABASE_URL` **不注入 API/Worker**，仅迁移 Job 使用；该角色需超级用户或 `BYPASSRLS`。
- **MFA（AUTH-04，M1-D2）**：`ENCRYPTION_KEK` 用于加密 TOTP 密钥（AES-256-GCM，AAD 绑定 realm+user）与派生恢复码 HMAC 子密钥（HKDF），**所有环境必填且不入库、不入日志、不回显**；丢失该密钥等于全部 MFA 绑定失效（需按下方 CLI 逐账号重置），因此纳入密钥备份与轮换流程（轮换工具见 TD-125）。`MFA_REQUIRE_PLATFORM`（默认 `false`）**建议生产开启**：开启后平台运营账号必须绑定验证器才能登录，策略由关转开时旧会话在刷新时被撤销（已签发访问令牌最长 15 分钟自然收敛，TD-132），建议在低峰开启并公告。相关可调项：`MFA_TOTP_ISSUER`（默认 `CampusCompanionElf`）/`MFA_TOTP_INTERVAL_SECONDS`（30）/`MFA_TOTP_WINDOW_STEPS`（±1 步）、`MFA_CHALLENGE_TTL_SECONDS`（默认 300）、`MFA_CHALLENGE_MAX_ATTEMPTS`（默认 5）、`MFA_ENROLL_TOKEN_TTL_SECONDS`（默认 600）、`MFA_RECOVERY_CODE_COUNT`（默认 10）、`JWT_AUDIENCE_MFA`。待激活密钥只存 Redis（TTL 内有效），Redis 故障时绑定流程 fail-closed。
- **平台账号初始化（无注册 UI）**：部署后执行
  `docker compose exec api python -m app.platform_admin --account <账号> --email <邮箱>`，
  初始密码仅在终端打印一次，账号默认 `must_change_password=true`；已有账号改邮箱用
  `--update-email-only`（同事务作废未使用重置令牌）。请通过安全渠道交付初始密码，勿写入工单/日志。
- **平台账号 MFA 破窗（运营侧丢验证器）**：`docker compose exec api python -m app.platform_admin --account <账号> --reset-mfa`
  清除该账号的 TOTP 密钥/恢复码/失败计数与锁定，自增 `token_version` 并撤销全部刷新会话（`admin_revoke`），
  写审计 `platform.user.mfa_reset`（账号掩码、撤销会话数）；命令**不打印任何密钥或验证码**，可安全留存在运维记录中。
  需要同时更新邮箱可加 `--email <新邮箱>`。强制策略下该账号下次登录重新绑定。租户侧校管/员工重置按计划 M2 交付（TD-126）。
- **网关与响应体**：`/auth/mfa/enroll` 与 `/auth/mfa/activate` 的响应分别含 TOTP secret 与恢复码明文，均带
  `Cache-Control: no-store`；Nginx/网关**不得开启响应体日志或缓冲落盘**， access log 只记路径与状态（`$request_body` 类字段禁止配置到这两个路由）。
- **平台开通能力**：平台运行角色对 `users/roles/role_permissions/user_roles` 零权限；申请单仅 SELECT+INSERT、审核记录仅 SELECT、租户仅 SELECT + 列级 `UPDATE(status,status_reason)`。开通/审核只能 `EXECUTE review_registration_request(...)`（`SECURITY DEFINER`，函数内锁申请、写审核记录与审计）。该函数 owner 必须是超级用户或 `BYPASSRLS`（自检与迁移都会强制），否则写入会被 FORCE RLS 拒绝。

## 7. Langfuse 独立主机

- **归属**：由 SaaS 运营方自托管在**独立观测主机**上（不与应用主机共用），平台/运维专属访问；**校方不部署、不直接访问**。不使用 Langfuse Cloud（校园数据不出运营方控制的环境）。
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
| mineru | `/v1/health`（只声明能力；M0-E 实测缺模型时也可能返回 ok，**上线验证必须带一次真实 `/v1/file_parse`**，见 TD-046） |
| tei | `/health` |
| backend | `/health/live`、`/health/ready` |
| nginx | `/health` |

- 当前 compose 未使用 `depends_on`；健康检查用于探针/运维，不自动控制启动顺序。启动顺序由脚本/人工保证：DB/Redis/MinIO → Qdrant/TEI/MinerU → backend/worker → nginx（M0-C 起接入）。
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
# core（无 profile）→ rag → parser 分开启动，避免 16GB 同时全开
docker compose up -d
docker compose --profile rag up -d
# parser 需先构建 MinerU 镜像（M0-E 已验证：构建 → 启动 → /v1/health → 真实解析）
docker compose --profile parser build mineru
docker compose --profile parser up -d
# 4. 后端
cd backend && uv sync && uv run alembic upgrade head
uv run uvicorn app.main:app --reload
# 5. 前端
cd frontend && pnpm install && pnpm --filter admin dev
pnpm --filter campus dev
```

> **M0-B 实测状态**：core（PostgreSQL/Redis/MinIO/MailHog/ClamAV）与 rag（Qdrant/TEI embed/rerank）已全部 healthy；TEI embed 返回 1024 维向量，rerank 返回有效分数；MinIO 从官方源码构建固定 RELEASE。MinerU parser profile 尚未构建/验证，留到 M0-E。

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
- [ ] `ENCRYPTION_KEK` 已随机生成并纳入密钥备份/轮换记录；未出现在镜像、仓库、日志或 `.env` 提交中。
- [ ] 生产 `MFA_REQUIRE_PLATFORM=true` 已按公告在低峰开启；平台运营账号完成绑定；`--reset-mfa` 破窗流程与审计可演练。
- [ ] `/auth/mfa/enroll`、`/auth/mfa/activate` 响应未被网关日志/缓存记录（`no-store` 生效）。
