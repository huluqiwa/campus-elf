# M3-KB-03A — 构建执行管线（KB-03 后端点）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-11-07 |
| 状态 | 待用户确认（确认前不写任何生产代码） |
| 关联功能点 | M3 第 6 点：KB-03 构建进度与执行管线（后端 A 点；SSE/前端进度为 KB-03B 下一简报）。KB-01/KB-02 已全点合并（`3e763ca`/`d6de5a6`/`7d1ce56`/`edc34f6`/`45f84d4`） |
| 验收引用 | `docs/milestone.md` §6.2（:247「KB-03 构建进度」）与 §6.4（:268-274）、`docs/spec.md` §3.6 KB-03（:260-272）、`docs/design.md` §7（:890-930）/§5.6（SSE 归 B 点）/§4.3（:224-227）、`docs/state-machines.md` §3/§4、TD-288（快照接口+取件执行器为本点首个交付项） |
| 前置技术债 | **TD-288**（`BUILD_QUEUED` 无消费者 → 本点兑现）；**TD-291** 在 KB-03B 兑现（按 version 查审批单端点与前端直显同批）；TD-046（上线验证必须带一次真实 `/v1/file_parse` → 本点业务场景即兑现）；TD-041（jieba SyntaxWarning M3 评估 → 本点决策见 D3） |
| 不做的事 | SSE `build.*` 事件流与 `kb_build_events` 持久化、Last-Event-ID 续传、心跳推送（KB-03B）；前端进度 UI（KB-03B）；TD-291 按 version 查审批单端点（KB-03B 与前端直显同批）；DOCX/PPTX 解析路径（D2 → TD-293）；发布/alias 切换（KB-05）；测试沙箱（KB-04）；切块参数实验本身（D3 → evals，TD-294）；派生版本/块编辑（KB-10）；ACL 人群过滤 payload（KB-09 在检索侧强制，构建 payload 预留字段） |

## 0. 现状核对（读代码与文档得到的事实）

**已有基座**

- `kb_build_jobs` 最小表已随 KB-02 建好（`d5e9a3c7b1f4`：id/tenant_id/version_id/status=BUILD_QUEUED/attempt/created_by + 部分唯一索引 `(tenant_id, version_id) WHERE status IN ('BUILD_QUEUED','BUILDING')` 兜底「一版本一活跃构建」）；design §4.3（:225）规划的全字段（stage/progress/processed_units/total_units/tier/fallback_reason/retry_count/error_code/message/started_at/finished_at）尚未落。
- 版本状态机 `app/kb/status.py`：`APPROVED`/`REJECTED` 本点是终态；state-machines §3 规划 `APPROVED → BUILDING → READY/FAILED`，§4 规划 job 内阶段 `QUEUED → PARSING → NORMALIZING → CHUNKING → EMBEDDING → INDEXING → VALIDATING → READY/FAILED`（阶段是 **job 的 stage 字段**，版本状态只有 BUILDING 一个值）；「重试创建新 attempt，不覆盖历史」「Worker 心跳；超时任务标记 FAILED/STALE」。
- 取件范式：`app/kb/scan_task.py`（短事务取件 → 事务外执行 → 条件更新收口）+ 看门狗 `retry_pending_kb_scans`（每 5 分钟 cron）；arq 队列命名 `workers/settings.py`（`<redis_prefix>:queue`）。
- 对象存储：版本源文件在 MinIO `kb/<tenant_id>/<document_id>/<version_id>/source<ext>`（`ObjectStore` 可读回字节）。
- 权限/审计：`school.kb.build`（build 端点已在 KB-02 落地，创建 → `BUILD_QUEUED` + 审计 `school.kb.build.created`）；快照/重试端点沿用 `school.kb.view`/`school.kb.build` + 数据范围 + 跨租 404。

**管线设计（design §7，全部为规划、零实现）**

