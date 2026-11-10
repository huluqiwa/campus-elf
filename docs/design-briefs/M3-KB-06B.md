# M3-KB-06B — 分块预览前端（api-client + KB 抽屉分块区块 + mock E2E）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-11-10 |
| 状态 | **待确认**（决策卡给出推荐项；用户拍板后编码） |
| 关联功能点 | M3 第 13 点：KB-06B（KB-06 的前端半；纯前端，`backend/` 零改动） |
| 验收引用 | `docs/spec.md` §KB-06（:294-300：列表/详情/统计正确、表格/代码未被拆坏、搜索只返回匹配块、分页正确、跨租 404、渲染净化、大文档分页/虚拟滚动）、`docs/milestone.md` §6.2、简报 `M3-KB-06.md` §4（后端契约） |
| 前置事实 | KB-06A 已合并（PR #55 → `d5a4a83`）：3 只读端点 + 四筛选 + 游标分页 + stats；本文档前端事实已在代码逐条核实（出处随文标注） |
| 不做的事 | `backend/` 生产代码零改动；虚拟滚动与 markdown 净化渲染（TD-307 已登记延后）；pg_trgm（TD-308）；语义检索/检索测试（KB-07）；版本切换 MVP 的 diff（KB-08） |

## 0. 现状（2026-11-10，均已在代码核实）

**后端契约（KB-06A，前端只消费）：**

- 3 端点（`school_kb.py:1064/:1098/:1119`）：列表（limit 1-100 默认 20 + cursor + q≤200/section_path≤500/chunk_type/parent_id）、详情（列表字段 + `document_version_id` + `parent{id,chunk_index}|null` + `children[{id,chunk_index}]`）、stats（total/parent/child/embedding 三态/token/char/section_count/sections[]）。
- 权限 `school.kb.view_chunks`；404 `VERSION_NOT_FOUND`「文档版本不存在」/`CHUNK_NOT_FOUND`「分块不存在」；400 `INVALID_CURSOR`「分页游标无效」。
- 筛选全部服务端执行（游标推进在过滤之后）；「只返回匹配块」由集成/业务测试背书。

**前端现状：**

- api-client `kb.ts`（157 行）：8 方法（upload/list/get/listVersions + publish/rollback/offline/delete），类型 `Kb*View`/`Kb*Result` 前缀、分页复用 `Page<T>`（org.ts）、无统一错误封装（`request<T>` 直接抛 `ApiError`）、契约行号注释锚定；**无任何 chunk 方法/类型**。
- `kb-logic.ts`（475 行）：纯函数层（`documentStatusMeta` 14 档 :208-227、`publishGate/offlineGate/deleteGate` 四则合取门禁 :247-286、`KB_*_CODE` 权限码常量 :236-237、`kbErrorInfo` 错误映射 :430）；测试 33 条。
- `KbDocumentDrawer.vue`（815 行）：**无 el-tabs**，纵向区块 = 基本信息 → 当前版本 → REJECTED 指引 → 审批/构建行 → 发布动作行（:608-661）→ 测试区块（`<section v-if="testGate.visible">` :663-712，按码 `school.kb.test` 门禁）→ 版本列表表（:714-734，**无行交互、无选中态**，写动作只针对 `current_version`）→ 加载更多。
- 交互模式：每动作独立 loading ref、`notifyGone()`（404 关抽屉）、`loadGeneration` 竞态作废、409 → `ElMessage.warning(服务端 message)`、其余 `reportError`、确认 `ElMessageBox.confirm`、写后 `refreshAfterAction()` 重读不手改状态。
- `kb-mock.ts`（713 行）：`state.documents` 有状态夹具（`KbVersionFixture` **无 chunk 字段**），`page.route` 正则分发，判据注释锚定后端行号（TD-253 口径），`json(route,...)` 统一响应，404 体 `{detail:{code,message}}`。
- `admin-kb.spec.ts` **34 条**（KB-01B 15 + TD-282 3 + KB-02B 4 + KB-04B 2 + KB-05B 10）；mock E2E 全量基线 **146**；单测基线 **247**（api-client 91 + admin 156）；全栈 E2E 基线 25（`admin-fullstack-kb-publish.spec.ts` 3 条 serial，READY 断言点 :232-237/:294-308/:422-446，`apiGet/apiPost` helper + `kb_admin/kb_approver/kb_publisher` 账号）。

