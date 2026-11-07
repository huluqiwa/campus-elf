# M3-KB-02B — 审批中心前端（KB-02 前端半）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-11-07 |
| 状态 | 已实现（2026-11-07，用户确认全按推荐项执行；独立审查两轮整改中，合并事实与 CI 读数待回填 §9） |
| 关联功能点 | M3 第 5 点：KB-02B 审批中心前端（KB-02 后端已合并 `edc34f6`，全部接口真实可用） |
| 验收引用 | `docs/spec.md` §3.6 KB-02（:250-258）、`docs/design.md` §5.10（:629-639）、`docs/design-briefs/M3-KB-02.md` §1（后端契约已交付）、KB-01B 前端先例（简报 `M3-KB-01B-frontend.md` D1–D9） |
| 不做的事 | 后端改动（`backend/` 零改动，见 D2 驳回理由可见性的边界）；构建进度展示（KB-03）；请假/账号删除的审批类型（M5）；全栈 E2E（TD-170 口径维持仅本地，本点不新增）；未审批文档的编辑/再上传（KB-05） |

## 0. 现状核对（读代码得到的事实）

**后端契约（全部已合并、真实可用）**

- 审批侧（`backend/app/api/school_approvals.py`）：`GET /school/approvals/todo|done`（cursor 分页 `limit≤100`，item = `ApprovalRequestResponse`：`id/type_code/title/status/result/reject_reason/submitter_id/document_version_id/payload/current_step/created_at/completed_at`）、`GET /school/approvals/{id}`（详情含 `steps[]` 与 `actions[]`：`action ∈ submit/approve/reject/cancel` + `reason/actor_id/created_at`）、`POST .../approve|reject`（body `{reason?}`，驳回空理由 422 `REJECT_REASON_REQUIRED`、理由 ≤500）、`POST .../cancel`（仅提交人，403 `APPROVAL_CANCEL_FORBIDDEN`）。错误码：`APPROVAL_NOT_FOUND` 404、`APPROVAL_ALREADY_DECIDED` 409、`SELF_APPROVAL_FORBIDDEN` 403。
- KB 侧新增（`backend/app/api/school_kb.py`）：`POST /school/kb/document-versions/{id}/submit-approval`（409 `KB_NOT_SUBMITTABLE`/`APPROVAL_ALREADY_PENDING`）、`POST /school/kb/document-versions/{id}/build`（409 `KB_NOT_APPROVED`/`BUILD_ALREADY_ACTIVE`）、`PUT /school/kb/documents/{id}`（元数据免审）。
- `done` 列表的定义是「与本人有过**任何**动作往来的单」——提交人提交后**立刻**能在已办里看到自己的单（撤销入口天然可发现）。

**前端现状**

- `kb-logic.ts:206-208` 的状态文案只有 `UPLOADED/PENDING_APPROVAL/SCAN_FAILED`——**APPROVED/REJECTED 文档现在会显示原码**（真实缺口，本点修）。
- api-client：`packages/api-client/src/kb.ts` 4 方法（`createKbApi(session)` 形态，`request<T>` + `buildQuery`）；rbac.ts 有 204/409 处理先例；**没有 approvals 模块**。
- 路由/菜单：`router/index.ts:71-82`（`kb/documents`=`school.kb.view`、`kb/upload`=`school.kb.upload`）；`AdminLayout.vue:19-33` MENU 按码渲染；守卫 `meta.permission` → 403 页。
- 页面先例：`PermissionAccountsView.vue` + `account-logic.ts`（cursor 栈翻页、「还有下一页」只认 `next_cursor`、写动作后重读当前页、理由前端先拦 `accountReasonError`、403 交全局 `reportError` 只提示一次 TD-258、未知档位显示原码）；`KbDocumentsView/KbDocumentDrawer`（cursor 栈、抽屉 generation 竞态守卫、404 一次提示）。
- mock 基建：`e2e/helpers/kb-mock.ts`（有状态 fixture：`kbScanState/kbVersion/kbDocument` + captures）、`rbac-mock.ts`（判据顺序逐条注释指向后端行号 TD-253）。
- 计数基线：单测 **198**（api-client 74 + admin 124）、mock E2E **120**、全栈 E2E 22（仅本地）。
- 已知坑：Playwright webServer 服务 `dist/`，改 `.vue` 后必须先 `pnpm build`（TD-254）；mock E2E 与全栈共用 4173/4174 串行。

