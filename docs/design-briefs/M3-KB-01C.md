# M3-KB-01C — 知识库全栈 E2E 实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-11-01 |
| 状态 | 已确认（2026-11-01，D1–D4 全按推荐项） |
| 关联功能点 | M3 第 3 点：KB-01C 全栈 E2E（KB-01A 后端已合并 `3e763ca`；KB-01B 前端已合并 `d6de5a6`） |
| 验收引用 | `docs/spec.md` §3.6 KB-01（:236-245）、`docs/test-plan.md` BS-09（:66、:220）、`docs/milestone.md` §6.2（:244）/§6.4（:257）、`docs/design-briefs/M3-KB-01B-frontend.md` D9（:127-133） |
| 不做的事 | 审批（KB-02）、构建/构建进度 SSE（KB-03）、检索/分块预览（KB-06/07）；不改后端业务代码、不改生产配置、不改 CI workflow |

## 0. 现状核对（读代码与实跑栈得到的事实）

**E2E 栈现状**

- `scripts/e2e-stack.sh up` 会重置测试库 schema / Redis、在宿主跑 `alembic upgrade head`、起 `deploy/docker-compose.e2e.yml`（object-init、backend `127.0.0.1:${E2E_BACKEND_PORT:-18000}`、arq worker）、再跑 `backend/scripts/e2e_seed.py` 写 `deploy/.e2e-accounts.json`（git 忽略）。
- compose 的 `&backend-env`（`deploy/docker-compose.e2e.yml:36-49`）**没有任何扫描相关环境变量**，因此落到 `Settings` 默认值：`app_env=development`（`backend/app/config.py:41`）、`clamav_fail_closed=true`（:164）、`dev_skip_file_scan=false`（:166）；compose 里**没有 ClamAV 容器**。
- 结果是：当前栈上传成功后走真实扫描 worker，连不上 ClamAV → fail-closed 保持 `UPLOADED` + `scan.error_code=SCAN_UNAVAILABLE`，5 分钟后看门狗重试。**当前栈不可能到达 `PENDING_APPROVAL`**，KB 的正常用户结果（「待审批」）在现状栈里无法端到端验证。

**两个扫描开关（本点 D1 的候选）**

- `DEV_SKIP_FILE_SCAN=true`：上传直接写 `PENDING_APPROVAL`、`scan_engine=dev-skip`、`scanned_at=now()`、写审计 `school.kb.scan.passed`，**不入队**（`backend/app/kb/service.py:802`、`:830`、`:847`，入队判据 `:417`）。接口派生 `scan.state=skipped`（`service.py:181-193`）。
- `CLAMAV_FAIL_CLOSED=false`：扫描不可用时降级 `dev-degraded`（仍走 worker）。两个开关都被 `Settings` 在非 dev/test 环境硬拒（`config.py:236-242`），生产红线不受影响。
- 默认教师只有 `school.kb.upload`、没有 `school.kb.view`（`backend/app/rbac/seeds.py:110-119`）；`school_admin` 有 view、没有 upload（`seeds.py:91-109`）。

**全栈 E2E 脚手架现状**

- `frontend/playwright.fullstack.config.ts`：`testDir=./e2e-fullstack`、admin preview 4173 / campus 4174、`E2E_ACCOUNTS_FILE` 注入账号、timeout 60s、workers 2；`admin` project 匹配 `**/admin*.spec.ts`。
- `frontend/e2e-fullstack/helpers/support.ts`：`E2EAccounts` 接口、`assertLocalBaseUrl`（拒绝非本机与 8000）、`apiLogin/apiGet/apiPost/apiPut`；multipart 用原生 `fetch + FormData`（`admin-fullstack-m2ui.spec.ts:117-126` 有现成写法）。
- 当前全栈 18 例；**不进 CI**（TD-170/TD-207，M6 决策），本点只做本地证据。
- 栈的隔离方式是「每次 `up` 重置」，用例间不清理数据；因此断言必须按唯一数据定位，不能断言全量条数。

**种子账号现状**

