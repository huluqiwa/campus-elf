# M3-KB-07B — 检索测试前端（api-client + 分块子视图检索测试区 + mock E2E + 全栈检索断言）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-11-11 |
| 状态 | **待确认**（决策卡给出推荐项；用户拍板后编码） |
| 关联功能点 | M3 第 15 点：KB-07B（KB-07 的前端半；`frontend/packages` 与 `apps/admin` 为主，e2e 编排有少量改动——偏离「纯前端」口径如实留痕） |
| 验收引用 | `docs/spec.md` §KB-06/07/08（:294-300：展示 top-k 分数与最终引用、能定位「为什么召回」、渲染净化）、简报 `M3-KB-07.md` §3（后端契约，已合并 PR #59 → `4cca9bb`）+ 卡 4A（UI 落点） |
| 前置事实 | KB-07A 已合并（端点/错误链/rerank 三态见简报 §3）；本文档前端事实已在代码逐条核实（出处随文标注） |
| 不做的事 | `backend/` 生产代码零改动；版本切换 MVP（KB-08）；ACL 视角参数（后端 422 兜底，前端不提供入口——KB-09 才有语义）；检索结果导出/对比视图（无真实需求不排期）；markdown 净化渲染（TD-307 口径不变） |

## 0. 现状（2026-11-11，均已在代码核实）

**后端契约（KB-07A，前端只消费）：**

- `POST /school/kb/versions/{version_id}/retrieval-test`：入参 `{query(≤300 码点 strip 后非空), top_k(1-20 默认 10), with_rerank(默认 false), as_user_id/as_audiences/ignore_acl(KB-09 预留,设值 422)}`；响应 `{version_id, query, top_k, with_rerank, rerank_status, latency_ms, pool_size, results[]}`，行含 `chunk_id/chunk_index/chunk_type/section_path/page_*/text_preview(≤200)/rrf_score/dense{score,rank}|null/sparse{...}|null/rerank_score|null/final_rank/is_final_reference`。
- 错误：404 `VERSION_NOT_FOUND`（两态同文）/ 409 `RETRIEVAL_TEST_NOT_INDEXED` / 422（`RETRIEVAL_TEST_QUERY_REQUIRED`、`RETRIEVAL_TEST_ACL_UNAVAILABLE`、pydantic 校验）/ 502 `RETRIEVAL_TEST_EMBED_FAILED`/`RETRIEVAL_TEST_SEARCH_FAILED`/`QDRANT_UNAVAILABLE`；rerank 失败**不报错**（`rerank_status=skipped|failed`，降级融合序）。

**前端现状（锚点）：**

- api-client `kb.ts`：**11 方法**（upload/list/get/listVersions + 4 发布动作 + listChunks/getChunk/getChunkStats），`KbChunk*` 类型齐；`kb.test.ts` **19 条**（api-client 全量 97）。
- `kb-logic.ts`（553 行）：分块族纯函数在 ：486-553（`KB_CHUNKS_CODE/chunksGate/normalizeChunkQ/chunkFilterError/chunkTypeLabel/embeddingStatusMeta/formatChunkStats/sectionOptions`）；`kb-logic.test.ts` **42 条**（admin 全量 165）。
- `KbDocumentDrawer.vue`（1272 行）：`chunksView`（:118）→ 子视图模板 `v-if="detail && chunksView"`（:653）：返回行（:656）→ 统计摘要（:670-672）→ 筛选行（:673-720）→ 列表表（:724-772，行「详情」按钮 `openChunkDetailById` :766）→ 加载更多（:773）→ 详情子面板（:792-）。**无任何检索测试 UI**。
- 竞态：`chunksGeneration` 代际作废全覆盖（:367/:464/:472 等）；`notifyGone`（404 关抽屉）与列表 404 → `reportError` 驻留的不对称是 KB-06B 偏离 6 的刻意口径。
- `e2e/helpers/kb-mock.ts`：有状态（TD-253 判据锚点口径），`state.chunksByVersion` + `kbChunk` 夹具工厂 + 分块三路由（KB-06B）。
- mock E2E 基线 **153**（`admin-kb.spec.ts` **41** 条，KB-06B 段 ：998 起）；单测基线 **262**；全栈 E2E 基线 **25**（链③ READY :434、分块断言 :461-478，`apiGet/apiPost` helper + `kb_admin/kb_approver/kb_publisher` 账号）。
- e2e compose：有 `tei-embed`（:92，external 卷 `campus-elf_tei_models` 复用 dev 栈模型，后端 env `TEI_EMBED_URL: http://tei-embed:80` :53）；**无 `tei-rerank`**。dev compose 的 tei-rerank（:202-229）镜像/参数可逐字照抄。

