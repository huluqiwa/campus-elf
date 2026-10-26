# M2-UI3 — 自定义角色 / 多角色绑定 / 权限账号 实现前设计简报

对应任务：TD-187（自定义角色 CRUD、单用户多角色绑定、按人授权）+ TD-191（转授禁令收口）+ spec §3.4 RBAC-01 与 §3.5 验收「删除权限账号后该账号无法登录、既有 token 失效、审计写入」+ requirements.md「创建/移除权限账号，支持多权限分配」。
决策日期：2026-10-26（用户选定下一个功能点为 M2-UI3）。

---

## 0. 现状核对（读代码得到的事实，不是设想）

| 事项 | 现状 | 影响 |
|---|---|---|
| `user_roles` 表 | 已建（`uq_user_roles_user_id_role_id`，两条复合外键保证同租，CASCADE） | 数据面就绪 |
| Principal 解析 | `app/auth/principal.py:164-196` 已按**多角色并集** `select distinct p.code ... join user_roles` 取权限；解析不到范围行 = 空集 fail-closed | 多角色绑定无需改鉴权内核 |
| 谁写 `user_roles` | **没有任何写入路径**（全仓只有 `platform_user_roles` 在开通时被写） | 内置角色 `school_admin`/`teacher_b_end` 目前无人绑定 → 「配了权限也授不出去」，这是本点的主线缺口 |
| `school.permission_account.manage` | 在 `app/rbac/catalog.py:68` 已声明（"开/停 B 端权限账号"），**无任何接口引用** | 权限账号管理接口直接用它，不必新增码 |
| 账号创建 | 随教师/学生建档发生（`POST /school/teachers`→`app/org/service.py:705 create_teacher`：随机初始密码 + `must_change_password=true`，`initial_password` 只在响应里出现一次、不落日志/审计） | 「创建权限账号」已具备；本点不重复造 |
| 账号停用 | 无接口。`users.status` 字段存在，登录侧已按 `status != 'active'` 拒绝（`app/auth/service.py:949/963/968`） | 只需写状态 + 复用既有会话撤销链路即可满足 spec 验收 |
| 自定义角色 | `roles.is_system` 字段存在，语义未定义；M2-UI2 对自定义角色返回 409 `ROLE_IMMUTABLE` + `lock_reason="待 M2-UI3"` | 本点把它变成可创建/可编辑/可删除 |
| 数据范围 | `data_scopes` 允许绑 role 或 user（CHECK `scope_binding`）；**`class` 范围行由 `app/org/scopes_sync.py` 按「任教 ∪ 班主任 ∪ 辅导员」做期望-实际差量同步**，且必须在关系变更同事务内调用 | 手工按人授 `class` 范围会被下一次同步当差量删掉 → 二者互斥，见 §5 |
| 出厂默认 | `defaults_applied`（TD-150）只对 `is_system` 角色有意义；`rbac_builtin_role_defaults` 是代码持有的定义表 | 自定义角色不参与同步与重置出厂 |
| TD-191 | 禁令只封 `school.role.manage` 这个码的扩散；已持该码者仍可把敏感码写进自己所在角色 | 本点多角色化后风险面扩大，必须一并处置 |

---

## 1. 目标与不做什么

### 1.1 目标

1. **自定义角色 CRUD**：租户内创建/编辑/删除非内置角色，权限集仍只能从代码持有的 60 码目录中选（`school`/`campus` 模块；决策 #6 把目录扩到 61 码），不接受平台域码。
2. **用户↔角色绑定**：给某个账号绑定/解绑多个角色（多角色并集生效），影响面预览 + 版本 CAS + 审计。
3. **权限账号开/停**：停用一个 B 端权限账号使其**立刻不能登录、既有 token 全部失效**；可重新启用。
4. **TD-191 收口**：把"自扩权"从隐式变成显式、可复盘的动作（具体方式见 §6 待拍板）。

### 1.2 不做什么（本点）

