# M3-KB-04A — 测试沙箱（KB-04 后端点）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-11-08 |
| 状态 | 已确认（2026-11-08，按推荐项执行；用户未逐项响应，沿用惯例） |
| 关联功能点 | M3 第 8 点：KB-04 测试沙箱（后端；前端测试管理 UI 随 KB-04B 下一简报）。KB-03 全点已合并（`87f9b83` + PR #45） |
| 验收引用 | `docs/spec.md` §3.6 KB-04（:273-282）、`docs/design.md` §4.3（:230-232）/§5.5（:468-469）/§7.5、`docs/state-machines.md` §3（:58-79, :93）、`docs/milestone.md` §6.2（:247） |
| 不做的事 | LLM 回答生成与 Ragas 评分（M4——Harness 无 LLM 客户端）；`evals/` 项目的 Ragas 评估批次（TD-294，独立 uv 项目）；发布侧「未通过测试不能发布」门禁（KB-05）；前端测试管理 UI（KB-04B）；Langfuse dataset/experiment（可选，M4）；对抗样本专有处理（标记字段落库即可） |

## 0. 现状核对

**关键约束（决定了 KB-04 的 MVP 边界）**

- **Harness 无 LLM 客户端**：`app/harness/` 只有契约/注册/工具骨架（M0 骨架），`app/config.py` 无 LLM 配置项，`openai`/`anthropic` 未引入——LLM 回答生成在 M4 才可用。
- **Qdrant 无 search 函数**：`app/kb/indexer.py` 只有 `ensure_collection`/`upsert_points`/`count_points`——检索查询（dense+sparse → 混合搜索）需要新建。
- **CSV/XLSX 解析已有范式**：`app/org/import_parser.py` 的 charset 探测 + `csv.Sniffer` + `openpyxl` 只读模式可直接复用到测试集文件解析。
- **状态机**：版本 `READY → TESTING → TEST_PASSED / TEST_FAILED`（`state-machines.md` §3）；`TEST_PASSED` 才能发布（KB-05 门禁）。
- **design 表已规划**：`kb_test_sets`（文件名/对象 key/状态）/ `kb_test_cases`（question/expected_answer/expected_points/expected_chunk_ids/category/is_adversarial/row_no）/ `kb_test_runs`（status/metrics_json/report_object_key/started_at/finished_at）——三表均为净新增（迁移）。
- **Qdrant 已有真实向量**：KB-03A 业务场景已将文档构建到 READY（真实 TEI dense + BM25 sparse），可被测试检索命中。

**spec 的验收拆解（逐条归点）**

| spec 验收 | 归属 |
|---|---|
| 未通过测试 → 发布拒绝 | **KB-05**（发布侧读版本状态） |
| 测试集 20 条 → 通过率/失败样例/引用命中/延迟；报告可查 | **本点**（检索命中即「引用命中」，延迟 = 每条检索耗时） |
| 标记会话不影响生产会话/统计 | **本点**（检索直查 Qdrant，不经 chat session——天然隔离） |
| 格式错误 → 行级错误，不启动评估 | **本点** |
| Ragas 报告 / 期望答案评分 | **M4**（LLM 回答生成 + Ragas 独立 uv 项目） |

## 1. 目标

1. **测试集上传**：`POST /school/kb/versions/{version_id}/test-sets`（multipart：CSV/XLSX/JSON，权限 `school.kb.test`）→ 解析 → 行级校验（缺 question → 行错误；格式非法 → 不启动评估）→ 落 `kb_test_sets` + `kb_test_cases`。
2. **测试运行**：`POST /school/kb/versions/{version_id}/tests`（`school.kb.test`；版本必须 READY）→ 取 case 集 → **逐条检索**（Qdrant dense+sparse 混合搜索 → top_k）→ 对账（expected_chunk_ids 命中 / expected_points 文本重叠）→ 指标（通过率/逐 case 命中/延迟）→ 版本状态迁移 TESTING → TEST_PASSED/TEST_FAILED → `kb_test_runs` 落报告。
3. **Qdrant search**：`app/kb/indexer.py` 新增 `search_points`（dense + sparse → 混合检索，为 KB-06/07 检索测试/检索测试端点复用）。
4. **状态机扩展**：`READY → TESTING`、`TESTING → TEST_PASSED / TEST_FAILED`；`TEST_FAILED → TESTING`（重跑）。
5. **报告查询**：`GET /school/kb/test-runs/{run_id}`（`school.kb.view` + 数据范围；metrics_json 全字段）。

