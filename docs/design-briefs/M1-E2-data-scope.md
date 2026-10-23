# M1-E2 — RBAC 数据范围、审批职责分离与平台跨租只读 实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-23 |
| 状态 | 已确认（2026-10-23，①~⑩ 逐项点头） |
| 关联功能点 | RBAC-02 数据范围（全校/班级/课程/个人）、RBAC-03 审批职责分离；TD-072 / TD-138 / TD-149 |
| 验收引用 | `docs/spec.md` §3.4 验收 5 条；`docs/milestone.md` M1 退出标准「RBAC+数据范围生效」；`docs/design.md` §8.1/§8.2、§5-2（矩阵）、§9 |

## 0. 已定口径（用户 2026-10-23 逐项确认，实现不得偏离）

| # | 决策 | 结论 |
|---|---|---|
| ① | `class`/`course` 范围 | **本期不做**，只落 `school`/`self` 的服务端过滤；班级/任课范围随 M2 组织与任课关系 |
| ② | 平台跨租通道（TD-072） | **本期做最小通道**：只读 + 必填理由 + 审计，不做预审批工单 |
| ③ | 提交人不能自批 | **应用层 + 数据库约束**，不加 DB 触发器 |
| ④ | 过滤写在哪 | **仓储基类统一注入**，另加裸 SQL 绕过守卫 |
| ⑤ | SQL 条数上限 | 单次带鉴权请求 **保持 ≤ 8**（现实测 5，本期预计 6） |
| ⑥ | RBAC-02 真实落点 | 新增租户侧最小只读接口 `GET /api/v1/school/personnel`（+ 详情），权限码 `school.personnel.view` |
| ⑦ | RBAC-03 真实落点 | 平台审核学校注册：申请加提交人列、审核记录加同名冗余列 + 同表 CHECK |
| ⑧ | 超管自批例外开关 | **新建 `system_settings` 表**（key/value + 修改理由 + 审计），平台运营接口可改、每请求读库热生效 |
| ⑨ | 出厂数据范围默认 | `school_super_admin`/`school_admin` = `school`；`teacher_b_end`/`student`/`teacher` = `self`；平台角色不写范围；**解析不到范围行 = 空集**（fail-closed，不回退全校） |
| ⑩ | 越权响应口径 | 列表静默过滤到范围内；显式 id 越权 → **404**（与「不存在」同形，不暴露存在性）+ 安全审计 `data_scope.denied` |

## 1. 目标

把「权限允许 ≠ 数据可看」这条边界真正落到服务端：解析用户的数据范围并强制注入查询；把「提交人不能自批」从文档口径变成库里可执行的约束；给平台一条受控的跨租只读通道（TD-072）；顺手把 E1 遗留的 realm 触发器口径收紧到与代码同一白名单（TD-149）。

不在本期：班级/课程范围与任课关系、M3/M4 的请假与 KB 审批流本身、跨租二次授权工单、运营后台界面（例外开关与角色授权界面留 M2）、字段级掩码完整查看审计闭环（`school.personnel.sensitive.read` 本期只保证「不给就不返回」，完整查看通道随 M2 人员模块）。

## 2. 候选方案

### 2-A 数据范围：解析与注入位置

**方案 A（选中，④）**：`Principal` 每请求解析 `scopes`；`TenantScopedRepository` 构造时必须带范围上下文，`scoped()`/`base_query()`/`get()` 统一追加范围谓词。

- 做法：`data_scopes` 按角色∪用户取并集 → 归一为 `ScopeSet`（`school` 优先，否则 `self` 的 `owner_id` 集合）；仓储新增 `DataScope` 值对象，`get/list_all` 走同一 `scoped()`；`self` 需要业务表有归属列，本期由「范围声明 + 谓词生成」承担，无归属列的表在构造时报错而不是静默放行。
- 优点：只有一处能漏；新接口默认受保护；与 E1「权限每请求从库读」同口径。
- 缺点/成本：要给现有仓储调用点补上下文（本期调用点少，auth 侧自有资源路径显式声明 `self`）；无归属列表需要显式声明。
- 风险：裸 SQL / `session.execute(text(...))` 绕过基类 → 用 2-E 守卫压住。
- 对 spec/design 的影响：`docs/design.md` §8.1-3 补「第三道防线：数据范围在仓储基类注入」。

