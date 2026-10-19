# M0-F — 基准、测试基础设施与 M0 验收收口 实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-20 |
| 状态 | 已确认（2026-10-20 用户确认 A/A/A/A） |
| 关联功能点 | M0-F（M0 收口功能点） |
| 验收引用 | `docs/milestone.md` M0 退出标准；`docs/spec.md` 测试与隔离要求；TD-002/005/010/029/030/031/033/034/035/047/052 |

## 1. 目标

把 M0 收口为可验收状态，共六件事：

1. **CPU 嵌入/重排基准**：只记录数字供 M3 参数决策与上云选型参考，不做调优（用户已明确开发期不追性能）。
2. **测试基础设施**：独立测试环境、pytest markers/timeout、假密钥集中、业务场景测试骨架（含"健康检查→trace"示例链路）。
3. **空壳 Loop**（M0 退出标准项）：`meta/step/done` SSE + `stop_reason` + 本地 trace 必发，不调 LLM。
4. **CI**：GitHub Actions 工作流 + 本地等效脚本（收口 TD-047/035/052）。
5. **应用最小收口**：`/health/ready` 依赖探活（TD-005）、structlog 统一 JSON 日志（TD-002）、TEI 模型离线预置（TD-010）。
6. **前端质量门禁缺口**：ESLint/Prettier、api-client Vitest、Element Plus 按需引入、e2e 失败态/接口 mock、tsconfig 覆盖（TD-029/030/031/033/034）。
7. **M0 验收报告**：退出标准 ↔ 证据矩阵 + TD 处置 + 已知缺口。

不解决：性能调优与压测（M6 k6）；KB/AI 业务功能（M3/M4）；Vant 按需样式（TD-032，仍 M6）；生产镜像烘焙（TD-054，M6）。

## 2. 候选方案

### 2.1 测试环境隔离

**方案 A（推荐）：独立 test compose 栈**

- 做法：`deploy/docker-compose.test.yml`（project 名 `campus-elf-test`），独立端口与实例：PostgreSQL（`campus_elf_test` 库）、Redis（独立 DB/密码）、MinIO（独立桶前缀）、Qdrant（独立 collection 前缀）；与 dev 栈按需并行/交替启动；`scripts/test-stack.sh` 封装 up/down。
- 优点：落实 spec「测试/生产配置、数据库、密钥严格隔离」；CI 用同一份定义（或 GH Actions services 等价配置）；测试数据随栈销毁。
- 缺点/成本：额外约 1–1.5GB 内存，需要按需启停；多一份 compose 维护。
- 风险：16GB 开发机同时开 dev(rag/parser)+test 会吃紧 → 文档写明"跑测试栈时先停 rag/parser"。
- 对 spec/design 影响：无（落实既有要求）。

**方案 B：复用 dev 栈 + 命名隔离**

- 做法：同一 PG/Redis/MinIO/Qdrant，仅用独立库名/DB 号/桶前缀区分。
- 优点：零额外内存。
- 缺点/成本：隔离不彻底（共享实例与资源上限），误操作面大；CI 仍需另一套 services 配置，等于两套写法。
- 风险：测试数据与开发数据互相污染。
- 结论：不推荐（不满足 spec 隔离要求的精神）。

### 2.2 CI 形态

**方案 A（推荐）：GitHub Actions 工作流 + 本地等效脚本**

- 做法：`.github/workflows/`：`backend.yml`（ruff + mypy + pytest，带 Redis/PostgreSQL service，`TEST_REDIS_URL=…:6379/15` 配合一次性 DB 护栏）、`frontend.yml`（pnpm install/lint/typecheck/vitest/build/audit + Playwright）、`evals.yml`（ruff + pytest）、`security.yml`（gitleaks + bandit + pip-audit）；另加 `scripts/ci.sh` 把同一批步骤在本地（Git Bash）串起来。
- 优点：远端就绪即生效；本地脚本先证明步骤可跑；TD-047/035/052 有落点。
- 缺点/成本：当前无远端仓库，工作流无法真正执行（只有本地脚本可验证）；Playwright 需浏览器缓存。
- 风险：Windows 本地与 ubuntu runner 差异（行尾/路径）→ 显式 `working-directory` 与 `bash` shell。
- 对 spec/design 影响：无（平台已由 A11 锁定 GitHub Actions）。