- `seed_tenant_user` 授 `permissions` 时创建**非系统自定义角色**并绑定，`scope_types` 写用户级数据范围（`backend/tests/support/auth.py:102-194`）；上传写入按权限放行、不按数据范围收窄（`service.py:773` `ScopeBypass`），文档带 `created_by`。
- 现有账号没有一个能同时「上传 + 查看」：超管 `admin@e2e-scenario.edu` 首登强制改密；`e2e-role-target` 绑内置 `school_admin`（有 view、无 upload）且被 TD-192 用例改动；`e2e-ops-admin` 没有 KB 权限；`e2e-teacher` 只有 `school.personnel.view`。
- 结论：**KB 全栈用例目前没有可用账号**，D2 必须新增。

**mock E2E 现状（TD-282 的落点）**

- `frontend/e2e/admin-kb.spec.ts` 18 例；`frontend/e2e/helpers/kb-mock.ts` 已有 `autoPassOnDetail`、`detailDelayMs`、`missingDocumentIds`、`captures.detailIds` 等能力，但**没有「详情固定返回 403」的选项**，也没有可见性/时钟控制。
- TD-282：上传结果页轮询的 403 停止、隐藏暂停、5 分钟截止三条分支只有 `shouldPollScan` 单测，没有页面级用例；残余「隐藏页每 5s 空转 timer、不监听 `visibilitychange`」「`pollInFlight` 不区分批次」。
- dev-skip 上传后没有 pending 文档，上传页**不会开始轮询**，所以这三条分支天然无法放在全栈用例里，只能放 mock。

**前端可断言文案（本点用例要逐字核对）**

- 文档状态：`PENDING_APPROVAL=待审批`、`UPLOADED=待扫描/扫描中`、`SCAN_FAILED=扫描未通过`（`frontend/apps/admin/src/kb-logic.ts:204-212`）。
- 扫描状态：`skipped=已跳过扫描`（warning）、`pending=扫描中/待扫描`（`kb-logic.ts:218-228`）；引擎 `dev-skip=开发环境跳过扫描`（`kb-logic.ts:235`）。
- 轮询提示（`frontend/apps/admin/src/views/KbUploadView.vue:279-292`）：「扫描已全部结束。」「自动刷新已停止（超过 5 分钟）…」「自动刷新已停止（无查看权限或被服务端拒绝）…」「扫描在后台进行；你没有知识库查看权限…」。
- 无权限直连：`ForbiddenView` 文案「你没有访问该页面的权限」。

## 1. 目标

用真实后端容器 + 真实测试库 + 真实前端产物，补上 KB-01 用户侧闭环的端到端证据，并收口 TD-282 的页面级缺口：

1. **全栈主链路**：校管登录 → 上传两份文件（带元数据）→ 结果页看到「待审批 / 已跳过扫描 / 开发环境跳过扫描」→ 列表找到文档 → 抽屉看到标签、描述、版本；接口侧核对 `scan.state=skipped`、`engine=dev-skip`、`status=PENDING_APPROVAL` 与元数据落库。
2. **真实契约边界**：同一幂等键重放返回同批次；同键换文件 409；新键同文件建新文档；整批原子（好文件 + 坏文件 → 422 且好文件未落库）；上传-only 账号的列表/详情 403、菜单与路由守卫、上传成功但只看到「无查看权限」静态说明。
3. **TD-282 收口**：在 mock E2E 补 403 停止、隐藏暂停、5 分钟截止三条页面级用例（断请求次数与文案），残余两条登记新 TD。
4. **不解决**：真实 ClamAV 引擎在 E2E 栈中的行为（感染隔离、看门狗重试）、审批/构建/SSE、文档列表服务端搜索、CI 集成。

**验收（本点）**

- 全栈 22 例通过（现 18 + 新 4），mock E2E 120 例通过（现 117 + 新 3），单测 198 例不回归。
- 主链路断言到**业务结果**：`PENDING_APPROVAL` + `scan.state=skipped` + `engine=dev-skip` + 「待审批」「已跳过扫描」；不只断言 HTTP 201。
- 整批原子用例必须证明坏批次里的好文件**没有**出现在列表中（不是只看 422）。
- 上传-only 账号：后端 403、前端无列表菜单、直连进 403 页、结果页零详情请求。
- 静态门禁：`typecheck` / `lint` / `format:check` / 单测 / `pnpm build` / mock E2E / 全栈 E2E 全绿；后端仅改种子脚本，跑 `ruff` / `mypy`（受影响文件）与 `bandit`。

## 2. 候选方案

### D1 全栈栈的扫描模式

