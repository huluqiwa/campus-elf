# M4-AI04 设计简报 — 引用语义校验 + citation 被引子集 + 无效引用事件

> 日期：2026-11-22。决策卡六张已经用户拍板（「全部按推荐」，见 §2）。
> 前置：AI-01a/b/c + AI-09 + AI-06/07 已合并（main `392c7a1`）。

---

## 1. 目标与验收

### 1.1 目标

spec AI-04 三条验收中「存在性/归属/超范围」已由 RETRIEVE 证据回查与哨兵
output.ref_range 覆盖（TD-332）；本点收口剩余两个缺口：

1. **语义相关性校验**：引用 `[n]` 存在且在范围内，但答案内容与被引证据语义无关
   （幻觉引用）→ citation 事件过滤 + 事件记录（spec「与答案相关」、design §907/981）。
2. **citation 被引子集**：两个 loop 现在全量发证据 citation（不管答案是否引用）；
   改为只发答案正文实际引用（`[n]`）且通过语义校验的条目。

**时序事实（方案形态的根据）**：delta 逐段发出不可撤回（TD-332 口径），但
citation 事件在流结束后、DONE 之前才发——此时全文已生成完毕，全文校验可行；
正文残留 `[n]` 标记为可接受降级（前端展示以 citation 事件为准，降级显示归
C 端聊天 UI 点）。

**本点明确不做**：检索分数阈值（卡 5 → 压测批 TD-327）；`campus.kb.qa` 独立
权限码（卡 6 → 继续延后 C 端聊天 UI 点，偏离 design §878 原文经用户拍板）；
前端「点击查看」UI；Ragas 评估。

### 1.2 验收编号

| 编号 | 验收内容 |
|---|---|
| M4-AI04-A1 | 语义校验：答案全文 vs 每个被引证据嵌入余弦相似度 ≥ 阈值 → 保留；低于阈值 → 从 citation 事件剔除 |
| M4-AI04-A2 | citation 被引子集：citation 事件只含答案正文引用的 `[n]`（checker=None 时仅按正文解析；checker 注入时再过语义校验）；正文零引用 → 零 citation 事件 |
| M4-AI04-A3 | 无效引用事件：被剔除的引用写 guard_events（rule=`output.ref_irrelevant`，direction=output，action=redact，severity=low，reason 只含序号与相似度数值）+ `campus_guard_hits_total` 计数 |
| M4-AI04-A4 | fail-open：校验环节本身故障（embedder 异常）→ 保留被引 citation + warning 日志，不中断回合（质量检查不阻塞——M6 口径） |
| M4-AI04-A5 | 阈值 Settings 化（`kb_ref_min_similarity`，默认 0.4）；两条路径（fast/react）同一 checker 与同一阈值 |
| M4-AI04-A6 | trace：VERIFY 阶段 span 记录 {checked, removed, min_similarity}（数值，无 PII） |
| M4-AI04-A7 | BS-M4-06（真实栈）：有效引用全保留 / 幻觉引用被剔除+留痕 / 零证据兜底回归 |

### 1.3 依赖与现状

- `ChatEvidence`（chunk_id/score/text/doc_title/section_path/page_start/page_end/
  chunk_type/version_id）是 citation 与 GENERATE 上下文的单一事实源
- `TEIEmbedder.embed(texts) -> list[list[float]]` 已在栈内（检索查询侧同款）
- `GuardEventsDbSink.append(tenant_id, trace_id, direction, results)` + flush 管道
  已有（AI-09）；`record_guard_hit(rule, severity, direction)` 已有
- 两个 loop 的 citation 段同构（全量 `enumerate(evidence)`）；均走 StreamSentinel
- 受影响既有断言：`test_fast_path_loop.py:184 [1,2]`（答案引用全部证据，保持绿）；
  `test_bs_m4_03_react.py:340 [1,2,3]`（答案只引 [1][2]，**[3] 业务入口未引用 →
  将红**，修法：脚本化答案补 `[3]` 引用，保持「KB+入口全局编号」断言意图）

---

## 2. 决策卡拍板记录（2026-11-22，用户「全部按推荐」）

| 卡 | 结论 |
|---|---|
| 卡 1 范围 | **A：语义校验 + citation 被引子集 + 事件记录** |
| 卡 2 判定方式 | **A：嵌入相似度**（TEI 批量一次请求，余弦，阈值 Settings 化） |
| 卡 3 失败动作 | **A：citation 过滤 + 事件记录**（正文残留 `[n]` 可接受；不追加更正 delta；非影子模式） |
| 卡 4 事件落点 | **A：复用 guard_events**（rule=`output.ref_irrelevant`，severity=low） |
| 卡 5 检索分数阈值 | **不做硬阈值**（零命中/人群空集维持「无证据」口径；登记 TD 随压测批） |
| 卡 6 `campus.kb.qa` 权限码 | **继续延后 C 端聊天 UI 点**（偏离 design §878「延后 AI-04」原文，经用户拍板） |

