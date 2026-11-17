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