**方案 B**：每个 service 手写 `where`。
- 优点：改动最小、无基类膨胀。缺点：漏一处即越权，且不可证伪。风险：与 §3.4「所有鉴权在服务端」比，属于可回归的散点。→ 不选。

**方案 C**：把范围塞进 RLS 会话变量（`app.scope_ids`）。
- 优点：数据库层强。缺点/成本：RLS 只懂租户不懂 owner 列语义，需要为每张表写策略、且 `self` 与 `school` 并集在 SQL 策略里难表达；本期只有 `school`/`self`，收益不抵复杂度。→ 不选（RLS 仍是第二道防线，不动）。

### 2-B 范围默认值与「无范围行」的语义

**方案 A（选中）**：出厂默认入库 + 缺省拒绝。`school_super_admin`/`school_admin` → `school`；`teacher_b_end`/`student`/`teacher` → `self`；平台角色不写 `data_scopes`（realm 不同表）。**用户解析不到任何范围行 ⇒ 空集**（列表返空、详情 404），不回退成全校。
- 风险：E1 已开通的租户 `data_scopes` 为空（TD-138），迁移必须为存量租户补齐内置角色默认范围，否则升级即「管理员看不到人员列表」。
- 与 E1 同步策略一致：**只补缺失、不覆盖人工改动**；默认表新增 `rbac_builtin_role_scope_defaults(realm, role_code, scope_type)`，`seed_builtin_roles()` 一并写 `data_scopes`，保持「只有一份真相」。

**方案 B**：无范围行 = 全校。→ 不选：默认放行违反 §3.4「跨租/越范围默认拒绝」。

### 2-C RBAC-02 的真实取证接口

**方案 A（选中，⑥）**：`GET /api/v1/school/personnel`（分页列表）+ `GET /api/v1/school/personnel/{user_id}`（详情），门禁 `school.personnel.view` + realm `tenant` + 数据范围。响应只回 `id/user_type/display_name/status/last_login_at` 与掩码邮箱（`mask_email`），`self` 范围只能看到自己一行；范围外 id 返回 **404 且写安全审计**（不暴露存在性）。
- 优点：`school.personnel.view` 目录描述就是「查看人员档案（按数据范围）」，语义天然匹配；`users` 是本期唯一有真实数据行的租户业务表；M2 人员模块直接复用同一接口与同一范围谓词。
- 缺点/成本：本期没有前端页面调用它（M2 补界面），需要 2 个只读接口 + 契约测试。
- 风险：响应泄露敏感字段 → 契约固定只回掩码邮箱、绝不回 `login_id`/`email` 明文/密码哈希/MFA 字段，用断言钉住。

**方案 B**：只做机制，接口留 M2。→ 用户已否：M1 退出标准要求「数据范围生效」，没有真实调用点无法验收，复审会按 Major。

### 2-D RBAC-03「提交人不能自批」

**方案 A（选中，⑦）**：把唯一真实审批面（平台审核学校注册）做实。
- 数据：`tenant_registration_requests.submitted_by_platform_user_id`（可空；匿名自助提交为空）、`school_review_records.submitted_by_platform_user_id`（审核时冗余写入）+ 同表 `CHECK (submitted_by_platform_user_id IS NULL OR reviewer_id <> submitted_by_platform_user_id OR self_exception_used IS TRUE)`。**第三条款是 2026-10-23 用户确认的偏离**（原钉的是两条款版）：例外路径本身要求「同人 + 标记为真」，两条款 CHECK 会让例外功能不可用。
- 服务：`evaluate_self_approval(submitter_id, reviewer_id, exception_enabled, reason)`（`backend/app/approval/sod.py`），例外默认关闭；例外开启时要求理由，写 `approval.self_exception.used` 审计并通知平台审计（本期通知=审计事件 + 平台审计可见记录，站内信随 M4 消息模块）。
- 为什么冗余列：跨表 CHECK 无法表达，触发器已被用户否掉（③）；冗余列 + 同表 CHECK 是唯一「不加触发器却有数据库硬约束」的写法。
- 风险：冗余列与源列不一致 → 审核写入在同一个 `SECURITY DEFINER` 函数里同时写两列，并有测试断言两者相等。
- 已知残留（用户确认接受）：CHECK 有第三条款后，「绕过函数直写同人 + 标记为真」这一形态数据库分不出真假——正常例外的判定发生在应用层与函数层，两者都会落 `approval.self_exception.used` 审计，直写伪造则没有。因此把「例外记录必须能对上审计」的反查登记为 **TD-161**（检测路径，排期 M2 审计巡检 / 随 TD-153 工单化一并收口）。数据库 CHECK 仍然硬挡「没走例外」的直写。口径写进矩阵 §5-4。
- 对 spec/design 的影响：`docs/design.md` §9 补一句例外路径与数据库约束的先后关系。

