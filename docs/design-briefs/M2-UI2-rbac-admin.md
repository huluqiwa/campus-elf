# M2-UI2 — 角色权限配置后台（含 TD-150 先决）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-26 |
| 状态 | **已确认**（2026-10-26，D1~D4 均按推荐方案） |
| 关联功能点 | TD-150（先决）、TD-137、TD-143、TD-126 |
| 验收引用 | `docs/spec.md` RBAC-01/02/03、AUTH-04/05；`docs/milestone.md` §5.2/§5.4；`docs/rbac-matrix.md` §4/§5 |
| 上游 | `docs/design-briefs/M1-E1-rbac-core.md`、`M1-E2-data-scope.md`、`M1-D2-mfa.md` |

---

## 0. 文档与现状核对

1. **迁移 head**：实机 `alembic heads` = `b9d3f7c1e5a9`（M2-ORG06）。`docs/handoff.md` 第 38 行仍写 `f8b2d6a0c4e8`（更早的 M2-ORG04 迁移），属笔误；第 34 行状态表写“M2-ORG04 B 端页面 ⏳ 未开始”与 M2-UI1 已完成矛盾。本点提交时一并回写 handoff/README，不改历史迁移。
2. **TD-150 现状**：`app/rbac/__init__.py` 对非全量型内置角色的补授判据是
   `not exists (select 1 from role_permissions where role_id = r.id)`；数据范围同理
   `not exists (select 1 from data_scopes where role_id = r.id)`。两者都会把“运营主动清空”误判成“从未初始化”。
3. **权限目录**：59 码；租户域白名单 `school`/`campus`；`school.role.manage`、
   `school.permission_account.manage` 已存在；**没有** MFA 重置专用码。
4. **角色模型**：`roles` / `platform_roles` 均无 `defaults_applied`，也无乐观锁 `version`。
5. **开通链路**：`provision_tenant()` / `review_registration_request()` 调用
   `seed_builtin_roles(uuid)`；该函数只读定义表补角色/权限/范围，不写任何“已初始化”标记。
6. **人员接口**：`GET /school/personnel` 返回 `id`（教师/学生档案 id），不返回 `user_id`/`login_id`；
   校方管理员账号不一定有教师/学生档案，因此 MFA 重置不能只挂在人员列表上。
7. **前端**：admin 已有按 `meta.permission` 过滤的菜单与路由守卫；无角色权限页。
8. **范围说明**：`design.md` §5.4 的 `POST /school/permission-accounts`（权限账号）尚未实现，
   M2 退出标准也包含“权限账号/多角色”。本简报的 TD-137 只覆盖“角色↔权限”，
   权限账号（用户↔角色分配、账号停用/删除）单列为 M2-UI3 候选；是否并入本点见 §9-D2。

---

## 1. 目标与不做什么

### 1.1 目标

让校方超管/被授权的管理员在 B 端完成三件事，且全部服务端鉴权、审计、热生效：

1. **改角色权限**：例如“校方授权管理员”默认没有 KB 审批/发布；超管在页面上勾选
   `school.kb.approval` 后，该角色的用户**下一次请求**即可审批，不被踢下线。
2. **重置角色出厂**：例如有人把 `school_admin` 的权限改乱，超管先看影响面预览
   （将加回/撤掉哪些权限、影响多少用户），输入角色名+理由后一键恢复出厂默认。
3. **重置用户 MFA**：例如教师换了手机、恢复码也用完，超管在后台按登录名找到该用户，
   填写理由后清空其 MFA 绑定并强制其重新绑定；该用户所有会话立即失效。

### 1.2 不做什么（本点）

- 不做自定义角色 CRUD（rbac-matrix §6-6：有真实需求再开）。
- 不做“权限账号”完整生命周期（新建校方员工账号、用户↔角色分配、停用/删除账号）——
  候选 M2-UI3；本点只做 MFA 重置所需的用户定位。
- 不做数据范围的图形化编辑（TD-143 重置会恢复出厂范围，但不提供任意增删范围行的 UI）。
- 不做权限缓存（TD-100 仍延后 M6）；继续每请求读库。
- 不做平台侧角色配置 UI（平台角色仍由代码/迁移管理）。

---

## 2. TD-150（先决）：`defaults_applied` 标记