## 1. 目标与验收

| 来源 | 本点覆盖 |
|---|---|
| spec「列表/详情/统计正确」 | api-client 3 方法 + 抽屉分块区块（列表表 + 详情子视图 + 统计摘要行） |
| spec「表格/代码未被拆坏」 | 纯文本渲染 `text_markdown`（不二次加工、无 v-html）；全栈 E2E 真实构建产物断言表格/代码整块 |
| spec「搜索关键词/章节只返回匹配块、分页正确」 | q 输入框 + 章节下拉（stats.sections）+ 类型下拉；游标「加载更多」只认 `next_cursor`（187g-C 口径） |
| spec「切换到旧版本查看该版本分块」 | 版本表加「分块」入口（每行），分块子视图按所选版本定域（决策卡 1） |
| spec「渲染净化」「大文档分页/虚拟滚动」 | 纯文本渲染无 v-html（不渲染即无 XSS 面）；服务端游标分页（TD-307 登记虚拟滚动延后） |
| spec「跨租 404」 | 404 文案可执行（后端门禁为主；前端 `notifyGone` 口径沿用） |

## 2. 决策卡（推荐项待拍板）

### 卡 1 · 分块区块形态与「版本定域」交互（抽屉现状无版本选中态）

- **A（推荐）：版本表加「分块」操作列**——每行一个「查看分块」按钮；点击进入该版本的**分块子视图**（抽屉内 section 切换，带「← 返回文档」）；默认入口不加（用户从版本行进入，语义天然按版本定域）。子视图头显示版本号/状态 + 统计摘要。
  - 优点：spec「切换旧版本看该版本分块」验收直接可达；不动现有写动作语义；与抽屉纵向区块模式一致。
  - 缺点：多一层视图状态（`chunksView: {versionId} | null`），需纳入 `loadGeneration` 竞态作废。
- B：常驻分块区块固定看 `current_version`。缺点：旧版本分块验收不可达（KB-05 后多版本回滚在产品流可达），不选。
- C：独立路由 `/kb/chunks`。缺点：与抽屉区块先例不符、跳转丢上下文，不选。

### 卡 2 · 列表呈现与详情

- **A（推荐）：扁平表格 + 详情子面板**——列：`#`（chunk_index）、类型（父/子标签）、章节（section_path 截断）、字符数、embedding 状态；行点击 → 详情子面板（`text_markdown` 纯文本 `<pre>` 展示 + 页码/token/哈希略 + 「查看父块」「查看子块(N)」导航链接，走详情端点的 parent/children 引用）；「只看子块」用 parent_id 筛选（详情面板对父块给「查看子块」快捷筛选）。
  - 优点：与后端扁平契约零阻抗；父子导航经详情引用闭环；无虚拟滚动依赖。
  - 缺点：树状分组视图不做（TD-306 已登记）。
- B：el-table-v2 虚拟滚动 + 树形。缺点：与游标分页叠加收益低、TD-307 已延后。不选。

### 卡 3 · 筛选 UI 与查询语义

- **A（推荐）**：q 搜索框（≤200 前端先拦，strip 后空不发）+ 章节下拉（选项 = stats.sections，精确匹配语义与后端一致）+ 类型下拉（全部/父块/子块）；**任一筛选变化即重置游标栈回第一页**（筛选变化后旧游标语义失效）；「加载更多」只认 `next_cursor`。
  - 优点：与后端契约（卡 4A）逐字对齐；「筛选+游标组合不重不漏」已有集成测试背书。
  - 缺点：无（B/C 均为子集）。
- B：只做 q。缺点：章节筛选验收不可达。不选。

### 卡 4 · 权限与统计消费

- **A（推荐）**：分块入口列与子视图整体按 `school.kb.view_chunks` 显示（`KB_CHUNKS_CODE` 常量 + `chunksGate` 纯函数，与 testGate 同模式）；统计在子视图头部以摘要行消费（总块数/父/子/token；sections 仅作章节下拉数据源）；无码时版本表不出现「分块」列。
  - 优点：与 testGate/发布门禁同构；权限矩阵（KB 角色 √ 其余 △）直接映射。
