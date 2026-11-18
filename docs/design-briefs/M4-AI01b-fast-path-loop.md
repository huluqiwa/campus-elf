# M4-AI01b 设计简报 — 快路径 Loop + SSE 真流（替换空壳，TD-059）

> 日期：2026-11-19。决策卡五张已经用户拍板（「确认」＝全按推荐，见 §2）；默认口径随拍板生效。
> 前置：M4-AI01a 已合并（`ff66f1d`）——本点消费 `LlmPort`/`FakeLlm`/熔断器，不再新增 LLM 依赖面。

---

## 1. 目标与验收

### 1.1 目标

把 `POST /campus/chat/turns` 从 M0 空壳替换为真实快路径：显式状态机
**输入轻检 → ROUTE（单意图直通）→ RETRIEVE（kb.search）→ GENERATE（LLM 流式）→
VERIFY（引用一致性）→ 输出轻检 → DONE**，SSE 事件按 design §5.8 契约；
TD-059 收口（删 `DEV_SKELETON_LOOP_ENABLED` 与空壳）。

**本点范围**：

- `app/harness/loop.py`：`FastPathLoop` 编排器（显式状态机 + 事件产出 + 预算 + 兜底链）
- `app/kb/chat_retrieval.py`：chat 侧检索服务（活跃版本发现 → 多 collection 并行 fan-out →
  ACL filter（KB-09B 人群解析 fail-closed）→ RRF 分数全局归并 top_k → 证据文本 PG 回查）
- `app/harness/chat_tools.py`：三只读工具注册（`kb.search`/`kb.read_chunk`/`campus.navigate`，
  走 `ToolExecutor` 全链：schema→租户→权限→只读→超时→audit）
- Prompt：`app/harness/prompt_files/kb_qa_v1.md`（Git 版本化模板，ADR-008 首个生产落点）+
  PromptRegistry 文件加载器
- `campus_chat.py` 重写：真 Loop 接线、九类 SSE 事件、取消/超时/兜底
- TD-059 清理：`dev_skeleton_loop_enabled` 字段 + 校验分支 + 7 处测试引用清零
- 审计适配：`app/harness/audit_db.py`（harness `AuditSink` → `app.audit.writer.append_audit`
  的 DB 适配器，tool.execute 落真审计表）

**本点明确不做**（防蔓延）：

- 多意图合并作答、ReAct 多步（下一站 AI-01c；`RouteResult` 契约保留）
- 会话/消息落库（卡 2-A；AI-06/07 点建三表迁移）
- rerank（卡 4-A：CPU 11–74s 不可进 chat，TD-056 口径）
- 七类输出红线全量护栏（AI-09 点；本点 OUTPUT_GUARD 为接口占位）
- C 端聊天 UI（独立点）、Ragas（AI-01 全点完成后批次）

### 1.2 验收编号

| 编号 | 验收内容 |
|---|---|
| M4-AI01b-A1 | SSE 事件序符合 design §5.8：meta → step(route/retrieve) → tool_call → tool_result → reasoning_summary → delta* → citation* → done；error 语义（retryable/partial）与契约一致 |
| M4-AI01b-A2 | 引用事件字段与 payload 同源（chunk_id/document=doc_title/page/section=section_path）；引用序号超出上下文数即剔除（伪造引用在输出层兜底，AI-09 前的最小防线） |
| M4-AI01b-A3 | ACL fail-closed：人群解析空集/解析失败 → 不发 Qdrant 查询 → 兜底话术（CAPABILITY_FALLBACK），**无权文档的标题/章节零泄漏**；伪造人群字段不可注入（服务端解析，请求体无人群体） |
| M4-AI01b-A4 | 兜底链：LLM 未配置 → error(LLM_NOT_CONFIGURED)+CAPABILITY_FALLBACK trace；检索零命中 → 兜底话术 delta+done(CAPABILITY_FALLBACK)；LLM 失败 → error(retryable/partial 按已发 delta)；回合 30s 超时 → error+TIMEOUT |
| M4-AI01b-A5 | `kb.search` 走 ToolExecutor：审计（DB `tool.execute`）、schema、只读、超时全链生效；`tenant_id_hash` 不泄明文 |
| M4-AI01b-A6 | TD-059：`dev_skeleton_loop_enabled` 与空壳代码/测试全仓清零（`grep` 验证），生产校验分支同步删除 |
| M4-AI01b-A7 | prompt 模板 Git 版本化（sha256 校验入 PromptRegistry），渲染缺变量/多变量显式失败 |