### 2.1 问题场景（必须先用测试钉住）

`school_admin` 出厂默认 6 个权限。新界面允许超管把这 6 个全部取消。
当前同步逻辑看到“该角色 0 条映射”，下一次 `sync_rbac_definitions()`（迁移/启动/运维）会把 6 个默认
全部加回来——运营会认为“撤销没保存/系统有 bug”。只撤最后一条时同理。
数据范围也有同一问题：把某角色唯一一条 `self` 范围删掉，下一次同步会补回。

### 2.2 方案

#### 方案 A（推荐）：`roles.defaults_applied` + `platform_roles.defaults_applied`

- 做法：
  - 迁移新增两列 `defaults_applied boolean not null default false`；同一迁移加
    `roles.version integer not null default 0`（供 TD-137 乐观锁）。
  - **存量回填**：迁移执行时把所有已存在角色（含平台角色）置为 `true`。
    理由：存量角色都已完成过开通/同步，当前状态就是事实；升级瞬间不再补授，避免把
    “升级前刚被清空”的角色又灌回默认。全量型超管仍按“只补不撤”继续对齐新码。
  - 同步语义：
    - 全量型角色（两个超管）：保持“只补缺失”，并确保 `defaults_applied=true`；
    - 其余内置角色：仅当 `defaults_applied=false` 时授出厂默认，授完置 `true`；
    - 数据范围：同一标记控制，`false` 时补出厂范围，补完置 `true`；
    - 自定义角色：不参与默认补授（定义表无记录），创建时直接置 `true`（本点无创建入口）。
  - `seed_builtin_roles(uuid)` 改版：只为 `defaults_applied=false` 的内置角色补默认，
    补完置 `true`；已存在的 `true` 角色不再被“补缺”。
  - 新租户开通：角色插入时默认 `false` → 种默认 → 置 `true`，一次成型。
  - 旧同步测试 `test_sync_reapplies_factory_defaults_when_builtin_role_is_emptied`
    改为断言“清空后同步不再补回”，并新增“标记为 false 的角色恰好补一次”。
- 优点：一列解决“清空 vs 未初始化”；迁移可逆；语义与 TD-150 原文一致；同时给 TD-137 提供版本号。
- 缺点/成本：改同步 SQL、开通函数、迁移回填与测试；`defaults_applied` 是运维可见的新概念。
- 风险：存量一律 `true` 会让“历史上从未初始化成功的空角色”不再自动修复；由 TD-143 重置提供补救路径。
- 对 spec/design 的影响：`design.md` §4.2 角色表补两列说明；`rbac-matrix.md` §5 补“清空不再回填”的口径。

#### 方案 B：独立标记表 `rbac_role_defaults_state`

- 做法：`(realm, role_id, applied_at)`；同步据此判断。
- 优点：不动 `roles` 热表；可保留“何时初始化”的时间。
- 缺点/成本：多一张租户级表（RLS + 复合外键 + 授权 + 覆盖门禁），同步多一次 join；
  平台/租户两套键；清理与角色删除级联要额外处理。
- 风险：新表漏 RLS/授权会被 `test_rls_policy_coverage.py` 拦下，成本高于收益。
- 对 spec/design 的影响：新增表需进 design §4.2 与隔离清单。

#### 方案 C：同步不再给存量角色补默认

- 做法：删除“非全量角色补默认”逻辑，只有新租户开通时种一次。
- 优点：最小改动，彻底消除“白撤”。
- 缺点：升级后新增的默认权限码永远到不了存量角色；运营无法通过“重置”恢复（除非另做 TD-143）；
  与“只补缺失、不覆盖人工改动”的既有口径冲突。
- 风险：把配置问题变成“版本升级后权限静默缺失”。
- 对 spec/design 的影响：需改 rbac-matrix §5 同步口径。

### 2.3 选择结论

选 **A**。B 的复杂度（新表+RLS+平台/租户双键）换来的“初始化时间”本点用不到；C 会让存量租户
永久落后于代码默认。A 同时满足 TD-150 原文与 TD-137 的并发控制需要。

---

## 3. TD-137：角色↔权限分配接口与配置页

### 3.1 方案

#### 方案 A（推荐）：只编辑“可编辑内置角色”的权限集

