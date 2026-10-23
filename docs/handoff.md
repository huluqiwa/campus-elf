# 接手手册（Handoff）— 校园陪伴精灵

> 用途：**任何新的 AI 编码工具或新会话接手本项目时，先读本文件**，再读 `AGENTS.md`（规则源头）。
> 本文件只写"状态 + 怎么跑 + 怎么交接"，规则细节一律以 `AGENTS.md` 为准；两者冲突时以 `AGENTS.md` 与用户最新指令为准。
> 维护要求：**每个功能点完成后必须更新本文件的 §1 状态表与 README 的「当前状态」**，避免交接信息过期。

---

## 1. 当前状态（截至 2026-10-23）

| 里程碑 | 功能点 | 状态 |
|---|---|---|
| M0 基座 | 全部（M0-A~M0-F） | ✅ 完成，验收报告 `docs/m0-acceptance.md` |
| M1 租户与身份 | M1-A 租户基座（RLS/三连接/仓储/上下文/中间件） | ✅ 完成（两轮审查通过） |
| M1 | M1-B 学校注册与平台审核（TEN-01/02/03） | ✅ 完成（五轮审查通过） |
| M1 | M1-C 三端登录与令牌**后端**（AUTH-01/02） | ✅ 完成（三轮审查通过） |
| M1 | M1-C-UI 三端登录页/强制改密页/守卫/HttpOnly 刷新 Cookie | ✅ 完成（两轮审查通过） |
| M1 | M1-C2 会话设备管理（AUTH-06，策略 C） | ✅ 完成（五轮审查通过；设备限额/列表/下线/退出其它/被踢即时失效；简报 `docs/design-briefs/M1-C2-session-devices.md`） |
| M1 | M1-D1 密码策略/历史/邮件重置、失败锁定（AUTH-03 员工/平台部分、AUTH-05） | ✅ 已完成（第五轮复审通过、348 passed、远端 CI 绿；简报 `docs/design-briefs/M1-D1-password-lockout.md`） |
| M1 | M1-D2 MFA（AUTH-04） | ✅ 完成（两轮审查通过、第二轮 0B/0M、后端 411 passed、前端 32 单测 + 28 E2E、远端 CI backend/frontend/security 全绿；简报 `docs/design-briefs/M1-D2-mfa.md`） |
| M1 | M1-E1 鉴权内核：权限矩阵 59 码落库 + 8 内置角色出厂默认 + 接口声明权限（RBAC-01 部分） | ✅ 完成（第五轮复审通过 0B/0M/3 Minor；后端 481 passed、ruff/mypy/alembic check/bandit/pip-audit 本地全绿、head=`e5f7a9c1b3d5`；远端 CI backend #39851973194 / security #39851973099 success；逐轮记录见 `docs/review-log.md`，简报 `docs/design-briefs/M1-E1-rbac-core.md`） |
| M1 | M1-E2 RBAC 数据范围与服务端过滤、审批职责分离、平台跨租只读（RBAC-02/03，含 TD-149 触发器收紧） | ✅ 完成（两轮审查：第一轮 0B/3M/4M 全部修复、第二轮主上下文 0B/0M（当日独立审查子智能体配额耗尽）；后端 561 passed、ruff/mypy 149 文件/alembic check/bandit×2 本地全绿、head=`f7a9b1c3d5e8`；远端 CI backend #39950816551 / security #39950816785 success；逐轮记录见 `docs/review-log.md`，简报 `docs/design-briefs/M1-E2-data-scope.md`） |
| M1 | M1-F 审计落库与哈希链（AUD-01） | ⏳ 未开始 |
| M1 | M1 验收报告 | ⏳ 未开始（需含 TD-110 真实后端全栈 E2E） |
| M2/M3/M4 | 组织与人员 / 知识库闭环 / AI 与 C 端 | ⏳ 未开始（已含 SC-01 新增 ORG-05、KB-09/KB-10、AI-10） |

- **建议的下一步顺序**：M1-F（审计哈希链）→ M1 验收报告（含 TD-110 真实后端全栈 E2E）→ M2 → M3 → M4。
- 最近提交：`main` 已含 M1-D2 MFA、M1-E1 鉴权内核与 M1-E2 数据范围/职责分离交付（不写具体哈希，避免每次提交后自相矛盾）；工作树应保持干净。
- 远端：`origin = git@github.com:huluqiwa/campus-elf.git`（SSH；本机 github.com 的 HTTPS 被重置，SSH 的 22/443 可用）。
- 远端 CI：4 条工作流（backend / frontend / evals / security），按目录路径过滤触发；**CI 未绿不得视为功能点完成**。
- 数据库迁移 head：`f7a9b1c3d5e8`（M1-E2 数据范围/职责分离/设置表）；`alembic check` 应无漂移。

