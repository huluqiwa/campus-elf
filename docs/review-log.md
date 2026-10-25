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

### M0-F 收尾：远端 CI 首次运行修复（2026-10-20）

- 背景：仓库推送 `huluqiwa/campus-elf`（SSH）后，4 条工作流首次运行**全部失败**（16–53 秒，属"起步即错"而非测试失败）。用 `gh run view <id> --log-failed` 取回真实报错。
- 发现与修复：
  1. **backend**：`GITHUB_ENV` 不会剥离引号，`test-stack.sh env` 输出的 `export K="值"` 被原样写入环境变量，导致 `TEST_REDIS_URL` 的字面量带 `"`，被一次性 DB 护栏（DB>=10）判非法。→ 新增 `env-plain` 子命令（无 export/无引号）供 CI 使用，`env` 保留给本地 `eval`。
  2. **evals**：用例集校验脚本路径多一层（`../../scripts` → `../scripts`），python 退出码 2。
  3. **frontend**：pnpm 11.28 改为 `allowBuilds` 映射，`onlyBuiltDependencies` 不再生效，CI 下 `ERR_PNPM_IGNORED_BUILDS: esbuild@0.28.2` 直接失败。→ 用官方 `pnpm approve-builds --all` 生成正确配置；本地以 `CI=true pnpm install --frozen-lockfile` 干净安装复现并验证。
  4. **security**：gitleaks-action 在仓库首次推送时对根提交执行 `<sha>^..<sha>`，报 `unknown revision` 误失败。→ 改为固定版本二进制（8.24.3）全历史扫描（同时移除一个第三方 action 依赖）；本地 Docker 实跑发现 2 处历史提交中的测试假值 `test-secret-key-123456`，已在白名单按精确字符串放行。
- 顺带优化：4 条工作流加**路径过滤**（只改前端就只跑前端），security 改为「推送 main + 每周一 03:00 + 手动」，把私有仓库 Actions 额度按需消耗。
- 复测证据（2026-10-20）：backend 46s ✅、evals 22s ✅、security 40s ✅、frontend（frontend+e2e）4m45s ✅；`gitleaks` 本地全历史扫描 14 提交/1.23MB「no leaks found」；后端 86 passed（env-plain 导出方式下）。
- TD 处置：TD-057（CI 待远端生效）→ 已完成；新增 TD-064（e2e 安装 Chrome 约 4.5 分钟，可缓存/限触发，M1）。

### M1-B 学校注册与审核：第一轮独立审查（2026-10-20）

- 审查范围：M1-B 未提交改动（迁移 `c688c44dec81`、开通服务、平台接口、测试）。
- 结论：**不通过**（1 Blocker + 14 Major + 16 Minor）。
- Blocker：平台角色可向**存量租户**插入 users/roles/role_permissions/user_roles（`platform_provision` 只校验平台 GUC，不绑定新租户）——实测插入后租户会话能读回后门超管。
- Major：校方超管拿到 `platform.*`；`login_id/display_name` 列宽不足导致审核 500；邮箱校验过弱（双 @/CRLF）；邮件失败留下已激活无密码租户；生产未强制平台/迁移连接与 SMTP；密钥明文；令牌非恒定时间比较；审计含明文邮箱；审计 `request_id` 全空；审核记录可改/删；先写对象后查重产生孤儿且无请求体上限；downgrade 遇 `school_admin` 数据失败；TD-073 自检缺失；失败路径无测试。
- 修复：见 `docs/design-briefs/M1-B-tenant-onboarding.md` §9.1（B1 改为 `SECURITY DEFINER provision_tenant()`；M1–M14 全部处置；Minor 魔数/对象 key/bcrypt/文档编号修复，其余登记 TD-083~089）。
- 复测：`ruff` 全绿、`mypy --no-incremental` 86 文件无问题、后端 **178 passed**（含 10 条新增回归）。
- 第二轮复审：待执行（CI 通过后）。

### M1-B 学校注册与审核：第二轮独立审查（2026-10-20）

- 结论：**不通过**（1 Blocker + 8 Major + 14 Minor）。
- Blocker B2：启动自检只检查租户连接、用全局 RLS 计数、空表 count=0 误判——平台连接可为超级用户、`users` 关闭 FORCE RLS 仍放行。
- Major：平台可直接改申请状态/伪造审核记录且能直接调用内部分通函数创建无审计租户；`tenants` 完整 DML；邮件已发但提交失败的反向窗口；校名 CRLF 导致申请永久 502；chunked 请求绕过体积限制；原地修改 revision 导致旧 schema 不生效；运行时强制 DDL 超级用户 DSN；SECURITY DEFINER owner 语义未强制；关键失败路径缺测试。
- 修复：见 `docs/design-briefs/M1-B-tenant-onboarding.md` §9.2（审核收进 `review_registration_request()`、平台最小权限、硬化 revision `d9f4a2b7c3e1`、自检覆盖双连接/逐表/owner、chunked 缓冲限制、控制字符、运行时移除迁移 DSN、新增 10 条失败路径测试）。
- 复测：`ruff` 全绿、`mypy` 86 文件无问题、后端 **186 passed**、`alembic check` 无漂移。
- 第三轮复审：待执行（CI 通过后）。

### M1-B 学校注册与审核：第三轮独立审查（2026-10-21）

- 结论：**不通过**（1 Blocker + 4 Major + 14 Minor）。
- Blocker B3：`review_registration_request` 内表名未限定 schema，平台会话可用同名 TEMP 表遮蔽——伪造申请数据创建"影子租户"，真实申请保持 pending 却已产生审核记录与审计。
- Major：自检 `bool_or` 判定被 `USING (true)` 宽松策略绕过；旧 schema（无 provision_tenant）升级后 downgrade 仍失败且测试未真实模拟第一轮；d9 downgrade 重新授予 users/roles 全量 DML（超出目标 revision）；全局 11MB 体积限制会阻断后续 KB 大上传。
- 修复：见 `docs/design-briefs/M1-B-tenant-onboarding.md` §9.3（函数内全部 `public.` 限定 + 遮蔽回归测试；自检要求所有策略合规 + 宽松策略测试；c688 downgrade 幂等化 + 第一轮 schema 往返测试；downgrade 精确恢复 c688 权限；体积限制改按路由 + 透传测试；失败审计断言等 Minor 修复）。
- 复测：`ruff`/`mypy` 全绿、后端 **189 passed**。
- 第四轮复审：待执行（CI 通过后）。

### M1-B 学校注册与审核：第四轮独立审查（2026-10-21）

- 结论：**不通过**（2 Blocker + 2 Major + 7 Minor）。
- Blocker B4：旧 schema 升级时 `CREATE OR REPLACE provision_tenant` 新建函数默认授予 PUBLIC EXECUTE（预撤销在创建之前被跳过），平台仍可无审计创建租户/超管。
- Blocker B5：`d9` downgrade 重建身份表 INSERT + `platform_provision` 策略，恢复 B1 攻击路径，且与 c688 规范状态不符。
- Major：第四轮修复仍写在已应用的 `d9` 内（旧 d9 环境不生效）；回归测试未覆盖上述两条路径。
- 修复：见 `docs/design-briefs/M1-B-tenant-onboarding.md` §9.4（撤销移到创建之后并无条件执行；downgrade 只还原 c688 规范状态；新增 `e5c9d1f7a2b4` 幂等重放 revision；测试补函数 ACL 与越权攻击断言）。
- 复测：`ruff`/`mypy` 全绿、后端 **189 passed**、head=`e5c9d1f7a2b4`。
- 第五轮复审：待执行（CI 通过后）。

### M1-B 学校注册与审核：第五轮独立审查（2026-10-21）——**通过**

- 结论：**通过**（0 Blocker / 0 Major / 7 Minor）。
- 验证：B4（旧 schema 升级后 `provision_tenant` 对 PUBLIC/平台不可执行，ACL `{campus_elf_test=X/...}`，平台调用抛 InsufficientPrivilegeError）；B5（`upgrade head → downgrade c688` 后平台向存量租户插 `school_admin` 被拒，`platform_provision=0`）；M28（`e5c9d1f7a2b4` 幂等重放，已应用 d9 的环境获得修复，downgrade/upgrade 往返正常）；M29（测试覆盖函数 ACL、平台调用拒绝、降级权限矩阵）。
- Minor 收口：e5 增加 `_require_bypass_role`/触发器/列宽/tenants 授权幂等兜底与触发器函数 PUBLIC 撤销；迁移测试补租户角色执行拒绝、越权插入置于同一事务；文档补"downgrade d9 不回退 hardening"说明与 TD-095。
- 审查证据：`ruff`/`mypy` 全绿、**189 passed**、`bash scripts/ci.sh` 全绿、`alembic check` 无漂移、head=`e5c9d1f7a2b4`。

