# M3-KB-05B — 发布/回滚/下线/删除前端按钮 + TD-292 UI 全栈 E2E 实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-11-10 |
| 状态 | **已确认**（2026-11-10 问题卡片：D1-A 补齐全 14 档 / D3-A 三账号职责分离 / D4-A e2e compose 加 tei-embed / D5-A 一次确认+影响明示，全按推荐拍板；D2-A 会话内记忆+409 兜底、D6-A 单 PR 未提异议按推荐执行） |
| 关联功能点 | M3 第 11 点：KB-05B（KB-05 的前端半 + TD-292 收口） |
| 验收引用 | `docs/spec.md` §KB-05（:284-292）、`docs/milestone.md` §6.2、`docs/state-machines.md` §3/§5、`docs/design.md` §7.4（alias 文档级命名，KB-05 已回写）、`docs/tech-debt.md` TD-292 |
| 前置事实 | KB-05 后端已合并（PR #51 → `fbd99ac`，迁移 head `c9e1f3a5d7b9`）；本文档所有契约点已在代码逐条核实（出处随文标注） |
| 不做的事 | `backend/` 生产代码零改动（测试种子/编排除外，见 D3/D4 口径）；构建进度条 UI（TD-296 已延后，本点 E2E 用 API 轮询快照替代）；KB-08 版本间 diff；k6 压测（M6） |

## 0. 现状（2026-11-10，均已在代码/配置核实）

**后端契约（已合并，前端只消费）：**

- 4 端点（`backend/app/api/school_kb.py`）：
  - `POST /school/kb/versions/{version_id}/publish`（:854，`PublisherDep`）
  - `POST /school/kb/versions/{version_id}/rollback`（:879，path id = **回滚目标**，D2-A）
  - `POST /school/kb/versions/{version_id}/offline`（:903，`PublisherDep`）
  - `DELETE /school/kb/versions/{version_id}`（:928，`DeleterDep`）
- 权限码：发布/回滚/下线 `school.kb.publish`；删除 `school.kb.delete`；查询 `school.kb.view`（构建快照 `GET /build-jobs/{id}` :460 也挂 `school.kb.view`）。
- 响应形状：publish/rollback → `{document_id, version_id, version_no, status, alias, collection, previous_version_id, published_at, replayed, alias_synced}`（:815-825）；offline/delete → `{document_id, version_id, status, was_active}`（:828-835）。
- 409 错误码全集（service.py/status.py）：`KB_NOT_TEST_PASSED`（:2476/2500）、`KB_INDEX_NOT_READY`/`KB_INDEX_INCOMPLETE`（:2363/2366/2479）、`KB_PUBLISHED_NOT_ACTIVE` 两分支（:2508 已撤指针 / :2514 非活跃，消息分别指向重放不可达与「请使用回滚端点」）、`KB_NOT_PUBLISHED`（:2648/2678/2813/2890）、`KB_NO_ACTIVE_PUBLISH`（:2683）、`KB_ROLLBACK_TARGET_IS_ACTIVE`（:2686）。服务端 message 均为可执行中文。
- **前端无法感知「当前活跃版本」**：`active_publish`（service.py:2272）是内部解析函数，未暴露任何端点；动作响应只带回 `was_active`/`previous_version_id`。打开抽屉时（本会话未做过动作）前端不知道哪个 PUBLISHED 版本活跃。

**前端现状：**

