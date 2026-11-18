# M3-TD294 设计简报——切块参数实验（检索直评估）

> 依据：`docs/tech-debt.md` TD-294（含 TD-041 分词器归并）+ design §7.2（:922）+ 决策卡 F1-F5（2026-11-17 用户拍板全按推荐）。

## 不做的事

- 不跑完整 ragas faithfulness（生成侧 LLM 链路不存在——AI-01 是 M4 点，harness 无 LlmPort 实现）；生成侧指标随 M4 AI 评估批次补跑，TD-294 台账如实留痕。
- 不判 A12 阈值（85/100/90/95/80 逐指标映射未定义——A12 判定属 AI-01/M4 评估批次）。
- 不动生产代码默认参数（结论若要求调默认 = 独立小 PR 回写 design + Settings）。
- 评估跑批不进 CI（evals/README 口径）。

## 0. 现状（2026-11-17 核实）

- 参数流向：`chunk_markdown(markdown, *, chunk_size, overlap_ratio, parent_size)`（`chunker.py:346`）是**纯函数**；build_task 从 Settings 传入（`build_task.py:637-639`）。网格参数可直调，无需动 Settings/env。
- 检索组件全部可复用：`embed_input_text` / `TEIEmbedder`（真实 TEI，dev 栈 tei-embed）/ `version_sparse_weights`（BM25 IDF）+ `tokenize`（jieba）/ `search_points`（dense+sparse 服务端 RRF）/ `rerank.py`（TEI rerank）。查询侧 sparse = `version_sparse_weights([query])[0]`（检索诊断同款，`retrieval_test.py:394`）。
- 评估集：kb_qa.jsonl 23 条 + citation.jsonl 23 条（expected_citations = `文档名#章节`）；fixtures/sample_kb.md 仅 3.7KB——粒度差异无统计信号。
- A12 阈值 85/100/90/95/80 逐指标映射未定义；rubric 只有「语义分≥1.5」。

## 1. 目标与验收

1. 12 格网格（子块 300/500/800 × 重叠 10/15/20%）+ 父块对照（800/1000/1200 × 最优重叠）在扩大合成语料上的检索直评估报告（`evals/reports/`）。
2. jieba vs pkuseg 同语料 sparse 对照结论；TD-041 关闭或升级。
3. TD-299（测试阈值 100%）复盘数据支撑。
4. design §7.2 回写（结论参数 + faithfulness 补跑登记）。

## 2. 决策卡（2026-11-17 用户拍板，全按推荐）

- **F1 评估形态 → A：检索直评估**（确定性指标，零 LLM 依赖）：recall@k / citation precision@k / MRR / context coverage。faithfulness 随 M4 补跑。
- **F2 语料 → A：扩大合成语料**：30–50KB、10–12 文档（段落/表格/代码混合、长原子表 >500），kb_qa/citation 各扩 ≥30 条；语料生成脚本进 evals（种子可复现，产物 fixtures/corpus/ 入库）。
- **F3 指标与判定**：主指标 recall@5；辅 precision@3 / MRR / coverage；**显著判据 = 组合间正负差 ≥2 个 case**；TD-299 随实验数据复盘。
- **F4 分词器 → A**：jieba vs pkuseg 同框架对照；HanLP（torch 重依赖）初筛排除；无显著优势维持 jieba 关 TD-041。
- **F5 交付形态 → A：evals 包新 runner**；不动生产代码；调默认 = 独立 PR。

## 3. 契约（评估 harness）

