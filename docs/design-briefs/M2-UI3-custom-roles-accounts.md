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
- `BS-M2UI3-H 停用在读账号`：给一位教务人员开号并绑定 → 停用 → 该账号所有接口 401、`/auth/me` 401、刷新重放 401、登录被拒；审计不含明文；重启用后恢复。（原拟编号 C 与 §13.5 的范围场景冲突，TD-187e 改用 H。）
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

- 抽 `_guard_self_grant(...)`：把"判据结果 → 422（ack/理由）→ `*.denied` + `school.role.self_grant`（打断）→ 成功侧 `school.role.self_grant`"封在一处，`kind` 作为入参（不拆两个函数）。
- 接线范围：`replace_role_permissions`、`replace_role_scopes`、`reset_role`（**权限 + 范围两个维度**）。绑定不接（§14.5）。
- `reset_role` 的 payload 增加 `self_grant_ack: bool = False`；范围侧是可达路径（`school_admin` 出厂范围含 `school`，见 TD-194 备注），未带 ack 的重置会 422。**187g 之前**，前端没有确认条，调用方需显式传 ack——这是本批已知的接口契约变化。
- 权限侧的"出厂默认 ∩ 敏感码 = ∅"不变量与两侧用例保持不变；抽取 helper 后仍需通过。

### 14.9 实现顺序（TDD 红→绿）

1. 迁移：`users.roles_version integer not null default 0`。
2. 纯逻辑单测先红：绑定 diff、空集、超管角色/目标、自绑、版本冲突、`_guard_self_grant` 的 ack/理由/超管豁免、删除指纹（含"绑了又解回原集合"）。
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
- `_guard_self_grant`：权限/范围两 kind 的 ack/理由/超管豁免；`reset_role` 范围净增 `school` 时 422、带 ack 200 且写 `school.role.self_grant`。

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

## 15. TD-187e：权限账号停用 / 启用（2026-10-29）

### 15.1 目标与验收

- 里程碑 M2 §5.4「删除权限账号后无法登录、token 失效、审计写入」；简报 §4.2 第 3 条「停用使其立刻不能登录、既有 token 全部失效，可重新启用」。
- 验收：停用后旧 access 每请求 401、旧 refresh 401、重新登录被拒；重新启用后可登录且角色绑定原样保留；两条动作都有审计；拒绝路径留痕。
- 前端按钮与确认框在 TD-187g；本点只交付接口、服务与测试。

### 15.2 候选方案

| 方案 | 做法 | 优点 | 缺点 / 风险 | 结论 |
|---|---|---|---|---|
| A | 在 API 里内联照抄 MFA 重置的撤会话 + 审计时序 | 改动面最小 | 第三份「锁账号 → 撤家族 → token_version → Redis」拷贝；漏一步就是安全缺口 | 不选 |
| B | 新建 `app/personnel/account_admin.py`，`_change_status(...)` 内部收口状态机、锁、撤会话与审计；API 只做鉴权与错误映射 | 与 `mfa_admin.py` 同构；撤会话时序只写一次；单测可直接打服务层 | 多一个模块 | **选 B** |
| C | 把 `status` 并进 `PUT /users/{id}/roles` | 少一个端点 | 绑定与停用是两个权限面、两种确认流；合并会让「改绑定」误触停用，且停用要理由而绑定不要 | 不选 |
| D | 只写 `status='disabled'`，不撤 refresh | 最简单 | 旧 refresh 在 15 分钟内仍能换发 access，等于没停用 | 不选 |

### 15.3 契约（接口 / 状态机 / 审计）

| 端点 | 权限 | 行为 |
|---|---|---|
| `POST /api/v1/school/users/{user_id}/deactivate` | `school.permission_account.manage` | 请求体 `{reason}`（≥10 字）；`status='disabled'` + 撤**未撤销且未过期**的刷新家族（谓词 `revoked_at is null and expires_at > now()`，`revoked_reason='admin_revoke'`）+ `token_version+1`；提交后删 Redis 会话标记与 MFA 待激活 secret；审计 `school.user.deactivated` |
| `POST /api/v1/school/users/{user_id}/activate` | 同上 | 请求体为空；`status='active'` 并清空 `failed_login_attempts` / `failed_login_window_started_at` / `locked_until`；**不动 MFA、不动角色绑定、不换 token**；审计 `school.user.activated` |

状态机（先做**无锁的本租归属探针**，探针跑在自己的短事务里、跑完即归还连接，再取账号级 advisory lock + `users` 行锁，最后判状态）。判据顺序：
realm → 理由长度 → 自停用 → **无锁归属探针（404，必须先于取锁：advisory key 不含 `tenant_id`，见 `design.md` §5.12 / TD-230；也必须跑在独立短事务里：否则未命中的 404 连同拒绝审计同时占两条连接，见第七轮 m3）** → 加锁/取行 → 不存在/跨租 → **已删除（先于角色）** → 超管 → **目标类型** → 状态机：

| 当前状态 | 停用 | 启用 |
|---|---|---|
| `active` | → `disabled`（撤会话） | 409 `ACCOUNT_ALREADY_ACTIVE` |
| `locked` | → `disabled`（撤会话；锁定本身即不可登录） | 409 `ACCOUNT_NOT_DISABLED`（解锁走锁定窗口到期或重置流程） |
| `disabled` | 409 `ACCOUNT_ALREADY_DISABLED` | → `active`（清锁定计数） |
| `deleted` | 404 `USER_NOT_FOUND` | 404 `USER_NOT_FOUND` |

拒绝判据（除理由过短、`REALM_MISMATCH` 这类**入参/调用方非法**外，全部写 `school.user.status_denied` 审计，独立事务，只落判据码；理由过短与 `RESET_REASON_REQUIRED` 同口径不落安全审计）。`REALM_MISMATCH` 在 HTTP 上到不了服务层（平台 principal 会先被租户依赖拒绝，由 `auth.permission.denied` 留痕），服务层这一条是纵深防御，不重复写业务拒绝审计：

| 场景 | 结果 |
|---|---|
| 目标账号持有 `school_super_admin` | 403 `ACCOUNT_SUPER_ADMIN_FORBIDDEN`（与绑定、MFA 重置同口径；**任何超管都不行**，不只是最后一名） |
| 目标已删除且仍挂超管绑定 | 404 `USER_NOT_FOUND`——删除判据**先于**角色判据，否则会回 403 泄露"这个 id 曾是超管" |
| 目标 `user_type` 不是教师/校方管理员（如学生） | 422 `ACCOUNT_TARGET_NOT_ALLOWED`（与用户↔角色绑定共用 `BINDABLE_USER_TYPES`；学生是 C 端账号，不在这条权限的射程内；判据先于状态机，两个方向同码） |
| 操作者停用自己 | 409 `ACCOUNT_SELF_DEACTIVATE`（比较 `Principal` 与路径参数，先于对目标账号的任何读取与状态写入；拒绝审计是另一条独立事务） |
| 跨租 / 不存在 / 已删除 | 404 `USER_NOT_FOUND`（同形，不泄露存在性；跨租与不存在两条在**取账号锁之前**由无锁探针给出，避免排进别租账号的锁队列；探针在独立短事务里，整条 404 路径任一时刻只占一条连接，由 `test_probe_denial_paths_hold_one_connection_at_a_time` 断言占用峰值 =1——该用例把同一个计数垫片依次挂到 `account_admin.db_session` 与 `mfa_admin.db_session` 上，**停用与 MFA 重置两条 404 路径都跑**） |
| 主体不是租户 realm | 403 `REALM_MISMATCH` |
| 停用理由缺失或 <10 字 | 422 `DEACTIVATE_REASON_REQUIRED` |

审计字段：成功 `status_before` + `sessions_revoked` +（停用）`reason`；拒绝 `operation`（deactivate/activate）+ `reason`（判据码）。系统**自己**不往审计里写登录名、邮箱、手机号——`metadata` 只由服务端派生（`reason` 判据码是常量字符串，不含目标身份）。但**停用的 `reason` 是操作者手写的自由文本，原样入库，本点没有做任何 PII 筛查或词表脱敏**：管理员如果在校验理由里写"该教师手机号 138xxxxxxxxxx 已离职"，那串号码就会留在 `audit_logs.metadata_json` 里（只有 500 字上限防超大字符串）。这与 §3 红线「敏感字段加密或脱敏、审计不得出现明文敏感信息」存在缺口，处置见 TD-232（写入侧掩码 + 前台提示，去向 M3）。`sessions_revoked` 在本点只数**未撤销且未过期**的家族（谓词与撤销语句逐字一致），与 MFA 重置侧的「全部未撤销（含过期死行）」不同口径，跨动作做告警前先看 `design.md` §5.12 与 TD-231。

### 15.4 用户裁决（2026-10-29）

1. **超管一律不能被租户管理员停用**（403，平台侧兜底）——「最后一名超管」因此自然安全，不做并发计数。
2. **不能停用自己**（409），与「不能给自己绑角色」同思路。
3. **停用必须填理由（≥10 字）**；启用不需要理由。

### 15.5 实现顺序（TDD 红 → 绿 → 重构）

1. 集成用例先红：`tests/integration/test_account_status.py`（撤会话全断言、状态矩阵、自停用、超管、跨租/删除、并发恰好一路成功、`locked → disabled`、启用清锁定）。
2. 业务场景先红：`tests/business/test_bs_m2ui3_account_status.py`（BS-M2UI3-H 全链路；C 已被 TD-187c 的范围场景占用，见 §13.5）。
3. 服务层：`app/personnel/account_admin.py`。
4. API：`app/api/school_rbac.py` 两个端点 + 错误映射。
5. 同 PR 落实 TD-223：`backend.yml` 静态检查后加 `alembic upgrade head` + `alembic check`。
6. 文档回写：`design.md` §4 权限矩阵说明、§5.12 错误码；本简报；`tech-debt.md`。

### 15.6 测试策略

- **集成（服务层，不走 HTTP）**：停用后 `users.status='disabled'`、`token_version+1`、**未撤销且未过期**的 `refresh_tokens` 家族全部 `revoked_reason='admin_revoke'`、Redis 会话标记消失（`session_exists` 为假），**已过期家族保持 `revoked_at is null`/`revoked_reason is null` 且不计进 `sessions_revoked`**（它本来就换不出令牌，算进去等于虚报断开的设备数）；启用只回 `active` 并清 `failed_login_attempts`/窗口/`locked_until`，**不动** `mfa_enabled`、角色绑定与 `roles_version`；状态矩阵与 11 条拒绝逐条断言 `AccountAdminError` 的码与 HTTP 状态（超管两向 403、realm 403、跨租与已删除同形 404、学生 422、重复动作 409）+ 拒绝审计逐条对码（该用例租户内恰好 11 条，且 `metadata` 只含 `operation`/`reason`、不含登录名/校码）+ 被拒目标的库内状态与会话一个字节都没变；**成功与拒绝两侧的 `actor_id` 都断言等于操作者本人**（"谁停用的/谁在试探"必须查得出来，`_audit_rows` 因此 `select actor_id::text`）；**理由过短这条断言的是「不落 `status_denied`」**——那 11 条清单里没有它，与 `RESET_REASON_REQUIRED` 同口径；自停用另测「先于对目标账号的任何读取就回 409」（操作者 id 在库里不存在时也得是 409 而不是 404）；同动作并发恰好一路 204、一路 409，且两条审计的 `actor_id` 分别是两个不同的操作者（用第二个同权限账号 `second_operator_id`，不是把同一个人写两遍）；**在途会话写入**（`test_deactivate_waits_for_an_inflight_session_write`）： holder 事务持账号锁并插入新家族未提交，用 `pg_blocking_pids()` 探针确认停用确实被挡住、提交后 `sessions_revoked=1` 把新家族一起撤销；**纯锁 holder**（`test_deactivate_waits_for_a_lock_only_holder`）：holder 只取 advisory lock、不写任何行，停用仍必须排在它后面（TD-227 的决定性用例）；**跨租 id 不进锁队列**（`test_cross_tenant_user_id_does_not_take_their_account_lock`）：别租账号正持着自己的会话写事务时，本租管理员拿那个 id 停用应立刻 404、一次都不排进那把锁（TD-230。承重的断言是「`pytest.raises` 真的拿到了异常」——排进队列就走不到这一步，只会撞到 `timeout = 120`；`not holder_task.done()` 在 `release` 未置位时恒真，只算兜底断言）；**探针 404 只占一条连接**（`test_probe_denial_paths_hold_one_connection_at_a_time`）：用 `monkeypatch` 给 `db_session` 包一层借还计数，同一个 `_SessionLedger` 依次挂到 `account_admin` 与 `mfa_admin` 两个模块上，各自走一次别租 id 的 404 后断言占用峰值 `== 1`、序列为 `open/close/open/close`，并断言两侧各落一条 `*_denied` 审计（`reason`/`actor_id` 逐条对；把任一模块的探针挪回主事务即转红，见下面第 ⑥ 组）；**校方管理员账号可被停启**（`test_school_admin_target_can_be_deactivated_and_restored`）：`BINDABLE_USER_TYPES` 的另一半不能只靠"学生被拒"反证——拿第二个同权限账号（`second_operator_id`）当目标，断言 `disabled` → `sessions_revoked == 0`（无会话可撤也要落状态）→ `school.user.deactivated` 的 `status_before == "active"` → 启用回 `active` 并落 `school.user.activated`。旧 access `/auth/me` 401、旧 refresh 401、重新登录 401 这三条是 **HTTP 链路**的断言，落在下面的业务场景用例，不在这一层。
- **业务场景**：BS-M2UI3-H（开号 → 绑角色 → 停用 → 全接口 401/刷新 401/重新登录 401「账号不可用」→ 启用恢复）；另加 **Redis 清理丢失降级**（打桩点在**真实客户端**上——`revocation._redis()` 返回一个 `delete()` 必抛 `RedisError` 的包装，其余方法原样转发；比整段打桩更接近真实故障，且避免在 patch 的 lambda 里再调 `revocation._redis()` 造成自递归）：停用仍回 204，access/refresh/重新登录三条路都由库内 `status` + `token_version` 断开；并**把"没有用户可见差异"变成断言**——启用 → 重新登录 → `GET /api/v1/auth/sessions`，断言被撤的家族不在设备列表里（`list_sessions` 取的是「库内未撤销 ∩ Redis 标记存在」，孤儿标记加不进列表）。
- **单元**：状态迁移表与理由校验（纯函数），不碰数据库。
- **摘锁 / 摘探针实测（2026-10-29，共 7 组，跑完即恢复原实现并 `grep MUTATION` 确认无残留）**：①停用侧只摘 `acquire_user_lock` → 在途用例**绿**、同动作并发用例**绿**、纯锁 holder 用例**红**（探针 10s 内观察不到任何会话排在 holder 上，说明停用没排队就提交了）；②只摘 `users ... FOR UPDATE` → 三条全**绿**；③两层同摘 → 在途与纯锁两条**红**，并发用例同时**红**（两路都返回 `StatusChangeResult`，`assert 2 == 1`）；④把无锁归属探针判据摘掉 → 跨租用例卡在**别租账号**的锁等待上，直到 `backend/pyproject.toml` 的 `timeout = 120` 由 pytest-timeout 打断整轮；⑤把 `revocation.delete_session` 吞 `RedisError` 那段摘掉 → Redis 降级用例**红**（证明它测的是降级代码本身，不是被桩打空的壳）。**结论**：advisory lock 在本链路**独立承重**（①里唯一转红的那条正是为它设计的交错，行锁够不着）→ **TD-227 收口**；归属探针承重（④）→ **TD-230 收口**；⑥把探针从独立短事务挪回主事务 → `test_probe_denial_paths_hold_one_connection_at_a_time` **红**（`opens` 变 `open/open/close/close`，占用峰值 2），恢复后 `md5sum` 与备份一致 → 第七轮 m3 的"404 路径只占一条连接"是真断言而非摆设；⑦**只**把 `mfa_admin.py` 的探针判断挪进探针自己的 `async with` 里（等价于"拒绝审计另开一条时探针仍占着连接"；停用侧保持原样）→ 同一条用例在 **MFA 那一段**转红、停用段仍先通过 → 第八轮 m5 的"两个模块各测一遍"两边都真承重，不是把停用侧的断言复读了一次。
- **不做**：前端（187g）。