- api-client `kb.ts` 仅 4 方法（upload/listDocuments/getDocument/listVersions）；`KbVersionView.status` 字段已存在（:19）。
- `kb-logic.ts` `DOCUMENT_STATUS_META`（:204-212）五档：UPLOADED/PENDING_APPROVAL/SCAN_FAILED/APPROVED/REJECTED。**权威状态全集 14 档**（`backend/app/kb/status.py:16-29`）：上述 5 档 + BUILDING/READY/FAILED/TESTING/TEST_PASSED/TEST_FAILED/PUBLISHED/OFFLINE/DELETED。
- `test-logic.ts:13-15` 另有一份 TESTING/TEST_PASSED/TEST_FAILED 文案（测试区块用）；`testable` 门禁含 READY（:47）。两处 map 并存。
- 抽屉 `KbDocumentDrawer.vue`：动作区现只有 提交审批/创建构建（PENDING_APPROVAL/APPROVED 态）；**版本表（:494-504）没有状态列**（版本号/文件名/大小/扫描/创建时间）。
- mock：`kb-mock.ts` 有状态（documents 数组），已覆盖 documents/versions 路由；`admin-kb.spec.ts` 现有 27 条；mock E2E 全量基线 139。
- 全栈 E2E：`deploy/docker-compose.e2e.yml` 已有 backend + arq worker（**worker 已注册 `build_kb_version`**，`app/workers/settings.py:88`）；依赖栈含 Qdrant；**无 MinerU、无 TEI**（`tei_embed_url` 默认 `http://127.0.0.1:8081`，容器内不可达）。MD/TXT 解析走 MarkdownNormalizer **不经过 MinerU**（`parser/dispatch.py:4,44`），故 TD-292 用 `.md` 文件时缺的外部引擎只有 TEI。
- 种子：`e2e_seed.py` 已有 `e2e-kb-admin`（upload+view）与 `e2e-kb-uploader`（仅 upload），自定义权限账号机制成熟（KB-01C 先例）。

## 1. 目标与验收

| 来源 | 本点覆盖 |
|---|---|
| spec §KB-05「发布前必须 TEST_PASSED；alias 原子切换」 | UI：发布按钮只在版本 `TEST_PASSED` 给；409 `KB_NOT_TEST_PASSED` 文案可执行（后端门禁为主，前端门禁只是 UX） |
| spec §KB-05「回滚只切 alias」 | UI：回滚目标从 PUBLISHED 版本中选择 + 一次确认；`KB_ROLLBACK_TARGET_IS_ACTIVE`/`KB_NO_ACTIVE_PUBLISH` 文案可执行 |
| spec §KB-05「下线/删除不影响在线会话」 | UI：下线/删除只对 PUBLISHED；确认弹窗明示影响（活跃发布撤除、状态终态不可逆）；`was_active` 响应用于结果提示 |
| TD-292（台账 :320） | 全栈 E2E：真实后端 + 真实 PG/Qdrant/TEI，UI 驱动 上传→提交审批→通过→构建→测试→TEST_PASSED→发布→回滚→下线→删除 全链 + 构建 409 门禁 + 职责分离边界；**本点收口 TD-292** |
| milestone §6.2 KB-05 行 | 前端按钮 + mock E2E 与既有 KB 前端节奏一致 |

## 2. 候选方案（逐决策卡 ≥2 个）

### D1 版本状态文案与收敛（kb-logic / test-logic 两处 map 并存）

- **A（推荐）：kb-logic `DOCUMENT_STATUS_META` 补齐全 14 档**（BUILDING/READY/FAILED/TESTING/TEST_PASSED/TEST_FAILED/PUBLISHED/OFFLINE/DELETED），作为文档与版本状态文本的唯一来源；test-logic 的 TESTING/TEST_* 三条改为引用 kb-logic（标签保持现文案）。版本表新增「状态」列消费同一 map。
  - 优点：单一来源，消灭两处漂移；版本表列直接可用；未知档位兜底「原码显示」保持。
  - 缺点：kb-logic/test-logic 各改一点（纯前端，无风险）；test-logic 单测可能有断言需同步。
- B：只补 PUBLISHED/OFFLINE/DELETED 三档（handoff §1 原口径），版本表状态列对 TESTING/TEST_* 继续走 test-logic 或未知兜底。
  - 缺点：版本表加状态列后 BUILDING/READY/FAILED/TESTING/TEST_* 六档必然出现在表里（构建中/可测试/测试通过都是版本生命周期真态），「三档」口径不够；两处 map 继续并存。不选。

### D2 回滚目标的「当前活跃」标识（前端无 active 端点）