### M1-C 三端登录与令牌：独立审查两轮（2026-10-21）

- 第一轮：**不通过**（1 Blocker + 5 Major + 13 Minor）。Blocker B1：平台路由只校验权限码不校验 realm，而 `permissions` 是全局目录、数据库层不阻止租户角色引用 `platform.*` → 实测租户令牌可读全部租户列表；Major：登出不校验刷新令牌 secret 且审计主体错误、>72 字节口令 500、未知 tenant_id 刷新 500、登录响应缺 spec 要求的 tenant_id/角色、JWT 占位值校验过窄（示例配置重复键覆盖原防线）。
- 第一轮修复：应用层 `realm="platform"` 断言 + 数据库守卫触发器（`b7d1e3f5a9c2`）+ 自检矩阵校验 + B1 回归测试；登出按哈希校验并记真实操作者；口令按字节拦截（登录 401/改密 422）；未知租户失败审计降级平台事件；`TokenResponse` 补 `tenant_id/roles`；占位值改子串匹配、示例配置去重置空；13 条 Minor 全修（撤销键 realm/前缀/超时、门禁精确路径+库值判定、反账号枚举等时比对、登出必审计、审计带 ip/ua、家族撤销独立提交、回滚归一化 `rotated`、自检覆盖注册函数）。
- 第二轮：**不通过**（0 Blocker + 1 Major + 9 Minor）。Major M1：新增矩阵自检在无租户上下文下统计 RLS 表恒为 0（空实现）。
- 第二轮修复：自检改逐租户上下文统计 + "植入越界映射必须拒绝启动"用例；Redis 超时真正传入；staging 也强制强密钥；平台方向守卫与种子矛盾 → `c8e2f4a6b1d3` 移除并写明约束；补齐剩余审计 ip/ua；修掉恒真断言、并发终态断言、测试残留清理、嵌套连接。
- 复测：`ruff`/`mypy`（95 文件）全绿、后端 **222 passed**、`alembic check` 无漂移、head=`c8e2f4a6b1d3`、本地 `scripts/ci.sh` 全绿。
- 第三轮（聚焦复审）：**通过**（0 Blocker / 0 Major / 3 Minor）。M1 实测闭环：空租户库通过、第二租户植入越界映射后拒绝启动（错误信息含租户 id）、清理后恢复通过；迁移链 `f1a2→a3f5→b7d1→c8e2` 三次往返幂等、平台方向守卫按设计移除后租户方向仍有效。
- 第三轮 Minor 收口：自检去掉未使用形参（可读性）、补 staging/占位值密钥测试（参数化三态）；启动自检逐租户串行开会话的容量前瞻登记 TD-109。
- 最终门禁：222 passed、ruff/mypy 全绿、`alembic check` 无漂移、head=`c8e2f4a6b1d3`、本地 `scripts/ci.sh` 全绿。

### M1-C-UI 三端登录页与强制改密页：实现完成，待独立审查（2026-10-21）

- 实现：后端 HttpOnly 刷新 Cookie（staging/production 强制 secure）+ api-client 认证会话（401 单飞刷新/403 分流）+ admin/campus 登录与改密页、路由守卫；E2E 9 条（接口 mock）与 api-client 13 条单测全绿；后端全量 236 passed。
- 第一轮独立审查：**不通过**（1 Blocker + 4 Major + 11 Minor）。Blocker：改密请求未带 Authorization，首登改密必然 401；Major：改密未换 Cookie（改密后刷新即掉线）、登出在 access 过期时不清 Cookie（共享设备"退出"无效且家族未撤销）、响应体仍回显 refresh 令牌（抵消 HttpOnly）、campus 的 E2E 未被 Playwright 收集（文档误报覆盖）。
- 修复：见 `docs/design-briefs/M1-C-UI-login-pages.md` §8 的修复清单（含新增回归：改密带鉴权头、仅 Cookie 登出、改密换 Cookie、X-Auth-Error 分流、有状态 mock、E2E 用例数下限守卫）。
- 复测：api-client **20 passed**、Playwright **12 passed**、后端全量 **238 passed**、lint/format/typecheck/build 全绿。
- 第二轮：**通过**（0 Blocker / 0 Major / 6 Minor）。实测确认：改密带 Bearer 且下发新 Cookie、仅凭 Cookie 可刷新；登出四种凭证形态（有效 Bearer / 仅 Cookie / 伪造 Bearer + 有效 Cookie / 无凭证）都安全且清 Cookie、不误撤销他人；staging 实测响应体不含 refresh 且 Cookie 带 Secure；E2E 收集 12 条（含 campus-login）并有用例数下限守卫。
- 远端 CI：frontend 工作流修了两处自身缺陷（守卫步骤的 YAML 标量、testMatch 被仓库路径误命中），最终四条工作流全绿。
- 第二轮 Minor 收口：改密接入 401 刷新重放；设计文档与 `.env.example` 补 `AUTH_REFRESH_TOKEN_IN_BODY`；登出 E2E 去掉重复 route 并补拦截断言；有状态 mock 的 must_change_password/tenant_id 与后端对齐；新增 `toUserMessage` 统一网络错误兜底；去掉 `OptionalPrincipal` 冗余默认值。

### M1-C2 会话设备管理：独立审查（2026-10-21~22）

- 交付：`sid` 访问令牌 + Redis 会话标记（被踢下一次请求 401）；租户 3 / 平台 2 台设备上限、超限踢最久未用；`GET /auth/sessions`、`DELETE /auth/sessions/{id}`、`POST /auth/sessions/revoke-others`；迁移 `d4e6f8a0b2c1`；审计 `auth.session.kicked/revoked/revoked_others/revoke_denied`。
- 第一轮：**不通过**（1 Blocker + 4 Major + 3 Minor）。Blocker：会话标记在事务提交后补写，与并发撤销交错时会把已踢会话复活；Major：条件更新失败后误报 reuse、无刷新令牌登出未撤销 DB family、迁移 downgrade 约束不可往返、租户 SQL 缺显式纵深过滤。修复：锁后重读、标记移入事务、family 级撤销判定、Bearer sid 兜底撤销、downgrade 原因归一化、SQL 全部带 `tenant_id`。
- 第二轮（聚焦复审）：**不通过**（0 Blocker + 3 Major + 2 Minor）。Major：Redis 故障时 Bearer-only 登出 500 且未撤销 family；设备被踢后重放已 rotated 旧令牌误报 reuse；reuse 家族撤销未取锁/未删会话标记；另修复 Bearer+刷新令牌重复审计。
- 第三轮：**不通过**（0 Blocker + 1 Major + 7 Minor）。Major：handoff/README/review-log 未回填；Minor：租户 `family_revoked` 查询缺 `user_id`、登出审计对已撤销 family 误报 `family_revoked=true`、`session_scope` 静默回退平台连接、bcrypt 在 advisory lock 内的放大面未登记、并发/迁移/安全回归缺口。
- 第三轮修复：文档与台账回填；`family_revoked` 补 `uid` 过滤；`revoke_refresh_family` 返回实际撤销结果并用于审计；`session_scope` 缺 `tenant_id` 直接报错；TD-116 补充 bcrypt 排队放大面；新增自然过期重放、标记缺失过滤、踢出 vs 在途刷新、改密后 rotated 重放、限额下调多踢、HTTP 双 family 登出审计真值、迁移回填与并发 barrier 用例。
- 第四轮：**不通过**（0 Blocker + 1 Major + 5 Minor）。Major：Bearer 属租户 A、刷新令牌属租户 B（或同租户他人）时，撤销审计落在 A 且丢失 `subject_user_id`。修复：`FamilyRevocation` 携带 `realm/tenant_id/subject_user_id`；第二台设备的审计写入归属租户并带 subject，主操作者 realm 保持不变。Minor：并发 barrier 实际未加、`session_scope` 无回归测试、过期 rotated 语义未入决策记录且引用错、文档提前写"通过"、Redis 会话键无租户段、同 family 登出未断言 `family_revoked`。已补 barrier/单元断言/决策记录/引用/TD-117/审计真值断言，并新增"家族活跃但旧令牌过期"用例。
- 第五轮（最终复审）：**通过**（0 Blocker / 0 Major / 5 Minor）。实测跨租/跨用户审计落在 subject 租户且 `actor_id` 为操作者、同 family 去重、已撤销 family 真值、过期 rotated 语义、`session_scope` fail-closed 均成立；全量 290 passed、ruff/mypy/alembic 全绿。Minor 收口：并发 barrier 改为按任务首次获取加屏障、补审计 actor 断言、引用改 §4 第 7 条；跨 family 审计非原子与跨 realm 矩阵登记 TD-118；Redis 键租户段登记 TD-117。
- 最终门禁：后端 **290 passed**、`ruff check app tests` 全绿、`mypy --no-incremental app tests`（99 文件）无问题、`alembic check` 无漂移、head=`d4e6f8a0b2c1`、本地 `scripts/ci.sh` 全绿。
- 远端 CI（`692e867`）：`backend` #39639721352 success、`security` #39639721255 success；`frontend`/`evals` 未触发（无对应路径改动）。

