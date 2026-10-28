# AGENTS.md — 校园陪伴精灵 AI 协作规范

本文件对本项目所有 AI 编码会话生效。若与用户口头指令冲突，以用户最新明确指令为准；但安全红线、生产开关、密钥规则不得绕过。

> **新会话/新工具接手时**：先读 `docs/handoff.md`（当前状态表、环境怎么起、工作流门禁、文档地图、已知环境坑），再按本文件执行；每个功能点完成后必须同步更新 `docs/handoff.md` §1 与 `README.md` 的「当前状态」。

## 1. 最高优先级纪律

1. **先计划，后编码**：未获用户明确确认，不写生产代码、不创建业务工程文件。
2. **Spec 是唯一事实来源**：实现中发现需求矛盾或遗漏，先提出修改 Spec 并经确认，不得静默决定。
3. **单点推进**：一次只开发一个功能点；完成后必须汇报，禁止并行铺开。
4. **假设显式化**：技术选型、数据结构、边界行为、默认值必须列入 spec/design 的“决策记录”或“待确认假设”。
5. **A1–A29、H1–H4、S1–S2、R1–R8、L1–L6、K1–K3** 已确认；新增/变更决策必须回写文档。
6. **测试门禁**：单元/集成/业务场景测试通过后，必须做独立代码审查；审查问题修复后重跑受影响用例+业务场景测试，复审通过才能提交。
   - **术语**：**功能点**＝一个 TD/子能力（提交粒度不变，仍单点单 commit）；**交付批次**＝一次可推送 main 的逻辑单元（通常是里程碑内的一组相邻 TD，如 M2-UI3 全部 187a–g）。
   - **每点跑「受影响子集 + 静态门禁」**（`ruff`/`mypy`/`bandit`/`alembic check`），**全量后端 + 前端全量套件只在交付批次末跑一次**；业务场景用例不受"子集"豁免，每点都要跑自己那条链路。
   - **审查粒度不降**：每个功能点仍做一轮独立审查，出 Blocker/Major 才复审；这条与本条无关，不许顺带放松。
7. **业务输出正确性**：不能只断言 HTTP 200/字段存在；必须断言数据库状态、状态机、审计、消息/通知、对象存储、Qdrant、AI 引用/拒答/兜底等业务结果。
8. **一次只提交一个可运行逻辑单元**；提交前展示 `git status` / `git diff --staged`。

## 2. 技术栈与版本

- Python **3.13.x**（`requires-python = ">=3.13,<3.14"`，`.python-version`=3.13）；使用 uv 与 `uv.lock`。
- Node.js 24.x LTS、pnpm 11.x；`pnpm-lock.yaml` 入库。
- FastAPI、SQLAlchemy 2 async、Alembic、Pydantic v2。
- LangGraph + 自定义 Harness；LangGraph 只做图执行，业务状态机用显式状态机。
- PostgreSQL 16、Qdrant、Redis 7.4、MinIO。
- MinerU **4.0.12**；`mineru[torch]`；Standard→Basic→Flash 自动降级；DOCX/PPTX 先转 PDF 后 Standard，失败回退 Flash 原生 Office；TXT/MD 直接 Markdown。
- BGE-M3 dense + jieba/BM25 sparse + bge-reranker-v2-m3（TEI CPU）。
- Vue 3 + TypeScript + Vite；管理端 Element Plus；C 端 Vant 4（移动端 H5，不做微信小程序 MVP）。
- Langfuse Server **4.63.0** + Python SDK **5.0.2**，生产独立主机自托管，开发 optional；默认元数据+哈希；不使用 `latest`。
- 禁止擅自升级/降级上述版本；M0 兼容性验证失败时先报告，再讨论替代。

## 3. 多租户与安全红线

- 所有业务表、查询、缓存 Key、对象存储路径、Qdrant collection/alias 必须带租户边界。
- 数据访问层强制 `tenant_id` 过滤；PostgreSQL RLS 作为第二道防线；跨租默认拒绝。
- 平台用户与租户用户分 realm/audience；不得信任前端传入的 tenant_id/role。
- 任何写操作必须服务端鉴权、租户校验、幂等键+唯一约束、状态机校验、审计。
- 敏感字段（身份证/手机号/邮箱/地址/令牌/API Key/聊天原文）加密或脱敏；完整查看需权限+理由+审计。
- 密钥只从 `.env`/密钥管理读取；不得入库、不得入日志、不得回显、不得提交。
- 日志、审计、trace、Langfuse 上报不得包含明文敏感信息。
- 生产环境 `DEV_SKIP_SCHOOL_REVIEW`、`DEV_SKIP_FILE_SCAN` 必须为 false。

