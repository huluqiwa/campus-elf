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
| 数据范围 | `data_scopes` 允许绑 role 或 user（CHECK `scope_binding`）；**`class` 的用户级范围行由 `app/org/scopes_sync.py` 按「任教 ∪ 班主任 ∪ 辅导员」做期望-实际差量同步**（读写谓词都限定 `user_id is not null`；角色级同型行没有合法写入方，见 §13.1 勘误），且必须在关系变更同事务内调用 | 手工按人授 `class` 范围会被下一次同步当差量删掉 → 二者互斥，见 §5 |
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
4. **§6 自扩权守卫**：`_assert_self_grant(...)` 三条写路径共用（角色权限替换、角色范围替换、用户绑定）。**TD-187b 修正（2026-10-27 裁定）：创建角色不接**，判据对新角色 id 恒不可达，见 §12。**TD-187c 补充（2026-10-27 拍板）：范围替换接的是范围版判据**（净增 `school` 才算敏感，见 §13.3），三条 = 权限/范围/绑定，不含创建。
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
| 4 | TD-191 自扩权收口 | **自扩权要明说**（§6 方案 A）：结果集触达自己所在角色且新增敏感码 → 必须 `self_grant_ack=true` + 理由，否则 422；`school.role.self_grant` 审计**无论是否超管、无论放行还是打断都写**（打断侧 `result="denied"`，见下方"实现口径"的补充说明）。硬封方案（只能授自己已有的码）作为后续 TD 与 M6 双人确认合并。**TD-187b 修正（2026-10-27 裁定）：三条路径缩成两条——创建自定义角色不接守卫，见 §12** |
| 5 | 操作者能否给自己绑角色 | **不能**：绑定接口拒绝对「操作者本人 user_id」写入（403 `BINDING_SELF_FORBIDDEN` + 拒绝审计），否则 §6 的确认守卫有第二条绕路。代价（校管换岗需超管代绑）已知并接受 |
| 6 | 删除角色是否单独授权码 | **新增 `school.role.delete`**，权限目录 60→61 码；`school.role.manage` 只保留建/改/绑定/范围编辑 |

### 决策 #4 的具体判据（实现口径）

- `SENSITIVE_SELF_GRANT_CODES = {school.role.manage, school.role.delete, school.personnel.sensitive.read, school.personnel.delete, school.personnel.mfa.reset, school.org.manage}`。
- 「触达自己」= 本次写操作的**结果权限集**（角色替换后该角色的码集 / 新绑定集并集）会经 `user_roles` 流到操作者自己的账号；判定在**写之前**用同一份并集算，不用提交值近似。
- 命中即要求 `self_grant_ack`；超管主体免 `ack` 但仍写审计（免的是打断，不是免留痕）。
- **审计覆盖到"被打断的那次"（2026-10-27 用户裁定：打断也写）**：决策 #4 原句「无论是否超管都写」只约束**主体**，没写**结果**。实现读成"放行与打断都写"，并经用户确认为定稿口径：`SELF_GRANT_*` 拒绝路径除通用 `school.role.permissions.denied` 外，再写一条 `school.role.self_grant`（`result="denied"` + `role_code` + `newly_reaching_self` + `self_grant_ack`）。理由：只记成功案例的报警等于只看得到得手的人，"谁在反复试探给自己加敏感码"这个信号恰恰在拒绝侧；且与本期既有口径一致——写操作的全部拒绝（含 404 探测）都算安全事件。代价是每个被打断的请求多一条审计行（独立事务，外层已回滚，见 TD-193）。
- **同一 `action` 两侧的 `reason` 含义不同（消费方必读）**：`school.role.self_grant` 放行侧的 `reason` 是操作者填写的自由文本理由，打断侧是判据码（`SELF_GRANT_CONFIRMATION_REQUIRED` / `SELF_GRANT_REASON_REQUIRED`，与本项目所有 `*.denied` 审计同口径）。按 `result` 字段分流即可，不要假设 `reason` 恒为自由文本。
- 三条写路径共用一个守卫函数：角色权限全量替换、创建自定义角色（以他人角色为源复制敏感码时）、用户↔角色绑定替换（受 #5 限制，路径实际只剩"给别人绑"）。**→ TD-187b 实现偏离，2026-10-27 用户裁定按新口径定稿，见 §12**：创建路径**不接**守卫（新角色 id 必不在操作者角色集合里，判据无可达分支）；"建角色再绑给自己"这条链由**决策 #5 的自绑禁令**收口，不是由守卫收口——绑定路径接线时按"绑定后该用户的权限并集"判，且 #5 必须如实落地。
- **`school.role.self_grant` 的字段与 §6 方案 A ② 的措辞差异（已按此实现）**：方案 A 写的是"含 before/after 码集"，交付形态改为「专用审计只记触达路径（`role_code` + `newly_reaching_self` + `confirmed_by` + `reason`），完整前后码集由同事务的 `school.role.permissions.updated` 承担」。理由：一次自扩权写两条审计，若两侧都塞全量码集就是同一份数据存两遍（且 `role.manage` 类角色的码集可达 60 项，审计表会被自己撑大）；`before` 可从 `added`/`removed` 与提交值精确还原（`before = (after ∪ removed) − added`），复盘时按 `request_id` 关联两条即可，信息量为零损失。报警侧只需要"哪几个敏感码流向了本人"，正是专用审计记的那几个字段。

---

## 12. TD-187b 实现结果与对决策 #4 的偏离（2026-10-27 用户裁定：**不接，改判据口径**）

### 12.1 交付面

