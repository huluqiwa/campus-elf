# M3-KB-10B 设计简报——切块编辑与派生重建前端（K5 前端半）

> 依据：`docs/design-briefs/M3-KB-10.md` 决策卡 D6（2026-11-14 拍板）+ 本简报决策卡 E0-E8（2026-11-17 用户拍板全按推荐）。
> 后端已交付：KB-10A（PR #76 → `28f8466`）+ 复审 hotfix（PR #77 → `319d25d`）；复审台账 TD-320/321/322 处置交本点。
> 侦察基准：main `afb48de`（2026-11-17，Explore 只读侦察报告）。

## 不做的事

- 不做微信小程序/移动端适配（管理端 Element Plus 桌面形态）。
- 不做富文本/Markdown 所见即所得编辑（纯 textarea + 纯文本 diff，KB-10 校验在后端）。
- 不新增写工具/AI 行为（K5 仅分块文本编辑；AI 面不在本点）。
- 不动 chunker/切块参数（TD-294 实验在本点之后）。

## 0. 现状（2026-11-17 侦察核实）

- **抽屉单文件三互斥子视图**：`KbDocumentDrawer.vue`（1909 行）——分块子视图 `:921`（`kb-chunks-view`）/ 人群子视图 `:1283`（`kb-acl-view`）/ 文档主体 `:1337`；子视图互斥先例 `openChunks:470-472`、`openAcl:501-507`（相互置 null + 独立代际守卫 `chunksGeneration:173`/`aclGeneration:158`）。
- **api-client 零占位**：`frontend/packages/api-client/src/kb.ts` 无 edits/derive/三列字样；`KbChunkView :90-104` 仅 13 字段；后端契约在 `school_kb.py`（`KbChunkResponse :1145-1164` 已含 `is_human_edited/edited_by/edited_at`；三端点 `:1491-1597`）。
- **事实缺口①**：`chunk_hash` 不在任何读端点回包（仅 KbChunkEditRequest Field 描述 `:1466`）——base_hash 前端无供给（卡 E2 解决）。
- **mock 层**：`frontend/e2e/helpers/kb-mock.ts`（1185 行）；`chunks` 路由正则 `:1078` 单段不匹配 `/edits`；`document-versions` 路由正则 `:1130` 只认 audiences——三端点均需扩展；错误体形态 `{detail:{code,message}}` 先例 `:1166-1174`。
- **kb-logic**：`frontend/apps/admin/src/kb-logic.ts`（716 行）+ 730 行 vitest；gates 全 permissions-only（`chunksGate:500`、`aclManageGate:624`）；错误码镜像先例 `audienceEditError:654-680`（文案内嵌错误码）；409 码→指引 map 先例 `PUBLISH_ACTION_HINTS:336-344`。
- **E2E**：mock `frontend/e2e/admin-kb.spec.ts`（1753 行扁平 test，KB-09C 段 `:1539-1740` 7 例）；全栈 `frontend/e2e-fullstack/admin-fullstack-kb-publish.spec.ts`（serial 模式，`configureAcl:96`/`waitForDocumentStatus:117` helpers）。
- **事实缺口②**：`e2e_seed.py:321-365` 无 `school.kb.chunk.edit/chunk.confirm` 码、无 `school_super_admin` 绑定（卡 E7 解决）。
- **事实缺口③**：前端无 role 判先例（`AuthUser.roles` 字段已存在无人消费）；derive 后端是码+role 双重门禁（`school_kb.py:1575-1581`，403 `CHUNK_CONFIRM_FORBIDDEN`）。
- **事实缺口④**：409 `CHUNK_EDIT_STATE` 回包只有 `{code,message}` 不带派生版本 id；`GET chunk-edits` 回包已含 `derived_version_id`（卡 E6 引导数据源）。

## 1. 目标与验收

引用 spec :322-334（KB-10 节）验收行 + 卡 D6：