- 解析（§7.1）：PDF/图片 → MinerU 4.0.12 V1 API（Standard → Basic → Flash）；TXT/MD 直接 Markdown 规范化；统一输出 Markdown + 页/章节/表格/代码元数据；`DocumentParserPort` 封装。M0-E §8 已实证：自定义 torch 镜像（`deploy/mineru/`，parser profile，端口 8100）三档真实解析通过（热态 1.9/2.0/0.9s，显存 421-455MiB）；**`/v1/health` 不能作为可用性证据，必须真实 `/v1/file_parse`**（TD-046）；测试夹具 `backend/tests/fixtures/mineru_smoke.pdf` 已存在。
- 切块（§7.2）：父子块；候选参数子块 300/500/800 字、重叠 10/15/20%、父块 800–1200；**表格/代码不拆**；「M3 必须用评估集比较后定参数，回写设计」——实验本身未做。
- 嵌入/稀疏（§7.3）：BGE-M3 1024 维（TEI CPU，dev 栈 `tei-embed` healthy）；Sparse = jieba 分词 + BM25（按版本维护 IDF）+ token hash uint32 → Qdrant sparse vector；每子块嵌入前加「文档名+章节路径」上下文。
- Qdrant（§7.4）：collection `tenant_{tenant_hash}_kb_v{version_id}`；payload 含 `tenant_id_hash/kb_id/document_id/version_id/chunk_id/parent_id/doc_title/section_path/page_start/end/chunk_type/text_hash/is_published`；named vectors `dense`+`sparse`；**alias 切换是 KB-05**（本点只建 version collection，不动 alias）。
- 发布侧（KB-05/KB-09）才需要 `kb_publish_aliases`/人群 token——本点不建。

**环境约束**

- **CI 无 GPU/无 MinerU/TEI 容器**：管线测试必须把解析/嵌入做成可注入 Port，CI 用 fake；真实 MinerU/TEI 的集成与业务场景测试只在本地 dev 栈跑（环境探测，不可达即 skip）。
- jieba 0.42.1 在 Python 3.13 有 SyntaxWarning（TD-041，当时延后 M3 评估）——M3 已到，本点决策见 D3。
- test 栈已有 Qdrant（`campus-elf-test-qdrant-1`），Qdrant 相关集成测试可在 CI 真跑。

## 1. 目标

1. **构建管线端到端**：arq worker 取件 `BUILD_QUEUED` → PARSING（MinerU / TXT/MD 规范化，tier 降级链）→ NORMALIZING → CHUNKING（父子块，参数可配）→ EMBEDDING（TEI dense + jieba/BM25 sparse）→ INDEXING（Qdrant version collection upsert）→ VALIDATING（计数对账）→ READY；任一阶段失败 → FAILED（error_code/message 落 job，可重试）。
2. **可观测最小集**：`GET /school/kb/build-jobs/{job_id}` 快照（TD-288 兑现：stage/progress/tier/error 全字段）；重试端点 `POST .../build-jobs/{job_id}/retry`（attempt+1，新 attempt 不覆盖历史）；tier/`fallback_reason` 落 job。
3. **Qdrant 版本索引**：`tenant_{hash}_kb_v{version_id}` collection 创建 + named vectors（dense+sparse）+ payload 齐全 + `kb_index_versions` 记录（发布/alias 留 KB-05）。
4. **状态机扩展**：版本 `APPROVED → BUILDING → READY/FAILED`、`FAILED → BUILDING`（重试）；job stage 阶段机（§4 表）；看门狗回收 stale BUILDING（心跳超时 → FAILED/STALE，可重试）。
5. **TD-288 兑现**：`BUILD_QUEUED` 行从创建（KB-02 已交付）到被取件执行的全链路打通。

**验收（本点，全部断言业务结果）**

