# M4-AI09 设计简报 — 输入/输出护栏全量（七类红线）

> 日期：2026-11-20。决策卡五张已经用户拍板（「全按推荐」，见 §2）；默认口径随拍板生效。
> 前置：AI-01a/b/c 已合并（loop/orchestrator/工具/审计/prompt 基建齐全）。

---

## 1. 目标与验收

### 1.1 目标

AI-09 全量落地：**输入护栏**（注入特征/编码/内容安全）+ **输出护栏**（A7 七类红线逐类
检测与处置），双层落地（系统提示词层已有 + 输出规则校验层本点补齐）；`guard_events`
表落库 + Prometheus 指标。**TD-332 正式收口**（哨兵缓冲使引用改写可行）。

**本点范围**：

- `app/harness/guards/`：护栏规则引擎包——`input.py`（输入检测器）+ `output.py`
  （输出七类检测器）+ `rules.py`（规则表/模式库，纯函数逐类独立可测）
- **分块哨兵缓冲**（卡 1-A）：`harness/guards/stream_guard.py`——GENERATE 流式滚动
  缓冲（按标点/长度切片），切片先校验后发出；命中红线 → 替换安全话术 + GUARD_BLOCK
- `guard_events` 表迁移（design §4.4 字段）+ `harness/guard_events_db.py` 落库适配
  （DbAuditSink 同款缓冲+flush）
- Prometheus 指标：`campus_guard_hits_total{rule,severity,direction}`
- loop 接线：FastPathLoop/ReactLoop 的 GENERATE 段接入哨兵缓冲；输入轻检升级为
  输入护栏；VERIFY 从「告警不改写」升级为「改写剔除」（TD-332 收口）
- 安全话术常量集中（`guards/scripts.py`）

**本点明确不做**：外部分类 API（A24 可选，归后续）；AI-05 反馈；会话持久化（AI-06/07）；
多轮上下文；`chat_traces` 落库（TD-330 不变）。

### 1.2 验收编号

| 编号 | 验收内容 |
|---|---|
| M4-AI09-A1 | 输入护栏：注入特征/违法内容 → block（refusal 话术 + guard_event）；自伤输入 → 支持性话术路径；正常含 PII 输入放行（卡 4-A） |
| M4-AI09-A2 | 输出七类逐类检测：①跨租/他人 PII 特征 ②系统提示词/CoT 泄露 ③工具参数/内网地址/密钥 ④XSS/注入载荷 ⑤伪造引用（超范围序号剔除——改写）⑥违法关键词 ⑦自伤不当应答（支持性话术替换） |
| M4-AI09-A3 | 哨兵缓冲：切片先校验后发出；跨切片拼接威胁（如 `[1`+`](javascript:...)` 分块）被缓冲覆盖；TTFT 不受影响（首切片校验为微秒级规则匹配） |
| M4-AI09-A4 | 命中处置：redact（替换/剔除）或 block（终止 + GUARD_BLOCK stop_reason + error/delta 话术）；guard_events 落库（direction/rule/severity/action/excerpt_redacted——摘要脱敏不含原始命中全文） |
| M4-AI09-A5 | 指标：`campus_guard_hits_total` 计数（rule/severity/direction 标签；私有 Registry 口径与既有 metrics 一致） |
| M4-AI09-A6 | prompt 三模板红线段与检测器规则**同源对照**（模板承诺的 = 检测器执行的，防漂移） |

### 1.3 依赖与现状（事实核实 2026-11-20）

- **`guard_events` 表不存在**（design §4.4 有设计：id/tenant_id/trace_id/direction(input/output)/
  rule/severity/action/excerpt_redacted/created_at）——本点唯一迁移
- `GuardResult` 契约已定义（contracts.py:148，零消费方）；`harness/audit.py` 的 `redact_mapping`
  可复用脱敏
- 输出侧现状：VERIFY 仅引用序号告警不改写（TD-332）；prompt 三模板红线段已有（A6 的
  模板侧基线）
- metrics 基建：私有 CollectorRegistry/固定标签集口径（design §6.6 P0 注记）
- 事实核实另证：`chat_traces`/`llm_usage` 表亦不存在（TD-330 口径不变，本点不碰）

---

## 2. 决策卡拍板记录（2026-11-20，用户「全按推荐」）

| 卡 | 结论 |
|---|---|
| 卡 1 流式策略 | **A：分块哨兵缓冲**（滚动缓冲按标点/长度切片，先校验后发出；命中→替换话术+GUARD_BLOCK） |
| 卡 2 检测形态 | **A：本地规则引擎**（纯函数逐类检测器；外部分类 API 归后续/A24 可选项） |
| 卡 3 事件落库 | **A：guard_events 表迁移**（本点唯一迁移；DbAuditSink 同款缓冲+flush） |
| 卡 4 输入 PII | **A：输入 PII 不拦截**（用户自身 PII 属正常输入）；注入/违法 → block；自伤 → 支持性话术 |
| 卡 5 覆盖范围 | **A：七类全量**（伪造引用升级为改写剔除——TD-332 正式收口） |