**方案 B**：只写可复用服务，落点等 M3/M4。→ 不选：本期规则无真实落点，且注册审核这条 spec 明确写了（「SaaS 审核员不得审核自己提交的学校」）。

### 2-E 裸 SQL 绕过守卫

**方案 A（选中）**：单元守卫扫 `app/**` 源码，任何直接 `select(`/`text(` 命中租户业务表且不在显式豁免清单（仓储基座本身 + `auth` 自有资源路径 + 迁移目录）即失败；同时仓储要求显式范围上下文，`NoScope(reason)` 这类绕过必须带理由并进审计。
- 优点：机器可证伪，复审可跑同一条命令复现。
- 风险：豁免清单被滥用 → 清单条目必须带注释理由，且条目数量有断言上限。

**方案 B**：只靠 code review 口头检查。→ 不选：违反 §10「更好做法必须可执行」。

### 2-F 平台跨租只读最小通道（TD-072）

**方案 A（选中，②）**：`POST /api/v1/platform/cross-tenant/personnel/read`，body `{tenant_id, reason, limit}`；门禁 `platform.cross_tenant.read` + realm `platform`；执行时**改用租户连接**并 `set_config('app.tenant_id', 目标租户, true)` + `SET TRANSACTION READ ONLY`，只 SELECT 人员掩码视图；请求/结果写审计（含 reason、目标租户、返回条数）。
- 为什么走租户连接：平台角色在库层没有业务表权限（design §8.2），换用租户上下文后 RLS 天然把可见性限制在**这一个**租户，且 READ ONLY 事务保证写不了；不需要新建跨租 DB 角色，也不引入「平台连接能读全库」的口子。
- 优点：只读、最小、有理由、有审计，满足 spec §3.4「跨租默认拒绝 + 记录安全事件」的例外通道。
- 缺点/成本：`reason` 走 body 不进 URL，日志侧要做截断脱敏。
- 风险：变成任意租户漫游 → 单请求只允许一个 `tenant_id`、只允许白名单资源（本期只有 personnel）、条数上限、审计 + `platform.audit.view` 可复核。

**方案 B**：预审批工单/二次授权表 + 有效期。→ 成本更高，且当前没有任何跨租读的真实需求方；登记 TD 推到 M6/首个真实需求。
**方案 C**：推到 M6。→ 不选：TD-072 的目标里程碑已定 M1-E2，design §8.2 也标了「M1-E」。

### 2-G 例外开关存储

**方案 A（选中，⑧）**：新表 `system_settings(key PK, value, updated_by, reason, updated_at)`（平台级，无 tenant_id），内置键 `approval.self_exception_enabled`；读=每请求从库取（与权限同口径，热生效、不踢会话）；写=`PUT /api/v1/platform/settings/{key}`，门禁 `platform.config.edit` + 必填理由 + 审计 + 值域校验（只允许已知键、布尔值）。
- 优点：符合既有偏好「运营可调的配置进库、前台可改、热更新」；后续 M2 运营后台直接复用同一张表。
- 缺点/成本：一张表 + 一个写接口 + 键白名单。
- 风险：键名膨胀无人管 → 键必须在代码目录里声明（白名单 + 类型 + 默认值），库里出现未知键启动自检失败。

**方案 B**：env `APPROVAL_SELF_EXCEPTION_ENABLED=false`。→ 不选：运营改不了、改动需重启，与上条偏好冲突。

### 2-H SQL 条数

**方案 A（选中，⑤）**：范围解析并入既有鉴权查询链，允许 +1 条（`select scope_type, scope_id from data_scopes ...`）；单次带鉴权请求上限 **≤ 8**，基线测试按新实测值更新并保留「权限判定本身加 0 条」的断言。
**方案 B**：与权限码合并成一条 `json_agg` 查询。→ 成本：SQL 复杂、可读性差、失败面大；上限 8 够用，不选。

