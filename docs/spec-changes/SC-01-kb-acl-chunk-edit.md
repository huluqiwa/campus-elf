# SC-01 文档查阅人群（ACL）与切块编辑 · Spec/Design 变更草案

- 状态：**已确认（2026-10-21）**——组织模型选 **方案 A**（独立 `departments` 树 + `classes.grade`）；§6 十条假设无异议；已回写 `requirements.md` / `spec.md` / `design.md` / `milestone.md` / `test-plan.md` / `rbac-matrix.md` / `state-machines.md` / `tenant-isolation.md` / `tech-debt.md`
- 提出日期：2026-10-21
- 影响里程碑：M2（+1 功能点）、M3（+2 功能点）、M4（+1 功能点）、M1-E（权限种子预留）
- 不影响：M0、M1-A、M1-B 已交付代码；M1-C / M1-D 顺序不变

## 1. 用户已确认的决策

| 编号 | 决策 | 结论 |
|---|---|---|
| K4 | 人群模型 | **C**：部门/科室 + 角色 + 班级 + 课程 + 指定人（+ 显式"全体"选项） |
| K4 | 默认可见性 | **fail-closed**：版本无 ACL 记录 = 不可检索；构建/发布前必须显式选择人群 |
| K5 | 块编辑与审批 | **A**：编辑生成派生版本 → 校方超管轻量确认 + 审计（不重走平台审批） |
| K5 | 编辑粒度 | **A**：MVP 仅支持编辑**子块文本**；不支持增删块/改父块归属 |

新增决策编号：**A30**（M2 部门组织单元）、**K4**（文档查阅人群 ACL）、**K5**（切块编辑与派生重建）。编号沿用现有系列，若希望改号请指出。

## 2. 文档查阅人群（ACL）

### 2.1 语义（对现有锁定的修订）

- ACL 是**版本级**授权：`kb_document_audiences` 记录"允许查阅该版本的人群"，多条并集。
- ACL 只约束 **AI 检索/引用** 与 **C 端问答**；B 端知识库管理界面仍由 `school.kb.view` + 数据范围控制，ACL 在界面上只作为展示标签（见 §6 假设 1）。
- **fail-closed**：版本 ACL 记录为 0 条 → 构建与发布接口返回 `409 ACL_REQUIRED`；检索过滤要求至少命中一个 ACL token。
- **ACL 变更不受"旧会话固定版本"保护**：安全策略始终取最新值；旧会话只固定内容版本（见假设 2）。
- 新版本创建时**复制**上一版本 ACL 但置 `acl_confirmed=false`，构建前需重新确认（避免静默沿用）；派生版本（块编辑）沿用源版本已确认 ACL。
- 无权用户的行为：**不召回、不引用、按意图逐项兜底话术**，不提示"存在但无权"（避免泄漏文档存在性）。
- 不做逐请求"无权命中"审计：过滤在 Qdrant 层完成，应用无法得知被过滤内容（见假设 8）。沙箱提供"忽略 ACL 的对比视图"（超管 + 审计），用于校方自检。

### 2.2 人群模型与组织数据（M2，新功能点 ORG-05）

候选方案：

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| A（推荐） | 新增独立 `departments` 树（部门/科室，支持层级）；`classes` 增加 `grade` 字段；人群类型 = department/role/class/grade/course/user/all | 不动 M2 既有班级/课程设计；改动面最小 | 三套层级并存（部门树、年级、班级） |
| B | 统一 `org_units` 树（type=department/grade/class），班级/任职改引用 `org_unit_id` | 层级统一、人群继承自然（选年级自动覆盖班级） | 改动 M2 既有设计，迁移与实现成本更高 |

推荐 **A**。若你希望"选年级自动覆盖其下所有班级"由数据库层级保证，则选 B。

`departments` 草案：`id, tenant_id, parent_id, name, code?, path, status, created_at, updated_at`；复合外键 `(parent_id, tenant_id) → (id, tenant_id)`；唯一约束 `(tenant_id, parent_id, name)`；教师任职关系（M2 既有）增加 `department_id`。

### 2.3 数据模型（M3）

`kb_document_audiences`

| 字段 | 说明 |
|---|---|
| id, tenant_id | 复合外键 `(version_id, tenant_id) → kb_document_versions(id, tenant_id)` |
| version_id | 绑定文档版本 |
| audience_type | `department / role / class / grade / course / user / all` |
| audience_id | `all` 时为空，其余必填（多态引用，同租户由触发器校验 + 审计） |
| created_by, created_at | 审计 |

CHECK：`(audience_type='all' AND audience_id IS NULL) OR (audience_type<>'all' AND audience_id IS NOT NULL)`；UNIQUE `(version_id, audience_type, audience_id)`；索引 `(audience_type, audience_id)`（C 端按用户人群反查）。