### M1-D1 密码策略/历史/邮件重置/失败锁定：第一轮审查与修复（2026-10-22）

- 交付：迁移 `b2d4f6a8c0e1`（用户邮箱/锁定字段、密码历史、重置令牌、校管邮箱触发器）；AUTH-03 策略/历史/改密接入；AUTH-05 失败锁定；员工/平台邮件重置（申请 202、确认 204、30 分钟单次令牌、并发单次消费、会话与刷新家族全撤销）；admin/campus 忘记密码/重置页；审计 `auth.password.*`。
- 第一轮独立审查：**不通过**（1 Blocker + 5 Major + 2 Minor）。
  - Blocker：downgrade 先恢复旧 `revoked_reason` CHECK，存量 `password_reset` 行导致回滚失败。
  - Major：学生自助重置与 M2 延后结论矛盾；改密后未使用重置令牌仍有效；同步 SMTP 可用响应耗时枚举且无 IP 限流、伪造 tenant_id 污染租户审计；重置令牌放 query string；design/TD/handoff/README 未回填。
  - Minor：单测恒真断言与跨租/并发/422 缺口；客户端仅校验长度、FastAPI 422 回显 `input`。
- 修复：回滚前归一化 `password_reset → password_change` 并补临时库往返用例；学生命中时只审计 `student_reset_deferred`；改密事务内作废未使用重置令牌；申请改 `BackgroundTasks` 后台发信 + Redis 固定窗口 IP 限流（fail-open，审计 `ip_rate_limited`）+ 失败审计按令牌是否属于该租户分派平台/租户；邮件链接改 fragment + `no-referrer`；新增跨租/并发/改密失效/限流/422 脱敏/迁移回滚用例；api-client 增加同口径密码策略预校验；补 design/TD-107/116/119~122/handoff/README/简报。
- 复测（第二轮复审前）：后端 **329 passed**、`ruff`/`mypy`（103 文件）/`alembic check` 全绿；前端 **24 单测 + 20 E2E**、lint/format/typecheck/build 全绿；迁移 head=`b2d4f6a8c0e1`。
- 第二轮复审：**不通过**（0 Blocker + 7 Major）。Major：文档门禁自相矛盾（提前写“两轮通过”/计数过期）；平台账号无邮箱导致重置闭环缺失；重置邮件在事务提交前发送（SMTP 失败会发信但令牌回滚）；改密接口可绕过登录失败锁定；并发登录失败测试缺口；前端密码策略与后端 Unicode 口径不一致；C 端学生入口指向不存在的管理员重置能力。
- 第二轮修复：重置令牌“先提交后发信”，投递失败独立事务作废令牌并审计；改密失败计数在事务内提交并接入锁定；新增并发登录失败用例（6 并发恰 5 次失败 + 1 次 locked）；新增 `python -m app.platform_admin` 创建平台账号/维护邮箱并走完整邮件重置；api-client 按码点/字节/Unicode 类别对齐策略；学生提示先改为“联系学校管理员”。
- 复测（第三轮复审前）：后端 **339 passed**、前端 **25 单测 + 22 E2E**、静态门禁全绿；迁移 head=`b2d4f6a8c0e1`。
- 第三轮复审：**不通过**（1 Blocker + 3 Major + 3 Minor）。Blocker：`tech-debt` 仍提前写“两轮审查通过”、`review-log` 仍“待执行”、README/handoff 计数过期（329/24/20）。Major：确认接口无限流且每次伪造令牌都写失败审计（可放大平台审计）；平台邮箱闭环只测了 CLI、租户校管邮箱触发器无测试；学生提示仍指向未交付的管理员重置。Minor：未知账号申请仍可向租户审计流写入；平台账号改邮箱未作废旧重置令牌；staging 可明文 SMTP、重置链接默认端口只覆盖 admin、前端数字/大小写分类与 Python 有边界差异。
- 第三轮修复：确认接口新增独立 IP 限流（默认 30 次/15 分钟，超限 429 且不写审计，补随机来源 IP 回归）；未知账号申请审计固定落平台并带 `attempted_tenant_id`；新增校管邮箱触发器 + 邮件重置全链路用例；平台账号改邮箱同事务作废未用令牌并补断言；学生提示改为“后续版本开放”不再指向不存在能力；staging 也强制真实 SMTP + TLS/STARTTLS；前端大小写改用 Unicode 二元属性、数字补齐上/下标；补 TD-123（按 realm 分流重置链接）。
- 复测（第四轮复审前）：后端 **342 passed**、`ruff check`/`mypy`（104 文件）/`alembic check` 全绿；前端 **25 单测 + 22 E2E**、lint/format/typecheck/build 全绿。
- 第四轮复审：**不通过**（0 Blocker + 2 Major + 5 Minor）。Major：反代后所有请求同源导致按 IP 限流失效/误伤，且直接信任 `X-Forwarded-For` 可被伪造；申请接口超限仍逐次写平台审计，未认证请求可放大写入。Minor：确认限流“不写审计”断言用畸形令牌恒真且随机 IP 可能碰撞；缺少代理头负向测试；平台 CLI 固定 12 位初始密码无法覆盖 13+ 字符账号；简报配置清单缺限流变量；429 无 `Retry-After`。
- 第四轮修复：新增 `resolve_client_ip`（仅 `TRUSTED_PROXY_IPS` 白名单对端可读 XFF，取首个合法地址，畸形/缺失回退直连），部署文档要求 Nginx 覆盖写 `X-Forwarded-For $remote_addr`，补 6 条单元测试；申请接口超限静默返回（仍 202）且不写审计，测试断言仅两次 `sent`；确认限流用例改用合法格式未知租户令牌并显式计数失败审计，新增 `revocation.clear_rate_limit` 清理计数、IP 用 198.18/15 文档网段；429 统一带 `Retry-After`；平台初始密码长度改为 `max(12, len(account)+4)`，CLI 用例改用 28 字符账号；简报补 4 个限流变量与 `TRUSTED_PROXY_IPS`；design/spec/deployment/handoff 同步。
- 复测（第五轮复审前）：后端 **348 passed**、`ruff check`/`mypy`（106 文件）/`alembic check` 全绿；前端 **25 单测 + 22 E2E**、lint/format/typecheck/build 全绿。
- 第五轮复审：**通过**（0 Blocker / 0 Major / 3 Minor）。实测：白名单外伪造 XFF 被忽略、白名单内取首值、畸形头回退；申请/确认超限均在审计/建令牌/解析前拦截，失败审计计数 2→2；429 带 `Retry-After`；长账号 CLI 可创建且初始密码不含账号，改邮箱与作废令牌同事务；迁移 downgrade 先归一化再恢复 CHECK，RLS/授权齐备，`alembic check` 无漂移；令牌 SHA-256/30 分钟/单次/并发一胜/三类变更作废；锁定并发 5 失败 + 1 锁定。
- 第五轮 Minor 收口：删除 `service.py` 未使用的 bcrypt 长度辅助函数；spec/design 中“初始密码 12 位”改为“默认 12 位、平台账号按账号长度加长”；`Retry-After` 采用整窗秒数（保守值）记入 TD-121，待 M6 网关限流统一返回精确剩余 TTL。
- 最终门禁：后端 **348 passed**、`ruff check`/`mypy`（106 文件）/`alembic check` 全绿；前端 **25 单测 + 22 E2E**、lint/format/typecheck/build 全绿。
- 远端 CI：`backend` #39669833089 success、`frontend` #39669067278 success、`security` #39669833371 success（首轮 #39669067241 因 bandit 将 10 条密码表 SQL 字面量误报 B105 失败；仅对这些 SQL 行加 `# nosec B105` 后通过）；`evals` 未触发（无相关路径改动）。

