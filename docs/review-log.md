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
- 远端 CI：待推送后回填。

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
