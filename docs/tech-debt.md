# 更好做法与技术债台账（tech-debt.md）

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-10-20 |
| 状态 | 生效；每条必须有处置 |
| 上游 | 功能点完成汇报、代码审查 findings、ADR 备选方案 |
| 规则 | 任何“更好的做法”不得只口头说明；必须登记并给出处置 |

---

## 1. 闭环规则

1. **登记时机**：每个功能点完成汇报时必须把本次提出的“更好做法/替代方案”登记为 TD 条目；代码审查发现的改进项同样登记。
2. **处置类型**：
   - `已完成`：本次或后续功能点已落地，附 commit/功能点。
   - `已采纳`：确认要做，指定目标里程碑；进入该里程碑范围或 spec/design 变更。
   - `已延后`：确认有价值但暂不做，写清原因与复查时点。
   - `已拒绝`：明确不做，写清理由（例如与需求冲突、成本大于收益）。
3. **里程碑边界评审**：每个里程碑结束时评审台账；未处置条目不得进入下一里程碑；`已采纳` 条目必须进入目标里程碑的 milestone/spec/design。
4. **优先级**：安全、租户隔离、合规、数据一致性类条目默认优先；低成本质量项可批量进入 M0-F。
5. **验收**：`已完成` 条目必须能被测试或运行验证；`已采纳` 条目必须在 milestone.md 中有对应功能点或验收编号。
6. **禁止静默丢弃**：任何被删除的条目必须在审查记录中说明原因。

---

## 2. 台账