- 业务场景（本地 dev 栈，真实 MinerU+TEI）：上传 PDF → 审批 → 构建 → READY；`kb_chunks` 有父子块（表格/代码块未拆）、Qdrant collection 点数与 chunks 对账一致、payload 字段齐全、`kb_index_versions` 有记录、job 的 tier=`standard` 且 `fallback_reason` 为空（或真实降级时记录原因）；**TD-046 兑现**（真实 `/v1/file_parse`）。
- 降级链：注入「standard 5xx → basic 成功」的 fake parser（CI 可跑）→ job.tier=basic、fallback_reason 记录；三档全失败 → FAILED + error_code，重试后 attempt+1 且历史不被覆盖。
- 门禁：REJECTED 版本重试 → 409（状态机）；跨租 job → 404；无 `school.kb.build` 重试 → 403；审计 `school.kb.build.retry`（成功/拒绝独立事务）。
- 看门狗：心跳超时的 BUILDING → FAILED/STALE（可重试），活跃任务唯一性不破坏。
- 静态门禁 + CI（fake ports）全绿：CI 用例集覆盖编排全阶段与降级/失败/重试；真实 MinerU/TEI 用例本地跑（env 探测跳过，CI 不红）。

## 2. 候选方案

### D1 范围切分（本简报 = KB-03A）

- **方案 A（推荐）：A = 执行管线 + 快照 + 重试（本点）；SSE 事件流/`kb_build_events`/前端进度/TD-291 = KB-03B（下一简报）**。
  - 优点：A 的验收不依赖任何 UI（API+库+Qdrant 断言）；B 的 SSE 需要 A 的真实进度事件做数据源，先后依赖天然成立；与 KB-01A→B、KB-02→02B 的拆分节奏一致。
  - 缺点：A 合并后进度只能靠快照轮询（无推送）——但快照轮询本来就是 §5.6 规划的兜底通道，不算造假。
  - 对 spec 的影响：无（milestone KB-03 的「SSE/轮询」整体在 A+B 两点内完成）。
- 方案 B：管线 + SSE 一个点。
  - 缺点：diff 规模失控（迁移 ×3 表 + 5 个新模块 + worker + 2 端点 + SSE 基建 + 前端），审查无法聚焦。不选。

### D2 解析格式范围

- **方案 A（推荐）：本点 PDF/MD/TXT 三类真实解析（MinerU / 直接规范化）；DOCX/PPTX → TD-293，随 KB-03 后续点补 LibreOffice 容器 + Flash 原生回退**。
  - 依据：design §7.1 的 DOCX/PPTX 路径需要 **LibreOffice headless 容器**——M0 未验证过该容器（显存/转换质量均为未知），引入它是运维+质量双重风险；而 PDF/MD/TXT 已有 M0 实证（`mineru_smoke.pdf` 三档通过），足以把管线、降级链、Qdrant、状态机全部打通验收。
  - DOCX/PPTX 的构建任务在本点会走到 PARSING → FAILED（`error_code=PARSER_UNAVAILABLE`，message 明确「该格式解析器尚未接入」，可重试）——**诚实失败**，不是静默吞。
  - 缺点：上传白名单里的 DOCX/PPTX 暂时构建不了；登记 TD-293 写死去向。
- 方案 B：本点引入 LibreOffice 容器。
  - 缺点：新容器（约 1GB）+ compose/部署文档 + 未验证的转换质量，diff 跨运维域；与「一次只推进一个可运行逻辑单元」冲突。不选。

### D3 切块参数与 jieba（TD-041 到期决策）

- **方案 A（推荐）：切块参数做成 `Settings` 可配（`KB_CHUNK_SIZE`/`KB_CHUNK_OVERLAP`/`KB_PARENT_CHUNK_SIZE`），临时默认 500/15%/1000（候选集中间值）；父块按章节聚合；表格/代码块整体不拆**。参数实验（300/500/800 × 10/15/20% 的 recall/faithfulness/citation precision 评估）归 evals 批次（TD-294），实验后回写 design 并改默认。
  - 依据：milestone §6.4「切块参数 M3 实验后定，不提前写死」——「不写死」≠「不实现」：参数可配 + 临时默认是唯一不阻塞管线的正解；评估需要已构建的语料，鸡生蛋。