## 1. 目标与验收

| 来源 | 本点覆盖 |
|---|---|
| spec「Given 检索测试 query，When 运行，Then 展示 top-k 分数与最终引用，能定位『为什么召回』」 | 分块子视图内检索测试区：query/top_k/with_rerank 输入 → 结果表逐行展示 dense/sparse（分数+该路排名）、RRF 融合分、rerank 分数（applied 时）、最终引用标记、「查看分块」复用详情导航 |
| spec「渲染净化」 | 分数纯数字、`text_preview` 纯文本，零 v-html（延续 KB-06B 口径） |
| 简报卡 4A「UI 联动：分块子视图内嵌」 | 即本点形态 |
| 全栈背书 | 链③追加检索断言（含一次真实 rerank），rerank 适配器获真实栈证据 |

## 2. 决策卡（推荐项待拍板）

### 卡 1 · 检索测试区形态与位置 → **A（推荐）：常驻紧凑区，插在统计摘要与筛选行之间**
标题行「检索测试」+ 一行输入（query 输入框 + top_k 数字输入 1-20 + with_rerank 开关〔默认关，旁边提示「CPU 重排约 10~30s」〕+ 运行按钮）+ 空态提示一行（「输入检索词后运行，查看逐路分数与最终引用」）；**结果表仅运行后渲染**（静态占用约 2 行高度，不挤压分块列表主功能）。
- B：el-collapse 折叠默认收起。缺点：入口不可见、验收多一步。不选。
- C：独立子视图（从版本行进）。缺点：简报卡 4A 已拍板内嵌，跳转丢上下文。不选。

### 卡 2 · 结果表列呈现 → **A（推荐）：排名行 + 双路「分数 · #名次」+ 最终引用标记 + 查看分块**
列：`#`（final_rank）、最终引用（`is_final_reference` success tag）、块（`#chunk_index` + 类型 tag）、章节、dense（`0.9786 · #1`，未召回「—」）、sparse（同形）、RRF（4 位小数）、rerank（`rerank_status=applied` 时有值否则「—」）、查看分块（`openChunkDetailById(chunk_id)` 复用既有详情导航——「最终引用定位到分块」闭环）。`formatScore`（4 位小数/None→「—」）与 `routeScoreText`（分数+名次拼装）进 kb-logic 纯函数。
- B：只显示分数不带名次。缺点：「为什么召回」可解释性弱。不选。

### 卡 3 · 状态管理与竞态 → **A（推荐）：结果作废并入 chunksGeneration 代际；loading 绑独立运行序号**
新 refs `retrievalTestLoading/retrievalTestError/retrievalTestResult`；`runRetrievalTest()` 双轨守卫——**结果作废**跟 `chunksGeneration`（切版本/关子视图/筛选变更都作废在途响应），**loading 存活期**跟独立运行序号（筛选变更 bump 代际但不重置 loading，绑代际会让「运行」按钮永久转圈——审查 M1，§8.8）；`openChunks` 重置全部输入/结果/错误（closeChunks 仅 bump 代际 + 置 null，行为等价：关视图不渲染、重开必经 openChunks）。错误：404/409/422/502 一律 `reportError`（服务端 message）**驻留**——检索测试 404 = 版本不可见但文档仍在，与分块列表 404 驻留口径一致（KB-06B 偏离 6 的不对称逻辑同源）；422 的 query 校验错误前端先拦（`retrievalQueryError`：strip 后空 + `codePointLength`>300，m-1 教训用码点度量）。
- B：独立 generation。缺点：与 chunks 竞态主链割裂，两套代际互踩。不选。

### 卡 4 · mock 伪分数策略 → **A（推荐）：确定性词命中**
query 对 `state.chunksByVersion` 夹具文本做包含判定：命中者排前（dense/sparse 同序、分数按位置递减），RRF 按后端公式 `1/(60+rank_d)+1/(60+rank_s)`（k=60 与 Qdrant 一致）计算，rerank fake 按位置反转；`with_rerank=true` 时池扩到 20（无 20 条夹具时=夹具数）。判据注释锚定 `retrieval_test.py` 行号（TD-253 口径）。mock 不实现 409/502（mock 无版本状态机分辨率，404 由既有 `findVersion` 探测路径覆盖）。
- B：随机分数。缺点：E2E 断言不稳定。不选。