- 不做权限模板 / 角色继承 / 互斥角色约束。
- 不做平台侧角色的自定义（`platform_roles` 保持代码驱动）。
- 不做按人直授单个权限码（见 §2 方案 A 的否决理由）。
- 不做账号删除（硬删 `users` 行会 CASCADE 掉档案，与 ORG「档案保留」语义冲突，且属 M5 账号删除状态机）。
- 不做 M6 的配置双人确认；若 §6 选「二次确认」方案，只落"单步显式确认 + 审计"，不伪装成双人复核。

---

## 2. 决策一：「多权限分配」的数据模型

### 方案 A：角色是唯一权限载体，按人只绑角色（推荐）

「权限账号」= 绑定了 ≥1 个角色的账号；「多权限分配」= 一个账号绑多个角色，权限取并集。

- 优点：与 `principal.py` 现有多角色并集解析零改动对接；审计口径单一（谁被授了什么 = 绑了哪些角色）；M2-UI2 已经建好的「全量替换 + `version` CAS + 差异预览 + 独立事务拒绝审计」模式可直接复用到绑定接口；`data_scopes` 的角色级语义不变。
- 缺点：想给某个人一项临时权限，要为他建一个角色。
- 缓解：提供 `copy_from_role_id`（从既有角色克隆权限集）把建角色成本降到一次点击；真正的"按人临时授权"登记 TD 待 M6 与审批引擎一起做。
- 对 spec 的影响：`requirements.md:184`「支持多权限分配」按"多角色"解释——需求原文未指定载体，需在 design.md 明确口径。

### 方案 B：新增 `user_permissions` 表按人直授

- 优点：字面满足"按人分配权限"，无需建角色。
- 缺点：RBAC 变成双轨（角色 ∪ 直授），「这个人到底有什么权限」要查两处；`Principal` 解析、审计、受控查看、M2-UI2 的 CAS/预览都要再复制一套；权限矩阵（§4，角色×权限）不再能核验实际能力，越权排查成本上升。
- 风险：直授权限天然绕过"重置出厂"与"角色审计"，安全事件复盘会漏项。
- 对 spec 的影响：需要新增"直授权限矩阵 + 独立审计动作"，属实质性扩范围。

### 方案 C：给每个权限账号自动建一个隐式 personal role

- 优点：仍是单轨，"按人"直观。
- 缺点：角色列表被隐式角色淹没，`is_system`/`editable`/`defaults_applied`/重置出厂每条逻辑都要多一个分支；账号删除要级联清理隐式角色；对 `roles.code` 唯一约束侵入大。
- 结论：比 B 复杂、比 A 收益不明确，不采用。

**推荐：方案 A。**

---

## 3. 决策二：本点的范围切分

| 选项 | 内容 | 代价 |
|---|---|---|
| A（推荐） | 自定义角色 CRUD + 用户↔多角色绑定 + 权限账号开/停 + 角色级数据范围编辑（`school`/`self` 两类） | 4 组接口 + 2 个页面区；迁移只需新增角色类型约束与索引 |
| B | 同 A，但账号开/停移出（另立 M2-UI4） | 少一组接口；但 spec §3.5 验收「删除权限账号」继续悬空，TD-187 只能算部分完成 |
| C | 只做用户↔角色绑定 + 账号开/停，自定义角色延后 | 最快打通"权限能授出去"；但运营仍只能用 8 个内置角色，矩阵里的细粒度组合落不了地 |

**推荐：A**——B/C 都会让 M2 台账留尾，且 `school.permission_account.manage` 这个码的存在本身就说明需求把开停账号视为权限后台的一部分。

---

## 4. 决策三：自定义角色的创建与删除语义

### 4.1 创建

- `code`：由调用方给，正则 `^[a-z][a-z0-9_]{2,62}$`，并拒绝与 8 个内置 code 及前缀保留名单冲突。理由：可读、可写进导入模板、可用于跨环境搬运；服务端生成 `custom_xxxx` 会让审计和工单里的角色名不可读。
  - **接口校验必须与库层同界**：`roles.code` 列是 `String(64)`，而该正则的总长度上限是 **63**（1 + 2..62）。Pydantic 侧写 `max_length=64` 会让 64 字符的提交穿过接口校验、在 INSERT 时撞 `ck_roles_code_format_valid`，返回一个 500/IntegrityError 而不是 422。因此创建接口的 `code` 校验固定为 `min_length=3, max_length=63` + 同一个正则（TD-187b 落地时照此写，不要"顺手"用列宽）。