### 1.3 依赖与现状（事实核实 2026-11-19）

- **检索面零新建**：`indexer.search_points`（融合查询带 `query_filter` + payload）、
  `audience.resolve_audience_tokens`（fail-closed 空集）、`acl_query_filter`、
  `collection_name(tenant_id, version_id)`、`kb/service._active_alias_row`（活跃 alias）；
  payload 十四字段含引用所需 `doc_title/section_path/page_start/page_end/chunk_id/chunk_type`，
  **不含 chunk 文本**（文本在 PG `kb_chunks`）——卡 3-A 的直读回查即为此设计
- **TEIEmbedder / AsyncQdrantClient** 均为 use-site 构造（build_task/retrieval_test 先例），
  无全局单例
- **PromptRegistry** 有注册表无生产加载器/文件——本点补齐（ADR-008 首个落点）
- **`campus.kb.qa` 不在权限目录**（`campus.chat.use`/`campus.nav` 已有，C 端持后者）——见 §3 D3
- **conversations 等三表不存在**（design §4.4 仅设计）——卡 2-A 零迁移
- **DEV_SKELETON 面**（7 文件）：config.py / campus_chat.py / test_config / test_skeleton_loop /
  test_bs_m0_01_health_to_trace / test_campus_chat_rbac / test_rbac_sql_baseline
- 业务场景脚手架可复用：`kb_publish_seed.seed_test_passed_version`（APPROVED→真实构建
  READY→TEST_PASSED）+ `patch_ports`（fake parser）+ 确定性 embedder

---

## 2. 决策卡拍板记录（2026-11-19，用户「确认」＝全按推荐）

| 卡 | 结论 |
|---|---|
| 卡 1 编排范围 | **A：快路径六段、单意图直通**；多意图/ReAct 下一站（`RouteResult` 保留） |
| 卡 2 会话持久化 | **A：零落库**；`conversation_id` 仅回显；每轮用当前活跃 alias 版本（pinned 语义随会话表） |
| 卡 3 证据文本 | **A：`kb.search` 只做 Qdrant 混合检索（元数据→citation）**；编排器 RETRIEVE 步内直调 chunk_read 取 top-k 文本（不进 tool_call 审计面） |
| 卡 4 fan-out/rerank | **A：全活跃版本并行 fan-out → RRF 分数全局归并；不接 rerank**（TD-056） |
| 卡 5 ROUTE/prompt | **A：规则直通零 LLM 调用**；系统提示词走 PromptRegistry + Git 模板文件 v1 |

默认口径（已生效）：TD-059 清零；LLM 未配置→error+CAPABILITY_FALLBACK；兜底链不提示
「存在但无权」；引用校验=序号一致性（存在性/归属落在 RETRIEVE 的 PG 回查——检索结果本就
租户+版本定域，二次回查属重复校验，简报如实注记此解释）；护栏全量归 AI-09；测试=
真实 PG+Qdrant+TEI + FakeLlm 注入。

---

## 3. 子决策 D1–D5（随简报确认生效）