### 15.7 预判技术债

| 事项 | 说明 | 处置 |
|---|---|---|
| 平台侧没有停用租户超管的 CLI | 租户内一律 403 之后，超管离职只能靠平台运维直接改库，缺可审计入口 | 登记 **TD-224**（已采纳，M6 SaaS 运营硬化） |
| 没有独立的「解锁账号」动作 | `locked` 只能等 15 分钟窗口到期或走密码/MFA 重置 | 登记 **TD-225**（已延后，M6 与平台账号锁定一起评估） |
| ~~advisory lock 与 `users` 行锁在本链路互为兜底？~~ **已收口** | 原疑问是 advisory lock 有没有**自己**承重的交错。第六轮新增 `test_deactivate_waits_for_a_lock_only_holder`（holder 只取账号锁、不写任何行）并复跑摘锁：只摘 `acquire_user_lock` 时**这条转红**（另两条竞态用例仍绿，由行锁兜住），只摘 `FOR UPDATE` 三条全绿，两层同摘三条齐红 → **advisory lock 独立承重**，`app/auth/sessions.py` 的「同一账号的登录/刷新/撤销/改密共享事务级 advisory lock，避免撤销与轮换竞态」表述保留 | **已完成**（TD-227，本 PR 内收口：`design.md` §5.12 竞态段与两处用例 docstring 已回改为实测口径；`tech-debt.md` TD-227 状态改「已完成」） |
| 账号级 advisory lock 的 key 不含 `tenant_id` | `hashtextextended(f"{realm}:session:{user_id}")` 让同一 id 在任何租户落到同一把锁上；启停与 MFA 重置原先"先取锁再判归属"，别租管理员可用一次注定 404 的请求排进**对方**账号的登录/刷新队列，404 的耗时同时变成侧信道。实测摘掉探针判据后跨租用例卡死到 pytest-timeout | **已实现**（`account_admin.py`、`mfa_admin.py` 在取锁前加无锁本租归属探针，第七轮 m3 再把它挪进**自己的短事务**——未命中的 404 不再连同拒绝审计同时占两条连接（每引擎 `pool_size=10 / max_overflow=10`）；停用侧用例 `test_cross_tenant_user_id_does_not_take_their_account_lock` + `test_probe_denial_paths_hold_one_connection_at_a_time`；MFA 侧同形用例登记 **TD-230** → 去向 TD-187f。并进 key 的方案**已拒绝**：会改全站登录/刷新锁语义） |
| `sessions_revoked` 跨动作口径不一致 | 停用侧只数「未撤销且未过期」，租户/平台 MFA 重置数「全部未撤销」（含过期死行）；同名字段进告警后跨动作对比会误判严重度，安全结论本身不受影响 | 登记 **TD-231**（已采纳，去向 TD-187f 同批；本点只在 `design.md` §5.12 写清三处口径与收口改法，不改两条已交付链路） |
| 新服务模块漏登记裸 SQL 豁免清单 | `app/personnel/account_admin.py` 直连 `users/refresh_tokens/roles/user_roles`，但没进 `tests/test_bare_sql_scope_guard_unit.py` 的 `SCOPE_EXEMPTIONS`——该守卫只在被跑到时报警，本点前几轮的"受影响子集"没包含它，于是拖到第六轮复测才转红 | **已实现**（补登记 + 理由 + 上限 `MAX_EXEMPT_MODULES` 14→15；把该文件固定进本功能的受影响子集，避免再靠偶然覆盖） |
| 并发用例失败路径悬挂任务（本点已修两处） | 探针/断言失败后未 `cancel()` 的任务会在用例之后才提交，占用连接、结果被静默丢弃 | **已实现**（`test_account_status.py` 在途用例、`test_rbac_binding.py` 并发删角色用例各补 `except BaseException: cancel → await → raise`） |
| 其余测试文件的审计助手仍不按用例租户收窄 | `test_rbac_binding.py:228`（`_audits`）、`test_auth_sessions.py`、`test_bs_m1c2_sessions.py`、`test_bs_m2ui2_mfa_admin.py`、`test_campus_chat_rbac.py` 用全表 `action` 查询配「精确条数」断言；同用例内的跨租子场景会直接命中，未修的悬挂任务只是再叠一层随机性 | 登记 **TD-228**（已采纳，去向 TD-187f；本点只把自己新增的两个文件的 `_audit_rows` 加了 `tenant_id` 过滤） |
| `alembic/` 不在 ruff 门禁范围内 | 本地与 CI 都只跑 `ruff check app tests scripts`；实测 `ruff check alembic` 有 3 条存量告警（`c688c44dec81` 的 `I001`、`e2a6c8d4b0f2`/`f7a9b1c3d5e8` 各一个空 `f` 前缀 `F541`），迁移目录的笔误没有任何机械提醒 | 登记 **TD-229**（已采纳，去向 TD-187f 同批或独立小 PR；先清零 3 条再把 `alembic` 加进门禁，改后跑 `alembic check` + 迁移相关用例证明无漂移） |
| 停用的校验理由是操作者手写的自由文本，未做 PII 筛查 | `reason` 除 `max_length=500` 外没有任何掩码/词表，原样进 `audit_logs.metadata_json`——管理员在里面写"手机号 138xxxxxxxxxx 已离职"，那串号码就长期留在审计里，有审计读取权的人（本校超管、平台运维）都看得到，备份/导出一起带走；`app/api/platform_audit.py:23` 那句「metadata_json 已在写入时脱敏」因此是个当前不成立的 blanket 承诺（第八轮 m4） | 登记 **TD-232**（已采纳，去向 M3 与 TD-226 同批：写入侧正则掩码 + 命中记 `pii_masked` + 前台输入提示 + 该注释按动作分列）；本点只把简报 §15.3 的表述收口成"系统自己不写、自由文本未筛查"，不偷改判据 |

## 16. TD-187f-A：测试与门禁补全（2026-10-29 简报；187f-B 见 §17，待开工时再写）

### 16.1 目标与验收

本批**不改生产行为**，只补测试与门禁。台账里指向 187f 的 6 条中，TD-231 由用户 2026-10-29 拍板拆到 **187f-B**（它要动 `mfa_admin.py`/`platform_admin.py`/`auth/service.py` 与两条已交付链路的既有用例，混在一个 diff 里看不清是哪处改动让读数变化）。留在本批的是 5 条，验收编号已登记进 `docs/milestone.md` §5.2/§5.4：

| 编号 | 条目 | 验收判据 |
|---|---|---|
| T1 | TD-221 | 定位 `app/auth` 分支覆盖率读数的非确定性来源，给出「会漂 / 已不漂」的实验结论；读数 ≥81，目标 82 |
| T2 | TD-205② | 新用例：同角色 `PUT /roles/{id}/permissions` 与 `PUT /roles/{id}/scope` 带同一 `version` 并发 → 恰好一个成功、`roles.version` 只 +1、409 的 `latest` 快照带的是赢家的新值 |
| T3 | TD-228 | `test_rbac_binding.py`（`_audits`）、`test_auth_sessions.py`、`test_bs_m1c2_sessions.py`、`test_bs_m2ui2_mfa_admin.py` 的审计/会话查询按用例租户（或本用例 uuid）收窄；其余 `create_task` + barrier 的并发用例补 `except BaseException: cancel → await → raise`。**两条边界（本批实施后写定）**：①`test_campus_chat_rbac.py:118` 判定**不适用**——两处都是 `any(...)` 包含式断言而非精确条数，一处已在 Python 侧按 `actor_id` 收窄，另一处断言的正是「平台 realm 那行 `tenant_id` 为空」，按租户收窄会把要抓的那一行滤掉；②「**必须为空**」类断言**保留全表口径**（新增 `_audit_rows_all`/`_audits_all`），收窄只用在「**精确条数**」类断言——按租户收窄会让「写错租户的那一行」从断言里溜过去，而那恰恰是要抓的 bug |
| T4 | TD-229 | `ruff check alembic` 3 条存量清零，`alembic` 进 `backend.yml` 与本地门禁；改后 `alembic upgrade head` + `check` 无漂移、迁移相关用例仍绿 |
| T5 | TD-230 | MFA 侧重置补一条与 `test_cross_tenant_user_id_does_not_take_their_account_lock` **同形**的「别租 id 不得排进对方账号锁队列」用例 |

### 16.2 当前事实（2026-10-29 从 CI 产物实测，不凭记忆）

- `app/auth` 分支覆盖 **409/504 = 81.15%**（本批开工时的读数）。81% 门槛需要的最少 covered 是 **409**——**余量为 0 条**，任何一条分支倒退就转红；82% 需要 **414（+5 条）**。
  - **本批交付后的实测读数（PR #14 → 合并提交 `dbec369`）**：PR run `40862216277` 与合并后 main run `40864488603`（**同一 commit**）的 `app/auth` 读数**逐条分支完全一致**，均为 **410/504 = 81.35%**，门槛 80% 通过；407 / 408 / 409 三种读数不再出现。
  - **目标 82 未达标**：82% 需要 414 条分支，现 410，**还差 4 条**（且 CI 口径比本地少 1 条，见 TD-234）。按 §16.5「不虚报达标」的口径如实登记，缺口留给下一批测试补全；**门槛数值本身不上调**。
- 逐文件缺口（未覆盖分支条数）：`service.py` **73/350**、`principal.py` 6、`mfa.py` 4、`tokens.py` 3、`sessions.py` 3、`revocation.py` 3、`passwords.py` 3。
- TD-221 记的两段"漂移嫌疑"里：一段的原行号区间（1335–1350）现在**一条分支都没有**——行号早因 docstring 增删漂移，必须按符号名重找；另一段 `2046 → 2047` 这一支**原本只靠别的用例顺路覆盖**（同一个 `if` 的另一支 `2046 → 2052` 覆盖着；"至今"指的是按正常语境跑的那几次，同 commit 的重跑把它踩到了，见下）。这条正好是"抬 82 该补的真实路径"之一，不是为凑数造分支。**本批已把它变成确定性覆盖**：新增用例 `test_activation_fails_closed_when_pending_secret_cannot_be_decrypted`（在 Redis 里为该用户放一条 AAD 与本人不匹配的待激活密文，逼 `activate_mfa` 走解密失败那条 500 fail-closed 分支），从此这条分支的覆盖不再取决于别的用例顺路踩不踩到。
- 历史那对同 commit 不同读数的 run（407 与 408）**产物已不可下载**（两个 run 都没有 `backend-coverage` 工件），拿不到分支级证据。今天能取到的两份是 PR #12 run 与合并后 main run：两份 `coverage.json` **除 `meta.timestamp` 外逐字节相同**（都 409）。
- CI 的测试命令是**串行** pytest：`addopts = "-q --strict-markers"`，无 `pytest-xdist`、无 `pytest-randomly`，`timeout = 120`。所以漂移**不来自**并行分片或随机顺序，剩下的候选只有三类：用例内真实并发时序、依赖 PG/Redis 的实际状态、依赖墙钟。

**T1 重跑实验结果（2026-10-29，同 commit 两次 CI）**

- 对 main 的 run `40809978351` 做 `gh run rerun`，取 attempt 1 与 attempt 2 两份 `backend-coverage` 产物（attempt 2 工件 id `12885057623`），逐分支 diff。`app/auth/*` 九个文件里**只有一个差异**：`service.py` 的 `2046 → 2047`——attempt 1 在 `missing_branches`，attempt 2 在 `executed_branches`，且 `2047` 这一行本身只在 attempt 2 被执行；行级集合与其余八个文件逐条相同。auth 读数因此 **409 → 410**（81.15% → 81.35%），两次都是绿的，但 409 那次**正好贴着门槛**。
- 形态比 TD-221 原先设想的"并发用例偶发少跑一条"更难看：`2046 → 2047` 是 `activate_mfa` 里"待激活 secret 解不开 → 写 `auth.mfa.secret_invalid` 审计 + 抛 500 `MFA_UNAVAILABLE`"这条 **fail-closed** 分支。全仓 `grep -rn "MFA_UNAVAILABLE" tests/` 只命中 `verify_mfa`、`login` 两条链路和一条 Redis 503，**没有任何用例断言激活链路会 500**——所以 attempt 2 的那条覆盖来自别处顺路踩到，而不是来自一条为它写的用例。
- 已做的定位（本地）：单独跑 `tests/integration/test_auth_d2_mfa_enroll.py`（12 条，含全部 `activate_mfa` 直调）**不覆盖**该弧；单独跑 `tests/business/test_bs_m1d2_mfa.py`（5 条，走 HTTP 激活端点）也**不覆盖**；`mfa_env` 与 `stack` 两个 fixture 都不 FLUSH Redis，而 `tests/support/db.py` 每次用例前 `TRUNCATE ... platform_users CASCADE` 会重生成随机 id，所以"旧 key 被后来的用例读到"这条猜测**不成立**。CI 语境的全量复跑（同命令、同 `--cov` 参数）用于判定它是"跨文件踩到"还是"只有 CI 踩得到"。
- **待确认假设**（写死在这里，坐实前不进 TD-221 的收口结论）：`2046 → 2047` 的覆盖来自跨文件效应或 CI 独有环境；两者都还没证据支持，全量本地读数出来后才知道。
  - **本批已坐实（2026-10-29）**：属**跨文件效应**——本地全量语境下那条弧会被别的用例顺路踩到，但它"能不能踩到"没有任何断言在守，于是同 commit 两次 CI 可以一次踩到、一次踩不到。新增的确定性用例把"踩到"变成"一定踩到"，假设就此不需要再分辨跨文件还是 CI 独有。
- 判定规则走到哪一支：§16.3 写的是「有任一进出 → 拿到确切行号，转 D 的最小改动并补确定性用例」。确切行号已拿到，D 的"最小改动"落到本批就是**新增一条确定性用例，直接让 `activate_mfa` 走解密失败分支**（在 Redis 里为该用户放入一条密文与 AAD/KEK 不匹配的待激活项），同时把 81% 门槛的 0 余量抬起来；**门槛数值本身不在本批上调**，留待下一批的稳定观测。