| ID | 来源 | 更好做法 | 处置 | 目标 | 证据/理由 |
|---|---|---|---|---|---|
| TD-001 | M0-A | 用 `create_app()` 工厂 + lifespan，避免导入即配置副作用 | 已延后 | M1 | 当前单进程入口可接受；接 DB/Redis 生命周期时统一改造 |
| TD-002 | M0-A | 统一 uvicorn/stdlib 日志为 JSON（structlog ProcessorFormatter） | 已完成 | M0-F | `configure_logging` 用 ProcessorFormatter 接管 stdlib 并清空 uvicorn handler；`tests/test_logging.py` 断言单行 JSON |
| TD-003 | M0-A | 版本号单一来源（importlib.metadata） | 已延后 | M1 | pyproject 与 Settings 重复 `0.1.0` |
| TD-004 | M0-A | 生产可关闭 `/docs`、`/redoc` | 已完成 | M0-A | `docs_enabled` 配置已落地（c3e53bb） |
| TD-005 | M0-A | `/health/ready` 检查 DB/Redis/Qdrant 依赖 | 已完成 | M0-F | 逐项探活（ok/not_configured/error）+ 失败 503，只回显异常类型不回显凭证；单元/业务场景覆盖 |
| TD-006 | M0-A | 收紧 mypy `ignore_missing_imports` | 已延后 | M1 | 先保证全绿；第三方 stub 稳定后再收紧 |
| TD-007 | M0-B | MinIO 运行时改用最小镜像（非 Go 全量镜像） | 已延后 | M6 | 已非 root；镜像体积 553MB 偏大 |
| TD-008 | M0-B | Redis 密码改用 config/secret，避免 argv 可见 | 已延后 | M1 | 当前仅 loopback；接生产前必须改 |
| TD-009 | M0-B | Postgres 显式 shared_buffers/max_connections 调优 | 已延后 | M1 | 当前仅 `shm_size` |
| TD-010 | M0-B | TEI 模型离线预置/支持 ETag 的镜像源 | 已完成 | M0-F | `deploy/tei/prefetch.py`（按固定 revision 预取到卷/目录，支持 HF_ENDPOINT）；生产镜像烘焙并入 TD-054（M6） |
| TD-011 | M0-B | ClamAV 固定精确版本 tag，而非 `stable` digest | 已延后 | M6 | 已用 digest 固定内容；tag 语义浮动 |
| TD-012 | M0-B | MinerU 4.0.12 镜像构建 + V1 API + 6GB 档位降级验证 | 已完成 | M0-E | 自定义 torch 镜像构建成功；standard/basic/flash 三档真实解析通过，显存 <500MiB 未降级 |
| TD-013 | M0-B | 默认只启 core，rag/parser 分开；文档与 compose 对齐 | 已完成 | M0-B | bbc2bfd 已修文档与 profile 口径 |
| TD-014 | M0-B | MinIO 构建注入 Version/ReleaseTag/CommitID | 已完成 | M0-B | 镜像 `--version` 已显示 RELEASE（bbc2bfd） |
| TD-015 | M0-C | `audit_level`/`idempotent`/`redacted_summary` 接入执行策略 | 已延后 | M4 | 当前所有工具执行都审计 |
| TD-016 | M0-C | 只读工具强制显式非空 input/output schema | 已采纳 | M1 | 当前 schema 默认 `{}`，契约强度不足 |
| TD-017 | M0-C | 安全上下文嵌套容器改为不可变映射 | 已延后 | M1 | `frozen=True` 对 `feature_flags` dict 无效 |
| TD-018 | M0-C | AuditSink/TraceSink 改 async 或 worker，避免阻塞事件循环 | 已采纳 | M1 | 落 PostgreSQL 前必须决策 |
| TD-019 | M0-C | `verify()` 支持外部锚点/空链语义；生产 WORM | 已延后 | M1 | 已支持 expected_head/count；空链仍 True |
| TD-020 | M0-C | Prompt 版本语义排序/latest 方法 | 已延后 | M1 | 当前字符串排序，v10/v2 有歧义 |
| TD-021 | M0-C | 移除 `E501` ignore | 已拒绝 | — | 中文 Prompt/文档长行需要；改动收益低 |
| TD-022 | M0-C | 区分 handler 自抛 TimeoutError 与 wait_for 超时 | 已延后 | M1 | 当前统一 TOOL_TIMEOUT；影响较小 |
| TD-023 | M0-C | trace/audit sink 故障隔离策略 | 已延后 | M1 | 当前任一 sink 异常会冒泡；审计策略需明确 |
| TD-024 | M0-C | `CapabilitySpec.permission` 必填或显式 public | 已延后 | M1 | 当前默认 None 即公开，属 fail-open |
| TD-025 | M0-C | 拆分外部 DTO 与内部 `AgentRequest`，TenantContext 只由认证层注入 | 已采纳 | M1 | 接 HTTP API 时必须落地 |
| TD-026 | M0-C | 审计/trace 落 PostgreSQL 仅追加表 + WORM 导出 | 已采纳 | M1/M6 | 当前内存实现不承担生产审计 |
| TD-027 | M0-C | 注册期 `check_schema` + 运行期 schema 异常降级 | 已完成 | M0-C | b15beff 已修并测试 |
| TD-028 | M0-C | `write_turn` 嵌套 spans 脱敏 | 已完成 | M0-C | b15beff 已修并测试 |
| TD-029 | M0-D | 补齐 ESLint/Prettier/lint 脚本并纳入门禁 | 已完成 | M0-F | 扁平配置 `eslint.config.mjs` + `.prettierrc.json`；`pnpm lint/format:check` 进 CI 与 `scripts/ci.sh` |
| TD-030 | M0-D | 为 api-client 增加 Vitest 单测 | 已完成 | M0-F | `packages/api-client/src/index.test.ts` 5 用例（尾斜杠归一、同源路径、非 2xx、超时中断、网络错误） |
| TD-031 | M0-D | Element Plus 按需引入 + manualChunks，降低 admin 1MB 包体 | 已完成 | M0-F | 按需引入生效：admin JS 1.03MB→112KB、CSS 361KB→8.94KB；实测 manualChunks 会让入口再导出无法摇树（914KB），已移除并记录 |
| TD-032 | M0-D | Vant 按组件引样式，降低 H5 CSS 体积 | 已延后 | M6 | 当前全量 199KB CSS |
| TD-033 | M0-D | e2e 增加失败态/接口 mock；CI 下不复用已有服务 | 已完成 | M0-F | 新增 `admin-health.spec.ts`（mock 200/503/悬挂超时三种分支）；`reuseExistingServer: !CI`；vite preview 补 proxy 与 dev 一致 |
| TD-034 | M0-D | `playwright.config.ts`/`e2e` 纳入 tsconfig 检查 | 已完成 | M0-F | 新增根 `tsconfig.json`（含 playwright.config.ts 与 e2e），`pnpm typecheck` 串行执行 |
| TD-035 | M0-D | CI 增加 `pnpm audit` 依赖漏洞扫描 | 已完成 | M0-F | security workflow + `scripts/ci.sh`；官方源实测 No known vulnerabilities found（本地镜像源不支持 audit endpoint，脚本自动切换） |
| TD-036 | M0-D | 用 OpenAPI 生成客户端替换手写 api-client | 已采纳 | M1 | 后端 OpenAPI 就绪后生成 |
| TD-037 | M0-D | 前端环境变量放到 `frontend/.env`，根 `.env` 不生效 | 已完成 | M0-D | c4d4dfa 新增 frontend/.env.example |
| TD-038 | M0-D | `/health` 与 `/api` 同源代理，避免 CORS | 已完成 | M0-D | c4d4dfa 已修 Major |
| TD-039 | 流程 | 历史 M0-A~M0-D 未做“实现前设计简报” | 已拒绝 | — | 历史功能点已交付并审查；M0-E 起强制先简报后编码，不回溯 |
| TD-040 | 流程 | 实现前设计简报门禁 | 已完成 | — | 本次更新 AGENTS/checklist/milestone/design-briefs |
| TD-041 | M0-E | jieba 0.42.1 在 Python 3.13 产生 SyntaxWarning；评估 pkuseg/HanLP 等替代 | 已延后 | M3 | 测试按类别过滤（`ignore::SyntaxWarning`；带 module 限定的写法对包内文件无效，已修正）；切块阶段统一评估分词器 |
| TD-042 | M0-E | ragas 0.4.3 与 langchain-community 0.4 不兼容（vertexai 模块被移除） | 已完成 | M0-E | 独立 `evals/` 项目 + `langchain-community<0.4`，导入通过 |
| TD-043 | M0-E | 官方 MinerU Dockerfile 基于 vLLM，要求 8GB+ 显存，6GB 不适用 | 已采纳 | M0-E | 改 `python:3.13-slim + mineru[torch]` 自定义镜像（VLM 引擎 `auto`→llama-cpp、模型源 `auto`、非 root）；运行期 tier 验证后收口 |
| TD-044 | M0-E | Langfuse 官方 compose 的 DATABASE_URL 默认值不随 POSTGRES_PASSWORD 变化 | 已缓解 | M6 | M0-E 已改为由 `POSTGRES_*` 拼装 `DATABASE_URL` 并 fail-fast；生产观测主机部署时再收口 |
| TD-045 | M0-E | MinerU 镜像内 `mineru[torch]` 的传递依赖（torch/transformers 等）未固定版本 | 已采纳 | M3 | 实测 `docker images` 10.5GB、容器内 site-packages 6.2GB（nvidia 3.2GB + torch 1.2GB）；建议预装固定 torch 版本（指定 index）并加 PyPI 镜像 |
| TD-046 | M0-E | MinerU `/v1/health` 只声明能力，不校验模型是否可用 | 已完成 | M0-E | 确认该行为；E3 验收链路补真实 `/v1/file_parse` 冒烟，健康检查不作为可用性证据 |
| TD-047 | M0-E | 缺 CI 导致 Arq 集成用例默认 skip，真实执行证据不自动复现 | 已完成 | M0-F | 4 个工作流（backend/frontend/evals/security）复用 `deploy/docker-compose.test.yml` 起独立测试栈并导出 TEST_*；本地等效 `scripts/ci.sh` 已跑通（远端生效待 TD-057） |
| TD-048 | M0-E | 观测栈镜像仅按 tag 固定，未 pin digest（与 core/rag 策略不一致） | 已采纳 | M6 | 生产观测主机部署时统一 pin digest |
| TD-049 | M0-E | Langfuse 复用 MinIO root 凭据（同一凭据既是 S3 access key） | 已延后 | M6 | 生产观测主机部署时改用专用用户 + 最小权限策略 |
| TD-050 | M0-E | 是否把 langgraph/langchain-core/openai 移入 optional 依赖组 | 已拒绝 | — | M0-E 简报已批准在 M0 验证 agent 组兼容性；M4 立即使用，拆组会扩大改动面 |
| TD-051 | M0-E | 依赖测试只断言导入与版本，不覆盖运行时 API 漂移 | 已采纳 | M1 | 接 LangGraph/OpenAI 客户端时补最小功能探针（两节点 StateGraph、实例化 AsyncOpenAI 不发网） |
| TD-052 | M0-E | 测试中的假密钥常量易被 secret 扫描误报 | 已完成 | M0-F | `tests/support/fakes.py` 集中 `FAKE_*`；`.gitleaks.toml` 白名单；security workflow 跑 gitleaks/bandit/pip-audit/pnpm audit |
| TD-053 | M0-E | MinerU 镜像 10.5GB（`mineru[torch]` 拉入 6GB+ CUDA 运行库） | 已延后 | M3 | 若 GPU 小模型加速收益低于体积成本，改 CPU-only torch 或改用官方 slim 变体 |
| TD-054 | M0-E | MinerU 生产分发形态：镜像推私有 registry + 模型离线预置 | 已采纳 | M6 | 生产不从公网重建镜像：push 到私有 registry 并预置模型（modelscope 可达）到卷/镜像，避免上线时拉 PyPI/HF |
| TD-055 | M0-E | 自建镜像 vs 官方 vLLM 路径的解析质量对照未做 | 已采纳 | M3 | 同软件同模型，但 VLM 走 CPU llama-cpp + GGUF(Q8_0) 与官方 GPU vLLM 全精度不同；M3 用真实校园文档（表格/公式/扫描/多栏）做质量对照与回归，必要时升精度或换后端；已有夹具 `backend/tests/fixtures/mineru_table_cn.pdf` |
| TD-056 | M0-F | CPU 重排延迟无法进入快路径预算（8 候选 p50≈11.7s vs 首 token P95≤3s） | 已采纳 | M3/M4 | 实测见 `docs/benchmarks-m0.md`；需在 M3/M4 从「减少候选 / 提高 CPU 配额 / 换更快模型 / GPU TEI / 仅 ReAct 路径重排」中决策并用评估与压测验证 |
| TD-057 | M0-F | CI 工作流尚无远端仓库，未真实执行 | 已完成 | M0-F | 远端 `huluqiwa/campus-elf` 已生效：首次运行修复 4 处配置问题（GITHUB_ENV 引号、evals 脚本路径、pnpm allowBuilds、gitleaks 首推区间）后 4 条工作流全绿 |
| TD-058 | M0-F | 本地 npm 镜像不支持 audit endpoint | 已完成 | M0-F | `scripts/ci.sh` 检测后自动切 registry.npmjs.org；CI 默认官方源 |
| TD-059 | M0-F | 空壳 Loop 无认证/无真实 LLM，仅骨架 | 已采纳 | M4 | 由 M4 真实 Loop 替换并移除 `DEV_SKELETON_LOOP_ENABLED`；生产默认关闭（404） |
| TD-060 | M0-F | `ragas`/`diskcache` 存在无修复版本的上游漏洞（PYSEC-2026-3046/2447） | 已延后 | 监控 | 两者均为上游最新版（ragas 0.4.3、diskcache 5.6.3）；evals 为隔离的本地评估项目、不进生产镜像；CI pip-audit 已显式 ignore 并在上游发版后移除 |
| TD-061 | M0-F | production 未强制关闭 `/docs`（OpenAPI 文档） | 已采纳 | M6 | 生产部署检查项加入 `DOCS_ENABLED=false`；当前仅可关闭，未做强制校验 |
| TD-062 | M0-F | `/health/ready` 每次探活新建连接（无池化） | 已采纳 | M1 | 接 SQLAlchemy/Redis 连接池后改为复用，避免高频探针连接 churn |
| TD-063 | M0-F | 空壳 Loop 忽略 `Accept: text/event-stream`；api-client 超时不可注入 | 已采纳 | M4 | 真实 Loop 落地时按契约返回 406 或文档化；e2e 悬挂用例改用可注入超时，去掉时序依赖 |
| TD-064 | M0-F | e2e job 每次安装 Chrome 约 4.5 分钟（占单次 CI 大部分额度） | 已采纳 | M1 | 可缓存 Playwright/Chrome 或改用内置 chromium（与「与本地一致的 Chrome 通道」取舍）；亦可 e2e 仅在 main/手动触发时跑 |
| TD-065 | M1-A | 平台跨租访问缺专门通道 | 已取代 | — | 与 TD-072 重复，已合并到 TD-072 跟踪 |
| TD-066 | M1-A | 开发/生产尚未提供应用登录角色与三连接分离 | 已采纳 | M1-B | 已实现：迁移创建组角色 `campus_elf_tenant`/`campus_elf_platform`，应用侧支持 `DATABASE_URL`/`PLATFORM_DATABASE_URL`/`MIGRATION_DATABASE_URL`，测试栈自动创建登录角色并导出 `TEST_APP_DATABASE_URL`/`TEST_PLATFORM_DATABASE_URL`；dev/prod 仍需部署脚本建登录角色并配置三连接 |
| TD-067 | M1-A | refresh token 过期清理任务未实现 | 已完成 | M1-C | Arq 每日 03:30 清理过期/久已撤销行（保留期 30 天，`cleanup_refresh_tokens`），租户表按租户上下文逐租户删除；集成测试覆盖保留期边界 |
| TD-068 | M1-A | 审计哈希链校验脚本/只读接口未实现 | 已完成 | M1-F | `scripts/verify_audit_chain.py`（逐行验 prev_hash + HMAC + 未入链行检测）+ `GET /platform/audit-logs`（platform.audit.view + cursor 分页）；集成测试覆盖合法链/错误密钥/未入链行 |
| TD-069 | M1-A | `updated_at` 仅 ORM 侧 `onupdate`，非 ORM 更新不刷新 | 已完成 | M2 | ✅ M2-ORG02 已收口：导入进度/终态回写统一走 ORM update（`import_task._update_progress/_finish`）；全库非 ORM 更新路径已清零（组织模块探针亦改为仓储） |
| TD-070 | M1-A | `list_all` 用 limit/offset 分页（design §5.1 约定 cursor） | 已采纳 | M2 | ✅ M2-ORG01-03 已收口：仓储新增 `list_cursor`（keyset (created_at,id)），组织五接口全部 cursor 分页；`/school/personnel` 的信封迁移随 B 端页面功能点一并做 |
| TD-071 | M1-A | 尚无「零 skip」CI 断言（仅覆盖缺变量场景） | 已采纳 | M1 尾 | 可在 CI 解析 pytest 的 -rs 输出，断言白名单外 skipped=0 |
| TD-072 | M1-A | 平台跨租查询通道（二次授权+审计）未实现 | 已完成 | M1-E2 | 通道已交付：`app/personnel/cross_tenant.py`（仅平台 realm + `platform.cross_tenant.read`、事务第一条语句 `SET LOCAL TRANSACTION READ ONLY`、一次一个 `tenant_id`、理由必填、成功读取写 `platform.cross_tenant.read` 审计且审计失败则请求失败）+ `POST /api/v1/platform/cross-tenant/personnel/read`；口径 ② 的"最小通道"落地，**二次授权的工单化仍延后（TD-153）** |
| TD-073 | M1-A | 非 development 环境启动自检（角色非超级用户 / RLS 已启用）未实现 | 已完成 | M1-B | `app/db/selfcheck.py` + lifespan：同时校验**租户与平台**连接角色、逐表 FORCE RLS/策略表达式、审核函数 owner、无上下文不可见；测试覆盖平台超级用户与 users 关闭 FORCE RLS（空表）两侧 |
| TD-074 | M1-A | 认证层未断言「realm 与连接类型匹配」 | 已采纳 | M1-C | 平台 realm 只能用平台连接（`platform=True`）、租户 realm 只能用租户连接；一处误判即会拿到平台连接读到全量审计，须在 M1-C 用测试固定 |
| TD-075 | M1-A | 迁移/运维脚本对 RLS 租户表的读写语义 | 已完成 | M1-B | 迁移用 DDL 角色（测试为超级用户）执行；应用租户/平台路径全部走组角色成员。平台开通改为 SECURITY DEFINER 函数，策略/授权语义在迁移回调与「平台不能写存量租户」回归测试中固定 |
| TD-076 | M1-B | 公开注册接口无限流 | 已采纳 | M6 | 由 Nginx/网关限流与验证码策略收口；当前仅做字段/大小校验 |
| TD-077 | M1-B | 初始密码邮件同步发送，失败无重发入口 | 已采纳 | M5 | 事务内发送：失败回滚开通并返回 502；失败补写 `tenant.approve.email_failed` 审计。M5 用 outbox + 平台重发替换 |
| TD-078 | M1-B | 审核接口使用临时开发令牌 | 已完成 | M1-C | 平台账号登录 + `platform.tenant.review` 权限替换；`DEV_PLATFORM_REVIEW_TOKEN` 已从配置/示例/测试栈删除；无权限访问写 `auth.permission.denied` 审计 |
| TD-079 | M1-B | 资质凭证未做病毒扫描 | 已采纳 | M3 | 统一接 ClamAV 并补扫历史凭证（设计简报 §3.3 方案 A） |
| TD-080 | M1-B | S3/MinIO 对象存储路径无 CI 集成测试 | 已采纳 | M0-F/M6 | 测试用本地目录实现；CI 接入官方 MinIO 镜像或自建镜像构建后补 S3 集成用例 |
| TD-081 | M1-B | 审核人使用占位 UUID（无真实平台账号） | 已完成 | M1-C | 审核审计 `actor_id` 为真实平台账号；业务测试断言非空且来自登录主体 |
| TD-082 | M1-B | 校方内置角色仅超管有全量权限映射 | 已完成 | M1-E1 | `school_admin`/`teacher_b_end` 按 rbac-matrix v1.1/v1.2 落库（迁移 `e5f7a9c1b3d5` + `app/rbac/seeds.py` 同步），并补齐 C 端 `student`/`teacher`；一致性与幂等测试见 `tests/integration/test_rbac_sync.py` |
| TD-083 | M1-B | 审核快照在事务回滚时可能留下孤儿对象 | 已采纳 | M1-C/M6 | 快照在事务内 `copy`，邮件失败回滚后对象仍在；M5 outbox 改造时改为「事务提交后复制 + 失败清理」，并给 `reviews/` 配 S3 生命周期兜底 |
| TD-084 | M1-B | 平台列表接口无分页（固定 limit 100） | 已采纳 | M1-C | 注册申请/租户/审核记录列表在正式平台端上线时改 cursor 分页并加索引验证 |
| TD-085 | M1-B | 审计查询/导出未实现 | 已采纳 | M6 | 平台审计只读接口与导出（含脱敏与权限）在 M6 落地 |
| TD-086 | M1-B | S3 客户端未配置 SSE/超时/重试 | 已采纳 | M6 | 生产对象存储硬化：默认 SSE、连接/读写超时、有限重试与失败告警 |
| TD-087 | M1-B | 校名重复提示可用于枚举学校是否注册 | 已延后 | M6 | 用户确认需要该 UX 提示；由公开接口限流/验证码（TD-076）降低枚举效率，M6 复核是否需要改为模糊提示 |
| TD-088 | M1-B | SMTP 在事务内发送：最长持锁 10s；若邮件已发而提交失败，校方会收到无效密码邮件 | 已采纳 | M5 | 反向窗口不产生账号（回滚），重试生成新密码覆盖旧邮件，安全影响可接受但体验不佳；M5 换 outbox（提交后投递 + 状态回写）彻底消除 |
| TD-089 | M1-B | 平台列表返回完整联系邮箱 | 已采纳 | M1-C | 正式平台权限/审计落地后按最小必要展示（掩码 + 详情查看需权限与审计） |
| TD-090 | M1-B | 拒绝令牌审计可被无鉴权请求刷写（append-only 表膨胀） | 已采纳 | M6 | 由网关限流（TD-076）+ 失败审计采样/有界安全日志收口；当前每次失败一条 |
| TD-091 | M1-B | 平台角色仍可 INSERT audit_logs（M1-A 授权），可伪造审计事件 | 已完成 | M1-F | 哈希链触发器 + 校验脚本 + 启动自检（触发器存在且 owner 可绕过 FORCE RLS）三层落地。残留：持密钥伪造的入链行无法被链校验区分（TD-162）；未哈希字段（ip/ua/metadata）篡改不可检测（TD-163，M6 外部锚点收口） |
| TD-093 | M1-B | 平台仅有 `tenants` 列级 `UPDATE(status,status_reason)`，状态变更无审计 | 已采纳 | M1-C/M6 | 改为经 SECURITY DEFINER 状态变更函数（内部写审计）或撤销该列级授权 |
| TD-094 | M1-B | 公开注册用同一平台连接 INSERT 申请单，平台凭据可绕过接口直接插入伪造申请 | 已完成 | M1-C | `submit_tenant_registration()` SECURITY DEFINER 负责去重/落库/审计；平台 INSERT 权限已撤销。残余：平台凭据仍可直接 EXECUTE 该函数（绕过公开接口的对象上传与体积校验），随 TD-092 在 M1-E 一并按"平台审核/注册参数最小化"收口 |
| TD-095 | M1-B | 降级到 c688 会恢复 `provision_tenant` 的平台 EXECUTE（无审计开通路径） | 已延后 | 运维手册 | 这是 c688 的历史状态；降级操作手册需标注风险，且生产默认只前滚不回滚（M6 部署手册收口） |
| TD-096 | K5 | 派生版本产生新 Qdrant collection，向量存储随编辑次数增长 | 已采纳 | M3 | M3 实现块编辑时评估"单 collection + 版本字段过滤"或定期回收未发布派生 collection；先记录成本 |
| TD-097 | K4 | 无逐请求"无权命中"审计（过滤在 Qdrant 层完成，应用无法得知被过滤内容） | 已延后 | M6 | 合规若要求"越权尝试审计"，需额外一次未过滤检索（成本+隐私），届时评估采样或仅对高风险租户开启 |
| TD-098 | K4 | 人群解析缓存与组织/权限变更的一致性（失效机制未实现） | 已采纳 | M3/M4 | 用短 TTL + 变更主动失效（Redis 版本号）；实现时验证"调岗后立即按新部门过滤" |
| TD-099 | K5 | `kb_embedding_cache` 向量复用缓存的失效与回收（模型升级、租户退租） | 已采纳 | M3 | 缓存键含模型版本；模型/切块参数变更时按版本失效；退租清理纳入生命周期 |
| TD-100 | M1-C | 每个认证请求都查库读权限（未缓存） | 已延后 | M6 性能专项 | M1-E1 用户确认改判（简报 §9-5）：本期不做缓存，先以 `tests/integration/test_rbac_sql_baseline.py` 钉住成本上界（一次带鉴权请求 ≤8 条 SQL，实测 5；权限声明本身 0 条）。引入短 TTL 缓存必须同时保留"变更下一次请求生效、不作废在线会话"与撤销即时性测试，届时对着这条基线评审 |
| TD-101 | M1-C | JWT 密钥轮换只支持单个 active kid（无并存旧 kid 校验） | 已延后 | M6 | 生产上线前补"双密钥并存"（旧 kid 仅校验、新 kid 签发）与轮换演练 |
| TD-102 | M1-C | 登出后的访问令牌撤销依赖 Redis 可用性 | 已延后 | M6 | Redis 不可用时登出仅撤销刷新令牌（访问令牌最长 15 分钟自然过期）；M6 监控与降级策略中明确 |
| TD-103 | M1-C | 前端令牌存储（access 内存 + refresh HttpOnly Cookie）与三端登录页未实现 | 已采纳 | M1-C-UI | 后端契约已就绪；前端登录页/改密页/路由守卫作为 M1-C-UI 子项实现并补 Playwright 主路径 |
| TD-104 | M1-C | 登录失败锁定/验证码未实现 | 已采纳 | M1-D | 属 AUTH-05；失败已写审计，M1-D 加计数与锁定 |
| TD-105 | M1-C | `certifi 2026.10.18` 命中 GHSA-6q48-6c7w-9gw2 | 已完成 | M1-C | backend/evals 锁文件均升级到 `certifi 2026.10.19`；临时忽略已删除；本地 pip-audit 复验通过 |
| TD-107 | M1-C | bcrypt 72 字节输入上限与 spec"最大 128 字符"冲突 | 已完成 | M1-D1 | 用户确认保留 bcrypt cost 12，spec/design 明确"10~128 字符且 ≤72 UTF-8 字节"；登录/改密/重置入口均按字节拦截（改密/重置 422，登录统一凭证错误），不再 500；argon2/预哈希不进入当前计划 |
| TD-110 | M1-C-UI | E2E 用接口 mock，未跑真实后端全栈链路 | 已完成 | M1 验收 | ✅ M1 验收已收口（2026-10-24）：专用 compose 栈 `deploy/docker-compose.e2e.yml` + `scripts/e2e-stack.sh` + `backend/scripts/e2e_seed.py`，`frontend/e2e-fullstack/` 12 例真实链路全绿（登录→改密→业务接口），见 `docs/m1-acceptance.md` §2 |
| TD-109 | M1-C | 启动自检的矩阵校验逐租户串行开会话（租户数百级时拖慢启动） | 已延后 | M1 尾/M6 | 当前量级无影响；租户规模上量后改为单次聚合（平台侧只读聚合视图或 SECURITY DEFINER 统计函数）或分批+告警 |
| TD-108 | M1-C | 多标签/重试类并发刷新会触发 reuse_detected 并强制全家族登出 | 已延后 | M1-D/M4 | 语义上符合"重用即泄漏"的安全取舍；前端需保证同一时刻只用一个刷新令牌（TD-103）；若实际体验受影响，评估短宽限期或幂等重放窗口 |
| TD-106 | M1-C | bandit B608 无法在无网络环境复跑 | 已完成 | M1-C | 认证模块拼接 SQL 全部改为按 realm 的字面量语句表（无 f-string、无 nosec）；本地 bandit 复跑 0 issue；另发现本机代理会劫持 127.0.0.1 流量，测试与工具链统一 `NO_PROXY=127.0.0.1,localhost,::1` |
| TD-092 | M1-B | 平台凭据可直接调用审核函数（有审计，但可绕过服务层邮件流程） | 已延后 | M1-C | 平台凭据属受信运营方，函数内强制写审计；M1-C 正式鉴权落地时由权限矩阵约束调用入口并复核是否拆分"审核"与"通知"两步 |
| TD-111 | M1-C2 | 会话标记删除失败或 Redis 不可用时，被踢/已下线设备的访问令牌最长 15 分钟内仍可用（鉴权对 Redis 故障 fail-open） | 已延后 | M6 | 与 TD-102 一并收口：Redis 故障监控/告警、标记删除失败重试，并评估“会话撤销待删队列”或数据库兜底校验；当前仍由 token_version/账号状态与 15 分钟短 TTL 限制影响面 |
| TD-112 | M1-C2 | `last_seen_at` 仅在刷新时更新，设备“最久未用”排序粒度约 15 分钟；持续活跃但长期不刷新的设备可能被优先踢出 | 已延后 | M6 | 每请求写库成本过高，本期接受；若真实投诉集中，改为 Redis 记录最近活跃时间并异步回写，保留现有排序语义 |
| TD-113 | M1-C2 | 会话上限 `MAX_SESSIONS_PER_USER_TENANT/PLATFORM` 只能改环境变量并重启，不能按租户差异化 | 已采纳 | M6 | 平台配置中心落地后收敛为按租户/端可配，并补配置变更审计与即时生效测试 |
| TD-114 | M1-C2 | 多标签浏览器、隐私模式、清 Cookie 后重登会被视为新设备并占用额度（不做设备指纹） | 已延后 | M1-C-UI/M6 | 以设备列表文案与“退出其它设备”缓解；不引入指纹识别（隐私争议大于收益），若投诉集中再评估“同 UA 短窗口合并”策略 |
| TD-115 | M1-C2 | 新设备登录/异常登录提醒未实现 | 已采纳 | M5 | 属消息中心能力；届时由登录事件触发站内信/邮件，并补用户可关闭开关与审计 |
| TD-116 | M1-C2 | 登录/刷新在用户级 advisory lock 内执行 bcrypt 与会话标记写入：知道 login_id 的未认证请求可排队放大同账号延迟（Redis 故障时每次约 2s），并阻塞该账号刷新 | 已延后 | M6 | 会话标记必须在事务内登记（防止撤销后复活），bcrypt 在锁内保证改密/停用与签发原子；M1-D1 仅增加账号锁定与重置申请 IP 限流，**登录侧仍无限流**，排队放大面未消除；若尾延迟异常，评估锁前预校验 + 锁后按密码版本复核、Redis 标记改带超时的异步写入 |
| TD-117 | M1-C2 | Redis 会话标记键为 `{prefix}:auth:session:{realm}:{sid}`，不含 tenant_id（jti 撤销键同样不含） | 已延后 | M6 | 与既有 jti 键保持一致的取舍：sid/jti 均为数据库生成的 UUID，跨租碰撞不可利用，且 token 的 realm/tenant 在鉴权时另行校验；M6 统一缓存键规范时一并加租户段（含密钥轮换与旧键清理） |
| TD-118 | M1-C2 | 带 Bearer 的登出若刷新令牌属另一 family，撤销在独立事务先提交、第二 family 审计随后单独写入；审计插入失败时会出现“已撤销但无审计”（接口 500） | 已延后 | M6 | 正常路径已由跨租/跨用户用例锁定；彻底解决需把撤销与审计合并为同一事务（`_revoke_by_jti` 内按“是否当前 family”决定是否写统一事件），并补 platform↔tenant 跨 realm 登出矩阵与跨用户已撤销 family 用例；成本高于当前风险，先登记 |
| TD-119 | M1-D1 | 重置邮件在进程内 `BackgroundTasks` 发送：无持久化队列/重试，SMTP 慢会占用 worker，进程重启会丢任务（DB 令牌已随事务提交或回滚） | 已延后 | M5/M6 | 当前单机 Compose + 低量级可接受；消息中心落地时改为 outbox + worker（与 `outbox_events` 统一），补投递重试、死信与告警 |
| TD-120 | M1-D1 | 已用/过期重置令牌与密码历史行无定期清理，长期随申请/改密增长 | 已延后 | M1 尾 | 令牌表有 `expires_at` 索引，先保证查询不退化；M1 收口前加定时清理（过期或已用超过 N 天的令牌），历史表已按用户保留最近 5 条 |
| TD-121 | M1-D1 | 重置申请/确认限流为 Redis 固定窗口且 fail-open；共享 NAT 出口可能误伤，Redis 故障时无限制；429 `Retry-After` 返回整窗而非剩余 TTL | 已延后 | M6 | 限流是防滥用附加层，不能成为可用性单点；来源 IP 由 `TRUSTED_PROXY_IPS` + 覆盖式 XFF 解析，白名单/反代配置错误仍可能误限或绕过（部署文档约束）；M6 监控/网关层统一限流时改为令牌桶并按租户/账号维度补充，同时返回精确剩余 TTL，保留 Redis 故障告警 |
| TD-122 | M1-D1 | 前端 `passwordPolicyError` 与后端密码策略是两份实现 | 已拒绝 | — | 前端只做 UX 预校验，后端始终权威；策略由 spec 固定且两端各有单测，抽共享协议层成本高于收益；策略变更时以 design/spec 为准同步两处；大小写已改用 Unicode 二元属性、数字补齐上/下标，仍可能有极少数 Unicode 边界差异，由服务端最终裁决 |
| TD-123 | M1-D1 | 单个 `PASSWORD_RESET_URL_BASE` 无法同时指向 admin 与 campus：当前启用的员工/平台重置均走 admin，C 端学生重置 M2 开放后需要按 realm 或统一落地页分流 | 已延后 | M2 | 学生自助重置属 M2；届时增加按 realm 的链接模板或统一重置落地页，并补两端口 E2E；当前默认 `http://127.0.0.1:5173`（admin）与已启用流程一致 |
| TD-124 | M1-D2 | 未提供 WebAuthn/Passkey 等抗钓鱼第二因素，TOTP 可被实时中继钓鱼诱导 | 已延后 | M6 | 本期按 spec 只做 TOTP；Passkey 需前端/设备/证明链改造，超出 M1 范围；M6 安全增强时与"记住设备"一起评估 |
| TD-125 | M1-D2 | `ENCRYPTION_KEK` 无轮换与批量重加密工具；KEK 泄露需手工重绑全部 MFA | 已延后 | M6 | 密文已带 `v1.` 版本前缀与 realm+user AAD，为双密钥解密留出空间；轮换工具需"新旧 KEK 并行解密 + 重写"任务与演练，M6 密钥管理专项交付；当前破窗为平台 CLI `--reset-mfa`（租户用户需管理员重绑） |
| TD-126 | M1-D2 | 租户角色（校管/员工）无法强制 MFA，且租户侧无 MFA 重置后台 | 已完成 | M2-UI2 | 重置部分交付：`POST /api/v1/school/users/{user_id}/mfa/reset`（专用码 `school.personnel.mfa.reset`，按 login_id 精确定位、清绑定+清锁定+删恢复码+撤刷新令牌家族+`token_version+1`+Redis 清会话、理由必填且 ≥10 字符、审计落库、禁止重置超管）+ 配置页 MFA 重置卡片；业务场景 `tests/business/test_bs_m2ui2_mfa_admin.py`、E2E `e2e/admin-roles.spec.ts`。「租户侧自设强制 MFA 策略」未做（现由平台策略位 `mfa_required` 统一下发），另立 TD-186 |
| TD-127 | M1-D2 | 无"记住此设备"可信 Cookie，MFA 账号每次登录均需动态码 | 已延后 | M6 | 可信设备需签名设备凭证 + 有效期与撤销策略，安全边界复杂；先用恢复码兜底；M6 与 Passkey 一并设计 |
| TD-128 | M1-D2 | `/auth/mfa/verify`、`/mfa/activate`、`/mfa/disable` 无独立 IP/全局限流，仅依赖单挑战尝试上限与账号锁定 | 已延后 | M6 | 与 TD-121 同源：限流不应成为可用性单点；在线 TOTP 猜码（10^6/30s）由 ±1 窗口 + 单挑战 5 次 + 账号锁定限制；M6 网关统一限流时按 IP/账号/租户维度补 |
| TD-129 | M1-D2 | 恢复码只支持一次性生成，无"再生成/查看剩余数量"API 与 UI | 已延后 | M2 | 用尽后需平台 CLI 或重新绑定自救；M2 随校管后台补 `POST /auth/mfa/recovery-codes/regenerate`（需二次 MFA）+ 前端展示剩余数量与告警 |
| TD-130 | M1-D2 | 以邮件/短信一次性码作为备选第二因素 | 已拒绝 | — | 邮件通道与密码重置共用，攻击者拿下邮箱即拿下第二因素；短信有成本、合规与携号转网风险，且易受 SIM swap；强度低于 TOTP 而复杂度更高 |
| TD-131 | M1-D2 | 用 Fernet 或 PostgreSQL `pgcrypto` 替代应用侧 AES-256-GCM+KEK | 已拒绝 | — | Fernet 无 AAD，无法把密文绑定 realm+user（跨用户搬移密文可解密）；pgcrypto 需把密钥下放到 DB、明文/密钥进入 SQL 与语句日志，违反"密钥不入日志/DB"红线 |
| TD-132 | M1-D2 | 强制 MFA 策略由关转开时，已签发的仅密码 access token 最长 15 分钟内仍可用（refresh 已按 `mfa_verified` 拒绝并撤销家族） | 已延后 | M6 | 策略变更时无会话登记表可即时踢出；M6 做"策略变更即时撤销"（按 realm 递增全局凭据版本并校验）后消除该窗口；部署文档已要求低峰开启并提示 15 分钟收敛 |
| TD-133 | M1-D2 | 待激活 TOTP 密钥只存 Redis（TTL 10 分钟，库内无记录）：Redis 重启/故障会打断绑定流程（fail-closed，需重新发起），同一用户重复发起会覆盖旧密钥 | 已延后 | M6 | 避免为此新增 DB 列、清理任务与"半成品密钥"状态机；密钥本身已用 AES-GCM+AAD 加密后再入 Redis；与 TD-111/102 的 Redis 依赖专项一并收口 |
| TD-134 | M1-D2 | TOTP 重放防护按用户全局单调（`mfa_last_used_step`）：同一 30 秒窗口内第二次登录会被判为重放并失败 | 已延后 | M2 | 改为按会话/挑战作用域记录已用 step 可消除该 UX 抖动，但需在 Redis 维护"每挑战已用码"集合并重验并发语义；当前有 ±1 窗口与恢复码兜底，属体验问题而非安全问题；M2 若真实反馈集中再改 |
| TD-135 | M1-D2 | 恢复码熵偏低（10 字符 × 32 字母表 ≈ 50 bit）：脱离上下文可离线爆破哈希 | 已延后 | M6 | 实际在线面已被三重限制：单用户 HMAC 子密钥隔离（跨用户不可复用）、`used_at` 单次消费、以及挑战尝试上限 + 账号锁定；符合本期 spec。M6 若做恢复码"再生成"（TD-129）时一并把长度提到 12 字符或加入校验位，并评估是否按 `argon2` 存摘要替代 HMAC-SHA256 |
| TD-136 | M1-E1 | 权限码全量落库但多数尚无接口使用（"码存在 ≠ 功能存在"）：59 码里本期只有 3 个挂到真实路由（`campus.chat.use`、`platform.tenant.view`、`platform.tenant.review`） | 已采纳 | M2/M3 | 矩阵是产品级权限规划，先落库才能配角色；随校管后台（M2）与 KB（M3）逐码收口，配 `rbac_check`（TD-142）比对"声明但未使用"的空转码 |
| TD-137 | M1-E1 | 角色↔权限分配无管理接口与 UI（只能靠 SQL/迁移） | 已完成 | M2-UI2 | 交付 `GET /school/roles`、`GET /school/permissions`、`PUT /school/roles/{id}/permissions`（全量替换 + `version` 乐观锁 409 带最新快照 + 目录外/平台域码 422 + 审计 diff + `school.role.manage` 声明）与配置页 `RoleSettingsView.vue`（权限按模块分组、增删差异预览、二次确认、保存后 `loadUser()` 热更新生效）；权限仍限 `school`/`campus` 目录，自定义角色与多角色绑定另立 TD-187 |
| TD-138 | M1-E1 | `data_scopes` 本期只落 `school`/`self` 语义且**无数据行**，`class`/`course` 未实现服务端过滤 | 已完成 | M1-E2 | E2 交付 school/self 的服务端强制过滤与数据行：出厂默认落 `rbac_builtin_role_scope_defaults`、新租户即写入、同步只补"还没有任何范围行"的角色，`Principal` 每请求解析范围（解析不到 = 空集 fail-closed），仓储基类注入谓词，越权 404 + `data_scope.denied` 审计；存量无行租户由 `assert_every_tenant_has_scope()` 在同步收尾 fail-fast（`tests/integration/test_data_scope_backfill.py`）。`class`/`course` 与任课关系另立 TD-151（M2/M3） |
| TD-139 | M1-E1 | 免鉴权路由白名单需随业务持续复核（新增接口可能被随手加进白名单） | 已采纳 | M2/M6 | 白名单已被 `PERMISSION_FREE_PREFIX_ALLOWLIST` 限定为「认证自助 / 健康探针 / 公开登记」三类并有测试兜底；第五轮审查另指出 `API_PREFIX/auth/` 整段免权限码，将来落在 `/auth/` 下的特权接口只要进白名单就只校验"已认证"——M2 新增 `/auth/*` 时必须逐条核对是否应改为按权限码校验，M6 安全评审复核理由文案 |
| TD-140 | M1-E1 | 权限矩阵变更缺"变更审批"流程（改代码即改权限，发版即生效） | 已延后 | M6 | 与 R7「配置双人确认」同源：M6 做变更单 + 第二人确认 + 生效前 dry-run 差异预览；本期靠 Git 评审与矩阵文档版本（v1.2）兜住 |
| TD-141 | M1-E1 | 前端未消费 `/auth/me` 的 `roles`/`permissions`，菜单仍全量展示 | 已延后 | M2 | 服务端已逐请求校验（前端隐藏不是安全边界），本期无越权风险；M2 校管后台按权限渲染菜单与按钮，并补"无权限入口不可见 + 直连仍 403"用例 |
| TD-142 | M1-E1 | 目录与"接口实际校验点"的一致性只有路由清单测试，没有反向比对（新增码却无处使用=空转，无人报警） | 已采纳 | M2 | 路由清单测试保证"接口都有权限"，不保证"权限都有接口"；M2 随后台做 `rbac_check`（矩阵↔目录↔路由声明三方比对报告），空转码进发布检查 |
| TD-143 | M1-E1 | 内置角色缺"重置为出厂默认"接口，误配只能手工改库 | 已完成 | M2-UI2 | 交付 `POST /school/roles/{id}/reset/preview`（只读，返回当前/出厂权限与数据范围差异、绑定用户数）+ `POST /school/roles/{id}/reset`（预览→确认，`confirm_role_code` 必须等于角色 code、理由 ≥10 字符、`version` 乐观锁、同时恢复权限与数据范围、审计 `school.role.reset`）；锁定角色/自定义角色统一 409 `ROLE_IMMUTABLE`（`lock_reason` 透出，绕过预览直连也被拒）、超管不可重置；预览不落审计由 `test_reset_preview_then_execution_restores_factory_defaults` 断言 |
| TD-144 | M1-E1 | 教师上传后"人群限本人/任课班级、扩全校需管理员确认"未落地 | 已采纳 | M2/M3 | 口径 ⑤ 本期只放开"持后台账号即可上传"；范围收敛依赖 M2 班级/任课关系与 M3 KB ACL，届时在构建入口做人群默认值 + 扩权审批 |
| TD-145 | M1-E1 | 权限映射变更即时生效、无双人确认（改错即全租户生效） | 已延后 | M6 | 与 TD-140 同源（R7）；本期风险由"只有迁移/SQL 能改映射、且必须发版"限制，M2 开放前台配置后必须先引入确认机制再放开 |
| TD-146 | M1-E1 | 全量型角色「只补不撤」：人工从 `school_super_admin`/`platform_super_admin` 收回的权限，会在下一次同步/发版被自动补回 | 已延后 | M2 | 矩阵把这两个角色*定义*成「范围内全量」（§1.2 + §4 定稿脚注 2026-10-23 + §5-11），若不补则新增权限码永远进不了超管视野；其余内置角色一律「未配置过才授默认」，人工改动不被覆盖（`tests/integration/test_rbac_sync.py` 锁住）。M2 配置界面若需长期压制某码，改为在库里维护"超管例外表"而非改变口径 |
| TD-147 | M1-E1 | 迁移 `downgrade` 把内置角色映射重置为「E1 之前的出厂状态」，不是「回滚前的真实状态」：E1 期间对系统角色的人工增授（及 C 端角色上的 `user_roles` 绑定）在回滚后丢失 | 已延后 | M1 尾/部署 | 逐租户快照恢复需要额外表与清理任务，本期回滚只承诺"结构可来回"（`test_migration_rbac_rollback.py` 覆盖 head↔downgrade↔head 重放）。生产回滚 SOP 必须先 `copy role_permissions/user_roles` 备份；M1 验收前把该约束写入 `docs/handoff.md` 部署注意（✅ 已写入 docs/handoff.md §5.1，2026-10-24） |
| TD-148 | M1-E1 | 路由清单依赖 FastAPI 内部结构（`_IncludedRouter`/`original_router`/`include_context.prefix`、`dependant.call` 上的标记），框架升级可能静默漏读路由 | 已采纳 | M6 依赖升级评审 | 已用 `test_manifest_sees_the_whole_app` 设下限（读到路由数 <20 或缺关键路径即红）把"静默失效"变成"测试失败"；升级 FastAPI/Starlette 时按该用例与白名单过期检查（`test_whitelist_has_no_stale_entries`）一并复核 |
| TD-149 | M1-E1 | DB 触发器 `role_permissions_realm_guard` 仍只按 `module = 'platform'` 拒绝入租户角色：`observability.*`/`eval.*` 可被手工 INSERT 进租户角色，而代码口径把它们算作平台运维域 | 已完成 | M1-E2 | 迁移 `f7a9b1c3d5e8` 与 E2 范围/职责分离迁移合并替换该函数：守卫改为 `module NOT IN ('school','campus')` 白名单（取值由 `catalog.TENANT_MODULES` 生成，与代码侧 `code_realm()`/启动自检同一份真相，不在 SQL 里抄字面量），`observability`/`eval` 不再能写入租户角色；正反两面由 `tests/integration/test_auth_login.py::test_db_guard_rejects_platform_domain_permission_for_tenant_role` 三码拒绝 + 放行钉住，downgrade 按名恢复 `b7d1e3f5a9c2` 原文 |
| TD-150 | M1-E1 | 出厂默认补授用「角色映射为空」当作「未配置过」的启发式：把内置角色的最后一条人工撤销掉，下一次同步会把默认值补回来 | 已完成 | M2-UI2 | 迁移 `c3f7a9d1e5b2` 给 `roles` / `platform_roles` 各加 `defaults_applied`（单标记，用户 2026-10-26 决策 D1）+ `roles.version` 乐观锁；存量行按「已出厂」全量置 true，同步改判 `defaults_applied IS NOT TRUE`，运营主动清空/撤掉最后一条从此不再被补回；列尚未建立的历史迁移重放保留旧启发式兜底（`use_defaults_marker=False` 分支，仅在 `defaults_marker_columns_exist` 为假时进入，无独立用例——登记 TD-185）；旧钉住行为改名并反转为 `tests/integration/test_rbac_sync.py::test_sync_does_not_reapply_defaults_after_role_is_emptied`，迁移成对 downgrade 见 `tests/integration/test_migration_rbac_admin_rollback.py` |
| TD-151 | M1-E2 | 数据范围只在仓储基类生效：`class`/`course` 与任课关系、`self` 的归属列语义要到 M2/M3 的组织与业务表才完整 | 已完成 | M2/M3 | ✅ class 已随 M2-ORG01-03 收口：`class_ids` 贯通 Principal→DataScopeContext，class 成员子查询工厂按模型注入（User=所辖班学生），同步函数保证范围行与关系一致；course 随 M3 业务表收口 |
| TD-152 | M1-E2 | `GET /api/v1/school/personnel` 本期没有前端入口，M1 只能后端取证 | 已采纳 | M2 | M2 人员页复用同一接口与同一范围谓词，并补 Playwright 用例（范围外不可见） |
| TD-153 | M1-E2 | 平台跨租只读通道是"二次授权弱化版"：只有权限码 + 必填理由 + 审计，没有预审批工单与有效期 | 已延后 | M6 或首个真实跨租需求 | 口径 ②：本期无跨租读的真实需求方；通道固定只读（`SET TRANSACTION READ ONLY`）、单租户、资源白名单，未来加工单表只扩不改 |
| TD-154 | M1-E2 | 「开启自批例外需通知平台审计」本期=审计事件（`platform.audit.view` 可复核），没有站内信/告警 | 已采纳 | M4 | 随 M4 消息模块接入平台运营通知；审计事件已含操作人/理由/租户 |
| TD-155 | M1-E2 | `system_settings` 无运营界面，且键在代码里白名单声明（加键要发版） | 已采纳 | M2 | M2 运营后台补配置页；键白名单是刻意的（未知键接口读不到也写不进，见 TD-160），不做动态注册 |
| TD-160 | M1-E2 | 未知键的**启动自检点名**未实现：库里被手工 INSERT 一个未声明的键时，应用层读不到它（`list_settings()` 只遍历 `SETTING_DEFS`）、也写不进（`put_setting()` 抛 `UnknownSettingKey`→404），但进程不会告警，运维无从发现这条"看不见的行" | 已采纳 | M2 | 与 TD-155 的运营后台一并做：在 `app/db/selfcheck.py` 加一条 `select key from system_settings where key = any(...)` 反向比对并在非 development 环境失败；本期先如实把口径写进 `app/settings/store.py` 模块 docstring，不让注释承诺没实现的事（第一轮自审发现） |
| TD-156 | M1-E2 | 自批例外的三道锁不等价：应用层与函数层的例外判定都会落 `approval.self_exception.used` 审计，而数据库 CHECK（`提交人为空 OR 审核人≠提交人 OR self_exception_used IS TRUE`）只挡"没带例外标记"的直写 | 已完成 | M1-E2 | 口径 ③「应用层 + 数据库约束」的必然形态；第三条款由例外功能逼出（2026-10-23 用户确认），本期用集成测试分别钉住三条路径，并写入矩阵 §5-4 与 `docs/design.md` §9；伪造直写的检测见 TD-161 |
| TD-157 | M1-E2 | `tenant_registration_requests.submitted_by_platform_user_id` 本期**没有真实写入方**：注册申请表仍由 C 端自助提交（无平台代提交人），三道锁的用例靠测试直插该列 seed | 已采纳 | M2 | 口径 ⑦ 的落点是"平台审核学校注册"，但"平台代提交/运营录入"入口随 M2 运营后台才有；届时写入方必须与 `review_registration_request()` 的复制语句同测验（否则提交人恒空、第一道锁空转）。`tests/integration/test_approval_separation.py:_seed_request` 的注释记录了这个后置依赖 |
| TD-158 | M1-E2 | 同一"越权"两类审计口径不一致：租户侧 `data_scope.denied` 是 best-effort（审计故障仍返回 404），平台跨租 `platform.cross_tenant.read` 是 fail-closed（审计写不进就请求失败）；且详情越权要多发一次"无范围探针"查询才能区分"不存在"与"存在但越权" | 已延后 | M2 或首个真实安全告警需求 | 取舍是刻意的：404 形状不能因审计故障变成 5xx（那会把基础设施故障泄给终端用户），而跨租读取本身属高危、不可审计即拒绝。探针成本已含在 SQL 基线余量里（上限 8，实测 6）。M2 接安全告警/看板时统一评估：是否改为"范围外 id 直接记 denial（不探针）+ 告警聚合" |
| TD-159 | M1-E2 | 简报 §5 承诺的"`/school/personnel` 1k 行分页 P95"本期未压测：只有 SQL 条数基线（`test_rbac_sql_baseline.py`，实测 6 / 上限 8）能证明成本有界，没有延迟分布数据 | 已采纳 | M6 | 随 NFR-01 的 k6 场景一并加入该接口（milestone §M6 压测清单）；本期不伪造延迟数字 |
| TD-164 | M1-F | 校验脚本 `--hash-key` 走 argv，进程列表/shell 历史可见 | 已采纳 | M6 | 改读环境变量 `AUDIT_HASH_KEY`；当前仅运维手动执行，风险低 |
| TD-165 | M1-F | 校验脚本不校验连接角色是否 BYPASSRLS（应用角色跑时 RLS 静默截断 → 假通过） | 已采纳 | M6 | 脚本启动时检查 `current_user` 并拒绝非超级用户/BYPASSRLS 连接 |
| TD-166 | M1-F | 查询接口 `total` 含游标条件（翻页后语义变为"剩余条数"） | 已延后 | M2 | 与审计查询页 UI 一并调整；当前首页 total 正确 |
| TD-167 | M1-F | 查询接口每次请求对 audit_logs 全量 count(*)，表只增不减 | 已采纳 | M6 | 量级上来后改物化计数或近似值；当前 <10k 行无感知 |
| TD-168 | M1-F | `CREATE TRIGGER` 无幂等保护（部分失败后重跑 upgrade 报已存在） | 已延后 | M2 | 迁移重跑场景罕见；需时改 DO 块判 pg_trigger |
| TD-169 | M1-F | `append_audit` 返回的 ORM 实例 `prev_hash/hash/hash_version` 是 flush 前过期值 | 已采纳 | M2 | 调用方需 refresh 或 RETURNING；M2 接审计查询页时一并处理 |
| TD-170 | M1 验收 | 全栈 E2E（`e2e-fullstack/`）未纳入远端 CI，当前仅验收期本机执行 | 已采纳 | M6 | frontend workflow 起 backend 容器需要镜像构建与依赖栈供给方案；随 M6 部署硬化一并决策，验收期以 `docs/m1-acceptance.md` §6 复现命令为准 |
| TD-171 | M1 验收 | 后端 Dockerfile（`deploy/backend/Dockerfile`）首版仅服务 E2E 验收：基础镜像未锁 digest、无镜像审计/资源口径/信号处理等生产硬化 | 已采纳 | M6 | 首次真实容器化演练已验证 uv+python3.13-slim 路径可跑通；M6 部署硬化时按生产清单补齐并接 `docs/deployment.md` |
| TD-172 | M1 验收 | backend 的 `scripts/` 目录不在 ruff/mypy 门禁范围（scope 为 `app tests`），`e2e_seed.py` 仅验收期单独检查 | 已采纳 | M2 | M2 第一个功能点开工时把 `scripts` 纳入 backend ruff/mypy scope 与 `scripts/ci.sh`（evals 已有 `src tests scripts` 先例），避免范围蔓延到本次验收提交 |
| TD-173 | M1-F 复审 | 审计写入在整段外层事务期间持有全局 advisory lock（单链串行化）：吞吐上限与外层事务时长耦合，高频写场景可能成为热点 | 已采纳 | M6 | 与 TD-163（外部锚点）同批评估：量级上来后分段链/锁内最小化；M1 量级（登录/权限拒绝频度）实测无感知 |
| TD-174 | M2-ORG01-03 | class 范围行与组织关系表双写：一致性靠「同事务 + 同步函数 + 部分唯一索引 + 对账测试」，无数据库级外键联动 | 已采纳 | M2 末评审 | 若 ORG-02 异步导入出现同步缺口，补周期对账任务（`expected_class_scope_pairs` vs 实际行）与告警；当前全部写路径单点收敛在 `app/org`，缺口风险集中在导入 |
| TD-175 | M2-ORG02 | 导入上传文件（含 PII）存对象存储且无生命周期/删除端点，失败 job 的孤儿对象永久保留 | 已采纳 | M6 | 随存储生命周期策略（TD-083 同类）统一：对象桶生命周期规则 + job 删除端点级联清理；M2 末评审确认无更早需求 |
| TD-176 | M2-ORG05 | 复合外键裸 `ON DELETE SET NULL` 会把全部引用列置 NULL（含 NOT NULL 的 tenant_id）→ 并发删除/调岗竞争时 500 而非优雅置空；同类模式已在 classes 班主任/辅导员 FK 存在 | 已采纳 | M6 | PG15+ 支持 `ON DELETE SET NULL (列清单)` 语法：M6 部署硬化时统一迁移改写（departments/teachers 与 classes 两处）；正常路径已被 409 预检挡住不可达 |
| TD-177 | M2-ORG05 | 部门树移动的子树 path 重写为大事务（子树行数线性）+ READ COMMITTED 下并发移动的路径重写竞争（已对被移动行 FOR UPDATE 降险） | 已采纳 | M6 | 校级规模（几十~几百部门）可忽略；M6 压测时用 EXPLAIN 验证 path 索引（varchar_pattern_ops）与重写耗时；如需强一致改 advisory lock |
| TD-178 | M2-ORG04 | HTTP 人员核查适配器（spec 说默认关闭，超时降级，人工确认）未实现 | 已延后 | M5/M6 | MVP 无真实第三方核查服务可对接；接口形状（适配器协议 + 超时降级 + 人工确认状态）在有真实服务时设计并实现 |
| TD-179 | M2-ORG04 | `PUT /school/teachers/{id}` 调岗与身份证更新分两步提交（身份证步失败 409/422 时调岗已生效），非原子 | 已采纳 | M2 末评审 | 复审 M5 对齐 D2-A 时的取舍：合并需把调岗逻辑迁出 departments 服务或提供组合服务函数，收益有限；若 B 端反馈「一步编辑」成刚需，提供单事务组合服务并保留逐项审计 |
| TD-180 | M2-ORG04 | 跨表（教师↔学生）HMAC 查重是两查询 check-then-act：同表并发由部分唯一索引兜底为 409，跨表并发同号竞窗仍可双写 | 已采纳 | M6 | 残余窗口极窄（同毫秒跨表写同号）；M6 若需收口，方案为 PG advisory lock（按 hmac 前缀）或物化 `person_id_cards` 单表唯一索引 |
| TD-181 | M2-ORG06 | 班级模型未含学年流转与业务编号：`(年级,班名)` 唯一键下每年夏季开学后全校年级标签失真、"按年级圈人"（KB-09）随之失准；大学同年级不同专业同名班无法建档 | 已采纳 | M2-ORG06 | 用户 2026-10-25 确认精简版：入学年份（届身份、唯一键成分、不可改）+ 当前年级可流转刷新 + 学年流转（幂等键+dry_run+归档标记）；**编号与毕业禁登录状态机从方案裁剪**，未来需要时按原全量方案扩展（git 历史 8c2820d 留档）；简报 `docs/design-briefs/M2-ORG06-academic-year.md` |
| TD-182 | M2-ORG06 | 学年流转并发残余风险：同幂等键由唯一索引 409 拦截、执行已加租户级 `pg_advisory_xact_lock` 串行化；**不同幂等键+分歧映射**先后串行提交（如一方归档、一方升级同起点年级）可产出混合状态行，与人工串行操作等价 | 已采纳 | M2 末评审 | 本点已加 advisory lock（复审 Minor 1 加固）；分歧 payload 的最终防线是 dry_run 预览+审计可追溯，若校方出现误操作场景再考虑操作确认二次接口 |
| TD-183 | M2-UI1 | 新增 3 条全栈 E2E（学年流转全链路/流转页浏览器可达/导入上传轮询，`e2e-fullstack/admin-fullstack-m2ui.spec.ts`）未跑：`e2e-stack` 固定占用 127.0.0.1:8000，被本机另一项目容器（photo-memory）占用；CI 不跑全栈 E2E（TD-110 同口径，本地门禁） | 已延后 | 释放 8000 端口后补跑（`scripts/e2e-stack.sh up` + `pnpm test:e2e-fullstack`）；如长期并存，给 e2e-stack 做端口可配（脚本已声明不做改配，需专项小改） |
| TD-184 | M2-UI1 | 列表拉取上限为人肉常量（PeopleView 班级解析取前 100、ClassesView 教师 200/课程 100/任教 200），超出部分显示「—」且无分页提示 | 已延后 | 有真实卡顿/截断反馈时（与虚拟滚动 TD 同批）；当前校级规模可忽略 |
| TD-185 | M2-UI2 | `sync_role_permissions(use_defaults_marker=False)` 的旧「映射为空=未初始化」启发式分支无独立用例，只在 `defaults_applied` 列尚未建立时进入 | 已延后 | M6 | 依赖/迁移清理评审：迁移 `c3f7a9d1e5b2` 已保证列存在，兜底只服务历史重放；届时若确认无需支持旧库重放，删除该分支与 `defaults_marker_columns_exist` 探测 |
| TD-186 | M2-UI2 | 租户侧不能自设「强制 MFA」策略（只能由平台策略位 `mfa_required` 统一下发），校管无法按角色/部门要求全员开启 | 已延后 | M6 | 与租户安全策略页一并交付（策略表 + 生效范围 + 到期提醒流）；本点按用户决策 D2 只做重置，平台位已覆盖最小合规；复查时点 M6 开题 |
| TD-187 | M2-UI2 | 权限后台只覆盖内置可编辑角色：缺自定义角色 CRUD、单用户多角色绑定、按人直授权限（权限账号） | 已采纳 | M2-UI3 | （用户 2026-10-26 决策 D2 明确移出本点；`role_permissions`/`user_roles` 数据面已支持多角色，缺的是创建/删除接口与页面） |
| TD-188 | M2-UI2 | `GET /school/users/lookup` 无接口级限流/失败计数，本校超管可借 404↔200 差异枚举登录名；当前只有查询审计留痕 | 已延后 | M6 | （与登录防拖库、`/auth/*` 限流 TD-139 同批做接口级速率限制）；调用面已收敛到 `school.personnel.mfa.reset` 单一权限 + 全量审计，枚举收益低 |
| TD-189 | M2-UI2 | admin 首屏为单包（所有视图静态 import，预览产物 ~655KB），2 worker 并发下 Playwright 断言撞默认 5s 超时；本点以 `expect.timeout=15_000` 放宽而非减小体积 | 已采纳 | M6 | 前端性能点：路由级 `defineAsyncComponent`/动态 import 分包 + 会话恢复请求并行，落地后回收 `expect.timeout` 到默认值 |
| TD-190 | M1-F | `tests/integration/test_audit_hash_chain.py` 用 `subprocess.run(text=True)` 收校验脚本输出，编码跟随本机 ANSI 代码页；中文 Windows（cp936）下子进程打印 ✅/❌ 直接 `UnicodeEncodeError`、父进程按 GBK 解码 UTF-8 字节 `UnicodeDecodeError`，两条用例在本机恒红（CI 的 Linux UTF-8 环境不暴露） | 已完成 | M2-UI2 | 抽 `_run_verify_script()` 统一注入 `PYTHONIOENCODING=utf-8` 并显式 `encoding="utf-8"`，两侧编码固定、不再依赖运行机器的 locale；本机复跑 8/8 通过（`test_verify_script_passes` / `test_verify_script_detects_unchained_row`） |
| TD-191 | M2-UI2 | **`school.role.manage` 转授禁令只封住该码本身的扩散**：已被超管授予该码的校方管理员，仍可提交一份**不含** `role.manage` 的权限集，把 `school.personnel.sensitive.read`/`school.personnel.delete`/`school.personnel.mfa.reset` 等敏感码加进自己所在角色（授了 manage 事实上等于授了本校全部权限）；多角色绑定后还能「A 角色保 manage、B 角色拿敏感码」 | 已采纳 | M6（打断层已在 M2-UI3 交付） | 用户 2026-10-26 两轮决策：① M2-UI2 采纳「禁止转授该码」而非传播守卫（两个收窄方案都会挡掉运营正常配置流）；② M2-UI3 采纳简报 §6 **方案 A「自扩权显式化」**而非硬封——结果集触达自己所在角色且净增敏感码时必须 `self_grant_ack`+理由，超管免打断不免留痕。**已交付**：`app/rbac/self_grant.py` 判据 + `PUT /school/roles/{id}/permissions` 接线（`_assert_self_grant`、CAS 前拒绝、`school.role.self_grant` 专项审计**放行与打断都写**（打断侧 `result="denied"` 走独立事务，报警因此看得到"未遂"），用例见 `tests/test_self_grant_guard_unit.py`、`tests/business/test_bs_m2ui3_self_grant.py`。**仍未收口**：用户↔角色绑定一条待 TD-187d 接线（按"绑定后该用户的权限并集"判）；创建自定义角色**不再收口**——2026-10-27 用户裁定按简报 §12 改口径，判据 ① 对新角色 id 恒不可达，接进去是线上永不触发的分支，那条链由决策 #5 的自绑禁令收口（同一裁定，TD-187d 的硬性前置，见 TD-194）；「只能授自己已有的码」的硬封与**双人确认/审批**（把 ack 换成审批）一并留在 M6——本守卫是显式化不是封死，被确认过的自扩权仍会发生，只能靠审计复盘与事后撤销 |
| TD-192 | M2-UI2 | 角色配置/重置/MFA 重置只有接口 mock 的 Playwright 用例（`e2e/admin-roles.spec.ts`），**没有真实后端全栈 E2E**：权限撤销后「下一个请求即 403、会话不断」、重置后热更新、MFA 重置后被目标用户踢出这三条链路目前只由后端业务场景测试覆盖，UI 侧与真实库的联动未跑过 | 已延后 | 与 TD-183 同批（释放 127.0.0.1:8000 后 `scripts/e2e-stack.sh up` + `pnpm test:e2e-fullstack`） | 本机 8000 被另一项目容器占用且明确禁止杀该容器；写用例的前置（专用账号、可编辑角色、可重置的 MFA 目标）已具备，端口可用后补 2–3 条即可 |
| TD-193 | M2-UI2 | 拒绝审计用「外层写事务未提交时另开一条连接写审计」的嵌套 `db_session`（`rbac/admin.py._audit_denied`、`personnel/mfa_admin.py`，与 M1 的 `personnel/service.py` 同模式）：一条被拒请求占两个连接池连接，且外层此刻可能持着 `roles ... FOR UPDATE`。无死锁环（审计写的是另一张表，`ROLE_NOT_FOUND` 分支根本没锁到行），但连接池被打满时被拒请求会**阻塞**而不是立刻 409/404 | 已延后 | M3 前评审 | 收口方案是把拒绝记进返回值、由路由在 `async with db_session(...)` 退出后再写审计（或引入 outbox），要同时改 `personnel/service.py` 的既有模式，属跨点重构；本期保持与存量口径一致，避免同一功能点里出现两套审计时序。M2-UI3 审查 Minor #6：自扩权打断分支新增第二个嵌套审计点（同一动作的两条留痕），把该条的池占用从"最多 2 连接"抬到"被打断时 2 连接 + 2 次审计写"——不改变结论，只提高 M3 前收口的优先级 |
| TD-194 | M2-UI3 | 自扩权守卫目前只在「角色权限全量替换」一条写路径接线；简报 §6/决策 #4 要求三条路径共用（创建自定义角色、用户↔角色绑定替换尚未落地），且现在每条路径各自手写"评估 → 打断 → 两条审计"的时序，接线时容易漏掉审计或把拒绝写成 CAS 之后 | 已采纳 | M2-UI3 TD-187b/TD-187d（同批） | 审查提出的"更好做法 #1"。落地方式：抽 `_guard_self_grant(principal, session, role_id, resulting_codes, *, ack, reason, request_id, role_code)` 把判据、422、`*.denied` + `school.role.self_grant(denied)`、成功侧专项审计的时序封在一个函数里，三条路径只调一次。不在本功能点做是因为另两条路径的代码还不存在——先建路径再抽 helper 会写两遍。**范围扩展（M2-UI3 二轮审查 Major #3）**：helper 落地时把第四条写路径 `reset_role` 一并接入。它今天**故意不过守卫**（恢复出厂按定义表重种，不接收操作者提交的码集），成立前提是不变量「可编辑内置角色的出厂默认 ∩ 敏感清单 = ∅」，已用两侧用例钉住：`tests/test_self_grant_guard_unit.py::test_factory_defaults_of_editable_roles_hold_no_sensitive_code`（seeds 侧）+ `tests/integration/test_rbac_custom_role_constraints.py::test_factory_default_table_holds_no_sensitive_code_for_editable_roles`（`rbac_builtin_role_defaults` 库侧）。谁往 `school_admin`/`teacher_b_end` 默认里加敏感码，这两条先红——那时"先接守卫再改默认"是硬顺序，不是可选优化。**TD-187b 落地后的收口范围修正（2026-10-27 审查 M-1；同日用户裁定「不接，改判据口径」，已定稿）**：创建路径**不接**守卫，且这不是漏接线——`_assert_self_grant` 的判据 ① 是"被写角色 ∈ 操作者自己所在角色集合"，而新建角色的 id 刚生成、`user_roles` 里不可能有指向它的行，① 恒假，接进去是一条无可达分支的死代码（要覆盖它只能伪造 `principal_role_ids`，测的是替身）。"建一个含敏感码的角色再绑给自己"这条链的承重墙是**决策 #5 的自绑禁令**（403 `BINDING_SELF_FORBIDDEN`），因此 TD-187d 的硬性前置：① #5 必须如实落地并留 `bind_denied` 审计；② 绑定侧守卫按「绑定后该用户的权限并集」判，不按单角色判；③ 一旦 #5 放宽（自助换岗/代自己绑），创建路径的守卫立刻复活，本条的"不接"结论作废。创建侧不是静默的：`school.role.created` 带完整 `permission_codes` + `copy_from_role_id`，含敏感码的自建角色可直接按码检索报警。**TD-187c 补的第二条腿（2026-10-27 用户拍板）**：三条 = 权限替换 + **范围替换** + 用户绑定（仍不含创建）。范围走**同形但独立**的判据（结果集净增 `school` 且触达本人角色，敏感项清单见 TD-203），不是把范围塞进码清单；审计沿用 `school.role.self_grant` 动作名 + `metadata.kind="scope"` 区分，helper 抽取时 `kind` 作为入参而不是两个函数。**TD-187c 落地时补的第 4 入口（helper 必须覆盖 `reset_role` 的两个维度）**：`reset_role` 之所以今天不过守卫，权限侧靠的是不变量「可编辑角色的出厂默认 ∩ 敏感码 = ∅」，但**范围侧该前提本来就不成立**——`school_admin` 的出厂默认范围就是 `school`。于是"别人把 `school_admin` 收窄成 `self` → 同角色的管理员点一次恢复出厂 → 静默拿回全校可见、无 ack"是一条可达路径。本期不假装它安全：`tests/test_self_grant_guard_unit.py::test_factory_defaults_of_editable_roles_do_hold_the_sensitive_scope` 把这个反向事实钉成契约（默认集 ∩ 敏感范围必须恰好是 `school_admin: [school]`，改默认即红），`design.md` §5.12 范围编辑那条明写"重置是第 4 个入口、本点未接"。TD-187d 抽 helper 时 `reset_role` 同时接 `kind="permission"` 与 `kind="scope"` 两条判定 |
| TD-195 | M2-UI3 | 「操作者已持有哪些码」在写事务内重读（`_principal_permission_codes`），与认证依赖每次请求解析的 `principal.permissions` 是**两套读取、同一 SQL 形状**：`app/auth/principal.py:171-182` 与 `rbac/admin.py` 各有一份"角色并集"查询，改一处口径（例如未来加入"用户级直授"）另一处会静默漂移 | 已采纳 | M3（引入按人直授或权限并集口径变更之前） | 审查 Minor #5 的修复把重复暴露出来：正确做法是把并集查询抽成 `app/rbac/principal_codes.py` 单一入口，由认证依赖与守卫共用；本期只有两处、口径已核对一致（含 `tenant_id` 复合 join），先登记避免在 TD-187d 绑定路径上出现第三份拷贝。**同批要一并收口的两处不对称**：① 超管免打断与 `_assert_delegable` 用的是 `principal.roles`（认证依赖在**请求进入时**从库读，非写事务内重读）——跨请求不存在 fail-open（撤销后下一请求即生效），但同一请求内"进入时是超管、写时已不是"的窗口靠抽 helper 时统一为事务内读取来封；本期不把 exemption 单独改成事务内读，因为同一请求的鉴权判据必须只有一个口径，改一半会造出两套"谁是超管"。② 前端 `selfExpansionCodes` 用的角色码集与 `/auth/me` 并集是**进页面时**的快照，落后于库时只能靠服务端 422 强制展开确认条补救（`role-self-grant-forced`，E2E 已覆盖）；这条不是测试漏洞而是本条存在的理由——抽成单一入口后，前端可在保存前重取一次并集，把"提示"升级成"与服务端同口径的预检"。**TD-187c 三轮审查补的第三条腿（范围侧同一问题）**：写事务内的 `_principal_scope_types`（`app/rbac/admin.py:462`）是 `app/auth/principal.py:DATA_SCOPE_SELECT_SQL` 的第二份同形查询（并集语义相同，前者额外带显式 `tenant_id` 谓词）；两处不一致时范围版自扩权判据会按一个运行时并不存在的"能力面"判净增——宽了静默放行，窄了把正常保存打断成 422。抽单一入口时**两条**（权限码并集、范围类型并集）一起收；在那之前由 `tests/integration/test_rbac_role_scope_edit.py::test_principal_scope_types_agrees_with_the_request_time_resolver` 钉住两边同解（种子同时含角色级与用户级行）。 |
| TD-196 | M2-UI3 | 超管自扩权分支（`confirmed_by="super_admin"` 与前端 `audit_only` 提示）在**一致种子下不可达**：`school_super_admin` 按矩阵 §4 持本校 school+campus 全量，敏感清单全在 school 域 → 净增恒为空。它兜的是"超管角色映射落后于目录"的漂移态（存量库/同步未跑/人工修剪），业务用例只能靠显式删映射构造 | 已延后 | M6（自扩权硬封 + 双人确认落地时一并评审） | 审查 Major #4 暴露的实情，如实登记而不是假装它是日常路径。保留豁免的理由：漂移态下最高权限账号既拿不到 ack 语义、又不能改自己被锁定的角色，硬要打断等于把人锁在配置页外、逼出"先建个临时角色再搬"的野路子。M6 做硬封时若改成"只能授自己已有的码"，这条分支的语义要重新拍板（届时超管命中不再可能，或改为强制理由） |
| TD-197 | M2-UI3 | 库层 `ck_roles_code_format_valid` 的长度上限（63）与 `roles.code` 列宽（`String(64)`）不一致：接口侧若按列宽写 `max_length=64`，64 字符的自定义角色码会通过 Pydantic、在 INSERT 时撞 CHECK，返回 500/IntegrityError 而不是 422 | 已完成 | M2-UI3 TD-187b（2026-10-27 交付） | 审查 Minor #7。落地：`RoleCreatePayload.code` 固定 `min_length=3, max_length=63` + `pattern=ROLE_CODE_PATTERN`（同一字面量，简报 §4.1），不改列宽（改列是迁移成本，且 CHECK 已经是更严的那道）。验证：单测 `test_role_create_payload_bounds_match_the_check_constraint` 逐字符比对迁移里的 `_CODE_FORMAT` 并断言 `"x"*64` 非法；业务用例 BS-M2UI3-B 走 HTTP 断言 `a`*64 → 422（穿不到 INSERT）；随本点全量 706 passed |
| TD-198 | M2-UI3 | `roles.name` 的"非空白"只在接口层拦（`RoleCreatePayload` 的 `field_validator` 去空白后判非法），服务层仅 `strip` 不校验，库层只有 NOT NULL + `String(100)`、没有非空白 CHECK：直调 `create_role(name="   ")` 仍能落一行没有名字的角色，列表里只能靠 code 认 | 已采纳 | M2-UI3 TD-187g | 复审 m-3 的残留。`code` 有两处强制同一字面量（接口 Pydantic + 库 CHECK，常量定义在服务层做单一来源）而 `name` 只有接口一处，这一处不同界是已知缺口不是遗漏。**注意 `code` 的服务层也不校验格式**（直调传非法码 = 撞库 CHECK 变 500），这是第三轮审查 m-2 纠正的文档口径，别再按"三层同界"写。收口方式：TD-187g 建"新建角色"表单时必填校验走同一判据，并补一条 `check (char_length(btrim(name)) > 0)` 的小迁移（`name` 与 code 不同，它不参与任何判据、只影响可读性，故不升级成本点阻塞项） |
| TD-199 | M2-UI3 | 删除预览的 `affected_users` **不受版本 CAS 保护**：`user_roles` 的写入不推进 `roles.version`，预览说"影响 1 人"、确认前同事又绑 1 人时，`version` 仍然匹配、删除照跑，操作者是在陈旧的影响面上按的确认（执行侧响应与审计记的是删除当时的真数，事后可查、事前失守）。第三轮审查 m-4 | 已采纳 | M2-UI3 TD-187d | 现状由 `tests/integration/test_rbac_custom_role_create_race.py::test_preview_affected_users_is_not_protected_by_the_version_cas` 钉住。收口二选一，须在 TD-187d 里明确拍板：① 绑定/解绑替换在同一事务里 bump 目标角色的 `roles.version`（最省，代价是"改绑一个人社群配置页就多一次 409"，且预览与执行之间任何一次绑定都会打断确认流）；② 删除确认改成"影响面指纹"而非版本号（`affected_users` + 码集哈希一起比，语义更准但要新增一套判据与错误码）。本期不动：`user_roles` 还没有任何写路径（只有 `seed_builtin_roles` 与测试直插），先落哪条都不影响现网，但**绑定接口一落地这条就从"纸面残余"变成真实破坏性窗口**，所以 TD-187d 的验收必须带上它。届时该集成用例要按注释翻成断言 409，不要删 |
| TD-200 | M2-UI3 | 前端"恢复出厂"按钮用 `selectedRole.is_system` 判可重置，等于在客户端复刻了服务端"有没有出厂定义"的判据（`rbac_builtin_role_defaults` 里有没有该 role_code 的行）。今天两者等价（内置⇔有定义，自定义⇔无定义），但判据一旦分叉（例如将来给某个内置角色撤掉定义行、或自定义角色支持"按模板初始化"）按钮就会点亮并恒 409，而 `RoleResponse` 里没有可供前端改正的字段。第三轮审查 m-6 | 已采纳 | M2-UI3 TD-187g | 收口方式：`RoleResponse` 增加服务端派生的 `resettable: bool`（与既有 `editable`/`lock_reason` 同口径，由 `_lock_reason` 那层一次算出），前端改跟 `resettable` 走，`is_system` 只用于展示"内置/自建"标签。本期不改后端契约的原因：TD-187b 只做服务端，前端角色页的重建在 TD-187g 一次性做，避免同一个响应模型改两轮。服务端始终是唯一的门禁（`openReset` 的 catch 已兜住"内置但无定义行"这条角例），本条只影响按钮的准确性 |
| TD-201 | M2-UI3 | 试图把平台域权限码写进租户角色（`PLATFORM_PERMISSION_FORBIDDEN`）按"入参非法"处理、**不落安全审计**，与 `ROLE_CODE_NOT_DELEGABLE`（转授禁令，落 `*.denied`）不对称。前者是跨 realm 边界的试探，报警侧目前看不到它 | 已延后 | M6（审计报警规则评审） | 第三轮审查 m-3 指出。本期维持现状的三个理由：① 平台码全局可见，被拒的原因是目录判据（`_validate_codes`）而不是隔离判据，没有对方租户的对象被探测；② `_validate_codes` 同时服务 PUT 与 POST，给它加审计会连带改掉 M2-UI2 已断言的 `permissions.denied` 留痕集，属跨功能点改口径；③ 加了之后"配置页填错码"也会进安全事件流，稀释信号。M6 做报警规则时若确认需要"谁在试图把平台码塞进租户角色"这一路，按 422 计数 + `request_id` 关联补，而不是回到 `*.denied`。口径已写进 `docs/design.md` §5.12 审计段 |
| TD-202 | M2-UI3 | 角色权限替换与角色范围替换**共享同一列 `roles.version`**（简报 §13.2 默认口径）：一次成功的权限保存会推高版本，让另一侧尚未提交的范围草稿在下一次保存时 409。服务端这样做是对的（一列版本覆盖两条写路径，避免出现"权限已更新、范围更新失败"的半更新态），代价落在前端 | 已采纳 | M2-UI3 TD-187g | 缓解方式已写进简报 §13.6：范围区保存前重取快照 + 409 自动采用最新（沿用 M2-UI2 权限区已有的那条路径），两个编辑区不做"同时脏"的乐观并发。彻底解法二选一——`roles` 加第二列版本，或权限与范围合并成一个端点一次提交；后者被 §13.2 决策② 否决（审计差异混成一团、草稿互相打回），前者要多一条迁移与一套 CAS 判据，收益不值。TD-187g 若实测"两个区互相打回"成为高频，再升级为分列 |
| TD-203 | M2-UI3 | 范围版自扩权判据把敏感项写死成 `school` 单一值（简报 §13.3），依据是 `app/rbac/scopes.py:sees_whole_school` 目前只有全校这一个"覆盖其余一切"的类型；`class`/`course` 不可经角色配置写入、`self` 只覆盖持有人自己的记录（∅→`self` 也是扩大，但够不到别人的数据），都不触发确认 | 已延后 | M3（引入按部门/课程过滤的范围时） | 若将来新增覆盖型范围类型（如 `department` 全部门、或 `course` 在 M3 真的贡献可见性，见 TD-151），这条敏感项清单必须同步扩，否则"把角色抬成跨部门可见"会走成静默配置。低成本防御：单测 `SENSITIVE_SCOPE_TYPES == frozenset({'school'})` 与 `KNOWN_SCOPE_TYPES` 差集断言并列，新增范围类型时那条断言先红，逼改的人回来读 §13.3 |
| TD-204 | M2-UI3 | 审查建议给 `RoleScopePayload.scope_types` 加 `max_length`（TD-187c 审查 Minor 8） | 已拒绝 | — | 单侧加限会让两条同族写路径的入参契约不一致（`permission_codes` 同样没有上限，而它的合法基数还大得多）；越界清单的真实代价只是服务层一次内存去重 + `unnest` 多几行，且每个元素都要过 `KNOWN_SCOPE_TYPES` 判定，重复项等价于只提交一次。真正的口径（"非字符串元素由 Pydantic 挡、未知字符串走服务层"）已写进 payload 文档串，比一个数字上限更有约束力 |
| TD-205 | M2-UI3 | TD-187c 二轮审查 Minor 4 拆出的两件事：① `replace_role_scopes` 的 `update ... and version = :version` 是**纵深防御而不是并发正确性的承重墙**——同一事务更早的 `FOR UPDATE` + `int(role["version"]) != version` 已经拦下落败者，去掉 SQL 谓词并发用例仍然绿，因此"这条用例证明了 CAS"是误读；② 权限与范围**共享一列** `roles.version`（TD-202）时的跨路径锁顺序无用例，现有并发用例两个写者都走范围路径 | 已采纳 | ① 随 M2-UI3 保留（不引入 monkeypatch 假测）；② TD-187f | ① 不做成用例：要证明"谓词生效"只能篡改前置比较或伪造行锁时序，测的是替身而不是产品代码；已在并发用例文档串明写它钉不住什么，防止后来者把它当 CAS 回归网。② TD-187f 补一条"权限 PUT 与范围 PUT 同版本并发 → 恰好一个成功、`version` 只 +1"，那是运营真实会做的动作（同一个角色两个区先后保存），也能顺带验证 409 的 `latest` 快照带的是哪一侧的新值 |
| TD-206 | 质量与安全运维 P0（2026-10-28 用户批准） | 四项短板：关键包无覆盖率门槛、安全扫描只在 main/周一跑、依赖无自动更新、无运行时指标与告警 | 已完成 | 2026-10-28（P0 单元，commit `29de4ec` + `c8e60e1`） | 落地：`pytest-cov` 关键包分支覆盖率门槛（基线 755 passed/1573s：rbac 73.87%、auth 81.15%、tenant 81.25%、audit 66.67%，阈值 73/81/81/66 写入 `backend/scripts/check_coverage.py`，反证抬到 99% 时 exit 1）、`security` 工作流覆盖 `feature/**` 与面向 main 的 PR、`.github/dependabot.yml`（包生态只开安全更新、Actions 周更分组 minor/patch；仓库级 Dependabot alerts/security updates 已于 2026-10-28 开启）、`/metrics` + 4 类 5 条告警规则（`deploy/observability/prometheus/alerts.yml`）；简报 `docs/design-briefs/P0-quality-security-ops.md` §7 |
| TD-207 | 质量与安全运维 P1 | 全栈 E2E 未进 CI/夜间：12–15 例只在本地手工跑，M2 新增链路欠跑（TD-183/192） | 已采纳 | M2-UI3 批次 B 末（与 TD-183/192 同批） | 端口可用后先补跑欠账，再把 `pnpm test:e2e:fullstack` 挂 nightly（不进每次推送，避免 Actions 分钟与 flaky 拖累） |
| TD-208 | 质量与安全运维 P1 | 无变异测试，关键模块的断言强度不可度量（本次三轮审查多次出现"用例名承诺了但没钉住"） | 已延后 | M3 边界 | 只对 RLS/RBAC/审计三个模块抽查（mutmut 或 cosmic-ray），每月一次、结果进 review-log，**不进门禁** |
| TD-209 | 质量与安全运维 P1 | 备份恢复与迁移回滚只有清单未演练 | 已采纳 | M6（SaaS 运营与硬化） | 脚本化 `pg_dump`/restore + `alembic downgrade` 一次 + `verify_audit_chain` 校验，季度执行并留记录 |
| TD-210 | 质量与安全运维 P1 | 批次末无固定的"新增端点/权限码/跨租面"威胁模型 diff | 已采纳 | M2-UI3 批次 B 起 | 每批次末在 review-log 加一节，逐条过本批次新增的攻击面与缓释措施 |
| TD-211 | 质量与安全运维 P2 | 无 DAST、镜像扫描、SBOM | 已延后 | M6（首次生产部署前） | ZAP baseline 对 staging + Trivy 扫镜像 + 生成 SBOM；与部署流水线一并落地 |
| TD-212 | 质量与安全运维 P2 | 实现与审查同源模型会共享盲点；安全关键 diff 无第二审查者 | 已延后 | M3 起 | `auth`/`tenant`/`rbac`/`audit` 的 diff 由第二个模型或人类复核并签字；先在一轮审查里试用成本 |
| TD-213 | 质量与安全运维 P2 | design.md 的端点表/错误码表无自动校验，文档漂移靠审查抓 | 已延后 | M3 | 把表做成测试数据源（路由权限清单守卫已有先例），改代码不改文档即红 |
| TD-214 | 质量与安全运维 P0 尾项 | `/metrics` 与告警规则已就绪，但未部署 Prometheus/Alertmanager、未接告警通道与看板；多 worker 汇总、反代层拒绝也尚未落地 | 已采纳 | M6（OBS-02 生产观测） | staging 建立时接 scrape 与 Alertmanager 路由；规则阈值按实测调，当前 5 条（4 类）是起始值；同时补 `PROMETHEUS_MULTIPROC_DIR` + `MultiProcessCollector`（多 worker 汇总），并在 Nginx/安全组显式拒绝 `/metrics` 对外访问；同时校准 `campus_http_stream_errors_total` 对客户端断开的可见性（`StreamingResponse` 可能在内层任务组消化断连，当前只统计实际向上传播的取消/异常） |
| TD-215 | 质量与安全运维 P0 尾项 | 指标只有计数器：没有请求耗时直方图/分位数，无法回答"慢不慢"（SSE 首 token P95、发布接口耗时等 SLO 指标同样缺位） | 已采纳 | M6（与 TD-214 同批） | 加 `campus_http_request_duration_seconds` 直方图（标签沿用 method/route/status 的有界口径）+ SSE 首事件延迟指标，与 scrape/看板一起落地；本期不做是因为直方图桶与 SLO 阈值需要在 staging 按真实流量定，先上会得到一组误导性的默认桶 |
| TD-216 | 质量与安全运维 P0 | `record_permission_denial`/`record_login_failure`/`record_audit_write_failure` 等 helper 的 `realm`/`reason`/`action` 是裸字符串，没有类型或白名单约束；当前调用点全是字面量，但未来有人传 `f"tenant.{tenant_id}"` 就会把 PII/高基数带进标签 | 已延后 | M3（与 TD-215 观测扩展同批评审） | 不改的理由：在 helper 内做白名单归一未知值会**静默**把新增的合法 reason/action 吞成 `other`，丢掉的正是告警要区分的维度；用 `Literal`/Enum 收敛则要在 `app/core/metrics.py` 里复制一份跨模块（auth/rbac/personnel/onboarding）的码表，与"权限项目录跟代码走"形成第二事实源。过渡口径：`method` 已归一（未知→`OTHER`，有测试），标签值只允许代码字面量由审查把关；`test_metrics_unit.py` 断言 `/metrics` 只出现 `campus_*`，且每个指标族的标签名都在白名单内（无 tenant/user 维度）。M3 加直方图时一并评估"标签值常量表 + 单测枚举"的收口方式 |
| TD-217 | 质量与安全运维后续 | 覆盖率只证明"代码被执行过"，不证明断言能抓住错误；关键包可能出现"高覆盖、弱断言"，逻辑被改坏但测试仍绿 | 已延后 | M6（与 TD-214/215 同批评审；先观察远端 CI 与覆盖率门槛是否稳定） | 在 `app/rbac`、`app/audit` 小范围试跑变异测试（mutmut 或 cosmic-ray），用"变异杀死率"定位弱断言并补强；不纳入每点门禁（成本高），先作为里程碑边界的抽查项，跑通后再决定是否常驻 |
| TD-162 | M1-F | 持 AUDIT_HASH_KEY 者可伪造入链行（hash_version=1 且哈希一致），链校验无法区分 | 已采纳 | M6 | 外部锚点（anchor_ref 预留字段 + 定期 WORM 导出哈希到外部存储）可收口；当前审计密钥仅平台运维持有，泄露面极小 |
| TD-163 | M1-F | 哈希未覆盖 ip/user_agent/metadata_json/before_json_redacted/after_json_redacted，超级用户改这些字段不可检测 | 已采纳 | M6 | 与外部锚点一并考虑（扩字段会破坏现有链）；当前仅覆盖核心业务字段（与简报字段表一致） |
| TD-161 | M1-E2 | 例外标记可被伪造直写：CHECK 有 `self_exception_used IS TRUE` 这一条款后，"绕过审核函数直写同人 + 标记为真"在数据库层与合法例外同形，无法区分 | 已采纳 | M2 | 检测改走反查而非再加一道约束（用户 2026-10-23 确认三条款 CHECK）：真例外一定带 `approval.self_exception.used` 审计，故 M2 审计巡检加一条反查——`school_review_records` 里 `self_exception_used IS TRUE` 却在同 `request_id` 找不到对应审计的行即告警。落点与 TD-153（跨租工单化）同一批安全巡检 |