- 服务层 `app/rbac/admin.py`：`create_role` / `preview_role_deletion` / `delete_role`、`RESERVED_TENANT_ROLE_CODES`（从 `builtin_roles("tenant")` 派生，不手抄）、`ROLE_CODE_PATTERN`、`RoleDeletionPreview`。
- 接口层 `app/api/school_rbac.py`：`POST /school/roles`、`POST /school/roles/{id}/delete/preview`（`school.role.manage`）、`POST /school/roles/{id}/delete`（**独立码 `school.role.delete`**，决策 #6）。
- 自定义角色语义定稿：**可编辑 ✔ / 可删除 ✔ / 可重置出厂 ✘**（`ROLE_DEFAULTS_UNAVAILABLE`，结构性不可能而非"待开发"）。M2-UI2 的业务用例据此改写，并新增断言"拒绝不推进 `roles.version`"。
- `reset_role` 顺手加固：出厂定义检查移到版本 CAS **之前**（原来在之后，靠事务回滚抹掉版本增量；顺序化后被拒请求不抢行锁、不推版本、同事看不到无谓的 409）。

### 12.2 偏离：创建路径不接自扩权守卫

**决策 #4 的字面要求**是三条写路径共用一个守卫函数（角色权限全量替换、创建自定义角色、用户↔角色绑定替换）。实现只在**角色权限全量替换**一条上接线（绑定路径随 TD-187d 建起来时再接），**创建路径有意不接**。

判据本身：`_assert_self_grant` 只在 ① 被改角色的 id ∈ 操作者自己所在的角色集合，且 ② 结果集相对操作者当前持有**净增**敏感码 时才可能命中。新建角色的 id 此刻刚刚生成，`user_roles` 里不可能有指向它的行，因此 ① 恒假 —— 不是"我们选择不触发"，是"该函数在这条路径上没有可到达的true分支"。

**那"先建带敏感码的角色、再绑给自己"这条链呢**（审查 M-1 提的攻击路径）：它确实绕开 `PUT /roles/{id}/permissions` 的 ack 守卫，但**收口它的不是守卫，是决策 #5**——绑定接口对"操作者本人 user_id"硬拒 403 `BINDING_SELF_FORBIDDEN`。**读这段必须记住时点**：本仓今日还没有任何 `user_roles` 写路径（§0 现状核对），所以这条链现在不可达不是因为 #5 已经生效，而是因为根本走不通；#5 是 **TD-187d 的落地前置，不是既有防线**。所以本点的残余风险不在"创建要不要接守卫"，而在 **#5 是否被如实实现**。据此把 TD-187d 的前置约束写死：

1. 绑定路径必须落 #5 的自绑禁令（硬 403 + `bind_denied` 审计），这是这条链的唯一承重墙；
2. 绑定侧仍要接守卫，但接的时机与判据要按"绑定后该用户的权限并集"算，不是按单角色算——将来若 #5 放宽（例如允许超管"代自己绑"或引入自助换岗），守卫立刻变成活的；
3. 创建路径本身**不静默**：`school.role.created` 审计带完整 `permission_codes` 与 `copy_from_role_id`，"谁建了一个含 `school.personnel.delete` 的角色"在审计流里可直接按码检索，报警面没有缺项。

**为什么不走"字面遵守"（把守卫也接到创建路径）**：那会引入一段结构上不可达的分支，为它写的用例只能靠 mock 把 `principal_role_ids` 伪造进新角色 id，测的是替身而不是行为；更坏的是它给后来人一个"创建已受守卫保护"的错误暗示，而真正的墙在 #5。

**代价与再评估点**已登记 `docs/tech-debt.md` TD-194（原条目就是"守卫只接线到一条路径"）：TD-187d 落地时按上面 1/2 收口，并把守卫时序抽成单一 helper（TD-194 的原始诉求）。**2026-10-27 用户已裁定按本口径定稿**（另两个备选"按字面接上守卫""创建含敏感码时强制填理由"均未采纳，理由见上：前者是不可达分支 + 只能测替身，后者改接口契约却不提供新的防线）。

### 12.3 与本简报其他条款的符合性