- `name`：1–100 字符，允许中文。
- 初始权限：空集，或 `copy_from_role_id` 指定的角色权限集（仅同租户、仅可编辑角色与自定义角色为源；以超管角色为源会被 §6 的敏感码规则约束）。
- 自定义角色 `is_system=false`、`defaults_applied` 保持 false 且**永不参与**同步补种与重置出厂（同步谓词已限定 `r.is_system`，需复核确认这条不变量并在测试里钉住）。

### 4.2 删除：方案 A 硬删（推荐）vs 方案 B 软删

- A：`DELETE`（或 `POST /roles/{id}/delete`）前必须预览 `affected_users`；执行要求逐字 `confirm_role_code` + 理由 ≥10 + `version` CAS；子表 `role_permissions`/`data_scopes`/`user_roles` 由复合外键 CASCADE 清理；审计 `school.role.deleted` 记完整权限集快照 + 受影响用户数。
  - 优点：与 M2-UI2 的预览/CAS/确认口径完全一致，只有一套规则；`code` 可复用（重建同名角色不冲突）。
  - 缺点：历史绑定不可回溯——靠审计快照补，删除前必须把快照写全（这是硬要求，不是可选项）。
- B：`status='archived'` 保留绑定行。
  - 缺点：所有角色查询都要加状态过滤，漏一处就是"归档角色仍生效"的越权面；与 `uq_roles_tenant_id_code` 冲突（重建同名需改名）；内置角色没有这个状态，等于两套规则。
- C：只允许删除未绑定任何人的角色。
  - 优点：最保守，绝无"删角色导致人失去权限"的意外。
  - 缺点：运营日常"这个岗不要了"必须先解绑再删，两步且第二步常被忘记；而预览 + 确认已经把意外挡住了。

**推荐：A**（含强制快照审计）。

---

## 5. 决策四：数据范围编辑做到哪一层

- 角色级（`school`/`self`）可编辑：**做**。这是 M2-UI2 留下的真实缺口——PUT 只改权限，不改范围，而"同一角色看全校还是只看本人"是运营最常配的一项。范围行不参与 `defaults_applied` 语义（design.md §9 已记），编辑走同一条 CAS。
- 用户级手工授 `class`/`course` 范围：**不做**。`scopes_sync.sync_class_scope_rows` 是期望-实际差量同步，手工加的班级范围行会在下一次关系变更时被当作差量删掉；两条写入路径同一张表 = 静默丢失。班级可见性的唯一事实来源应是任课/班主任/辅导员关系（M2-ORG01-03 D1 既定口径）。
- 用户级 `school`/`self` 范围：同样**不做**——由角色决定更可控，且 `Principal` 的范围解析已是"角色范围并集"。
- 需要按人特例看某个班 → 登记 TD：给「特例可见性」设计独立表 + 与同步的优先级规则，属跨点重构。

---

## 6. 决策五：TD-191 的收口方式（高风险，必须拍板）

背景：`school.role.manage` 的持有者能给自己所在角色补任意 `school.*`/`campus.*` 码；M2-UI3 上线多角色后，他还能"新建一个含敏感码的角色并绑到自己身上"——**风险面比 M2-UI2 更大**。M2-UI2 当期否决的两个收窄方案（`requested ⊆ principal.permissions`、禁止编辑自己所在角色）当时的否决理由在多角色模型下部分失效（超管可以先授出所需码所在的自定义角色），需要重新选。

