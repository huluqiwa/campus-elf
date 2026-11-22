# M4-UI1 设计简报 — C 端聊天核心（SSE 消费器 + 聊天页 + 会话管理 + campus.kb.qa 权限码）

> 日期：2026-11-22。决策卡六张已经用户拍板（「按你推荐的来」，见 §2）。
> 拆分（卡 1-A）：UI1=聊天核心（本简报）→ UI2=体验收口（引用抽屉/反馈入口/思考折叠/全栈 E2E/3D 待机）。
> 前置：AI 后端链路 11 点全部合并（main `3b06a31`）；Langfuse 导出随 Ragas 批（用户默认）。

---

## 1. 目标与验收

### 1.1 目标

C 端（Vant 4 H5）可用聊天：学生登录后进入聊天页，输入问题 → 流式看到回答
（9 类 SSE 事件消费）→ 停止/重试 → 会话列表与历史 → 清空会话。
聊天入口按 `campus.kb.qa` 权限码渲染（权限码本期落地，design §889 两次改判的终点）。

**本点范围**：

- 后端小改：`campus.kb.qa` 权限码（目录 + 迁移插 permissions 行 + 矩阵文档 v1.3 + 测试）
- `packages/api-client` 新增 campus 模块：`streamTurn`（SSE 解析器）+ 会话列表/历史/清空方法
- `apps/campus` 新增：ChatView（消息列表/流式正文/停止/重试/错误态/兜底展示）、
  ConversationsView（会话列表 + 清空）、路由与 HomeView 入口（按权限渲染）
- mock E2E：`campus-chat.spec.ts`

**本点明确不做**：引用抽屉、反馈入口、思考折叠面板（UI2）；全栈 E2E 链⑤（UI2）；
3D 待机（UI2 落地 CSS 方案，真 3D 登记 TD）；`campus.kb.qa` 码的按钮级细分消费；
语音输入；多模态。

### 1.2 验收编号

| 编号 | 验收内容 |
|---|---|
| M4-UI1-A1 | `campus.kb.qa` 落地：目录行 + 迁移插入 + 矩阵文档 v1.3 + 测试（student/teacher 内置角色默认含新码〔新租户种子路径〕；超管 FULL_SCOPE 只补不撤自动获得；**存量租户不自动**〔TD-150 口径〕——测试钉住） |
| M4-UI1-A2 | SSE 解析器：9 类事件正确分帧（含半包缓冲/多事件一 chunk/跨 chunk 事件）；abort 停止传播；非 200/非 SSE 响应错误化 |
| M4-UI1-A3 | ChatView 流式正文：delta 逐字追加渲染；reasoning_summary 以步骤提示条轻量展示（折叠面板归 UI2）；citation 事件子点 1 仅记录数据（抽屉归 UI2） |
| M4-UI1-A4 | 停止：AbortController abort → UI 标「已停止」；服务端 trace USER_CANCELLED（既有弧回归） |
| M4-UI1-A5 | 重试：error（retryable）/已停止/不完整回答 → 「重新提问」按钮 = 新 turn |
| M4-UI1-A6 | 会话列表：分页（加载更多）；点击进入历史（GET messages 渲染）；清空（DELETE）→ 界面清空 + 确认弹层 |
| M4-UI1-A7 | 入口权限渲染：持 `campus.kb.qa` → 显示聊天入口；不持 → 不显示（直接访问路由仍可进——页面内操作由后端 403 兜底） |
| M4-UI1-A8 | mock E2E `campus-chat.spec.ts`：正常流（meta→step→delta→done 渲染）/兜底（CAPABILITY_FALLBACK 文案）/error 停止/重试/清空/权限渲染 |

### 1.3 依赖与现状

- SSE 契约 design §5.8 已定；后端 9 类事件已实现（AI-01b/c）
- api-client：approvals/auth/imports/health 模块已有（vitest 同目录）；**无 campus/chat**
- campus app：Vant 4 + vue-router；HomeView 为健康检查占位 + 登出
- E2E：Playwright route.fulfill + helpers（auth-mock/kb-mock 先例）
- `campus.kb.qa` 不在目录（`app/rbac/catalog.py` 95~102 行 campus 域 9 码）；权限矩阵文档 v1.2

---

## 2. 决策卡拍板记录（2026-11-22，用户「按你推荐的来」）