- **方案 A（推荐）：E2E compose 打开 `DEV_SKIP_FILE_SCAN`（dev-skip）**。
  - 做法：只在 `deploy/docker-compose.e2e.yml` 的 `&backend-env` 增加 `DEV_SKIP_FILE_SCAN: "true"`（注释写明「仅 E2E 开发环境；非 dev/test 会被 `Settings` 拒绝」）；`app_env` 保持默认 `development`，`clamav_fail_closed` 保持默认 `true`。
  - 优点：确定性、零额外容器、启动快；上传后立刻是终态，主链路不用等扫描；`skipped` 本身也是 KB-01B 必须展示的一种真实状态（开发/降级），用例同时验证「不得显示成通过」的警告口径。
  - 缺点/成本：E2E 不再经过 arq 扫描 worker 与 ClamAV，「真实引擎→通过/感染」这条链路没有全栈证据；该缺口登记新 TD，由后端集成/业务测试继续兜底（`tests/integration/test_kb_scan_worker.py`、`tests/business/test_bs_m3_01_kb_upload.py` 已覆盖）。
  - 风险：有人误把开关带进非 E2E 环境——由 `config.py:236-238` 硬拒兜底；compose 文件单独注释。
  - 对 spec/design 的影响：无，仅测试编排。
- 方案 B：加真实 ClamAV 容器（`clamav/clamav`），`CLAMAV_HOST=clamav`。
  - 优点：可验证真实引擎、EICAR 感染隔离、worker 链路。
  - 缺点：镜像约 1.5GB、`start_period 360s`，本机 Docker 资源 11GB 下启动慢且易抖动；`stable` 是浮动 tag（与「不使用 latest/浮动版本」的版本纪律冲突，需锁 digest）；E2E 每次等待病毒库更新，时间不可控。
  - 风险：CI/本机网络差时病毒库下载失败，造成与代码无关的红灯。
- 方案 C：保持现状（fail-closed）。
  - 缺点：所有文档停在 `UPLOADED` + `SCAN_UNAVAILABLE`，主链路（待审批）无法验证；只能测「扫描故障」一种形态，等于把 KB-01C 做成错误路径验收。不选。

### D2 全栈账号

- **方案 A（推荐）：新增两个专用账号**。
  - `e2e-kb-admin`：`user_type=school_admin`，权限 `school.kb.upload` + `school.kb.view`，数据范围 `school`（看全校）。
  - `e2e-kb-uploader`：`user_type=teacher`，权限仅 `school.kb.upload`，数据范围 `self`（默认教师的真实形态，TD-278 的现实场景）。
  - 口令：`secrets` 随机生成（`Kb-` 前缀 + `token_urlsafe`），只写 git 忽略的账号文件、不打印、不入日志；避免新增固定字符串触发 gitleaks `generic-api-key` 误报。种子末尾追加登录烟测，失败即中止。
  - 优点：与 TD-192 的角色/账号零交叉；上传-only 账号能同时验证「真实后端 403 + 前端菜单/守卫/静态说明」，这是 mock 无法替代的部分；不污染 `school_admin` 内置角色的默认权限。
  - 缺点：种子多两个账号、账号文件多两个条目；随机口令每次 `up` 变化（无人需要记住，读账号文件即可）。
- 方案 B：复用 `e2e-role-target`（内置 `school_admin`，有 view）再补 upload。
  - 缺点：它被 TD-192 用例当作「撤权/恢复出厂」的目标，任何权限改动都会与那些用例互踩；且会模糊「内置角色默认」的语义。不选。
- 方案 C：复用一个两者兼备的账号。
  - 缺点：无法验证上传-only 的真实 403 与「无查看权限」体验（TD-278），只能再靠 mock，等于把权限边界留给假接口。不选。

### D3 TD-282 的落点