| 方案 | 做法 | 优点 | 缺点/风险 |
|---|---|---|---|
| A（推荐） | **自扩权显式化**：定义 `SENSITIVE_SELF_GRANT_CODES`（`school.role.manage`、`school.personnel.sensitive.read`、`school.personnel.delete`、`school.personnel.mfa.reset`、`school.org.manage`）；当一次写操作（角色权限替换 / 角色绑定 / 新建角色）**结果集会作用于操作者自己所在的任何角色**且新增码命中该名单时：① 必须带 `self_grant_ack=true` + 理由，否则 422 `SELF_GRANT_CONFIRMATION_REQUIRED`；② 无论是否放行都写 `school.role.self_grant` 审计（含 before/after 码集与触达路径）；③ 对超管主体免确认但同样留痕 | 不挡正常配置流；把隐式越权变成显式动作 + 一条可报警/可复盘的审计流水；M6 双人确认上线时可直接替换 ① 为审批 | 不是硬封——被确认过的自扩权仍会发生（但全程可查、可事后撤）；需要新增一条审计动作与前端确认交互 |
| B | **只能授自己已有的码**（`requested ⊆ principal.permissions`），超管主体豁免 | 数学上封死自扩权 | 会挡掉"校管给教师加自己没被预设的码"这一常见动作，运营必须先找超管；与 A 相比收益是封死，代价是把超管变成瓶颈。多角色下还有第二条绕路：新建角色 → 绑给自己 → 该角色的码必须 ⊆ 自己已有码，仍封得住，但实现要把"结果集并集"算清楚，复杂度高于 A |
| C | 维持 M2-UI2 现状（只禁转授 `role.manage`），把收口整体推到 M6 双人确认 | 零成本 | 多角色化后风险面已经扩大，`design.md §5.12` 现在写的"真实边界"会变成低估；审查大概率按 Major 复现 |

**推荐：A**，并把 B 的完整实现与 M6 双人确认一起登记为后续 TD。选 C 需要用户明确接受"本期不收紧"。

---

## 7. 接口与权限声明（拟）

| 方法/路径 | 权限码 | 说明 |
|---|---|---|
| `POST /school/roles` | `school.role.manage`（+ §6 自扩权确认） | 创建自定义角色；`copy_from_role_id` 可选 |
| `DELETE /school/roles/{role_id}` | `school.role.manage` | 硬删；`confirm_role_code` + `reason` + `version` |
| `GET /school/users/{user_id}/roles` | `school.role.manage` | 该账号当前绑定 + 生效权限并集（只读，便于运营核对） |
| `PUT /school/users/{user_id}/roles` | `school.permission_account.manage` | 全量替换绑定集 + `version`/CAS + 差异预览 + 审计 `school.user.roles.updated`；**不能给自己绑**（防止绕过 §6，见 §8） |
| `POST /school/roles/{role_id}/preview-affected` | `school.role.manage` | 一次提交的影响面（受影响账号数、新增/移除码、是否触达自己） |
| `POST /school/users/{user_id}/deactivate` | `school.permission_account.manage` | `status='disabled'` + 撤刷新家族(`revoked_reason='admin_revoke'`) + `token_version+1` + Redis 清会话；审计 `school.user.deactivated` |
| `POST /school/users/{user_id}/activate` | `school.permission_account.manage` | 回 `active`；**不自动恢复 MFA 状态**；审计 `school.user.activated` |

新增权限码：`school.role.delete`（决策 #6）——把"建/改/绑定"与"删角色"分开，避免 `role.manage` 单码即可清场；权限目录 60→61 码。

错误契约新增：`SELF_GRANT_CONFIRMATION_REQUIRED`(422)、`SELF_GRANT_REASON_REQUIRED`(422)、`BINDING_SELF_FORBIDDEN`(403)、`ROLE_CODE_TAKEN`(409)、`ROLE_NOT_DELETABLE`(409，内置角色)、`ACCOUNT_ALREADY_DISABLED`(409)、`ACCOUNT_ALREADY_ACTIVE`(409)；沿用既有 `ROLE_NOT_FOUND`/`USER_NOT_FOUND`/`ROLE_VERSION_CONFLICT`/`UNKNOWN_PERMISSION_CODE`/`PLATFORM_PERMISSION_FORBIDDEN`/`RESET_REASON_REQUIRED` 口径。

---

## 8. 实现顺序（数据 → 接口 → 界面；TDD 红→绿）