### 16.2.1 T1 收口结论（2026-10-29，PR #14）

- **漂移已消除**：摇摆源 `service.py` 的 `2046 → 2047` 现在由 `test_activation_fails_closed_when_pending_secret_cannot_be_decrypted` 独占覆盖，**不再取决于别的用例顺路踩不踩到**。PR run 与合并后 main run 是**同一个 commit** 的两次独立执行，读数**逐条分支完全一致**（`app/auth` 410/504 = 81.35%），这是 §16.3 方案 A 要求的证据形态。
- **读数 ≥81 达成**（81.35%，门槛 80%）；**目标 82 未达成**，缺 4 条真实分支，如实登记到 `docs/tech-debt.md` 的 TD-221 行，不写"达标"。
- **门槛不上调**：保持 80。理由是读数稳定 ≠ 余量够——81.35% 距 81% 只有 2 条分支的余量，抬到 81% 门槛则任何一条倒退即红；抬到 82% 门槛则需要先把缺的 4 条补上，两者都留待下一批。
- **新登记 TD-234**：CI 与本地的分支归因口径差一条（`682 → 683` 弧在 12 份 CI 产物里既记为 missing 又记为已执行），成因未定。因此 82% 的目标分支数按 **CI 读数**算（414 条），不按本地算。

### 16.3 T1 的定位方案（这是本批唯一有真方案之争的点）

| 方案 | 做法 | 优点 | 缺点 / 风险 | 结论 |
|---|---|---|---|---|
| A | 对**同一个 commit** 重跑 CI（`gh run rerun <main run>`），下载 attempt 1 / attempt 2 两份 `coverage.json`，逐分支 diff | 只有"同 commit 两次跑"能把"代码变了导致读数变"与"跑得不一样"分开；产物机制与 diff 口径今天已经验证可用 | 一次约 13 分钟（免费额度内，当前每月只用几十分钟）；一次重跑可能碰不到漂移，历史上是 3 次观测出 2 种读数，可能要 2–3 次 | **选 A 作主手段** |
| B | 本地把 auth 相关子集连跑 5 次取分布 | 快、可反复 | 子集 ≠ 全量语境（连接池、PG 里的数据量、并发用例的交错都不同），且本地历史一直稳定 409，**复现不出 CI 那侧的 407/408**——等于在不会出错的环境里找错 | 只做辅助验证 |
| C | 不定位，门槛按最低观测钉死在 80，等它自然收敛 | 零成本 | 这正是 TD-221 立项要否掉的做法：门槛永远贴着"最坏那一次"，81/82 目标不可达，且没人知道倒退是从哪条开始的 | **不选** |
| D | 给用例去时序化（注入时钟、把并发交错改成显式 barrier 断言） | 从根上让读数确定 | 在不知道是哪条之前做＝全量重写并发用例，成本与风险都远高于先定位 | 定位出具体分支后再评估 |

**判定规则（先写死，免得事后"差不多就行"）**：A 若两次 attempt 的 covered 分支集合有任一进出 → 拿到确切行号，转 D 的最小改动并补确定性用例；若 3 次重跑完全一致 → 结论按「漂移源已随 187c/187d 的实现变更消失，但读数仍只认 CI 产物」收口 TD-221，门槛是否上调留给下一批的稳定观测，**不在本批顺手抬门槛**。

### 16.4 实现顺序（TDD 红 → 绿）

0. **T1 实验先行**（重跑 CI + diff）：它决定后面要不要动并发用例，放最后做会让本批的结论无法写；
1. **T5**：MFA 侧跨租锁队列用例（最小、独立，改前用「摘掉 MFA 探针判据」验证它会红）；
2. **T2**：权限 × 范围同版本并发用例——预期**直接绿**（187d 已实现赢家唯一），它的作用是回归网，红/绿状态要如实写「新增即绿，钉的是既有行为」，不假装是红→绿；
3. **T3**：5 个文件的查询收窄 + 取消兜底；改完受影响子集必须**仍全绿**（这类改动如果让某条用例转红，说明那条断言本来就在靠跨租数据或悬挂任务凑数，要当场看清而不是绕过）；
4. **T4**：lint 清零 + 门禁扩围（唯一动到 CI 配置的一步）；
5. **补分支用例**（把 auth 抬向 82）：只补真实路径，按 §16.2 的清单挑；
6. 一轮独立审查 → 出 Blocker/Major 才复审 → 提交 → PR → 远端 CI 全绿 → 合并 → 回填 `handoff.md`/`README.md`/`review-log.md`/台账状态。

### 16.5 测试策略与门禁

- 受影响侧只有 backend（加 `backend.yml` 一处），security 因 workflow 改动也会跑；本地跑受影响子集 + `ruff`/`mypy`/`bandit`/`alembic check`，全量后端交给这个 PR 的远端 CI。
- 覆盖率类结论**只认 PR run 的 `coverage.json`**；若最终没到 82，PR 描述与 `review-log.md` 里写清「本批补了 N 条真实分支 → 读数 X/Y」，不虚报达标。
- T3 会改到 M1/M2-UI2 时代的既有用例，但**只改查询谓词与任务清理**，不改任何断言的业务含义；若某条断言必须改含义才能过，停下来提出来，不静默放宽。
- **实施后补的一条边界（本批写定）**：「精确条数」类断言按租户收窄（「别租同名动作不参与本用例的计数」），「**必须为空**」类断言**保留全表口径**（「这条动作全表一次都没发生」，包括写到别租/NULL 租户的那一行）。前者是 TD-228 想要的收窄，后者若也收窄就把「写错租户」这类 bug 放过了——这是**口径澄清**，不是放宽。

### 16.6 预判技术债（本批已知取舍）

- 读数可复现性依赖 CI 侧产物；本地与 CI 不可比这件事本身是长期成本 → 若 T1 得出"仍会漂"，把「给 auth 关键分支加确定性用例」另开 TD，而不是把门槛继续往下调。
- `scripts/check_coverage.py` 的包与阈值是**手写字典**，新增关键包要人工同步 → 本批不动，观察是否真踩过（踩过再开 TD）。
- 本批不碰 `sessions_revoked` 的口径与类型（TD-231 → 187f-B），`docs/design.md` §5.12 那条"三处口径不同"的警告在本批结束时仍然有效，不能提前删。

## 17. TD-187f-B：审计字段 `sessions_revoked` 的口径与类型收口（T6 = TD-231）

> 用户 2026-10-29 拍板：TD-231 从 TD-187f-A 拆出来单独一个 PR。理由是它要动
> `app/personnel/mfa_admin.py` / `app/platform_admin.py` / `app/auth/service.py` 三个**生产模块**与
> 两条已交付链路的既有用例，混进「纯测试补全」的 diff 里就分不清是哪处改动让读数变化。

### 17.1 目标与验收

**问题（`docs/design.md` §5.12 的两段警告，2026-10-29 实测复核仍成立）**：

1. **同一个键，三种计数口径**：`sessions_revoked` 在停用侧（`school.user.deactivated`）只数
   **未撤销且未过期**的家族；在租户 MFA 重置侧（`school.user.mfa_reset`）与平台 MFA 重置侧
   （`platform.user.mfa_reset`）数**全部未撤销**家族（含已过期但还没被清理的死行）。同一个键进告警后
   跨动作对比会误判严重度——安全结论本身不受影响（两侧都是"把还活着的会话全断了"）。
2. **同一个键，两种类型**：自助链路 4 个动作（`auth.mfa.activated` / `auth.mfa.disabled` /
   `auth.password.changed` / `auth.password.reset`）把它写成**布尔 `True`**（只表达"撤销发生过"）；
   管理侧 3 个动作写成**条数整数**；`auth.password.reset` 另开一个 `sessions_revoked_count` 整数键
   表达同一件事。后果：巡检 SQL `sum((metadata_json->>'sessions_revoked')::int)` 在布尔行上直接报错
   （PG 不能把 `true` cast 成 int），改用 `::text` 又把 `true` 与 `3` 当两类值。

**验收（对应 milestone §5.2 的 T6）**：

- `sessions_revoked` 这个键**全仓只写整数**，含义统一为「**本次操作真正断开的活跃会话家族数**」
  （活跃 = 未撤销且未过期，与设备列表 `list_sessions` 的取数口径一致）。
- 布尔语义改用**新键 `sessions_all_revoked`**，只出现在自助链路 4 个动作（"本次操作把旧会话全部撤销了"）。
- 租户 / 平台两条 MFA 重置链路各补一条「**已过期家族不计入 `sessions_revoked`**」的用例。
- `docs/design.md` §5.12 那段「三处口径不同」的警告改成「三处一致」，并补一张**键名对照表**。
- `auth.password.reset` 的 `sessions_revoked_count` 消失——整数并进同名键 `sessions_revoked`。

**消费方盘点（2026-10-29 实测）**：全仓 `grep sessions_revoked` 的命中只在 `backend/app/`、
`backend/tests/`、`docs/` 三处；`deploy/`、`scripts/`、前端**无任何消费方**。所以改名不会打断既有链路，
受影响的只有 5 处测试断言。

### 17.2 候选方案

**必做部分（三个候选方案都包含，无争议）**：

- **M1**：自助链路 4 处布尔 `sessions_revoked` → `sessions_all_revoked`
  （`auth.mfa.activated` / `auth.mfa.disabled` / `auth.password.changed` / `auth.password.reset`，
  均在 `app/auth/service.py` 的审计 metadata 里）。
- **M2**：`auth.password.reset` 的 `sessions_revoked_count` → 并进同名整数键 `sessions_revoked`。
- **M3**：租户 MFA 重置（`app/personnel/mfa_admin.py:300-310`）与平台 MFA 重置
  （`app/platform_admin.py:225-235`）的**取家族 SELECT** 加 `and expires_at > now()`，与停用侧对齐；
  **撤销 UPDATE 保持全量未撤销**（`revoked_at is null`）——把已过期的死行一并标成撤销是安全的，
  收窄反而不划算。撤销语句与计数语句谓词不同，是**有意为之**，与停用侧"逐字一致"的取舍不同：
  停用侧两条都收窄是因为它必须让"撤销了几台"与"库里标了几台"相等；MFA 侧只需要"计数准"。

**有争议的部分（M2 之后，`auth.password.reset` 那个整数到底数什么）**：

| 方案 | 做法 | 优点 | 缺点 / 成本 | 风险 | 对 design.md 的影响 |
|---|---|---|---|---|---|
| **A（推荐）** | 把自助链路共用的 `_SQL[realm]["active_families"]`（`app/auth/service.py:179` / `:330`）收窄为 `revoked_at is null and expires_at > now()`，`auth.password.reset` 的整数随之变成「活跃家族数」 | 整数键 `sessions_revoked` 在**全部 4 个写它的动作**上口径一致（停用 / 租户 MFA / 平台 MFA / 密码重置），TD-231 一次收干净；顺带让 `active_families` 这个名字名副其实 | 改动落在 4 条自助链路共用的 SQL 上：MFA 激活 / MFA 关闭 / 改密的 `old_families` 随之变成"只含活跃家族" | **实测为无影响**：那 3 条链路拿 `old_families` 只用于 `revocation.delete_sessions`（Redis 标记清理），而会话标记的 TTL = 刷新令牌有效期（`app/auth/revocation.py:1-11`、`:99-110`），家族过期时标记早已自动回收；"撤销"本身走 `revoke_user_tokens`（全量未撤销），不受影响 | §5.12 的口径表与键名对照表按"三处一致 + 自助链路另用 `sessions_all_revoked`"改写 |
| B | 不动自助链路的家族查询，`auth.password.reset` 的整数维持「全部未撤销家族数」 | 零生产 SQL 改动，风险最低 | 整数键 `sessions_revoked` 仍是**两种口径**（管理侧活跃 / 自助侧全量），TD-231 只统一了类型、没统一口径 | 跨动作告警的口径错位原样保留 | §5.12 的警告只能改一半（"类型已统一，口径仍分两处"） |
| C | `auth.password.reset` 的那个整数**不并入** `sessions_revoked`，改用第三个键 `sessions_revoked_all` | 三个键各自含义唯一、零行为改动 | 多一个键名；那个数表达的是"连死掉的记录一起标了撤销"，**当前没有任何消费方**（`deploy/` 与前端都没有巡检 SQL） | 无 | §5.12 要多写一行键名说明 |

### 17.3 选择结论

**选 A**，理由三条：

1. **它是唯一能真正兑现 TD-231 初衷的**：TD-231 立项理由是"同一个键跨动作对比会误判"。B 与 C 都留着
   "同一个整数键两种口径"或"多一个说不清含义的键"，只解决了类型分叉那半。
2. **它的副作用经实测为空**：`old_families` 在 MFA 激活 / MFA 关闭 / 改密三条链路里**只**喂给
   `revocation.delete_sessions`，而会话标记 TTL（`refresh_token_ttl_seconds`，默认 7 天）与刷新令牌
   `expires_at` 同源（同在 `app/auth/service.py` 的 `_issue_tokens` 与 `_rotate` 里取同一个设置项），
   家族过期时标记已自动回收；撤销动作本身走
   `revoke_user_tokens`（全量），收窄家族清单**不影响谁还能不能继续登录**。
3. **它让 `active_families` 这个名字第一次名副其实**：这个 SQL 叫"active"，实际查的是"未撤销"，
   名字长期名不副实（停用侧另写了一条真正 active 的 SQL，两处并存本身就是分叉的温床）。

**不选 B**：留半截。**不选 C**：为没人消费的数字再开一个键，键越多越容易再分叉。

**顺带确认的一处留白**：自助链路另外 3 个动作（`auth.mfa.activated` / `auth.mfa.disabled` /
`auth.password.changed`）**不写**整数——它们原来就没有数字，本次不做无中生有的加法（§17.6 登记为
TD-235 候选，等真要做跨动作告警时再统一补）。

### 17.4 实现顺序（TDD 红 → 绿）

1. **红灯用例（先写，先确认它会红）**：
   - 租户 MFA 重置：造一个「已过期但未撤销」的家族 + 一个活跃家族 → 断言审计里
     `sessions_revoked == 1`（只数活跃那个）。改前会红（改前数 2）。
   - 平台 MFA 重置：同形一条。
   - `auth.password.reset`：造同形场景 → 断言审计里 `sessions_revoked == 1` 且**没有**
     `sessions_revoked_count` 这个键。
   - 自助链路 3 个**只写布尔**的动作（MFA 激活 / MFA 关闭 / 改密）：断言审计里
     `sessions_all_revoked is True` 且 `sessions_revoked` **不在** metadata 里
     （`auth.password.reset` 的布尔另有红灯 ③ 覆盖，它同时还写整数条数）。
2. **改生产代码**：`mfa_admin.py` / `platform_admin.py` 的取家族 SELECT 加 `expires_at > now()`；
   `service.py` 的 `_SQL[realm]["active_families"]` 收窄；`service.py` 4 处布尔改名；
   `service.py` 的 `sessions_revoked_count` 并进 `sessions_revoked`。
3. **绿灯**：复跑上面 5 条 + 受影响子集。
4. **改既有测试**：5 处断言跟着改（**只改键名与期望值，不改断言语义**）；若某条断言必须改语义才能过，
   停下来提出，不静默放宽。