- **方案 A（推荐）：三条分支全部放 mock E2E，本点收口页面级缺口**。
  - 403 停止：给 `kb-mock` 增加 `detailError?: { status, body }`（详情固定返回 403）+ `autoPassOnDetail:false`（文档保持 pending）；断言首个详情请求后轮询停止（再等 5–6 秒请求数不增长）且文案切到「自动刷新已停止（无查看权限或被服务端拒绝）…」。
  - 隐藏暂停：`page.addInitScript` 覆盖 `document.visibilityState` 的可写开关；上传后置为 hidden，等一轮（≥5s）断言**零**详情请求、文案仍是普通说明（不是「已停止」）；恢复 visible 后能在下一轮看到请求并最终「扫描已全部结束。」。
  - 5 分钟截止：`addInitScript` 包一层只影响 `Date.now` 的偏移函数（不动 `setTimeout`）；上传完成后把偏移推到 5 分钟以上，下一轮 tick 命中 `elapsed >= SCAN_POLL_WINDOW_MS` 即停，断言零请求 + 超时文案，再等一轮确认不再请求。
  - 优点：`shouldPollScan` 的判据与页面行为终于连起来；不用造角色/撤权账号，不动 TD-192 的串行链路；`Date.now` 偏移只影响「已过时长」判断，Element Plus 的动画/消息定时器不被劫持（比 `page.clock.install()` 稳）。
  - 缺点：三条用例各含 1–2 次真实的 5 秒等待，合计约 35 秒；只在 mock 层证明，真实后端 403 的形态由全栈权限用例另证。
  - 风险：`document.visibilityState` 覆盖依赖 Chromium 的可配置属性；Playwright 用 `Desktop Chrome`，可行；若被浏览器限制，回退为通过 `Object.defineProperty(Document.prototype, "visibilityState", …)` 注入并在用例注释说明。
- 方案 B：把 403 分支放全栈（用角色管理员撤掉 `e2e-kb-admin` 的 view）。
  - 缺点：需要额外角色管理账号与串行编排，且撤权会改变账号后续用例的前提；隐藏/超时仍要回 mock，等于两套都得做。不选。
- 方案 C：继续延后。
  - 缺点：审查已把它列为 Minor 并指定 KB-01C；再延会变成第二次欠账。不选。

### D4 全栈 E2E 与 CI

- **方案 A（推荐）：维持本地执行，不加入远端 CI**（TD-170/TD-207 口径）。
  - 做法：本点只在本地跑全栈并记录命令、例数、时间；PR 描述与 `docs/m1-acceptance.md` §6 更新复现方式。
  - 优点：不引入 Docker-in-CI、镜像构建、依赖栈供给与 Actions 分钟成本；与 M6「部署/CI 硬化」一并决策。
  - 缺点：合并后没有自动回归；靠里程碑验收与本地点跑兜底（现状即如此）。
- 方案 B：本点顺手把全栈挂进 frontend workflow。
  - 缺点：需要 backend 镜像构建、test 依赖栈（PG/Redis/MailHog/Qdrant）、seed、preview 与端口编排，diff 从测试跨到 CI/部署层；与 TD-170 的 M6 决策重复。不选。

## 3. 选择结论

- **D1 A（dev-skip）／D2 A（两个专用账号）／D3 A（三条分支进 mock）／D4 A（全栈保持本地）**。

### 3.1 范围内子决策（随简报一并确认）

- **compose**：只加 `DEV_SKIP_FILE_SCAN: "true"`，放在 `&backend-env`，注释「仅 E2E；非 dev/test 环境会被 Settings 拒绝（config.py:236-238）」；不加 `APP_ENV`、不加 `CLAMAV_*`。
- **账号字段**：账号文件新增 `kb_admin`、`kb_uploader`（各含 `school_code/login_id/password`）；`E2EAccounts` 接口同步扩展；两个账号都进登录烟测。
- **全栈用例文件**：新建 `frontend/e2e-fullstack/admin-fullstack-kb.spec.ts`，`test.describe.configure({ mode: "serial" })`；每例开头 `runId = Date.now()`，标题/文件名带 `runId`，断言按唯一数据查找，不做全量条数断言。
- **4 条全栈用例**：
  1. **UI 主链路**：UI 登录 `e2e-kb-admin` → 菜单「上传文档」→ 选 `.md`（UTF-8）+ `.pdf`（`%PDF-1.4` 头）→ 展开元数据，给第一行填 `标题/标签/描述` → 提交 → 断言结果表 2 行、批次号、`replayed=false`、「待审批」「已跳过扫描」「开发环境跳过扫描」、「扫描已全部结束。」；再进「知识库」按标题找到文档、打开抽屉断言标签/描述/v1/文件名/版本表；接口侧核对 `status`、`scan.state=skipped`、`engine=dev-skip`、`mime`、`size_bytes` 与元数据。
  2. **幂等**（Node API，`apiLogin` + 原生 `FormData`）：同键同文件两次 → 第二次 `replayed=true` 且 `batch_id`/文档 id 相同；同键换文件 → 409 `IDEMPOTENCY_KEY_REUSED`；新键同文件 → 新文档 id。
  3. **整批原子**（UI）：选 `[good-<runId>.md, bad-<runId>.md]`，坏文件内容含 NUL（UTF-8 可解码，命中 `TEXT_BINARY_CONTENT`，`backend/app/kb/validation.py` 的文本校验）；前端本地预检不拦；提交后断言 `kb-file-errors` 出现坏文件名与 `TEXT_BINARY_CONTENT`；再用接口列表证明 `good-<runId>.md` 不存在。
  4. **权限边界**：`e2e-kb-uploader` API `GET /documents` 与 `GET /documents/{id}` → 403；UI 登录后菜单只有「上传文档」；直连 `/kb/documents` 进 403 页（「你没有访问该页面的权限」）；UI 上传成功后出现「你没有知识库查看权限」静态说明，且页面全程**零**详情请求（`page.on("request")` 计数）；`e2e-kb-admin` 的列表能看到这份 self 范围上传的文档（`school` 范围覆盖全校）。