- **jieba（TD-041）**：继续用 jieba 0.42.1 做 BM25 分词（`import` 时的 SyntaxWarning 用 warnings filter 收敛 + 测试按类别过滤，沿用 M0-E 既有处理），替代分词器评估登记 TD-294 附带项——换分词器影响 sparse 向量与检索召回，等实验一并定。
- 方案 B：等评估定参再实现切块。缺点：管线全线阻塞，评估又需要管线产出。不选。

### D4 测试策略（CI 无 GPU 的约束）

- **方案 A（推荐）：解析/嵌入做成 `DocumentParserPort`/`EmbeddingPort` 可注入接口**——CI 集成/业务测试用 **fake ports**（脚本化 tier 降级/失败/超时，断言编排逻辑）；真实 MinerU/TEI 的集成用例加 `mineru_url`/`tei_url` 可达性探测（不可达 → `pytest.skip`，CI 不红），业务场景「PDF 构建到 READY」**只在本地 dev 栈跑**并记录读数（与全栈 E2E 仅本地同一口径，TD-170 同源）。
  - 优点：CI 945+ 用例照常全绿；真实引擎证据由本地业务场景 + TD-046 兑现记录承担。
  - 缺点：CI 对「真实解析质量」零覆盖（本就测不了）；降级链的真实行为靠本地用例。
- 方案 B：CI 加 MinerU 容器。缺点：GitHub Actions 无 GPU，MinerU CPU 模式冷启动 4 分钟+，CI 时长/稳定性不可接受。不选。

### D5 降级链的判定与记录

- **方案 A（推荐）：tier 从 `Settings.KB_PARSER_TIER_ORDER`（默认 standard→basic→flash）逐档尝试；当前档失败（HTTP 5xx/超时/解析错误）→ 记录失败原因并降下一档；全档失败 → FAILED**。job 落 `tier`（**成功档位**）与 `fallback_reason`（未用首选档时记录「standard: <原因>」链式摘要）。
  - 依据：M0-E 实证 standard 未触发降级、显存非瓶颈（VLM 走 CPU）——降级主要防解析失败而非 OOM；§7.1「记录 tier/降级原因」正是这个语义。
- 方案 B：按显存预估选档。缺点：无可靠预估信号，过度设计。不选。

### D6 取件执行器与看门狗

- **方案 A（推荐）：arq worker 新函数 `build_kb_version`（`scan_kb_version` 同款三段式：短事务取件+版本转 BUILDING → 事务外执行管线 → 条件更新收口 READY/FAILED）；cron 看门狗 `retry_stale_build_jobs`（心跳超时 >10 分钟的 BUILDING → FAILED/STALE，可重试）**；`heartbeat_at` 字段由管线各阶段推进时更新。
  - 依据：与扫描 worker 同构，评审面最小；`kb_build_jobs` 部分唯一索引（`WHERE status IN ('BUILD_QUEUED','BUILDING')`）已在 KB-02 建好，天然兜底「取件与重试并发」。
- 方案 B：同步执行（API 内跑管线）。缺点：MinerU 解析秒级~分钟级，阻塞请求；§4 明确异步队列。不选。

## 3. 选择结论

- **D1 A（A/B 两点切分）／D2 A（PDF/MD/TXT 先行，DOCX/PPTX → TD-293）／D3 A（参数可配 + 临时默认 500/15%/1000，实验 → TD-294）／D4 A（fake ports 进 CI + 真实引擎本地跳过式集成）／D5 A（逐档降级 + 链式 fallback_reason）／D6 A（arq 三段式 + 心跳看门狗）**。用户 2026-11-07 问题卡片确认 D1–D4 全按推荐项执行；D5/D6 按推荐默认（未提异议）。

## 4. 实现顺序与 TDD 测试清单