### 卡 5 · 全栈断言与 tei-rerank 编排 → **A（推荐）：e2e compose 加 tei-rerank + 链③两组真实检索断言**
compose 服务与 dev compose（:202-229）逐字一致（含 `--max-client-batch-size 8`/`--auto-truncate`），external 卷复用；后端 env `TEI_RERANK_URL: http://tei-rerank:80`（:53 旁）；等待上限沿用 tei-embed 模式。链③ READY+分块断言后追加：① `with_rerank=false`（query=表格块全文 → 断言 top-1=表格块、dense/sparse 双路、RRF>0、is_final_reference）；② `with_rerank=true` 一次真实调用（`rerank_status=applied`、`rerank_score` 非空、final_rank 连续）——**rerank 适配器获得真实栈背书**。用例数 25 → 25（链③内扩断言，不新增链）。
- B：不加 tei-rerank，全栈只验 disabled。缺点：真实 rerank 无端到端证据（KB-07A 汇报差距清单第 3 条）。不选。
- 风险显式化：`BAAI/bge-reranker-v2-m3`（~2GB）依赖 `campus-elf_tei_models` 卷里已有缓存（M0 CPU 重排基准下载过）；若本机卷无此模型，e2e 栈首启会拉取（`HF_HUB_DOWNLOAD_TIMEOUT=600` + start_period 600s 容错，`scripts/e2e-stack.sh` 预检提示与 tei-embed 同款）。

### 卡 6 · PR 口径 → **A（推荐）：单 PR**（api-client + kb-logic + drawer + mock + mock E2E + 全栈编排与断言）；偏离「纯前端」口径处（e2e compose + 后端 env 各一行）如实留痕（KB-06B 两处偏离先例）。全栈本地执行不进 CI（TD-170/TD-207）。

## 3. 选择结论

推荐 **卡 1A/2A/3A/4A/5A/6A**。无 spec/design 修订需求（纯消费 KB-07A 契约；TD-307 渲染口径、TD-314 耗时提示即缓解措施均维持登记口径）。

## 4. 实现顺序（TDD 红→绿；api-client → 纯函数 → 组件 → mock → E2E → 全栈）

1. api-client `kb.ts`：`KbRetrievalTestRouteHit/KbRetrievalTestResultRow/KbRetrievalTestResult/KbRetrievalTestInput` 类型 + `retrievalTest(versionId, input)`（契约行号注释锚定 `school_kb.py` 路由与 `retrieval_test.py`）；`kb.test.ts` 先红（URL/method/body 序列化/错误透传）。
2. `kb-logic.ts` 纯函数（`kb-logic.test.ts` 先红）：`rerankStatusLabel`（4 档文案）、`formatScore`、`routeScoreText`、`retrievalQueryError`（strip 空 + 码点 ≤300）、`retrievalTopKError`（1-20）、`retrievalRowsPlan`（结果表行装配：引用标记/类型标签/查看分块目标）。
3. `KbDocumentDrawer.vue`：检索测试区（卡 1/2 形态）+ `runRetrievalTest`（卡 3 竞态/错误口径）；纯文本渲染无 v-html。
4. `e2e/helpers/kb-mock.ts`：+1 有状态路由（卡 4 判据）。
5. `admin-kb.spec.ts` mock E2E 红→绿（+6~8：区渲染/默认快查结果表/双路分数与名次/最终引用标记/查看分块导航/rerank 开关状态文案/空 query 前端拦/404 驻留）。
6. 全栈：e2e compose 加 tei-rerank + env；链③追加两组断言（卡 5）。
7. 门禁：`typecheck/lint/format:check/build` 全绿 + **`pnpm build` 后跑 E2E**（TD-254 前检）；单测/mock E2E 基线更新。

## 5. 测试策略

| 层 | 范围 |
|---|---|
| 单元 | api-client `retrievalTest`（+4~5）；kb-logic 纯函数（+7~9：文案/格式化/错误/行装配，含非 BMP 码点断言） |
| mock E2E | `admin-kb.spec.ts` +6~8（基线 41 → 47~49，全量 153 → 159~161）；mock 判据锚定后端行号 |
| 全栈 E2E | 链③追加两组检索断言（真实栈含真实 rerank；25 → 25；本地执行不进 CI，TD-170/TD-207 口径） |
| 后端 | 零改动（`backend/` 无 diff 是本点门禁之一；e2e compose 的 env 一行除外） |
| 审查对照 | TD-253 锚点口径 + 187g-C「mock 比后端严」先例 + KB-06B 四条时序 Major（轮询/分页/抽屉/卸载）+ 卡 3 竞态 + m-1 码点度量 |