- **文件内容**：`.md` 用纯文本；`.pdf` 用最小合法头 `%PDF-1.4\n...\n%%EOF\n`（仅魔数校验，不解析）；坏文件用 `a\0b` 字节（合法 UTF-8 + NUL），不要用非法 UTF-8（会得到 `TEXT_ENCODING_INVALID`，测不到 NUL 分支）。
- **幂等键**：`kb-e2e-<runId>-a/b/c`，满足 8–128 位 `[A-Za-z0-9._:-]`。
- **mock 扩展**：只加 `detailError`（详情固定返回错误）；隐藏/时钟用用例内 `addInitScript`，不改生产代码、不加测试专用 URL 参数。
- **计数口径**：全栈 18→22；mock E2E 117→120（`admin-kb.spec.ts` 18→21）；单测不变（198）。
- **不做**：跨租第二租户、413/308MB 大文件、>20 页游标翻页、真实 EICAR 感染、全栈入 CI、审批/构建。

## 4. 实现顺序与 TDD 测试清单

1. **编排层**：`deploy/docker-compose.e2e.yml` 加 `DEV_SKIP_FILE_SCAN`；`bash scripts/e2e-stack.sh up` 重建。
2. **种子**：`backend/scripts/e2e_seed.py` 加两个账号（随机口令、登录烟测、账号文件条目）；`frontend/e2e-fullstack/helpers/support.ts` 扩展 `E2EAccounts`。
3. **全栈用例（先跑红）**：新建 `admin-fullstack-kb.spec.ts`，按 4 条用例拆两次跑：先主链路（1），红→绿后再补（2）（3）（4）。
4. **mock 收口**：`kb-mock.ts` 加 `detailError`；`admin-kb.spec.ts` 追加 3 条页面级用例（403/隐藏/超时）；先确认现状下这三条会失败，再补选项/驱动。
5. **门禁与审查**：`pnpm typecheck/lint/format:check`、admin 单测、`pnpm build`、mock E2E、全栈 E2E；后端 `ruff check`/`mypy`/`bandit`（只动 `scripts/e2e_seed.py`）；独立审查 `git diff --staged`。
6. **文档**：本简报状态回填、`test-plan.md` BS-09 证据、`milestone.md` 进度、`handoff.md` §1、README「当前状态」、`review-log.md`、`tech-debt.md`（TD-282 收口 + 新 TD）。

红→绿清单：

- [ ] 全栈 1：真实上传 → `PENDING_APPROVAL`/`skipped`/`dev-skip` + 元数据 + 列表/抽屉
- [ ] 全栈 2：幂等重放（同 id）/同键换文件 409/新键同文件新 id
- [ ] 全栈 3：整批 422 逐文件错误 + 好文件未落库
- [ ] 全栈 4：上传-only 403、菜单/守卫、静态说明、零详情请求、admin 可见
- [ ] mock 1：详情 403 → 首轮后停止 + 文案 + 请求数不增长
- [ ] mock 2：隐藏 → 零请求 + 文案不变；恢复 visible → 继续并结束
- [ ] mock 3：`Date.now` 偏移过 5 分钟 → 停止 + 超时文案 + 零请求
- [ ] 既有 18 全栈 + 18 mock KB 用例不回归

## 5. 测试策略