5. **改文档**：`docs/design.md` §5.12（口径一致 + 键名对照表）、本简报 §17、
   `docs/tech-debt.md`（TD-231 转已完成）、`docs/handoff.md` §1、`docs/milestone.md` §5.2/§5.4。
6. **本地门禁 → 独立审查 → commit → push → PR → 远端 CI 全绿 → rebase 合并 → main CI 全绿 → 删分支 →
   文档回填 PR**。

### 17.5 测试策略与门禁

- 单元：无（这条改动没有纯函数）。
- 集成：租户 MFA 重置 / 平台 MFA 重置 / 密码重置 / MFA 激活 / MFA 关闭 / 改密，六个文件覆盖
  「过期家族不计入」与「键名改名」两类断言。
- 业务场景：`tests/business/test_bs_m2ui2_mfa_admin.py`（租户 MFA 重置的审计 metadata 键集合断言）
  与 `tests/business/test_bs_m2ui3_account_status.py`（停用侧）跟着改。
- 门禁：`ruff` / `mypy` / `bandit` / `alembic check`；受影响侧的全量套件交给 PR 的远端 CI。
- **无迁移**：本点不动 schema（审计 metadata 是 JSON 列，改键名不需要迁移）。

### 17.6 预判技术债

- **TD-235（候选，本批不排）**：自助链路 4 个动作里只有 `auth.password.reset` 写整数计数，另外 3 个
  （MFA 激活 / MFA 关闭 / 改密）只写布尔 `sessions_all_revoked`。真要做"跨动作设备数对比"的告警时，
  这 3 个动作需要补整数——届时它们的 `old_families` 已在手（本批收窄后就是活跃家族数），补起来很轻。
- **审计键改名没有迁移窗口**：历史行里仍留着旧的 `sessions_revoked: true`。本仓无巡检消费方，短期无害；
  若将来上巡检，需要按 `created_at` 切一个时间点，之前的行按旧键读。
- **`sessions_all_revoked` 这个名字**：表达的是"本次操作把旧会话全部撤销了"，与整数键的"断了几台"是
  两件事。名字容易和布尔混淆，改名前后各留一次注释说明。

### 17.7 高风险确认

本点动的是**认证链路的家族查询**（`_SQL[realm]["active_families"]` 被 4 条自助链路共用），按 AGENTS.md
§11.7 属"认证"类，需用户确认后再编码。§17.3 的结论（方案 A）与两条待确认事实已列入本次确认：

- 用户 2026-10-29 已拍板的部分：TD-231 拆成独立 PR、撤销 UPDATE 保持全量、布尔侧改名
  `sessions_all_revoked`、`sessions_revoked_count` 并入同名整数键。
- 本次新增待确认的部分：**方案 A**（收窄共用 SQL，让整数键在 4 个动作上口径统一）。

### 17.8 实施结果（2026-10-29）

按 §17.4 的 TDD 顺序落地，红→绿证据如下：

| 步骤 | 用例 | 改前 | 改后 |
|---|---|---|---|
| 红灯 ① | `tests/integration/test_account_status.py::test_mfa_reset_counts_only_live_families` | `sessions_revoked == 3`（含已过期家族） | `== 2` |
| 红灯 ② | `tests/integration/test_auth_d2_mfa_enroll.py::test_admin_mfa_reset_counts_only_live_families` | `sessions_revoked == 2` | `== 1` |
| 红灯 ③ | `tests/integration/test_auth_d1_reset.py::test_password_reset_counts_only_live_families` | metadata 里 `sessions_revoked: True` + 多一个 `sessions_revoked_count: 2` | `sessions_revoked == 1`（int）、无 `sessions_revoked_count`、有 `sessions_all_revoked` |
| 红灯 ④ | `tests/integration/test_auth_d2_mfa_enroll.py` 的 MFA 激活 / 关闭两条断言 | `KeyError: 'sessions_all_revoked'` | 通过 |
| 红灯 ⑤ | `tests/business/test_bs_m1c_auth.py::test_bs_m1c_first_login_password_gate_and_change` | `KeyError: 'sessions_all_revoked'` | 通过 |
| 红灯 ⑥（审查后补） | `tests/integration/test_auth_d1_reset.py::test_platform_password_reset_counts_only_live_families` | 把平台侧 `active_families` 改回旧写法 → `assert 2 == 1`（把已过期家族算了进去） | `== 1`（int）、无 `sessions_revoked_count`、有 `sessions_all_revoked` |

生产改动三处（与 §17.4 step 2 一致，零偏离）：

1. `app/personnel/mfa_admin.py`、`app/platform_admin.py`：取家族 SELECT 加 `and expires_at > now()`；**撤销 UPDATE 保持全量未撤销**（有意与停用侧"两条语句逐字一致"的取舍不同——停用侧要让"撤销了几台"等于"库里标了几台"，MFA 侧只需要计数准）。
2. `app/auth/service.py`：`_SQL[realm]["active_families"]`（租户 / 平台两处）收窄为 `revoked_at is null and expires_at > now()`；4 处布尔 `sessions_revoked` 改名 `sessions_all_revoked`；`sessions_revoked_count` 并入同名整数键 `sessions_revoked`。
3. 既有断言只改键名与期望值，**未放宽任何断言语义**；`tests/business/test_bs_m2ui2_mfa_admin.py` 的 `set(metadata) == {"sessions_revoked", "reason"}` 与 `== 1` 原样保留（该用例没有过期家族，计数不变）。

**红灯过程中发现并堵掉的一个陷阱**：`True == 1` 在 Python 成立，所以"只断言 `metadata["sessions_revoked"] == 1`"会让**布尔值蒙混过关**——红灯 ③ 最初就是这样"值对了"却仍然红在别处。三处新用例都补了 `type(metadata["sessions_revoked"]) is int` 钉住类型，这才是 §17.1 验收里"全仓只写整数"的真正回归网。

**文档回写**：§5.12 由"三处口径不同 + 类型分叉"改写为"口径一致 + 键名对照表"；`docs/tech-debt.md` TD-231 追加实施记录、另登记 TD-235（自助链路另外 3 个动作只写布尔不写条数，已延后到 M3）；`docs/handoff.md` §1 与 `docs/milestone.md` §5.2/§5.4 按"已实现、待合并回填 PR 号"登记。

**独立审查与整改（1 轮，0 Blocker / 1 Major / 5 Minor）**：唯一阻塞项是**平台 realm 侧没有回归网**——审查实测「只把平台 `active_families` 改回旧写法，全部 57 条相关用例照样全绿」。整改：补红灯 ⑥ 的平台用例，并把该文件原先**不带租户过滤**的审计读取拆成 `_audit_metadata(action, tenant_id=...)` 与 `_platform_audit_metadata(action)`（`tenant_id is null`）——平台与租户共用 `auth.password.reset` 动作名，不收窄就会把租户那次的行一起读进来。另 5 条 Minor（台账处置列仍是 `已采纳`、M1-D2 简报 §5.6 审计表仍是旧键名、过期家族被登记了 Redis 会话标记、简报 §17.4 "4 处"应为"3 处"、行号引用漂移）已全部整改，详见 `docs/review-log.md` 同名小节。

**交付结果（2026-10-29）**：PR #16 单 commit `47ab2c0` → **rebase 合并提交 `0ffdb2d`**。PR CI backend run `40907206389` **807 passed / 788.31s**、security run `40907206149` success；合并后 main 推送 CI backend run `40909787189` **807 passed / 805.66s**、security run `40909787157` success；四包分支覆盖率两次 run 逐包一致（`app/auth` 410/504 = 81.35%，门槛 80）。用例数 803 → 807（新增 4 条）。

## 18. TD-187g：前端角色页与权限账号页（含 TD-198 / TD-200 / TD-202）

> 状态：**已拍板**（2026-10-30，三条决策全按推荐，见 §18.3）。本点是 M2-UI3 的**最后一批**（milestone §5.2 T7），
> 也是 M2-UI3 里唯一一个**前后端都要动**的点（TD-200 要后端下发字段、TD-198 要一条迁移），
> 因此 §18.2 的三个决策定完之后才动手。

### 18.1 目标与验收

**范围（`docs/milestone.md` §5.2 T7）**：「187g 前端角色页与权限账号页（含 TD-198 `name` 非空白、
TD-200 `resettable` 下发）」。

**现状核对（2026-10-30 读代码得到的事实，不是设想）**：

| 后端能力（已交付） | 端点 | 权限码 | 前端入口 |
|---|---|---|---|
| 自定义角色创建 | `POST /school/roles` | `school.role.manage` | **无** |
| 自定义角色删除（预览/执行） | `POST /school/roles/{id}/delete/preview` · `/delete` | 预览 `school.role.manage`、执行 `school.role.delete` | **无** |
| 角色数据范围读写 | `GET`/`PUT /school/roles/{id}/scopes` | `school.role.manage` | **无** |
| 账号↔角色绑定 | `GET`/`PUT /school/users/{id}/roles` | `school.permission_account.manage` | **无** |
| 权限账号停用/启用 | `POST /school/users/{id}/deactivate` · `/activate` | `school.permission_account.manage` | **无** |
| 角色列表 / 权限目录 | `GET /school/roles` · `GET /school/permissions` | `school.role.manage` | 有（M2-UI2 角色权限页） |
| 恢复出厂 | `POST /school/roles/{id}/reset/{preview,}` | `school.role.manage` | 有（M2-UI2，按 `is_system` 点亮按钮） |

前端已有的只有 M2-UI2 的 `RoleSettingsView.vue`（`settings/roles`）：勾权限、恢复出厂、MFA 重置。
**M2-UI3 后端交付的五项能力，今天前端一个入口都没有**——运营实际能做的只有「改权限码」。

**两个后端缺口（本批必须补，否则前端做不出来）**：

1. **权限账号页没有「列账号」的接口**。现有能碰到账号的接口全是「按 id 操作」或「按登录名精确查一个」：
   - `GET /school/users/lookup?login_id=` → 一次一个账号（`school.personnel.mfa.reset`）；
   - `GET /school/users/{user_id}/roles` → 单账号绑定快照；
   - `GET /school/teachers` → 要 `school.personnel.view`（**另一个码**），只有教师档案、**没有角色与状态字段**，
     且 `school_admin` 账号没有教师档案行 → **不可用**。
   → 运营想「看看谁持了教师 B 端角色」「把离职教师的账号停掉」，今天**没有任何一条路**。
2. **两个权限码把绑定页变成半死路**（§14.10 延后项）。绑定写要 `school.permission_account.manage`；
   但绑定页得先列角色才能勾，`GET /school/roles` 要 `school.role.manage`。
   只持前者的账号打开绑定页 → 列不出角色 → 只读死路；只持后者的账号 → 能改角色配置但碰不了任何账号。

**验收（对应 milestone T7）**：

- **A1** 角色页能新建自定义角色（`code`/`name` 必填校验与后端**同一判据**）、能删除自定义角色
  （两步预览 → 逐字确认 code + 理由 ≥10 字 + 版本 + 影响面指纹）。
- **A2** 角色页能编辑角色级数据范围：可改 `school`/`self` 两类；`class`/`course` 只读展示并写清
  「为什么不可改」（后端 §13.2 决策③：这两类必须绑具体 id，本端点没有那个入参通道）。
- **A3** 「恢复出厂」按钮跟服务端下发的 `resettable` 走，**不再跟 `is_system` 走**（TD-200）；
  `is_system` 降级为纯展示标签（内置/自建）。
- **A4** `roles.name` 补库层 `check (char_length(btrim(name)) > 0)`（TD-198）；前端新建表单的
  `name` 必填校验与后端同一判据（去空白后非空）。
- **A5** 权限账号页能列出本校权限账号（教师 + 校方管理员）、能改某个账号的角色绑定、能停用/启用、
  能重置 MFA（把 M2-UI2 那个「安全工具」卡片从角色页挪到账号页更合适，但**不是必须**——见 §18.6）。
- **A6** 两个权限码的问题有明确落法（§18.2 决策 2）。
- **A7** 纯逻辑抽 `*-logic.ts` 走单测；两个页面各配 mock E2E。

**不做（本期）**：账号硬删、角色继承/模板、权限码按人直授（§1.2 已定）、账号列表的
数据范围裁剪（权限账号管理是**租户级**操作，见 §18.2 决策 1 方案 A 的风险栏）。

### 18.2 候选方案

#### 决策 1：权限账号页怎么列出账号

| 方案 | 做法 | 优点 | 缺点 / 成本 | 风险 | 对 design.md 的影响 |
|---|---|---|---|---|---|
| **A（推荐）** 新增 `GET /school/permission-accounts`（`school.permission_account.manage`，分页），返回 `id` / `display_name` / `user_type` / `status` / `role_codes` / `roles_version` / `mfa_enabled`，**不回显 `login_id`** | 运营能浏览、能按角色与状态筛；契约与已有 `UserLookupResponse` 同族（都不回显 login_id） | 要写端点 + 查询 + 用例（**不需要迁移**：读 `users` / `user_roles` 现成表） | **列表不按人员数据范围裁剪**——权限账号管理是租户级操作，按 `school`/`self` 裁会让管理员看不到全校账号；这一点要写进契约与文档，否则后来者会「顺手」加范围过滤 | 无新增权限码；按 `user_type in ('teacher','school_admin')` 过滤（与 `BINDABLE_USER_TYPES` 同一集合） |
| B 复用 `GET /school/users/lookup?login_id=`，权限账号页 = 搜索框 + 单账号卡片 | 零后端改动 | 运营必须知道登录名才能定位；查不到「谁持了某角色」；一次只能管一个账号；`lookup` 每次都写审计（查十个人十行审计） | 页面价值≈0，只能算「把已有按钮摆好看」 |
| C 复用 `GET /school/teachers` + `GET /school/students` | 零后端改动 | 不用写后端 | 要**两个**权限码（`school.personnel.view` + `school.permission_account.manage`），只持其一的账号打不开页面；教师响应没有角色/状态字段，要再发 N 次单账号请求补齐 | **绕过 `permission_account.manage` 就把全校人员目录递出去** —— 违反最小权限；直接否决 |

#### 决策 2：两个权限码怎么落（§14.10 的延后项）

| 方案 | 做法 | 优点 | 缺点 / 成本 | 风险 | 对 design.md 的影响 |
|---|---|---|---|---|---|
| **A（推荐）** 新增 `GET /school/bindable-roles`（`school.permission_account.manage`），只返回可授角色的 `id` / `code` / `name` / `is_system`，**不含 `permission_codes`** | 只持绑定权的账号能独立完成绑定；最小权限——绑定页不需要知道角色有哪些权限；不把完整权限矩阵泄给只管绑定的人 | 多一个只读端点（约 60 行 + 用例） | 读路径的「可授」判据必须与写路径**同一口径**：写路径挡「目标持超管 → 403」「提交超管角色 → 422」，读路径至少要把 `bindable=false` 的超管角色**不列出来**（列出来 = 能勾不能存） | 无 |
| B 让 `GET /school/roles` 接受「二选一」（`role.manage` **或** `permission_account.manage`） | 零新端点 | 少写一个端点 | 响应形状随调用方变；绑定权账号会拿到**完整 `permission_codes`** —— 权限矩阵外泄给不需要它的人；`role.manage` 账号会看到 `bindable=false` 的超管角色 | §14.10 说的「二选一」省下的只是一个端点，代价是响应形状分叉 + 矩阵外泄；不采纳 |
| C 绑定页要求同时持两个码 | 零新端点 | 最简单 | 运营得多给一个码才能绑角色 —— **绑定权与配角色权是两个独立职责**（决策 #6「删角色另要 `school.role.delete`」就是同一条思路：拆开才不让一个人干所有事） | 与既定方向相反 |