1. **迁移（head `d5e9a3c7b1f4` → 新）**：`kb_build_jobs` 加列（stage/progress/processed_units/total_units/tier/fallback_reason/retry_count/error_code/message/started_at/heartbeat_at/finished_at + stage CHECK）；新表 `kb_chunks`（design §4.3 全字段 + RLS）与 `kb_index_versions`（+ RLS）；downgrade 可回滚（数据前提照 KB-02 惯例写明）。
2. **状态机与快照（先红）**：`app/kb/status.py` 扩展（APPROVED→BUILDING→READY/FAILED、FAILED→BUILDING；BUILDING/READY/FAILED 对 `assert_build_allowed` 的语义——只有 APPROVED 可**首次**构建，READY/FAILED 重试走 retry 端点）；快照端点 `GET /school/kb/build-jobs/{job_id}`（TD-288）。
3. **解析端口与适配器（先红）**：`app/kb/parser/`（`DocumentParserPort` 协议 + `MinerUParser` 真实 HTTP 适配器 + `MarkdownNormalizer`（TXT/MD 直接规范化）+ tier 降级编排 `TieredParser`）；单测用 fake HTTP（respx 或手写 transport）钉降级链。
4. **切块与嵌入（先红）**：`app/kb/chunker.py`（父子块、重叠、章节路径、表格/代码不拆——纯函数）+ `app/kb/embedding/`（`EmbeddingPort` + TEI 适配器 + jieba/BM25 sparse 权重 + uint32 token hash）。
5. **Qdrant 索引（先红）**：`app/kb/indexer.py`（collection 命名/创建、named vectors upsert、payload、计数对账；真实 Qdrant 集成测试）。
6. **worker 编排（先红）**：`app/kb/build_task.py`（`build_kb_version` 三段式 + 阶段推进 + 心跳 + 条件收口 + 审计 `school.kb.build.completed/failed`）+ 看门狗 `retry_stale_build_jobs` + `workers/settings.py` 注册；重试端点（attempt+1、状态机、审计 `school.kb.build.retry`）。
7. **业务场景（本地）**：真实栈 PDF 构建到 READY（TD-046 兑现记录）+ 降级/失败路径（fake ports）。
8. **门禁与审查**：ruff/mypy/bandit/alembic check；受影响子集；独立审查 staged diff。
9. **文档**：简报状态/§9、design §5.5/§7 落地备注、state-machines、test-plan、handoff、README、milestone、review-log、tech-debt（TD-293/294 登记 + TD-288/TD-046 收口）。

红→绿清单：

- [ ] 状态机：APPROVED→BUILDING→READY/FAILED、FAILED→BUILDING 合法；REJECTED/UPLOADED 构建 409（既有 KB-02 用例不回归）；未知阶段拒绝
- [ ] 快照：全字段返回（stage/progress/tier/fallback_reason/error_code/attempt/retry_count/时间戳）；跨租 404；无 view 403
- [ ] 解析：PDF → MinerU `/v1/file_parse`（真实本地）；TXT/MD → 规范化；tier 降级链逐档尝试 + fallback_reason 链式记录；全档失败 → FAILED
- [ ] 切块：父子块结构与 `section_path/page_start/overlap` 正确；表格/代码块整体不拆；参数从 Settings 读取；重叠不产生空块/无限循环
- [ ] 嵌入：dense 1024 维 + sparse（jieba 分词 + BM25 权重 + uint32 hash）；嵌入前拼「文档名+章节路径」上下文；TEI 不可达 → FAILED（可重试）
- [ ] Qdrant：collection 命名 `tenant_{hash}_kb_v{version_id}`；named vectors 双向量 upsert；payload 十字段齐全；跨租 collection 隔离
- [ ] 编排：阶段推进（PARSING→…→VALIDATING）与心跳更新；任一阶段失败 → FAILED + error_code；VALIDATING 计数对账不一致 → FAILED
- [ ] 重试：FAILED → BUILDING（attempt+1，retry_count+1，历史不覆盖）；APPROVED/READY 版本重试 409；审计齐
- [ ] 看门狗：心跳超时 BUILDING → FAILED/STALE；活跃任务唯一性不破坏
- [ ] CI（fake ports）全绿；真实引擎用例本地跑通且在 CI 被 skip