**验收（本点，全部断言业务结果）**

- 上传：合法 CSV/XLSX/JSON → `kb_test_sets`/`kb_test_cases` 落库；缺 question 行 → 行级错误；空文件 → 422；跨租版本 404。
- 运行：READY 版本 + 测试集 → TESTING → 完成后 TEST_PASSED 或 TEST_FAILED（取决于命中率阈值）；`kb_test_runs` 有 metrics_json（pass_rate/total/latency_p50/p95/逐 case 结果）；审计 `school.kb.test.completed`。
- 状态机：REJECTED/BUILDING 版本运行测试 → 409；TEST_PASSED 重跑 → 允许（重新评估）；TESTING 并发 → 409（活跃 run 唯一）。
- 搜索：dense+sparse 混合查询命中已索引子块（真实 Qdrant 集成验证）。
- 权限：无 `school.kb.test` → 403；跨租测试集/版本 → 404。

## 2. 候选方案

### D1 测试运行的范围（MVP 边界）

- **方案 A（推荐）：M3 = 检索命中测试（无 LLM）**——每条 case 的 question → embed → Qdrant 混合搜索 → top_k 子块 → 对账 expected_chunk_ids 命中率 + expected_points 文本重叠率 → 阈值判定通过/失败。LLM 回答生成 + Ragas 评分 → M4（Harness 接 LLM 后）。
  - 优点：不阻塞 M3 闭环（spec 验收只要求「通过率/引用命中/延迟」——全部可检索层满足）；LLM 回答质量归 M4（有 AI Harness 后用 Ragas 跑完整评估）。
  - 缺点：「回答正确性」未验证（M3 只验「检索能否找到正确块」）——但这正是 spec「引用命中」的语义。
  - 对 spec 的影响：无（验收全满足；LLM 部分归 M4）。
- 方案 B：等 M4 LLM 就绪后做全链路测试（检索+回答+Ragas）。缺点：M3 闭环（→KB-05 发布）被阻塞；「测试不通过不能发布」门禁需要测试先行。不选。

### D2 测试集文件格式与解析

- **方案 A（推荐）：CSV/XLSX/JSON 三格式，复用 `import_parser.py` 范式（charset 探测 + Sniffer + openpyxl）**；列名 `question` 必填，`expected_answer`/`expected_points`/`expected_chunk_ids`/`category`/`is_adversarial` 可选；JSON 支持数组 `[{...}]`。
  - 行级错误：`{row_no, code, message}`（缺 question / 格式非法）→ 上传 422 返回错误列表，不启动评估；合法行入库（部分合法部分非法 → 整批拒绝，与 KB-01A 整批原子同口径）。
- 方案 B：只支持 JSON（手写）。缺点：校方运营不写 JSON；CSV/XLSX 是普通人的工具。不选。

### D3 测试状态机与重跑

- **方案 A（推荐）**：版本 `READY → TESTING → TEST_PASSED / TEST_FAILED`；`TEST_FAILED → TESTING`（重跑）+ `TEST_PASSED → TESTING`（重新评估也允许——重新测试是低风险操作）。活跃 run 唯一（部分唯一索引 `(tenant_id, version_id) WHERE status='TESTING'`）。
  - 依据：spec 允许重复运行测试（测试集修改后重跑）；状态机 §3 的 TESTING 是过程态。