#### 决策 3：187g 拆几个 PR

| 方案 | 做法 | 优点 | 缺点 / 成本 | 风险 |
|---|---|---|---|---|
| **A（推荐）** 拆 3 个：**187g-A** 后端契约补齐（`resettable` 下发 + `name` CHECK 迁移 + 两个新只读端点）/ **187g-B** 前端角色页（新建、删除、范围、`resettable`）/ **187g-C** 前端权限账号页（列表、绑定、启停、MFA） | 每块可独立验证、独立回滚；前端大改与后端契约分开，出错能定位在哪一侧 | 3 轮审查 + 3 次 CI（时间成本） | 187g-A 合入后前端还没用它 → 中间态是「后端先到」，不影响线上（没人调） |
| B 拆 2 个：后端一个、前端（两个页面）一个 | 少一轮 CI | 前端 diff 巨大（两个页面 + 6 个端点 + mock），审查难度高 | 一处出错要整体回滚 |
| C 一个大 PR | 最快 | diff 跨前后端 + 迁移 + 两个页面，违反 AGENTS.md §1.8「一次只提交一个可运行逻辑单元」 | 不采纳 |

### 18.3 选择结论（**用户 2026-10-30 已拍板，三条全按推荐**）

**用户裁决**：① 权限账号页用**新增列出接口**（`GET /school/permission-accounts`）；② 两个权限码用
**新增只列角色的接口**（`GET /school/bindable-roles`）；③ 187g **拆 3 个 PR**（A 后端契约 / B 前端角色页 /
C 前端账号页）。三条推荐的理由如下，落地时不得偏离。

**决策 1 选 A**（新增 `GET /school/permission-accounts`）：理由三条——
① B/C 都做不出「浏览权限账号」这件事，而这是权限账号页存在的意义；
② C 会把全校人员目录递给出不起 `permission_account.manage` 的人，直接违反最小权限；
③ 代价可控：不加权限码、不加迁移、不改写路径，只多一条只读端点 + 用例。

**决策 2 选 A**（新增 `GET /school/bindable-roles`）：理由三条——
① 它是三个方案里唯一**不外泄权限矩阵**的：B 会让只管绑定的账号拿到全量 `permission_codes`；
② 它把「绑定权」与「配角色权」保持成两个独立职责，与决策 #6 的拆分思路一致；
③ 少写一个端点的 B 方案省下的成本，远小于「矩阵外泄 + 响应分叉」的长期成本。

**决策 3 选 A**（拆 3 个 PR）：与 187f → 187f-A/187f-B 的既有做法一致；后端契约先落，
前端两个页面分开改，任何一块出问题都能单独回滚。

### 18.4 实现顺序（TDD 红 → 绿）

**187g-A（后端契约补齐）**

1. **迁移**：`roles` 加 `check (char_length(btrim(name)) > 0)`（TD-198）。**先验存量再动约束**——
   与 `d7a1c3e5b9f4` 同一做法：带着坏行加约束，PG 只会给一句看不懂的 `check constraint ... violated`。
2. **`resettable` 下发**（TD-200）：`RoleSummary` 加 `resettable: bool`，由「该角色码在
   `rbac_builtin_role_defaults` / `rbac_builtin_role_scope_defaults` 里有定义行」派生
   （与 `_preview` 判 `ROLE_DEFAULTS_UNAVAILABLE` 的**同一处**取值，不能各算各的）→ `RoleResponse` 下发。
   **再并上"角色可编辑"这一道门**（2026-10-30 独立审查 Major 1 修正）：重置实际要过
   `_assert_editable` → `ROLE_DEFAULTS_UNAVAILABLE` 两道门，只算"有出厂定义"会给校方超管/学生/教师
   这三个**内置锁定角色**点亮一个永远 409 的按钮——TD-200 要治的病换个方向复发。
   判据与 `docs/tech-debt.md` TD-200 原定的收口方式（"由 `_lock_reason` 那层一次算出"）一致。
3. **`GET /school/permission-accounts`**（决策 1 方案 A）：分页 + `user_type in ('teacher','school_admin')`
   过滤 + 每行的 `role_codes`（join `user_roles` → `roles.code`）+ `roles_version` + `status` + `mfa_enabled`；
   不回显 `login_id`。
4. **`GET /school/bindable-roles`**（决策 2 方案 A）：只返回 `id`/`code`/`name`/`is_system`；
   `bindable=false` 的超管角色不列。
5. 用例：两个新端点的权限码 / 跨租 / 过滤 / 形状；`resettable` 在「有定义 / 无定义」与
   「可编辑 / 锁定」四种组合上取值正确（锁定角色恒 false）；迁移的库层不变量用例
   （`name` 全空白插入被 CHECK 挡）。

**187g-B（前端角色页）**

1. `rbac-logic.ts`（或新的 `role-page-logic.ts`）加纯逻辑：新建角色表单校验（`code` 正则 +
   `name` 去空白非空，与后端同判据）、删除确认校验（逐字 code + 理由 ≥10）、范围编辑差异
   （`scopeDiff`）、范围保存前的「重取快照」决策。
2. 页面：新建角色对话框、删除角色（预览 → 确认框 → 执行）、范围编辑区（可改两类 + 只读两类 + 原因）。
3. **TD-200**：`role-reset` 按钮改跟 `resettable` 走（`is_system` 只做标签）。
4. **TD-202**（权限/范围共享 `roles.version`）：范围区保存前**重取快照**、409 时**自动采用 latest**
   （沿用权限区已有的 `conflictedLatestRole` 路径），两个编辑区不做「同时脏」。
5. mock E2E：新建 / 删除 / 范围保存 / 409 自愈 / `resettable=false` 时按钮不亮。

**187g-C（前端权限账号页）**

1. 路由 `/settings/accounts`，`meta.permission = "school.permission_account.manage"`；
   菜单项按权限显示（`AdminLayout`）。
2. 账号列表（角色码、状态、类型、MFA）→ 点某行 → 绑定抽屉（`GET/PUT /users/{id}/roles`，
   角色清单来自 `GET /school/bindable-roles`）+ 停用/启用（理由 ≥10 字）+ MFA 重置。
3. 自锁/超管保护在界面层提示（后端 403 才是真边界）。
4. mock E2E：列表渲染 / 改绑定（捕获 `PUT` 体）/ 停用（理由校验）/ 启用 / 403 提示。

**每个 PR 的收尾**：本地受影响子集 + 静态门禁 → 独立审查 → commit → push → PR → 远端 CI 全绿 →
rebase 合并 → main CI 全绿 → 删分支 → 文档回填 PR → §9 八段汇报。

### 18.5 测试策略与门禁

- **单元（前端）**：纯逻辑（表单校验、范围差异、删除确认）；沿用 `rbac-logic.test.ts` 的写法。
- **单元/集成（后端，187g-A）**：两个新端点的权限与形状；`resettable` 派生；`name` CHECK 的库层不变量。
- **mock E2E**：两个页面各一个 spec 文件（`frontend/e2e/admin-role-pages.spec.ts`、
  `admin-permission-accounts.spec.ts`），mock helper 扩 `rbac-mock.ts`（有状态：绑定/启停改 mock 内部状态）。
- **门禁**：后端 `ruff`/`mypy`/`bandit`/`alembic check`（187g-A 有迁移）；前端 `lint`/`format`/`typecheck`/单测/mock E2E。
- **无全栈 E2E**：本批不做（TD-183/192 的全栈 E2E 仍在 backlog；本地 8000 端口被占）。

### 18.6 预判技术债

- **TD-237**：`GET /school/permission-accounts` 不按人员数据范围裁剪（租户级操作），
  将来若要「校方管理员只能管自己片区的账号」，需要另加范围维度；本期写进契约注释与 design.md。
- **TD-238**：`GET /school/bindable-roles` 与 `GET /school/roles` 是两份角色目录，
  将来若角色目录再加字段（默认范围、标签），要决定是否合并（合并 = 绑定权账号拿到矩阵，回到决策 2 方案 B）。
- **TD-242（2026-10-30 独立审查 m9 追加）**：权限账号列表里**会出现超管账号**（超管持有者是
  `school_admin` 类型，落在 `BINDABLE_USER_TYPES` 内），但给它绑角色必然 403
  `BINDING_SUPER_ADMIN_FORBIDDEN`（决策 D 第 2 条）。**TD-187g-A 已把判据补成服务端派生的
  `PermissionAccountResponse.is_super_admin`**（从同一批 `role_codes` 派生，不另开 join），
  TD-187g-C 的绑定抽屉据此**不给超管账号可勾选的角色**，并给一句只读说明；不能"点了再弹
  403"兜底，也**不要在前端按 `role_codes` 硬认超管**——角色集合一变判据就失灵。
- **TD-243（2026-10-30 独立审查 m2，已在本 PR 修）**：分页游标的时间戳若不带时区，PG 按会话
  时区解释，手造的"未来时刻"游标会让翻页**悄悄回到第一页**。`decode_cursor` 现在要求带时区，
  naive 直接 400（`tests/test_org_scope_sync_unit.py::test_decode_cursor_rejects_timestamp_without_timezone`）。
- **TD-244（2026-10-30 独立审查 m3，已在本 PR 修）**：迁移的存量预检会被 RLS 遮蔽成 0 行
  （`roles` 已 FORCE ROW LEVEL SECURITY）。本点迁移按 MFA 迁移的同款写法加了
  `_assert_can_bypass_rls()`——迁移连接不是超级用户/BYPASSRLS 就显式报错，不默默数成 0。
- **TD-245（2026-10-30 独立审查提的边界）**：`bindable-roles` 会把内置角色的 `id` 给只持账号
  管理权的人，配合绑定写路径可把内置角色绑给任意教师账号。提权面有限（内置角色不含
  `school.role.manage`），且绑定路径按 TD-194 决策**不接**自扩权守卫——已拍板的边界，不是漏洞。
  但要写进文档：将来给自定义角色加"可绑定"标记前，必须评估它的权限码会不会经绑定流出去。
- **MFA 重置入口的位置**：M2-UI2 把它放在角色权限页底部（「安全工具」卡片）。
  权限账号页做出来后它更贴切，但**搬走要动 M2-UI2 已交付的页面与它的 E2E**——
  本期**不搬**（A5 允许留在原处），登记为后续可选项。
- **TD-198 的迁移窗口**：`name` CHECK 只影响写入路径，历史行里若有全空白名会挡住迁移 → 迁移前验存量并报错。
- **`resettable` 与 `is_system` 并存**：两个字段都留着，但只有一个是判据（`resettable`）；
  将来若给自定义角色加「按模板初始化」，`resettable` 不用改、`is_system` 也不用改。

### 18.7 高风险确认

本点动的是**权限面**（角色增删、范围编辑、账号绑定、账号启停），按 AGENTS.md §11.7 属「权限」类，
**需用户确认后才编码**。**用户 2026-10-30 已拍板**（三条全按推荐，见 §18.3）：

1. **决策 1**：权限账号页用**新增列出接口** `GET /school/permission-accounts`。
2. **决策 2**：两个权限码用**新增只列角色的接口** `GET /school/bindable-roles`（不含 `permission_codes`）。
3. **决策 3**：187g **拆 3 个 PR**（A 后端契约补齐 / B 前端角色页 / C 前端权限账号页）。

本节只记录这三条已拍板结论；实现过程中的其余取舍按 §18.4/§18.6 执行，不再逐条回头确认。

## 19. TD-187g-B：前端角色页（新建 / 删除 / 数据范围 / `resettable` 判据）

> 状态：**用户 2026-10-30 已拍板**（§19.7 四条全按推荐）。TD-187g 拆 3 个 PR 的决策见 §18.3 决策 3，
> 本节是 §18.4「187g-B（前端角色页）」的展开。本点是**纯前端 PR**——后端契约已随 PR #18
> 合入 main（合并提交 `8936c3f`），本点**不改后端一行代码**。

### 19.1 目标与验收

**范围**：`docs/milestone.md` §5.2 T7 里「角色页」的那一半（账号页那半是 TD-187g-C）。

**现状核对（2026-10-30 读代码得到的事实，不是设想）**：

| 后端能力 | 端点 | 权限码 | 前端入口 |
|---|---|---|---|
| 自定义角色创建 | `POST /school/roles` | `school.role.manage` | **无** |
| 删除预览 | `POST /school/roles/{id}/delete/preview` | `school.role.manage` | **无** |
| 删除执行 | `POST /school/roles/{id}/delete` | **`school.role.delete`** | **无** |
| 角色级数据范围读 | `GET /school/roles/{id}/scopes` | `school.role.manage` | **无** |
| 角色级数据范围预览 | `POST /school/roles/{id}/scopes/preview` | `school.role.manage` | **无** |
| 角色级数据范围写 | `PUT /school/roles/{id}/scopes` | `school.role.manage` | **无** |
| 权限码替换 | `PUT /school/roles/{id}/permissions` | `school.role.manage` | 有（M2-UI2） |
| 恢复出厂 | `POST /school/roles/{id}/reset/{preview,}` | `school.role.manage` | 有，但按钮跟 `is_system` 走 |
| 角色列表 / 权限目录 | `GET /school/roles` · `GET /school/permissions` | `school.role.manage` | 有 |

前端现状：`frontend/apps/admin/src/views/RoleSettingsView.vue`（636 行）、
`frontend/apps/admin/src/rbac-logic.ts`（147 行）、`rbac-logic.test.ts`（236 行）、
`frontend/e2e/admin-roles.spec.ts`（483 行）、`frontend/e2e/helpers/rbac-mock.ts`（382 行）、
`frontend/packages/api-client/src/rbac.ts`（118 行）。

**三处契约缺口**（前端补，后端不用改）：

1. `RoleView` 缺 `bindable`（TD-187d 下发）与 `resettable`（TD-200，PR #18 下发）——
   `frontend/` 全仓 grep `resettable|bindable` **0 命中**。
2. `RoleResetInput` 缺 `self_grant_ack`——后端 `RoleResetPayload` 自 TD-194 起就有这个字段，
   前端 `rbac.ts:48-52` 没带（今天没人给「恢复出厂」传 ack，是因为前端还没接自扩权那半）。
3. 新端点一个都没接：`createRole` / `previewRoleDeletion` / `deleteRole` / `listRoleScopes` /
   `previewRoleScopes` / `replaceRoleScopes`。