## 3. 选择结论

- 选择：2-A/A、2-B/A、2-C/A、2-D/A、2-E/A、2-F/A、2-G/A、2-H/A（即 §0 的 ①~⑧）。
- 理由：三条能力（范围过滤、职责分离、跨租只读）各自都必须在 M1 有一个**真实调用点 + 可执行断言**，否则只是库表；同时不引入触发器、不引入跨租 DB 角色、不把范围语义下推到 RLS。
- 为什么不选其他：B/C 方案的共同问题是「能力没有落点」或「把安全边界交给不可证伪的口头检查」。
- 需要改文档：`docs/rbac-matrix.md`（§2 数据范围交付口径、§4 增行 `platform.cross_tenant.read`/`school.personnel.view` 的接口列、§5-4 例外与约束先后）、`docs/design.md`（§8.1 三道防线、§8.2 最小通道、§9 例外）、`docs/tech-debt.md`（TD-072/138/149 收口 + 新 TD）。

## 4. 实现顺序与 TDD 测试清单

1. 数据/契约：迁移 `m1e2_data_scope_sod.py`（新 head）——`data_scopes` 默认范围表 + 存量租户补齐 + `seed_builtin_roles()` 扩写范围；`system_settings` 表 + 内置键；`tenant_registration_requests`/`school_review_records` 提交人列 + CHECK + 审核函数改造；`role_permissions_realm_guard` 触发器替换为 `module NOT IN ('school','campus')`（TD-149，与范围迁移合并做一次）。
2. 服务：`ScopeSet` 解析与谓词生成；`TenantScopedRepository` 范围注入 + 显式豁免；`evaluate_self_approval` + 例外读取；跨租只读通道服务。
3. 接口：`GET /api/v1/school/personnel{,/{id}}`、`POST /api/v1/platform/cross-tenant/personnel/read`、`PUT /api/v1/platform/settings/{key}`；全部接入 `require_permission` 声明与路由清单测试。
4. 界面：本期不做（M2 运营后台与人员页复用）。
5. 红→绿→重构测试（2026-10-23 实现后回填；迁移实际编号 `f7a9b1c3d5e8_m1e2_data_scope.py`）：
   - [x] 单元：`ScopeSet` 并集/school 优先/空集 fail-closed（`tests/test_data_scope_unit.py`）；仓储缺范围上下文/缺归属列构造失败、豁免必须带理由（同上 + `tests/db/test_repository.py`）；裸 SQL 绕过守卫（`tests/test_bare_sql_scope_guard_unit.py`）；`system_settings` 键白名单与值域（`tests/test_system_settings_unit.py`）；`evaluate_self_approval` 四态（`tests/test_approval_sod_unit.py`）。
   - [x] 集成：存量租户迁移后管理员可见全校、`self` 用户列表只剩自己、越权 id 404 且有 `data_scope.denied` 审计、"真不存在"不留 denial（`tests/integration/test_data_scope_enforcement.py`，含跨租会话在数据库层只读、非平台主体拒绝、审计写不进即拒绝、响应形状不含 `login_id`）；数据库 CHECK 真实挡下「审核人=提交人」直写 + 带例外标记必须放行（`tests/integration/test_approval_separation.py::test_database_check_blocks_direct_self_approval_write`）；函数层第二道锁独立成立且错误映射 403/422（同文件 `test_database_function_gate_blocks_self_approval_without_app_layer`）；例外开启后应用层放行且审计带理由（同文件，含"改完不重启下一次请求即生效"）；触发器对 `platform.*`/`observability.*`/`eval.*` 入租户角色三向拒绝 + `school.*` 放行（`tests/integration/test_auth_login.py::test_db_guard_rejects_platform_domain_permission_for_tenant_role`）；`data_scopes` 同步只补缺失不覆盖人工改动（`tests/integration/test_data_scope_backfill.py`）；设置接口鉴权与审计（`tests/integration/test_platform_settings_api.py`）；迁移往返（独立临时库：升级→开通种出范围→降级逐件退回 E1→再升级可重放，`tests/integration/test_migration_data_scope_rollback.py`）。
   - [x] 业务场景：`tests/business/test_bs_m1e2_scope_and_sod.py` — 自批 403 → 开例外 → 审核通过并租户上线（两条审计齐）；平台无码 403 / 有码无理由 422 / 有理由 200 且只回该校+脱敏+审计带理由与行数；`self` 范围教师列表只剩自己。
   - [x] E2E：本期无前端改动，沿用既有 28 条；新增接口的契约由后端测试覆盖（TD-152：M2 人员页补 Playwright）。
   - [x] 性能：SQL 条数基线更新（鉴权请求实测 6，上限 ≤8）。**人员列表分页 P95 本期未压测** → TD-159（M6 随 NFR-01 k6 加入）。