- 方案 B：TEST_PASSED 后不允许重跑（需要 reset）。缺点：不必要的门禁。不选。

### D4 Qdrant 检索函数

- **方案 A（推荐）：`search_points` 放 `app/kb/indexer.py`**——输入 query 向量（dense + sparse）+ collection + top_k → 混合搜索（Qdrant `query_points` API 的 prefetch 模式：dense 排序 + sparse 排序 → RRF 融合）→ 返回 `[{chunk_id, score, payload}]`。
  - Qdrant 1.19.2 原生支持 `query_points` with `prefetch` + `fusion: RRF`（服务端混合检索），不需要客户端手动 RRF。
- 方案 B：客户端手动 RRF（dense 查一次 + sparse 查一次 → Python 合并）。缺点：多一次网络往返，RRF 融合逻辑重复。不选。

### D5 测试通过的阈值

- **方案 A（推荐）：通过 = 全部 case 的 expected_chunk_ids 均在 top_k 命中**（100% 命中才过；top_k = case 级可配，默认 10）。expected_points（文本重叠）作为**报告维度**（不参与 pass/fail 判定——阈值待 TD-294 实验后定）。
  - 依据：spec「引用命中」= 期望引用是否被召回；100% 是最保守的安全侧阈值（宁严勿松——测试没过不该发布）。
- 方案 B：按命中率阈值（如 ≥80% 通过）。缺点：阈值无评估依据（TD-294），提前写死违反「不提前写死」纪律。不选。

### D6 切分：KB-04A（后端本点）/ KB-04B（前端测试管理）

- **方案 A（推荐）：本点只做后端**（API + 状态机 + Qdrant search + 报告）；前端测试管理 UI（上传/运行/报告查看）随 KB-04B 下一简报。与 KB-01A→B、KB-02→02B 节奏一致。
- 方案 B：全栈一个 PR。缺点：diff 过大（迁移 + 解析器 + worker + 3 端点 + Vue 页面 + mock E2E）。不选。

## 3. 选择结论

- **D1 A（检索命中 MVP，LLM → M4）／D2 A（CSV/XLSX/JSON 复用解析范式）／D3 A（TEST_PASSED 可重跑）／D4 A（Qdrant 原生 RRF prefetch）／D5 A（100% 命中阈值）／D6 A（后端先行）**。用户 2026-11-08 未逐项响应问题卡片——按既往 KB-02/02B/03A/03B 全部按推荐项确认的惯例，D1–D6 全按推荐项执行。

## 4. 实现顺序与 TDD 测试清单

1. **迁移（head `f2a8b4c6e0d7` → 新）**：`kb_test_sets`/`kb_test_cases`/`kb_test_runs` 三表 + FORCE RLS + 部分唯一索引（TESTING 活跃唯一）。
2. **状态机（先红）**：`status.py` 扩展 `READY→TESTING`、`TESTING→TEST_PASSED/TEST_FAILED`、`TEST_FAILED→TESTING`、`TEST_PASSED→TESTING`；`assert_test_allowed`（只 READY/TEST_FAILED/TEST_PASSED 可发起）。
3. **Qdrant search（先红）**：`indexer.py` 增 `search_points`（Qdrant prefetch RRF）；真实 Qdrant 集成测试（KB-03A 构建后的 collection 查询）。
4. **测试集解析器（先红）**：`app/kb/test_parser.py`（CSV/XLSX/JSON → 行级 cases + errors；纯函数）。
5. **测试运行 worker（先红）**：`app/kb/test_task.py`（`run_kb_test`：取 TESTING → 逐 case 检索 → 对账 → 指标 → TEST_PASSED/FAILED 收口；复用 build_task 三段式范式）。
6. **端点（先红）**：test-sets 上传 / tests 运行 / test-runs 查询 / approvals-by-version（TD-291 已在 KB-03B 交付，此处只补测试覆盖）。
7. **业务场景**：真实引擎（MinerU→构建→测试）全链路（探测 skip 同 TD-295）。
8. **门禁与审查**。