- **形态**：in-process 评估 harness（`evals/src/campus_elf_evals/`），直调真实组件（chunker 纯函数 + TEI 真实嵌入 + Qdrant 临时 collection + 生产同款检索函数）；绕过 HTTP/构建编排（上传/审批/任务调度不是实验变量）——简报注记该取舍。
- **corpus.py**：种子化生成器 → `evals/fixtures/corpus/*.md`（入库）；每文档元信息（title/章节清单）与语料同源。
- **cases 扩充**：kb_qa/citation 各新增 ≥7 条对准新语料章节（问题/expected_points/expected_citations 与生成器事实清单对齐）。
- **grid_runner.py**：读网格 → 每格切块（纯函数）→ embed_input_text + TEI 嵌入 → version_sparse_weights → Qdrant 临时 collection（`eval-grid-<cell>` 前缀，跑完删除）→ 每条 case 检索（`search_points` RRF 融合，top_k=8）→ 指标计算 → `evals/reports/td294-grid-<date>.json` + 控制台摘要。
  - rerank 对照组：最优格上跑 TEI rerank（RerankPort）后指标对照。
  - 分词器对照组：tokenize 替换为 pkuseg 切分（依赖加入 evals），重跑最优格 sparse 指标。
- **指标判定**：case 命中 = top-k 中存在块其 `section_path` 归属期望章节且文档标题匹配；coverage = expected_points 文本（关键短语）被 top-8 块文本包含的比例。

## 4. 实现顺序

1. corpus.py + fixtures/corpus 生成入库。
2. cases 扩充（kb_qa/citation ≥30）。
3. metrics.py（recall/precision/MRR/coverage 纯函数 + 单测）。
4. grid_runner.py + rerank/分词器对照组。
5. 跑批 → 报告 → 结论回写（design §7.2 + TD 台账）。

## 5. 测试策略

- metrics 纯函数 vitest 式单测（pytest，evals/tests）。
- corpus 生成器确定性（同种子同产物）单测。
- runner 冒烟：单格 × 3 case 本地跑通（真实 TEI/Qdrant）。
- 全批跑批读数入报告；不进 CI。

## 6. 预判技术债

- TD-294a：context coverage 为字符串包含近似（未做语义判定）——M4 ragas 批次以 faithfulness 替代。
- TD-294b：合成语料与真实校文分布差异——M4 首个真实租户语料批次复核结论。

## 7. 高风险确认

- 无生产代码改动、无租户数据、无迁移；评估 collection 用独立前缀并用后即删（dev 栈）。
- 2026-11-17 用户「确认全按推荐」。

## 8. 实现期偏离登记（2026-11-18；复审二轮 m5 补记——§9 重写时曾误删本节，本段恢复）

1. **语料规模**：70KB（拍板卡 30–50KB）——10 文档 × ~7KB 生成自然落点；规模扩大只增信号不减有效性。
2. **用例交付形态**：独立 `chunk_grid.jsonl`（32 条与语料章节一一对应）而非「kb_qa/citation 各扩 ≥30」——既有两文件绑定 sample_kb 事实且与新语料同题名（奖学金评定办法等）冲突；独立文件避免交叉污染（复审批注）。
3. **collection 前缀**：`eval-td294-*`（§3 契约写 `eval-grid-<cell>`）——含实验名更防撞。
4. **分词器对照口径**：pkuseg/HanLP py3.13/Windows 无法构建（实测，TD-041 台账留痕）→ 降级为 jieba vs 字级 bigram 基线（卡 F4 可行替代口径）。
5. **查询 dense 无标题包裹**（复审 m1 注记）：生产两路检索 query 会用文档标题包裹（test_task.py:307 / retrieval_test.py:309）——评估场景是全库问答、无标题绑定，故用原始问题；取舍注记（简报 §0 只声明了 sparse 查询侧同款，此处补 dense 侧）。
6. **rerank 对照降规模**：16 case 子集双组（CPU rerank 单 case 100-250s——TEI 日志实测，全量双组需小时级）；rerank 报告 `evals/reports/td294-rerank-20261118.json`。

## 9. 实现结果与实验读数（2026-11-18；复审修正版——sparse/原文 + tie-break + 32 case 重跑）