| 层级 | 内容 |
|---|---|
| 单元 | 无新增（`shouldPollScan`/文案已由 KB-01B 单测覆盖，本点不复制判据） |
| mock E2E | `admin-kb.spec.ts` +3：TD-282 三条停止分支的页面级行为与请求次数 |
| 全栈 E2E | `admin-fullstack-kb.spec.ts` +4：主链路、幂等、整批原子、权限边界 |
| 后端 | 只改种子脚本；接口/服务零改动，既有 917 例不重跑全量（本地跑受影响静态门禁，PR 由 CI 跑全量） |
| 安全/权限 | 上传-only 账号真实 403；跨范围 404/403 不泄漏；`DEV_SKIP_FILE_SCAN` 仅测试编排，生产开关红线不动 |
| 性能 | 不做（全栈为功能验收，不是压测） |

## 6. 预判技术债

| TD 编号 | 条目 | 处置 |
|---|---|---|
| TD-284（新） | **全栈 E2E 用 dev-skip，未覆盖真实 ClamAV 引擎**：worker 扫描、感染隔离、fail-closed/看门狗在真实栈中没有端到端证据；后端集成/业务测试覆盖，但「容器 + 病毒库 + E2E 栈」组合未被验证 | 已延后 → M6（与 TD-170 全栈入 CI、部署硬化同批决定是否加 ClamAV profile 与 EICAR 用例） |
| TD-282（既有） | 三条轮询停止分支无页面级 E2E | **本点已完成页面级三条**；残余「隐藏页每 5s 空转 timer / 不监听 visibilitychange」「`pollInFlight` 不区分批次」拆出 TD-285 |
| TD-285（新） | 上传结果页轮询的两处内部残余：隐藏时仍有空转 timer（不发请求，用户不可见）；`pollInFlight` 跨批次，旧批次在途会让新批次首轮延后 5 秒（无数据污染） | 已延后 → M6（若真实运营反馈上传后首轮刷新偏慢，再改 `pollInFlight` 为按 generation 记录） |
| TD-170 / TD-207（既有） | 全栈 E2E 未进 CI/夜间 | 维持已采纳 → M6，不在本点改变 |

## 7. 高风险确认

- **是否属于高风险功能**：是（权限边界、租户数据范围、上传入口、扫描开关）。本点不改生产代码与生产配置，风险集中在「测试账号权限是否最小」「dev-skip 是否只存在于 E2E 编排」「断言是否真的证明业务结果」。
- **需要用户确认的点**（推荐项在前）：
  1. **D1 扫描用 dev-skip**：E2E 栈打开 `DEV_SKIP_FILE_SCAN`，主链路验证「待审批 + 已跳过扫描」，不加 ClamAV 容器；真实引擎缺口登记 TD-284 延后 M6。
  2. **D2 新增两个专用账号**：`e2e-kb-admin`（上传+查看、全校范围）、`e2e-kb-uploader`（仅上传、本人范围），随机口令只写 git 忽略的账号文件。
  3. **D3 TD-282 三条分支进 mock**：403 停止、隐藏暂停、5 分钟截止；用 `detailError` + visibility/`Date.now` 注入，不引入生产测试钩子。
  4. **D4 全栈仍只在本地跑**：不把全栈 E2E 加进远端 CI，维持 TD-170 的 M6 决策。
- **确认结论**：用户 2026-11-01 确认全部按推荐项执行（D1 dev-skip、D2 两个专用账号、D3 三条分支进 mock、D4 全栈只在本地）。

## 8. 审查对照

- 审查时重点验证：`DEV_SKIP_FILE_SCAN` 只出现在 E2E compose 且有注释、生产校验未被改动；两个种子账号权限最小（uploader 无 view）、口令不打印不提交；全栈用例断言业务状态（`PENDING_APPROVAL`/`skipped`/`dev-skip`/元数据/整批未落库）而非只看状态码；上传-only 的 403/菜单/守卫/零详情请求四条都成立；用例数据唯一、互不依赖、可重复跑；mock 三条分支断言请求次数与准确文案；没有为测试修改 `frontend/apps/**` 或 `backend/app/**`；未触碰 `components.d.ts`。
- 允许的偏差：全栈仅本地执行（TD-170）；真实 ClamAV/感染/看门狗不在本点（TD-284）；全栈不覆盖 >20 页分页与大文件 413（mock 已覆盖 413 文案，分页由 mock 覆盖）；隐藏分支不要求消除空转 timer（TD-285）。