- 可编辑：`school_admin`、`teacher_b_end`。
- 只读/锁定：
  - `school_super_admin`：矩阵定义“本校全部权限（school+campus）”，锁定防止自锁；
  - `student` / `teacher`：C 端基础角色，锁定防止把登录/聊天/请假入口改坏。
- 不做自定义角色与用户↔角色分配。
- 接口：
  - `GET /api/v1/school/roles`（`school.role.manage`）
    → `[{id, code, name, is_system, editable, lock_reason, version, defaults_applied,
        user_count, permission_codes[]}]`
  - `GET /api/v1/school/permissions`
    → 租户域 43 码（school 34 + campus 9），按 module/action 分组，含中文描述。
  - `PUT /api/v1/school/roles/{role_id}/permissions`
    body `{permission_codes: string[], version: int, reason?: string}`；**全量替换**。
    - 未知码/平台域码 → 422；
    - 跨租 role_id → 404；
    - 锁定角色 → 409 `ROLE_IMMUTABLE`；
    - `version` 不匹配 → 409 `ROLE_VERSION_CONFLICT`（返回最新角色快照）；
    - 成功：替换映射、`version+1`、同事务写审计
      `school.role.permissions.updated`（before/after/added/removed/reason）。
- 热生效：Principal 每请求读库，无需撤销会话；测试断言“撤销后下一请求 403、会话仍有效”。
- 优点：范围收敛、风险最低；覆盖 TD-137 原文；锁定规则把“把自己锁死”降到只剩
  `school_admin` 自撤 `school.role.manage`（超管仍在，可恢复）。
- 缺点/成本：管理员不能自定义角色；用户↔角色分配仍靠 SQL/后续功能点。
- 风险：`school_admin` 可撤掉自己的管理权限；页面在保存前提示，审计留痕，超管可恢复。
- 对 spec/design 的影响：`design.md` §5.4 补 3 个接口；rbac-matrix §4 补“可编辑/锁定”说明。

#### 方案 B：A + 权限账号（用户↔角色分配 + MFA 重置列表）

- 做法：同页增加“权限账号”页签：搜用户、分配 B 端角色、重置 MFA、停用账号。
- 优点：一次补齐 M2 退出标准“权限账号/多角色”；TD-126 有天然 UI。
- 缺点/成本：涉及账号创建/停用/角色分配/最后超管保护/会话撤销，工作量约为 A 的 2 倍；
  与本点“角色权限配置”主题混杂，审查面变大。
- 风险：账号停用语义与 DEL-01~05 状态机重叠（M5 才交付），容易做半套。
- 对 spec/design 的影响：`design.md` §5.4 的 `POST /school/permission-accounts` 需完整定义。

#### 方案 C：只做后端接口，不做页面

- 做法：交付 3 个接口 + 审计，页面留 M2-UI3。
- 优点：最快；安全面小。
- 缺点：TD-137 明确要求“接口 + 配置页”；运营仍无法自助。
- 风险：不满足验收。
- 对 spec/design 的影响：无。

### 3.2 选择结论

推荐 **A**（本点）。B 的账号生命周期与 M5 删除状态机强相关，硬塞进本点会制造半成品；
权限账号/多角色另开 M2-UI3，并在 milestone 中保留退出标准。C 不满足 TD-137。

---

## 4. TD-143：内置角色重置出厂

### 4.1 方案

#### 方案 A（推荐）：权限 + 数据范围一起恢复，预览 + 原因 + 输入角色名

- 仅对 §3 的“可编辑角色”开放；锁定角色返回 409（超管本来就是全量，C 端角色无重置意义）。
- `POST /api/v1/school/roles/{role_id}/reset/preview`
  → `{permissions: {added[], removed[]}, scopes: {added[], removed[]},
      affected_users: n, version}`
  只读，不写库、不审计（或写一条只读审计可选）。
- `POST /api/v1/school/roles/{role_id}/reset`
  body `{confirm_role_code: string, reason: string(≥10), version: int}`。
  - `confirm_role_code` 必须等于角色 code（防误点）；
  - 事务内：删除该角色全部 `role_permissions` 与 `data_scopes(role_id=...)` 行 →
    按 `rbac_builtin_role_defaults` / `rbac_builtin_role_scope_defaults` 重种 →
    `defaults_applied=true`、`version+1` → 审计 `school.role.reset`
    （before/after/added/removed/affected_users/reason）；
  - 不撤销用户会话（权限下一请求生效）。