| 条款 | 落点 |
|---|---|
| §4.1 校验与库层同界（3..63 + 同一正则） | **两处强制**：`RoleCreatePayload` 的 `Field(pattern=ROLE_CODE_PATTERN, max_length=63)` 与库层 `ck_roles_code_format_valid`；常量定义在服务层做单一来源，用例从迁移源码里解析正则逐字符比对（不抄第二遍）。服务函数本身不校验格式，直调传非法码 = 撞库 CHECK 变 500（第三轮 m-2 把"三处同界"的说法改直） |
| `name` 非空 | **只在接口层拦**（`field_validator` 去空白、全空白判非法）：服务层还会再 strip，否则落库一行没名字的角色。库层 `roles.name` 只有 NOT NULL + 宽 100，没有"非空白"CHECK，直调服务仍可落空名 → 登记 TD-198（与 §4.1 的 code 不同界这一处是已知缺口，不是遗漏） |
| `confirm_role_code` 收 64 而非 63 | **有意为宽**：这个字段只做逐字相等比较，超长串必然是"用错了"，让它走到服务层才有 `{code,message}` 的 `ROLE_CODE_MISMATCH`；收成 63 会先撞 Pydantic 的数组形 422，把错误契约打断。与 M2-UI2 的 `RoleResetPayload` 同界 |
| §4.1 初始权限二选一 | `ROLE_INITIAL_SOURCE_AMBIGUOUS` 422：不选"以显式清单为准、静默忽略复制源"，配置类接口不能猜调用方意图。**空清单与不给该字段等价**（复制模式下前端勾选框本来就是空的，判它非法等于逼调用方靠"少传字段"表达意图），只有"非空清单 + 复制源"才触发——第三轮 m-6 提的第二种写法（`None` 默认值区分两者）判为不必要，理由同上 |
| §4.1 复制源限制 | 锁定内置角色（超管/C 端）不可作源：`ROLE_COPY_SOURCE_NOT_EDITABLE` 422；不存在与跨租同形 404 |
| §4.2 硬删 + 影响面预览 + 逐字确认 + 理由 + 完整码集快照 | `delete/preview` 只读不审计；`role_permissions`/`data_scopes`/`user_roles` 靠复合外键 CASCADE；删除前快照进 `school.role.deleted`；响应 `version` 返回被删行的最后版本号而不是从未存在的 `+1` |
| §4.1 内置码保留 | `RESERVED_TENANT_ROLE_CODES` 人读 409 + 库层 `ck_roles_builtin_code_is_system` 兜手工 SQL |
| §11 决策 #3 同名可重建 | 硬删后同码再建成功（`uq_roles_tenant_id_code` 随主行消失）；跨租同名互不影响 |
| 审查三轮 m-1（跨租复制源的留痕够不够） | 该条 `create_denied` 的 `resource_id` 是"试图建的码"（此刻还没有 role id），**被探测的 role id 进 `metadata.copy_from_role_id`**，否则巡检回答不了"谁在拿别人家的 id 枚举"；业务用例 BS-M2UI3-B 逐项断言四条留痕的 metadata |
| 审查三轮 m-3（"写侧全部拒绝都留痕"说过头了） | 口径改成"越权/撞码/目标不可用三类留痕"，并显式列出**入参非法**这一族不落安全审计：`UNKNOWN_PERMISSION_CODE`、`PLATFORM_PERMISSION_FORBIDDEN`、`ROLE_INITIAL_SOURCE_AMBIGUOUS` + Pydantic 挡在进服务层之前的格式/长度非法（BS-M2UI3-B 正向断言它们不在 `create_denied` 里）。`ROLE_CODE_NOT_DELEGABLE` 是同族里**故意反过来**留痕的那一个：它判的是操作者有没有资格，不是码合不合法。平台域那条要不要单开报警登记 TD-201 |
| 审查三轮 m-4（影响面数字的过期窗口） | 绑定不推进 `roles.version` → 预览→确认之间新绑的人不被 CAS 挡下；执行侧快照与审计取的是删除当时的真数（事前失守、事后可查），由 `test_preview_affected_users_is_not_protected_by_the_version_cas` 钉现状，收口在 TD-199（TD-187d 二选一拍板） |
| 审查三轮 m-6（前端复刻了服务端的可重置判据） | `RoleSettingsView` 的恢复出厂按钮今天跟 `is_system` 走，与"有没有出厂定义"等价但不是同一判据；`RoleResponse` 下发服务端派生 `resettable` 的收口登记 TD-200（TD-187g 一并做，避免响应模型改两轮）。服务端 `reset_role` 恒 409 是唯一门禁，本条只影响按钮准确性 |
| 审查三轮 m-5（`rowcount != 1` 分支零覆盖） | **不补**，理由是造不出真场景而不是遗漏：读侧 `SELECT … FOR UPDATE` 与写侧 `DELETE` 共用同一策略的同一个 `USING`，一致策略下不可能"看得见却删不动"，命中只能 monkeypatch rowcount（测替身）。分支本身作为 RLS 漂移的保险留在代码里，说明写在集成用例模块头，同一文件的第 4 条改为测真实存在的并发面（m-4） |
| 审查 m-1（并发兜底别误报） | `IntegrityError` 只在**约束名精确等于** `uq_roles_tenant_id_code` 时翻成人读 409（名字取 asyncpg 原生异常的 `constraint_name`——PG 协议字段 `n`，不是错误文本子串；SQLAlchemy 的适配异常上没有这个属性，要往 `exc.orig.__cause__` 取一层，两处都取不到按"不是同码冲突"处理、宁可 500。子串匹配会把 `..._v2` 这类同前缀约束一并误判）；撞格式 CHECK/FK 原样抛出（无全局 handler → Starlette 纯文本 500，响应体不含 SQL），不把库层故障伪装成业务冲突 |

---

## 13. TD-187c：角色级数据范围编辑（2026-10-27 用户拍板四项，批次 A 的一半）

### 13.1 目标与验收

补 M2-UI2 留下的真实缺口：`PUT /school/roles/{id}/permissions` 只改权限、不改范围，而"同一角色看全校还是只看本人"是运营最常配的一项（§5 决策四）。验收口径：

- 角色级 `school`/`self` 可全量替换，下一请求即生效、不作废在线会话（与权限同口径）；
- `class`/`course` **不可手工授**（本点理由，不是 §5 那条用户级理由）：两类都必须绑定具体班级/课程 id（库 CHECK `ck_data_scopes_scope_id_required_for_class_course`），而角色配置端点的入参只有"类型"这一维、没有"选哪个班"的通道——写下去要么撞 CHECK，要么造出解析层读不懂的行（`scopes.py:scope_context_from_rows` 对缺 id 的 `class` 抛 `ValueError`，该角色每个持有人下一次请求 500）。`course` 另外还本期不贡献可见性（TD-151）。
  **勘误（TD-187c 独立审查 Major 2）**：本条原写"`class` 行归 `scopes_sync` 的差量同步所有，手工写会被下一次关系变更当差量删掉"——那句只对**用户级**行成立（`scopes_sync` 的读写谓词都限定 `user_id is not null`，删除按 `user_id + scope_id` 定位），角色级同型行今天**没有任何合法写入方**，也就没有"被同步删掉"这回事。理由换成上面这条与库层事实一致的。
