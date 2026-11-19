# M4-AI01c 设计简报 — ReAct 路径（LLM 路由/多意图/预算执行）

> 日期：2026-11-20。决策卡五张已经用户拍板（「全按推荐」，见 §2）；默认口径随拍板生效。
> 前置：AI-01a（LlmPort/熔断）与 AI-01b（快路径/工具/审计/prompt 基建）已合并。

---

## 1. 目标与验收

### 1.1 目标

AI-01 的后半：**LLM 路由（多意图识别）→ 有界 ReAct 工具循环 → 多意图合并作答（逐项兜底）**，
统一入口按问题复杂度分派快路径（AI-01b 管道原样复用）或 ReAct。同时收口两项登记债：
**TD-334**（ctx 从 principal 派生）与 **TD-328**（tool_calls 拼装=complete 路径收窄收口）。

**本点范围**：

- `app/harness/router.py`：LLM 路由器（route_v1 分类 prompt → `RouteResult.intents[]`；
  JSON 解析失败/超时 → 单意图 simple 兜底 + warning）
- `app/harness/budget.py`：回合预算计数器（decisions/tools/retrievals/tokens；design §6.2 数值
  Settings 化）
- `app/harness/react_loop.py`：`ReactLoop`（THINK→ACT→OBSERVE 有界循环 → 合并 GENERATE）
- `app/harness/orchestrator.py`：`ChatOrchestrator` 统一入口（发 meta/step(route) → 路由 →
  分派快路径/ReAct 管道）
- prompt 模板：`route_v1.md`（分类）+ `react_v1.md`（工具说明/逐意图小节/引用格式）
- TD-334 落地：`FastPathLoop.run`/`ReactLoop.run` 接收 principal `permissions`，ctx 从实际权限派生
- `campus.navigate` 激活（ReAct 中 LLM 可自主调用）

**本点明确不做**（防蔓延）：

- LangGraph 图执行（卡 1-A：显式状态机，ADR-002 二次注记留痕）
- 流式 tool_calls 拼装（决策步非流式——TD-328 收窄为 complete 路径并收口）
- 写工具/长期记忆/多轮上下文（会话历史随 AI-06/07）
- 性能实测（口径到位，实测随 TD-327 真连同批）

### 1.2 验收编号

| 编号 | 验收内容 |
|---|---|
| M4-AI01c-A1 | 路由：LLM 分类产出 `RouteResult.intents[]`（name ∈ {kb_qa, navigate}）；JSON 解析失败/异常 → 单意图 `simple=true` 兜底（落快路径）+ warning |
| M4-AI01c-A2 | 分派：`simple=true` 单意图 → 快路径管道事件序**逐字节不变**（AI-01b 回归面）；否则 ReAct 路径 |
| M4-AI01c-A3 | ReAct 循环：THINK（complete+tool_specs）→ ACT（ToolExecutor）→ OBSERVE（tool 角色消息回喂）；预算边界 5 决策/3 工具/2 检索/12k token；超限 → 立即带已有证据 GENERATE（降级不报错）+ budget span |
| M4-AI01c-A4 | 多意图合并作答：逐意图小节、引用 [n] 全局编号、某意图零证据 → 该意图兜底小节（逐项兜底，不整题拒答） |
| M4-AI01c-A5 | TD-334：ctx.permissions = principal 实际权限集（run 签名变更，快路径同步）；缺 `campus.nav` 的用户在 ReAct 中调用 navigate → PERMISSION_DENIED |
| M4-AI01c-A6 | TD-328 收口：tool_calls 拼装=complete 路径（SDK 已拼 dict 直用）；design/TD 回写「流式拼装不需要」 |
| M4-AI01c-A7 | navigate 激活：入口类意图 → navigate 工具 → SSE 回答含 route/label；快路径行为不变 |

### 1.3 依赖与现状（事实核实 2026-11-20）