### M1-D2 MFA（两步验证）：两轮审查与修复（2026-10-22）

- 交付：迁移 `c4d6e8f0a2b3`（`users`/`platform_users` 加 `mfa_last_used_step`；两侧刷新令牌加 `mfa_verified` 与 `mfa_change` 撤销原因；新增 `mfa_recovery_codes`（RLS）与 `platform_mfa_recovery_codes`，只存 HMAC 摘要）；`app/security/{encryption,totp}.py`（AES-256-GCM + realm/user AAD、HKDF 子密钥、TOTP、恢复码）；`app/auth/mfa.py`（`aud=campus-elf-mfa` 的挑战/绑定短时令牌）；`POST /auth/mfa/verify|enroll|activate|disable`；登录改为 `TokenPair | MfaChallenge | MfaEnrollmentRequired` 三态（密码正确但未过第二步时不签发任何令牌/Cookie）；强制策略仅平台运营账号（`MFA_REQUIRE_PLATFORM`，默认 false）；启用/关闭 MFA 轮换全部会话（`token_version+1` + 撤销所有刷新家族 `mfa_change` + 删除 Redis 会话标记 + 重新签发）；admin 端 TOTP 登录 + 强制绑定页 + 安全设置页（二维码本地生成）；campus 端仅 TOTP 登录步骤；审计 `auth.mfa.enrollment_started`/`activated`/`enrollment_failed`/`activation_failed`/`recovery_used`/`secret_invalid`/`disabled`/`disable_failed`。
- 第一轮独立审查：**不通过**（0 Blocker + 1 Major + 4 Minor）。
  - Major：`mfa_recovery_codes` 只有 RLS 策略登记，缺少真实的跨租户隔离用例（A 租户上下文能否读到/消费 B 租户恢复码）。
  - Minor：`decode_mfa_token` 未拒绝"平台 realm 却带 tenant_id"的令牌（创建侧有守卫、解析侧没有，纵深不一致）；`verify_mfa` 注释与实际消费顺序描述不一致（易被误读为水印先于投递）；登录时挑战注册失败只写 warning（与校验侧 fail-closed 不一致，会把 Redis 故障推迟成用户侧更难解释的错误）；恢复码熵偏低（10×32 字母表 ≈ 50 bit）需登记处置。
- 第一轮修复：新增 `test_mfa_recovery_codes_are_tenant_isolated`（A 上下文只见自己的摘要、跨租 select 为空、A 发起的 UPDATE 命中 0 行且 B 的 `used_at` 仍为 NULL）；`decode_mfa_token` 补 realm/tenant 组合守卫并加"手工伪造令牌被拒"单元测试；`verify_mfa` 注释改为明确"GETDEL 是投递前的唯一闸门，`challenge_live=False` 时水印/恢复码写入随事务回滚"；`register_mfa_challenge` 在 Redis 不可用时抛 `RedisUnavailableError`，两处登录调用点转 `MFA_UNAVAILABLE`/503，并补 fail-closed 回归（恢复后登录可继续、期间无刷新令牌落库）；恢复码熵登记 TD-135（已延后 M6，与 TD-129 再生成能力一并处理）。
- 复测（第二轮复审前）：后端 **411 passed**、`ruff check app tests` 全绿、`mypy --no-incremental app tests`（116 文件）无问题、`alembic check` 无漂移、head=`c4d6e8f0a2b3`；前端 typecheck/lint/prettier/build 全绿、api-client **32 单测**、Playwright **28 E2E**。
- 第二轮复审：**通过**（0 Blocker / 0 Major）。确认 Major 已闭环、三项 Minor 修复正确，未发现新缺陷；硬性不变量成立（未完成第二步不发会话/Cookie、secret 与恢复码明文只在激活响应出现一次且 `Cache-Control: no-store`、二维码本地渲染、待激活密钥仅存 Redis 并 fail-closed、Redis 故障 503 不降级为密码单因素）。
- 最终门禁：后端 **411 passed**、`ruff`/`mypy`（116 文件）/`alembic check` 全绿；前端 **32 单测 + 28 E2E**、lint/format/typecheck/build 全绿。
- 远端 CI（`5add509`）：`backend` #39771902296 success、`frontend` #39771902306 success、`security` #39771902298 success；`evals` 未触发（无对应路径改动）。
- 门禁补记：首次本地 `scripts/ci.sh` 在 `security: bandit（app）` 失败（`app/auth/service.py` 待激活分支用 `assert row is not None` 触发 B101，Low/High-confidence 会让 bandit 非零退出）；已改为 `if failure_reason is None and row is not None` 的类型收窄守卫，落空分支由既有 `RuntimeError` fail-closed 兜住。复跑本地 CI 24 步全绿后才提交。

### M1-E1 鉴权内核（权限矩阵落库 + 接口声明权限）：四轮独立审查（2026-10-22~23）

- 交付：矩阵 v1.2 的 59 码落库（platform 12 / school 34 / campus 9 / observability 2 / eval 2）并删除 9 个无引用旧码；`app/rbac/{catalog,seeds,routes,__init__}.py`（权限目录 + `TENANT_MODULES`/`code_realm()` 单一 realm 口径、8 个内置角色出厂默认、幂等同步、路由清单守卫）；迁移 `e5f7a9c1b3d5`（定义表 `rbac_builtin_roles`/`rbac_builtin_role_defaults`、`seed_builtin_roles(uuid)` SECURITY DEFINER 且 EXECUTE 对 PUBLIC 与应用角色全部收回、重写 `provision_tenant`/`review_registration_request` 改为同源调用、新增 C 端 `student`/`teacher`）；`require_permission(code, *, realm)` 接入 3 个真实权限码；权限增删下一次请求即生效且不作废在线会话（口径 ⑥/⑧，TD-145）；拒绝审计 `auth.permission.denied`。
- 第一轮独立审查：**不通过**（1 Blocker + 1 流程 Major + 5 Major + 8 Minor）。
  - Blocker：staged 树 ≠ 被测树（`test_tenant_onboarding.py`/`test_skeleton_loop.py`/`tech-debt.md` 有未暂存的安全相关改动，其中包含"平台角色不能 `EXECUTE seed_builtin_roles(uuid)`"的唯一断言、以及会泄漏 `DEV_SKELETON_LOOP_ENABLED` 的 fixture 缓存清理），因此 `434 passed` 不能作为这份 diff 的证据。
  - 流程 Major：E1 预判技术债未登记、`handoff.md` §1 与 README「当前状态」未回填（AGENTS.md §10.1/§1）。
  - Major：①路由清单只识别 `APIRoute`，`@app.websocket`/`app.mount` 会被静默漏检（正是 §3.3-A 要堵的洞），而用例只断 `len(ENTRIES) >= 20`；②旧码无条件删除，运营自定义角色上 `school.user.view` 一类的授权被静默清零且无重映射，比简报 §5.1-① 承诺的"无引用才删"弱；③`observability.*`/`eval.*` 没有 realm 归属，触发器、种子自检、同步守卫清一色只挡 `module='platform'`，租户角色照矩阵拿到 `eval.run` 完全合法；④"映射为空即补出厂默认"的启发式没有用例（`teacher_b_end` 默认只有 1 码，撤掉它=空映射=下次发版补回）；⑤迁移的开通函数体从历史文件 `e5c9d1f7a2b4` 取常量并按那份文件校验锚点，将来任何对 `review_registration_request` 的加固都会被下一次升级静默回退。
  - Minor（摘）：拒绝审计写失败被静默吞掉、权限去重只按码（realm 矛盾的重复声明被掩盖）、免鉴权白名单前缀过粗、`rbac_builtin_role_defaults` 缺外键、平台令牌用例其实没持有 `campus.chat.use`（与它的 docstring 不符）、`RAISE EXCEPTION` 用 `%`、downgrade 补回旧码不带 description。