- 优点：真正的“出厂状态”；预览让管理员知道会影响谁；输入角色名避免一键误清。
- 缺点/成本：数据范围也重置，可能改变数据可见性；需要预览接口多一次查询。
- 风险：重置会覆盖运营对范围的收窄；预览与确认文案必须明确列出范围变化。
- 对 spec/design 的影响：`design.md` §5.4 补接口；rbac-matrix §5 补重置口径。

#### 方案 B：只重置权限，不动数据范围

- 优点：影响面更小，避免“突然看不到数据”。
- 缺点：“出厂默认”不完整；数据范围仍可能处于被清空状态。
- 风险：运营以为已恢复出厂，实际范围仍是坏的。
- 对 spec/design 的影响：需明确“出厂=仅权限”的例外口径。

#### 方案 C：只提供命令行/运维脚本

- 优点：无 UI 风险。
- 缺点：不满足 TD-143“接口 + 二次确认 + 影响面预览”。
- 风险：误操作只能改库。

### 4.2 选择结论

推荐 **A**。若用户更在意“不改变现有数据可见性”，可选 B；但需在 spec 里写清例外。

---

## 5. TD-126：租户侧 MFA 管理员重置

### 5.1 方案

#### 方案 A（推荐）：新增专用权限码 `school.personnel.mfa.reset`

- 权限码：新增第 60 个码，module=`school`，action=`reset`，描述“管理员重置用户 MFA”。
  - 校方超管由“school 模块全量”自动获得；
  - `school_admin` 出厂默认**不含**（△ 可授予），符合“丢设备后由超管兜底”的最小授权。
  - `rbac-matrix.md` §3/§4 与 `test_rbac_matrix_unit.py` 的 59→60 同步更新。
- 接口：
  - `POST /api/v1/school/users/{user_id}/mfa/reset`
    body `{reason: string(≥10)}`。
  - 目标定位：本点只提供按 `login_id` 精确查询的只读接口
    `GET /api/v1/school/users/lookup?login_id=...`（同权限码；返回 `id/display_name/user_type/
    status/mfa_enabled/roles[]`，不回显邮箱/身份证/密码/MFA 密钥）。
    UI 先输入登录名 → 确认目标 → 填理由 → 重置。
- 服务端行为（复用平台 `reset_platform_mfa` 的口径）：
  - 租户内查询用户，跨租/不存在统一 404；
  - 目标必须是租户用户；平台账号不可经此接口；
  - 目标必须 `mfa_enabled=true`，否则 409 `MFA_NOT_ENABLED`；
  - 不得重置自己（409 `MFA_SELF_RESET`，自助走恢复码/关闭 MFA）；
  - 可选保护：目标若持有 `school_super_admin`，操作者也必须是超管（本点先禁止重置超管，
    由平台 CLI 兜底；理由写入设计）；
  - 事务内：用户级锁 → 撤销全部 refresh 家族（`admin_revoke`）→ 删除恢复码 →
    清 `mfa_enabled/mfa_secret_enc/mfa_last_used_step/failed_login_attempts/locked_until` →
    `token_version+1` → 审计 `school.user.mfa_reset`（操作者/目标掩码/原因/撤销会话数）；
  - 提交后删除 Redis 会话标记与待激活 secret（失败仅告警，不回滚 DB）；
  - 响应 204；日志/审计不含 TOTP secret/恢复码。
- 优点：最小权限；审计清晰；与平台 CLI 同一语义；不新增用户列表页。
- 缺点/成本：新增权限码要改矩阵与测试；按登录名精确查询对超管可用，但不是完整账号管理。
- 风险：管理员可重置超管 MFA 的路径已禁止；按 login_id 查询可枚举（需权限+审计+限流，限流列 TD）。
- 对 spec/design 的影响：新增权限码进 rbac-matrix；`design.md` §5.4 补接口；spec AUTH-04 补验收。

#### 方案 B：复用 `school.permission_account.manage`

- 优点：不加权限码，矩阵不动。
- 缺点：能改角色的人自动能重置任意人 MFA，权限粒度过大；审计主体难区分。
- 风险：角色配置管理员可借 MFA 重置接管账号（仍需密码，但风险面扩大）。
- 对 spec/design 的影响：无。