- **A（推荐）：会话内记忆 + 409 兜底，后端零改动**。抽屉在本会话内每收到一次 publish/rollback 成功响应即记录 `activeVersionId = response.version_id`；回滚目标下拉对已知活跃项打「当前活跃」标签并禁选；刷新/首次打开时未知活跃 → 列出全部 PUBLISHED 版本可选，选到活跃版本吃后端 409 `KB_ROLLBACK_TARGET_IS_ACTIVE`（服务端 message 已可执行，toast 引导改选）。
  - 优点：不越「backend/ 零改动」红线；错误路径已有真实门禁；实现小。
  - 缺点：多标签页/刷新后活跃标识缺失，用户可能先点一次 409 才知道。可接受（409 文案明确）。
- B：后端在文档详情回包加 `active_version_id`。缺点：违反本点「backend/ 生产代码零改动」口径，属契约变更需另立后端点。不选（登记 TD，M4/KB-08 如有真实需求再做）。

### D3 TD-292 全栈 E2E 的账号拆分（e2e_seed.py 改动）

- **A（推荐）：三账号职责分离**。既有 `e2e-kb-admin` 保持「上传+查看」（提交审批用 `school.kb.upload`）；新增 `e2e-kb-approver`（`school.approval.todo` + `school.kb.approval` + `school.kb.view`）；新增 `e2e-kb-publisher`（`school.kb.view` + `school.kb.build` + `school.kb.test` + `school.kb.publish` + `school.kb.delete`）。
  - 优点：与产品原则「审批职责分离」一致；能断言真实边界——uploader 打不开审批中心（菜单/路由守卫）、approver 看不到发布按钮、publisher 403 审批接口（UI 上无入口即断言菜单隐藏）。
  - 缺点：种子多两个账号；E2E 登录切换多两步（既有 admin E2E 已有身份切换先例，成本低）。
- B：单一 operator 账号持全链码。缺点：一个账号什么都能干，职责分离断言做不了；与真实使用形态不符。不选。

### D4 TD-292 全栈 E2E 的 TEI 来源（e2e compose 改动）

- **A（推荐）：`deploy/docker-compose.e2e.yml` 新增 `tei-embed` 服务**（镜像与 dev compose 同源），backend/worker 环境加 `TEI_EMBED_URL: http://tei-embed:80`。构建（embedding）与测试运行（问题向量化）都需要 TEI；Qdrant 已在依赖栈。
  - 优点：e2e 栈自洽，不依赖 dev 栈是否在跑；与「worker 服务进 e2e compose」先例同性质（编排改动）。
  - 缺点：e2e 栈多一个容器（TEI CPU ~1-2GB 内存，按需 up/down 可接受）。
- B：`TEI_EMBED_URL: http://host.docker.internal:8081` 借用 dev 栈 TEI。缺点：依赖 dev 栈恰好起着且端口未变，跨栈耦合脆弱，E2E 稳定性差。不选。

### D5 删除/下线确认强度

- **A（推荐）：一次确认 + 影响明示**。删除/下线用 `ElMessageBox.confirm` 一次确认，文案明示：该版本状态为终态不可逆（状态机无出边，重发走新版本）、若 `was_active` 活跃发布将撤除且新会话检索不到该文档；**不做逐字确认码**（后端 DELETE 无逐字/理由契约——前端加严会造出「mock 比后端严」的假门禁，187g-C 审查 Blocker 先例）。
- B：逐字输入版本号确认。缺点：后端无对应校验，纯前端仪式感，mock 与真实后端契约漂移风险。不选。

### D6 PR 拆分口径

- **A（推荐）：一个 PR**。前端按钮 + mock E2E + TD-292 全栈 E2E 同点交付（TD-292 本来就绑定本点收口；种子/compose 改动小且都是测试编排）。全栈 E2E 本地执行不进 CI（TD-170/TD-207 口径不变），CI 侧只跑受影响 frontend 套件 + security。
- B：拆两个 PR（按钮+mock / TD-292）。缺点：TD-292 的 UI 驱动链路依赖按钮先合并，拆开反而制造中间态。不选；若本机 TEI/资源不济导致全栈 E2E 拖延，届时再报告拆分。