## 5. 测试策略

| 层级 | 内容 |
|---|---|
| 单元 | 范围归一与谓词生成、仓储上下文强制、例外判定四态、设置表白名单、裸 SQL 守卫、矩阵一致性 |
| 集成 | 真实 PG：迁移补范围、越权 404、CHECK 硬挡、触发器收紧、同步不覆盖人工改动、RLS 与范围叠加 |
| 业务场景 | 三条 spec §3.4 验收在真实后端全栈跑通（school/self、自批拒绝与例外、跨租只读默认拒绝） |
| E2E | 本期不改前端（无界面入口），登记 TD 到 M2 |
| AI 评估 | 不涉及（无对话链路改动） |
| 性能 | SQL 条数基线 + 人员列表分页 |

## 6. 预判技术债（编码前先登记，编号从 TD-151 起）

| TD 编号 | 条目 | 处置（拟） |
|---|---|---|
| TD-151 | 范围过滤只在仓储基类生效；`class`/`course` 与任课关系、`self` 的归属列语义在 M2/M3 才完整 | 已采纳 → M2/M3 |
| TD-152 | `/school/personnel` 无前端入口，M1 只能后端取证 | 已采纳 → M2 人员页 + Playwright |
| TD-153 | 跨租只读通道无预审批工单/有效期（二次授权弱化版），靠权限码+理由+审计 | 已延后 → M6 或首个真实跨租需求，理由：本期无需求方 |
| TD-154 | 「通知平台审计」本期=审计事件，站内信/告警随 M4 消息模块 | 已采纳 → M4 |
| TD-155 | `system_settings` 无运营界面与键版本化（改键靠发版） | 已采纳 → M2 运营后台 |
| TD-156 | 例外路径先应用层放行再落库，数据库 CHECK 只挡未走例外的直写（两者非同一道锁） | 已完成（本期附测试与矩阵口径）→ 复审确认 |

实现中新登记（编码阶段才发现的取舍，同一口径要求三态处置）：

| TD 编号 | 条目 | 处置 |
|---|---|---|
| TD-157 | `submitted_by_platform_user_id` 本期无真实写入方（平台代提交入口在 M2），三道锁的用例靠测试直插该列 | 已采纳 → M2 |
| TD-158 | 越权审计两处口径不一致（租户侧 best-effort vs 跨租 fail-closed）+ 详情越权需第二次探针查询 | 已延后 → M2 或首个真实安全告警需求 |
| TD-159 | 简报承诺的人员列表分页 P95 未压测（本期只有 SQL 条数基线） | 已采纳 → M6（NFR-01 k6） |
| TD-160 | 未知设置键的启动自检点名未实现（应用层读不到/写不进，但进程不告警） | 已采纳 → M2（随 TD-155 运营后台） |
| TD-161 | CHECK 第三条款让「直写同人 + 例外标记」与合法例外同形，数据库层分不出真假 | 已采纳 → M2（反查审计的检测，与 TD-153 同批巡检） |

## 7. 高风险确认

- 属于高风险功能：是（权限、租户隔离/跨租、职责分离、数据库约束、迁移改写既有函数与触发器）。
- 需要用户确认并已确认的点：①~⑧（见 §0）。
- 本轮追加确认的两点默认值（已并入 §0 ⑨⑩）：
  1. **出厂数据范围默认**：`school_super_admin`/`school_admin` = `school`；`teacher_b_end`/`student`/`teacher` = `self`；平台角色不写范围；解析不到范围行 = 空集。
  2. **越权响应口径**：列表静默过滤到范围内；显式 id 越权 → **404**（不暴露存在性）并写安全审计 `data_scope.denied`。