- 范围抬升触达操作者自己所在角色时走自扩权确认（13.2 决策①）；
- 空清单合法但预览必须报出"该角色 N 名持有人将看不到任何数据"（13.2 决策④）。

### 13.2 四项拍板结论（用户 2026-10-27）

| # | 结论 | 备选与为什么不选 |
|---|---|---|
| ① | **范围编辑接自扩权守卫**：结果范围集触达操作者所在角色且**净增 `school`**（全校）时必须 `self_grant_ack=true` + 非空理由，放行与打断都写 `school.role.self_grant`（`metadata.kind="scope"` 区分权限那条） | 不接：判据只覆盖权限码，则 `role.manage` 持有者可静默把自己所在角色从 `self` 抬到 `school`。范围放大与补一个敏感码在效果上是同一件事——可见数据集变大；design.md §5.12「能力只在 PUT 与绑定流动」这句话本来也盖不住范围这一路。**守卫写路径从此是三条**：权限替换、范围替换、用户绑定（创建角色仍不接，§12.2 裁定不变），TD-194 的抽 helper 口径同步改 |
| ② | **独立端点** `PUT /school/roles/{id}/scopes`（全量替换 + 差异预览语义与权限端点对称） | 合并进 `PUT permissions`：请求少一次，但两个编辑区互相覆盖——权限草稿脏了会把范围草稿一起打回 409，审计的 `added/removed` 也要把"码"和"范围"混进一条 payload。分开则各自 diff、各自留痕、前端各回各的草稿 |
| ③ | **手工授 `class`/`course` 被拒要留痕**（`school.role.scopes.denied`） | 归"入参非法"族不留痕（同目录外码、平台域码）：这一条拦的不是码合不合法，而是**往库里写一条本端点补不全 `scope_id` 的行**的意图（见 §13.1 勘误），与 `ROLE_CODE_NOT_DELEGABLE` 同类（判据看资格与不变量，不看字面格式），报警侧需要看得到 |
| ④ | **允许空范围清单**，预览提示持有人数 | 禁止空清单：会挡掉"先建角色、后配范围"的正常流程；且范围解析本就是 fail-closed（`scopes.py`：解析不到范围行 = 空集，绝不回退成全校），空集不会放大可见性，风险方向是"看不见"而不是"看见太多" |

未列问题的默认口径（无需拍板，写进代码注释与 design.md）：范围编辑复用 `school.role.manage`，**不新开权限码**（新开会让"配一个角色"要两个码，且 `role.manage` 的语义本就是配角色）；与权限替换**共享同一个 `roles.version`** 乐观锁（一列版本覆盖两条写路径，避免"权限 CAS 成功、范围 CAS 失败"的半更新态）。

### 13.3 判据扩展（`app/rbac/self_grant.py`）

现有判据是纯权限码的（`SENSITIVE_SELF_GRANT_CODES` × 净增 × 触达本人角色）。本点加一条**同形**的范围判据，不把它揉进码判据里：

- 输入：库内该角色当前范围类型并集、提交后的结果范围集、操作者所在角色在库内的范围并集；
- 触发条件：结果集含 `school` 且**操作者当前生效的范围并集不含**（净增，读库重读，与权限侧同一口径），且被写角色 ∈ 操作者所在角色集合；
- `self` 不入敏感清单：∅ → `self` 确实也是扩大，但扩大出来那一份只有持有人自己的记录，够不到任何别人的数据，问一次 ack 只会淹没真信号（原写法"`self` 不比现状更宽"不准确，TD-187c 审查 Minor 6 勘误）。`class` 压根不可经角色配置写入（§13.1），到不了这一步；`school` 是唯一敏感项，因为它是"覆盖其余一切"的那个类型（`scopes.py:sees_whole_school`）。
- 与权限判据共用 `_assert_self_grant` 的调用骨架与留痕动作，差异只在 `metadata.kind` 与敏感项定义 → 这就是 TD-194 抽 helper 的第二条腿。

### 13.4 接口与错误契约（新增）

| 方法/路径 | 权限码 | 说明 |
|---|---|---|
| `GET /school/roles/{id}/scopes` | `school.role.manage` | 该角色当前范围行（只读，含 `editable=false` 的 `class` 行——角色级同型行今天没有合法写入方，只有手工 SQL 能造出来，用例靠它钉住窄删除；前端按"不可编辑"渲染） |
| `POST /school/roles/{id}/scopes/preview` | `school.role.manage` | 差异预览：新增/移除范围类型、结果集是否触达本人、持有人数、空清单提示；只读不审计 |
| `PUT /school/roles/{id}/scopes` | `school.role.manage`（+ §6 范围版确认） | 全量替换 `school`/`self`；`version` CAS；审计 `school.role.scopes.updated`（记 added/removed/resulting/affected_users/reason），被拒走 `school.role.scopes.denied` |

错误码：`SCOPE_TYPE_NOT_EDITABLE`(422，含 `class`/`course`，留痕)、`UNKNOWN_SCOPE_TYPE`(422，不在 `ScopeType` 内，不留痕，且**排在可编辑性判定之前**)、`SELF_GRANT_CONFIRMATION_REQUIRED`/`SELF_GRANT_REASON_REQUIRED`(422，沿用)、`ROLE_VERSION_CONFLICT`(409，回带最新角色快照)；内置锁定角色沿用 `ROLE_IMMUTABLE`；库内 CHECK 与模型 `ScopeType` 漂移时 `SCOPE_CATALOG_OUT_OF_SYNC`(409)。

版本冲突**不复用新码**：两条写路径本就共享同一列 `roles.version`（§13.2 未列问题那句），冲突对运营是同一件事——"这个角色被别人改过了，刷新重试"。再造一个 `ROLE_SCOPE_VERSION_CONFLICT` 只会让前端多认一个码、语义不变，属无收益的契约扩张。