**一个必须先知道的事实（影响决策 B2）**：**角色码与角色名的格式校验在 HTTP 路径上落不到后端的
业务错误契约**。`RoleCreatePayload.code` 挂 `pattern=ROLE_CODE_PATTERN`（`^[a-z][a-z0-9_]{2,62}$`，
`backend/app/rbac/admin.py:75`）、`name` 挂 `@field_validator` 抛 `ValueError`——**这两条都撞 Pydantic**，
返回的是 FastAPI 的**数组形 422**（`[{loc,msg,type}]`），**不是** `{code,message}` 形状。
而服务层那道 `ROLE_NAME_NOT_BLANK`（`admin.py:1266`）只在**直调服务层**时命中，走 HTTP 时 Pydantic 先拦了。
→ 前端不预检，用户提交空名字会拿到 `String should match pattern ...` 这种读不懂的提示。

**验收（对应 §18.1 的 A1–A4 角色页那半）**：

- **B1** 能新建自定义角色：`code` / `name` 的前端校验与后端**同一判据**（TD-198 表单侧那半）；
  初始权限二选一（显式清单 / 复制同租户可编辑角色）能被表达；同名同码冲突（409 `ROLE_CODE_TAKEN`）
  有可读提示。
- **B2** 能删除自定义角色：两步（预览 → 确认）→ 逐字 code + 理由 ≥10 字 + `version` + **影响面指纹**
  （TD-199）原样回传；`409 ROLE_DELETE_PREVIEW_STALE` 时回带最新影响面，界面能重试。
- **B3** 能编辑角色级数据范围：可改 `school`/`self` 两类；`class`/`course` 只读展示并写清
  「为什么不可改」（§13.2 决策③：这两类必须绑具体 id，本端点没有那个入参通道）；
  空清单合法且预览提示「N 名持有人将看不到任何数据」。
- **B4** 「恢复出厂」按钮跟服务端下发的 **`resettable`** 走，**不再跟 `is_system` 走**（TD-200）；
  `is_system` 降级为纯展示标签（内置/自建）。
- **B5** 权限区与范围区共用 `roles.version`（TD-202）时**不静默互相踩**：范围保存 409 自动采用
  `latest`（沿用现有 `conflictedLatestRole` 路径）。
- **B6** 纯逻辑抽 `rbac-logic.ts` 走单测；配 mock E2E。

**不做（本期）**：账号↔角色绑定、账号启停、MFA 重置入口搬迁（属 TD-187g-C / §18.6 已定）、
角色继承/模板、权限码按人直授、账号硬删。

### 19.2 候选方案

#### 决策 B1：可手工授的数据范围类型清单（`{school, self}`）怎么给前端

| 方案 | 做法 | 优点 | 缺点 / 成本 | 风险 | 对后端的影响 |
|---|---|---|---|---|---|
| **A（推荐）** 前端复刻一份常量 `EDITABLE_SCOPE_TYPES = {school, self}`，单测把字面量钉死并注释指向 `admin.py:83` | 不动后端；本点保持**纯前端 PR**（§18.3 决策 3） | 零契约变更；后端仍是唯一门禁（提交不可授类型 → 422 `SCOPE_TYPE_NOT_EDITABLE`） | 后端将来加一类可授范围，前端要跟着改；不同步时**少一个可选项**（不是给出错误选项） | 失败模式是「少给一个选项」，不是「做错操作」——运营点不到但不会做错 | 无 |
| B 后端在 `GET /school/roles/{id}/scopes` 的响应里下发清单 | 响应从 `list[...]` 改成对象 | 前端零复刻 | **改已交付端点的响应形状** → 破坏已有后端用例；且本点变前后端混合 PR，与 §18.3 决策 3 冲突 | 前端从"少一个选项"变成"多一个错选项"（后端把 `class` 列成可授）——但后端自己不会这么写 | 改契约 + 改后端用例 |
| C 新增只读端点下发清单 | 新端点 | 前端零复刻 | 多一个端点、多一次请求；且要回答「权限码是什么」——它是**租户域**的，端点要挂哪个码又是一次决策 | 同 B | 新端点 + 决策 |

#### 决策 B2：角色码 / 角色名的格式校验放哪

| 方案 | 做法 | 优点 | 缺点 / 成本 | 风险 | 对后端的影响 |
|---|---|---|---|---|---|
| **A（推荐）** 前端复刻 `code` 正则与 `name` 去空白非空两条判据，提交前本地拦 | 前端预检 | 用户拿到中文提示（「角色码只能是小写字母开头的 3–63 位小写字母/数字/下划线」「角色名不能为空或全空白」）；数组形 422 不再出现在这条路 | 前端复刻了一份**格式规则**（不是内容清单）——后端改正则时前端要跟着改 | 复刻漂移的后果是「前端放行、后端 422（数组形）」，不是「前端拦住合法输入」 | 无 |
| B 后端把 `code`/`name` 的格式校验从 Pydantic 下放到服务层，返回 `{code,message}` | 契约升级 | 前端零复刻；错误契约统一（与 §18.1 A4 同族） | 改**已交付**端点的错误形状 → 已有后端用例要改；本点变前后端混合 PR | 改已交付契约要过一轮审查，且会让 187g-A 的「契约已定」结论作废 | 改契约 + 改用例 |
| C 前端只做粗校验（长度 + 字符集），精确交给服务端 | 折中 | 复刻更少 | 非法码仍然撞数组形 422 | 用户体验没解决 | 无 |

#### 决策 B3：新建角色的「初始权限」怎么在界面上表达

后端 `RoleCreatePayload` 是**二选一**：给 `permission_codes`（默认空集）**或** `copy_from_role_id`
（复制同租户可编辑角色）。**两份非空同时给 → 422 `ROLE_INITIAL_SOURCE_AMBIGUOUS`**（后端明确
「不选以显式清单为准、静默忽略复制源」——配置类接口不能猜）。

| 方案 | 做法 | 优点 | 缺点 / 成本 | 风险 |
|---|---|---|---|---|
| **A（推荐）** 对话框里单选「从空白开始 / 复制现有角色」；选复制时选一个**可编辑**角色；权限勾选框只在「空白」模式下可用 | 与后端二选一契约一一对应，用户不可能表达出后端拒绝的组合 | 复制源下拉要只列 `editable=true` 的角色 | 复制时看不到「会复制哪些权限码」的预览（后端也没提供这个预览端点） | 低 |
| B 只做「从空白开始」，复制本期不做 | 最省 | 运营想「照着教师 B 端建一个」得手抄一遍权限码；后端能力白给 | 低 |
| C 两个独立控件（复制源下拉 + 权限勾选区），提交时以勾选为准 | 界面上更自由 | 用户可能同时填了两份，提交后 422 `ROLE_INITIAL_SOURCE_AMBIGUOUS`（或更糟：以为复制生效了） | 中 |

#### 决策 B4：删除入口按哪个权限码显示

后端**预览**要 `school.role.manage`、**执行**要 `school.role.delete`（决策 #6：删角色另给专用码）。
页面路由本身要 `school.role.manage`，所以「只有 `role.manage`、没有 `role.delete`」的管理员
**能进页面、能看到角色、但删不了**。

| 方案 | 做法 | 优点 | 缺点 / 成本 | 风险 |
|---|---|---|---|---|
| **A（推荐）** **执行控件**按 `school.role.delete` **是否在 `auth.user.permissions` 里**决定给不给；**入口**按「是不是内置角色」决定给不给（内置角色服务端恒 409 `ROLE_NOT_DELETABLE`，给了也是死路）。没有执行码时对话框降级为**只读影响面 + 一句「你没有删除角色权限（school.role.delete）」**，不给必然 403 的按钮 | 界面与后端权限一致；权限拆分（决策 #6）在前端也看得见 | 多一条只读分支 | 无 |
| B 入口按 `school.role.manage` 显示，执行时才 403 | 简单 | 给一个必然 403 的按钮——死路 | 中 |
| C 两个码都要才显示入口 | 简单 | 把「能预览影响面」也藏了，运营连影响面都看不到 | 低 |

> **2026-10-30 实施后修正（独立审查 m13）**：本节 A 方案原措辞「删除按钮按 `school.role.delete` 决定显不显示」自相矛盾——它随后又说没有执行码时要给只读影响面（那就必须先把入口给出来）。
> 落地的口径是**入口与执行分成两件事**：入口按「是不是内置角色」给（自定义角色恒给，内置角色不给），
> **执行控件**按 `school.role.delete` 给；缺执行码时入口文案变成「查看删除影响面」，对话框只给影响面
> 不给确认表单。§19.3、§19.4 的同一处措辞一并按此修正。

### 19.3 选择结论

**决策 B1 选 A**（前端复刻清单 + 单测钉字面量 + 登记 TD）：理由——① 本点的核心约束是
「保持纯前端 PR」（§18.3 决策 3 已拍板），B/C 都要动后端；② 失败模式是**少一个可选项**，
不是给出错误选项（后端仍是唯一门禁，提交不可授类型会 422 `SCOPE_TYPE_NOT_EDITABLE`）；
③ 复刻的是**可授清单**这一条窄规则，且用单测把字面量钉死并注释指向后端常量行号。
**登记 TD-247**：可手工授的范围类型清单未由后端下发，前端复刻了一份（已延后到 M3 / 与 TD-226 同批）。

**决策 B2 选 A**（前端预检 `code` 正则 + `name` 非空）：理由——① 撞 Pydantic 的两条校验在 HTTP
路径上返回**数组形 422**，用户读不懂，前端不预检就是让用户去撞它；② B/C 要么改已交付契约、
要么体验没解决；③ 复刻的是**格式规则**（不是内容清单），与 `sensitive_self_grant` 那条
「清单必须后端下发」的原则不冲突——那条原则针对的是**会增删的内容集合**，格式规则前端必须知道
才能做即时校验。**后端改正则时前端要跟着改**：登记 TD-248（格式规则双份维护，钉在单测里）。

**决策 B3 选 A**（单选二选一 + 复制源只列可编辑角色）：理由——后端契约就是二选一且明确拒绝
「两份非空」，界面必须能表达这个约束；C 会造出后端拒绝的组合。

**决策 B4 选 A**（**执行控件**跟 `school.role.delete`，无执行码时给只读影响面）：理由——决策 #6
把「能配」与「能删」分成两个码，前端若不跟着分，就等于把后端特意拆开的职责又合回去；
B 会给一个必然 403 的按钮；C 会把「能预览影响面」也藏了（运营连影响面都看不到）。
**落地口径（2026-10-30 实施后修正，见 §19.2 的说明）**：入口按「是不是内置角色」给（自定义角色恒给），
**执行控件**按 `school.role.delete` 给；缺执行码时入口文案改成「查看删除影响面」、对话框降级为只读预览。

### 19.4 实现顺序（TDD 红 → 绿）

**第 0 步（契约对齐，先做）**：`frontend/packages/api-client/src/rbac.ts`
1. `RoleView` 加 `bindable: boolean` + `resettable: boolean`（后端 PR #18 已下发）。
2. `RoleResetInput` 加 `self_grant_ack?: boolean`。
3. 新增类型与方法：`RoleCreateInput` / `createRole`、`RoleDeletionPreview` /
   `previewRoleDeletion` / `RoleDeleteInput` / `deleteRole`、`RoleScopeRow` /
   `listRoleScopes`、`ScopeChangePreview` / `previewRoleScopes`、`RoleScopeInput` /
   `replaceRoleScopes`。
4. **同步**：`rbac.test.ts` 的 `ROLE_VIEW` fixture 加两个字段；`rbac-mock.ts` 的 `RoleFixture`
   加两个字段（**必填**，让 TS 把所有漏改的 fixture 点出来）。

**第 1 步（纯逻辑，先红后绿）**：`frontend/apps/admin/src/rbac-logic.ts` + `rbac-logic.test.ts`
- `ROLE_CODE_PATTERN`（字面量复制 + 注释指向 `admin.py:75`）、`roleCodeError(code)`、
  `roleNameError(name)`（TD-198 表单侧）。
- `deleteConfirmError(roleCode, confirmCode, reason)`（逐字 code + 理由 ≥10，与
  `resetConfirmError` 同型）。
- `EDITABLE_SCOPE_TYPES`、`SCOPE_LABELS`、`scopeDiff(current, next)`。
- `roleDeleteGate(permissions)` → 能不能显示删除入口 / 能不能执行。
- `resetUnavailableReason(role)` → `resettable=false` 时的一句人话（前端不再自己判 `is_system`）。

**第 2 步（页面）**：`RoleSettingsView.vue`
1. **「恢复出厂」按钮改跟 `resettable` 走**（`RoleSettingsView.vue:425`）——`is_system` 只做标签。
2. **新建角色对话框**：code / name / 初始权限二选一 / 复制源 / 理由。
3. **删除角色**：入口按「是不是内置角色」给（自定义角色恒给），**执行控件**按
   `school.role.delete` 给（缺码时对话框降级为只读影响面）；预览（影响面 + 指纹）→ 确认框
   （逐字 code + 理由）→ 执行（回传 `version` + `fingerprint`）；`ROLE_DELETE_PREVIEW_STALE` 时
   回带最新影响面并可重试。
4. **数据范围卡片**：读 `GET /roles/{id}/scopes` → 可改两类 checkbox + 不可改两类只读说明 →
   预览（added/removed/affected_users/empties_scope/retained_non_editable/newly_reaching_self/
   requires_confirmation）→ 确认 → `PUT`（带 `version` + `reason` + `self_grant_ack`）。
5. **TD-202**：范围区保存用**当前** `role.version`（权限区保存后 `version` 已 +1 并回写 `roles`
   数组 → 自动同步）；409 走现有 `applyConflict`（载入 `latest` + 提示），不静默覆盖。

**第 3 步（mock E2E）**：`frontend/e2e/helpers/rbac-mock.ts` 扩路由与 `captures`
（create / deletePreview / delete / scopes 读 / scopes 预览 / scopes 写，**有状态**：删掉后列表里
真的没有它）；新建 `frontend/e2e/admin-role-pages.spec.ts`；现有 `admin-roles.spec.ts` 只改
fixture 字段与「恢复出厂」那条用例的判据注释。

**第 4 步（收尾）**：本地前端门禁（lint / format / typecheck / 单测 / mock E2E）→ 独立审查 →
commit → push → PR → 远端 CI 全绿 → rebase 合并 → main CI 全绿 → 删分支 → 文档回填 PR → §9 八段汇报。

### 19.5 测试策略与门禁

- **单元（`rbac-logic.test.ts`）**：新建表单校验（code 正则的**每个**拒绝分支 + name 全空白）、
  删除确认校验、范围差异、`roleDeleteGate`（有/无 `school.role.delete`）、
  `resetUnavailableReason`。
- **单元（`packages/api-client/src/rbac.test.ts`）**：新方法的路径 / 方法 / 请求体形状
  （尤其 `deleteRole` 必须带 `fingerprint`、`replaceRoleScopes` 必须带 `scope_types`+`version`）。
- **mock E2E（`admin-role-pages.spec.ts`）**：新建成功 / 同码冲突 409 有提示 / 删除两步（预览 →
  确认 → 执行，断言捕获的请求体带 `fingerprint` 与 `version`）/ 删除理由不足 10 字不发请求 /
  `resettable=false` 时「恢复出厂」按钮**不出现** / 范围保存（断言 `PUT` 体）/ 范围 409 自愈 /
  范围预览 `empties_scope` 提示 / `class`/`course` 行不可改且有说明。