- 确认结论：2026-10-23 用户逐项点头，简报转「已确认」，可进入编码。

## 8. 审查对照

- 审查时重点验证：范围解析是否 fail-closed；仓储是否存在未声明上下文的构造点；裸 SQL 守卫是否真能复现一次越权；CHECK 是否被测试真实触发（不是只测应用层）；触发器收紧后 `observability.*`/`eval.*` 是否两面取证；跨租通道是否确实 READ ONLY 且只命中一个租户；例外开启是否带理由+审计；SQL 条数是否 ≤8；矩阵/design 是否同步；TD 是否有处置。
- 允许的偏差：接口命名与分页参数细节、审计 action 字符串（需与矩阵 §5 一致）、`system_settings` 列顺序。
- 不允许的偏差：改成 DB 触发器实现自批拦截、把范围过滤下推到 RLS、把 `class`/`course` 半做进来、跨租通道放宽写权限。

## 9. 交付结果与实现期偏差（2026-10-23 回填）

### 9.1 实现结果

- 数据层：迁移 `f7a9b1c3d5e8` 建 `rbac_builtin_role_scope_defaults`（出厂范围默认）与 `system_settings`（平台策略开关）；扩写 `seed_builtin_roles(uuid)` 为新租户写 `data_scopes`；`review_registration_request()` 承担 RBAC-03（复制提交人 + 例外判定）；`role_permissions_realm_guard` 收紧到 `catalog.TENANT_MODULES` 白名单。
- 服务层：`Principal` 每请求解析范围（角色行 ∪ 用户行，解析不到 = 空集）；`TenantScopedRepository` 统一注入 `tenant_id` + 范围谓词，`ScopeBypass(reason=…)` 是唯一绕过口，缺归属列在构造期报错；越权详情 404 + `data_scope.denied` 审计（best-effort）。
- 接口层：`GET /api/v1/school/personnel`(+`/{id}`，`school.personnel.view`，响应固定 6 字段)；`POST /api/v1/platform/cross-tenant/personnel/read`（只读事务 + 单租户 + 理由 + 审计 fail-closed）；`GET/PUT /api/v1/platform/settings`（`platform.config.view/edit`，改值必填理由 + 审计）。
- 职责分离：应用层 `_guard_self_approval()`、数据库函数 `SELF_APPROVAL_*`、同表三条款 CHECK 三道锁；例外开关 `approval.self_exception_enabled` 每请求读库热生效，例外使用与拒绝各写审计。
- 测试：E2 新增单元/集成/业务场景用例；全量 561 passed；裸 SQL 守卫清单与 SQL 条数基线（实测 6 / 上限 8）。

### 9.2 实现期偏差与取舍（均已登记 TD）

- CHECK 由简报初稿的两条款改为三条款（`OR self_exception_used IS TRUE`）：2026-10-23 用户确认「接受三条款+补检测」；伪造直写的检测走反查审计（TD-161）。
- 自批拒绝审计在事务回滚之后另开平台会话写入，避免"拒绝记录随回滚丢失"。
- 同一"越权"两处审计口径刻意不同：租户侧 best-effort（审计故障仍 404）、跨租 fail-closed（审计失败即请求失败）（TD-158）。
- `rbac_builtin_role_scope_defaults` 不加指向角色定义表的外键：应用角色仅 `SELECT`、写入只来自 SECURITY DEFINER 的 `seed_builtin_roles`，低价值不改，留 M2 配置界面复评。
- 人员列表 P95 未压测（TD-159）；前端人员页延后 M2（TD-152）。

### 9.3 门禁与审查

- 门禁：后端 **561 passed**、`ruff check app tests` 全绿、`mypy --no-incremental app tests`（149 文件）无问题、`alembic check` 无漂移且 head=`f7a9b1c3d5e8`、`bandit -r app -x tests` / `bandit -r alembic -s B608` 退出码 0。
- 审查轮次（详见 `docs/review-log.md` M1-E2 小节）：第一轮独立审查 0 Blocker + 3 Major + 4 Minor（响应泄漏 `login_id`、三条款 CHECK 未拍板、docstring 虚承诺等），全部修复；第二轮因当日独立审查子智能体配额耗尽改在主上下文执行（非独立上下文），结论 0 Blocker / 0 Major。
