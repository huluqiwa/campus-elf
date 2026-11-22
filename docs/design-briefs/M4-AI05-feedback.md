# M4-AI05 设计简报 — 反馈采集（C 端提交 + B 端处理 + 匿名化）

> 日期：2026-11-22。决策卡六张已经用户拍板（「确认」，见 §2）。
> 前置：AI-01a/b/c + AI-09 + AI-06/07 + AI-04 已合并（main `f3a4b15`）。

---

## 1. 目标与验收

### 1.1 目标

spec AI-05 落地：C 端对 AI 回合提交反馈（有用/无用 + 原因标签 + 可选文本），
B 端校方查看/标记处理/答复；**提交即知情同意、脱敏、可删除匿名化**。

**地基层已备好（零权限迁移）**：权限码三枚在目录（`campus.feedback.create`
student/teacher C 端默认持有；`school.feedback.view`/`school.feedback.update`
校方超管全量型自动获得、school_admin 显式授予）；AES-256-GCM 加密三件套已有
（`app/security/encryption.py`）；conversation/turn/trace 三 id 已落库（AI-06/07）。

**本点范围**：

- 迁移（1 个）：`ai_feedback` + `ai_feedback_replies` 两表（design §4.4 字段，
  FORCE RLS + 仅追加纪律）+ `app/db/models/ai.py` 扩 2 模型
- C 端：`POST /api/v1/campus/feedback`（挂 `campus.feedback.create`；幂等重放）+
  `DELETE /api/v1/campus/feedback/{id}`（自助删除 → 匿名化，spec「申请删除反馈」）
- B 端：`GET /school/feedback`（列表，时间/评分筛选）+ `GET /school/feedback/{id}`
  （详情：受控解密快照）+ `POST /school/feedback/{id}/handle`（标记处理）+
  `POST /school/feedback/{id}/reply`（答复落 `ai_feedback_replies`）——design §5.11
- 匿名化函数（M5 账号删除流程将复用）：user_id 置空、comment/快照清除、
  status=anonymized、写审计
- 快照加密：被反馈轮问/答/引用摘要 → AES-256-GCM（AAD 绑定）；comment 提交时
  PII 脱敏

**本点明确不做**：B 端反馈管理**页面**（归 UI 批次，卡 1）；站内信推送集成
（答复落表即可，消息系统归 M5，卡 6）；账号删除全流程（M5，本点交付匿名化
函数与自助触发端点）；看板统计聚合。

### 1.2 验收编号

| 编号 | 验收内容 |
|---|---|
| M4-AI05-A1 | 提交：落库 + consent_at 服务端记录（提交即知情同意）+ 关联 conversation/turn/trace 三 id（归属校验：非本人/跨租/不存在 → 404）+ 返回成功；同 (conversation_id, turn_id) 重复提交 → 200 replayed=true 返回既有反馈 |
| M4-AI05-A2 | 快照：被反馈轮问/答/引用摘要 AES-256-GCM 加密落 `content_snapshot_enc`；comment 过 PII 脱敏后落库 |
| M4-AI05-A3 | B 端列表/详情：挂 `school.feedback.view`；列表（评分/时间筛选 + 游标分页）；详情受控解密展示快照（问题/答案/引用摘要）；跨租 404 |
| M4-AI05-A4 | 标记处理：`school.feedback.update`；submitted → handled 状态机（handled_by/handled_at 服务端记录）；重复处理幂等；写审计 |
| M4-AI05-A5 | 答复：`school.feedback.update`；落 `ai_feedback_replies`；写审计；站内信推送归 M5 |
| M4-AI05-A6 | 匿名化：C 端自助 DELETE（仅本人）→ user_id 置空、comment/快照清除、status=anonymized、审计；幂等（已匿名化再删 → 200）；M5 账号删除流程复用同一函数 |
| M4-AI05-A7 | B 端详情对匿名化反馈不再展示快照/评论（仅保留 rating/tags/status 供统计） |

### 1.3 依赖与现状

- 两表未建（迁移 docstring 明确"不含 feedback——后续点"）；无模型
- `encrypt_secret/decrypt_secret(plaintext, kek=, aad=)` 已有；KEK 从 Settings
  注入（身份证采集先例 `app/security/id_card.py` 可参照 AAD 构造）
- PII 检测器 `app/harness/guards/rules.py`（身份证/手机号/邮箱模式）已有
- 幂等重放先例：KB 上传 `replayed=true`（`UploadOutcome`）
- B 端列表游标分页先例：KB 文档列表（keyset `(created_at,id)`）

---

## 2. 决策卡拍板记录（2026-11-22，用户「确认」）

| 卡 | 结论 |
|---|---|
| 卡 1 范围 | **A：纯后端闭环**（B 端页面归 UI 批次） |
| 卡 2 幂等语义 | **A：同 (conversation_id, turn_id) 唯一约束，重复提交 200 + replayed=true** |
| 卡 3 快照加密与脱敏 | **A：快照 AES-256-GCM 加密 + comment PII 脱敏 + B 端受控解密** |
| 卡 4 匿名化触发 | **A：匿名化函数 + C 端自助 DELETE 端点（实测弧）+ M5 账号删除复用** |
| 卡 5 reason_tags | **固定枚举**：inaccurate/irrelevant/outdated/harmful/other + comment ≤500 字，库层 CHECK |
| 卡 6 答复通道 | **落 `ai_feedback_replies` 表**；站内信推送归 M5 消息点 |