| 卡 | 结论 |
|---|---|
| 卡 1 拆分 | **A：拆 2 子点**（UI1 聊天核心 / UI2 体验收口） |
| 卡 2 SSE 消费形态 | **A：fetch POST + ReadableStream 手写解析**（EventSource 不支持 POST/Authorization） |
| 卡 3 `campus.kb.qa` | **A：UI1 落地**（目录+迁移+矩阵 v1.3+测试；存量租户显式授予口径钉住） |
| 卡 4 3D 待机 | **A：CSS/SVG 轻量动画 + Lottie 接口位（UI2）；真 3D 登记 TD 延后** |
| 卡 5 断线重连 | **A：严格按 design §5.8**（保留已收 delta + 重新提问=新 turn；零后端改动） |
| 卡 6 E2E | **mock + 全栈两套**（mock 归 UI1；全栈链⑤归 UI2） |

---

## 3. 子决策 D1–D6

| # | 决策 | 结论 |
|---|---|---|
| D1 | SSE 解析器 | `campusChat.streamTurn(body, { signal, onEvent })`：fetch POST → ReadableStream → TextDecoder(stream=true) → 按 `\n\n` 分帧（残包缓冲跨 chunk 保留）→ `event:`/`data:` 解析 → discriminated union 事件回调；HTTP 非 200 → 解析错误信封抛 `ChatApiError` |
| D2 | 权限码迁移 | 迁移只插 `permissions` 目录行（`campus.kb.qa`，campus 模块）——**角色映射不在迁移里动**（运行时同步职责，TD-150 口径）：新租户种子自动含；FULL_SCOPE 超管「只补不撤」自动获得；存量 student/teacher 需显式授予或重置出厂——矩阵文档 v1.3 注记 + 测试钉住 |
| D3 | 前端消息模型 | `{ id, role: 'user'\|'assistant', content, reasoningSteps: string[], citations: Citation[], status: 'streaming'\|'final'\|'stopped'\|'error'\|'fallback' }`；assistant 消息在 meta 事件后预插入（占位气泡） |
| D4 | 会话路由 | `/chat`（新会话）+ `/chat/:conversationId`（续聊：进入时 GET messages 渲染历史）；meta 事件回显 conversation_id → router.replace 更新 URL（刷新可续聊） |
| D5 | 停止 | AbortController.abort() → fetch 抛 AbortError → 前端吞掉并标 stopped；服务端经 GeneratorExit 走 USER_CANCELLED（既有弧）；abort 后到达的事件忽略 |
| D6 | mock E2E 的 SSE | `route.fulfill` 一次性返回完整 `text/event-stream` 文本（前端解析器按流处理不受影响）；错误/停止弧用 `route.abort()` |

---

## 4. 组件设计

- `packages/api-client/src/campus-chat.ts`：`streamTurn` + `listConversations` /
  `getConversationMessages` / `clearConversation`（复用既有 request 基建与鉴权头）
- `apps/campus/src/views/ChatView.vue`：消息气泡列表（user 右/assistant 左）+
  输入框（≤2000 字与后端契约一致）+ 停止按钮（streaming 态）+ 步骤提示条
  （reasoning_summary）+ 重试条（error/stopped 态）
- `apps/campus/src/views/ConversationsView.vue`：会话列表（卡片：时间/清空按钮）+
  加载更多；清空确认（van-dialog）
- `apps/campus/src/stores/chat.ts`：会话/消息状态（Pinia，auth store 先例）
- 路由：`/chat`、`/chat/:conversationId`、`/conversations`；HomeView 入口按
  `hasPermission('campus.kb.qa')` 渲染

---

## 5. 实现顺序（TDD 红→绿）

| 步 | 内容 | 测试 |
|---|---|---|
| 1 | 后端 `campus.kb.qa`：目录 + 迁移 + 矩阵 v1.3 + seeds 注记 | 迁移往返 + RBAC 同步测试（新码进目录/超管自动补/存量不自动）+ alembic check |
| 2 | api-client `campus-chat.ts`（SSE 解析器 + 3 方法） | vitest：分帧/半包/多事件一 chunk/abort/错误信封 |
| 3 | stores/chat.ts + ChatView + ConversationsView + 路由/入口 | vitest 组件状态机 |
| 4 | mock E2E `campus-chat.spec.ts`（A8 全弧） | Playwright mock |
| 5 | 门禁（ruff/mypy/py 覆盖率门 + vue-tsc/vitest/前端门）+ 独立审查 | 全量受影响子集 |

## 6. 测试策略

- **后端**：权限码单测（目录/同步/矩阵解析比对）+ 迁移往返 + 受影响 RBAC 子集
- **前端单元**：SSE 解析器（伪 ReadableStream 注入字节序列：半包/跨 chunk/非法行）
  + chat store 状态机 + api-client 方法（fetch mock）