1. **迁移**：`roles` 加自定义角色的 CHECK（`code` 格式 + `is_system` 与 code 保留名单互斥）+ `users.status` 取值 CHECK（若尚无）+ 可选新码 `school.role.delete`。
2. **不变量测试先红**：`tests/integration/test_rbac_custom_roles.py`——自定义角色不被同步补种、不参与重置、复合外键保证不跨租。
3. **服务层**：`app/rbac/custom_roles.py`（创建/删除/影响面预览）、`app/rbac/binding.py`（用户↔角色全量替换 + 拒绝审计复用 `_audit_denied`）、`app/personnel/account_admin.py`（开/停，复用 `app/auth/revocation.py` 与 MFA 重置同一条撤销链路）。
4. **§6 自扩权守卫**：`_assert_self_grant(...)` 三条写路径共用（角色替换、绑定、创建角色）。
5. **API**：`app/api/school_rbac.py` 扩端点 + 声明权限 + 错误契约（`SELF_GRANT_CONFIRMATION_REQUIRED`、`ROLE_IN_USE`、`ROLE_CODE_TAKEN`、`ACCOUNT_ALREADY_DISABLED`、`REALM_MISMATCH`）。
6. **业务场景测试**：`tests/business/test_bs_m2ui3_*.py`（见 §9）。
7. **前端**：admin「角色权限」页加"新建/删除角色"与范围编辑；新「权限账号」页（账号列表 + 角色绑定抽屉 + 启停按钮，全部 `data-testid`）；api-client 类型；mock E2E。
8. **文档回写**：design.md §4 权限矩阵、§5.12 错误码表、§9 落地口径；README/handoff；TD-187/191 处置。

**「不能给自己绑角色」的理由（写死在接口层）**：若允许自绑，运营流程"给自己加个角色"就会成为 §6 守卫的第二条绕路（新建角色 → 自绑 → 拿到全部码）。代价是"校管本人也要超管来绑"，用 §7 的预览接口 + 明确 `lock_reason` 降低摩擦。此条为待确认项，若用户希望放行则必须强制走 §6 确认。

---

## 9. TDD 测试清单与测试策略

**单元**
- `test_role_code_validation`：`code` 格式、保留名单、撞内置码 → `ROLE_CODE_TAKEN`。
- `test_self_grant_guard`：结果集触达自己 + 敏感码 + 无 ack → 422；有 ack → 200 且审计出现；非敏感码不触发；超管免确认但留痕。
- `test_binding_union`：多角色权限并集、解绑后下一个请求即失权（纯函数级推导）。

**集成（真实 PG）**
- `test_custom_role_not_seeded_by_sync`：建自定义角色 → 跑 `sync_rbac_definitions` → 权限集与范围行不变（钉住 `is_system` 不变量）。
- `test_custom_role_reset_forbidden`：对自定义角色调 `/reset` → 409（从 M2-UI2 的"待 M2-UI3"改为明确语义）。
- `test_cascade_on_role_delete`：删角色 → `role_permissions`/`data_scopes`/`user_roles` 同租户子行清空，且**不串租户**（跨租同名角色不受影响）。
- `test_deactivate_revokes_sessions`：停用 → `refresh_tokens` 全 `revoked_reason='admin_revoke'`、`token_version+1`、Redis 会话键消失、旧 access 调 `/auth/me` 401、旧 refresh 401、重新登录 401/403；启用后可登录且原角色绑定仍在。