---

## 3. 子决策 D1–D6

| # | 决策 | 结论 |
|---|---|---|
| D1 | checker 注入形态 | `RefSemanticChecker` 独立类，chat_assembly 构建注入两个 loop；`None` = 未启用（仅正文解析被引子集）——既有测试与降级路径零破坏 |
| D2 | 判定粒度 | **答案全文 vs 逐条被引证据**（不逐句）；全文累计 = 实际发出的 delta 文本（含哨兵替换文本） |
| D3 | 被引解析 | `REF_MARKER`（`\[(\d+)\]`）扫全文，去重升序；`n > evidence_count` 已被哨兵剔除，此处不重复处理 |
| D4 | 嵌入调用 | 一次批量 `embed([answer, *cited_chunk_texts])`；余弦相似度；**embedder 异常 → fail-open**（A4） |
| D5 | 阈值 | `kb_ref_min_similarity: float = 0.4`（BGE-M3 余弦初值；**未经真实分布校准**——压测批实测校准，预判 TD-P1）；相关判定 = `sim >= threshold` |
| D6 | VERIFY span | 复用 stream_stage_span 形态新增 `verify` span（attributes={checked, removed, min_similarity}，数值型） |

---

## 4. 实现设计

### 4.1 新模块 `app/harness/ref_semantics.py`

```python
class RefVerdict:      # index / chunk_id / similarity / relevant
class RefCheckResult:  # cited: list[int] / verdicts / removed / checked: bool
class RefSemanticChecker:
    def __init__(self, *, embedder: EmbedPort, min_similarity: float) -> None
    async def check(self, answer_text: str, evidence: Sequence[ChatEvidence]) -> RefCheckResult
```

- `EmbedPort` 协议：`embed(texts) -> list[list[float]]`（TEIEmbedder 天然满足；
  测试用 FakeEmbedder 返回可控向量）
- 纯解析路径：`parse_cited(answer_text, evidence_count) -> list[int]`（checker=None
  时两个 loop 直接用）

### 4.2 两个 loop 接线（对称）

1. 构造参数加 `ref_checker: RefSemanticChecker | None = None`
2. delta 路径累积答案全文（含哨兵替换文本 + tail 文本 = 用户实际所见）
3. 流结束、发 citation 前：
   - `ref_check = await checker.check(text, evidence)`（checker 有）/ `parse_cited`（无）
   - **fail-open**：check 抛异常 → warning + 按 parse_cited 结果发（不剔除）
4. citation 事件只发最终保留集（升序）
5. removed 非空 → `guard_events_sink.append(...)` + `record_guard_hit` + verify span

### 4.3 Settings

`kb_ref_min_similarity: float = 0.4`（0~1 校验）；chat_assembly 传入。

---

## 5. 实现顺序（TDD 红→绿）

| 步 | 内容 | 测试 |
|---|---|---|
| 1 | `ref_semantics.py`（parse_cited + RefSemanticChecker + 余弦） | 单测（FakeEmbedder：同向量=1.0 保留/正交=剔除/阈值边界/fail-open） |
| 2 | Settings + chat_assembly 注入 | 装配断言 |
| 3 | 快路径接线（全文累计 + citation 过滤 + 事件） | 单测（stub checker 三弧：保留/剔除/None 解析模式） |
| 4 | ReAct 接线（同构） | 单测 + 修 `test_bs_m4_03` 脚本答案补 `[3]` |
| 5 | BS-M4-06（真实栈三弧） | 真实 PG+Qdrant+TEI+FakeLlm |
| 6 | 门禁 + 独立审查 | ruff/mypy(app+tests)/bandit×2/alembic check |

## 6. 测试策略

- **单元**：parse_cited / 判定纯函数（FakeEmbedder）/ 两 loop citation 过滤弧 /
  fail-open / guard_events 缓冲计数 / verify span attributes
- **集成**：真实 TEI 相关/无关样本判定（校准观测，不锁死分布）
- **业务场景**：BS-M4-06 三弧（有效全保留 / 幻觉剔除+guard_events 行+metrics /
  零证据兜底回归）
- **回归**：test_fast_path_loop citation 断言（答案引全部证据 → 应保持绿）；
  受影响子集全量
- **门禁**：ruff / mypy app+tests / bandit×2 / alembic check（零迁移）

## 7. 预判技术债