| # | 决策 | 结论 | 备选与否决理由 |
|---|---|---|---|
| D1 | 编排引擎 | **显式 async 状态机（线性管道 + 取消传播），不引入 LangGraph**；LangGraph 依赖保留在 pyproject，ReAct 点（有循环/条件边）才真正上 graph——对 ADR-002 的偏离如实登记 | 线性管道套图框架是负收益；AGENTS §2「LangGraph 只做图执行，业务状态机用显式状态机」正是此意 |
| D2 | 工具权限码 | **MVP 三工具：`kb.search`/`kb.read_chunk` 挂既有码 `campus.chat.use`**（端点已强制，重复断言同码等价）；`campus.navigate` 挂 `campus.nav`（目录已有、C 端持有）；**`campus.kb.qa` 独立码延后 ReAct 点**随权限目录扩展（新码=目录+落库迁移+角色授予） | design §6.3 的 `campus.kb.qa` 现在引入=目录迁移+授权，快路径无多工具权限分离诉求；design 回写注记 |
| D3 | 审计落点 | **`DbAuditSink` 适配器**：harness `AuditSink` 协议 → 每事件独立短事务 `append_audit`（tool.execute 落真审计表）；trace 维持 InMemory sink（`chat_traces` 表不存在，落库随会话持久化点；TD-018 口径不变） | 直接在工具 handler 里写审计会绕过 ToolExecutor 统一面 |
| D4 | 活跃版本发现 | **`kb_publish_aliases` 全表（租户内）JOIN 文档 PUBLISHED 态** → 活跃 `version_id` 列表 → `collection_name` fan-out；无 alias 或全下线 → 检索零命中兜底 | 逐文档查 alias 产生 N+1 |
| D5 | 回合预算 | **`TURN_TIMEOUT=30s` 硬超时（asyncio.timeout 包全程）**；RETRIEVE/生成段不单独设卡（预算是压测口径非运行时强制）；`top_k` 走 Settings（`kb_chat_top_k` 默认 5，ge=1 le=20） | 段级硬超时与 30s 回合预算重复计时 |

---

## 4. SSE 事件与兜底链（本点契约细化）

```text
成功（有命中）：
  meta(turn_id/trace_id/conversation_id)
  → step(route,「正在理解问题」)
  → step(retrieve,「正在检索学校知识库」)
  → tool_call(kb.search, running)
  → tool_result(kb.search, ok, duration_ms, hits=N)
  → reasoning_summary(「已找到 N 条相关资料，正在核对引用」)
  → delta*（LLM 流式正文）
  → citation*(index/chunk_id/document/page/section；序号=上下文序)
  → done(tokens_in/tokens_out/first_token_ms/latency_ms/stop_reason=FINAL_ANSWER)

兜底与失败：
  检索零命中 / 人群空集：meta → step* → tool_result(hits=0)
    → reasoning_summary → delta(兜底话术，不提示「存在但无权」)
    → done(stop_reason=CAPABILITY_FALLBACK, tokens=0)
  LLM 未配置：meta → step* → tool_result → error(LLM_NOT_CONFIGURED, retryable=false, partial=false)
    → 结束（无 done；trace stop_reason=CAPABILITY_FALLBACK）
  LLM 失败：已发 delta → error(code, retryable, partial=true)；未发 → error(partial=false)
  回合超时：error(LLM_TIMEOUT, partial=按已发 delta) + trace TIMEOUT
  客户端断开：trace stop_reason=USER_CANCELLED（对齐空壳先例）；LLM 任务随生成器关闭取消
    （LlmPort.stream 的 BaseException → release_trial 保证熔断名额回收，AI-01a 已钉）
```

- `first_token_ms` = meta 起（Nginx 口径为 design §13；应用内从回合起点计，压测口径另测）。
- 每回合必写本地 trace（TraceRecord：intents=["kb_qa"]、route_path="fast"、prompt 三元组、
  tokens、stop_reason）——空壳先例「每个已开始回合必发 trace」延续。

---

## 5. 实现顺序（TDD 红→绿）

无迁移、无新权限码；数据面全部复用。顺序：

