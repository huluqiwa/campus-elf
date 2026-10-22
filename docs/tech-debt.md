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
| TD-068 | M1-A | 审计哈希链校验脚本/只读接口未实现 | 已采纳 | M1-F | 表与仅追加约束已就绪；M1-F 实现链校验脚本 + 平台审计只读接口 |
| TD-069 | M1-A | `updated_at` 仅 ORM 侧 `onupdate`，非 ORM 更新不刷新 | 已延后 | M2 | 需要时补数据库触发器或统一走 ORM 更新 |
| TD-070 | M1-A | `list_all` 用 limit/offset 分页（design §5.1 约定 cursor） | 已采纳 | M2 | 稳定排序已补（created_at, id）；列表接口落地时改 cursor 分页 |
| TD-071 | M1-A | 尚无「零 skip」CI 断言（仅覆盖缺变量场景） | 已采纳 | M1 尾 | 可在 CI 解析 pytest 的 -rs 输出，断言白名单外 skipped=0 |
| TD-072 | M1-A | 平台跨租查询通道（二次授权+审计）未实现 | 已采纳 | M1-E2 | 平台连接已无业务表权限；E1 只落权限码与鉴权边界，M1-E2 落地经审批的平台跨租只读查询通道（二次授权+审计+`platform.*` 码与 realm 校验闭环） |
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
| TD-091 | M1-B | 平台角色仍可 INSERT audit_logs（M1-A 授权），可伪造审计事件 | 已采纳 | M1-F | 审计哈希链（TD-068）落地后伪造会在链校验中暴露；启动自检目前不覆盖"审计写入是否只经函数"，M1-F 一并收口 |
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
| TD-110 | M1-C-UI | E2E 用接口 mock，未跑真实后端全栈链路 | 已采纳 | M1 验收 | M1 验收阶段用专用 compose 栈（后端 + 测试库 + 前端 preview）跑全栈 E2E，覆盖登录→改密→业务接口的真实链路 |
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
| TD-126 | M1-D2 | 租户角色（校管/员工）无法强制 MFA，且租户侧无 MFA 重置后台 | 已延后 | M2 | 强制依赖"丢设备后自助/管理员重置"能力，否则账号锁死；需 M1-E 权限矩阵确定谁能重置，并在 M2 校管后台交付 `mfa_reset` 接口与审计 |
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
| TD-137 | M1-E1 | 角色↔权限分配无管理接口与 UI（只能靠 SQL/迁移） | 已延后 | M2 | 口径 ③ 明确本期不做界面；M2 校管后台交付 `school.role.manage` 接口 + 配置页，并补配置审计与热更新生效测试 |
| TD-138 | M1-E1 | `data_scopes` 本期只落 `school`/`self` 语义且**无数据行**，`class`/`course` 未实现服务端过滤 | 已采纳 | M1-E2/M2 | 口径 ④：E2 做 school/self 的服务端过滤与越权回归；班级/任课范围依赖 M2 的组织与班级关系，KB 文档范围依赖 M3 ACL，届时再写 `data_scopes` |
| TD-139 | M1-E1 | 免鉴权路由白名单需随业务持续复核（新增接口可能被随手加进白名单） | 已采纳 | M2/M6 | 白名单已被 `PERMISSION_FREE_PREFIX_ALLOWLIST` 限定为「认证自助 / 健康探针 / 公开登记」三类并有测试兜底；第五轮审查另指出 `API_PREFIX/auth/` 整段免权限码，将来落在 `/auth/` 下的特权接口只要进白名单就只校验"已认证"——M2 新增 `/auth/*` 时必须逐条核对是否应改为按权限码校验，M6 安全评审复核理由文案 |
| TD-140 | M1-E1 | 权限矩阵变更缺"变更审批"流程（改代码即改权限，发版即生效） | 已延后 | M6 | 与 R7「配置双人确认」同源：M6 做变更单 + 第二人确认 + 生效前 dry-run 差异预览；本期靠 Git 评审与矩阵文档版本（v1.2）兜住 |
| TD-141 | M1-E1 | 前端未消费 `/auth/me` 的 `roles`/`permissions`，菜单仍全量展示 | 已延后 | M2 | 服务端已逐请求校验（前端隐藏不是安全边界），本期无越权风险；M2 校管后台按权限渲染菜单与按钮，并补"无权限入口不可见 + 直连仍 403"用例 |
| TD-142 | M1-E1 | 目录与"接口实际校验点"的一致性只有路由清单测试，没有反向比对（新增码却无处使用=空转，无人报警） | 已采纳 | M2 | 路由清单测试保证"接口都有权限"，不保证"权限都有接口"；M2 随后台做 `rbac_check`（矩阵↔目录↔路由声明三方比对报告），空转码进发布检查 |
| TD-143 | M1-E1 | 内置角色缺"重置为出厂默认"接口，误配只能手工改库 | 已延后 | M2 | 出厂默认已在定义表（`rbac_builtin_role_defaults`）里可查，重置语义=先撤后种；M2 提供接口需带二次确认 + 审计 + 影响面预览，避免一键清空运营配置 |
| TD-144 | M1-E1 | 教师上传后"人群限本人/任课班级、扩全校需管理员确认"未落地 | 已采纳 | M2/M3 | 口径 ⑤ 本期只放开"持后台账号即可上传"；范围收敛依赖 M2 班级/任课关系与 M3 KB ACL，届时在构建入口做人群默认值 + 扩权审批 |
| TD-145 | M1-E1 | 权限映射变更即时生效、无双人确认（改错即全租户生效） | 已延后 | M6 | 与 TD-140 同源（R7）；本期风险由"只有迁移/SQL 能改映射、且必须发版"限制，M2 开放前台配置后必须先引入确认机制再放开 |
| TD-146 | M1-E1 | 全量型角色「只补不撤」：人工从 `school_super_admin`/`platform_super_admin` 收回的权限，会在下一次同步/发版被自动补回 | 已延后 | M2 | 矩阵把这两个角色*定义*成「范围内全量」（§1.2 + §4 定稿脚注 2026-10-23 + §5-11），若不补则新增权限码永远进不了超管视野；其余内置角色一律「未配置过才授默认」，人工改动不被覆盖（`tests/integration/test_rbac_sync.py` 锁住）。M2 配置界面若需长期压制某码，改为在库里维护"超管例外表"而非改变口径 |
| TD-147 | M1-E1 | 迁移 `downgrade` 把内置角色映射重置为「E1 之前的出厂状态」，不是「回滚前的真实状态」：E1 期间对系统角色的人工增授（及 C 端角色上的 `user_roles` 绑定）在回滚后丢失 | 已延后 | M1 尾/部署 | 逐租户快照恢复需要额外表与清理任务，本期回滚只承诺"结构可来回"（`test_migration_rbac_rollback.py` 覆盖 head↔downgrade↔head 重放）。生产回滚 SOP 必须先 `copy role_permissions/user_roles` 备份；M1 验收前把该约束写入 `docs/handoff.md` 部署注意 |
| TD-148 | M1-E1 | 路由清单依赖 FastAPI 内部结构（`_IncludedRouter`/`original_router`/`include_context.prefix`、`dependant.call` 上的标记），框架升级可能静默漏读路由 | 已采纳 | M6 依赖升级评审 | 已用 `test_manifest_sees_the_whole_app` 设下限（读到路由数 <20 或缺关键路径即红）把"静默失效"变成"测试失败"；升级 FastAPI/Starlette 时按该用例与白名单过期检查（`test_whitelist_has_no_stale_entries`）一并复核 |
| TD-149 | M1-E1 | DB 触发器 `role_permissions_realm_guard` 仍只按 `module = 'platform'` 拒绝入租户角色：`observability.*`/`eval.*` 可被手工 INSERT 进租户角色，而代码口径把它们算作平台运维域 | 已采纳 | M1-E2 | 本期把 realm 判定收敛到 `catalog.code_realm()`（模块白名单 `school`/`campus` = 租户域）并有 `_self_check_seeds`、同步守卫、启动自检（`app/db/selfcheck.py`，第四轮审查 M1 一并改为同一白名单）钉住；库里那道防线要同步改成 `module NOT IN ('school','campus')`，属于对已上线触发器的替换，与 E2 的租户范围/职责分离迁移合并做一次，避免两个迁移前后改写同一触发器 |
| TD-150 | M1-E1 | 出厂默认补授用「角色映射为空」当作「未配置过」的启发式：把内置角色的最后一条人工撤销掉，下一次同步会把默认值补回来 | 已延后 | M2 | 改成 `roles.defaults_applied`（或 `role_defaults_granted` 标记表）才能区分"清空"与"未初始化"，代价是一列 + 迁移 + 语义变更（撤销最后一条从此不失效）。当前口径已知代价，行为由 `test_sync_reapplies_factory_defaults_when_builtin_role_is_emptied` 钉住；M2 前台配置界面（TD-137）上线前必须先落地标记，否则"撤销一条=白撤"会被用户当成 bug |

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
| M1-E1 | TD-136~150（并把 TD-082 收口为已完成、TD-072 目标改 M1-E2、TD-100 按用户确认改判 M6） | TD-082（矩阵落库）/ TD-100 的基线测试部分 | TD-136/138/139/142/144（M2/M3/E2）/ TD-148（M6 依赖升级评审）/ TD-149（M1-E2 与范围守卫迁移合并） | TD-137/140/141/143/145/146/147/150（M2/M6/M1 尾） | 无 | 第五轮复审通过（0B/0M/3 Minor：后置守卫与启动自检统一绑定 `TENANT_MODULES`、downgrade 补 `is_system`、`/auth/` 免鉴权面并入 TD-139；本地门禁全绿——后端 481 passed、ruff/mypy 128 文件/alembic check/bandit×2，远端 CI 见台账） |

> M0-E 开始前先处理 `已采纳/目标 M0-E` 的条目：TD-012（MinerU）——已完成（自定义镜像 + 三档实测）。
> M0-F 已完成（TD-002/005/010/029~031/033~035/047/052 全部收口）。M1 开始前处理：TD-016/018/025/026/036/051/057。
