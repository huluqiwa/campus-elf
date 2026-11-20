# M4-AI06/07 设计简报 — 会话持久化 + 上下文注入 + trace 落库（TD-330 收口）

> 日期：2026-11-21。决策卡五张已经用户拍板（「全按推荐」，见 §2）。
> 前置：AI-01a/b/c + AI-09 已合并；真连 smoke 已通过（TD-327 关闭）。

---

## 1. 目标与验收

### 1.1 目标

AI-06（近 10 轮短期记忆）+ AI-07（清空当前会话）+ **TD-330 收口**（trace/llm_usage 落库）。
统一入口改造：chat turns 从「无状态单轮」升级为「有状态多轮对话」。

**本点范围**：

- 迁移（1 个）：5 张表——`conversations`、`conversation_messages`、`chat_traces`、
  `chat_trace_spans`、`llm_usage`（design §4.4 字段设计）
- `app/db/models/ai.py` 扩展：5 个模型
- `app/harness/context.py`：`ContextBuilder`（近 10 轮对话 → LLM messages）
- `app/harness/persistence.py`：消息写入（user+assistant 一次性）+ trace 落库
  （`DbTraceSink` 替代 InMemory——TD-330 收口）
- API 3 端点：列表/历史/清空
- `POST /campus/chat/turns` 改造：pinned_kb_version 固定 + 上下文注入 + 持久化
- `campus.conversation.clear` 权限消费（AI-07）

**本点明确不做**：滚动摘要（conversation_summaries 延后）；长期记忆；C 端 UI；
Langfuse 导出。

### 1.2 验收编号

| 编号 | 验收内容 |
|---|---|
| M4-AI0607-A1 | 消息持久化：回合结束（done/error/guard_block）写入 user+assistant 两条；中断（USER_CANCELLED）不写 assistant |
| M4-AI0607-A2 | 上下文注入：第二轮提问时 LLM messages 含近 10 轮对话原文（user/assistant 交替）；空会话仅当前问题 |
| M4-AI0607-A3 | 清空会话：messages.status → deleted + 写审计（campus.conversation.clear）+ 后续提问不带入已清空消息 |
| M4-AI0607-A4 | pinned_kb_version：首轮创建 conversation 时写入当前活跃版本，后续轮固定不变 |
| M4-AI0607-A5 | trace 落库：chat_traces/chat_trace_spans 每回合必写（DbTraceSink）；InMemory 保留为测试替身 |
| M4-AI0607-A6 | llm_usage：每回合一条（provider/model/tokens/latency） |
| M4-AI0607-A7 | API：列表（游标分页）/历史（消息游标）/清空（幂等 + 权限 + 审计） |

### 1.3 依赖与现状

- **表全部未建**（design §4.4 有字段设计）；`campus.conversation.clear` 权限码已有
- 现有 `POST /campus/chat/turns` 已带 `conversation_id` 参数（orchestrator 接收但当前仅回显）
- `InMemoryTraceSink` 为当前 trace sink（TD-330 收口目标：DbTraceSink）
- `TraceRecord`/`TraceSpan` 契约已定义（AI-01a），trace 落库为「形状搬迁」

---

## 2. 决策卡拍板记录（2026-11-21，用户「全按推荐」）

| 卡 | 结论 |
|---|---|
| 卡 1 范围 | **A：一个功能点含 AI-06 + AI-07 + TD-330** |
| 卡 2 表范围 | **A：5 张表（不含 summaries——延后）** |
| 卡 3 上下文 | **A：近 10 轮原文注入** |
| 卡 4 清空 | **A：软删（status → deleted）** |
| 卡 5 API | **A：3 端点（列表/历史/清空）+ turns 改造** |

---

## 3. 子决策 D1–D5

| # | 决策 | 结论 |
|---|---|---|
| D1 | conversation 创建时机 | 首次 `chat/turns` 且 `conversation_id=None` → 服务端创建并回显 UUID；客户端传 UUID → 查找/创建 |
| D2 | 消息持久化时机 | 回合结束（done/error/guard_block）时**一次性写入** user + assistant 两条（USER_CANCELLED 不写 assistant） |
| D3 | pinned_kb_version 固定 | 首轮创建 conversation 时写入当前活跃版本（从 alias 表解析）；后续轮固定不变 |
| D4 | 上下文注入位置 | orchestrator.run 在路由之后、分派之前查近 10 轮 → 传给 loop → loop 注入 LLM messages（system 之后、user 之前） |
| D5 | trace 双写策略 | DbTraceSink 替换 InMemoryTraceSink（生产装配）；InMemory 保留为测试替身（test fixture 注入） |

---

## 4. API 设计