- **已就位（零新建）**：`LlmRequest.tool_specs` 透传 + `LlmResponse.tool_calls`（complete 路径
  SDK 已拼）；三工具注册（navigate 休眠）；`TurnToolState` 白名单；trace `intents[]`/
  `route_path` 字段；AgentEvent 九类；`build_turn_tools`/executor/audit 全套
- **缺口（本点新建）**：ReactLoop/预算计数器/路由器/orchestrator/两个 prompt 模板/TD-334 签名变更
- design §6.2 预算数值：`MAX_MODEL_DECISIONS=5、MAX_TOOL_CALLS=3、MAX_RETRIEVALS=2、
  TURN_TIMEOUT=30s、TOKEN_BUDGET=12000`
- requirements :108：ReAct 首个 SSE 状态事件 P95≤3s、首答案 token P95≤5s（口径到位，实测
  随 TD-327）

---

## 2. 决策卡拍板记录（2026-11-20，用户「全按推荐」）

| 卡 | 结论 |
|---|---|
| 卡 1 编排形态 | **A：统一入口 ChatOrchestrator（路由→分派）**；显式状态机，不引入 LangGraph |
| 卡 2 路由 | **A：专用分类 prompt（JSON intents[]），失败单意图兜底** |
| 卡 3 工具选择 | **A：function calling（tool_specs 透传 + complete 决策步）**；TD-328 收窄收口 |
| 卡 4 合并作答 | **A：逐意图检索→证据合并去重→单次 GENERATE（逐意图小节/全局编号）** |
| 卡 5 预算耗尽 | **A：带已有证据降级 GENERATE，不报错** |

默认口径（已生效）：TD-334 ctx 派生；预算常量 Settings 化；THINK/ACT 用 step+reasoning_summary
表达；prompt 新增 route_v1/react_v1；性能实测随 TD-327；BS-M4-03 真实栈；read_chunk 白名单
不变。

---

## 3. 子决策 D1–D5（随简报确认生效）

| # | 决策 | 结论 | 备选与否决理由 |
|---|---|---|---|
| D1 | 结构分派 | `ChatOrchestrator` 持 `FastPathLoop` + `ReactLoop` + 路由器；orchestrator 发 meta/step(route)（首事件立出，路由耗时不阻塞首事件），路由结果作参数传入对应 loop（loop 跳过自身 route 段） | loop 内自带路由会造成 meta 前静默期，违反首状态 P95 |
| D2 | 路由降级 | JSON 解析失败/路由 LLM 异常 → 单意图 `simple=true` + warning（落快路径；用户始终有回答） | 路由失败报错=体验灾难 |
| D3 | 意图名集合 | MVP `{kb_qa, navigate}`；未知意图名按 kb_qa 处理（诚实降级） | 更多意图随能力扩展 |
| D4 | 决策步流式 | 决策步一律非流式 `complete()`（tool_calls 完整返回）；仅最终 GENERATE 流式 | 流式 tool_calls 拼装无消费方（TD-328 收窄） |
| D5 | 超预算 trace | budget 状态落 budget span（attributes：decisions/tools/retrievals/tokens 计数），不进 error_code | budget 不是错误 |

---

## 4. 事件序与 trace

```text
simple=true（快路径，AI-01b 事件序逐字节不变）：
  meta → step(route) → [AI-01b 全部]

ReAct（route_path="react"）：
  meta → step(route) → reasoning_summary(「识别到 N 个问题」)
  → [step(think) → tool_call → tool_result → reasoning_summary(观察摘要)]*   ← 有界循环
  → step(generate) → delta* → citation* → done
  （某意图零证据：delta 内该意图小节为兜底话术，不产生该意图 citation）

trace：route_path="react"、intents=路由意图名列表、budget span（attributes 计数）、
  prompt 三元组（react_v1）、失败弧 error_code 沿用既有词表
```

- 路由段预算：路由用独立一次 complete（不计入 5 决策——决策预算只管 ReAct 循环；
  路由失败走 D2 降级）。