## 3. 选择结论

推荐 **D1-A / D2-A / D3-A / D4-A / D5-A / D6-A**。无 spec/design 修订需求（design §7.4 已由 KB-05 回写为文档级 alias；本点纯消费）。TD-292 随本点收口（台账处置改「已完成」）。

## 4. 实现顺序（TDD 红→绿；数据→接口→界面 ≈ api-client → 纯函数 → 组件 → E2E）

1. **api-client** `kb.ts`：`KbPublishResult`/`KbVersionStateResult` 类型 + 4 方法；先写 `kb.test.ts` 失败用例（URL/method/无请求体/响应类型映射）。
2. **kb-logic 纯函数**（单测先行，`kb-logic.test.ts` 红）：
   - 状态文案补 9 档（D1-A）；
   - 动作门禁纯函数：`publishGate`（TEST_PASSED + `school.kb.publish`）、`offlineGate`/`deleteGate`（PUBLISHED + 各自码）、`rollbackTargets`（versions.status === PUBLISHED 过滤 + 已知活跃排除）；
   - 409/404 错误码 → 提示映射（服务端 message 优先；`KB_PUBLISHED_NOT_ACTIVE` 特判引导回滚；`KB_NOT_TEST_PASSED` 引导先跑测试沙箱）。
3. **KbDocumentDrawer.vue**：动作区加 发布/下线/删除按钮 + 回滚目标选择（下拉 + 确认）；版本表加「状态」列；动作成功后**重读详情与版本列表**（不手改本地状态，187g-C 口径）；`was_active`/`replayed` 结果提示。
4. **kb-mock.ts**：4 条有状态路由（publish 门禁 TEST_PASSED、replay 语义、rollback 三类 409、offline/delete 状态机 + alias 行内存状态），判据注释**锚定 service.py 行号**（TD-253 口径）。
5. **admin-kb.spec.ts**：新增 mock E2E（红→绿）：发布成功链 / 门禁 409 各码 / 回滚成功与 `KB_ROLLBACK_TARGET_IS_ACTIVE` / 下线 / 删除 / 缺码隐藏按钮 / 版本表状态列渲染 / `KB_PUBLISHED_NOT_ACTIVE` 引导回滚。
6. **TD-292 全栈 E2E**：`e2e_seed.py` 加 `e2e-kb-approver`/`e2e-kb-publisher`（D3-A）；`deploy/docker-compose.e2e.yml` 加 `tei-embed` + `TEI_EMBED_URL`（D4-A）；新增 `frontend/e2e-fullstack/admin-fullstack-kb-publish.spec.ts`——UI 驱动：admin 上传 `.md` → approver 审批中心通过 → publisher 创建构建 → API 轮询 `GET /build-jobs/{id}` 至 READY（快照挂 `school.kb.view`）→ 上传测试集 + 发起运行 → TEST_PASSED → 发布 → 断言列表/抽屉 PUBLISHED 文案与发布结果（`replayed`/`alias_synced` 字段真实回包）→ 回滚到旧版本 → 下线 → 删除（`was_active` 断言）→ 职责分离断言（uploader 无审批中心入口、approver 无发布按钮）。构建 409 门禁用未审批版本断言（KB-02 TD-292 原断言）。
7. **文档回写**：本简报实现结果、review-log、tech-debt（TD-292 → 已完成；D2-B 登记 TD）、handoff §1、README「当前状态」。

## 5. 测试策略

| 层 | 范围 |
|---|---|
| 单元 | api-client `kb.test.ts`（4 方法 + 类型）；`kb-logic.test.ts`（14 档文案、四动作门禁、回滚目标过滤、错误码映射、活跃记忆） |
| mock E2E | `admin-kb.spec.ts` +N 条；全量基线 139 → 预期 +6~9 |
| 全栈 E2E | TD-292：真实后端+PG+Qdrant+TEI 全链（§4.6 断言面）；既有 22 条回归复跑 |
| 后端门禁 | 生产代码零改动；`e2e_seed.py` 改动跑 ruff/mypy（scripts）+ bandit |
| 前端本地门禁 | `typecheck`/`lint`/`format:check`/`build` 全绿；改 `.vue` 后先 `pnpm build` 再跑 E2E（TD-254 前检） |
| 审查对照 | KB-05 后端三轮 findings 类型（恢复路径实测、409 提示可执行、404 不泄露）+ 187g-C「mock 比后端严」Blocker 先例 + TD-253 锚点口径 |