| 步 | 内容 | 测试 |
|---|---|---|
| 1 | `chat_retrieval.py`：活跃版本发现 + fan-out 归并 + ACL fail-closed + 文本回查 | 单元（fake qdrant client）红→绿 + 集成（真实 PG+Qdrant+词袋 embedder，KB-07A 先例） |
| 2 | `chat_tools.py` 三工具 spec/handler + `audit_db.py` 适配器 | 单元（fake audit sink 断言事件形状）+ ToolExecutor 全链（权限/只读/schema/超时） |
| 3 | prompt 文件 + 加载器 | 单元（sha256/缺变量/多变量红→绿） |
| 4 | `FastPathLoop` 状态机：事件序/引用一致性/兜底链/30s 超时/取消 | 单元（FakeLlm + fake retriever 全弧） |
| 5 | `campus_chat.py` 重写 + `create_app` 装配 + TD-059 清零 | SSE 集成（ASGI 流解析：事件序/错误路径/RBAC） |
| 6 | 业务场景 BS-M4-01/02（真实 PG+Qdrant+TEI + seed 发布版本 + FakeLlm） | 事件序与引用字段对照 / ACL fail-closed 零泄漏 / 零命中兜底 / LLM 未配置 |

## 6. 测试策略

- **单元**：loop 状态机全弧（fake 端口注入时钟/脚本）、chat_retrieval 归并/空集/文本回查丢弃、
  工具 schema/审计事件形状、prompt 加载器。
- **集成**（真实 PG+Qdrant，词袋 embedder 两侧同源确定性——KB-07A 先例）：chat_retrieval
  端到端（含 ACL filter 与多版本 fan-out）；SSE 端点（ASGI 流解析、RBAC 403、未配置 LLM）。
- **业务场景**：BS-M4-01（真实栈：seed TEST_PASSED 版本→发布→chat 回合→断言事件序/引用/
  trace/审计落库）；BS-M4-02（ACL：无权人群零召回零引用零标题泄漏；零命中兜底；LLM 未配置
  兜底）。FakeLlm 脚本化回答，检索/嵌入全真实。
- **门禁**：ruff / mypy / bandit×2 / alembic check（零迁移）+ 受影响子集。

## 7. 预判技术债（实现 PR 登记）

| 预判 TD | 内容 | 处置方向 |
|---|---|---|
| TD-P1 | trace 仍 InMemory（`chat_traces` 表未建），生产不可查 | 会话持久化点建表落库（TD-018 口径） |
| TD-P2 | 跨版本 fan-out 为串行/受限于事件循环并发度，版本数大时检索延迟线性涨 | MVP 规模可控；k6 压测批次复核，必要时 asyncio.gather 并发已内置 |
| TD-P3 | 引用「语义相关性」校验缺失（本点仅序号一致性） | AI-04 引用校验点补 |
| TD-P4 | 多文档同分 RRF 归并次序稳定性（跨 collection 并发返回序） | 归并按 (score desc, chunk_id asc) 确定性排序钉住 |

## 8. 对 spec/design 的回写清单（随实现 PR）

| 文档 | 变更 |
|---|---|
| design §6.3 | 三工具权限注记（MVP 挂 `campus.chat.use`/`campus.nav`；`campus.kb.qa` 延后 ReAct 点） |
| design §5.8/§6.2 | 快路径事件序与兜底链按 §4 落地口径（引用校验=序号一致性 + RETRIEVE PG 回查解释） |
| design §6.1/ADR-002 | 快路径为显式状态机、LangGraph 延后 ReAct 点的偏离留痕（D1） |
| requirements AI-01 | 快路径范围注记（多意图/ReAct 归后续点） |

## 9. 高风险确认

AI 域功能点，按 §11.7 从严：**简报经用户确认后再编码**。新增待确认项＝§3 D1–D5 与
§4 兜底链细化（决策卡五张已拍板）。

---

## §10 实现注记（编码后回填）

（待实现 PR 回填：落地差异、读数、审查处置。）