## 4. 知识库门禁

- 上传后只做文件落库、ClamAV、元数据登记；**审批通过后才允许解析/切块/嵌入/索引/构建**。
- 按单个文档版本审批；内容修改必须重审；标签/描述等元数据变更免审但留审计。
- 未审批调用构建接口必须返回 `409` 并审计；前端按钮禁用只是 UX。
- `TEST_PASSED` 才能发布；测试沙箱用草稿配置+标记会话，同一运行时，禁止另写 Agent。
- 已发布文档修改生成新版本；下线/删除不影响在线会话，旧版本保留至会话结束或 7 天。
- 构建进度用 SSE + 轮询兜底；分块预览含列表/详情/统计/搜索/检索测试/版本切换。

## 5. AI / Harness 规范

- 只读工具：`kb.search`、`kb.read_chunk`、`campus.navigate`；MVP 不注册写工具。
- 快路径 + 多步 ReAct：最多 5 次模型决策、3 次工具、2 次检索、30s、12k token 预算。
- 快路径首答案 token P95≤3s；ReAct 首状态≤3s、首答案≤5s；两套指标分开统计。
- 多意图：`RouteResult.intents[]`，一次合并作答，逐项兜底，不能整题拒答。
- SSE 事件：`meta/step/tool_call/tool_result/reasoning_summary/delta/citation/done/error`。
- 只展示脱敏后的步骤/摘要；不展示原始 chain-of-thought、系统提示词、工具参数、内部地址、PII、密钥；不伪造思考内容。
- 提示词必须是版本化文件，不得硬编码；Langfuse 只做草稿/实验，Git 是生产唯一事实来源。
- Langfuse 非审计日志、非运行时依赖；故障不得影响聊天。

## 6. 测试与代码审查

### 6.1 测试分层
- 单元：状态机、校验器、权限规则、切块/掩码、SSE 映射。
- 集成：真实 PG/Redis/MinIO/Qdrant；LLM/嵌入/重排 mock。
- 业务场景：真实基础设施；从 Given/When/Then 跑完整链路；断言业务输出与副作用。
- E2E：Playwright；三端登录、KB 审批/构建/分块/发布、C 端流式、请假、删除、审批、看板/反馈。
- AI 评估：Ragas 独立跑批；每类≥20 条、对抗≥3 条；A12 阈值。
- 性能：k6；核心 API、SSE、并发、发布/回滚。

### 6.2 代码审查
- 测试通过后，使用独立 review agent/上下文审查 staged diff；输出 Blocker/Major/Minor。
- 安全、租户隔离、权限、状态机、数据一致性、AI 护栏、Langfuse 脱敏问题一律 Blocker/Major。
- 修复后重跑受影响用例 + 本功能点的业务场景链路；**全量单元/集成/业务场景在交付批次末跑一次**（定义见 §1.6）。前端每点跑受影响的单测/mock E2E 文件，批次末跑 `lint`+`typecheck`+`format:check`+全部单测与 Playwright；AI 补评估在里程碑边界跑。
- 审查仍是**每个功能点一轮独立审查**，出 Blocker/Major 才复审；改成批次末一次审不属于本条授权范围。
- 实质变更重新审查；无 Blocker/Major 后才 commit。
- 审查记录写入 `docs/review-log.md`。

## 7. Git 规范

- 模式 A：`main` + `feature/*` + `hotfix/*`；无人工 PR；**不在 `main` 上直接改代码提交**（`main` 只接受来自功能分支的快进合并）。唯一例外是 Dependabot 自动创建的依赖 PR（包生态安全更新；Actions 周更仅 minor/patch）：必须按 §2 逐条审查、CI 绿后手动合并，不在 PR 上开发功能。
- **依赖自动更新**：`.github/dependabot.yml` 中包生态只开安全更新（`open-pull-requests-limit: 0` 关闭常规版本更新），Actions 周更分组只含 minor/patch；Dependabot 只开 PR，不自动合并，审查与合并仍按本节规则执行。
- 分支命名：`<类型>/<任务号>-<简短描述>`，无任务号用日期。
- Conventional Commits：`feat/fix/docs/style/refactor/perf/test/build/ci/chore/revert`。
- 提交前必须：`git status`、`git diff --staged`；检查密钥/产物/临时调试代码/大文件。
- 禁止 `git add .` / `git add -A`；禁止 force push、直接 push 共享分支、`reset --hard`、`clean -fd`、修改 hooks/GPG。
- 误提交密钥：立即停止 push，`git rm --cached`，并告知密钥已泄露、必须轮换。
- **远端与推送**：`origin = git@github.com:huluqiwa/campus-elf.git`（SSH；本机 github.com 的 HTTPS 被重置，SSH 的 22/443 通道可用）。**推送粒度＝交付批次**（§1.6）：功能点各自 commit 在功能分支上累积，批次末统一执行 功能分支提交 → `git branch -f main <feature-branch>`（等价的快进合并）→ `git push origin main`；**CI 未绿不得视为该批次完成**。批次内每个功能点仍要完成「受影响子集+业务场景通过 + 独立审查通过」才能 commit，未通过的点不得混进批次推送。
- **CI 查询**：`gh run list` / `gh run view <id> --log-failed`（新机器需 `winget install --id GitHub.cli` + `gh auth login`）；4 条工作流按目录路径过滤触发；security 在推送 main / feature 分支、面向 main 的 PR（Dependabot 安全更新）与每周一 03:00 运行。