## 6. 预判技术债

| TD | 条目 | 处置 |
|---|---|---|
| 无新增 | TD-314（CPU 重排延迟）由卡 1 的 opt-in + 耗时提示承载前端半；TD-307（markdown 渲染）口径不变 | — |

## 7. 高风险确认

本点纯前端消费只读契约，不触认证/租户隔离/写路径；渲染净化为 spec 明文（无 v-html 延续）；权限入口由既有 `chunksGate` 承担（检索测试在分块子视图内，不另设门禁——后端就是同码 `view_chunks`，前端拆两个码=假门禁，187g-C 先例）。决策卡经用户拍板后编码。

## 8. 实现注记（对 §2/§4 决策的如实偏离/细化，独立审查后登记）

1. **顺带修复 KB-07A 生产缺陷：TEIReranker 客户端分批（用户 2026-11-12 拍板）**——全栈首跑暴露 TEI `--max-client-batch-size 8`（dev/e2e compose 同值）：适配器单请求发整个池（14 条 > 8）被 TEI 拒绝 → `RerankError` → 降级 "failed"，**任何 pool>8 的真实 rerank 必失败**（集成层 FakeReranker 无此暴露）。修复 = `rerank.py` 按 `batch_size` 逐批 `/rerank` 后按全局下标拼回（与 TEIEmbedder 批次模式同款）+ config `tei_rerank_batch_size` 默认 8 + 单测（分批拼回/余数批/任一批失败整体失败）。偏离「backend 零改动」口径如实记。
2. **顺带修复：编排层 rerank 降级从静默改为 warning 留痕**（只记异常类型名，零 query/文本 PII）——本次排障教训：静默降级让 TEI 批量上限问题完全不可诊断。
3. **config `tei_rerank_timeout_seconds` 默认 60→300**：KB-07B 全栈实测 CPU 重排「单批 8 条 × 真实文档块文本」≈74s、4 条 ≈35s（M0 基准 11.7s 是短文本口径），60s 会让 pool>6 的诊断恒超时降级；分批后超时按批计。UI 耗时提示同步改「CPU 重排较慢（可达 1~2 分钟）」（TD-314 口径更新）。
4. **e2e compose 加 `KB_RETRIEVAL_TEST_RERANK_POOL: "4"`**：CPU 重排池配 4（≈35s）——真实栈背书够用，不把套件总时长拖到分钟级。
5. **链③ top-1 断言修正**：只索子块（KB-03B Minor 3），`chunkItems.find` 找到的表格块是父块（不在索引）——断言改为「top-1 ∈ 表格内容块集合（被索引的父子任一）」。首跑 24/25 红于此（红对了：真实栈证据的价值兑现）。
6. **链② 一次 600s 超时红为环境负载**（同机并行集成/TEI 探针 + 镜像重建后的重负载轮，总时长 11.5m vs 基线 4m）；干净环境重跑全绿（链② 51s、链③ 1.9m）——KB-06B「先分清环境负载/口令污染/真缺陷」口径的再次应用。
7. **mock 细化**：`latency_ms` 常量 1.2（mock 无真实计时）；「最终引用」tag 计数断言 scope 到 tbody（表头列名同文，getByText 会把表头算进去——首跑红教训）；**mock 422 分支精简**（审查 m1）：纯空 query 是 pydantic 层（数组 shape，mock 不复刻）、全空白 strip 后空才是路由层 `{code,message}`——mock 只保留后者，top_k 校验分支删除（同属 pydantic 层且 UI 不可达）。
8. **审查 M1（时序竞态，已修）**：检索在途时改分块筛选 → chunksGeneration bump → 迟响应正确丢弃，但 `retrievalTestLoading` 若也绑代际会**永久卡死「运行」按钮**（Element Plus loading 吞 click，审查员实读 node_modules 源码确认）——loading 存活期改绑独立「运行序号」（`retrievalRunSeq`），结果作废仍跟 chunksGeneration；补 mock E2E 回归（retrievalDelayMs=400 在途 → 改筛选 → 断言按钮恢复可点 + 迟响应未渲染）。连带发现并修复：耗时提示文案加长后 `.retrieval-inputs` flex 溢出挤压 el-input-number 失去可操作性（fill 30s 超时，两次复现）——`flex-wrap: wrap` 结构性修复 + 文案缩短为「重排较慢（分钟级）」。
9. **审查 M2 处置**：卡 2A 的「块列类型 tag」**已补**（chunkTypeLabel 复用）；卡 3A 措辞修正为「openChunks 重置六个 ref；closeChunks 仅 bump 代际 + 置 null（行为等价：关视图不渲染、重开必经 openChunks）」；§4 计划的 `retrievalRowsPlan` 纯函数**未实现（有意）**——行装配在模板内联（KB-06B 偏离 1 同款死代码纪律：签名函数写完测试后删除，装配逻辑无分支可测），登记于此。
10. **e2e rerank 调用 top_k 5→4**：与 `KB_RETRIEVAL_TEST_RERANK_POOL=4` 对齐（pool=max(4,4)=4），env 真实生效（审查 m2）。