### 13.5 实现顺序（TDD 红→绿）

1. 纯判据单测先红：范围版判据（净增 `school` 才触发、收窄 `school`→`self` 不算、改别人所在角色不算、`self` 不入清单）；
2. 服务层 `app/rbac/admin.py`：`list_role_scopes` / `preview_role_scopes` / `replace_role_scopes`（复用 M2-UI2 的 CAS + 差异 + `_audit_denied` 骨架）；
3. API 三条路由 + payload：`scope_types` 收 `list[str]`（**有意不用 `Literal` 收窄**——非字面量必须走到服务层才有人读的 `{code,message}` 422，Pydantic 先拦会换成 FastAPI 的数组形校验错误，与 `RoleDeletePayload.confirm_role_code` 同口径）；
4. 业务场景 BS-M2UI3-C：自定义角色持有人从「无范围（空集）」→ `school` → `self` → 空清单，每步用**同一条 token** 断言 `/school/personnel` 的真实行集（不停在 HTTP 200），并断言预览/审计/`affected_users` 与范围行一致；
5. 前端（归 TD-187g 批次 B）：范围编辑区 + 差异预览 + 自扩权确认条复用。

### 13.6 预判技术债

- `class` 范围行的只读展示与手工特例的长期方案沿用 §10 那条（独立表 + 与同步的优先级规则），本点不动；
- 范围与权限共享一列 `version` → 前端两个编辑区会互相让对方的草稿失效，本点用"保存前重取快照 + 409 自动采用最新"缓解，彻底解法是分列版本或合并端点 → 登记 TD；
- `school` 之外若将来出现第二个"覆盖型"范围（如 `department` 全校化），13.3 的敏感项清单要跟着改，本点先写死 `school` 单一事实来源。

---

## 14. TD-187d：用户↔多角色绑定（2026-10-29 简报，用户已拍板）

对应任务：TD-187d（M2-UI3 第四批）、TD-194（守卫 helper 抽取 + `reset_role` 双维度接线）、TD-199（删除预览影响面失去 CAS 保护）。本批**只做绑定**；权限账号启停归 TD-187e。

### 14.1 现状核对（读代码得到的事实）

| 事项 | 事实 | 影响 |
|---|---|---|
| `user_roles` 表 | 已建：`uq_user_roles_user_id_role_id` + 两条复合外键（用户、角色都必须同租，`ondelete=CASCADE`）；模型只有 `user_id`/`role_id`，**没有 version/updated_at** | 数据面就绪；绑定的并发控制没有现成载体 |
| 写入路径 | 全仓**没有任何 `insert/update/delete user_roles`**（只有 `Principal` 与审计侧的读取、测试直插） | 本接口是第一条写路径，TD-199 的"预览过期"从纸面变成真实窗口 |
| 权限生效时机 | `app/auth/principal.py:140-196` **每个请求都重读** `user_roles → role_permissions → permissions`；`users.status != active` 直接拒绝 | 绑定/解绑**下一个请求即生效**，不需要撤 token、不打断在线会话（与 M2-UI2 权限编辑同口径） |
| 绑定 CAS 载体 | `users` 只有 `token_version`（语义是"令牌失效"），**没有**可用于绑定集的版本列 | 需要一个新列，或改用"期望集合"比对（§14.4） |
| 目标用户 | `GET /school/users/lookup` 已能按登录名精确定位并返回 `id/user_type/status/roles`（MFA 重置用，查询留审计） | 绑定页复用，不重复造 |
| 角色目录 | `GET /school/roles` 的 `RoleResponse` 已含 `permission_codes`、`user_count`、`version` | 前端可在本地算绑定前后的权限差；服务端仍以落库结果为准（TD-195 口径） |
| 内置租户角色 | 8 个：`school_super_admin`/`school_admin`/`teacher_b_end` + C 端 `student`/`teacher`；教师账号可能同时需要 C 端 `teacher` 与 B 端 `teacher_b_end` | 不能一刀切"只许绑 B 端角色"；真正不能绑的只有超管角色 |
| 超管保护先例 | MFA 重置对超管目标一律 403（`app/personnel/mfa_admin.py`），由平台 CLI 兜底 | 绑定沿用同一保护口径 |
| TD-199 | 删除预览的 `affected_users` 不受 `roles.version` 保护；`test_preview_affected_users_is_not_protected_by_the_version_cas` 钉住现状，注释要求绑定落地时翻成 409 断言 | 本批已拍板用**影响面指纹**收口（§14.3）；该用例已翻成 409 断言并改名 `test_preview_then_rebind_makes_the_delete_stale` |

### 14.2 目标与不做

**目标**

1. `GET /school/users/{user_id}/roles`：读该账号当前绑定、绑定版本、生效权限并集。
2. `PUT /school/users/{user_id}/roles`：全量替换绑定集，版本 CAS，差异审计。
3. **自绑禁令**（决策 #5）：操作者不能改自己的绑定，403 + 拒绝审计。
4. **超管保护**：不能改超管的绑定、不能经本接口把超管角色授给别人。
5. **TD-199 收口**：绑定变化使删除预览的**影响面指纹**失效（§14.3）。
6. **TD-194 收口**：抽出守卫 helper，并把 `reset_role` 的权限/范围两个维度接上（§14.8）。

**不做**

- 权限账号启停（TD-187e）、前端页面（TD-187g）。
- 不做独立的绑定预览端点：前端用 `GET /users/{id}/roles` + `GET /school/roles` 本地算差异，服务端以落库后的审计 diff 为准（与 TD-195/§10 的前端预检口径一致）。若 187g 发现前端算不准，再加服务端预览。
- 不做按人直授权限、不做账号删除、不做 C 端角色的分配策略（M4）。