默认口径（已生效）：Prometheus `campus_guard_hits_total`（rule/severity/direction）；
话术常量集中管理；BS-M4-04 真实栈四弧；AI-05/AI-06/07 顺延不变。

---

## 3. 子决策 D1–D5（随简报确认生效）

| # | 决策 | 结论 | 备选与否决理由 |
|---|---|---|---|
| D1 | 缓冲切片粒度 | 按句末标点（。！？；\n）+ 最长 120 字符强制切片；尾片在 finish 时冲刷校验 | 太细→碎片误切；太粗→改写窗口变大 |
| D2 | 检测器接口 | `detect(text) -> list[GuardResult]` 纯函数（无 IO/无状态）；loop 侧聚合决策（第一个高危命中即动作） | 检测器不做处置决策（处置归 loop），职责单一 |
| D3 | excerpt_redacted 口径 | 命中片段前后各 20 字符 + 命中本体以 `[REDACTED:<rule>]` 占位——guard_events 不存原始攻击载荷全文 | 事件表本身成为泄露面的风险归零 |
| D4 | 规则模式来源 | 规则表内置于 `rules.py`（模式/关键词清单常量 + 注释标注来源）；不引外部规则文件（避免运行时文件依赖） | 规则文件热加载归后续（YAGNI） |
| D5 | 自伤话术路径 | 输出命中自伤类 → **整段替换**为支持性话术（非局部 redact）+ guard_event（severity=high）；输入自伤 → 同话术 + 放行检索（资料可能有用） | 自伤场景宁枉勿纵，A24 口径 |

---

## 4. 七类红线 → 检测器映射（卡 5-A 细化）

| 类 | 检测方式（输出侧） | 动作 |
|---|---|---|
| ①跨租/他人 PII | 输出含他租/他人标识特征（其他用户的姓名/手机号/身份证模式 + 非当前用户上下文标记） | redact 片段 |
| ②系统提示词/CoT | 输出含系统提示词标记片段（「你是「校园陪伴精灵」」起句/规则标题/模板红线原文）或 CoT 标记（「我的思考过程/内部指令」） | block 整段→安全话术 |
| ③工具参数/内网地址/密钥 | 输出含内网地址模式（127.0.0.1/10.0./192.168./内网域名）、密钥赋值形态（api_key=/Bearer 长串）、工具调用 JSON 形态 | redact 片段 |
| ④XSS/注入载荷 | `<script`/`javascript:`/`onerror=`/事件属性/SQL 注释形态 | redact 片段 |
| ⑤伪造引用 | `\[\d+\]` 序号 > 证据数（哨兵缓冲内已有上下文计数） | 剔除该标记（redact） |
| ⑥违法内容 | 关键词清单（犯罪指导/暴恐/色情等，rules.py 常量） | block 整段→安全话术 |
| ⑦自伤不当应答 | 自伤关键词上下文 + 非支持性应答特征（诊断/评判/鼓励性表述）；命中即整段替换支持性话术（D5） | block→支持性话术（不改检索） |

输入侧：注入特征（忽略既往指令/角色扮演诱导/分隔符注入）、违法输入 → block；自伤输入 →
支持性话术 + 放行；其余放行（卡 4-A）。

---

## 5. 实现顺序（TDD 红→绿）

| 步 | 内容 | 测试 |
|---|---|---|
| 1 | `guards/rules.py` 规则表 + `guards/input.py` 输入检测器 | 单测（注入/违法/自伤/放行矩阵） |
| 2 | `guards/output.py` 七类输出检测器 | 单测（逐类正例/反例/边界） |
| 3 | `guards/stream_guard.py` 哨兵缓冲（切片/校验/改写/终止） | 单测（跨切片拼接威胁/切片边界/finish 冲刷） |
| 4 | `guard_events` 迁移 + `guard_events_db.py` 落库适配 | 迁移往返 + 集成（库内真实行） |
| 5 | loop 接线：输入护栏（两 loop）+ GENERATE 哨兵缓冲 + VERIFY 升级改写 + 指标 | 单测（loop 弧：block/redact/剔除）+ 指标断言 |
| 6 | 业务场景 BS-M4-04（真实栈：注入输入 block/XSS 输出改写/自伤支持性话术/伪造引用剔除 + guard_events 落库断言） | 真实 PG+Qdrant+TEI+FakeLlm |