- **mock E2E**：A8 全弧（SSE mock 用完整 event-stream 文本；停止弧 route.abort）
- **UI2 收口**：全栈链⑤ + 引用抽屉/反馈/折叠（下一简报）
- **门禁**：后端四件套 + 前端 vue-tsc/vitest/eslint + 前端覆盖率门（既有口径）

## 7. 预判技术债

| 预判 TD | 内容 | 处置方向 |
|---|---|---|
| TD-P1 | 存量租户 student/teacher 不自动获得新码（TD-150 口径使然） | 文档注记 + 运营显式授予；M5 批量工具评估 |
| TD-P2 | 真实 3D 待机模型（Three.js/glTF） | UI2 落 CSS 方案后按需评估 |
| TD-P3 | 历史消息分页（GET messages limit=50 上限，无游标翻页 UI） | UI2/后续点 |
| TD-P4 | SSE 解析器无自动重连（design §5.8 口径：展示已完成部分+重试） | 保持 design 口径 |

## 8. 文档回写清单

design §889 权限注记改写（campus.kb.qa 已落地）/ 矩阵文档 v1.3 / requirements §2.1.2
C 端功能点注记 / handoff §1 + README（合并后 docs PR）。

## 9. 高风险确认

权限码变更（目录+迁移+角色默认口径）属权限面改动，按 §11.7 从严：**简报经用户
确认后再编码**。前端本身无数据写入面新增（全部消费既有已审 API）。

---

## §10 实现注记（编码后回填）

### 10.1 落地文件

- `backend/app/rbac/catalog.py`（+campus.kb.qa）/ `docs/rbac-matrix.md` v1.4 /
  `backend/tests/integration/test_rbac_sync.py`（+A1 专项断言）——**零迁移**
- `frontend/packages/api-client/src/campus-chat.ts`（355 行：SSE 解析器+3 方法）+ 测试 8 例
- `frontend/apps/campus`：stores/chat.ts / ChatView.vue / ConversationsView.vue /
  router / HomeView 入口 / main.ts（Vant List/Dialog 注册）
- `frontend/e2e/campus-chat.spec.ts`（8 例）+ `e2e/helpers/chat-mock.ts`

### 10.2 与简报的偏离（全登记）

1. **零迁移**（简报写「迁移插 permissions 行」）：先例 d7a1c3e5b9f4 注记——新权限码由
   `sync_rbac_definitions()` 幂等落库（目录即事实源）；迁移只做结构变更。角色映射口径
   不变：新租户种子自动、超管 FULL_SCOPE 只补不撤、存量 student/teacher 显式授予
   （A1 测试钉住）。
2. **矩阵文档 v1.4**（简报写 v1.3——现网已是 v1.3，顺延 v1.4）。
3. **停止弧移交全栈 E2E（UI2）**：mock route.fulfill 一次性返回完整 SSE 文本，
   无法模拟流中断时机——UI 侧停止逻辑（AbortController）由 api-client 单测
   （abort → ChatAbortError）覆盖，E2E 级停止弧随 UI2 全栈链⑤。
4. **campus app 无 vitest 组件测试**：campus 包测试基建为 Playwright-only（KB 先例）；
   组件薄逻辑（气泡渲染）由 mock E2E 覆盖，store 状态机核心（SSE 应用）由
   api-client 解析器单测覆盖。store 独立单测登记 TD-P5。
5. **Playwright glob 教训**：`?` 是单字符通配（非字面问号）——路由 mock 需用
   `*` 后缀匹配 query；`route.fulfill` 的 SSE 文本前端流式解析兼容。
6. **E2E 排查插曲（非缺陷）**：mock 权限数组曾出现 [null]——cleanup 误删
   `CHAT_PERMISSION` 常量定义（spec import undefined）；已恢复并保留「密文式
   debug cell」排查法。HomeView 的 health 探测移除（占位页改聊天入口主页）。

### 10.3 门禁读数（2026-11-22）

- 后端：RBAC 同步+矩阵 **45 passed**（含 A1 专项）；ruff/mypy/bandit/alembic check
  （零迁移）全绿——受影响后端子集随最终提交复跑
- 前端：api-client vitest **120 passed**（含 campus-chat 8 例）；campus vue-tsc 净；
  eslint 净；**mock E2E 8 passed**（20.3s）

### 10.4 审查处置

（待独立审查后回填。）