`kb_document_versions` 增加：`acl_confirmed_by, acl_confirmed_at`。

### 2.4 检索强制过滤（M3 + M4）

- 构建时把版本 ACL 写入每个 chunk 的 Qdrant payload：`audiences: ["department:<uuid>", "role:<uuid>", ...]`、`version_id`、`is_human_edited`。
- 检索过滤：`must` 条件 + `min_should: 1`（用户人群 token 至少命中一个）；未命中即不召回。
- 用户人群集合由**服务端**按登录身份解析，前端不传（沿用"不信任前端 tenant_id/role"原则）：
  - B 端用户：所属部门 + 角色 + 任课/班主任/辅导员班级 + 授课课程 + 自身；
  - C 端学生：班级 + 年级 + 自身；
  - 平台用户：不参与租户知识库检索（跨租红线不变）。
- 解析结果短 TTL 缓存，组织/权限变更时失效；解析失败或无匹配 → 空集（fail-closed）。
- 检索测试沙箱：`retrieval-test` 增加 `as_user_id` / `as_audience`（默认按操作者自身），超管可选"忽略 ACL"并强制审计。

### 2.5 权限码与接口

| 权限码 | 授予 |
|---|---|
| `school.kb.acl.manage` | 校方超管、授权管理员（配置 ACL） |
| `school.kb.retrieval_test.unfiltered` | 仅校方超管（沙箱忽略 ACL 对比视图，强制审计） |

接口：`GET/PUT /school/kb/document-versions/{id}/audiences`；`POST .../retrieval-test` 增加人群视角参数；`POST .../build` 与 `POST .../publish` 增加 `409 ACL_REQUIRED`（未确认 ACL）与 `409 ACL_UNCONFIRMED`。

审计：`kb.acl.updated`（变更前后值）、`kb.acl.confirmed`、`kb.retrieval_test.unfiltered`。

## 3. 切块编辑与派生重建（K5 → M3 新功能点 KB-10）

### 3.1 流程

```text
已审批/已发布版本 v1
  → 编辑子块文本（多次，草稿态，带 base_hash 乐观锁）
  → 生成派生版本 v1.1（derivation_type=chunk_edit, parent_version_id=v1，复制全部分块）
  → 校方超管轻量确认（confirm + 审计；不重走平台审批）
  → 派生构建：COPY（复用缓存）→ EMBED（仅改动块）→ INDEX → VALIDATE → READY
  → 测试沙箱（含人群视角）
  → 发布 alias 切流；旧会话继续固定 v1
```

- 允许在源版本状态为 `APPROVED / TEST_PASSED / PUBLISHED` 时派生；草稿/审批中版本直接改块（仍在原版本内）。
- 派生版本沿用源版本 ACL；如校方同时修改 ACL，需重新确认 ACL。
- 失败可重试，失败版本不影响源版本与线上 alias。

### 3.2 数据模型

- `kb_chunk_edits`：`id, tenant_id, source_version_id, chunk_id, before_hash, after_hash, new_content, edited_by, reason(必填), status(draft/submitted/confirmed/discarded), derived_version_id, created_at`。
- `kb_document_versions` 增加：`parent_version_id`、`derivation_type('initial','chunk_edit')`。
- chunk 表/payload 增加：`is_human_edited, edited_by, edited_at`。
- 向量复用缓存：以 `(tenant_id, model, chunk_hash)` 为键存 dense/sparse 向量（建议对象存储 + 元数据表；表结构在 M3 设计简报定），派生构建仅对改动块重新推理。

### 3.3 API 与权限

| 权限码 | 授予 |
|---|---|
| `school.kb.chunk.edit` | 校方授权管理员、超管（提交块编辑） |
| `school.kb.chunk.confirm` | 仅校方超管（确认派生并构建） |

接口：`POST /school/kb/chunks/{chunk_id}/edits`（带 `base_hash`，冲突返回 `409 CHUNK_CONFLICT`）；`GET /school/kb/document-versions/{id}/chunk-edits`（diff）；`POST /school/kb/document-versions/{id}/derive`（确认并派生）。

校验：非空、长度上限（切块参数 M3 实验后定）、Markdown 结构完整（表格/代码块闭合）、`chunk_hash` 与 token 数重算；父块归属与 chunk 数量不变。

审计：`kb.chunk.edit.submitted`（chunk_id、before/after hash、理由；不落原文）、`kb.chunk.edit.confirmed`（确认人）、`kb.version.derived`（parent/derived、改动块数）、发布审计记录派生链。

## 4. 对既有锁定规则的修订建议