---

## 2. 环境与运行（Windows 11 + Git Bash + Docker Desktop/WSL2）

```bash
# 0) 一次性：本地配置（.env 不入库）
cp .env.example .env      # 必须补 JWT_SECRET（≥32 字节，openssl rand -hex 32）与 ENCRYPTION_KEK（32 字节 base64，openssl rand -base64 32）

# 1) 起测试栈（postgres/redis/qdrant/mailhog → 4 个 healthy）
bash scripts/test-stack.sh up
eval "$(bash scripts/test-stack.sh env)"      # 导出 TEST_* 连接串（含 TEST_SMTP_*、TEST_MAILHOG_API）

# 2) 后端
cd backend
uv sync --frozen
uv run ruff check app tests
uv run mypy --no-incremental app tests
REQUIRE_TEST_SERVICES=1 uv run pytest -q          # 应全绿（当前 411 passed）
uv run alembic -x db_url="$TEST_DATABASE_URL" check   # 迁移漂移检查

# 3) 前端
cd ../frontend
pnpm install --frozen-lockfile
pnpm lint && pnpm format:check && pnpm typecheck && pnpm test:unit && pnpm build
pnpm test:e2e                                     # 需系统 Chrome（channel: chrome），当前 32 单测 / 28 E2E

# 4) 一键本地 CI（与远端工作流一一对应；--with-e2e 追加 Playwright）
cd .. && bash scripts/ci.sh            # 或 bash scripts/ci.sh --with-e2e
```

### 已知环境坑（本机真实踩过，务必先处理）

1. **系统代理会劫持 127.0.0.1**：若本机开了代理，httpx/uv 会把 localhost 流量发给代理端口 → MailHog/Qdrant 连接被拒、`uv` 拉包失败。跑测试/工具链前先执行：
   `export NO_PROXY="127.0.0.1,localhost,::1,*" no_proxy="127.0.0.1,localhost,::1,*"`
   （测试侧已在 `backend/tests/conftest.py` 设默认值，但 `uv`/`pnpm` 需要 shell 层设置。）
2. **不要用 `python3`**：本机 `python3` 是 Windows Store 桩，会静默失败；统一用 `uv run python` 或 `python`。
3. **Windows 与编码**：`alembic.ini` 必须纯 ASCII（GBK 解析问题）；脚本文件保持 UTF-8。
4. **测试库重置**（仅测试库）：`drop schema public cascade; create schema public; grant usage on schema public to public;` 然后 `alembic ... upgrade head`。
5. **MailHog 端口**：测试栈 SMTP=51025、API=58025（不是 1025/8025，那是开发栈）。测试必须用 `TEST_SMTP_*`/`TEST_MAILHOG_API`，不要写死默认端口（CI 上会因 1025 无服务而 502）。
6. **测试栈容器重启后**：偶发端口转发短暂异常，重跑 `bash scripts/test-stack.sh up` 即可。
7. **Playwright 必须在 `frontend/` 根目录跑**（配置是 `frontend/playwright.config.ts`，用系统 Chrome + `webServer` 起 4173/4174 预览）。在 `frontend/e2e/` 里跑找不到配置 → 既没有 `baseURL`（`Cannot navigate to invalid URL`），又会退回未安装的 Playwright Chromium（`Executable doesn't exist ... chromium_headless_shell`）；本机装了系统 Chrome，正确做法是回根目录跑，不必下载浏览器。
8. **`bandit -r app` 对任何 issue 都非零退出（含 Low）**，其中 B101（`assert` 用于运行时收窄类型）最容易踩：生产代码不要用 `assert` 做不变量守卫，改成把条件并入分支判断（`if x is not None and ...`）并由末尾的 `RuntimeError` 兜住落空分支。M1-D2 首次本地 CI 就是这条挂的。

### 2.1 接手时先做"环境自检"（新工具跑这几条，然后汇报，别猜）