### 14.3 决策 A（已拍板 2026-10-29）：TD-199 用「影响面指纹」收口

用户选择**指纹**，不采用"绑定 bump 角色版本"。

**做法**

- `POST /school/roles/{id}/delete/preview` 除 `affected_users` 外，新增 `fingerprint`：
  `SHA-256(role_id + 按 id 排序的受影响 user_id 列表)` 的十六进制。只回哈希，不回显受影响用户清单。
- `POST /school/roles/{id}/delete` 的请求体新增必填 `fingerprint`；既有 `version` 保留——它负责"角色权限/范围在预览后被改"，指纹负责"持有人集合在预览后被改"，两套判据各管一件事。
- 执行时在 `roles` 行锁（`for update`）之后重新计算指纹；不一致 → 409 `ROLE_DELETE_PREVIEW_STALE`，回带最新预览（人数 + 指纹 + 版本）。调用方刷新后重新确认。
- 删除执行路径在重算指纹前对 `user_roles` 的持有人行取 `FOR UPDATE`（预览不锁）：并发**解绑**不碰角色行锁，不锁持有人行时它能在"重算指纹 → CASCADE"之间提交，让响应与审计的 `affected_users` 多算一个已经解绑的人。锁序 `roles → user_roles`，与绑定的 `users → roles → user_roles` 无环。
- 指纹只覆盖**持有人集合**，不覆盖权限码：权限/范围变化已经由 `roles.version` 挡住，避免同一次变化触发两条错误码。
- 指纹无过期时间：它绑定的是状态而不是时间，状态没变就一直有效。
- 并发顺序：删除先锁角色行；绑定写路径读取提交角色时持 `FOR KEY SHARE`（与 `user_roles` 插入所需的外键锁同档）。绑定要么在指纹重算前提交（指纹不匹配 → 409），要么被角色行锁挡到删除提交后（读不到角色 → 404 `ROLE_NOT_FOUND` + 拒绝审计）。不会出现"按旧人数删掉、绑定同时成功"，也不会把并发暴露成外键 500。
  - 实现对齐（2026-10-29 复审 Major）：初稿只写了"绑定插入对角色行取外键共享锁"，那样删除先提交时绑定会在 INSERT 撞外键变成 500；落地时把锁提前到 `_roles_by_ids` 的角色读取（`for key share`），于是同一时序变成"等锁 → 读到 0 行 → 404"。用例 `test_rbac_binding.py::test_role_deleted_mid_binding_yields_404_instead_of_a_foreign_key_error` 先用 `pg_blocking_pids()` 确认有会话被本用例事务挡住，再提交删除；判据不绑定语句文本，因此锁被移除时"绑定改在 INSERT 外键检查处被挡、最终 `IntegrityError`"同样会如实转红，而绑定若晚于删除提交才启动（探测不到等待）也判失败，不留假绿。

**为什么不用版本 bump（原推荐 A）**：你选指纹，是因为改绑不应让角色上未提交的权限/范围草稿失效——持有人变化只影响"删除风险面"，不影响角色自身的权限配置。代价是新增一个响应字段、一个请求字段与一个错误码，且要覆盖"绑了又解回同一集合 → 指纹相同、删除成功"这条用例。

**测试翻面**：`test_preview_affected_users_is_not_protected_by_the_version_cas` 改名为指纹用例，断言"预览 → 改绑 → 删除 = 409"；另补"改绑后又解回原集合 → 删除成功"。（已落地：两个用例现为 `test_preview_then_rebind_makes_the_delete_stale` 与 `test_rebinding_back_to_the_same_set_keeps_the_preview_valid`。）

### 14.4 决策 B（技术默认，可推翻）：绑定自身的并发控制

**推荐：给 `users` 加一列 `roles_version integer not null default 0`**，`GET` 下发，`PUT` 必须带；不等则 409 并回带最新绑定快照。

| 方案 | 说明 | 取舍 |
|---|---|---|
| **A（推荐）** | 新列 `users.roles_version`，每次绑定替换 `+1` | 与 `roles.version` 同一种交互（读 version → 提交 version → 冲突带快照）；一次小迁移；语义单列，不碰 `token_version` |
| B | 请求带 `expected_role_ids`（客户端回显读到的集合），服务端按集合相等判 CAS | 不建列；但请求体翻倍，集合相等在"角色被删/重建"时语义模糊，前端要额外保存一份快照 |
| C | 不加锁/不比对，后写覆盖 | 两个管理员同时改同一账号的绑定会静默丢掉一个人的改动——权限回收场景下这是安全问题，不采用 |

两个版本号职责不同，不能合并：`users.roles_version` 防"同一账号的两次绑定互相覆盖"；`roles.version` 仍是角色自身权限/范围编辑的 CAS（删除预览的新鲜度由 §14.3 的指纹负责，绑定**不再**触碰 `roles.version`）。

### 14.5 决策 C（已拍板 2026-10-29）：绑定路径不接自扩权守卫

**先说事实**：在决策 #5（禁止改自己的绑定）成立的前提下，绑定**别人**的角色不会改变操作者自己的权限并集；而 `evaluate_self_grant` 判的是"被写角色的权限集是否让操作者本人净增敏感码"。绑定接口既不写角色权限集，也不允许目标是操作者本人——**判据没有可达的 true 分支**。