- 第一轮修复：三个文件全部入暂存并重跑全量门禁；登记 TD-136~TD-150；`framework_routes()` + `FRAMEWORK_ROUTE_PATHS` 把既非 `APIRoute` 也非 `include_router` 的对象显式枚举出来，并由用例断言"只能是框架资产"；`LEGACY_GRANT_GUARD_SQL` + `assert_no_legacy_grants()` 在删码前拒绝"仍被非全量型角色引用"（必须先人工改授再同步），守卫角色码改为参数化传入；`catalog.TENANT_MODULES`/`code_realm()` 定单一口径，种子自检、同步收尾守卫、矩阵 §5-11 统一到该白名单（用户定稿：观测/评估只属平台、不下放，租户自助评估将来另开 `school.eval.*`）；补"清空全部映射后重新补齐出厂默认"与"同名非系统角色拒绝落任何语句"用例；新增 `test_migrations_after_e1_keep_provisioning_functions_seeded`（按 `down_revision` 串链，禁止 E1 之后重写开通函数却不调用 `seed_builtin_roles`）；补 `rbac_builtin_role_defaults.permission_code → permissions.code` 外键、审计失败的告警、`(code, realm)` 去重、平台用例改持 `campus.chat.use`。
- 第二轮（对抗复审，逐条取证）：**通过**（0 Blocker / 0 Major / 0 正式 Minor，1 条信息项）。逐项取证：`seed_builtin_roles` 的 `search_path` 顺序与 `public.` 全限定、EXECUTE 只能由迁移属主调用；补写语句的 `tenant_id` 一律取自父行 + 复合外键使"A 租映射写进 B 租"不可能；旧码删除与 downgrade 集合精确；downgrade 恢复的函数体确为链上最新定义；`require_permission` 的 realm 断言先于权限断言且拒绝审计只带 path+reason；免鉴权路由仅健康探针/认证自助/公开登记三类；RBAC 用例断的是库态与文档态而非恒真式。信息项"存量租户若曾手工授过旧码，删除后不会自动补矩阵后继"与已登记的 TD-150 及"绝不覆盖运营改动"口径一致，不改动。
- 第三轮（另一独立上下文复审）：**不通过**（0 Blocker + 2 Major + 5 Minor）。
  - Major M1：`docs/rbac-matrix.md` §4 与实现冲突两处，且 §4 **从没被机器校验**——`_section()` 只解析 §3，矩阵的 √/△ 全靠手抄成三份字面量断言，改任一格不会有任何测试变红，而简报 §7 声称"§3/§4 逐条一致"。冲突 A：「评估运行/报告」行给租户角色标 △，实现守卫按 `code_realm()` 拒绝 → 运营照矩阵授权（触发器放行）后，下一次发版的同步整批回滚、升级被挡死；冲突 B：C 端三行把校方超管列标 —，实现是 `school`+`campus` 模块全量。
  - Major M2：`test_build_sync_statements_rejects_ghost_and_platform_codes` 名不符实——函数体只断言语句非空与首条是删除，把 `build_sync_statements()` 里的 `_self_check_seeds()` 接线整行删掉也不会红。
  - Minor：守卫 SQL 硬编码两个超管码字面量（与 `seeds.FULL_SCOPE_ROLES` 两份真相）且只查 `roles` 不查 `platform_roles`；TD-136 写"4 码挂路由"实为 3；简报写 8 条路由清单用例实为 10；`logger.warning(..., exc_info=True)` 会把 SQLAlchemy 异常串里的 `[parameters: ...]` 写进日志；SQL 基线只设上界无下界。
- 第三轮修复：按用户定稿"只属平台，不下放"**改文档方向而不是放松守卫**——矩阵升 v1.2：§4 新增「对应权限码」列、拆分平台权限行、把 `eval.*`/`observability.*` 的租户列由 △ 改 —、补三行漏项（`school.kb.delete`、`school.account_termination.request/appeal/freeze`、`campus.conversation.clear`/`campus.nav`）、加"平台超管列只覆盖平台运维域 16 码"的说明与脚注。`test_rbac_matrix_unit.py` 扩到 37 条，新增四条把 §4 变成受测输入的断言：行不重不漏并集等于 59 码、8 个角色列的 √ 集合 = `seeds.py` 出厂默认、△ 集合与默认不相交、租户五列只出现 tenant 域码（实测 35 行、八列 √ 数 16/7/3/43/6/1/9/7）。接线改为参数化注入幽灵码/`platform` 码/`observability` 越域码并断 `build_sync_statements()` 抛 `RuntimeError`；守卫的角色码由 `FULL_SCOPE_ROLES` 绑定传入并补 `platform_roles` 同名检查；日志去掉 `exc_info`、只记异常类型（新增 `test_principal_denial_audit_unit.py` 2 条：写失败仍返回 403 且日志不含 SQL 参数/令牌片段、成功路径逐字段核 `action`/`resource_id`/`result`/`metadata`）；SQL 基线补下界；TD-136 与简报计数改正。
- 复测（第四轮复审前）：矩阵/路由清单/审计单元 39 条、RBAC 集成 21 条全绿；逐格翻转矩阵 √/△ 实测会让 §4 对应用例变红。
- 第四轮复审：**不通过**（0 Blocker + 1 Major + 3 Minor）。Major：`app/db/selfcheck.py` 启动自检仍按 `module = 'platform'` 判越界，与本期定稿的 `code_realm()` 白名单不一致 → 运营手工 `INSERT` `observability.*`/`eval.*` 进租户角色时，触发器（TD-149）与启动自检双双放行，"同步守卫是唯一防线"只在重跑同步时成立。Minor：拒绝审计单元用例吞掉 `db_session()` 实参（租户落哪个连接没断言）、集成只读 `action`/`metadata_json`（审计的租户归属无断言）；review-log/handoff/README 未回填；矩阵版本行仍是 v1.1。
- 第四轮修复：自检判据改 `p.module <> all(cast(:tenant_modules as text[]))`，参数取 `sorted(TENANT_MODULES)`（纯读检查、零迁移成本；触发器收紧仍按 TD-149 留到 M1-E2），并把登录集成用例参数化为两种越权形态——`platform.tenant.view`（触发器硬挡，用例临时禁用触发器造数据）与 `observability.trace.view`（触发器不挡，正是代码侧白名单的取证点）；把谓词临时改回 `module = 'platform'` 复跑，第二条必然 `DID NOT RAISE`，证明用例非空转后还原文件。单元改为捕获 `db_session` kwargs 并断言租户侧 `tenant_id=`、平台侧 `platform=True` 与 `actor_realm`/`actor_id`/`tenant_id`/`metadata`；集成的拒绝审计按 `actor_id` 过滤并核 `tenant_id`（平台事件为空）；矩阵版本行、`app/rbac` 各文件与简报的引用统一到 v1.2；本条记录与 handoff/README 按事实顺序在提交/CI 之后回填。

- 第五轮（最终复审，独立上下文）：**通过**（0 Blocker / 0 Major / 3 Minor）。四条重点防线逐条取证成立：realm 边界（`require_permission` 先判 realm、平台超管持全量码仍被挡；启动自检绑定 `TENANT_MODULES` 的判据可捕获 `observability`/`eval` 越权，参数化用例非空断言）；幂等同步（只补不撤 / 空映射才补默认两种口径、撤销不被加回、旧码仍被运营引用时 fail-fast 且不动数据）；迁移往返（临时库 head→downgrade→head，FK CASCADE 顺序与文档一致）；开通同源（两条开通路径都只调 `seed_builtin_roles`，定义表只读、EXECUTE 已收回）。拒绝审计卫生、矩阵 §3/§4 与代码双向钉住、trace 身份取主体且 `tenant_id is None` 显式 403、定义表 RLS 白名单理由均复核通过。
- 第五轮 Minor 收口：①`TENANT_PLATFORM_LEAK_CHECK_SQL` 原本把 `('school','campus')` 抄成 SQL 字面量，与 `catalog.TENANT_MODULES`（自称 realm 唯一口径、启动自检已绑它）是两份真相，白名单日后调整会静默漂移 → 改为 `p.module <> all(cast(:tenant_modules as text[]))` 并由 `_tenant_leak_check_params()` 绑定（同步/异步两条后置守卫同一参数；实测传空列表时 `<> all('{}')` 恒真会报错，判据不会静默失效）；②downgrade 第 2 步删 C 端角色只按 `code = ANY(...)`，与第 1 步特意过滤 `is_system` 的口径不对称 → 补 `WHERE is_system`，回滚绝不碰运营自建的同名角色；③`/auth/` 整段免权限码（M2 若在 `/auth/` 下加特权接口，进白名单即只校验"已认证"）→ 并入 TD-139 的 M2 复核项。
- 第五轮修复后复测：`ruff check app tests` 全绿、`mypy --no-incremental app tests`（128 文件）无问题、`bandit -r app -x tests` 与 `bandit -r alembic -s B608` 退出码 0、受影响用例 44 条（`test_rbac_sync.py` + `test_migration_rbac_rollback.py` + `test_rbac_matrix_unit.py`）全绿；后端全量 **481 passed**（7 分 02 秒）。
- 最终门禁：后端 **481 passed**、`ruff`/`mypy`（128 文件）/`bandit`×2 全绿、`alembic check` 无漂移、head=`e5f7a9c1b3d5`。本期不改前端，故无新增 Playwright 用例（该项在简报 §8 登记）。
- 远端 CI（`6e916af`）：`backend` #39851973194 success、`security` #39851973099 success；`frontend`/`evals` 未触发（无对应路径改动）。