## 1. 目标

1. **审批中心页**（`/approvals`，`school.approval.todo`）：待办/已办两个 tab（各自 cursor 栈翻页）+ 详情抽屉（审批单字段、步骤与动作时间线、版本快照、驳回理由）；通过/驳回/撤销三个写动作，判据与文案全部来自服务端，前端只做体验层。
2. **KB 文档页审批入口**：抽屉里按状态+权限给「提交审批」「创建构建」按钮（409/403 由后端裁决，toast 采用 detail.code 对应文案）；补 `APPROVED`/`REJECTED` 状态文案。
3. **不解决**：文档维度直显驳回理由（需后端按 version 查审批单的只读端点，D2 落定处置）；构建进度展示（KB-03）。

**验收（本点）**

- 审批人（持两码）登录：菜单出现「审批中心」；待办列表可见 PENDING 单、点开详情、填理由驳回成功后单据消失于待办、出现在已办、理由与动作留痕可见；通过需一次确认。
- 仅持 `school.kb.approval` 无 `school.approval.todo`：直连 `/approvals` 进 403 页（后端为唯一门禁的对照面）。
- 提交人：上传后在自己抽屉看不到提交入口（无 upload 时）或可提交（有 upload）；提交后在审批中心已办里看到自己的单并可撤销，撤销后版本回待审批可再提交。
- 自批：上传+审批双码账号审批自己提交的单 → 403 `SELF_APPROVAL_FORBIDDEN`（toast 服务端文案），单据与版本状态不变。
- 静态门禁：`typecheck`/`lint`/`format:check`/`build`/单测/mock E2E 全绿；`backend/` 零改动（D2 按推荐项时）。

## 2. 候选方案

### D1 审批中心页面形态

- **方案 A（推荐）：独立路由 `/approvals` + 菜单「审批中心」**，挂在 `school.approval.todo`；页内 tab 待办/已办 + 详情抽屉。
  - 优点：与 design §5.10 的规划一致（审批中心是独立治理页面，将来 LEAVE/DEL 类型直接复用同一页面）；权限码唯一（todo），与「KB 文档页」天然分离；光标翻页/抽屉/写动作全部有 PermissionAccountsView 先例。
  - 缺点：多一个页面与一份 mock。
  - 对 spec 的影响：无。
- 方案 B：作为 KB 文档页的一个 tab。
  - 缺点：路由 `meta.permission` 只能挂一个码（KB 页挂 `school.kb.view`，而无 view 的上传教师也需要看自己的提交与撤销）；审批将来不只见 KB。不选。

### D2 KB 文档页入口与「驳回理由文档维度可见性」（本点唯一的跨域张力）

- **方案 A（推荐）：纯前端；入口按钮按「状态 + 权限」显示，重复动作吃后端 409；驳回理由在审批中心内可见，文档抽屉 REJECTED 给静态指引**。
  - 做法：抽屉文档头按 `document.status` + `/auth/me` 权限渲染「提交审批」（`school.kb.upload` + PENDING_APPROVAL）与「创建构建」（`school.kb.build` + APPROVED）；同版本已有待审批单/活跃构建时按钮不隐藏，点了收 409（`APPROVAL_ALREADY_PENDING`/`BUILD_ALREADY_ACTIVE`，toast 用服务端 message）；REJECTED 抽屉显示「已驳回，理由请到审批中心「已办」查看」静态指引（不带链接参数——已办按动作往来过滤，提交人/审批人都能找到该单）。
  - 优点：`backend/` 零改动（与 KB-01B 同口径，PR 小、审查聚焦）；「服务端是唯一门禁，前端只做体验层」是本项目一贯边界；spec KB-02 验收「REJECTED 查看看法显示理由/审批人」在审批单维度完整满足（提交人与审批人都在已办可见）。
  - 缺点/成本：文档抽屉不能直显驳回理由与审批状态——登记新 TD，处置指向下一个后端点（KB-03 的版本详情响应扩展/快照接口同域，顺带补最经济）。