## 8. 环境与运行

- Windows 11 + Docker Desktop/WSL2（11GB/16CPU/4GB swap）。
- 开发依赖全部容器化；禁止依赖本机全局环境。
- 默认 Compose profile 不启 Langfuse/MinerU 重服务以外的东西按需；Langfuse 开发 optional。
- 测试临时数据/文件/目录用后即清；测试/生产配置、数据库、密钥严格隔离。
- 生产部署 Linux + Docker Compose + Nginx + HTTPS；无 K8s。

## 9. 汇报格式

每个功能点完成后必须汇报：

1. 实现了什么。
2. 新增/修改/删除的文件清单。
3. 测试覆盖了哪些场景，每个测试的验证目的；业务场景与预期输出对照。
4. 代码审查 findings、修复、复测结果。
5. 当前 Spec 完成度与下一步。
6. 当前 Git 分支状态、未提交改动、本次 commit 清单。
7. 为什么采用这种写法，是否有更好的写法。
8. **与规范/最佳实践的差距**：本轮发现但尚未处理的质量、安全、流程缺口，逐条写
   「问题 → 影响（用白话，不用代号代替解释）→ 建议动作 → 去向（TD 编号/里程碑）」。
   不要因为用户没问就省略，也不要只给代号不说后果。

## 10. 更好做法与技术债闭环

1. **禁止只口头说“更好的做法”**：每个功能点完成汇报中的替代方案/优化建议，必须在 `docs/tech-debt.md` 登记为 TD 条目。
2. **每条必须有处置**：`已完成`（附 commit/功能点）、`已采纳`（指定目标里程碑）、`已延后`（原因+复查时点）、`已拒绝`（理由）。
3. **里程碑边界评审**：每个里程碑结束评审台账；未处置条目不得进入下一里程碑；`已采纳` 条目必须进入目标 milestone/spec/design。
4. **优先级**：安全、租户隔离、合规、数据一致性优先；低成本质量项批量进入 M0-F。
5. **验收**：`已完成` 必须有测试或运行验证；`已采纳` 必须在 milestone.md 有对应功能点或验收编号。
6. **代码审查联动**：审查清单必须检查“更好做法是否已登记并有处置”；没有 TD 处置的改进项按 Major 处理。

## 11. 实现前设计简报（强制，先于编码）

**每个功能点或里程碑单元在写任何代码前**，必须先产出 `docs/design-briefs/<ID>.md`：

1. **目标与验收**：引用 spec/milestone 的功能点与验收编号。
2. **候选方案 ≥2 个**：含当前默认方案；逐个写做法、优点、缺点/成本、风险、对 spec/design 的影响。
3. **选择结论**：选哪个、为什么、为什么不选其他方案；是否需要改 spec/design。
4. **实现顺序**：数据 → 接口 → 界面；TDD 红→绿→重构的测试清单。
5. **测试策略**：单元/集成/业务场景/E2E/评估/性能。
6. **预判技术债**：本次已知的取舍登记 `docs/tech-debt.md`。
7. **高风险确认**：认证、租户隔离、权限、状态机、删除、发布、加密、AI 护栏类功能，简报需用户确认后再编码。

禁止在未写简报的情况下直接编码；代码审查以简报为对照，发现未登记方案或偏离未处置的按 Major 处理。

## 12. 文档优先级

`docs/requirements.md` → `docs/spec.md` → `docs/design.md` → `docs/milestone.md` → `docs/test-plan.md`。
任何实现细节与文档冲突，以用户最新确认的 spec/design 为准；冲突必须先报告并按流程回写文档。