- `TURN_TIMEOUT=30s` 覆盖整个 ReAct 循环（含决策步）；GENERATE 首 token 后按客户端节奏
  （AI-01b 口径延续）。

---

## 5. 实现顺序（TDD 红→绿）

| 步 | 内容 | 测试 |
|---|---|---|
| 1 | `budget.py` 预算计数器（计数/超限判定/settings 化） | 单测 |
| 2 | `router.py` 路由器（prompt 渲染+JSON 解析+失败兜底+意图白名单） | 单测（FakeLlm 脚本） |
| 3 | `react_loop.py` 循环（THINK→ACT→OBSERVE 全弧/预算降级/逐意图兜底/白名单反馈/navigate） | 单测（FakeLlm 脚本化 tool_calls） |
| 4 | `orchestrator.py` 分派 + TD-334 签名变更（快路径同步） | 单测（分派矩阵/事件序回归） |
| 5 | 装配（chat_assembly 换 orchestrator）+ `campus_chat.py` 端点接线（permissions 透传） | SSE 端点测试更新 |
| 6 | 业务场景 BS-M4-03（真实栈：多意图分段作答+引用；预算降级；navigate 入口） | 真实 PG+Qdrant+TEI+FakeLlm |

## 6. 测试策略

- **单元**：budget 全弧、router 解析矩阵（合法/非法 JSON/意图越集/异常）、ReactLoop
  循环弧（含预算降级/逐意图兜底/白名单反馈/navigate/orchestrator 分派矩阵）、快路径
  事件序回归（simple 路径逐字节不变）。
- **集成**：SSE 端点（ReAct 路径全流、TD-334 权限派生矩阵）。
- **业务场景**：BS-M4-03（真实栈：seed 发布 KB → 多意图提问〔「差旅报销怎么办理？另外请假入口在哪？」〕
  → 事件序/分段作答/全局引用/navigate route；预算降级弧；TD-334 缺 nav 权限拒绝）。
- **门禁**：ruff / mypy / bandit×2 / alembic check（零迁移）+ 受影响子集。

## 7. 预判技术债（实现 PR 登记）

| 预判 TD | 内容 | 处置方向 |
|---|---|---|
| TD-P1 | 路由 LLM 调用增加快路径首事件延迟（多一跳；meta 先出缓解） | TD-327 真连同批压测核对 P95≤3s |
| TD-P2 | function calling 依赖 provider 支持；不支持时 ReAct 不可用 | 路由/决策异常降级快路径 + warning；TD-327 真连验证 |
| TD-P3 | 多意图共享预算可能饿死低置信意图 | 按路由置信度顺序消费预算；压测复核 |

## 8. 对 spec/design 的回写清单（随实现 PR）

| 文档 | 变更 |
|---|---|
| design §6.2 | ReAct 落地口径（orchestrator 分派/预算 Settings 化/超预算降级） |
| design §6.3 | navigate 激活注记 + TD-328 收口（拼装=complete 路径） |
| tech-debt | TD-328/334 改已完成；TD-P1/P2/P3 登记 |
| requirements §5.1 | ReAct 落地注记（同 AI-01b MVP 边界格式） |

## 9. 高风险确认

AI 域功能点（LLM 自主选工具=AI 护栏敏感面），按 §11.7 从严：**简报经用户确认后再编码**。
新增待确认项＝§3 D1–D5 与 §4 事件序（决策卡五张已拍板）。

---

## §10 实现注记（编码后回填）

### 10.1 落地文件