- 方案 B：本点顺带给后端加 `GET /school/kb/document-versions/{id}/approval`（`school.kb.view` 只读，返回最近审批单），抽屉直显审批状态与驳回理由。
  - 优点：spec 验收「文档维度可见」一步到位。
  - 缺点：打破「前端点零后端」拆分（diff 跨两域）；M2-UI3 的先例是「后端前置契约单独成点」（TD-187g-A）；一个只读端点单独成点又太碎。倾向把该端点挂到 KB-03（版本详情/快照同域）。
- **取舍**：推荐 A，TD 登记去向写死「KB-03 版本详情响应扩展时顺带补端点」，避免悬空。

### D3 通过/驳回的交互口径

- **方案 A（推荐）：驳回 = 理由必填前端先拦（trim 非空、≤500）+ 一次提交；通过 = 无理由输入 + 一次确认弹窗（动作不可逆）**。
  - 依据：后端驳回空理由 422（前端先拦省一跳，与 `accountReasonError` 同型）；通过/驳回都终态不可撤（引擎无反向迁移），确认文案写明「通过后该版本即可创建构建」。
- 方案 B：两个动作都套逐字确认。缺点：驳回已有理由输入作为摩擦，再加确认是重复负担。不选。

### D4 mock 层的组织

- **方案 A（推荐）：新增 `e2e/helpers/approval-mock.ts`（有状态：todo/done/detail/approve/reject/cancel + submit/build 两条 KB 路由挂在 kb-mock 的文档上）**，判据顺序逐条注释以 `school_approvals.py`/`school_kb.py` 的**函数名**为锚点（TD-253 口径；函数名比行号抗漂移，2026-11-07 审查 m1 统一口径）；新 spec `admin-approvals.spec.ts` + `admin-kb.spec.ts` 追加入口用例。
  - 优点：审批状态机（PENDING→终态、撤销回 PENDING）在 mock 里真实流转，用例断言「列表/抽屉随动作变化」而不只是请求发出。
  - 缺点：helper 多一份（kb-mock 与 approval-mock 共享文档 fixture，用 `document_version_id` 关联）。
- 方案 B：全部塞进 kb-mock.ts。缺点：kb-mock 已 400+ 行，审批状态机与文档状态机混在一个状态对象里。不选。

## 3. 选择结论

- **D1 A（独立路由）／D2 A（纯前端 + TD 去向写死 KB-03）／D3 A（驳回先拦 + 通过一次确认）／D4 A（独立 approval-mock）**。用户 2026-11-07 问题卡片确认：驳回理由可见性走纯前端+指引（TD-291 挂 KB-03），D1/D3/D4 及其余按推荐执行。

## 4. 实现顺序与 TDD 测试清单

1. **api-client**：`packages/api-client/src/approvals.ts`（7 方法 + 契约类型，对齐后端响应模型字段）+ 单测（请求形态/204/409 透传）。
2. **判据纯函数层**：`apps/admin/src/approval-logic.ts`（状态文案与色调、`canDecide/canCancel/isSelfRequest` 判据、`approvalReasonError`（驳回 trim 非空 ≤500）、409/403 错误码到人话文案映射、`typeLabel` 未知档位显示原码）+ 单测。
3. **审批中心页**：`ApprovalsView.vue`（两 tab + cursor 栈翻页 + 「数据可能不是最新」横幅）+ `ApprovalDetailDrawer.vue`（generation 竞态守卫 + 动作时间线 + 写动作 + reportError 口径）；路由 + 菜单。
4. **KB 抽屉入口**：`KbDocumentDrawer.vue` 按状态+权限渲染提交审批/构建按钮（409 toast）+ `kb-logic.ts` 补 `APPROVED`/`REJECTED` 文案（+单测）。
5. **mock 与用例**：`approval-mock.ts` + `admin-approvals.spec.ts`（先红后绿）+ `admin-kb.spec.ts` 追加。
6. **门禁与审查**：`pnpm typecheck/lint/format:check` → 单测 → `pnpm build` → mock E2E（TD-254：先 build）；独立审查 `git diff --staged`。
7. **文档**：简报状态/§9、design §5.10 落地备注、test-plan BS-10 证据、handoff §1、README、milestone、review-log、tech-debt。

红→绿清单：

