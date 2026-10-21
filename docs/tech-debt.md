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
| TD-072 | M1-A | 平台跨租查询通道（二次授权+审计）未实现 | 已采纳 | M1-E | 平台连接已无业务表权限；M1-E 落地经审批的跨租查询通道并审计 |
| TD-073 | M1-A | 非 development 环境启动自检（角色非超级用户 / RLS 已启用）未实现 | 已完成 | M1-B | `app/db/selfcheck.py` + lifespan：同时校验**租户与平台**连接角色、逐表 FORCE RLS/策略表达式、审核函数 owner、无上下文不可见；测试覆盖平台超级用户与 users 关闭 FORCE RLS（空表）两侧 |
| TD-074 | M1-A | 认证层未断言「realm 与连接类型匹配」 | 已采纳 | M1-C | 平台 realm 只能用平台连接（`platform=True`）、租户 realm 只能用租户连接；一处误判即会拿到平台连接读到全量审计，须在 M1-C 用测试固定 |
| TD-075 | M1-A | 迁移/运维脚本对 RLS 租户表的读写语义 | 已完成 | M1-B | 迁移用 DDL 角色（测试为超级用户）执行；应用租户/平台路径全部走组角色成员。平台开通改为 SECURITY DEFINER 函数，策略/授权语义在迁移回调与「平台不能写存量租户」回归测试中固定 |
| TD-076 | M1-B | 公开注册接口无限流 | 已采纳 | M6 | 由 Nginx/网关限流与验证码策略收口；当前仅做字段/大小校验 |
| TD-077 | M1-B | 初始密码邮件同步发送，失败无重发入口 | 已采纳 | M5 | 事务内发送：失败回滚开通并返回 502；失败补写 `tenant.approve.email_failed` 审计。M5 用 outbox + 平台重发替换 |
| TD-078 | M1-B | 审核接口使用临时开发令牌 | 已完成 | M1-C | 平台账号登录 + `platform.tenant.review` 权限替换；`DEV_PLATFORM_REVIEW_TOKEN` 已从配置/示例/测试栈删除；无权限访问写 `auth.permission.denied` 审计 |
| TD-079 | M1-B | 资质凭证未做病毒扫描 | 已采纳 | M3 | 统一接 ClamAV 并补扫历史凭证（设计简报 §3.3 方案 A） |
| TD-080 | M1-B | S3/MinIO 对象存储路径无 CI 集成测试 | 已采纳 | M0-F/M6 | 测试用本地目录实现；CI 接入官方 MinIO 镜像或自建镜像构建后补 S3 集成用例 |
| TD-081 | M1-B | 审核人使用占位 UUID（无真实平台账号） | 已完成 | M1-C | 审核审计 `actor_id` 为真实平台账号；业务测试断言非空且来自登录主体 |
| TD-082 | M1-B | 校方内置角色仅超管有全量权限映射 | 已采纳 | M1-E | `school_admin`/`teacher_b_end` 的权限矩阵在 M1-E 按 rbac-matrix 落库 |
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
| TD-100 | M1-C | 每个认证请求都查库读权限（未缓存） | 已延后 | M1-E | 先保证"权限变更立即生效"；M1-E 权限矩阵落地后按需加短 TTL 缓存 + 变更失效，并保留失效即时性测试 |
| TD-101 | M1-C | JWT 密钥轮换只支持单个 active kid（无并存旧 kid 校验） | 已延后 | M6 | 生产上线前补"双密钥并存"（旧 kid 仅校验、新 kid 签发）与轮换演练 |
| TD-102 | M1-C | 登出后的访问令牌撤销依赖 Redis 可用性 | 已延后 | M6 | Redis 不可用时登出仅撤销刷新令牌（访问令牌最长 15 分钟自然过期）；M6 监控与降级策略中明确 |
| TD-103 | M1-C | 前端令牌存储（access 内存 + refresh HttpOnly Cookie）与三端登录页未实现 | 已采纳 | M1-C-UI | 后端契约已就绪；前端登录页/改密页/路由守卫作为 M1-C-UI 子项实现并补 Playwright 主路径 |
| TD-104 | M1-C | 登录失败锁定/验证码未实现 | 已采纳 | M1-D | 属 AUTH-05；失败已写审计，M1-D 加计数与锁定 |
| TD-105 | M1-C | `certifi 2026.10.18` 命中 GHSA-6q48-6c7w-9gw2 | 已完成 | M1-C | backend/evals 锁文件均升级到 `certifi 2026.10.19`；临时忽略已删除；本地 pip-audit 复验通过 |
| TD-107 | M1-C | bcrypt 72 字节输入上限与 spec"最大 128 字符"冲突 | 已延后 | M1-D | 本期按字节拦截（登录 401/改密 422）；M1-D 评估 argon2id 或 sha256 预哈希以支持完整 128 字符策略，并考虑历史哈希兼容 |
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
| TD-116 | M1-C2 | 登录/刷新在用户级 advisory lock 内执行 bcrypt 与会话标记写入：知道 login_id 的未认证请求可排队放大同账号延迟（Redis 故障时每次约 2s），并阻塞该账号刷新 | 已延后 | M1-D/M6 | 会话标记必须在事务内登记（防止撤销后复活），bcrypt 在锁内保证改密/停用与签发原子；本期由登录限流（M1-D）先兜底，若尾延迟仍异常，评估锁前预校验 + 锁后按密码版本复核、Redis 标记改带超时的异步写入 |
| TD-117 | M1-C2 | Redis 会话标记键为 `{prefix}:auth:session:{realm}:{sid}`，不含 tenant_id（jti 撤销键同样不含） | 已延后 | M6 | 与既有 jti 键保持一致的取舍：sid/jti 均为数据库生成的 UUID，跨租碰撞不可利用，且 token 的 realm/tenant 在鉴权时另行校验；M6 统一缓存键规范时一并加租户段（含密钥轮换与旧键清理） |
| TD-118 | M1-C2 | 带 Bearer 的登出若刷新令牌属另一 family，撤销在独立事务先提交、第二 family 审计随后单独写入；审计插入失败时会出现“已撤销但无审计”（接口 500） | 已延后 | M6 | 正常路径已由跨租/跨用户用例锁定；彻底解决需把撤销与审计合并为同一事务（`_revoke_by_jti` 内按“是否当前 family”决定是否写统一事件），并补 platform↔tenant 跨 realm 登出矩阵与跨用户已撤销 family 用例；成本高于当前风险，先登记 |

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

> M0-E 开始前先处理 `已采纳/目标 M0-E` 的条目：TD-012（MinerU）——已完成（自定义镜像 + 三档实测）。
> M0-F 已完成（TD-002/005/010/029~031/033~035/047/052 全部收口）。M1 开始前处理：TD-016/018/025/026/036/051/057。