#### 方案 C：只在人员详情页给教师/学生重置，校管账号走平台 CLI

- 优点：改动最小，复用现有人员页。
- 缺点：不满足 TD-126 的“校管/员工”；校方管理员丢设备仍需平台介入。
- 风险：验收缺口。

### 5.2 选择结论

推荐 **A**。B 违反最小权限；C 不满足 TD-126 范围。

---

## 6. 数据与接口清单

### 6.1 迁移（一个新 revision，父 `b9d3f7c1e5a9`）

1. `roles.defaults_applied boolean not null default false`
2. `platform_roles.defaults_applied boolean not null default false`
3. `roles.version integer not null default 0`
4. 存量回填：两张角色表全部 `defaults_applied=true`（理由见 §2.2）
5. 重建 `seed_builtin_roles(uuid)`：按标记补默认并置 `true`（含权限与范围）
6. downgrade：删除三列 + 恢复旧函数体（幂等）

### 6.2 接口

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/v1/school/roles` | `school.role.manage` | 角色列表 + 权限码 + 版本 + 用户数 |
| GET | `/api/v1/school/permissions` | `school.role.manage` | 租户域权限目录 |
| PUT | `/api/v1/school/roles/{id}/permissions` | `school.role.manage` | 全量替换 + 版本锁 |
| POST | `/api/v1/school/roles/{id}/reset/preview` | `school.role.manage` | 重置预览 |
| POST | `/api/v1/school/roles/{id}/reset` | `school.role.manage` | 重置出厂 |
| GET | `/api/v1/school/users/lookup` | `school.personnel.mfa.reset` | 按登录名精确定位 |
| POST | `/api/v1/school/users/{id}/mfa/reset` | `school.personnel.mfa.reset` | 重置 MFA |

### 6.3 审计 action

- `school.role.permissions.updated`
- `school.role.reset`
- `school.user.mfa_reset`

全部同事务写入；失败回滚业务改动。

### 6.4 前端

- 路由 `/settings/roles`，菜单名“角色权限”，`meta.permission=school.role.manage`。
- 角色列表（左侧/上方）+ 权限分组勾选（school/campus 按模块折叠）+ 保存确认
  （显示新增/移除数量）+ 重置按钮（预览弹窗 → 输入角色名+理由）。
- “MFA 重置”入口：同页“安全工具”卡片或独立小页；输入登录名查询 → 展示目标 →
  填理由 → 确认。只对 `school.personnel.mfa.reset` 显示。
- 保存成功后若当前用户角色受影响，调用 `auth.loadUser()` 刷新菜单/权限。
- 锁定角色显示只读原因；不允许勾选。

---

## 7. 实现顺序与 TDD 清单

1. **数据/迁移**：新增列 → 回填 → 新 `seed_builtin_roles` → 迁移往返测试。
2. **同步语义**：改 `build_sync_statements` → 更新旧测试 → 新增“清空不回填/补一次”测试。
3. **TD-137 后端**：角色/权限查询 → 全量替换 → 版本冲突 → 锁定/越权/未知码测试 → 热生效业务场景。
4. **TD-143 后端**：预览 → 重置 → 原因/角色名/版本/审计/影响面测试。
5. **TD-126 后端**：权限码与种子 → lookup → 重置 → 会话撤销/审计脱敏/跨租/自我保护测试。
6. **前端**：api-client 类型与单测 → 角色权限页 → 重置弹窗 → MFA 重置 → mock E2E。
7. **文档回写**：handoff §1、README 状态、design §4.2/§5.4、rbac-matrix §3/§4/§5、
   spec AUTH-04/RBAC-01 验收、tech-debt TD-126/137/143/150 处置、review-log。

### 7.1 红→绿用例（最小集）

- [ ] 单元：权限码新增后种子自洽（租户角色不越域）；锁定角色判定；版本冲突映射。
- [ ] 集成：迁移回填后所有角色 `defaults_applied=true`；同步清空不回填；标记 false 补一次。
- [ ] 集成：全量替换写入/删除行 + 审计 diff；跨租 404；平台域码 422；版本 409。
- [ ] 集成：重置预览不写库；重置恢复权限+范围；审计含影响用户数；downgrade/upgrade 往返。
- [ ] 集成：MFA 重置清字段/撤家族/`token_version+1`/审计无 secret；非 MFA 409；自己 409；
      超管目标 403/409；跨租 404；下一请求旧 access 失效。
- [ ] 业务场景：管理员 A 撤销角色 X 的 `school.kb.approval`，角色 X 的用户 B 下一请求审批 403，
      会话仍可访问其他有权限接口；超管恢复后 B 下一请求恢复 200。
- [ ] 业务场景：老师 C 丢 MFA，超管重置后 C 用密码登录进入“需重新绑定 MFA”流程，
      旧刷新令牌全部失效。
- [ ] 前端单测：权限分组渲染、保存 diff、409 冲突提示、锁定角色禁用、重置预览/确认校验。
- [ ] Playwright mock：角色页加载 → 勾选保存 → 提示成功 → 重置预览/确认 → MFA 查询/重置。
- [ ] 全栈 E2E：待 TD-183 的 8000 端口释放后，补一条“改权限 → 目标用户下一请求生效”。

---

## 8. 测试策略

| 层级 | 内容 |
|---|---|
| 单元 | 角色可编辑判定、权限码过滤、版本冲突、重置 diff 计算、MFA 目标校验 |
| 集成 | 迁移回填、同步语义、接口写库/审计/RLS、跨租、Redis 会话撤销 |
| 业务场景 | 权限热生效（不加踢）、重置出厂可恢复、MFA 重置后重新绑定 |
| E2E | admin mock 主路径；全栈待 TD-183 端口释放 |
| AI 评估 | 不适用 |
| 性能 | 不适用（管理面低频）；角色列表用户数用聚合查询，避免 N+1 |

---

## 9. 已确认决策（2026-10-26）

- **D1（TD-150）**：采用**单标记** `defaults_applied`，同时管权限与数据范围；存量角色一律回填 `true`。
- **D2（TD-137 范围）**：本点只做“角色权限 + 重置 + MFA 重置”；权限账号/多角色另开 M2-UI3。
- **D3（TD-143）**：重置同时恢复权限与数据范围。
- **D4（TD-126 权限码）**：新增 `school.personnel.mfa.reset`（59→60 码）。

---

## 10. 预判技术债

| 编号 | 条目 | 处置 |
|---|---|---|
| 新增 | MFA 重置的登录名查询无限流，存在枚举/滥用面 | 已延后 M6（与 TD-121/128 网关限流合并） |
| 新增 | 自定义角色 CRUD 仍缺 | 已延后（rbac-matrix §6-6：有真实需求再开） |
| 新增 | 权限账号/多角色生命周期（创建/停用/删除） | 已采纳 M2-UI3 |
| TD-100 | 权限每请求读库，无缓存 | 仍延后 M6（本点不引入缓存） |
| TD-151/152 | 数据范围 class/course 过滤与人员页面入口 | 维持原处置 |

---

## 11. 高风险确认

- 是否属于高风险功能：**是**（权限、租户隔离、会话撤销、MFA）。
- 需要用户确认：D1~D4；锁定 `school_super_admin`/`student`/`teacher` 是否接受；
  重置是否连数据范围；MFA 重置是否禁止超管目标（本简报建议禁止，平台 CLI 兜底）。
- 确认结论（2026-10-26）：D1~D4 均按推荐方案；锁定 `school_super_admin`/`student`/`teacher`
  与“禁止重置超管 MFA（平台 CLI 兜底）”一并采纳。

---

## 12. 审查对照

- 审查时重点验证：
  - 同步不会把人工清空的权限/范围补回；升级不改变存量配置。
  - 全量替换不会写入平台域/跨租权限；锁定角色不可改；版本冲突不静默覆盖。
  - 重置的预览与执行一致；审计含 before/after/影响面；失败全回滚。
  - MFA 重置不能跨租、不能重置平台账号、不能重置自己/超管；审计无 secret；
    `token_version` 与刷新家族撤销同事务；Redis 清理失败不阻断 DB 结果。
  - 权限变更下一请求生效且不踢会话。
- 允许的偏差：`POST /school/permission-accounts` 不在本点；自定义角色不在本点；
  数据范围图形化编辑不在本点。