```bash
uname -a                                   # 是否 Linux/macOS/Windows(Git Bash/WSL)
docker ps --format '{{.Names}} {{.Status}}' | head        # 能否操作 Docker；测试栈是否已在跑
test -f .env && echo ".env 存在" || echo ".env 缺失"      # .env 不入库，缺失需从 .env.example 复制
for k in JWT_SECRET ENCRYPTION_KEK; do grep -q "^$k=.\+" .env 2>/dev/null && echo "$k 已配" || echo "$k 缺失/为空"; done
gh auth status 2>&1 | head -5               # 能否查远端 CI
git ls-remote origin -h refs/heads/main     # 能否访问远端仓库（SSH/HTTPS 权限）
env | grep -i proxy | sed 's/=.*/=<set>/'   # 是否有系统代理（会劫持 127.0.0.1）
```

**要求它按这五项汇报**（每项写"能满足 / 不能满足 / 需要你提供"）：
1. 能否运行 Docker 与 bash（不能跑 Docker ⇒ 集成/业务场景测试与 e2e 无法本地执行，只能用单元测试 + 远端 CI 代替，必须在汇报里说明）；
2. 测试栈是否已启动（没起则先 `bash scripts/test-stack.sh up`）；
3. `.env` 与 `JWT_SECRET`/`ENCRYPTION_KEK` 是否就绪（缺失则从 `.env.example` 复制并分别生成 `openssl rand -hex 32` / `openssl rand -base64 32`；KEK 缺失会导致应用启动失败与 MFA 用例无法运行）；
4. 能否访问 GitHub 并查询 CI（`gh auth login` / SSH key / PAT）；
5. **是否具备"第二上下文/子 agent"能力**——这是独立审查门禁的前提；不满足时按 §6 的替代方案执行，并明确告知用户本轮的审查由谁做。

注意：**环境相关的"状态"（栈有没有起、凭据有没有配）只有用户能确认**；"配置方法"已经在 §2 里，不需要再问用户。

---

## 3. 工作流门禁（每个功能点都必须走完，顺序不可省）

1. **先写设计简报**：`docs/design-briefs/<ID>.md`，含目标与验收编号、**≥2 个候选方案**、选择结论与理由、实现顺序（数据→接口→界面）、TDD 红→绿清单、测试策略、预判技术债、高风险确认；**高风险功能（认证/租户隔离/权限/状态机/删除/发布/加密/AI 护栏）必须等用户确认后再编码**。
2. **TDD 实现**：先写失败测试，再实现；分层跑：单元 → 集成（真实 PG/Redis/MinIO/Qdrant，外部 LLM/嵌入/重排 mock）→ **业务场景**（从 Given/When/Then 跑完整链路，断言业务输出与副作用，不只看 HTTP 200）。
3. **独立代码审查**：**另起一个独立上下文/子 agent** 审查 `git diff --staged`，输出 Blocker/Major/Minor；安全、租户隔离、权限、状态机、数据一致性、AI 护栏、Langfuse 脱敏问题一律按 Blocker/Major。
4. **修复 + 复测**：修完重跑"受影响用例 + 全量单元/集成/业务场景"（前端补 Playwright，AI 补评估）；实质变更要再复审，直到无 Blocker/Major。
5. **提交 + 推送 + 远端 CI 绿**：
   ```bash
   git add <明确路径...>            # 禁止 git add . / -A
   git status && git diff --staged  # 检查密钥/产物/调试代码/大文件
   git commit -m "feat(<scope>): ..."   # Conventional Commits
   git branch -f main feature/M0-foundation && git push origin main
   gh run list --limit 4 && gh run view <id> --log-failed   # 必须看到 success
   ```
   禁止：`--force`、`reset --hard`、`clean -fd`、直接 push 共享分支（本项目 main 只接受来自功能分支的快进合并）。
6. **汇报**：按 `AGENTS.md` §9 的 7 段格式（实现了什么 / 文件清单 / 测试与业务场景对照 / 审查 findings 与修复复测 / Spec 完成度与下一步 / Git 状态 / 为什么这样写与更好的做法）。
7. **技术债闭环**：任何"更好的做法"必须登记 `docs/tech-debt.md` 并给处置（已完成/已采纳/已延后/已拒绝）；里程碑边界要评审台账。
8. **回写文档**：更新 `docs/design-briefs/<ID>.md` 实现结果、`docs/review-log.md`、`docs/tech-debt.md`、相应设计/规格章节，以及本文件 §1 与 README 的「当前状态」。

---

## 4. 文档地图（谁是事实来源）