---

## 3. 子决策 D1–D6

| # | 决策 | 结论 |
|---|---|---|
| D1 | 快照内容 | 被反馈轮的 user 问题原文 + assistant 回答全文 + citation 列表（index/chunk_id/document/page/section）——B 端详情「问题/答案摘要/引用」的数据源；从 conversation_messages 按 trace_id 取（1 user + 1 assistant） |
| D2 | AAD 构造 | `aad = f"ai_feedback:{tenant_id}:{feedback_id}"`（提交后才知道 feedback id → 加密在 INSERT 前用预生成 id）；**密钥 KEK 复用 Settings 既有 KEK**（与身份证/MFA 同一 KEK、不同 AAD 域） |
| D3 | rating 词表 | `helpful` / `unhelpful`（有用/无用），库层 CHECK |
| D4 | status 词表 | `submitted` → `handled`；`anonymized`（终态，匿名化后不可再处理）；CHECK 约束 + 应用层状态机 |
| D5 | 归属校验 | 提交时校验 conversation/turn(trace) 存在、属于当前用户且同租（RLS + 显式 user_id 谓词）；不满足 → 404（不泄露存在性） |
| D6 | B 端列表排序 | 按 `created_at desc, id desc` keyset 游标（仓储 codec，CursorError→400——AI-06/07 先例）；筛选 rating（可选） |

---

## 4. API 设计

### C 端（前缀 `/campus`，realm=tenant）

- `POST /campus/feedback`（挂 `campus.feedback.create`）
  - body：`{conversation_id, turn_id, trace_id, rating: helpful|unhelpful,
    reason_tags: [...], comment?: string≤500}`
  - 201 `{feedback_id, replayed: false}` / 200 `{feedback_id, replayed: true}`（幂等重放）
- `DELETE /campus/feedback/{id}`（本人；匿名化 + 审计；幂等 200）

### B 端（前缀 `/school`，realm=tenant）

- `GET /school/feedback`（挂 `school.feedback.view`；`rating` 筛选 + 游标分页）
- `GET /school/feedback/{id}`（详情：rating/tags/status/快照受控解密
  {question, answer, citations}；匿名化反馈不返回快照/comment——A7）
- `POST /school/feedback/{id}/handle`（挂 `school.feedback.update`；幂等）
- `POST /school/feedback/{id}/reply`（body `{content≤1000}`；落 replies；写审计）

### 审计动作

- `campus.feedback.create`（成功/幂等重放）/ `campus.feedback.delete`（匿名化）
- `school.feedback.handled` / `school.feedback.replied`

---

## 5. 实现顺序（TDD 红→绿）

| 步 | 内容 | 测试 |
|---|---|---|
| 1 | 迁移（2 表，FORCE RLS + CHECK + 仅追加纪律）+ 2 模型 | 迁移往返 + alembic check + RLS 策略门禁 |
| 2 | `app/harness/feedback.py`（提交/匿名化/快照加密/PII 脱敏服务函数） | 单测（快照加密往返/AAD 绑定/PII 脱敏/匿名化字段清空） |
| 3 | C 端 2 端点 | 集成（幂等重放/归属校验 404/跨租 404/审计） |
| 4 | B 端 4 端点 | 集成（权限边界/状态机/受控解密/游标分页/答复审计） |
| 5 | 匿名化弧 + BS-M4-07 | 真实栈全链：AI 回合→提交→B 端处理→答复→自助删除匿名化 |

## 6. 测试策略

- **单元**：快照加密往返 + AAD 绑定（错 AAD 解密失败）；PII 脱敏（手机号/邮箱/身份证）；
  匿名化字段清空；状态机非法迁移拒绝
- **集成**：真实 PG——C 端提交/幂等/归属 404/跨租 404/审计行；B 端 4 端点权限边界
  （无码 403/跨租 404）+ 受控解密 + 游标分页；匿名化幂等
- **业务场景**：BS-M4-07（真实栈：发布 KB → AI 回合 → 提交反馈 → B 端处理 → 答复 →
  自助删除匿名化 → B 端详情不再展示快照）
- **门禁**：ruff / mypy app+tests / bandit×2 / alembic check（往返实测）+ 受影响子集

## 7. 预判技术债

| 预判 TD | 内容 | 处置方向 |
|---|---|---|
| TD-P1 | B 端反馈页面缺失（后端 API 先行） | UI 批次 |
| TD-P2 | 答复无站内信推送（用户不知情） | M5 消息点集成 |
| TD-P3 | 账号删除全流程未接匿名化（M5 接入） | M5 账号删除点 |
| TD-P4 | reason_tags 枚举固定（无自定义标签） | 看板/统计批次按需扩展 |

## 8. 文档回写清单

design §5.11 端点落地注记 / §4.4 表落地注记 / requirements AI-05 落地注记 /
milestone AI-05 过账（里程碑边界评审）/ handoff §1 + README（合并后 docs PR）。

## 9. 高风险确认

反馈含用户原文（comment/快照）——加密+脱敏+受控展示涉及敏感数据处理；匿名化
涉及数据删除语义；按 §11.7 从严：**简报经用户确认后再编码**。