**方案 B：只做本地脚本，CI 延后**

- 拒绝理由：TD-047 就是"缺 CI"，且平台已锁定，早写早受益。

### 2.3 CPU 嵌入/重排基准范围

**方案 A（推荐）：最小基线**

- 做法：`benchmarks/embed_rerank_bench.py`（httpx 直连 TEI）：embed 单条与批量 8/32 的 p50/p95 与吞吐；rerank 8/32 候选的 p50/p95；每项重复 30 次；结果写 `docs/benchmarks-m0.md`（含机器规格与"非门禁"声明）。
- 优点：半小时内完成，为 M3 切块/批处理与阿里云实例档位提供数量级概念。
- 缺点/成本：单机单次，不代表生产（文档标注清楚）。
- 风险：低。

**方案 B：完全不做基准**

- 拒绝理由：M0 计划本身包含此项，且 M3 的切块/批量参数需要至少一个数量级依据。

### 2.4 TEI 模型离线预置（TD-010）

**方案 A（推荐）：预取脚本 + 生产镜像阶段收口**

- 做法：`deploy/tei/prefetch.py`（`huggingface_hub.snapshot_download` 按已固定 revision 下载 BGE-M3 / bge-reranker-v2-m3 到 `tei_models` 卷；支持 `HF_ENDPOINT` 覆盖）；文档说明 CI/生产用同一脚本预热；生产镜像烘焙与 TD-054（MinerU）一起在 M6 收口。
- 优点：现在即可离线准备；不引入 4.5GB 级新镜像；与 MinerU 生产分发统一。
- 缺点/成本：预取时仍需能访问 HF 或可用镜像源。
- 风险：`hf-mirror.com` 不返回 ETag（已实测），预取需直连或 modelscope；失败时退回现网下载。

**方案 B：自建烘焙权重的 TEI 镜像**

- 缺点：镜像 +4.5GB、构建慢、升级成本高 → 更适合生产阶段（M6）。

**方案 C：保持现状仅文档化**

- 拒绝理由：TD-010 就是要求解决，不能原样延后。

## 3. 选择结论

- 选择（2026-10-20 用户确认）：**2.1 方案 A**（独立 test 栈）、**2.2 方案 A**（GitHub Actions 工作流 + 本地等效脚本）、**2.3 方案 A**（最小基线基准）、**2.4 方案 A**（TEI 预取脚本 + 生产烘焙 M6 收口）。
- 理由：落实 spec 隔离要求（独立测试栈）；按已锁定平台（GitHub Actions）建 CI，本地脚本先证明步骤可跑；基准只留数量级依据、不做调优；TEI 离线预置用脚本解决、不在 M0 引入 4.5GB 级镜像。
- 是否需要改 spec/design：否（均为落实既有约定）。

## 4. 实现顺序与 TDD 测试清单

1. **测试基础设施**：`pytest --strict-markers` + `pytest-timeout`；`tests/conftest.py` 集中 `FAKE_*` 假密钥（TD-052）；`tests/support/` 提供 HTTP/DB/Redis fixture；独立测试栈（若选 2.1A）。
2. **应用最小收口**：
   - `/health/ready`：并发探活 PostgreSQL（asyncpg `select 1`）、Redis（PING）、Qdrant（`/healthz` 或客户端），逐项状态 + 总体 200/503；未配置的依赖按"未配置"呈现（dev 友好），production 下必需项缺失即启动失败。
   - 日志：structlog `ProcessorFormatter` 接管 stdlib/uvicorn，输出单行 JSON（TD-002）。
   - 空壳 Loop：`POST /api/v1/campus/chat/turns`（`Accept: text/event-stream`）依次发 `meta`→`step`×N→`done{stop_reason}`，不调 LLM；由 `DEV_SKELETON_LOOP_ENABLED` 控制（生产默认 false → 404）；写入本地 trace 并在 `meta` 回传 trace_id。
