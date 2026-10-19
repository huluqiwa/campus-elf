# 校园陪伴精灵 — 代码审查日志（review-log.md）

> 每个功能点完成后追加一条；没有审查记录的功能点不得标记完成。
> 详细清单见 `docs/code-review-checklist.md`。

| 功能点 | 日期 | commit/diff | 审查人/agent | Blocker | Major | Minor | 修复/复测 | 结论 |
|---|---|---|---|---|---|---|---|---|
| 文档阶段（非代码） | 2026-10-19 | docs/* | 待用户确认 | 0 | 0 | 0 | N/A | 已确认 |
| M0-A 后端最小骨架 | 2026-10-19 | feature/M0-foundation | 独立 Explore review agent | 0 | 1 | 12 | Major 已修复；低成本 Minor 已修；其余记录 | 已提交 |
| M0-B Compose 依赖栈 | 2026-10-19 | feature/M0-foundation | 独立 Explore review agent | 1 | 3 | 14 | Blocker/Major 已修；关键 Minor 已修；MinerU 留 M0-E | 已提交 |
| M0-C Harness 骨架 | 2026-10-19 | feature/M0-foundation | 两轮独立审查（第二轮补做） | 0 | 8+1 | 20 | 两轮 Major 已修；关键 Minor 已修；其余记录 | 已提交 |
| M0-D 前端脚手架 | 2026-10-19 | feature/M0-foundation | 独立 Explore review agent | 0 | 1 | 7 | Major 已修；关键 Minor 已修；其余记录 | 可提交 |

### M0-A 后端最小骨架

- 日期：2026-10-19
- 范围：`backend/pyproject.toml`、`app/config.py`、`app/core/logging.py`、`app/api/health.py`、`app/main.py`、`tests/test_health.py`、`backend/README.md`
- 审查发现：
  - **Major**：健康检查挂在 `/api/v1` 下，与 spec/部署契约 `/health/live|ready` 不一致，探针会 404。已修复为根路径挂载，并新增回归测试 `test_health_not_under_api_prefix`。
  - **Minor 已修**：配置 `env_file` 锚定项目根目录；日志级别显式校验 + `force=True`；健康响应 `Literal["ok"]`；`docs/redoc` 可按环境关闭；测试 fixture 类型、完整响应断言、根路径断言；README 同步；mypy 覆盖 `app tests`。
  - **Minor 记录/后续**：uvicorn/httpx 标准库日志暂未统一 JSON（M0-B 处理）；ready 依赖检查在 M0-B 接入；版本号重复、`extra="ignore"`、`ignore_missing_imports` 全局放宽、`create_app` 工厂化留待后续收紧。
- 复测：
  - `uv run ruff check .`：通过
  - `uv run pytest`：3 passed
  - `uv run mypy app tests`：通过
  - 健康检查：`/health/live`、`/health/ready` 200；`/api/v1/health/live` 404
- 结论：可提交。

## 记录模板

```markdown
### <功能点 ID / 名称>
- 日期：
- 设计简报：docs/design-briefs/<ID>.md
- commit/diff：
- 审查人/agent：
- 范围：
- Findings：
  - Blocker：
  - Major：
  - Minor：
- 修复：
- 复测：
  - 单元：
  - 集成：
  - 业务场景：
  - E2E：
  - AI 评估：
- 复审：
- 更好做法与 TD 处置：TD-xxx（已采纳/已延后/已拒绝/已完成）
- 结论：可提交 / 阻塞
```

### M0-B Compose 依赖栈

- 日期：2026-10-19
- 范围：`deploy/docker-compose.yml`、`deploy/.env.example`、`deploy/README.md`、`deploy/minio/Dockerfile`、`deploy/minio/README.md`、`deploy/mineru/README.md`、`deploy/initdb/README.md`、`docs/deployment.md`、`.gitignore`、根 `.env.example`
- 审查发现：
  - **Blocker**：MinIO 只有本地 tag，干净 checkout 无法启动。已修复：`deploy/minio/Dockerfile` 在构建阶段克隆官方固定 RELEASE，Compose 增加 `build`，`docker compose up -d` 可自动构建。
  - **Major**：MinIO 二进制版本未注入 ldflags，`minio --version` 显示 `DEVELOPMENT.GOGET`。已修复：注入 `Version/ReleaseTag/CommitID/ShortCommitID`；实测显示 `RELEASE.2025-10-15T17-29-55Z` 与 commit `9e49d5e7...`。
  - **Major**：`vendor/` 未被忽略，63MB 源码/嵌套 `.git` 可能入库。已修复：`.gitignore` 增加 `vendor/`。
  - **Major**：文档默认 `core+rag+parser` 与 16GB 资源上限矛盾。已修复：默认只开 core，rag/parser 分开；MinerU 3584m；移除不存在的 `--profile core`。
  - **Major**：MinerU 镜像/端点未验证。已明确标记为 M0-E 待验证，parser profile 不作为 M0-B 验收项。
  - **Minor 已修**：Postgres healthcheck 加 `-h 127.0.0.1` + `shm_size`；Qdrant 改 HTTP `/healthz` 200 校验；ClamAV `start_period` 360s；TEI `start_period` 600s 且固定模型 revision；Redis 增加 `maxmemory/policy`；根 `.env.example` 的 Redis URL/MinIO 变量命名对齐；部署文档去除 `depends_on` 漂移；补充 TEI 冒烟命令。
  - **Minor 记录/后续**：Redis 密码仍通过 argv 传入（后续用 config/secret）；MinIO 运行时复用 Go 镜像体积偏大（后续换最小镜像）；Postgres shared_buffers 未显式调优；MinerU 构建/端点 M0-E 验证。
- 复测：
  - `docker compose --profile rag config --quiet`：通过
  - 8 个服务全部 healthy：PostgreSQL/Redis/MinIO/MailHog/ClamAV/Qdrant/TEI embed/TEI rerank
  - PostgreSQL `select 1`、Redis `PONG`、MinIO `/minio/health/live` 200、Qdrant 建删集合、ClamAV 扫描 OK、MailHog SMTP 投递 1 封
  - TEI embed 返回 1024 维；TEI rerank 返回分数 0.997518
  - MinIO 以非 root 运行，版本正确
- 结论：可提交；MinerU 留 M0-E。

### M0-C Harness 骨架

- 日期：2026-10-19
- 范围：`backend/app/harness/{contracts,registry,tools,trace,audit,prompts,__init__}.py`、`backend/tests/harness/*.py`、`backend/pyproject.toml`
- 第一轮独立审查：0 Blocker / 8 Major / 14 Minor；Major 清单与修复：
  1. 工具策略可绕过 → `ToolExecutor` 强制校验 tenant_status、permission、side_effect、availability_flag、data_scope（含范围层级）。
  2. 无 schema 校验 → 用 `jsonschema` 校验 input/output schema，新增 `INVALID_ARGS`/`INVALID_RESULT`/`INVALID_SCHEMA`。
  3. spec 可变可绕过 → `ToolSpec`/`PromptSpec`/`AuditEvent` 冻结 + `extra=forbid`；注册默认拒绝写工具，`allow_write=True` 才可注册且执行层仍拒绝。
  4. trace/audit 可选 → `ToolExecutor` 构造参数改为必需；测试显式注入。
  5. `TenantContext` 缺状态/KB 版本 → 增加 `tenant_status`、`kb_version_ids`。
  6. 审计链可重算/截断/重放 → HMAC-SHA256 + 序号 + event_id + 深拷贝 + `head_hash`/`count` + `verify(expected_head, expected_count)`；测试覆盖篡改、重放、截断。
  7. 脱敏不完整、trace 未脱敏 → 扩展敏感 key 与值正则（邮箱/手机/证件/Bearer），trace span attributes 写入前脱敏。
  8. 测试缺口 → 扩展到 41 个用例，覆盖权限/写工具/可用性/租户状态/数据范围/schema/超时/异常/取消/缺失工具/审计篡改/重放/截断/脱敏/Prompt 严格变量/CRLF。
- 第二轮独立复审（余额恢复后补做）：8 个 Major 中 7 项已实质修复；新增 **F1 Major** 与 F2/F3 Minor。已修复：
  - **F1（Major）**：`InMemoryTraceSink.write_turn` 未脱敏嵌套 `spans[].attributes`，存在明文旁路。已修复为对嵌套 span attributes 递归脱敏，并新增断言。
  - **F2**：handler 返回非 `ToolResult` 时降级为 `TOOL_ERROR`；input/output schema 解析异常（含 `$ref`）统一降级为 `INVALID_SCHEMA`；两者都保证 trace/audit 落盘。
  - **F3**：`ToolRegistry.register` 用 `jsonschema.Draft202012Validator.check_schema` 校验 schema 合法性，非法直接 `ValueError`。
  - **F5**：`ToolRegistry.available`/`CapabilityRegistry.available` 增加 tenant_status 与 data_scope 过滤。
  - **F8**：`AuditEvent` 增加 `trace_id`，工具审计与 trace 建立关联。
  - **F9**：`PromptSpec` 增加 `sha256` 与 template 一致性校验。
  - **F10**：脱敏补充 15 位证件号、带空格手机号，以及 credential/session/otp/verify_code/student_no/employee_no 等 key。
- 剩余 Minor（M0-C 不阻塞，留后续）：
  - F4：`audit_level`/`idempotent`/`redacted_summary` 尚未被消费，声明与执行仍有漂移。
  - F6：`input_schema`/`output_schema` 默认空，未强制只读工具显式声明。
  - F7：`frozen=True` 对嵌套 dict（如 `feature_flags`）无效，安全上下文需改不可变映射。
  - F11：`AuditSink`/`TraceSink` 为同步接口，接 DB 时需评估 async/worker。
  - `verify()` 空链返回 True；生产需外部锚点/WORM 配合。
  - `PromptRegistry.versions` 按字符串排序；`E501` 仍 ignore；handler 自抛 TimeoutError 与 wait_for 超时未区分；trace/audit sink 故障未隔离；`CapabilitySpec.permission` 默认公开；`AgentRequest` 内嵌 TenantContext 需认证层注入；审计/trace 仍为内存实现。
- 复测：
  - `uv run pytest`：48 passed
  - `uv run ruff check .`：通过
  - `uv run mypy app tests`：通过
- 更好做法与 TD 处置：TD-015~TD-028 已登记 `docs/tech-debt.md`，其中 TD-027/028 已完成，TD-016/018/025/026 已采纳，TD-021 已拒绝（理由见台账），其余延后。
- 结论：两轮审查的 Major 均已修复，可提交；进入 M0-D 时把剩余 Minor 纳入技术债清单。

### M0-D 前端脚手架

- 日期：2026-10-19
- 范围：`frontend/**`（pnpm workspace、apps/admin、apps/campus、packages/api-client/ui/config、Playwright 配置与 e2e）、根 `.env.example` 前端段
- 审查发现：
  - **Major**：前端直连 `127.0.0.1:8000/health/live`，而后端无 CORS、Vite 代理只含 `/api`，浏览器下健康检查必然失败。已修复：Vite 代理增加 `/health`；前端默认 `VITE_API_BASE_URL` 留空走同源相对路径；新增 `frontend/.env.example`；根 `.env.example` 说明 Vite 不读根 `.env`。
  - **Minor 已修**：api-client 规范化尾斜杠 + 5s 超时；Element Plus 配置 zh-CN locale；Playwright `reuseExistingServer` 改为 `!CI`；README 代理/环境变量说明。
  - **Minor 记录/后续**：缺 ESLint/Prettier/lint 脚本；缺 Vitest；Element Plus 全量引入导致 admin JS 1.03MB、CSS 361KB；Vant 全量 CSS；e2e 只有静态渲染断言、无失败态/接口 mock；`playwright.config.ts`/`e2e` 未纳入 tsconfig；未跑 pnpm audit。
- 复测：
  - `pnpm install`：成功（esbuild 构建已批准）
  - `pnpm build`：admin/campus 均成功
  - `pnpm typecheck`：通过
  - `pnpm test:e2e`：2 passed（admin + campus，系统 Chrome + vite preview）
  - 代理行为：`/health` 转发到 127.0.0.1:8000；后端未启动时页面显示“后端未连接”，冒烟不受影响
- 更好做法与 TD 处置：TD-029~TD-038 已登记 `docs/tech-debt.md`，其中 TD-037/038 已完成，TD-029~031/033~036 已采纳，TD-032 延后。
- 结论：Major 已修，可提交；剩余 Minor 记入 M0-F/M1 技术债。

### M0-E 兼容性与外部组件验证

- 日期：2026-10-20
- 范围：`backend/pyproject.toml` + `app/config.py` + `app/workers/**` + `tests/**`（依赖分组、Arq、WorkerSettings）、`evals/**`（独立 uv 项目 + 冒烟脚本）、`deploy/mineru/**`、`deploy/observability/**`、`deploy/docker-compose.yml`（parser profile）、`docs/**`
- 审查方式：两个独立审查 agent（① 后端依赖/Arq/evals；② 部署/MinerU 容器/观测栈），第一轮 + 复审两轮；高风险项（容器安全、缓存路径、模型来源、队列命名落地）由审查者实机复核
- 第一轮发现与修复：
  - **Blocker（部署）**：compose 的 mineru `command` 是裸 `api-server`，而该入口不存在（是 `mineru-kit` 子命令）→ 容器 127 退出循环。已改为完整 argv，实机 `Cmd` 与 `RestartCount=0` 验证。
  - **Blocker（部署）**：`MINERU_MODEL_SOURCE=local` 但镜像不含模型 → 首次解析必失败。已改 `auto`（huggingface/modelscope），模型落 `mineru_data` 卷。
  - **Major（部署）**：`MINERU_MODEL_VLM_ENGINE=torch` 非法（合法值 auto/llama-cpp/vllm/lmdeploy/mlx），会让任何 mineru 命令在导入期崩溃；已改 `auto`，实测解析日志为 llama-cpp 引擎。
  - **Major（部署）**：模型缓存路径与卷不匹配（挂 `/root/.cache` 而实际 `/data/mineru`）→ 已统一到 `/data` 单卷。
  - **Major（后端）**：`test_arq_smoke` 无条件 `flushdb()`，URL 漏写 DB 段会静默落到业务库 DB 0 → 新增一次性 DB 护栏（DB≥10；反例 0/无段/5 全部被拒），去掉 flushdb，改唯一队列隔离 + `wait_for` 超时 + finally 清理 + 队列清空断言。
  - **Major（后端）**：Worker 直读 `os.environ` 且有硬编码无凭证回退 DSN → `Settings.redis_url` 改必填、`build_redis_settings` 从配置构造，缺失即 ValidationError（fail-fast）。
  - **Minor 已修**：jieba 过滤按类别生效（原带 module 写法无效）；evals 打包映射 `src/campus_elf_evals`；evals README 命令改 `cd evals && uv run python -m …`；观测栈敏感项全部 `${VAR:?}`、`DATABASE_URL` 由 `POSTGRES_*` 拼装、`pull_policy: never` + README 预构建 MinIO、`POSTGRES_VERSION` 默认 `17-alpine`、文件头注释改项目规则；MinerU 镜像非 root + `ipc: host` + ulimits、健康检查去 wget 回退、`start_period 300s`、显式 `--concurrency 1`、官方 vLLM 构建命令标注废弃。
  - **Minor（部署）**：`deploy/observability/.env(.example)` 的 `DATABASE_URL` 死配置已删除；`deploy/README.md`、`docs/deployment.md`、`docs/milestone.md`、根 `README.md` 的“未验证/自动降级”表述已按实测更正。
- 复审发现与修复：
  - **Major（后端，复审新增）**：`RedisSettings.default_queue_name` 在 arq 0.28 中不被 `create_pool`/`Worker` 读取，队列命名空间实际未生效且单测断言的是无效中间量（假绿）。已按 arq 真实契约重做：`WorkerSettings.queue_name`（arq CLI 传给 Worker）+ `create_producer_pool()`（`create_pool(default_queue_name=…)`）；单测断言 `create_worker(WorkerSettings).queue_name == "campus_elf:queue"`，集成用例改成“生产者不传 `_queue_name` → 消费者仍能取到任务”。
  - **Minor 已修**：`backend/tests/conftest.py` 提供 `REDIS_URL` 默认值，单元测试不再依赖开发者本机 `.env`；`evals/scripts/langfuse_smoke.py` 全部异常转可读 `FAIL`；ruff `src` 增补 `scripts`。
  - 复审结论：部署/观测栈侧**通过**（无 Blocker/Major 残留）；后端侧修复后重测通过。
- 复测证据：
  - `backend`：`uv lock --check` 通过；`ruff check app tests` 通过；`mypy app tests` strict 通过（31 files）；`TEST_REDIS_URL=redis://…:6379/15 uv run pytest` → **58 passed**（含真实 burst worker 集成）；未设 `TEST_REDIS_URL` 时集成用例 skip 而非 fail。
  - `evals`：`ruff check .` 通过；`pytest` → 2 passed。
  - MinerU：镜像构建成功（`mineru:4.0.12`，非 root）；`/v1/health` ok（version 4.0.12）；真实解析三档——standard 冷 248.1s（含模型下载）/热 1.9s、basic 2.0s、flash 0.9s，Markdown 内容正确；显存采样 12 次均 455–485 MiB，未降级。
  - Langfuse：观测栈从零冷启动（新项目名 + 必填项 fail-fast）全部 healthy，`{"status":"OK","version":"4.63.0"}`；`evals/scripts/langfuse_smoke.py` → `OK trace=c486f13c… observation=b9ad3d14…`（首次失败仅因 `langfuse` bucket 未建，已写入 README 前置步骤）。
- 更好做法与 TD 处置：TD-041（证据更正，延后 M3）、TD-042（完成）、TD-043（完成：自定义镜像 + 三档实测）、TD-044（已缓解，M6 收口）、TD-045（已采纳，M3 固定传递依赖）、TD-046（完成：健康检查≠可用）、TD-047（已采纳，M0-F CI）、TD-048/049（已采纳/延后，M6）；新增 TD-050（已拒绝）、TD-051（已采纳，M1 功能探针）、TD-052（延后 M0-F）、TD-053（延后 M3，镜像体积）。
- 结论：两轮审查的 Blocker/Major 均已修复并复测通过，可提交；剩余 Minor 已登记 TD。

### M0-F 测试基础设施与 M0 收口

- 日期：2026-10-20
- 范围：`backend/app/{api,core,harness}`（`/health/ready` 探活、structlog 统一 JSON、空壳 Loop SSE、配置校验、sink 容量）、`backend/tests/**`（测试栈/Fixtures/业务场景 BS-M0-01）、`scripts/{ci.sh,test-stack.sh,check_eval_cases.py}`、`.github/workflows/**`、`.gitleaks.toml`、`frontend/**`（ESLint/Prettier/Vitest/按需引入/e2e mock/tsconfig）、`benchmarks/**`、`deploy/{docker-compose.test.yml,tei/prefetch.py}`、`docs/m0-acceptance.md` 等
- 审查方式：两个独立审查 agent（① 后端/CI；② 前端/脚本与基准），第一轮 + 复审两轮；审查者对关键结论均实机复现（含反例）
- 第一轮（①）1 Blocker + 4 Major：
  - **Blocker**：production 下 `DEV_SKELETON_LOOP_ENABLED=true` 曾被放行 → 未鉴权的空壳入口可上线。修复：`Settings` 生产校验新增 fail-fast（`test_production_rejects_skeleton_loop_switch`）。
  - **Major**：`/health/ready` 遇非法 Redis DSN 抛 500 而非 503（`from_url` 在 try 外）；生产启动报错回显 `REDIS_URL`（含口令）→ 启用 `hide_input_in_errors=True` 并补测试；空壳 Loop 在客户端断开/生成失败时不落 trace、不发 `error` 事件 → 生成器三分支收尾（`CLIENT_CANCELLED` / `STREAM_ERROR` / 正常），并补中断路径测试；评估用例集门禁口径弱于 README（PI/CT 对抗未按 20 校验、缺类别不报错）→ 固定类别清单与逐类下限。
  - **Minor 已修**：验收报告数字/台账错列、日志入口顺序约束写进 backend README、`_trace_sink` 缺失改为显式失败、内存 sink 加容量上限、业务场景复用一次性 DB 护栏、`ci.sh` 与 CI 步骤对齐（`uv sync --frozen`、`--with-e2e`、去掉重复 `-q`）、`.env.example` 补开关、gitleaks 白名单收窄、SSE 字段对齐 `design.md` §5.8。
- 第一轮（②）2 Major：
  - **Major**：`scripts/test-stack.sh` 的 `wait_healthy` 会把 `compose ps` 报错/空输出判成 healthy（假绿）；**复审发现该修复实际未落地**（脚本化替换静默未匹配），已重新实现并用两个负例复测：期望容器数不足 9 → 超时失败；compose 文件不存在 → 60 次重试提示 + 失败。
  - **Major**：Prettier 门禁未接入且当时红灯（与 TD-029「已完成」矛盾）→ `.prettierrc` 改 `endOfLine: auto`、生成物入 `.prettierignore`、全量格式化、`format:check` 进 `frontend.yml` 与 `ci.sh`。
  - **Minor 已修**：测试栈 redis 改 tmpfs + 关闭持久化（不再产生匿名卷）、提示走 stderr、`print_env` 读取实际 user/db、`up-all` 前置镜像检查、单实例约束文档化、删除冗余 `preview.proxy`（Vite 7 继承 `server.proxy`）、e2e 失败产物路径改 `test-results/`、prefetch 本地模式改 `cache_dir`、基准文档措辞收敛、e2e 悬挂用例窗口 15s→20s、`vite.config` 注释注明测量条件。
- 第二轮（①）：**通过**。剩余 Minor 已处理：验收报告 TD 表按台账重排（TD-043 → 已采纳、补 TD-044/048）、台账评审行纳入 TD-061~063、`conversation_id` 在 meta 与 trace 间改为同一解析值（非 UUID 由服务端另分配）、sink 容量补单测、keep-alive 头加语义注释、`ci.sh` 差异说明补 `pnpm install`、本日志补记。
- 第二轮（②）：**有条件通过**（唯一 Major 即上面的 `wait_healthy` 实际未修），修复后复测通过。
- 复测证据（最终）：
  - `cd backend && uv run ruff check app tests`、`uv run mypy app tests`（44 文件）全绿；
  - `eval "$(bash scripts/test-stack.sh env)"` 后 `uv run pytest` → **86 passed**（单元 82 + 业务场景 3 + 集成 1；连续三轮稳定）；
  - 反例：production + 开关 → ValidationError；错误文本不含口令；`REDIS_URL=127.0.0.1:56379` → 503 + `detail=ValueError`；DB 0/无 DB 段/5 → 护栏拒绝；
  - 空壳 Loop：契约字段（meta/step/done/error）与中断、错误、缺失 sink 三条路径均有实测；
  - `bash scripts/ci.sh --with-e2e` → 除本机未装 gitleaks（SKIP）外全部 PASS（含 bandit/pip-audit 双项目、Prettier、Vitest、Playwright 5 passed）；
  - 前端：`pnpm lint/format:check/typecheck/test:unit/test:e2e` 全绿；admin JS 1.03MB→112KB、CSS 361KB→8.94KB。
- 更好做法与 TD 处置：TD-002/005/010/029~035/047/052/058 已完成；新增 TD-056（CPU 重排延迟，M3/M4）、TD-057（CI 待远端生效，M1）、TD-059（空壳 Loop 认证，M4）、TD-060（ragas/diskcache 无修复版本，监控）、TD-061（生产未强制关 /docs，M6）、TD-062（ready 探活连接池化，M1）、TD-063（Accept 校验与可注入超时，M4）。
- 结论：两轮审查的 Blocker/Major 全部修复并复测通过，可提交；M0 验收见 `docs/m0-acceptance.md`。

## 高风险功能复审记录

| 功能点 | 复审人 | 日期 | 结论 |
|---|---|---|---|
| M1 认证/令牌 | 用户 | 待定 | 待定 |
| M1 租户隔离/RLS | 用户 | 待定 | 待定 |
| M3 KB 审批/发布 | 用户 | 待定 | 待定 |
| M4 AI 护栏/SSE | 用户 | 待定 | 待定 |
| M5 账号删除状态机 | 用户 | 待定 | 待定 |
| M6 配置双人确认 | 用户 | 待定 | 待定 |
| M6 Langfuse 外送/脱敏 | 用户 | 待定 | 待定 |