| 方案 | 做法 | 结果 |
|---|---|---|
| **A（推荐）** | 不接守卫；**自绑禁令是这条链的唯一承重墙**；用例钉住"给自己绑 = 403 + `school.user.roles.denied`"；在 TD-194 备注里写死"#5 一旦放宽（自助换岗/代绑），绑定路径必须立刻按绑定后的操作者权限并集接守卫" | 与 §12.2 对创建路径的裁定同口径：不写结构上不可达的分支，不写只能靠 mock 的用例 |
| B | 按 TD-194 字面"绑定侧也要接守卫" | 只能把"目标用户的并集"硬塞进"操作者是否扩权"的判据，语义是错的；或写成永不触发的分支，给后来人"绑定已受守卫保护"的错误暗示 |

**已选 A**，并同步改 TD-194 的措辞：三条写路径 = 权限替换 + 范围替换 + `reset_role`（第四入口），**绑定不接**；#5 是绑定的硬前置。选 B 需要接受一条不可达分支与一条 mock 用例。

### 14.6 决策 D（已拍板 2026-10-29，全部按推荐）：可绑定对象与超管保护

| # | 规则 | 推荐 | 理由 |
|---|---|---|---|
| 1 | 目标用户类型 | 只允许 `teacher`/`school_admin`；`student` → 422 `BINDING_TARGET_NOT_ALLOWED` | 「权限账号」是 B 端概念；给学生绑后台角色等于绕过 B 端账号边界 |
| 2 | 目标用户状态 | `deleted` 视为不存在（404，留拒绝审计）；`locked`/`disabled` 可绑 | 停用账号先配好角色再启用是正常流程；`locked` 是临时状态，不应阻止配置 |
| 3 | 目标持有超管角色 | 一律 403 `BINDING_SUPER_ADMIN_FORBIDDEN`（增、删、清空都不行） | 与 MFA 重置同口径：超管由平台侧管理，避免租户内互相削权/加权 |
| 4 | 提交的角色含超管角色 | 422 `ROLE_NOT_BINDABLE` | 否则持 `permission_account.manage` 的人可把任意账号提成超管，绕过平台开通流程 |
| 5 | 空绑定集 | 允许 | "收回全部权限"是合法的离职/停岗动作；自绑被禁，不存在管理员误清自己 |
| 6 | 跨租角色/用户 id | 一律 404 + 拒绝审计 | 不泄露存在性，沿用本项目既有口径 |

第 1 条只拦"目标用户"，不拦"角色类型"：教师账号可能合法地同时持有 C 端 `teacher` 与 B 端 `teacher_b_end`。

### 14.7 接口与错误契约

| 方法/路径 | 权限码 | 说明 |
|---|---|---|
| `GET /school/users/{user_id}/roles` | `school.permission_account.manage` | 返回 `{user_id, display_name, user_type, status, roles_version, roles[], permission_codes[]}`；`roles[]` 含 `{id, code, name, is_system, bindable}`（与 `RoleResponse.bindable` 同源，超管角色为 false）。只读，不审计成功查询 |
| `PUT /school/users/{user_id}/roles` | `school.permission_account.manage` | 全量替换；body `{role_ids: UUID[], version: int, reason?: str≤500}`；成功返回与 GET 相同形状 + `added/removed` 角色与权限码 diff |

- `RoleResponse` 增加服务端派生字段 `bindable: bool`（当前只有 `school_super_admin` 为 false），前端绑定页据此过滤，避免"能选中、提交必 422"的死胡同（与 TD-200 的 `resettable` 同口径）。
- 删除预览/删除契约（TD-199）：`RoleDeletionPreviewResponse` 增加 `fingerprint`；`RoleDeletePayload` 增加必填 `fingerprint`，`version` 保留。
- 不新增权限码；绑定与启停共用 `school.permission_account.manage`。
- 错误码新增：`BINDING_SELF_FORBIDDEN`(403)、`BINDING_TARGET_NOT_ALLOWED`(422)、`BINDING_SUPER_ADMIN_FORBIDDEN`(403)、`ROLE_NOT_BINDABLE`(422)、`BINDING_VERSION_CONFLICT`(409，回带最新绑定快照)、`ROLE_DELETE_PREVIEW_STALE`(409，回带最新删除预览)；沿用 `USER_NOT_FOUND`(404)、`ROLE_NOT_FOUND`(404)、`REALM_MISMATCH`(403)。
- 审计：
  - 成功 `school.user.roles.updated`：`role_codes_before/after`、`added_role_codes`、`removed_role_codes`、`added_permission_codes`、`removed_permission_codes`、`reason`；不写登录名/邮箱等明文。
  - 拒绝 `school.user.roles.denied`：`reason` 为判据码（`BINDING_SELF_FORBIDDEN`/`BINDING_TARGET_NOT_ALLOWED`/`BINDING_SUPER_ADMIN_FORBIDDEN`/`ROLE_NOT_BINDABLE`/`USER_NOT_FOUND`/`ROLE_NOT_FOUND`）。
  - 版本冲突不入拒绝审计（不是安全事件），只回 409 + 最新快照。
  - 差异的时点：`added/removed_permission_codes` 是**本事务读到**的角色权限并集之差。并发改角色权限不会被绑定的 `FOR KEY SHARE` 挡住（那是 `FOR NO KEY UPDATE`，两者不冲突），所以审计描述的是"这次绑定让该账号的能力面按本事务看见的样子变了多少"；同一时刻的角色权限编辑有它自己的 `school.role.permissions.updated` 审计与 `roles.version` CAS，两件事不互相覆盖。不在绑定里加锁角色权限映射——那会把一次改绑升级成"锁住该角色的全部权限行"。

### 14.8 TD-194 helper 与 `reset_role` 接线