| 文档 | 作用 |
|---|---|
| `AGENTS.md` | **规则源头**：纪律、红线、门禁、Git/汇报规范（优先级最高） |
| `docs/requirements.md` → `docs/spec.md` → `docs/design.md` → `docs/milestone.md` → `docs/test-plan.md` | 事实来源链（冲突时按此顺序，用户最新确认优先） |
| `docs/design-briefs/*.md` | 每功能点的实现前简报 + 实现结果 + 审查修复记录 |
| `docs/spec-changes/SC-*.md` | 规格变更提案（已确认的决策编号 A30/A31、K4/K5 等） |
| `docs/milestone.md` | 里程碑范围与退出标准（M1~M6） |
| `docs/test-plan.md` | 测试分层、业务场景矩阵（BS-xx）、A23 隔离矩阵、AI 评估 |
| `docs/rbac-matrix.md`、`docs/state-machines.md`、`docs/tenant-isolation.md` | 权限矩阵 / 状态机 / 隔离规范 |
| `docs/tech-debt.md` | 技术债台账（TD-001~TD-110），未处置不得进下一里程碑 |
| `docs/review-log.md` | 审查与复审记录（每轮结论、发现、修复） |
| `docs/deployment.md` | 部署、三连接角色、生产强制配置、启动自检 |
| `docs/m0-acceptance.md`、`docs/benchmarks-m0.md` | M0 验收与基准 |

### 4.1 阅读范围建议（省上下文，别全读）

**第一档 · 必读且以它为准**（接手时一次读完）：
`AGENTS.md`（规则源头）→ `docs/handoff.md`（本文件，现状与操作）→ `docs/milestone.md`（下一步范围与退出标准）→ `docs/spec.md` + `docs/design.md` 的对应章节 → `docs/tech-debt.md`（未处置条目）→ `docs/review-log.md`（最近审查结论）→ **正在做的那个功能点的** `docs/design-briefs/<ID>.md`。

**第二档 · 按需查**（做相关功能时才打开）：
`docs/test-plan.md`（写测试/业务场景矩阵时）、`docs/rbac-matrix.md`（权限）、`docs/state-machines.md`（状态流转）、`docs/tenant-isolation.md`（隔离与 A23 矩阵）、`docs/deployment.md`（部署/生产配置/自检）、`docs/compliance.md`（留存与合规）、`docs/observability-langfuse.md`（观测）。

**第三档 · 仅供参考，接手时不必读**：
已完成功能点的简报（只读在做的那个）、`docs/m0-acceptance.md`、`docs/benchmarks-m0.md`、`docs/spec-changes/README` 之外的提案（已确认的当作决策查，不必通读）。

**要求新工具汇报时明确说清**（这样你能一眼看出它有没有读对）：
1. 读了哪几份文档；2. 下一步的依据是哪一份（例如"依据 milestone §5 + 简报 M1-C2"）；3. 哪些文档它认为仅供参考；4. 有没有互相矛盾或与代码不一致的地方。
**发现文档互相冲突时不得自行决定**：按 `AGENTS.md` §12 的优先级（requirements → spec → design → milestone → test-plan，用户最新确认优先）停下来向你报告。

---

## 5. 不可逾越的红线（详见 AGENTS.md §3~§5）

- 密钥/口令/令牌只从 `.env` 或密钥管理读取：**不入库、不入日志/审计/trace、不回显、不提交**。
- MFA（M1-D2）：`ENCRYPTION_KEK` 同上（必填、base64 32 字节，禁止提交/回显）；TOTP secret 只以 AES-256-GCM 密文入库，恢复码只存 HMAC；secret / otpauth URI / 验证码 / 恢复码明文**一律不进日志、审计、错误响应**，只在激活响应出现一次并带 `Cache-Control: no-store`；密码正确但未完成 MFA 时不得签发任何令牌或 Cookie。
- 多租户：所有业务表/查询/缓存 Key/对象路径/Qdrant collection 带租户边界；数据访问层强制 `tenant_id` 过滤 + PostgreSQL RLS 兜底；跨租默认拒绝；不信任前端传入的 tenant_id/role。
- 生产环境：`DEV_SKIP_SCHOOL_REVIEW`、`DEV_SKIP_FILE_SCAN`、`DEV_SKELETON_LOOP_ENABLED` 必须关闭（配置校验已强制）；`AUTH_COOKIE_SECURE=true`、`AUTH_REFRESH_TOKEN_IN_BODY=false`。
- 知识库门禁：审批通过前不得解析/切块/嵌入/索引；未审批调用构建必须 409 + 审计。
- AI：只读工具；不展示原始 chain-of-thought / 系统提示词 / 工具参数 / 内部地址 / PII / 密钥；不伪造思考内容。