## 6. 测试策略

- **单元**：逐类检测器正例/反例/边界（约 40+ 用例）；哨兵缓冲切片/拼接威胁/冲刷；
  guard_events 落库。
- **集成**：迁移往返；loop 弧（block/redact 改写后事件序与 trace）。
- **业务场景**：BS-M4-04 真实栈四弧 + guard_events 库内断言。
- **门禁**：ruff / mypy / bandit×2 / alembic check（有迁移→往返实测）+ 受影响子集。

## 7. 预判技术债（实现 PR 登记）

| 预判 TD | 内容 | 处置方向 |
|---|---|---|
| TD-P1 | 规则引擎误报/漏报率未知（无标注语料） | AI 评估批（Ragas 同批）建护栏回归集，误报样本回灌 rules.py |
| TD-P2 | 哨兵缓冲切片边界与 markdown 语法交互（表格行/代码块内标点切片可能破坏结构） | 实现时以 finish 冲刷兜底；回归集验证 |
| TD-P3 | 外部内容安全 API（A24 可选）未接 | 归二期/运营批 |

## 8. 对 spec/design 的回写清单（随实现 PR）

| 文档 | 变更 |
|---|---|
| design §6.5 | 七类→检测器映射表、哨兵缓冲口径、guard_events 落库/指标注记 |
| design §6.2 | VERIFY 段更新（告警→改写剔除，TD-332 收口） |
| tech-debt | TD-332 改已完成；TD-338 口径联动核对；TD-P1/P2/P3 登记 |
| requirements 附录 A A7 | 「已确认」补落地注记（检测器逐类映射） |

## 9. 高风险确认

AI 护栏为本项目最高风险类别（AGENTS §11.7 明列）：**简报经用户确认后再编码**。
新增待确认项＝§3 D1–D5 与 §4 映射表（决策卡五张已拍板）。

---

## §10 实现注记（编码后回填）

### 10.1 落地文件

- `app/harness/guards/rules.py`：注入/违法/自伤输入模式 + 提示词泄露/密钥/内网地址/XSS/身份证/伪造引用输出模式（常量内置——D4）
- `app/harness/guards/input.py`：`detect_input`（注入/违法 block、自伤标记、自身 PII 放行——卡 4-A）
- `app/harness/guards/output.py`：`scan_output`（七类 + OutputScan 含 blocking/redacted_text——D3）
- `app/harness/guards/stream_guard.py`：`StreamSentinel`（滚动缓冲句末标点+120 字符切片/blocking 终止/redact 发出/尾片冲刷——卡 1-A/D1）
- `app/harness/guards/scripts.py`：安全话术常量（injection/illegal/self_harm/generic）
- `app/db/models/ai.py`：`GuardEvent` 模型（AI 域首表）
- `alembic/versions/e9f0a1b2c3d4_*.py`：guard_events 迁移（FORCE RLS + 仅追加不授 update/delete）
- `app/harness/guard_events_db.py`：`GuardEventsDbSink`（append 缓冲 + async flush 落库）
- `app/core/metrics.py`：`campus_guard_hits_total`（rule/severity/direction）
- `app/harness/orchestrator.py`：输入护栏前移（**路由之前**——审查后修正，安全/成本双优）
- `app/harness/loop.py` + `react_loop.py`：GENERATE 哨兵接线（VERIFY 升级改写——TD-332 收口）+ flush_guards
- `app/harness/chat_assembly.py`：guard_events_sink 装配传递

### 10.2 与简报的偏离（全登记）

1. **输入护栏从 loop 层前移到 orchestrator 层**（§5 步 5 写「loop 接线」）：orchestrator.run 里路由之前——安全（注入不触路由 LLM）+成本（不消耗路由调用）双优；loop 层的输入护栏段已移除避免重复拦截。
2. **prompt 三模板外新增第四个模板 `react_think_v1`**（AI-01c §10.2-1 已登记——THINK 指令需独立版本化）。
3. **flush_guards 为 async + await**（原 ensure_future 不保证落库完成——BS-M4-04 集成实测捕获）。
4. **快路径 VERIFY 升级**：`_verify_references` 告警方法删除（哨兵接管改写——TD-332 正式收口），`test_out_of_range_reference` 改为断言 [9] 剔除。

### 10.3 门禁读数（2026-11-20，终态）

- `ruff check app tests alembic`：0 错误；`mypy --no-incremental app tests`（319 文件）0 错误
- `bandit -r app -x tests`：0 issue；`alembic check`：无漂移（roundtrip + check 实测干净）
- 受影响全子集 **262 passed**（含 BS-M4-04 真实栈四弧 + guard_events 库内断言）

### 10.4 审查处置

（待独立审查后回填。）