**业务场景（真实基础设施，全链路）**
- `BS-M2UI3-A 从零建角色并授人`：超管建「教务处职员」→ 勾 `school.personnel.view`+`school.personnel.update` → 绑给某教师 → 该教师**不重登**下一个请求 `PUT /school/students/{id}` 由 403 变 200；审计两段（角色创建 + 绑定）串得上。
- `BS-M2UI3-B 回收即时生效`：解绑 → 下一个请求 403；会话不断（与 M2-UI2 场景 1 同口径）。
- `BS-M2UI3-C 停用在读账号`：给一位教务人员开号并绑定 → 停用 → 该账号所有接口 401、`/auth/me` 401、刷新重放 401、登录被拒；审计不含明文；重启用后恢复。
- `BS-M2UI3-D 自扩权路径`：持 `role.manage` 的管理员 ① 新建角色含 `school.personnel.sensitive.read` 并绑给自己 → 无 ack 422 / 有 ack 200 且 `school.role.self_grant` 留痕；② 试图给自己绑已有角色 → 403/422（按 §8 的待确认项）。
- `BS-M2UI3-E 删除角色`：预览显示 `affected_users=2` → 未逐字确认 422、理由过短 422、版本过期 409 → 正确提交后角色与其映射消失，两个账号权限并集立即缩小，审计含删除前完整码集快照。
- `BS-M2UI3-F 越权与跨租`：无 `school.role.manage` 的角色接口 403；无 `permission_account.manage` 的绑定/启停 403；他租角色 id / 用户 id 一律 404 且不泄露存在性；平台域码出现在权限集 → 422。
- `BS-M2UI3-G 并发`：两个操作者同时改同一角色的绑定集/权限集 → 后者 409 带最新快照，库不出现半提交状态。

**前端**
- vitest：角色创建表单校验、绑定抽屉的差异计算、启停二次确认文案、`role.manage` 菜单热更新。
- mock Playwright：新建→绑定→停用主路径、§6 自扩权确认弹窗、409 不静默覆盖、无权限时菜单消失且直连被挡。
- 全栈 E2E：**继续受阻**（TD-183/192 同因，本机 8000 被 photo-memory 占用，禁杀）；本点写 `e2e-fullstack` 用例但按 TD 登记待补跑，或先做端口可配专项（需用户点头）。

**门禁**：AGENTS.md §6 全套 + 独立审查两轮；审查发现"更好做法"必须登记 TD 并给处置（§10）。

---

## 10. 预判技术债（本点已知取舍）

| TD（待编号） | 内容 | 处置建议 |
|---|---|---|
| 按人临时授权缺失 | 选方案 A 后，"给一个人一项临时权限"需建一个角色 | 已采纳：M6 与审批引擎一起做"临时授权 + 到期回收" |
| 用户级 `class` 范围手工特例 | 与 `scopes_sync` 互斥，本点不做 | 已延后：出现真实"特例可见"需求时专项设计 |
| 自扩权非硬封 | 方案 A 允许"确认后自扩" | 已采纳：M6 双人确认接管（把 ack 换成审批） |
| 删除角色的历史绑定不可回溯 | 只靠审计快照 | 已完成口径：快照写入是硬要求，用例断言审计里能还原码集 |
| 权限账号列表页 | 本点只做绑定/启停，不做完整账号列表（含状态筛选/分页） | 已延后：M4 C 端联调时一并补 |
| 全栈 E2E | 端口受阻 | 已延后：与 TD-183/192 同批 |
| `reset_role` 不过自扩权守卫 | 恢复出厂不会要求 ack；成立的前提是"出厂默认 ∩ 敏感清单 = ∅"这一不变量，而不是代码里判过 | 已排期：TD-194 把它并入共用守卫；本期用两侧用例钉住不变量（`test_self_grant_guard_unit.py` 算子侧 + `test_rbac_custom_role_constraints.py` 库侧），任一新增敏感码进出厂清单即红 |
| 前端预检吃的是进入页面时的快照 | 本页的角色码集与 `/auth/me` 权限并集都可能落后于库，判据只能算"提示"不能算"防线"；已按服务端 422 强制展开确认条（保留草稿），真正的 enforcing 仍在服务端 | 已延后：TD-195 统一权限并集读取口径后，前端改为保存前重取一次并集；本期不引入"看起来更安全"的前端判定 |

---

## 11. 高风险确认清单（2026-10-26 用户逐条拍板，全部已确认）