- 抽 `guard_self_grant(...)`：把"判据结果 → 422（ack/理由）→ `*.denied` + `school.role.self_grant`（打断）→ 成功侧 `school.role.self_grant`"封在一处，`kind` 作为入参（不拆两个函数）。
- 接线范围：`replace_role_permissions`、`replace_role_scopes`、`reset_role`（**权限 + 范围两个维度**）。绑定不接（§14.5）。
- `reset_role` 的 payload 增加 `self_grant_ack: bool = False`；范围侧是可达路径（`school_admin` 出厂范围含 `school`，见 TD-194 备注），未带 ack 的重置会 422。**187g 之前**，前端没有确认条，调用方需显式传 ack——这是本批已知的接口契约变化。
- 权限侧的"出厂默认 ∩ 敏感码 = ∅"不变量与两侧用例保持不变；抽取 helper 后仍需通过。

### 14.9 实现顺序（TDD 红→绿）

1. 迁移：`users.roles_version integer not null default 0`。
2. 纯逻辑单测先红：绑定 diff、空集、超管角色/目标、自绑、版本冲突、`guard_self_grant` 的 ack/理由/超管豁免、删除指纹（含"绑了又解回原集合"）。
3. 服务层 `app/rbac/binding.py`：`get_user_bindings` / `replace_user_bindings`（目标 `users` 行 `SELECT … FOR UPDATE` 串行化 + `users.roles_version` CAS + 目标/角色校验 + 审计）；`admin.py` 的删除预览/删除加指纹计算与比对（`ROLE_DELETE_PREVIEW_STALE`）。
   - 实现对齐（2026-10-29）：串行化没有复用 `auth.sessions.acquire_user_lock`（advisory lock 的语义是"会话写操作串行化"，会把绑定和登录/刷新挤成一条队列）；改用目标用户行锁——只与同账号的另一次绑定、以及 MFA 重置（同样锁用户行）互斥，范围正好。
4. API：两条绑定路由 + payload/response + `bindable` 字段 + 删除契约的 `fingerprint` + 错误映射。
5. `reset_role` 接 helper + payload ack；改 TD-199 的既有集成用例为 409 断言。
6. 业务场景：建角色→绑人→**不重登**下一请求生效→解绑即时失权→自绑 403→超管保护→过期预览 409。
7. 文档回写：design.md 权限矩阵/错误码、handoff §1、TD-187d/194/199 处置。

### 14.10 测试清单

**单元**

- 绑定 diff（增/删/不变/空集）、超管角色拒绝、自绑拒绝（在服务入口、任何写之前）。
- 删除指纹：同集合不同顺序哈希相同；绑了又解回原集合哈希不变；不同角色同集合哈希不同（`role_id` 入哈希）；空集合稳定。
- `guard_self_grant`：权限/范围两 kind 的 ack/理由/超管豁免；`reset_role` 范围净增 `school` 时 422、带 ack 200 且写 `school.role.self_grant`。

**集成（真实 PG）**

- 自绑 403 + `school.user.roles.denied`；学生目标 422；超管目标 403；超管角色 422；跨租 user/role 404 且无存在性泄露。
- 绑定成功：`user_roles` 行正确；**下一请求**权限并集变化（不换 token）；`school.user.roles.updated` 的 added/removed 权限码可还原。
- 版本 CAS：两人并发改同一账号绑定，后者 409 + 最新快照，库无半提交。
- TD-199（指纹）：删除预览 → 改绑 → 删除 = 409 `ROLE_DELETE_PREVIEW_STALE`；改绑后又解回原集合 → 删除成功；权限/范围变化仍由 `version` 触发 409（两套判据互不替代）。
- `reset_role`：范围净增无 ack → 422 且不推版本；带 ack → 200 + 审计。

**业务场景（真实基础设施）**

- BS-M2UI3-A 建角色→绑人→该教师下一请求 403 变 200；审计两段串得上。
- BS-M2UI3-B 解绑→下一请求 403；会话不断。
- BS-M2UI3-D 自绑 403 / 超管角色 422 / 超管目标 403，三类拒绝都留痕。
- BS-M2UI3-E 删除预览→改绑→删除 409（TD-199），刷新后 200。

**前端**：本批不做（187g）。

### 14.11 预判技术债

| 事项 | 说明 | 处置建议 |
|---|---|---|
| 绑定页需要两个权限码 | 绑定要 `permission_account.manage`，列角色要 `role.manage`；只持其一的账号打不开完整页面 | 已延后：TD-187g 评估是否让 `GET /school/roles` 接受"二选一"，本期不改读接口权限 |
| 无服务端绑定预览 | 差异由前端用 `GET /roles` 的码集本地算，存在与权限并集快照的时差 | 已延后：与 TD-195 同批统一"保存前重取"口径 |
| 最后一名超管无专门保护 | 绑定的超管保护是"目标持有超管即拒绝"，但账号停用（187e）仍需同一套保护，否则可停掉最后一名超管 | 已采纳：TD-187e 复用本批判据并补"最后一名超管"用例 |
| `users.roles_version` 与 `token_version` 易混淆 | 前者管绑定并发，后者管令牌失效；改错会让绑定触发全端重登或完全不设防 | 已在 design.md/handoff 写明；仅绑定服务读写 `roles_version` |
| 删除要同时回传 `version` 与 `fingerprint` | 指纹管持有人集合、版本管角色权限/范围；前端漏传任一个都拿不到 409 的正确语义 | 已采纳：TD-187g 的删除确认框同时回传两者；错误码按 `ROLE_VERSION_CONFLICT` / `ROLE_DELETE_PREVIEW_STALE` 分流 |
| 改绑不再让角色编辑草稿失效 | 这是选指纹换来的收益；代价是删除契约多一个必填字段 | 已按 §14.3 定稿 |