### `POST /campus/chat/turns`（改造）
- 请求体不变（message + conversation_id 可选）
- 服务端：查/创建 conversation → 输入护栏 → 路由 → 分派 loop → 回合结束持久化
- 响应 SSE 不变

### `GET /campus/conversations`（新增）
- 游标分页（`limit` + `cursor`）；当前用户自己的会话
- 挂 `campus.chat.use`

### `GET /campus/conversations/{id}/messages`（新增）
- 返回该会话的消息列表（排除 deleted；游标分页）
- 挂 `campus.chat.use` + 会话归属校验（跨租/非本人 404）

### `DELETE /campus/conversations/{id}/messages`（新增，AI-07）
- 软删该会话全部非 deleted 消息；幂等（已清空再调仍 200）
- 写审计 `campus.conversation.clear`（metadata 含清空条数）
- 挂 `campus.conversation.clear`

---

## 5. 实现顺序（TDD 红→绿）

| 步 | 内容 | 测试 |
|---|---|---|
| 1 | 迁移（5 表）+ 5 个模型 | 迁移往返 + alembic check |
| 2 | `persistence.py`：ConversationRepo + MessageRepo + TraceRepo | 单测 + 集成（真实 PG） |
| 3 | `context.py`：ContextBuilder（近 10 轮 → LlmMessage 列表） | 单测 |
| 4 | orchestrator.run 改造（persistence + context 注入 + trace 落库） | 单测（FakeLlm 脚本） |
| 5 | API 3 端点 | 集成（HTTP 全链 + RBAC + 游标分页） |
| 6 | BS-M4-05（真实栈：发消息→查历史→上下文注入验证→清空→验证不影响其他会话） | 真实 PG+Qdrant+TEI+FakeLlm |

## 6. 测试策略

- **单元**：budget/router/react/orchestrator 回归（不变）+ ContextBuilder 消息窗口切片 + persistence CRUD
- **集成**：真实 PG（persistence CRUD + 游标分页 + RLS）；SSE 端点（persistence 弧）
- **业务场景**：BS-M4-05（真实栈多轮对话 + 上下文注入验证 + 清空 + guard_events 不受影响）
- **门禁**：ruff / mypy / bandit×2 / alembic check（往返实测）+ 受影响子集

## 7. 预判技术债

| 预判 TD | 内容 | 处置方向 |
|---|---|---|
| TD-P1 | 近 10 轮窗口固定=10（不做自适应） | 压测批复核 |
| TD-P2 | conversation_summaries 延后（无摘要 → 10 轮以前的内容丢失） | AI-06 后续迭代 |
| TD-P3 | pinned_kb_version 不含「版本下线后自动切换」 | 版本切换手动触发 |

## 8. 文档回写清单

design §6.4 ContextBuilder 落地注记 / §6.6 trace 落库口径 / tech-debt TD-330 收口 / requirements AI-06/07 注记。

## 9. 高风险确认

会话持久化涉及用户数据写入（messages 表含用户提问原文），按 §11.7 从严：**简报经用户确认后再编码**。

---

## §10 实现注记（编码后回填）

### 10.1 落地文件

- `alembic/versions/f0a1b2c3d4e5_*.py`：5 表迁移（conversations/conversation_messages/chat_traces/chat_trace_spans/llm_usage，全 FORCE RLS；trace/usage 三表 readonly 授权——仅追加纪律）
- `app/db/models/ai.py` 扩展：5 模型
- `app/harness/persistence.py`：get_or_create_conversation（含 pinned 解析 + 客户端 id 幂等 + 并发回查）/save_message/get_recent_messages/save_trace（含 llm_usage 写入）/save_trace_span/build_context_messages
- `app/harness/trace_db.py`（新增）：DbTraceSink（缓冲 + flush + span 双通道去重；TD-330 收口）
- `app/harness/orchestrator.py`：run 加 persist 参数 + conv_id 持久化 + 上下文注入 + trace flush 接线 + 护栏回合 trace（route_path="guard"）
- `app/harness/llm.py`：LlmPort 协议补 provider_name/model_name（trace/usage 落库字段来源）
- `app/api/campus_chat.py`：3 端点（列表 keyset 游标/历史/清空单事务）
- `app/core/metrics.py`：无新增（guard 指标已在 AI-09）

### 10.2 与简报的偏离（全登记）