## 6. 预判技术债

| TD | 条目 | 处置 |
|---|---|---|
| TD-292 | 本点收口 | 完成后台账改「已完成」（附 commit/读数） |
| 新 TD | 前端无法感知当前活跃版本（无 active 端点；D2-A 会话内记忆是补偿，刷新/多标签后未知） | 已延后 → M4/KB-08 有真实需求时后端详情回包加 `active_version_id`（届时属后端点） |
| 新 TD | 构建等待仅靠 API 轮询快照（TD-296 进度条仍未做，本点不复用轮询实现到 UI） | 重申 TD-296 延后口径不变 |
| TD-296 | （既有）构建进度条 | 口径不变 |

## 7. 高风险确认（已完成，2026-11-10）

发布/回滚/下线/删除按钮属「发布/删除」高风险类。用户在问题卡片上拍板：**D1-A / D3-A / D4-A / D5-A 全按推荐**；D2-A（会话内记忆+409 兜底）与 D6-A（单 PR 含 TD-292）按推荐执行、未提异议。据此编码。

**追加确认（2026-11-10，实现中）**：TD-292 全栈 E2E 抓到后端真缺陷——KB-04A 的测试运行任务 `run_kb_test` 从未注册进 arq worker（worker 报 function not found，运行永远停在 TESTING；集成测试直调函数未暴露，真实部署必挂），不修则 TEST_PASSED 不可达、发布链路收不了口。用户拍板**本 PR 顺带修**（`app/workers/settings.py` 注册 + 显式 timeout_s=600 + 守卫用例同步），「backend/ 零改动」口径在汇报与 PR 中如实记一笔。

## 8. 实现注记（对 §2 决策的两处如实偏离/细化）

- **D1-A 半项按「分域保留双 map」落地**（审查 m-3）：简报原案「test-logic 的 TESTING/TEST_* 三条改为引用 kb-logic」。实现中发现 test-logic 的 map 是**测试运行态**（含 STALE），与版本生命周期档位是不同域，强行合并会把运行态 STALE 混进版本文案。落地形态：kb-logic `DOCUMENT_STATUS_META` 补齐全 14 档作为**版本/文档状态**唯一来源；test-logic 运行态 map 原样保留（消费方不同），kb-logic 注释明确分域。比原案更合理，已登记非静默决定。
- **409 码覆盖映射**（审查 m-4 的兑现口径）：mock E2E 覆盖 `KB_NO_ACTIVE_PUBLISH`/`KB_ROLLBACK_TARGET_IS_ACTIVE`（UI 可达）；`KB_NOT_TEST_PASSED` 的 409 路径由全栈链② API 断言（下线后重发）；`KB_PUBLISHED_NOT_ACTIVE` 两分支 UI 正常流不可达（发布按钮只对 TEST_PASSED）——「指针已撤」分支由 KB-05 集成测试直删 alias 行实测（`test_kb_publish.py:423`），**「已发布非活跃 → 请使用回滚端点」分支（service.py:2513-2519）全库无实测**，mock 侧有该分支实现但无用例触达；补后端集成用例归 KB-05B 后续或 M3 边界评审（不阻塞本点，登记在 review-log）。`KB_INDEX_*` 两码 mock 明示不支持（真实后端复核索引，有注释锚点）。

## 9. 实现结果（2026-11-10）