1. 分块行/详情可见 `is_human_edited` 标记与编辑人/时间（spec :333 验收 7）。
2. `chunk.edit` 码主体可对子块提交编辑（对话框：new_content + reason + base_hash），409/422/403 族有 UI 反馈（spec :330 验收 4）。
3. `chunk.confirm` + `school_super_admin` 双判主体可发起派生；`DERIVE_NO_EDITS`/`CHUNK_EDIT_STATE` 有指引（spec :330 验收 1/5）。
4. 已派生版本再提交 → 409 引导到派生版本（TD-321 承接，卡 E6-A）。
5. 编辑长度上限 = `max(500, 原块码点)`（TD-320 拍板，卡 E5-A；spec :324 回写）。
6. mock E2E +7；全栈链④「编辑→派生→发布→旧会话不漂移」；后端 BS-M3-11 已钉嵌入计数（前端不重复断言调用次数，卡 E7）。

## 2. 决策卡（2026-11-17 用户拍板，全按推荐）

### 卡 E0 · 拆分 → **A：单 PR**
前端整块 + 2 个后端小改（E2 chunk_hash 透出、E5 上限改法）+ seed 补码；~8-10 前端文件 + 测试（KB-06B/09C 先例）。

### 卡 E1 · 编辑入口 → **A：分块行「编辑」按钮 + el-dialog 对话框**
- 编辑按钮挂分块表格行 + 详情面板（`chunk.edit` 码渲染，KB-09C 人群入口同模式）；对话框 textarea + reason + 客户端预检纯函数（`audienceEditError` 同款镜像 `validate_edit_content`）。
- 409 `CHUNK_CONFLICT`：对话框内警示不关闭（服务端 message 优先，KB-09C 先例）。

### 卡 E2 · base_hash 供给 → **A：后端读端点补 `chunk_hash` 一列**
`KbChunkView`/详情回包补 `chunk_hash`（KB-10B PR 内后端小改；spec :328 透出要求）。不选前端自算（双事实源）/chunk-edits 间接取（新块无解）。

### 卡 E3 · 提交记录与派生确认区 → **A：提交记录进分块子视图；派生按钮挂版本行操作列**
- `GET chunk-edits` 提交列表 + 纯文本 diff（当前 text vs `new_content`）放分块子视图。
- 派生按钮挂版本行操作列（分块/人群入口旁）：`chunk.confirm` 码 **+ `school_super_admin` role 双判**（前端首个 role 判，`AuthUser.roles` 消费起点）。

### 卡 E4 · is_human_edited tag → **A：分块表加列 + 详情面板头部 tag**
「嵌入」列旁加「人工修订」tag；mock `KbChunkFixture` 补三列（升级 :38 注释口径）。

### 卡 E5 · TD-320 拍板 → **A：上限改 `max(500, 原块 char_count)`**
后端 `validate_edit_content` 上限改 `max(CHUNK_CONTENT_MAX_CHARS, len(原块))`（超长原子块可保持原长度修正）；前端对话框按原块码点显示上限提示；spec :324 与本简报回写。不选禁改（修表格场景废）/维持 500（内容丢失诱因）。

### 卡 E6 · TD-321 承接 → **A：409 引导跳转，零后端改动**
submit 409 `CHUNK_EDIT_STATE` → 前端调 `GET chunk-edits` 取 `derived_version_id` → toast/链接引导「到派生版本继续编辑」；编辑按钮不禁用（后端行为安全）。不选 KbError 扩 payload（改动面大）。

### 卡 E7 · E2E 与 seed → **A：mock + 全栈全做**
- mock：3 端点有状态 mock（`chunk-edits`/`derive` 路由正则扩展）+ ~7 条用例（09C 段规模）。
- 全栈链④：seed 补 `chunk.edit/chunk.confirm` 码 + `school_super_admin` 绑定；链④断言 READY + 源 collection 不漂移（嵌入计数断言留在后端 BS-M3-11，前端不重复）。

### 卡 E8 · TD-322 → **A：维持延后**
围栏内管道行误伤维持「已延后（KB-10B 前评估）」处置；本点不改校验逻辑，E5-A 落地时保持轻量校验口径一致。

## 3. 契约（前端）