- B：统计单独请求挂 view_chunks、列表另挂 view。缺点：后端就是单码，前端拆两个码是造假门禁（187g-C「mock 比后端严」Blocker 先例）。不选。

### 卡 5 · mock 与全栈 E2E 范围

- **A（推荐）**：`kb-mock.ts` 加 3 条有状态路由（chunks 列表/详情/stats，判据锚定 `chunk_read.py` 行号；state 增 `chunksByVersion` 夹具）；`admin-kb.spec.ts` +6~9 条（列表渲染/四筛选/加载更多/详情父子导航/缺码无入口/404 关抽屉/统计摘要）；全栈 `admin-fullstack-kb-publish.spec.ts` 链③ READY/TEST_PASSED 后追加分块断言（API `apiGet` 列表 + UI 打开分块区块断言表格/代码整块文本）。
- B：只做 mock E2E。缺点：TD-292 全栈口径下「表格/代码未被拆坏」无真实构建产物证据。不选。

### 卡 6 · PR 口径

- **A（推荐）：单 PR**（纯前端 + mock E2E + 全栈追加断言），沿 KB-04B 口径；全栈本地执行不进 CI（TD-170/TD-207）。

## 3. 选择结论

推荐 **卡 1A / 2A / 3A / 4A / 5A / 6A**。无 spec/design 修订需求（TD-306/307 维持已登记口径；本点纯消费 KB-06A 契约）。

## 4. 实现顺序（TDD 红→绿；api-client → 纯函数 → 组件 → mock → E2E）

1. api-client `kb.ts`：`KbChunkView`/`KbChunkDetail`/`KbChunkStats`/`KbChunkRef` 类型 + `listChunks(versionId, query?)/getChunk(chunkId)/getChunkStats(versionId)` 3 方法（契约行号注释锚定 `school_kb.py:1064/:1098/:1119`）；`kb.test.ts` 先红（URL/method/query 序列化/类型映射/400/404 断言）。
2. `kb-logic.ts` 纯函数（`kb-logic.test.ts` 先红）：`KB_CHUNKS_CODE` 常量、`chunksGate`、`chunkTypeLabel/embeddingStatusMeta`、`chunkQueryPlan`（q strip→空不发、筛选变化重置游标判据）、`chunkFilterError`（q>200 前端先拦）、`formatChunkStats` 摘要文案、`sectionOptions`（stats→下拉）。
3. `KbDocumentDrawer.vue`：版本表「分块」列（按码）+ 分块子视图（列表/筛选/加载更多/详情子面板/统计摘要 + `loadGeneration` 纳管 + `notifyGone` 沿用）；**纯文本渲染、无 v-html**（单测钉住：搜模板无 v-html）。
4. `kb-mock.ts`：3 条有状态路由（判据锚定 `chunk_read.py` 行号，TD-253 口径；state 增 chunks 夹具）。
5. `admin-kb.spec.ts` mock E2E 红→绿（+6~9 条）。
6. 全栈追加：链③ READY 后 API 断言 `GET .../chunks`（非空、含表格整块）+ UI 打开分块区块断言文本保真。
7. 门禁：`typecheck/lint/format:check/build` 全绿 + **`pnpm build` 后跑 E2E**（TD-254 前检）；单测/mock E2E 基线更新。

## 5. 测试策略

| 层 | 范围 |
|---|---|
| 单元 | api-client 3 方法（+6~8）；kb-logic 纯函数（+10~14：门禁/文案/查询计划/筛选错误/统计摘要/sections 下拉） |
| mock E2E | `admin-kb.spec.ts` +6~9（基线 146 → 预期 152~155）；mock 判据锚定后端行号 |
| 全栈 E2E | 既有链③追加分块断言（真实构建产物；本地执行不进 CI）；25 → 25（不新增链，只扩断言） |
| 后端 | 零改动（`backend/` 无 diff 是本点门禁之一） |
| 审查对照 | 187g-C「mock 比后端严」Blocker 先例 + TD-253 锚点口径 + KB-01B 四条时序 Major（轮询/分页/抽屉/卸载）+ 卡 1 子视图竞态 |

## 6. 预判技术债