**交付清单**（15 文件 + 生成文件 components.d.ts）：
- 前端生产代码：`packages/api-client/src/kb.ts`（4 方法 + 2 契约类型）、`apps/admin/src/kb-logic.ts`（14 档文案 + 3 门禁纯函数 + rollbackTargets + kbPublishActionMessage + 2 权限码常量）、`apps/admin/src/components/KbDocumentDrawer.vue`（发布/回滚/下线/删除按钮 + 回滚下拉 + 版本表状态列 + activeVersionId 会话记忆）。
- 测试：`kb.test.ts` +6、`kb-logic.test.ts` +10、`kb-mock.ts` 4 条有状态路由（判据逐字锚定 service.py/status.py，审查修复 m-1/m-2/m-7 后与后端状态同步语义一致）、`admin-kb.spec.ts` +7、`admin-fullstack-kb-publish.spec.ts` 新 3 条（TD-292）、`e2e_seed.py` +2 账号（e2e-kb-approver / e2e-kb-publisher，D3-A）、`docker-compose.e2e.yml` +tei-embed（D4-A，复用 external 卷 campus-elf_tei_models）、`e2e-stack.sh` +wait_tei_healthy + 卷预检、`support.ts` 账号类型。
- 后端修复：`app/workers/settings.py` 注册 `run_kb_test`（Function 包装 timeout_s=600）+ `tests/test_worker_settings.py` 守卫 4→5（len/name/timeout 三重钉住）。

**本地门禁读数**（2026-11-10，win32 / Node 24 / CPython 3.13）：
- 前端：`typecheck`/`lint`/`format:check`/`build` 全绿；单测 **247**（api-client 91 = 85+6、admin 156 = 146+10）；mock E2E **146**（基线 139 + 7）；全栈 E2E **25 passed（3.0m，22 既有 + 3 新）**。
- 后端：`ruff`（app/tests/scripts/alembic）All checks passed；`mypy --no-incremental` 253 文件 0 错误；`bandit` ×2 exit 0；`alembic check` 无漂移；`test_worker_settings.py` 9 passed。
- 全栈 E2E 关键验证：审批→构建 409 门禁→publisher 403 职责分离→审批人 UI 通过→构建→READY（真实 TEI embedding + Qdrant，构建 ~31s）→测试运行（worker 修复后 `test_passed, pass_rate 1.0`）→UI 发布→重放 `replayed=true`→回滚 409 兜底→UI 下线→下线后重发 409 `KB_NOT_TEST_PASSED`→UI 删除→发布响应契约逐字段。
- **跑法坑（本点实测）**：全栈套件设计为「每轮种子跑一次全量」——改密类用例（forced-change/forgot-reset）会永久改账号口令，不重置种子直接复跑必假失败（本次 run #4 五条失败即此因，重置种子后 25/25）；抽屉/审批抽屉开着时 mask 挡退出登录按钮，换账号前必须先关抽屉。

**独立审查**（第 1 轮，独立子智能体审 staged diff）：**0 Blocker / 1 Major / 7 Minor**，结论「修复前不可提交」。M-1 文档回写未随 diff 交付（本节即补齐）；m-1 mock offline/delete 非活跃分支覆写 document.status（后端只在 was_active 同步）→ 已修（syncDocument 参数）；m-2 mock publish/rollback 不迁移 current_version 指针 → 已修（migrateCurrentVersion）；m-3 D1-A 半项偏离 → §8 登记分域保留；m-4 409 码覆盖口径 → §8 映射 + 后端第二分支无实测登记 review-log；m-5 components.d.ts 未暂存 → 已暂存；m-6 wait_tei_healthy 上限（400s）< start_period（600s）+ external 卷无预检 → 已修（700s + 预检与可操作提示）；m-7 mock 重放 published_at 返回新鲜时间戳 → 已修（activePublish 持久化首发时间）。

**第 2 轮复审**（同一子智能体续会话，只审修复增量）：**0 Blocker / 0 Major，结论「可以提交」**。唯一残留（Minor，不阻塞）：**回滚成功响应的 `published_at` mock 回传回滚时刻而非目标版本首发时间**（后端 service.py:2700/2740 回传首发时间；UI 不消费该字段，纯契约保真）——修法需 mock 按 versionId 持久化各版本首发时间，随后续 mock 维护点处理，留痕于此。