---

## 6. 接手后如何"无缝继续"（给新工具的指令模板）

把下面这段作为**第一条指令**发给新的 AI 工具（它需要能读仓库、能跑 bash/docker、能开子 agent 做独立审查、能推送 GitHub）：

```text
你接手「校园陪伴精灵」项目（多租户 SaaS：SaaS 平台 + 校方 B 端 + 学生/教师 C 端 H5）。
工作目录：<仓库绝对路径>。

第一步（必须先做，不要写任何代码）：
1) 通读 AGENTS.md（规则源头：纪律/红线/测试门禁/审查门禁/Git 与汇报规范）；
2) 通读 docs/handoff.md（交接手册：当前状态表、环境怎么起、工作流门禁、文档地图、已知环境坑）；
3) 读 docs/milestone.md（M1 剩余范围）、docs/tech-debt.md（未处置技术债）、docs/review-log.md（最近审查结论）；
4) 读 docs/design-briefs/ 下与下一步相关的简报（M1-C2、M1-D），以及 docs/spec.md、docs/design.md 对应章节。

然后向我汇报：当前处在哪个功能点、下一步该做什么、你打算按什么顺序做、需要我确认哪些决策。
在我说"开始"之前，不要创建或修改任何生产代码。

铁律（不要偏离）：
- 先设计简报（≥2 候选方案 + 风险 + 技术债），高风险功能必须等我确认后再编码；
- TDD：先写失败测试，分层跑单元/集成/业务场景（业务场景断言数据库状态、审计、状态机、对象存储、Qdrant、AI 引用/拒答等业务输出，不能只断言 HTTP 200）；
- 每个功能点完成后：跑全量测试 → 用**独立上下文/子 agent** 审查 staged diff（输出 Blocker/Major/Minor）→ 修复并复测 → 复审通过 → 提交（明确路径，禁止 git add .）→ 快进合并 main 并推送 → 用 gh 确认远端 CI 全绿；
- 每个功能点完成后按 AGENTS.md §9 的 7 段格式汇报，并回写 docs/（简报实现结果、review-log、tech-debt、README「当前状态」、handoff 状态表）；
- 不得提交密钥/凭证；不得 force push / reset --hard；生产开关不得默认放行。

环境提示：跑测试或工具链前先 `export NO_PROXY="127.0.0.1,localhost,::1,*"`（本机代理会劫持 127.0.0.1）；
起测试栈 `bash scripts/test-stack.sh up` 并 `eval "$(bash scripts/test-stack.sh env)"`；一键 CI `bash scripts/ci.sh`。

现在开始第一步，读完汇报，不要写代码。
```

### 若新工具不支持"子 agent"做独立审查

审查门禁不能省，替代做法（按优先级）：
1. 用**第二个会话/第二个工具**专门审查（给它的输入是 `git diff --staged` 与验收清单，要求输出 Blocker/Major/Minor）；
2. 至少让同一个工具在"审查模式"下重读 diff（要求它逐条对照 `docs/code-review-checklist.md` 与 AGENTS §6.2，并明确说出"哪些点已核对、证据是什么"）；
3. **人工交替审查**：把 diff 与 checklist 交给你本人或另一位同事确认后，再提交。

### 需要事先准备好的东西

- 仓库访问：`git@github.com:huluqiwa/campus-elf.git` 的 SSH key，或改用 HTTPS + PAT；`gh auth login`（查询 CI 用）。
- Docker Desktop 已启动（测试栈 4 个容器）。
- 本地 `.env`（从 `.env.example` 复制，必须补 `JWT_SECRET` 与 `ENCRYPTION_KEK`）。
- 明确告诉它"哪个模型/工具负责写代码、哪个负责审查"，避免自查自批。

---

## 7. 交接检查清单（交接前逐条确认）

- [ ] 工作树干净（`git status`），无未提交改动、无临时文件。
- [ ] `main` 已推送且远端 CI 全绿（backend/frontend/evals/security）。
- [ ] `docs/handoff.md` §1 状态表与 README「当前状态」是最新的。
- [ ] 未处置技术债清单已读（`docs/tech-debt.md`），知道哪些条目目标在下一个里程碑。
- [ ] 待确认决策已列明（当前：无阻塞；M1-C2 四项细节已确认）。
- [ ] 本机环境可复现：测试栈起来、后端/前端测试全绿、`scripts/ci.sh` 全绿。