## 9. 实现结果（2026-11-12）

**交付清单**（16 files +1092/-32，`git diff --shortstat main...HEAD` 直读；含审查修复增量）：`packages/api-client`（kb.ts +5 类型/1 方法、index.ts 导出、kb.test.ts +5）、`apps/admin`（kb-logic +5 纯函数、kb-logic.test +5、KbDocumentDrawer 检索测试区）、`e2e`（kb-mock 检索路由 + admin-kb.spec +7）、`e2e-fullstack`（链③两组断言）、编排（compose tei-rerank + 2 env、e2e-stack.sh 参数化等待）、backend 顺带修（rerank.py 分批、retrieval_test.py 留痕、config 2 字段、test_kb_rerank_unit +2）、本简报。

**本地门禁读数**（2026-11-12，win32 / Node 24 / CPython 3.13）：前端 `typecheck`/`lint`/`format:check`/`build` 全绿（build 后跑 E2E，TD-254）；单测 **272**（api-client 102 + admin 170）；mock E2E **160 passed**（基线 153 + 7）；全栈 E2E **25 passed**——最终全绿轮 **2.8m**（重置种子，链③含真实 rerank 断言 applied；此前一轮 4.0m 为 top_k=5 口径）；backend 受影响回归：rerank/检索单元 **20 passed** + 集成 16 + 业务 2（**18 passed / 188s**）、ruff/mypy（264 文件）全绿、bandit×2 由 CI 复跑。

**独立审查**（第 1 轮，独立只读子智能体审 commit `43d55d3` diff）：**0 Blocker / 2 Major / 4 Minor**，11 项重点清单逐项实读核验（含按 bigram 手推三条用例全部期望值、Element Plus 2.14.7 源码实读确认 loading 吞 click）。处置：M1 loading 卡死 → **已修**（运行序号）+ flex 溢出连带修复 + 回归用例；M2 未登记偏离 → 类型 tag **已补** + retrievalRowsPlan 内联化**登记**（偏离 9）；m1 mock 422 精简 **已修**（偏离 7）；m2 e2e top_k 对齐 **已修**（偏离 10）；m3/m4 简报精度 **已修**（本段 + 卡 3A）。

**独立复审**（第 2 轮，同一子智能体审修复增量）：**0 Blocker / 0 Major / 2 Minor**——M1 双轨方案经四类场景 trace 无新竞态、回归用例真钉修复；m-a 结果渲染补运行序号门 → **已修**（一行加固，消除「Enter 并发旧响应覆盖新结果」整类）；m-b §9 数字两次写错 → **已修**（改贴 `git diff --shortstat` 直读）。无 Blocker/Major，可提交。

**PR 与合并事实（2026-11-11，UTC 2026-11-11T18:29:16Z）**：PR #61（head 分支 `feature/m3-kb07b-retrieval-frontend`，4 commits `43d55d3` 功能 + `405d5ce` 审查处置 + `589c755` 简报措辞 + `27d8708` 复审处置）→ rebase 合并为 **`b2776c9`+`405d5ce`+`589c755`+`27d8708`**（对齐 main tip `311d460`），分支已删；合并授权由用户在 CI 全绿后确认。PR CI：backend run `43073998202` **success（job 23m30s，1085 passed + 1 skipped）**、四包覆盖率 211/264=79.92 / 417/506=82.41 / 13/16=81.25 / 4/6=66.67% 门槛全过（与基线逐包一致）；frontend run `43073998120`（单测 170 + Playwright **160 passed / 2.7m**，frontend 与 e2e 两 job 同 run）；security run `43073998126` success（39s）。合并后 main（tip `27d8708`）推送 CI：backend run `43078231330` **success（pytest 20:36，1085 passed + 1 skipped，四包覆盖率与 PR run 逐包一致）**、frontend run `43078231396`（单测 170 + Playwright 160 / 2.6m）success、security run `43078231392` success。