- **api-client 新增**（`kb.ts`）：
  - `submitChunkEdit(chunkId, {new_content, reason, base_hash})` → `POST /school/kb/chunks/{chunk_id}/edits`（201 `KbChunkEditResponse`）
  - `listChunkEdits(versionId)` → `GET /school/kb/document-versions/{version_id}/chunk-edits`（`KbChunkEditResponse[]`）
  - `deriveVersion(versionId)` → `POST /school/kb/document-versions/{version_id}/derive`（201 `KbDeriveResponse`）
  - 类型：`KbChunkEditRequest/Response`、`KbDeriveResponse`；`KbChunkView` 补 `is_human_edited/edited_by/edited_at` 三列；`chunk_hash` 随 E2 后端小改补充。
- **kb-logic 新纯函数**（vitest 逐个 describe）：
  - `chunkEditGate(permissions)`（单码 `school.kb.chunk.edit`）、`chunkConfirmGate(permissions, roles)`（码 + `school_super_admin` 双判）
  - `chunkEditError(newContent, maxChars)`（镜像 validate_edit_content：trim 非空/上限/围栏配对/表格行组；文案内嵌错误码）
  - `chunkEditConflictMessage(code)`（409/422 族码→指引 map：CHUNK_CONFLICT/CHUNK_CONTENT_INVALID/CHUNK_EDIT_TARGET_INVALID/CHUNK_EDIT_STATE/DERIVE_NO_EDITS/CHUNK_CONFIRM_FORBIDDEN；服务端 message 优先）
  - `formatEditDiff(before, after)`（纯文本逐行对比摘要：仅返回变更行集，不引第三方 diff 库）
- **UI 状态**：编辑对话框 `el-dialog`（teleport 先例——E2E 用 page 级定位）；提交记录区（分块子视图内，列表 + diff 展开）；派生按钮（版本行，禁用态=已派生/无提交由数据推导）。

## 4. 实现顺序（TDD 红→绿）

1. **后端小改**：E2 `chunk_hash` 透出（`KbChunkResponse` + `_chunk_response`；集成断言补字段）→ E5 上限 `max(500, 原块码点)`（单测红→绿 + spec :324 回写）。
2. **api-client + 类型**：kb.ts 三方法 + 类型 + `chunk_hash` 字段。
3. **kb-logic 纯函数**：vitest 红→绿（gate×2 / 校验镜像 / 409 map / diff）。
4. **mock 层**：kb-mock 三端点有状态 mock + fixture 三列升级。
5. **UI**：分块行编辑按钮 + 对话框 + 提交记录区 + diff + 派生按钮 + tag；`el-dialog` 内错误回填。
6. **mock E2E**：+7（编辑提交 201/409 乐观锁/422 校验镜像/派生 201/409 指引/tag 渲染/权限门禁）。
7. **全栈链④**：seed 补码 → 编排编辑→派生→发布→旧会话不漂移断言。
8. **handoff/README 回写**随点收口。

## 5. 测试策略

- **单元（vitest）**：kb-logic 新纯函数全覆盖（校验镜像 9+ / gate×2 / 409 map / diff 边界）。
- **后端受影响单测/集成**：`test_kb_chunk_edit_unit.py`（E5 上限红→绿）、`test_kb_chunks.py`（chunk_hash 断言）、`test_kb_chunk_edits.py` 回归。
- **mock E2E**：+7 条（对齐 09C 段命名「KB-10B …」）。
- **全栈 E2E**：链④（seed 补码后 UI 驱动编辑→派生→发布→源不漂移）。
- **静态**：eslint/typecheck/format:check + ruff/mypy/bandit/alembic check（后端触碰面）。
- **AI 评估**：不涉及（无 AI 行为变更）。

## 6. 预判技术债（实现时登记）

- 预判 TD-1：diff 为逐行对比简化版（不做 word-level），长表格 diff 可读性有限——KB-10C/M3 边界评估。
- 预判 TD-2：提交记录区无分页（`GET chunk-edits` 全量列表）——编辑量大后需分页（与后端同批）。
- TD-320/321 处置随卡 E5/E6 落地后改为「已完成」；TD-322 维持延后。

## 7. 高风险确认

- 权限/role 判（卡 E3 首个 role 判）与 409/422 族映射——已按推荐拍板。
- 后端触碰面（E2/E5）不涉及租户边界/状态机/加密——E2 是只读列透出、E5 是校验上限放宽（`max` 下限仍 500），均无新增红线面。
- 本简报确认后开始编码（2026-11-17 用户「确认」）。