| TD | 条目 | 处置 |
|---|---|---|
| TD-306 | 章节子树（前缀）筛选与块树视图 | 口径不变（本点扁平列表 + parent_id 导航） |
| TD-307 | markdown 净化渲染（DOMPurify）与虚拟滚动 | 口径不变（本点纯文本 + 游标分页；契约双字段已备） |
| 新候选 | 分块子视图的「从详情跳父块」跨页游标定位（父块可能不在当前已加载页） | 实现时若做「跳转即按 id 直查详情」则无此债；登记实现注记即可 |

## 7. 高风险确认

本点纯前端消费只读契约，不触认证/租户隔离/写路径；渲染净化为 spec 明文要求（无 v-html 为本期口径，TD-307 登记升级路径）。决策卡经用户拍板后编码。

## 8. 实现注记（对 §2/§4 决策的如实偏离/细化，独立审查后登记）

1. **「chunkQueryPlan」纯函数未落地**（§4 第 2 步）：实现改为组件内显式 `applyChunkFilters()`（清除列表+游标+详情后重载第一页），筛选签名函数写完测试后删除（死代码纪律）；「筛选变化回第一页」行为由 mock E2E 筛选用例覆盖。
2. **`backend/scripts/e2e_seed.py` 给 e2e-kb-publisher 补 `school.kb.view_chunks`**：全栈链③分块断言需要该码（测试编排改动，KB-05B 种子先例；偏离「纯前端」口径如实记一笔）。
3. **MailHog 端口覆盖机制**（`deploy/docker-compose.test.yml` + `scripts/test-stack.sh`）：本机 Hyper-V 保留段 50949-51048 盖住默认 SMTP 51025（bind forbidden，e2e 栈起不来）；沿用 `TEST_PG_HOST_PORT` 同款机制加 `TEST_SMTP_HOST_PORT`/`TEST_MAILHOG_API_HOST_PORT`（compose 默认值不变、CI 零影响；本机 git 忽略的 `.env.test` 设 51625）。handoff 已知坑 14 的机制面由此从 PG 扩到 MailHog。
4. **卡 2A「行点击 → 详情」实现为显式「详情」按钮**（审查 m-5）：行点击留白给未来行交互；详情入口更醒目。列表另加**「内容」预览列**（单行截断、DOM 含全文）——行辨识可用性 + 全栈 hasText 断言依赖 DOM 全文。
5. **「无 v-html」的钉住方式**：admin 无组件级单测基建（全部 *-logic 纯函数测试），未按 §4 第 3 步补模板单测；由全栈链③ `toContainText("| 参数 | 默认值 |")` **间接钉住**（若被渲染成 HTML，textContent 不含字面竖线会红）；审查 grep 实核 admin 全部模板零 v-html 真实使用。
6. **404 主题取舍**（审查 m-4）：卡 5A 候选主题「404 关抽屉」未交付——mock 无法表达「版本可见但 chunks 404」（chunks 与版本列表共用 findVersion 探测），需加旋钮不值；**stats 404 → notifyGone（关抽屉）与列表 404 → reportError（驻留）的不对称是刻意的**：列表 404 = 版本不可见而文档仍在，错关抽屉反而错；stats 挂了子视图即死路，notifyGone 兜底。
7. **审查 m-1 修正**：`chunkFilterError` 由 UTF-16 `.length` 改 `codePointLength`（后端按码点度量——原实现会假拦 >100 个 emoji 的合法搜索），补非 BMP 断言。
8. **审查 m-2/m-6**：mock 与 api-client 的判据注释行号按实测订正（q=234/section=236/排序=241/游标=242-253/INVALID_CURSOR raise=249/取满才给游标=254-257/sections 排序=389/KbChunkResponse 模型=973-989）；分页夹具 parent_id 指向真实存在的父块。mock sections 排序用 `localeCompare`，与 Python 码点序在非 ASCII 路径可能不同序（当前夹具全 ASCII 等价；如需严格对齐随夹具扩展时处理）。
9. **顺带修复既有生产缺陷：`kb_embed_batch_size` 默认 16 → 8**（用户拍板，run_kb_test 先例）：链③追加表格使子块数达 9，全栈实测抓出 TEI CPU ONNX 后端硬上限 8（服务端 warn 强制 `max_batch_requests=8`、超限 422 `EMBED_HTTP_ERROR`）——**默认 16 时任何 ≥9 子块的文档构建必 FAILED**（真实部署中等以上文档必挂；此前测试文档子块数均 ≤8 从未触发）。修复＝config 默认值对齐最严格后端上限（`backend/app/config.py`，一行），GPU 部署用环境变量调高；e2e 栈镜像随 `--build` 重建。偏离「纯前端」口径的第二个记点。
10. **全栈链② Escape 竞态修复**：Escape 关回滚下拉时冒泡触发 el-drawer 自身 close-on-press-escape，关闭动画中 close-btn 点不到（全量三轮超时、隔离跑绿＝时序竞态；trace 决定性证据 `drawer-head intercepts pointer events`）→ Escape 后按终态分支（已关跳过 close，与「重开抽屉」汇合等价）；链②/链③ setTimeout 300s→600s 容错全量串行负载。终态**重置种子全量 25 passed（2.0m）一次过**。