- [ ] api-client：7 方法请求形态与路径正确；422/409/403 错误透传带 detail.code
- [ ] approval-logic：状态文案映射齐全（PENDING/APPROVED/REJECTED/CANCELLED + 未知档位原码）；`approvalReasonError`（空/空白/超长）；`canDecide`（有码 + PENDING + 非自批不可前端判——自批只能由后端 403 兜）与 `canCancel`（PENDING + 是提交人）判据
- [ ] 审批中心：待办/已办 tab 数据正确、游标翻页不重不漏、加载中禁用翻页；详情抽屉步骤/动作/快照渲染；404 文档一次提示
- [ ] 驳回：空理由前端拦截；成功后单据从待办消失、已办可见、理由展示
- [ ] 通过：一次确认；成功后单据出待办入已办
- [ ] 撤销：提交人在已办撤销成功 → 单据 CANCELLED 展示；非提交人不显示撤销按钮（判据 `isSelfRequest`）
- [ ] 自批：mock 返回 403 `SELF_APPROVAL_FORBIDDEN` → toast 服务端文案、列表状态不变
- [ ] 无 `school.approval.todo`：菜单无「审批中心」、直连 `/approvals` 进 403 页
- [ ] KB 抽屉：PENDING_APPROVAL + upload → 提交按钮；重复提交 409 toast（`APPROVAL_ALREADY_PENDING` 文案）；APPROVED + build → 构建按钮、重复 409 toast；APPROVED/REJECTED 状态文案正确（不再显示原码）
- [ ] 既有用例不回归（单测 198、mock E2E 120）

## 5. 测试策略

| 层级 | 内容 |
|---|---|
| 单元 | api-client approvals 请求形态；approval-logic 判据/文案/错误映射；kb-logic 两态文案 |
| mock E2E | `admin-approvals.spec.ts` 新增约 12–15 条（列表/抽屉/三动作/自批/403/守卫）；`admin-kb.spec.ts` +3–4（抽屉入口/409/文案） |
| 全栈 E2E | 不做（TD-170 口径；KB-01C 模式的审批全栈链路留待 M3 收尾或 KB-05 前统一补） |
| 后端 | 零改动（D2 A）；静态门禁不涉及 |
| 性能/评估 | 不做 |

## 6. 预判技术债

| TD 编号 | 条目 | 处置 |
|---|---|---|
| TD-291（新） | **文档抽屉不直显审批状态与驳回理由**：需要「按 version 查最近审批单」的只读端点（`school.kb.view`）；纯前端点无法关联（todo/done 分页不可按 version 查询） | 已采纳 → KB-03（版本详情响应扩展/快照接口同域时顺带补端点 + 抽屉直显；届时本点留下的「静态指引」替换为真实数据） |
| TD-292（新） | **审批全栈 E2E 缺口**：真实后端的审批→构建链路无端到端证据（与 KB-01C 同模式缺口） | 已延后 → M3 收尾或 KB-05 前（与 TD-170 全栈本地化口径一致，届时一并补 approval + build 两条链路） |
| TD-254 / TD-170（既有） | dist/ 前检、全栈仅本地 | 维持既有口径，本点不改 |

## 7. 高风险确认

- **是否属于高风险功能**：审批动作本身是高风险（状态机、不可逆决定），但本点**只做前端体验层**——全部判据、状态迁移、权限裁决都在已合并的后端；前端风险集中在「mock 判据抄错导致用例假绿」（TD-253 教训）与「动作后列表状态不同步」。
- **需要用户确认的点**（推荐项在前）：
  1. **D2 驳回理由可见性走纯前端**：文档抽屉 REJECTED 给「到审批中心已办查看」指引，不直显理由；「按 version 查审批单」只读端点挂到 KB-03 顺带补（TD-291）。
  2. **D1 审批中心独立路由 `/approvals`**（挂 `school.approval.todo`），不做进 KB 文档页。
  3. **D3 通过一次确认、驳回理由先拦**（无逐字确认）。
- **确认结论**：用户 2026-11-07 问题卡片确认全部按推荐项执行（① D2 纯前端+指引，TD-291 去向 KB-03；② D1 独立路由 `/approvals`；③ D3 通过一次确认、驳回理由先拦；④ D4 独立 approval-mock）。