- **门禁**：前端 `lint` / `format` / `typecheck` / 单测 / mock E2E；**后端零改动**，
  本点不需要 `ruff`/`mypy`/`bandit`/`alembic check`（`app/**` 与 `alembic/**` 都不碰）。
- **无全栈 E2E**：本批不做（TD-183/192 仍在 backlog；本地 8000 端口被占）。

### 19.6 预判技术债

- **TD-247（新增）**：可手工授的数据范围类型清单未由后端下发，前端复刻了
  `EDITABLE_SCOPE_TYPES = {school, self}`。失败模式是「少一个可选项」不是「做错操作」，
  故本期不阻塞；处置 = 已延后（M3 或与 TD-226 的 API 契约统一同批）。
- **TD-248（新增）**：角色码格式规则（`^[a-z][a-z0-9_]{2,62}$`）在前后端各存一份。
  用单测把字面量钉死并注释指向后端常量行号；后端改正则时必须同批改前端。
- **TD-239（既有余债，本点相关）**：库层 `roles.name` 的 CHECK 只覆盖 ASCII 空白，
  接口层用 Python `strip()` 更严（U+3000 接口拒、库收）。**前端表单按 `strip()` 口径**
  （全 Unicode 空白）→ 与接口层一致，与库层差一档（库收、前端拒，方向安全）。
- **TD-202（既有余债，本点缓解）**：权限区与范围区共享 `roles.version` → 一侧保存会让另一侧草稿
  409。本点缓解：**保存时用当前 `role.version`**（权限保存后 `version` 已回写），
  409 时自动采用 `latest` 并提示。**没有**做「两个区同时脏」——那需要两个独立版本列（后端改动），
  本期不做。
- **MFA 重置入口仍在角色页**：权限账号页（TD-187g-C）做出来后它更贴切，但搬走要动 M2-UI2
  已交付的页面与它的 E2E → 本期**不搬**（§18.6 已定），187g-C 收尾时复核。
- **`school.role.delete` 缺失时的半死路**：按决策 B4 处理（只读影响面 + 一句说明），
  不是新债，但**要在界面上说清楚**，否则运营会以为按钮坏了。

### 19.7 高风险确认

本点动的是**权限面**（角色增删、范围编辑），按 AGENTS.md §11.7 属「权限」类，
**需用户确认后才编码**。§18.7 已拍板的三条（决策 1/2/3）覆盖了 TD-187g 的整体形状。
**§19.2 的四条取舍（B1–B4）用户 2026-10-30 已拍板，四条全按推荐**：

| 决策 | 拍板结论 | 关键理由 |
|---|---|---|
| **B1** 可手工授范围类型清单 | **前端自己写一份**（`EDITABLE_SCOPE_TYPES = {school, self}`） | 保持本点为纯前端 PR；失败模式是「少一个可选项」不是「做错操作」；后端仍是唯一门禁（422 `SCOPE_TYPE_NOT_EDITABLE`） |
| **B2** `code`/`name` 格式校验 | **前端先查一遍** | 这两条撞 Pydantic → 数组形 422，用户读不懂；后端那道 `ROLE_NAME_NOT_BLANK` 只在直调服务层时命中，走 HTTP 时 Pydantic 先拦 |
| **B3** 新建角色初始权限 | **单选，只能选一种** | 后端契约就是二选一且明确拒绝「两份非空」；界面必须能表达出这个约束 |
| **B4** 删除入口权限码门禁 | **按 `school.role.delete` 显示** | 决策 #6 特意把「能配」与「能删」分成两个码；没有执行码时给只读影响面 + 一句说明，不给必然 403 的按钮 |

落地时不得偏离这四条。


## 20. TD-187g-C：前端权限账号页（列表 / 绑定 / 启停 / MFA 重置）

> 状态：**待拍板**（§20.2 的 C1、C2、C5、C6 四张卡需用户裁决；C3、C4 由契约事实与已拍板结论
> **唯一确定**，列出来只为留痕，不重开决策）。本节是 §18.4「187g-C（前端权限账号页）」的展开，
> 对应 §18.1 验收 **A5**，也是 M2-UI3 的**最后一批**。
> **纯前端 PR**（§18.3 决策 3 的第 3 块）：`backend/` 一行不改——账号侧 8 个端点都已在 `main` 上
> （绑定与启停分别随 TD-187d / TD-187e 交付，两个只读列表端点随 187g-A / PR #18 → `8936c3f` 交付，
> MFA 重置与登录名定位随 M2-UI2 交付）。

### 20.1 目标与验收

**现状核对（2026-10-30 读代码得到的事实，不是设想）**：

| 后端能力 | 端点（前缀 `/api/v1/school`） | 权限码 | 前端入口 |
|---|---|---|---|
| 权限账号列表 | `GET /permission-accounts`（`school_rbac.py:767`） | `school.permission_account.manage` | **无** |
| 可授角色目录 | `GET /bindable-roles`（`:488`） | 同上 | **无** |
| 单账号绑定快照 | `GET /users/{id}/roles`（`:837`） | 同上 | **无** |
| 绑定全量替换 | `PUT /users/{id}/roles`（`:858`） | 同上 | **无** |
| 账号停用 | `POST /users/{id}/deactivate`（`:917`） | 同上 | **无** |
| 账号启用 | `POST /users/{id}/activate`（`:948`） | 同上 | **无** |
| MFA 重置（按 `user_id`） | `POST /users/{id}/mfa/reset`（`:891`） | `school.personnel.mfa.reset` | 有（角色页底部「安全工具」卡片，**只能先按登录名查**） |
| 按登录名定位 | `GET /users/lookup?login_id=`（`:809`） | `school.personnel.mfa.reset` | 有（同上卡片） |

前端现状（文件与行号）：`frontend/apps/admin/src/router/index.ts`（140 行，`settings/roles` 的
`meta.permission` 写法在 `:79-82`，权限守卫在 `:135-137`，**没有账号页路由**）；
`frontend/apps/admin/src/views/AdminLayout.vue`（MENU 数组 `:19-26` 共 6 项、`:33` 按权限过滤菜单）；
`frontend/packages/api-client/src/rbac.ts`（223 行，`RbacApi` `:144-162` 的 12 个方法里**账号侧 0 个**）；
`frontend/e2e/helpers/rbac-mock.ts`（786 行，无账号侧路由）。

**九条契约事实**（每条都在界面上有对应决策，先摆事实再谈方案）：

1. **列表没有搜索参数，也没有 `login_id`**。`GET /permission-accounts` 只接 `limit`（1–100，默认 50；
   上限 `binding.py:47`）与 `cursor`（≤256）（`school_rbac.py:770-771`）；行字段是
   `id/display_name/user_type/status/role_codes/roles_version/mfa_enabled/is_super_admin`（`:298`），
   **故意不回显 `login_id`**（学号/工号属敏感标识符，`:299` 注释）
   → 运营手里只有「姓名 + 角色 + 状态」，没有可直接查的工号。这条决定 **C1**。
2. **状态可达集合只有三个**：`active` / `locked` / `disabled`。列表按 `BINDABLE_USER_TYPES`
   （`binding.py:44` = `{teacher, school_admin}`）过滤并**排除 `deleted`**（SQL 在 `binding.py:402`）；
   DB CHECK `ck_users_status_valid`（`alembic/versions/779f731db342_m1a_tenant_base.py:153`）里**没有** `pending`
   → 标签映射只需三档；出现第四档说明后端契约变了，要显式暴露而不是静默显示英文原码。
3. **`is_super_admin` 是服务端派生的判据**（`binding.py:427`，从同一批 `role_codes` 派生，不另开 join）。
   §18.6 TD-242 已定：绑定入口据此关掉，**不许在前端按 `role_codes` 硬认超管**。
4. **两份「角色」来源不是同一个集合**：`GET /bindable-roles` 只列 `bindable=true` 的角色
   （判据 `role_is_bindable(code) = code != "school_super_admin"`，`backend/app/rbac/admin.py:257`）；
   `GET /users/{id}/roles` 的 `roles[]` **不过滤**，但每行带 `bindable`（`BoundRoleResponse:230`）。
   今天两个集合在「非超管账号」上恰好重合，**重合是巧合不是契约**（TD-245 明说将来自定义角色可能加
   「可绑定」标记）→ 见 **C3**。
5. **`PUT /users/{id}/roles` 是全量替换 + 乐观锁**：body `role_ids`（必填，空数组＝收回全部）+
   `version`（必填，取 `roles_version`）+ `reason`（可空，≤500）。
   **绑定理由没有 ≥10 字判据**（`binding.py:462` 只做 `strip()` 后空 → `None`），
   与停用 / MFA 重置的 ≥10 字**不是同一道门** → 绑定区不能写「理由至少 10 字」。
   （这正是 187g-B 审查 m2 记进 TD-252/TD-253 的那类坑：把别处的判据手抄过来。）
6. **三类写操作各有自锁禁令，且都在服务端**：改自己的绑定 → 403 `BINDING_SELF_FORBIDDEN`
   （`binding.py:472`，**在读目标之前**就判，也先于「目标不存在」）；停用自己 → 409
   `ACCOUNT_SELF_DEACTIVATE`；重置自己 MFA → 409 `MFA_SELF_RESET`；**启用没有自锁判据**
   （`school_rbac.py:948-953` 连 body 都没有）。`AuthUser.userId` 已下发（`auth.ts:23`）
   → 前端能认出「这行是我」，按 187g-B 决策 B4 的口径**不给必然失败的按钮，给一句为什么**。
7. **停用 / 启用 / MFA 重置都返回 204 No Content**（`school_rbac.py:891/917/948`）。服务层算出的
   `sessions_revoked` 条数**被路由丢弃**，只落在审计（`school.user.deactivated` / `school.user.mfa_reset`）
   → 界面**不能说**「已踢掉 N 台设备」，只能说「会话已失效，需重新登录」；要显示条数得先改后端契约
   （TD-235 同族，本期不做）。
8. **超管账号在三个方向上分别被挡**：改绑 403 `BINDING_SUPER_ADMIN_FORBIDDEN`、启停 403
   `ACCOUNT_SUPER_ADMIN_FORBIDDEN`、MFA 重置 403 `MFA_SUPER_ADMIN_FORBIDDEN`（三条码不同，文案各写一句）。
   另有一条容易踩的顺序：**已 `deleted` 的账号先判 404**，再判超管（`docs/review-log.md` 187e 审查已记）
   → 列表里看不到 `deleted`，前端不需要处理这一档。
9. **409 版本冲突只有绑定回带 `latest`**：`_binding_error` 的 409 载荷含 `latest`（最新快照），
   `_account_error` / `_mfa_error` **不带** → 绑定冲突能自动采用最新；启停 / MFA 冲突只能重读列表那一页。
   分页语义与 `GET /school/teachers` 同形：`next_cursor` **只在这一页填满 `limit` 时才非空**
   （`binding.py:431-435`）→ 「还有没有下一页」只能跟 `next_cursor` 走，不能按 `items.length < limit` 猜。

**验收（对应 §18.1 A5）**：

- **CA1** 有 `/settings/accounts` 路由 + 菜单项，门禁 `school.permission_account.manage`；
  直连 URL 权限不足由现有守卫挡到 403 页（`router/index.ts:135-137`）。
- **CA2** 列表显示「姓名 / 类型（教师·校方管理员）/ 状态（启用·锁定·停用）/ 角色码 / MFA」，
  游标分页能往后翻也能回翻（沿用 `PeopleView.vue:60-74` 的游标栈做法）。
- **CA3** 打开某账号 → 绑定区以 `GET /users/{id}/roles` 为基底，能勾能撤，保存发 `PUT` 且带 `version`；
  409 采用 `latest` 重载并提示，**不静默覆盖别人的修改**；当前持有但不在可授目录里的角色
  **不会被静默收回**（C3）。
- **CA4** 能停用（理由 ≥10 字前端先拦、不足不发请求）、能启用（无 body）；结果靠**重读那一页**反映，
  不靠前端手改本地状态。
- **CA5** 能重置 MFA，入口直接吃 `user_id`（不再要求先按登录名查一遍）。
- **CA6** 「这行是我」与「这行是超管」两种情况**不给必然失败的按钮**，各给一句只读说明；
  后端 403/409 仍是真边界。
- **CA7** 纯逻辑抽 `account-logic.ts` 走单测；页面配 mock E2E（`admin-permission-accounts.spec.ts`）。

**不做（本期）**：账号硬删、按人员数据范围裁剪列表（TD-237 已定：权限账号管理是租户级操作）、
给列表加服务端搜索（属后端 PR，破 §18.3 决策 3）、显示被撤销的会话条数（TD-235 同族）、
批量改绑 / 批量启停、把 MFA 卡片从角色页搬走（§18.6 已定本期不搬）。

### 20.2 候选方案

#### 决策 C1：运营怎么在列表里找到一个账号

事实前提：后端没有搜索参数、行里没有 `login_id`（事实 1）；唯一能按登录名精确定位的
`GET /users/lookup?login_id=` 挂的是**另一个码** `school.personnel.mfa.reset`，且它的响应
**不含 `roles_version`**（要绑定还得再发一次 `GET /users/{id}/roles`）。

| 方案 | 做法 | 优点 | 缺点 / 成本 | 风险 |
|---|---|---|---|---|
| **A（推荐）** 两段式：① 当前页本地按「姓名 / 角色码」过滤（纯前端、即时）；② 另给一个「按登录名精确定位」框，**只在持 `school.personnel.mfa.reset` 时显示**，命中后拿 `id` 直接开抽屉 | 保持纯前端 PR；`rbacApi.lookupUser()` 已存在且在用（`RoleSettingsView.vue:343`）→ 零新契约；只知道工号的人也能定位 | ① 只覆盖**已加载的那一页**，翻页后要重跑过滤；② 搜索框可见性挂在 MFA 码上，语义别扭，界面要写清「按工号精确定位需要 MFA 重置权限」 | 低：最坏是「找不到」，不会做错操作 |
| B 只做当前页本地过滤 + 翻页 | 最省，判据最干净 | 全校几百上千教师时要点很多页；「我知道工号」这条最自然的诉求满足不了 | 低（功能缺口，不是错判） |
| C 给后端 `GET /permission-accounts` 加 `q=` / `status=` | 搜索覆盖全校，语义最正 | **破 §18.3 决策 3 的纯前端约束** → 本点变前后端混合 PR、多一轮契约审查；且 `q` 若匹配 `login_id` 就把敏感标识符变成可枚举面（TD-237 那条最小权限考虑） | 中（工期 + 契约） |
| D 只用 `lookup` 当搜索 | 零新代码 | 只持 `permission_account.manage` 的人**一查就 403**——入口显示、点击必失败，正是 187g-B 决策 B4 拒绝的死路 | 高 |

#### 决策 C2：账号详情的形态