| 位置 | 现有表述 | 修订为 |
|---|---|---|
| `spec.md:201`、`spec.md:480` | 内容修改生成新版本并重审；标签/描述免审但审计 | **原文/结构变更**（重传、重切块、增删块）→ 新版本 + 重审；**块级文本校正** → 派生版本 + 校方超管轻量确认 + 审计；标签/描述/ACL 变更 → 免审但强制审计（ACL 需重新确认） |
| `design.md` §5.5 / §5.7 | 分块预览只读；构建门禁=审批通过 | 增加：块编辑与派生构建 pipeline；构建/发布门禁 =（审批通过 or 派生确认通过）AND ACL 已确认 |
| `design.md:679` | Qdrant 每租户 collection + payload 校验 | 明确 payload 含 `audiences/version_id/is_human_edited` 与 `min_should:1` 过滤语义 |
| `milestone.md` | M2 ORG-01～04；M3 KB-01～08；M4 AI-01～09 | M2 **ORG-05**；M3 **KB-09 / KB-10**；M4 **AI-10**（身份→人群解析与过滤） |
| `test-plan.md` | BS-09～15 | 新增 **BS-16**（ACL fail-closed：未配 ACL 构建/发布 409）、**BS-17**（跨人群拒绝：财务文档对非财务用户不召回/不引用/兜底）、**BS-18**（块编辑→派生→确认→构建→测试→发布→新旧会话版本固定）、**BS-19**（并发编辑冲突 409 + 审计完整性） |

## 5. 影响面与工作量

- **零改动**：M0、M1-A、M1-B（表、RLS、三连接、注册审核均不受影响）。
- **M1-E（紧接着的功能点）**：权限种子增加 4 个权限码（`school.kb.acl.manage`、`school.kb.chunk.edit`、`school.kb.chunk.confirm`、`school.kb.retrieval_test.unfiltered`）；数据范围不动（ACL 独立于 `data_scopes`）。**M1-E 设计简报会预留 ACL 维度**，避免 M2/M3 返工。
- **M2**：+1 功能点（ORG-05 部门/科室与教师归属、导入与错误报告）。
- **M3**：+2 功能点（KB-09 ACL 与检索过滤；KB-10 块编辑与派生重建），含 B 端 UI（人群选择器、块编辑/diff、人群视角检索测试）。
- **M4**：+1 功能点（AI-10 身份→人群解析、无权兜底、引用不泄漏）。
- **测试/评估**：4 条业务场景 + AI 评估新增"越权泄漏"对抗集（≥3 条）。
- 粗估：新增 4 个功能点量级；因知识库尚未实现，属计划内追加，不产生推翻式重构。对 M1-C/M1-D 无影响。

## 6. 显式假设（不同意请指出，否则按此实现）

1. ACL 只约束 AI 检索/引用与 C 端问答；B 端 KB 管理界面按 `school.kb.view` + 数据范围控制，ACL 仅作展示标签。
2. ACL 变更即时生效，且不受"旧会话固定版本"保护（安全策略优先）。
3. 新版本默认继承上一版本 ACL 但需重新确认；派生版本沿用源版本已确认 ACL。
4. 允许显式"全体"人群选项（`audience_type='all'`），但必须显式保存并审计，不能作为默认值。
5. MVP 不支持增删块、改父块归属或调整父子结构；这类变更走"重传文档/重切块 + 重审"。
6. 允许"编辑人 = 确认人"（MVP 小团队现实），审计分别记录编辑人与确认人。
7. 部门树支持层级；学生人群用班级/年级，教职工人群用部门 + 角色。
8. 不逐请求审计"无权命中"；沙箱提供"忽略 ACL"对比视图（超管 + 审计）。
9. Qdrant 保持"每租户 + 每版本 collection"；派生版本产生新 collection，未改块向量复用（存储成本上升，M3 实现时评估并登记 TD）。
10. 派生构建跳过解析（MinerU）与切块阶段，只做复制/局部重嵌/索引/校验。

## 7. 确认后要做的文档回写（本次不动）

1. `docs/requirements.md`、`docs/spec.md`：新增 A30/K4/K5 决策与修订条款（§4 表）。
2. `docs/design.md`：§5.5/§5.7 流程与接口、数据模型（3 张新表 + 2 处字段扩展）、检索过滤语义、权限码、审计事件、状态机引用。
3. `docs/milestone.md`：ORG-05、KB-09/KB-10、AI-10，及各自验收标准。
4. `docs/test-plan.md`：BS-16～BS-19 与 AI 对抗评估条目。
5. `docs/rbac-matrix.md`、`docs/state-machines.md`、`docs/tenant-isolation.md`：权限矩阵、版本/编辑状态机、ACL 跨租拒绝。
6. `docs/tech-debt.md`：登记派生版本 collection 存储成本、无权命中审计缺失等条目。