| 预判 TD | 内容 | 处置方向 |
|---|---|---|
| TD-P1 | 阈值 0.4 为经验初值，未经真实问答分布校准（误杀/漏放风险） | 压测批 TD-327 实测校准 + Ragas 批观测 |
| TD-P2 | 正文残留 `[n]` 标记的降级显示口径 | C 端聊天 UI 点（前端以 citation 事件为准渲染） |
| TD-P3 | 检索低分命中无阈值（卡 5） | 压测批实测定 |
| TD-P4 | `campus.kb.qa` 独立权限码继续延后 | C 端聊天 UI 点 |

## 8. 文档回写清单

design §6.2 VERIFY 段实现注记 + §7.5 引用校验口径更新（语义维度落地）/
§878 campus.kb.qa 延后口径改写 / tech-debt TD-332 收口改写（语义维度完成）/
requirements AI-04 注记 / milestone AI-04 过账（里程碑边界评审时）。

## 9. 高风险确认

引用校验触及 AI 输出改写（citation 剔除），按 §11.7 从严：**简报经用户确认后
再编码**；判定本身 fail-open 不阻断回答（质量检查不阻塞——M6 口径延续）。

---

## §10 实现注记（编码后回填）

### 10.1 落地文件

- `app/harness/ref_semantics.py`（新增）：EmbedPort 协议 / parse_cited / cosine_similarity /
  RefVerdict / RefCheckResult / RefSemanticChecker（含 **nav: 豁免**）+ `REF_IRRELEVANT_RULE`
- `app/harness/loop.py`：构造器 ref_checker 参数；答案全文累计（正常分片+哨兵替换+尾片）；
  `_resolve_citations`（fail-open）；citation 被引子集 + verify span + guard 事件
- `app/harness/react_loop.py`：同构接线
- `app/harness/chat_assembly.py`：RefSemanticChecker 构建（TEI embedder + Settings 阈值）注入两 loop
- `app/config.py`：`kb_ref_min_similarity`
- 测试：`tests/harness/test_ref_semantics.py`（9）+ test_fast_path_loop/test_react_loop 追加弧（4）+
  `tests/business/test_bs_m4_06_ref_semantics.py`（真实栈三弧）

### 10.2 与简报的偏离（全登记）

1. **阈值 0.4 → 0.7（真实 TEI 探针校准，2026-11-22）**：BGE-M3 对任意中文句对的余弦
   基线很高——实测相关对 0.82~0.85、无关对 0.56~0.68、同域不同主题 0.64~0.68，**0.4
   初值下全部配对 ≥0.4，校验完全无效**。0.7 为探针分界值；D5 的「未经校准」预判
   （TD-P1）兑现为本次实测校准，压测批复核保留。
2. **nav: 入口证据豁免语义校验**（简报未预期）：`chunk_id` 以 `nav:` 开头的业务入口
   证据是 ReAct 决策器**主动获取**的确定性证据（非检索相关性产物），文本为
   label:route 短标签——语义校验对它必然误杀（业务入口引用是 AI-01c 核心 UX）；
   引用即视为相关（sim=1.0），不参与嵌入批次。
3. **guard_events 落库列 = `excerpt_redacted`**（非 reason）：GuardResult.reason 经
   GuardEventsDbSink 映射到脱敏摘录列（AI-09 既有管道）。
4. **BS-M4-06 弧 3 从「零证据兜底」改为「全部引用失效」**：两块迷你语料对任意 query
   必有命中（无分数阈值——卡 5 口径），「不相关 query → 零命中」不可构造；改为断言
   答案与两块全无关时 citation 面为空 + 两条 guard 行（A2 边界）。零证据兜底回归由
   BS-M4-02 ACL 弧背书。
5. **既有测试修正三处（行为变更连带）**：test_orchestrator 脚本答案「答。」→「答 [1]。」
   （citation 事件需正文引用）；BS-M4-01 答案补 `[2]`（招待费句——与脚本断言 [1,2]
   对齐且语义真实）；BS-M4-03 答案补 `[3]`（办理入口句——原答案引用 [2] 表达「请假
   入口」与招待块语义错位）。

### 10.3 门禁读数（2026-11-22）

- `ruff` 0 / `mypy` app+tests 327 文件 0 / `bandit`×2 EXIT=0 / `alembic check` 无漂移（零迁移）
- 受影响子集（harness 全量 + business 全量 + baseline + bare SQL 守卫 + rbac +
  conversation_api）：终值见提交信息（全绿为合并前提）
- 真实 TEI 探针（校准依据）：相关对 0.8199/0.8440/0.8505；无关对 0.5568/0.6428/0.6760

### 10.4 审查处置

（待独立审查后回填。）