**交付**：`evals/src/campus_elf_evals/`（corpus.py / write_cases.py / metrics.py / grid_runner.py〔rerank 双组显式对照 + --tokenizer + tie-break + created-collection 泄漏防护〕/ tokenizer_compare.py / rerank_subset.py〔CPU rerank 降规模子集口径〕）+ `evals/tests/test_metrics.py`（8 条）+ `evals/fixtures/corpus/`（10 文档 70KB）+ `evals/cases/chunk_grid.jsonl`（32 条）+ `scripts/check_eval_cases.py`（chunk_grid 类别注册，对抗豁免 `-1` 哨兵）。

**读数**（32 case，修正口径：sparse=子块原文〔生产 build_task:712 同源〕、payload/coverage/rerank=原文、tie-break=生产 `_stable_hits` 同款）：

| 组 | 格 | recall@5 | p@3 | mrr | coverage |
|---|---|---|---|---|---|
| 网格 | 300/0.10 | 1.000 | 0.750 | 0.969 | 1.000 |
| 网格 | 300/0.15 | 1.000 | 0.729 | 0.979 | 1.000 |
| 网格 | 300/0.20 | 1.000 | 0.719 | 0.979 | 1.000 |
| 网格 | 500/0.10 | 1.000 | 0.719 | 0.977 | 1.000 |
| 网格 | 500/0.15 | 1.000 | 0.688 | 0.979 | 1.000 |
| 网格 | 500/0.20 | 1.000 | 0.740 | 0.964 | 1.000 |
| 网格 | 800/0.10–0.20 | 1.000 | **0.365–0.375** | 0.953–0.969 | 1.000 |
| 父块 | 500/0.15 × 800/1000/1200 | 1.000 | 0.688–0.698 | 0.969–0.984 | 1.000 |
| 分词器 | jieba vs bigram（500/0.15，32 case 修正口径） | 1.000/1.000 | 0.677/0.6875 | 0.979/0.948 | 1.000 |
| rerank | 500/0.15 无 rerank → TEI rerank（16 case 子集） | 1.000 | 0.688→**0.771** | — | 1.000 |
| rerank | 300/0.10 无 rerank → TEI rerank（16 case 子集） | 1.000 | 0.750→**0.771** | — | 1.000 |

**结论**：
1. recall@5 全格饱和（1.000，32/32）——校务语料上召回对切块参数不敏感（dense 主导 RRF）。
2. p@3 为唯一区分指标：**300 组均值 0.733 ≈ 500 组 0.723（细块无优势）> 800 组 0.372（显著劣化 ≈11 case 当量，远超 ≥2 case 判据）**；重叠比例（格内 spread ≤1.5 case）与父块尺寸影响弱。
3. **参数维持默认 500/15%/1000**（300/500 持平取块数更少 89 vs 118、嵌入/存储成本更低现默认；800 排除）；design §7.2 已回写；state-machines 不变量不变。
4. **rerank 是 p@3 最大杠杆**（16 case 子集双组，零降级，最终重跑读数）：500/0.15 上 0.688→0.771、300/0.10 上 0.750→0.771——**rerank 后两格打平（0.771/0.771）**，粒度差异被 rerank 抹平；mrr 同至 1.000。CPU rerank 单 case 100-250s（TEI 日志实测：推理 12-30s + 排队 10-37s）——全量双组需小时级，故降规模（§8 偏离注记 6）。
5. 分词器（32 case 修正口径复测）：jieba 0.677 vs bigram 0.6875——差 1 hit 当量（噪声水平）持平；pkuseg/HanLP py3.13/Windows 无法构建（实测）→ jieba 维持（TD-041 关闭）。
6. TD-299 复盘：recall 全饱和 → 100% 命中阈值可达，「过严」前提不成立 → 维持 100%（口径注记：实验 recall 为引用级 any-hit@5，run_kb_test 为 chunk-id 全集 ⊆ top-10 更严——TD-294b/M4 复核）。
7. tie-break（生产 `_stable_hits` 同款）后跨轮可复现——首轮的 ±0.011 噪声根因即 Qdrant 并列排序不稳定。
8. faithfulness（生成侧）随 M4 ragas 批次补跑（TD-294a）；合成语料分布差异复核（TD-294b）。