3. **业务场景骨架**：BS-M0-01「健康检查→trace」：真实 PG/Redis → 调 `/health/ready` → 调空壳 SSE → 断言事件顺序（meta→step→done）、`stop_reason` 取值 → 断言本地 trace 有 turn 且审计链 `verify()` 通过 → 断言 Langfuse 关闭时不受影响。
4. **基准**：脚本 + `docs/benchmarks-m0.md`。
5. **CI**：工作流 + `scripts/ci.sh`；以本地跑通 `ci.sh` 为证据。
6. **前端 TD**：ESLint + Prettier + `lint` 脚本；api-client Vitest（尾斜杠归一、5s 超时、非 2xx 错误路径）；Element Plus 按需引入（`unplugin-vue-components`）；e2e 失败态（后端断开→页面提示）与接口 mock 用例；tsconfig 纳入 `playwright.config.ts`/`e2e`；`pnpm audit`。
   - 实现偏离：原计划的 `manualChunks` 被移除——它会保留 Element Plus 包入口的再导出，反而抵销按需引入（详见 §9 执行结果与 TD-031）。
7. **验收报告**：`docs/m0-acceptance.md`。

- 红→绿→重构测试清单：
  - [ ] 单元：ready 逐项判定（依赖在线/离线/未配置）、SSE 事件序列与 `stop_reason`、日志 JSON 形状、api-client TS 单测
  - [ ] 集成：真实 PG/Redis 的 `/health/ready`、SSE 端点（httpx 流式读取断言）
  - [ ] 业务场景：BS-M0-01（断言业务输出：事件序列、trace 落库/trace sink、审计链）
  - [ ] E2E：admin/campus 页面 + 失败态
  - [ ] 性能：基准脚本产出数字（非门禁）

## 5. 测试策略

| 层级 | 内容 |
|---|---|
| 单元 | ready 判定表、SSE 序列、日志格式、api-client 行为、共享假密钥 |
| 集成 | 真实 PG/Redis 的 ready 与 SSE；CORS/代理不受影响 |
| 业务场景 | BS-M0-01：健康检查→空壳 Loop SSE→本地 trace 必发→Langfuse 关闭不影响 |
| E2E | Playwright：页面渲染 + 后端断开失败态 |
| AI 评估 | 不涉及（空壳 Loop 无 LLM） |
| 性能 | 基准记录（embed/rerank p50/p95），明确非门禁 |

## 6. 预判技术债

| TD 编号 | 条目 | 处置 |
|---|---|---|
| TD-002/005/010/029/030/031/033/034/035/047/052 | 台账中指向 M0-F 的条目 | 本功能点内逐条收口或改期 |
| 新增（预判） | 无远端仓库时 CI 工作流只能本地等效验证 | 记录为"待远端生效" |
| 新增（预判） | Windows 本地与 ubuntu runner 环境差异 | 工作流显式 `bash` + `working-directory`，本地 `ci.sh` 覆盖 |
| 新增（预判） | Playwright CI 浏览器缓存成本 | 缓存 `~/.cache/ms-playwright` |
| 新增（预判） | 空壳 Loop 无认证（dev-only 开关） | M1 接入认证后替换并移除开关 |

## 7. 高风险确认

- 是否属于高风险功能：**部分**——空壳 Loop SSE 与 `/health/ready` 部署面属"AI 护栏/SSE"与"部署"高风险清单的雏形，需重点审查（不展示原始 CoT、生产开关默认关闭、不泄露内部地址/密钥、503 语义正确）。
- 需要用户确认的点：2.1 测试环境隔离方式；2.2 CI 形态；2.3 基准范围；2.4 TEI 离线预置方式。
- 确认结论（2026-10-20）：四项均按推荐执行（A/A/A/A）；本功能点完成后照常走独立代码审查 + 修复 + 复测。

## 8. 审查对照