| 方案 | 做法 | 优点 | 缺点 / 成本 | 风险 |
|---|---|---|---|---|
| **A（推荐）** 点行 → `el-drawer`：打开时发 `GET /users/{id}/roles` 拿**新鲜**快照（`roles_version` + 生效权限并集），抽屉内放绑定区 + 启停 + MFA 重置 | 与角色页「列表 + 面板」同一心智；每次打开都重读 → 版本不陈旧；这一个 `GET` 正好给齐绑定所需的全部字段 | 抽屉要处理加载 / 失败 / 409 三种态 | 低 |
| B 表格行内展开（`el-table` expand） | 少一层弹层 | 展开仍要发 GET（否则 `version` 陈旧），等于把三个操作区塞进一行高度 | 中：可用性格局风险 |
| C 子路由 `/settings/accounts/:userId` | URL 可分享、刷新不丢 | 要新增路由 + 守卫 + 深链加载态；本点没有「把账号链接发给同事」的诉求 | 低，但成本不值 |

#### 决策 C3：绑定区的数据源（**本节唯一有静默数据丢失风险的点**）

| 方案 | 做法 | 优点 | 缺点 / 成本 | 风险 |
|---|---|---|---|---|
| **A（推荐）** 以 `GET /users/{id}/roles` 的 `roles[]` 为基底渲染**已持有**角色（各自按 `bindable` 决定能否撤），`bindable-roles` 只补「还没持有、可新增」的候选；提交集合 = 已持有 ∪ 新勾选，**不含**界面上根本没渲染的行 | 结构上杜绝「保存时把一个界面上看不见的角色静默收回」；将来 `bindable` 判据收窄（TD-245 明说会改）时不用回来改这里 | 已持有但 `bindable=false` 的行要只读展示 + 一句说明（今天只有超管命中，而超管整行已只读） | 无 |
| B 只用 `bindable-roles` 渲染全部 checkbox，勾上的提交 | 代码最短（一份目录 + 一个 Set） | 依赖「两集合今天重合」这个巧合（事实 4）；判据一变，持有人会被静默移出 | **中高**：187g-B 范围选择器 Blocker 的反向版本（那边是「多撤」，这边是「偷偷撤」） |
| C 本期不做绑定，只做启停 | 最省 | §18.1 A5 明写要能改绑定；后端 TD-187d 整条能力继续没人用 | 功能缺口 |

#### 决策 C4：超管账号 / 「这行是我」怎么挡（**沿用已拍板，不重开**）

§18.6 TD-242 定了超管口径，187g-B 决策 B4 定了「入口与执行分离、不给必然失败的按钮」，
事实 6 给了自锁禁令。本点落地口径：

- `is_super_admin=true` → 绑定 / 停用 / 启用 / MFA 重置**四个写控件都不给**，
  只读说明「该账号是校方超管，角色绑定与启停由平台开通流程管理，租户内不可修改」；
- `row.id === auth.user.userId` → 绑定 / 停用 / MFA 重置不给（三条自锁禁令），
  **启用**没有自锁判据 → 正常给；文案写「不能修改自己的账号（改绑/停用/重置 MFA 需由其他管理员操作）」；
- 缺 `school.personnel.mfa.reset` 码 → MFA 重置区不渲染（不是置灰后再 403）。

| 方案 | 做法 | 优点 | 缺点 | 风险 |
|---|---|---|---|---|
| **A（推荐，即上述口径）** 不给写控件 + 一句为什么 | 界面与后端权限一致，运营不会以为按钮坏了 | 多三条只读分支 | 无 |
| B 一律给按钮，失败再弹后端 403/409 | 代码最短 | 明知必然失败的按钮；审计里出现一批注定失败的拒绝记录 | 中（与已拍板的 B4 口径冲突） |

#### 决策 C5：MFA 重置入口放哪

| 方案 | 做法 | 优点 | 缺点 / 成本 | 风险 |
|---|---|---|---|---|
| **A（推荐）** 账号页给一个「重置 MFA」区（直接吃 `user_id`），角色页那张「安全工具」卡片**原样留着** | 从列表就能重置，不用再想登录名；不动 M2-UI2 已交付页面与它的 E2E（A5 明确允许不搬） | 两个入口 → 理由校验必须同源（同一个纯函数），登记 TD | 低 |
| B 搬走（删角色页卡片） | 只有一个入口 | 要动 M2-UI2 已交付页面 + 改它的 E2E；本点 diff 变大，且删掉有人用惯的旧入口 | 中 |
| C 账号页不做 MFA，仍靠角色页那张卡 | 最省 | 从账号列表想重置 MFA 得先知道登录名再切页面——而列表**故意不显示登录名**（事实 1），这条路对本页用户几乎走不通 | 功能缺口 |

#### 决策 C6：停用 / MFA 重置的确认强度

| 方案 | 做法 | 优点 | 缺点 / 成本 | 风险 |
|---|---|---|---|---|
| **A（推荐）** 理由 ≥10 字前端先拦（不足不发请求）+ 对话框里一次「确认」；**不要求逐字输入账号名** | 与后端判据一致；停用**可逆**（启用即回），仪式强度低于删除角色（逐字 code）一档，与操作的不可逆程度相称 | 手滑停用一名教师的代价 = 对方当前会话失效、需重新登录 | 低 |
| B 加「逐字输入姓名」二次确认 | 更防手滑 | 为可逆操作加不可逆级别的仪式，运营会嫌麻烦；且 `display_name` 可为 `null` → 「逐字输入 null」无法定义 | 低，但成本不值 |
| C 一键直发（只有按钮，没有理由栏） | 最快 | 理由不足会真的发出去并拿 422，把服务端校验当前端 UX；停用是「立刻把人踢下线」的动作，不该单点 | 中 |

### 20.3 选择结论（**待用户拍板后回填；未回填不开工**）

推荐：C1 = A、C2 = A、C3 = A、C4 = A（既定口径）、C5 = A、C6 = A。
用户拍板后，本节按 §19.3 的格式逐条写理由与 TD 处置，并回填 §20.4 的口径修正。

### 20.4 实现顺序（TDD 红 → 绿，拍板后执行）

**第 0 步（契约对齐）**：`frontend/packages/api-client/src/rbac.ts`（同域、同一工厂，不新开模块）

1. 类型：`PermissionAccountView`（8 字段，逐一对齐 `PermissionAccountResponse:298`）、
   `BindableRoleView`（4 字段，**不含 `permission_codes`**）、`BoundRoleView`（含 `bindable`）、
   `UserBindingsView`（`roles[]` + `permission_codes[]` + `roles_version`）、
   `BindingUpdateInput`（`role_ids`/`version`/`reason?`）、`BindingUpdateResult`
   （快照 + 4 个差异数组）。分页信封复用 `org.ts:9` 的 `Page<T>`（`{items, next_cursor}` 同形）。
2. 方法：`listPermissionAccounts(query)`、`listBindableRoles()`、`getUserBindings(userId)`、
   `replaceUserBindings(userId, input)`、`deactivateUser(userId, input)`、`activateUser(userId)`；
   后三条中 `deactivateUser` / `activateUser` 与已有的 `resetUserMfa` 都返回 `Promise<void>`
   （204 已由 `request<T>` 在 `auth.ts:404` 统一处理）。**`activateUser` 不发 body**（事实 8）。
3. `rbac.test.ts` 补形状用例：路径 / 方法 / 请求体（尤其 `replaceUserBindings` 必带 `version`、
   `deactivateUser` 必带 `reason`、**`activateUser` 不带 body、不发 `Content-Type`**）。

**第 1 步（纯逻辑）**：新建 `frontend/apps/admin/src/account-logic.ts` + `account-logic.test.ts`

- `ACCOUNT_STATUS_LABELS`（三档，未知档回落显示原码并给一句「未知状态」——不静默当启用）
  与 `USER_TYPE_LABELS`（两档）。
- `deactivateReasonError(reason)`：**去空白后 ≥10 字**，停用与 MFA 重置**共用这一个函数**
  （两条判据相同；绑定理由**不走**它——事实 5）。
- `bindingDiff(current, next)` → `{ added, removed, emptiesAll }`，用于「收回全部权限」提示
  （后端没有绑定预览端点，差异只能在界面算）。
- `accountRowGate(row, selfUserId, permissions)` → `{ canBind, canDeactivate, canActivate, canResetMfa, reason }`；
  把「超管 / 是我 / 缺码」三挡集中在一个纯函数里，页面只消费 `reason` 文案（C4）。
- `filterAccountsLocally(items, keyword)`（C1 ①：姓名 / 角色码，大小写不敏感，只作用当前页）。

**第 2 步（页面）**：新建 `frontend/apps/admin/src/views/PermissionAccountsView.vue`

1. 列表 + 游标栈翻页（照 `PeopleView.vue:60-74`、`:101-107`），`limit=50`；
   「下一页」是否可点**只看 `next_cursor`**（事实 9）。
2. 顶部过滤区：本地过滤框 + （持 MFA 码时）按登录名精确定位框；定位命中 → 用 `id` 直接开抽屉。
3. 抽屉：`GET /users/{id}/roles` → 以已持有角色为基底渲染（C3-A）→ 变更差异提示 → `PUT`
   （带 `version` + 可选 reason）→ 成功后用响应的 `roles_version`（已 +1）与 4 个差异数组
   提示「已授予 X / 已收回 Y」；409 → 采用 `latest` 重载 + 一句「该账号的绑定刚被他人改过，已刷新，请重新确认」。
4. 启停：停用对话框（理由 ≥10 先拦）/ 启用直发；成功后**重读当前页**，不手改本地 `status`。
5. MFA 重置区（C5-A）：理由 ≥10 → `POST .../mfa/reset`；成功提示「该账号的登录会话已失效，需重新登录」，
   **不带条数**（事实 7）。
6. `router/index.ts` 加 `settings/accounts`（`meta.permission = "school.permission_account.manage"`）；
   `AdminLayout.vue:19-26` 的 MENU 加一项「权限账号」。

**第 3 步（mock + E2E）**：`frontend/e2e/helpers/rbac-mock.ts` 扩 6 条路由，**有状态**
（`PUT` 改内部绑定并 `version+1`、停用改 `status=disabled`、启用回 `active`、MFA 重置清 `mfa_enabled`、
列表按 mock 状态现算 `role_codes`/`is_super_admin`）；门禁顺序与判据**逐条注释指向后端行号**
（`binding.py:472`、`:501`、`:515`、`:541`、`school_rbac.py:917` 等）——TD-253 的口径。
新建 `frontend/e2e/admin-permission-accounts.spec.ts`。

**第 4 步（收尾）**：前端门禁（`pnpm lint` / `pnpm format:check` / `pnpm typecheck` /
`pnpm test:unit` / **`pnpm test:e2e`**，不手动 `npx playwright test`——TD-254）→ 独立审查 →
commit → push → PR → 远端 CI 全绿 → 用户确认后 rebase 合并 → main CI 全绿 → 删分支 → 文档回填 PR →
§9 八段汇报。

### 20.5 测试策略与门禁

- **单元（`account-logic.test.ts`）**：`deactivateReasonError` 的每个拒绝分支（空串 / 全空白 / 9 字 /
  10 字 / 含中文空白）；`bindingDiff` 的 `emptiesAll`；`accountRowGate` 的超管 / 是自己 / 缺 MFA 码 /
  正常四种组合；`filterAccountsLocally` 的大小写与角色码命中；未知状态档回落。
- **单元（`packages/api-client/src/rbac.test.ts`）**：6 个新方法的路径 / 方法 / 请求体形状；
  204 端点返回 `undefined` 而不是解析空体。
- **mock E2E（`admin-permission-accounts.spec.ts`）**：列表渲染（三档状态、超管标签）/
  翻页（断言第二页带的 `cursor` 来自第一页 `next_cursor`）/ 本地过滤命中与不命中 /
  精确定位框在无 `school.personnel.mfa.reset` 时**不出现** / 打开抽屉发 `GET` /
  改绑定（断言捕获的 `PUT` 体带 `role_ids` + `version`）/ 撤空时给「收回全部权限」提示 /
  409 后采用 `latest` 不静默覆盖 / **已持有但不可授的角色不被提交集合漏掉**（C3-A 的回归钉子）/
  停用理由 9 字**不发请求**、≥10 字才发 / 停用成功后列表显示「停用」/ 启用**不发 body** /
  超管与自己两行**没有任何写按钮** + 一句说明 / MFA 重置成功提示不含设备条数。
- **门禁**：前端 `lint` / `format` / `typecheck` / 单测 / mock E2E；**后端零改动**，
  本点不跑 `ruff`/`mypy`/`bandit`/`alembic check`（`app/**` 与 `alembic/**` 都不碰）。
- **无全栈 E2E**：本批不做（TD-183/192 仍在 backlog；本地 8000 端口被占）。

### 20.6 预判技术债

- **TD-255（新增）**：账号搜索能力缺口——本地过滤只覆盖当前页，「精确定位」框的可见性挂在
  `school.personnel.mfa.reset` 上（语义别扭，因为它本质是 MFA 权限而不是查询权限）。
  收口 = 后端给 `GET /permission-accounts` 加 `q=`/`status=`，届时撤掉该框；与 TD-237（列表不按人员范围裁剪）同批评估。
- **TD-256（新增）**：停用 / 启用 / MFA 重置都回 204，`sessions_revoked` 只在审计里
  → 界面无法回答「踢了几台设备」，运营只能报「让他重新登录」。与 TD-235 同族。
- **TD-257（新增）**：MFA 重置入口两处并存（角色页「安全工具」卡片 + 账号页重置区），
  判据文案靠同一个 `deactivateReasonError` 纯函数保持同源；是否搬并等账号页稳定后再判
  （§18.6 已定本期不搬）。
- **TD-249 / TD-250（既有，本点续账）**：本点新增复刻的门禁码
  （`school.permission_account.manage`、`school.personnel.mfa.reset`）与状态 / 类型标签映射
  （`active`/`locked`/`disabled`、`teacher`/`school_admin`）归到这两条的收口里，后端下发目录时同批改。
- **TD-253（既有）**：本点新加的 6 条 mock 路由同样是手抄后端判据，后端改动不会让 mock 变红；
  收口仍靠 TD-183/192 全栈 E2E。
- **TD-242 / TD-245（既有）**：C3-A 的意义就是不在 `bindable` 判据收窄时静默撤权；
  本点交付后 TD-242 可标 `已完成`。
- **实现时的自检信号**：如果发现自己在写「把 `listBindableRoles()` 直接 map 成 checkbox 列表」，
  那就是滑回了 C3 的 B 方案——停手，改成以 `getUserBindings()` 的 `roles[]` 为基底。

### 20.7 高风险确认

本点动的是**权限面 + 他人账号可用性**（改绑他人角色、把他人账号下线、重置他人 MFA），
按 AGENTS.md §11.7 属「认证 / 权限 / 状态机」类，**简报经用户拍板后才编码**。
需要用户拍板的是 **C1（怎么找账号）、C2（详情形态）、C5（MFA 入口放哪）、C6（确认强度）** 四张卡。
C3（绑定数据源以当前绑定为基底）由事实 4 的静默撤权风险唯一确定，C4（超管 / 自锁门禁）
由 §18.6 TD-242 与 187g-B 决策 B4 已拍板口径唯一确定——两条**按 A 落地，不再单独问**。