## 9. 实现结果（2026-11-11）

**交付清单**（12 文件 +1373/-17 + 审查修复增量）：

- 生产代码：`packages/api-client/src/kb.ts`（`KbChunkView/KbChunkRef/KbChunkDetailView/KbChunkSectionStat/KbChunkStatsView/KbChunkListQuery` + `listChunks/getChunk/getChunkStats`）、`src/index.ts`（导出补齐）、`apps/admin/src/kb-logic.ts`（`KB_CHUNKS_CODE/chunksGate/normalizeChunkQ/chunkFilterError/chunkTypeLabel/embeddingStatusMeta/formatChunkStats/sectionOptions`）、`apps/admin/src/components/KbDocumentDrawer.vue`（版本表「分块」列 + 子视图：列表/筛选/加载更多/详情面板父子导航/统计摘要；`chunksGeneration` 竞态作废全覆盖；纯文本渲染）。
- 测试：`kb.test.ts` +6（19）、`kb-logic.test.ts` +9（42）、`kb-mock.ts` 3 条有状态路由 + 夹具工厂 `kbChunk` + `state.chunksByVersion`、`admin-kb.spec.ts` +7（41）、全栈链③追加 API+UI 分块断言。
- 编排：`e2e_seed.py` publisher 补码（偏离 2）、MailHog 端口覆盖（偏离 3）。

**本地门禁读数**（2026-11-11，win32 / Node 24）：

- 前端 `typecheck`/`lint`/`format:check`/`build` 全绿；单测 **262**（api-client 97 = 91+6、admin 165 = 156+9）；mock E2E **153 passed（基线 146 + 7，TD-254 前检后跑）**。
- 后端 `ruff`/`mypy`（scripts 3 文件）0 错误；`bandit` scripts 仅既有 5 项（CI 不对 scripts 跑 bandit，与 KB-05B 口径一致）。
- 全栈 E2E：25 条（重置种子全量；链③新增分块断言：API 列表非空 + 表格同块 + stats 互证 + UI 详情纯文本保真；读数见 PR 描述回填）。

**独立审查**（第 1 轮，独立只读子智能体审 staged diff 12 文件）：**0 Blocker / 0 Major / 6 Minor**，结论「可以提交」；8 项重点清单逐项实读核验通过（api-client 契约逐字段、竞态主链路、渲染净化 grep 实核零 v-html、mock 判据行为全对齐含 TD-231 拒 bool、既有 publish/rollback/offline/DELETE 分发零破坏、种子/端口机制同源）。Minor 处置：m-1 码点度量 **已修**（偏离 7）；m-2 锚点行号 **已订正**（偏离 8）；m-3 详情代际守卫 **已补**；m-4/m-5 **偏离 4/5/6 登记**；m-6 夹具修正 **已改** + localeCompare 备注登记（偏离 8）。无 Blocker/Major，按 §6.2 不触发复审。

**PR 与合并事实（2026-11-11）**：PR #57（head `72b4daf`）→ rebase 合并 `6949ec3`+`780d62e`，分支已删；合并授权由用户在 CI 全绿后确认。PR CI backend run `42877592475` **1047 passed, 1 skipped（24:52）**、四包覆盖率门槛全过（79.92/82.41/81.25/66.67%，与基线逐包一致）；frontend/e2e run `42877592208`（单测 **262**、Playwright **153 passed / 3m26s**）；security run `42877592330` success（42s）。TD-310/311 随本 PR 登记（`780d62e`）。