红→绿清单：

- [ ] 状态机：READY→TESTING→TEST_PASSED/FAILED 合法；REJECTED/BUILDING 测试 409；TESTING 并发 409
- [ ] 解析器：CSV/XLSX/JSON 三格式；缺 question 行级错误；空文件 422；整批原子
- [ ] 搜索：dense+sparse RRF 命中已索引子块；跨租 collection 隔离
- [ ] 运行：metrics_json 含 pass_rate/total/latency/逐 case 命中；TEST_PASSED/FAILED 状态迁移 + 审计
- [ ] 快照/报告查询：全字段返回；跨租 404；无 view/test 码 403

## 5. 测试策略

| 层级 | 内容 | 运行地 |
|---|---|---|
| 单元 | 解析器（三格式 × 行级错误）、指标计算、状态机扩展 | CI |
| 集成 | Qdrant search（KB-03A 构建后真实查询）、测试运行编排（fake parser/embedder + 真实 Qdrant）、快照端点权限/跨租 | CI |
| 业务场景 | 真实引擎：构建→上传测试集→运行→TEST_PASSED（探测 skip 同 TD-295） | 本地 |
| 静态门禁 | ruff / mypy / bandit / alembic check | 本地+CI |

## 6. 预判技术债

| TD 编号 | 条目 | 处置 |
|---|---|---|
| TD-298（新） | **LLM 回答生成 + Ragas 评分未接**：M3 检索命中测试不含回答质量评估；expected_answer/expected_points 只存储不参与判定 | 已采纳 → M4（Harness 接 LLM 后，Ragas 独立 uv 项目跑） |
| TD-299（新） | **测试通过阈值 100% 过严**：TD-294 实验后可能需要调整为命中率阈值 | 已延后 → TD-294 评估后定 |
| TD-300（新） | **测试集版本绑定**：测试集绑定 document_version_id，版本切换后需重新绑定/上传——版本切换场景（KB-08）未覆盖 | 已延后 → KB-08（版本切换时决定测试集迁移策略） |

## 7. 高风险确认

- **属于高风险功能**：状态机扩展（TESTING/TEST_PASSED/TEST_FAILED 是 KB-05 发布门禁的前置）；Qdrant search 是共享原语（KB-06/07 复用）。
- **需要用户确认的点**（推荐项在前）：
  1. **D1 检索命中 MVP（无 LLM）**——LLM 回答/Ragas → M4。
  2. **D3 TEST_PASSED 可重跑**（重新评估不被门禁阻止）。
  3. **D5 100% 命中阈值**（宁严勿松；阈值调整归 TD-294 实验后）。
  4. **D6 后端先行**（KB-04B 前端下一简报）。
- 其余两项（D2 解析格式、D4 Qdrant 原生 RRF）推荐直接按 A 执行，有异议请在确认时指出。
- **确认结论**：按推荐项执行（用户未逐项响应，沿用既有惯例）。

## 9. 交付事实（2026-11-08）

- **PR #47**（`feature/m3-kb04a-test-sandbox`，head `df780bc`）→ rebase 合并，分支已删除。合并授权：用户在 PR CI 全绿后确认。
- **PR CI**：backend run `42393250936` **986 passed, 1 skipped（992.33s / 16:32）**（skipped=真实引擎 CI skip，TD-295）；四包分支覆盖率 79.92/82.41/81.25/66.67% 全过；security run `42393251058` success。
- **本地门禁**：ruff/mypy（249 文件）/bandit×2/alembic check（roundtrip 实测）全绿；67 passed（KB-03A/B 回归 + 新增测试）。
- **TD**：TD-298（LLM 回答+Ragas → M4）、TD-299（100% 阈值调整 → TD-294）、TD-300（测试集版本绑定 → KB-08）已登记。