- 审查时重点验证：
  - `/health/ready` 逐项探活与 503 语义；production 下必需项缺失是否 fail-fast；
  - 空壳 Loop：事件顺序、`stop_reason`、生产开关默认关闭、`meta` 中 trace_id 与本地 trace 一致、不泄露内部信息；
  - 测试隔离是否真的独立（库/端口/桶/collection），测试数据是否可清理；
  - CI 与本地 `ci.sh` 步骤是否等价、缺服务时是否给出可读失败；
  - 基准数字是否有机器规格与"非门禁"声明；
  - 前端 lint/vitest/按需引入是否真正进入脚本与 CI（而非只有配置文件）。
- 允许的偏差：CI 工作流在无远端仓库时只做静态校验（本地 `ci.sh` 已执行）；基准数字不设阈值。

## 9. 执行结果（2026-10-20）

| 项 | 结果 | 证据 |
|---|---|---|
| 独立测试栈 | `deploy/docker-compose.test.yml` + `scripts/test-stack.sh`（postgres/redis/qdrant，minio 为可选 profile）；tmpfs 存储、独立端口 5xxxx | `scripts/test-stack.sh up` → 全部 healthy |
| 应用最小收口 | `/health/ready` 逐项探活（失败 503、只回显异常类型）；structlog/uvicorn 统一单行 JSON；空壳 Loop SSE（meta/step/done + error，字段对齐 design.md §5.8） | `tests/test_health.py`、`test_readiness.py`、`test_logging.py`、`test_skeleton_loop.py` |
| 业务场景骨架 | BS-M0-01：就绪 → 空壳 SSE → trace 必发（含中断路径）→ 只读工具审计链可校验 | `tests/business/test_bs_m0_01_health_to_trace.py`（3 用例） |
| 基准 | TEI embed 2.4–2.9 items/s、rerank 8 候选 p50 11.7s；批上限 8 | `docs/benchmarks-m0.md`、`benchmarks/results/m0_embed_rerank.json` |
| CI | 4 个 workflow + `scripts/ci.sh`（含 `--with-e2e`）；本地全量跑通 | `.github/workflows/`、`scripts/ci.sh` |
| TEI 离线预置 | `deploy/tei/prefetch.py`（HF 缓存布局，支持 `--volume`/`--target`/`--endpoint`） | 对 `campus-elf_tei_models` 卷执行成功 |
| 前端门禁 | ESLint/Prettier/Vitest/按需引入/e2e mock；admin JS 1.03MB→112KB、CSS 361KB→8.94KB | `pnpm lint/format:check/typecheck/test:unit/test:e2e` |
| 安全审计 | 修复 3 个真实漏洞（cryptography/pytest/aiosmtplib）；evals 侧 2 项无修复版本已登记 TD-060 | `pip-audit`、`pnpm audit` |

### 审查发现与修复（M0-F）

- **Blocker**：production 下 `DEV_SKELETON_LOOP_ENABLED=true` 曾被放行（未鉴权入口可上线）→ 配置校验新增 fail-fast + 单测。
- **Major**：`/health/ready` 遇非法 Redis DSN 抛 500 而非 503；生产启动报错回显 REDIS_URL（含口令，已启用 `hide_input_in_errors`）；空壳 Loop 在客户端断开/流内失败时不落 trace、无 error 事件（已补 try/except 与 `CLIENT_CANCELLED`/`STREAM_ERROR` 记录）；评估用例集校验口径弱于 README（已按类别固定期望，缺类别即失败）；测试栈 `wait_healthy` 可能假绿；Prettier 门禁未接入且当时红灯（已接入 CI/`ci.sh` 并全量格式化）。
- **Minor**：`_trace_sink` 静默改道（改为显式失败）、内存 sink 有界化、业务场景复用一次性 DB 护栏、`ci.sh` 与 CI 步骤对齐（`uv sync --frozen`、`--with-e2e`、去掉重复 `-q`）、gitleaks 白名单收窄、`.env.example` 补开关、SSE 字段对齐契约、测试栈 env 输出与 minio 预检查等。