- `app/harness/budget.py`：`TurnBudget`（四维计数/上限校验/snapshot）
- `app/harness/router.py`：`LlmRouter` + `RouterOutcome`（route_v1 分类/JSON 解析矩阵/D2 降级/D3 意图白名单/simple 判定由路由器计算）
- `app/harness/react_loop.py`：`ReactLoop`（THINK→ACT→OBSERVE 有界循环 + navigate 入口证据 + 合并 GENERATE + budget span + 超时/取消/异常弧对齐 AI-01b）
- `app/harness/orchestrator.py`：`ChatOrchestrator`（meta/step(route) 首事件立出 + 路由分派 + TD-334 permissions 必传）
- prompt：`route_v1.md` ×1 + `react_think_v1.md` ×1（**新增第三模板**——THINK 指令需独立版本化，偏离 §1「两个模板」登记于此）+ `react_v1.md`
- `app/harness/chat_tools.py`：`openai_tool_specs`（registry→tools 参数转换）
- `app/harness/loop.py`：`format_evidence` 提取公共函数（react_loop 同源）+ FastPathLoop.run 签名变更（permissions/skip_route_stage/trace_id——TD-334/D1）
- `app/harness/chat_assembly.py`：装配换 `ChatOrchestrator`（预算 Settings 化传递）
- 测试：budget 7 / router 7 / react_loop 9 / orchestrator 4 / BS-M4-03（1，真实栈多意图全链）

### 10.2 与简报的偏离（全登记）

1. **prompt 三模板而非两个**（§1 写 route_v1+react_v1）：THINK 决策指令需独立版本化文件（`react_think_v1.md`）——提示词不得硬编码（AGENTS §5），多一个模板是纪律要求。
2. **OBSERVE 用文本回喂而非 tool 协议**（D3 细化）：`LlmMessage` 无 tool_call_id 字段；文本 `[观察]` 消息 provider 无关且不扩契约。
3. **工具预算耗尽即退出循环**：工具不可用后继续决策无意义——`can_act()` 为假直接 GENERATE（比「仅标记」更强的降级）。

### 10.3 门禁读数（2026-11-20，终态）

- `ruff check app tests alembic`：0 错误；`mypy --no-incremental app tests`（307 文件）0 错误
- `bandit -r app -x tests`：0 issue；`alembic check`：无漂移（零迁移）
- 受影响全子集 **232 passed**（终态，含审查修复新增：router None 弧 1 + PERMISSION_DENIED 锚定；harness 全部 + chat_retrieval 单元/集成 + audit_db + SSE 端点 + config + 装配 + chat_rbac + bs_m0_01 + sql_baseline + BS-M4-01/02/03）

### 10.4 审查处置（2026-11-20，独立审查第 1 轮：0B/1M/7m）

- **M1（TD-334 纪律性收口→签名强制）**：orchestrator.run `permissions` 去默认值（必传，漏传即类型错误）；docstring 明确「漏传即静默越权」；TD-334 处置列补「loop 层 None 兜底仅 standalone/测试专用，待评估移除」。
- **m1** requirements §5.1 补 AI-01c 落地注记 + design §6.3 注记更新（navigate 已激活/TD-328 收口/campus.kb.qa 延 AI-04）；**m2** TD-338（token 预算只观测语义，压测批）；**m3** 补 router None llm_port 单测；**m4** 权限拒绝断言锚定 PERMISSION_DENIED（trace span）；**m5** TD-339（THINK 消息历史形态，真连批验证）；**m6** TD-340（ReAct 超时含背压口径，压测批统一）；**m7** while-else 死代码删除 + import re 顶置 + SSE 测试 docstring 修正。
- 复测终态：router/react/orchestrator **21 passed**；受影响全子集 **232 passed**；ruff/mypy 0。

### 10.5 判例沉淀

1. **「统一入口」的公共 API 是权限收口的真正战场**：loop 层收口只改了内部构造，orchestrator.run 的可选默认值才是漏传入口——签名强制（去默认值）才是结构性收口（审查 M1）。
2. **SSE tool_result 刻意信息最小化**（无 error_code）——精确错误码断言要走 trace span/审计 metadata，不能只在 SSE 面断言（审查 m4）。
3. **多意图预算分配顺序**（卡 5-A 延伸）：预算按路由顺序消费，低置信意图可能饿死——TD-337 已登记压测复核。
