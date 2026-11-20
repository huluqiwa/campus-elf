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

（待实现 PR 回填。）