| # | 事项 | 结论 |
|---|---|---|
| 1 | 范围切分 | **全部四项**：自定义角色增删 + 用户↔多角色绑定 + 权限账号启停 + 角色级数据范围编辑（§3 选项 A） |
| 2 | 「多权限分配」数据模型 | **只绑角色**，不引入 `user_permissions`、不做隐式个人角色（§2 方案 A） |
| 3 | 自定义角色删除语义 | **硬删 + 影响面预览 + 逐字确认 + 理由 + 完整码集快照审计**（§4.2 方案 A）；子表靠复合外键 CASCADE，同名 code 可重建 |
| 4 | TD-191 自扩权收口 | **自扩权要明说**（§6 方案 A）：结果集触达自己所在角色且新增敏感码 → 必须 `self_grant_ack=true` + 理由，否则 422；`school.role.self_grant` 审计**无论是否超管、无论放行还是打断都写**（打断侧 `result="denied"`，见下方"实现口径"的补充说明）。硬封方案（只能授自己已有的码）作为后续 TD 与 M6 双人确认合并 |
| 5 | 操作者能否给自己绑角色 | **不能**：绑定接口拒绝对「操作者本人 user_id」写入（403 `BINDING_SELF_FORBIDDEN` + 拒绝审计），否则 §6 的确认守卫有第二条绕路。代价（校管换岗需超管代绑）已知并接受 |
| 6 | 删除角色是否单独授权码 | **新增 `school.role.delete`**，权限目录 60→61 码；`school.role.manage` 只保留建/改/绑定/范围编辑 |

### 决策 #4 的具体判据（实现口径）

- `SENSITIVE_SELF_GRANT_CODES = {school.role.manage, school.role.delete, school.personnel.sensitive.read, school.personnel.delete, school.personnel.mfa.reset, school.org.manage}`。
- 「触达自己」= 本次写操作的**结果权限集**（角色替换后该角色的码集 / 新绑定集并集）会经 `user_roles` 流到操作者自己的账号；判定在**写之前**用同一份并集算，不用提交值近似。
- 命中即要求 `self_grant_ack`；超管主体免 `ack` 但仍写审计（免的是打断，不是免留痕）。
- **审计覆盖到"被打断的那次"（2026-10-27 用户裁定：打断也写）**：决策 #4 原句「无论是否超管都写」只约束**主体**，没写**结果**。实现读成"放行与打断都写"，并经用户确认为定稿口径：`SELF_GRANT_*` 拒绝路径除通用 `school.role.permissions.denied` 外，再写一条 `school.role.self_grant`（`result="denied"` + `role_code` + `newly_reaching_self` + `self_grant_ack`）。理由：只记成功案例的报警等于只看得到得手的人，"谁在反复试探给自己加敏感码"这个信号恰恰在拒绝侧；且与本期既有口径一致——写操作的全部拒绝（含 404 探测）都算安全事件。代价是每个被打断的请求多一条审计行（独立事务，外层已回滚，见 TD-193）。
- **同一 `action` 两侧的 `reason` 含义不同（消费方必读）**：`school.role.self_grant` 放行侧的 `reason` 是操作者填写的自由文本理由，打断侧是判据码（`SELF_GRANT_CONFIRMATION_REQUIRED` / `SELF_GRANT_REASON_REQUIRED`，与本项目所有 `*.denied` 审计同口径）。按 `result` 字段分流即可，不要假设 `reason` 恒为自由文本。
- 三条写路径共用一个守卫函数：角色权限全量替换、创建自定义角色（以他人角色为源复制敏感码时）、用户↔角色绑定替换（受 #5 限制，路径实际只剩"给别人绑"）。
- **`school.role.self_grant` 的字段与 §6 方案 A ② 的措辞差异（已按此实现）**：方案 A 写的是"含 before/after 码集"，交付形态改为「专用审计只记触达路径（`role_code` + `newly_reaching_self` + `confirmed_by` + `reason`），完整前后码集由同事务的 `school.role.permissions.updated` 承担」。理由：一次自扩权写两条审计，若两侧都塞全量码集就是同一份数据存两遍（且 `role.manage` 类角色的码集可达 60 项，审计表会被自己撑大）；`before` 可从 `added`/`removed` 与提交值精确还原（`before = (after ∪ removed) − added`），复盘时按 `request_id` 关联两条即可，信息量为零损失。报警侧只需要"哪几个敏感码流向了本人"，正是专用审计记的那几个字段。