## 5. 测试策略

| 层级 | 内容 | 运行地 |
|---|---|---|
| 单元 | 切块器（纯函数全边界）、规范化、sparse 权重/hash、collection 命名、状态机扩展、降级编排（fake HTTP） | CI |
| 集成 | fake parser/embedding 的 worker 编排全阶段（真实 PG/Redis/Qdrant）；真实 Qdrant 建集合/upsert/对账；快照/重试/看门狗 | CI |
| 本地集成 | 真实 MinerU `/v1/file_parse`（mineru_smoke.pdf 三档）、真实 TEI 嵌入 1024 维 | 本地（探测 skip） |
| 业务场景 | 真实栈：上传 PDF → 审批 → 构建 → READY 全链路 + Qdrant/chunks 对账（TD-046 兑现） | 本地 |
| E2E/前端 | 不做（KB-03B） | — |
| 静态门禁 | ruff / mypy / bandit ×2 / alembic check | 本地+CI |

## 6. 预判技术债

| TD 编号 | 条目 | 处置 |
|---|---|---|
| TD-288（既有） | `BUILD_QUEUED` 无消费者、快照接口缺失 | **本点收口**（取件执行器 + 快照端点） |
| TD-046（既有） | 上线验证必须带一次真实 `/v1/file_parse` | **本点兑现**（业务场景真实解析 + 读数记录） |
| TD-293（新） | DOCX/PPTX 解析路径未接：需 LibreOffice headless 容器（转 PDF → MinerU Standard）+ Flash 原生 Office 回退；本点这类构建任务 PARSING → FAILED `PARSER_UNAVAILABLE`（诚实失败，可重试） | 已采纳 → KB-03 后续点（M3 内）：加容器（compose/部署文档）+ 转换适配 + 质量抽检 |
| TD-294（新） | 切块参数实验未做：临时默认 500/15%/1000（Settings 可配）；300/500/800 × 10/15/20% 的 recall/faithfulness/citation precision 评估 + jieba 替代分词器评估（TD-041 到期） | 已采纳 → evals 批次（M3 内）：评估后回写 design 并调整默认 |
| TD-295（新） | 真实引擎用例在 CI 被 skip：MinerU/TEI 降级链的真实行为只有本地证据（CI fake ports 只测编排） | 已延后 → M6（与 TD-170 全栈入 CI、GPU runner 决策同批） |

## 7. 高风险确认

- **属于高风险功能**：是（状态机扩展、异步 worker、外部引擎集成、Qdrant 租户隔离——红线：collection 命名含 tenant hash + payload 强制租户字段，跨租 collection 一律不触碰）。
- **需要用户确认的点**（推荐项在前，与 §2 编号对应）：
  1. **D1 A/B 两点切分**：本点只做执行管线 + 快照 + 重试；SSE/前端进度/TD-291 归 KB-03B。
  2. **D2 解析格式**：本点 PDF/MD/TXT 真实解析；DOCX/PPTX 构建诚实失败（`PARSER_UNAVAILABLE`），LibreOffice 路径 → TD-293（KB-03 后续点）。
  3. **D3 切块临时默认 500/15%/1000**（Settings 可配），评估实验 → TD-294；jieba 续用（TD-041 评估并入 TD-294）。
  4. **D4 测试策略**：CI fake ports 全绿；真实 MinerU/TEI 用例本地跑（探测 skip）；业务场景仅本地（TD-170 同口径）。
- 其余两项（D5 降级链、D6 worker 形态）推荐直接按 A 执行，有异议请在确认时指出。
- **确认结论**：用户 2026-11-07 问题卡片确认 D1–D4 全按推荐项执行（① A/B 两点切分；② PDF/MD/TXT 先行，DOCX/PPTX 诚实失败 → TD-293；③ 切块可配+临时默认 500/15%/1000 → TD-294；④ fake ports 进 CI + 真实引擎本地跑）。D5/D6 未提异议，按推荐执行。