---

## 3. 里程碑边界评审记录

| 评审时点 | 新增条目 | 已完成 | 已采纳进入下一里程碑 | 已延后 | 已拒绝 | 结论 |
|---|---|---|---|---|---|---|
| M0-A | TD-001~006 | TD-004 | TD-005 | TD-001/002/003/006 | 无 | 通过 |
| M0-B | TD-007~014 | TD-013/014 | TD-012 | TD-007~011 | 无 | 通过 |
| M0-C | TD-015~028 | TD-027/028 | TD-016/018/025/026 | TD-015/017/019~024 | TD-021 | 通过 |
| M0-D | TD-029~038 | TD-037/038 | TD-029~031/033~036 | TD-032 | 无 | 通过 |
| M0-E | TD-041~053 | TD-012/042/046 | TD-045/047/048/051 | TD-041/049/052/053 | TD-050 | 通过（两轮审查，Blocker/Major 全部修复） |
| M0-F | TD-002/005/010/029~031/033~035/047/052 + TD-056~064 | TD-002/005/010/029~035/047/052/057/058 | TD-051/054~056/059/061~064 | TD-041/049/053/060 | TD-050 | 通过（M0 收口，见 docs/m0-acceptance.md） |
| M1-D1 | TD-107/116/119~123 | TD-107 | TD-121（M6）/ TD-119（M5/M6）/ TD-120（M1 尾）/ TD-123（M2） | TD-116 | TD-122 | 通过（第五轮复审 0B/0M，远端 CI 绿，348 passed） |
| M1-D2 | TD-124~135 | 无（本期取舍全部为延后/拒绝，功能内改动记于 review-log） | TD-126/129/134（M2） | TD-124/125/127/128/132/133/135（M6） | TD-130（邮件/短信 OTP）、TD-131（Fernet/pgcrypto） | 通过（第二轮复审 0B/0M，后端 411 passed + 前端 32 单测/28 E2E，远端 CI `5add509` backend/frontend/security 全绿） |
| M1-E1 | TD-136~150（并把 TD-082 收口为已完成、TD-072 目标改 M1-E2、TD-100 按用户确认改判 M6） | TD-082（矩阵落库）/ TD-100 的基线测试部分 | TD-136/138/139/142/144（M2/M3/E2）/ TD-148（M6 依赖升级评审）/ TD-149（M1-E2 与范围守卫迁移合并） | TD-137/140/141/143/145/146/147/150（M2/M6/M1 尾） | 无 | 第五轮复审通过（0B/0M/3 Minor：后置守卫与启动自检统一绑定 `TENANT_MODULES`、downgrade 补 `is_system`、`/auth/` 免鉴权面并入 TD-139；本地门禁全绿——后端 481 passed、ruff/mypy 128 文件/alembic check/bandit×2/pip-audit，远端 CI（`6e916af`）backend #39851973194 / security #39851973099 success） |
| M1-E2 | TD-151~161（并把 TD-072/138/149 收口为已完成） | TD-072（平台跨租最小通道）/ TD-138（范围数据行 + 服务端过滤）/ TD-149（触发器收紧）/ TD-156（三道锁不等价的如实口径） | TD-151/152/154/155/157/159/160/161（M2/M3/M4/M6） | TD-153/158（M6 或首个真实跨租/安全告警需求） | 无 | 两轮审查（第一轮 0B/3M/4M 全部修复；第二轮因当日独立审查子智能体配额耗尽改在主上下文执行，0B/0M）；本地门禁全绿——后端 561 passed、ruff/mypy 149 文件/alembic check/bandit×2，远端 CI（`d7a7663`）backend #39950816551 / security #39950816785 success |

> M0-E 开始前先处理 `已采纳/目标 M0-E` 的条目：TD-012（MinerU）——已完成（自定义镜像 + 三档实测）。
> M0-F 已完成（TD-002/005/010/029~031/033~035/047/052 全部收口）。M1 开始前处理：TD-016/018/025/026/036/051/057。