### M1-E2 数据范围 + 职责分离 + 平台跨租只读：两轮审查（2026-10-23）

- 交付：迁移 `f7a9b1c3d5e8`（新表 `rbac_builtin_role_scope_defaults`/`system_settings`；注册申请表增 `submitted_by_platform_user_id` + `self_exception_used` + 同表 CHECK；扩写 `seed_builtin_roles(uuid)` 为新租户写 `data_scopes`；`review_registration_request()` 承担 RBAC-03；`role_permissions_realm_guard` 收紧到 `TENANT_MODULES` 白名单）；`app/rbac/scopes.py` + `app/db/repository.py`（`school`/`self` 谓词、解析不到范围 = 空集 fail-closed、`ScopeBypass(reason=…)` 显式绕过、缺归属列构造期报错）与 `Principal` 每请求范围解析；`GET /api/v1/school/personnel`(+`/{id}`，`school.personnel.view`，响应固定 6 字段、不含 `login_id`，越权 404 + `data_scope.denied` 审计）；`POST /api/v1/platform/cross-tenant/personnel/read`（平台 realm + `platform.cross_tenant.read`、事务首条 `SET LOCAL TRANSACTION READ ONLY`、单租户、理由必填、审计失败即请求失败、脱敏返回）；`GET/PUT /api/v1/platform/settings`（`platform.config.view/edit`、改值必填理由、`platform.setting.update` 审计、未知键 404、每请求读库热生效）；自批三道锁（应用层 403/422 + 回滚后拒绝审计、数据库函数 `SELF_APPROVAL_*`、同表三条款 CHECK）与例外开关；裸 SQL 守卫清单 + SQL 条数基线（实测 6 / 上限 8）；业务场景 2 条（自批被拒→开例外→放行并留审计；跨租默认拒绝→授权+理由→只读返回并留审计）。
- 第一轮独立审查：**不通过**（0 Blocker + 3 Major + 4 Minor）。
  - Major M1：人员列表/详情响应带 `login_id`，违反口径 ⑥「学号/工号不外发」。
  - Major M2：迁移把简报钉住的两条款 CHECK 改成三条款（多 `OR self_exception_used IS TRUE`）未经用户拍板，且"直写同人 + 标记为真"与合法例外在库层同形。
  - Major M3：`app/settings/store.py` 模块 docstring 宣称"启动自检会点名未知键"，实现里没有——注释承诺了不存在的功能。
  - Minor：停用/删除租户的可读性注释与实现不一致；详情越权在审计写入失败时把 404 变成 500；只读事务用例断言空转（只断异常类型）；`platform.cross_tenant.read` 的目录描述仍写"二次授权"，与 TD-153 延后口径不符。
- 第一轮修复：响应删除 `login_id`、补 `last_login_at` 与 `_RESPONSE_KEYS`/`login_id not in text` 硬断言；三条款 CHECK 经用户确认（「接受三条款+补检测」）并登记 TD-156/TD-161；docstring 改为如实陈述并登记 TD-160；修正租户状态注释；越权审计改 best-effort（类型级日志、不改变 404）；只读用例改为断言 PG 报错含 `read-only transaction` 且探针无残留；目录描述改为"工单化延后 TD-153"；补跨租审计失败 fail-closed、绕过接口层直调审核函数的第二道锁、数据库守卫正反两面（平台域三码拒绝 + 租户域放行）等用例。
- 第二轮复审（当日子智能体配额耗尽，改在主上下文执行，非独立上下文）：**通过**（0 Blocker / 0 Major）。复核 `principal`/`repository`/`seeds`/`onboarding`/审批/设置/API/迁移与全部 E2 用例：范围解析 fail-closed、三道锁互相独立、跨租通道只读先行且审计失败即失败、响应形状与 SQL 上限、权限码/白名单/矩阵口径一致，未发现新缺陷。附带记录：`rbac_builtin_role_scope_defaults` 未加指向角色定义表的外键（应用角色仅 `SELECT`，写入只来自 SECURITY DEFINER 的 `seed_builtin_roles`），判断为低价值不改，留 M2 配置界面复评。
- 最终门禁：后端 **561 passed**（全量退出码 0）、`ruff check app tests` 全绿、`mypy --no-incremental app tests`（149 文件）无问题、`bandit -r app -x tests` / `bandit -r alembic -s B608` 退出码 0、`alembic check` 无漂移、head=`f7a9b1c3d5e8`；本期不改前端（`/school/personnel` 无前端入口，TD-152），无新增 Playwright 用例。
- 远端 CI（`d7a7663`）：`backend` #39950816551 success、`security` #39950816785 success；`frontend`/`evals` 未触发（无对应路径改动）。

### M1-F 审计哈希链：独立审查两轮（2026-10-24）

- 第一轮：**不通过**（1 Blocker + 4 Major + 4 Minor）。Blocker B1：并发插入下触发器无串行化 → 链必然分叉；Major：校验脚本对未入链行静默、created_at 依赖会话时区、测试覆盖与简报清单不符、文档未同步。
- 第一轮修复：advisory lock 串行化 + 并发测试；校验脚本报告未入链行；created_at 统一 UTC 编码；补并发/错误密钥/自检触发器缺失测试；config 注释改 fail-open；文档回写。
- 第二轮：**不通过**（0 Blocker / 5 Major）。MA-1：BIGSERIAL sequence 在锁前分配，锁序与 sequence 序倒置仍可分叉；MA-2：未入链检测"链从未开始"分支死代码 + 零测试；MA-3：自检测试仍用 os.environ.setdefault + 篡改检测是恒真断言；MA-4：README/review-log 未更新；MA-5：第一轮 Minor 项无 TD 处置。
- 第二轮修复：锁后重分配 sequence（锁序=sequence 序）；死代码修复（first_chained IS NULL 时也检测 v0 行）；自检测试改 monkeypatch（第三轮复审确认落地）；篡改检测改为脚本级 subprocess 测试（退出码 1 + 未入链行输出）；README/review-log 更新；MA-5 各项登记 TD。
- 最终门禁：后端 574 passed、ruff/mypy/bandit 全绿。

### M1 验收 + TD-110 全栈 E2E（验收审查，2026-10-24）

- 设计简报：docs/design-briefs/M1-acceptance.md
- commit/diff：staged diff（19 文件，+1309：全栈 E2E 基础设施 + 12 用例 + 验收报告与文档回写）
- 审查人/agent：独立审查子 agent（与实现者独立上下文）
- Findings：Blocker 0；Major 1（简报 §2 两处实现偏差未在 §9 登记：seed 口令写死为测试域常量、迁移/seed 在宿主机执行）；Minor 9（TD 计数过期 169→171、CI 触发预测不实、e2e_seed 死代码、scripts 不在门禁范围、Dockerfile 冗余步骤、.dockerignore 缺 .env*、compose 缺 QDRANT_URL、越权断言 [401,403] 可收紧为 403、review-log 待补）。
- 修复：Major 与 Minor 全部落实（越权断言经核实 `require_permission` 的 realm 判定恒为 403 后收紧；TD-172 登记 scripts 门禁范围；TD-173 另见 M1-F 条目）。
- 复测：全栈 E2E 12/12 passed（29.4s）；mock E2E 28 passed；后端 578 passed；ruff/mypy/alembic check 全绿。
- 结论：**可提交**（0 Blocker / 0 Major）。

### M1-F 审计哈希链：第三轮复审确认（随 M1 验收，2026-10-24）

- commit/diff：`070e9c7`（M1-F 生产代码）+ 本次修复（`session.py` 注释、审计集成测试两处、新增单测、回滚回归、TD-173）
- 审查人/agent：独立审查子 agent（同验收审查不同焦点）
- Findings：Blocker 0；Major 1（`verify_chain()` 篡改检测两分支零测试，简报 §5 TDD 承诺未兑现）；Minor 6（fixture 死代码、docstring 失实、advisory lock 未登记 TD、set_config 异常 repr 边界、「集成 9 条」应为 8 条、无 M1-F downgrade 用例）。
- 修复：新增 `tests/test_audit_chain_verify_unit.py` 5 例覆盖 `verify_chain` 全部分支（篡改/断链/链头/密钥错误）；`test_m1f_downgrade_with_audit_rows_then_upgrade` 验证 downgrade 保留审计行、再 upgrade 后新行 prev_hash 接上原链尾；Minor 全部落实；e2e 库行篡改用例经复审确认**不要求补**（subprocess 接线已被未入链行用例覆盖，会话级禁用触发器的恢复风险与收益不对等）。
- 复测：受影响用例 23 passed；全量 **578 passed**（505s）；ruff/mypy 153 文件全绿。
- 口径更正：M1-F 集成测试正确计数为 **8 条**（此前 commit message 与本记录的「9 条」系首条用例拆两项误计）。
- 结论：**M1-F 复审通过，移除「待复审确认」标注**。

### M2-ORG01-03 人员档案与组织关系（2026-10-24）

- 设计简报：docs/design-briefs/M2-ORG01-03.md（D1-D4 已确认；§9 记录 9 项实现期偏差）
- commit/diff：staged diff（24 文件，约 +2990：迁移五表 + D1 class 数据范围贯通 + 组织 CRUD/审计/cursor + BS-M2-01）
- 审查人/agent：独立审查子 agent（与实现者独立上下文），三轮
- Findings：
  - 第一轮：Blocker 0；Major 3（坏 cursor 500、启动自检 RLS 清单缺新表、handoff/README 未随提交）；Minor 11（同值短路跨 id 空间、审计口径不一致、断言弱、死代码工厂、过滤下推、在职校验、count 全行加载、department_id 取舍未记、端点数/矩阵/catalog 描述、双 docstring、list[Any] 契约）。
  - 第二轮：**不通过**——修复声明与实际 diff 系统性不符（长补丁脚本中途断言失败导致部分文件未写盘，7 项未落地、2 项半成品、临时脚本残留）。教训：批量补丁必须逐文件写盘后立即验证。
  - 第二轮修复：弃用补丁脚本，逐文件 Edit + 落盘 grep 验证；全部 14 项落地（CursorError 窄异常、typed response_model、selfcheck 清单、handoff/README、同值比较后置、审计档案 id 口径、断言增强、死代码删除、extra 过滤下推、在职校验、SQL count、简报/matrix/catalog 回写、docstring、临时脚本清理）。
  - 第三轮：仅剩 1 项（HTTP 级坏游标 400 断言）→ BS-M2-01 补 3 行断言并复测通过；复审确认通过。
- 复测：受影响用例 55 passed（含 RLS 覆盖/隔离/readiness）；全量 **596 passed**（511s）；ruff/mypy 161 文件全绿；alembic check 无漂移（head c9d5f1e3a7b9）。
- 更好做法与 TD 处置：TD-070（cursor，本点收口/Personnel 迁移随 B 端页面）、TD-151（class 收口，course 随 M3）、TD-174（新，双写对账，M2 末评审）。
- 结论：**可提交**（0 Blocker / 0 Major）。

### M2-ORG02 CSV/XLSX 异步导入（2026-10-24）

- 设计简报：docs/design-briefs/M2-ORG02-import.md（D1-D4 已确认；§9 偏差 + §10 复审记录）
- commit/diff：staged diff（20 文件 +1651 → 修复后 25 文件）
- 审查人/agent：独立审查子 agent（与实现者独立上下文），两轮
- Findings：
  - 第一轮：Blocker 0；Major 6（解析器空列错位映射、错误行 field 恒 "row"、D3 文件内重复策略未实现且简报失实、上传体积限制未接线、retry 入队失败死端、running 卡死无恢复）；Minor 9（裸 add 绕守卫、空行行号口径、_http_error 死代码、解析阻塞事件循环、job 生命周期审计缺失、冲突预探测重复查询、进度判定漏 skip、编码/性别别名、design.md/handoff/README 文档缺口）。
  - 第一轮修复：全部 6 Major + 9 Minor 落地（列位映射、OrgError.field、seen_numbers/FILE_DUPLICATE、BodySizeLimit 接线、补偿清 retried_by、看门狗 cron、错误行仓储化、空行占号、to_thread、审计、CAS、文档回写）。
  - 第二轮：Major 2 项小修（① exc.field 未消费——一行修复 + BS 断言 ORG_LOGIN_TAKEN field=login_id；② M6 恢复承诺未兑现——选定方案 b：failed 任务可 retry 整文件重跑，已导入行以冲突报错收敛，_finish/进度仅 running 可写不复活终态）；Minor：cron 节奏修正（每 15 分钟四条）、BS retry 断言补 field、README/handoff 落盘、design.md 重复行删除、守卫 sa_update 盲区登记、retry CAS/补偿用例补齐。
- 复测：受影响用例 55 passed；全量 **618 passed**（1211s）；ruff/mypy 167 文件全绿；alembic check 无漂移（head e2a6c8d4b0f2）。
- 更好做法与 TD 处置：TD-175（新，导入文件对象无生命周期，M6）；TD-174 对账在本点实测恒一致（导入不产生教师范围行）。
- 结论：**可提交**（0 Blocker / 0 Major）。

### M2-ORG04 身份证加密采集与受控查看（2026-10-25）

- 设计简报：docs/design-briefs/M2-ORG04-id-card.md（D1/D3/校验/D4 方案 A 已确认；§9 实现结果含复审修复记录）
- commit/diff：staged diff（初版 20 文件 +1948 → 修复后 22 文件）
- 审查人/agent：独立审查子 agent（与实现者独立上下文），两轮
- Findings：
  - 第一轮：**Blocker 2**（B1 AAD 身份绑定错位——建档路径绑 `User.id`、受控查看绑 `Teacher.id/Student.id`，导入写入的密文永久不可解；B2 `import_row_errors.raw_json` 落身份证明文）；
    **Major 6**（M1 受控查看 404 路径 UnboundLocalError→500；M2 422/501 异常映射缺失+导入吞成 INTERNAL_ERROR；M3 无 `(tenant_id, id_card_hmac)` 唯一约束且 create 路径零查重；M4 列表 `id_card_mask` 恒 NULL（死代码掩盖）；M5 未登记偏离已确认简报——D2 写入路径建成被否方案 B 的专用端点、D3 reason 走 Query 泄 URL、design.md 记载不存在的能力；M6 测试缺口——422/501/跨租/明文不泄漏/导入回读均无断言）；Minor 5（死代码 `_check_hmac_unique`/占位变量、config 注释失实、.env.example 缺 ID_CARD_KEK、审计缺 created|updated 区分、`IdCardWriteRequest.reason` 未审计）。
  - 修复：B1 建档预生成档案 id、AAD 统一绑 `Teacher.id/Student.id`；B2 `_redact_raw`（掩码化）在 `_record_row_error` 单点收口；M1 `view=None` 前置 + last_error 文案；M2 `validate_or_422` 统一译 422 `ID_CARD_INVALID`、路由/导入补 501 与 `ID_CARD_NOT_CONFIGURED` 结构化行错误；M3 迁移 `f8b2d6a0c4e8`（两表部分唯一索引）+ 模型镜像 + create 内 `_check_hmac_unique` + IntegrityError→409；M4 `_id_card_masks_for` 批量回填（掩码列只读、行已过范围过滤）；M5 移除专用端点、create/edit 增 `id_card_number`、reason 改请求体、design.md 路由表校正；M6 BS 重写（同号 409/422/404/403/501/跨表查重/导入回读/raw_json 脱敏/审计明文不泄漏/KEK 缺省 501）；Minor 全部落地。
  - 第二轮（复审）：**PASS，0 Blocker / 0 Major**；Minor 3 项处置——① 受控查看未捕获 `IdCardDecryptError`（密文损坏→裸 500）：已采纳随密钥轮换 TD（简报 §6）落地结构化 `ID_CARD_DECRYPT_FAILED`，当前 fail-closed 不泄漏明文；② `validate_or_422` 先于 `_kek()`：接受现序（入参合法性校验与功能可用性独立，KEK 缺省+非法号返回 422），登记为既定口径；③ 提交卫生（ORG-05 复审测试混入本批）：按 AGENTS §1.8 拆分提交——`test_org_structure.py` 复审测试（经 ruff format 清理补丁脚本遗留空行）与 ORG-05 review-log 补录条目单独先行提交，ORG-04 功能点单独提交。
- 复测：受影响用例 92 passed（BS 3 + 集成 73 + 单元 19 初验）；全量 **637 passed**（1117s）；ruff/mypy 173 文件全绿；alembic check 无漂移（head `f8b2d6a0c4e8`）；bandit（app + alembic，CI 同命令）exit 0。
- 更好做法与 TD 处置：TD-179（PUT 调岗+身份证两步提交非原子，M2 末评审）；TD-180（跨表查重竞窗，M6）；TD-178（核查适配器，M5/M6）。
- 结论：**可提交**（第二轮复审 0 Blocker / 0 Major）。

### M2-ORG05 部门/科室树 复审补录（2026-10-25）

- 设计简报：docs/design-briefs/M2-ORG05-departments.md（§9 交付物与 10 条偏差要点在档）。
- 说明：独立审查在 M2-ORG04 之前的会话完成，逐条 findings 清单未随会话上下文存续，本条目为**补录**——只登记有据可查的产出：
  - 复审落地的回归测试（`backend/tests/integration/test_org_structure.py`，7 passed）：`test_class_unique_constraint`（(tenant_id, grade, name) 唯一约束）、`test_teacher_student_one_to_one`（(tenant_id, user_id) 1:1 约束）、`test_class_scope_partial_unique_index`（class 范围行部分唯一索引）、RLS 隔离/同步/对账用例重写收紧；
  - 简报 §9 补记两条复审修正（部门列别名归一、POST teachers 增 department_id 与 design §5.4 对齐）。
- 门禁：随 M2-ORG04 批次复测（见上：637 passed / ruff / mypy / alembic check / bandit 全绿，迁移链 `a4c8e2f6b1d3` → `f8b2d6a0c4e8`）。
- 结论：**复审通过（补录口径）**； findings 明细不可考，后续功能点审查记录当轮落盘（本条为流程教训）。

### M2-UI1 B 端管理页面（2026-10-26）

- 设计简报：docs/design-briefs/M2-UI1-b-end-pages.md（D1 纯 UI+TD-141 / D5 弹窗理由 / D6 三步向导 / D7 mock 主+全栈，均选 A 已确认）
- commit/diff：staged diff（35 文件 +3347/−26 修复后）
- 审查人/agent：第一轮独立审查子 agent；**第二轮因子智能体配额耗尽（exceed quota limit）改主上下文执行**（与 M1-E2 先例同口径，如实登记）
- Findings：
  - 第一轮：Blocker 0；**Major 6**（① 导入重试响应形状错——前端/mock/单测都按 job 本体编码，真后端返回 {source_job_id, job_id}，点击后轮询 /imports/undefined；② 受控查看弹窗在途请求迟到响应可越过关闭动作残留明文且串人；③ 学年流转幂等键每次点击重新生成，响应丢失后重试击穿后端唯一防线；④ 班主任/辅导员指派漏实现（简报 §1.3 验收项）；⑤ 教师/学生列表掩码列为死列（后端响应无该字段）；⑥ 全栈 spec 缺 field import 无法运行——typecheck/lint 覆盖不到 e2e-fullstack 目录）；Minor 6（部门删除 409 文案、轮询中断无提示、删除无确认、列表截断上限、409 重放 UX 未测、mock classes DELETE 假 200）。
  - 第一轮修复：① api-client retry 改 ImportRetryRef + ImportsView 按 ref.job_id 重新取进度 + mock/单测同步真实形状；② 弹窗 watch 打开/关闭都清空 + await 后 visible 守卫丢弃迟到响应；③ 幂等键预览成功时生成一次、跨重试稳定、reset 才重置 + E2E 断言两次执行体键恒等；④ 建班/编辑弹窗补班主任/辅导员下拉（与 API 字段一一对应）；⑤ **后端补真列**：TeacherItem/StudentItem + 响应模型增 email_masked/id_card_mask（列表批量回填邮箱掩码 + 档案掩码列，建档带证即回掩码）+ BS-M2-04 列表掩码断言 + 前端掩码列；⑥ 补 import。Minor：确认弹窗（班级/部门）、轮询中断一次性提示、org-mock DELETE 分支、TD-184 登记。
  - 第二轮（主上下文）：逐项复核 6 项修复落盘与语义（retry 形状/晚到响应丢弃/键稳定性/指派下拉/掩码列端到端/import），另修一处自检发现的细节（建档响应的 email_masked 用实际入参而非 None）；未发现新 Blocker/Major。
- 已登记偏差（简报 §9）：受控查看 GET→POST（浏览器 fetch 禁止 GET body，理由仍在请求体）；看板/权限账号移出范围（后端未实现：看板属 M6 DASH-01）；3 条全栈 E2E 未跑（TD-183 端口 8000 被本机另一项目容器占用）。
- 复测：前端 mock E2E 47（含新增键稳定性用例）/单元 57/lint/typecheck/build 全绿；后端受影响 21 passed；全量见提交前门禁。
- 更好做法与 TD 处置：TD-141 收口（本点）；TD-183/184 新增延后项；OpenAPI codegen 切换延后 M3（简报 §6）。
- 结论：**可提交**（第一轮 6 Major 全修复，第二轮主上下文复核通过）。

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

### M2-ORG06 入学年份与学年流转（2026-10-25）

- 设计简报：docs/design-briefs/M2-ORG06-academic-year.md（用户确认三项决策：唯一键=入学年份+班名 / 归档=行保留+审计 / 毕业学生账号本点不动；TD-181 精简采纳版）
- commit/diff：staged diff（17 文件 +1470/−56 → 修复后含文档批）
- 审查人/agent：独立审查子 agent（与实现者独立上下文），两轮
- Findings：
  - 第一轮：Blocker 0；**Major 1**（归档态未回头收口既有写路径：create_student/create_assignment 可写归档班、delete_class 可硬删归档行、update_class 可改写历史 grade/name——与「行永不删除/沿革保留」的既定语义自相矛盾）；Minor 5（不同幂等键并发无串行化、rollover 缺 school 范围要求与 list_classes 不对称、transfer 端点/审计动作未回写 design.md 且 handoff/README 未更新、"中途失败全回滚"测试未交付、archive_grades 元素无长度约束）。
  - 第一轮修复：Major——新增 `_require_active_class` 统一探针（create_student/create_assignment 归档封闭 422 `CLASS_ARCHIVED`）；delete_class 归档拒删 409 `ORG_CLASS_ARCHIVED`；update_class 归档行禁改 grade/name（422 `ORG_CLASS_ARCHIVED_READONLY`），班主任/辅导员纠错仍可；BS 断言四路径 + 纠错放行。Minor——执行前取租户级 `pg_advisory_xact_lock` 串行化（残余登记 TD-182）；`_require_school_scope` 对称收口；transfer 回写 design.md；补回滚原子性测试（注入审计故障 → 班级与批次行零残留）；archive_grades 元素 1~16 约束。
  - 第二轮：**PASS，0 Blocker / 0 Major**；提交前文档项（handoff/README/review-log 随本提交 stage、简报补"重放错误码不采纳"处置）——均已落。
- 实现期偏差（登记于简报 §9）：学生转班能力缺口补建 `POST /school/students/{id}/transfer`（简报假设的"沿用学生编辑"实际不存在）；ClassUpdateRequest 增 entry_year 仅作拦截（Pydantic 静默丢弃未知字段的坑）；导入定位加活跃过滤 + CLASS_AMBIGUOUS；grade 列名不改语义改当前年级；规划/执行分离（纯函数按原年级同时匹配，链式映射无级联）。
- 复测：单元 12 + BS-M2-06 两用例（含归档封闭与回滚零残留断言）+ 受影响套件 96 passed；ruff/mypy 176 文件全绿；迁移回滚用例修复后单独复测 11 passed；全量见提交前门禁记录。
- 更好做法与 TD 处置：TD-181（已采纳，本点精简版）；TD-182（流转并发分歧残余，M2 末评审）。
- 结论：**可提交**（第二轮复审 0 Blocker / 0 Major）。