1. **orchestrator.run 加 `persist: bool = True` 参数**（简报未预期）：测试用 `persist=False` 跳过 DB 依赖——不然 orchestrator 单测需要真实 PG。
2. **SQL baseline 从 16→32**：新增会话查/创 2 + pinned 解析 2 + 消息历史/写入 + trace/usage 落库 + 审计——TD-100 口径重校准（实测注记见 baseline 文件头）。
3. **TraceRecord.route_path 扩展 "guard" 字面量**（简报未预期）：A5「每回合必写」要求护栏终止回合也落 trace；护栏回合在路由之前，无 fast/react 归属，新增字面量避免谎报路径。
4. **LlmPort 协议补 provider_name/model_name 只读属性**（简报未预期）：llm_usage.provider 无数据来源（LlmStreamEvent 不携带模型名），从端口对象读取是流式路径下唯一稳定来源；FakeLlm 同步补齐。
5. **客户端传入 conversation_id 时按该 id 建行**（D1 细化）：并发同 id 双建靠主键唯一 + 回查收敛（m6）；他人占用同 id → ValueError → orchestrator 降级新 uuid。
6. **清空端点单事务**（m4）：归属校验 + 软删 + 审计同一 db_session 提交；软删 UPDATE 收敛在 `persistence.clear_messages_in_session`（第 2 轮 Major-2 后定稿——端点层不做裸 UPDATE，显式 tenant_id 谓词）。

### 10.2.1 第 2 轮复审修复追加偏离（2026-11-21）

7. **main.py 生产缺省装配 = DbTraceSink**（第 2 轮 Blocker：TD-330 生产可达性）；InMemory 降级为测试显式注入；新增装配断言测试（无 DB 依赖）。
8. **orchestrator 会话查/创失败回退 = 全新 uuid4**（第 2 轮 Major-1：绝不复用客户端 conversation_id——跨用户读写边界）；配套跨用户隔离集成用例。
9. **列表游标复用仓储 encode_cursor/decode_cursor**（第 2 轮 Minor-2：坏游标 CursorError→400 统一契约，naive 时间戳防护免费获得）。
10. **get_recent_messages 排除 failed**（第 2 轮 Minor-6：失败回合的空 assistant 占位不进上下文/历史）。
11. **loop/react_loop 移除 persist 死参数**（持久化唯一在 orchestrator 层——第 2 轮 Minor-5）。
12. **SQL baseline 实测重注 36/上限 40**（插桩逐条核对；主优化杠杆 = db_session 数量，TD-100 口径）。

### 10.3 门禁读数（2026-11-21，终态）

- `ruff` 0 / `mypy` 150 文件 0 / `bandit`（app + alembic）0 / `alembic check` 无漂移
- 迁移 roundtrip 实测：downgrade e9f0a1b2c3d4 → upgrade head，授权口径 = trace/usage 三表仅 SELECT+INSERT（仅追加）
- 受影响子集：harness 全量 + rbac_sql_baseline + bare SQL 守卫 + campus_chat_rbac + conversation_api（7 例）+ business（BS-M4-01..05，BS-M4-05 扩 A4/A5/A6 断言）——**324 passed（0 失败，30:05，2026-11-21 终态实测）**

### 10.4 审查处置

**第 1 轮（2026-11-21）：1 Blocker / 7 Major / 9 Minor，「不能提交」**——全部处置见 review-log 第 2 轮记录：
- B1 五项全部接线：save_message（done/error/guard 三路径）/get_recent_messages（上下文注入，BS 断言历史 verbatim 注入）/DbTraceSink（新增 + 生产装配）/pinned_kb_version（创建时解析，BS 断言 == 发布版本）/llm_usage（save_trace 内写入）
- M1 双前缀/M2 游标/M3 conv 一致/M4 授权/M5 脱敏/M6 兜底/M7 零覆盖：逐一修复，新增 test_trace_db（3）+ test_conversation_api（7）+ BS-M4-05 扩断言
- Minor m1–m9：窗口 20 条 / baseline 注记 / ScopeBypass 理由 / 单事务 / limit 校验 / 并发回查 / 简报回写（本文）/ 死代码+DB 时钟 / 注释修正——全部落地

**第 2 轮（2026-11-21）：1 Blocker / 3 Major / 6 Minor，「不能提交」→ 全部修复**（见 review-log）：
- Blocker（B1-3 复发）：main.py 生产缺省 DbTraceSink + 装配断言测试
- Major-1 跨用户边界：失败回退全新 uuid4 + 隔离用例；Major-2 架构守卫：软删收敛 persistence 层；Major-3 恒真断言：改 verbatim 历史注入断言
- Minor 6 条全落地（游标复用/active 校验/死分支/死参数/failed 排除/TD-341 登记）

**第 3 轮聚焦复审（2026-11-21，同审查员）：通过（0B/0M/0 新 Minor；2 条提示级备注当场以 docstring/注释/TD-341 补注收口）。可进入提交流程。**
