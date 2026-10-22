# M1-E1 设计简报 — 权限矩阵落库与服务端鉴权内核（RBAC-01）

| 项 | 内容 |
|---|---|
| 功能点 | M1-E1（M1-E 的第一步；E2 为数据范围服务端过滤 + 审批职责分离 + 平台跨租只读通道） |
| 需求依据 | `docs/spec.md:55`（RBAC-01 角色与权限矩阵）、`docs/spec.md:182-194`（§3.4 权限与数据范围，含 5 条验收）、`docs/milestone.md:97`（M1-E 范围）、`docs/milestone.md:111`（M1 退出标准） |
| 矩阵依据 | `docs/rbac-matrix.md`（v1.2，§1 角色 / §2 数据范围 / §3 权限点 / §4 矩阵 / §5 约束 / §6 定稿） |
| 已确认口径（2026-10-22 用户选择） | ① M1-E 拆两步；② ~~超管角色码改名~~ **核对后为既成事实**：M1-B 的 `provision_tenant()` 已建 `school_super_admin`/`school_admin`/`teacher_b_end` 三角色，无需改名迁移（`alembic/versions/e5c9d1f7a2b4:47-50`）；③ 矩阵权限码全量落库；④ 数据范围本期只做 `school` + `self`；⑤ **知识库上传对所有持后台账号的角色默认开放**，但"扩大全校可见人群"需管理员再确认，且上传者永不自批；⑥ **权限配置分两层**：权限项目录跟代码走，角色↔权限映射与数据范围存数据库、可前台增删改、热更新（界面 M2 交付）；⑦ **命名归一**：矩阵 `campus.chat` → `campus.chat.use`，删除与 `school.audit.view`/`platform.audit.export` 重复的 `audit.view`/`audit.export`（矩阵升 v1.1）；⑧ **删除无引用的旧码**（原记 7 个，**核对后为 9 个**：`school.profile.view/edit`、`school.user.view/create/edit/delete`、`school.role.assign`、`school.leave.approve`、`campus.leave.submit`），矩阵为唯一口径；⑨ **本步一起建 C 端内置角色** `student`/`teacher`，否则 `/campus/chat/turns` 挂权限后学生直接 403 |
| 风险等级 | **高**（鉴权、权限分配、租户隔离）→ 本简报确认后才编码 |

---

## 1. 目标与验收

**目标**：把 `docs/rbac-matrix.md` 的角色与权限点变成库里真实的数据，并让每一个非公开接口都必须声明它需要的权限码；缺权限一律 403 且写 `auth.permission.denied` 审计。本步**不实现**数据范围过滤、审批分权、跨租通道（→ E2）。

验收对齐 spec §3.4：

1. 平台/租户内置角色与权限映射可在库中查到，且与矩阵一致（可由代码生成，不允许手写漂移）。
2. 未授予权限码的主体调用受保护接口 → 403 + 审计；realm 不匹配（拿平台码走租户路由或反之）→ 403，不因"码碰巧存在"放行。
3. 跨租访问仍由 RLS + 显式过滤兜底（M1-A 已交付，本步补角色维度的回归）。
4. 权限/角色变更后**下一次请求即生效**（现状：每请求查库，本步保持并加断言）。
5. 平台连接读不到租户业务表（现状保持，回归用例覆盖新种子）。

不在本步验收范围（明确推到 E2/M2）：班级/课程范围过滤、"提交人不能自批"、超管例外开关、`platform.cross_tenant.read` 二次授权通道、前端菜单按权限渲染。

## 2. 现状（已核对）

- 表结构齐备：`permissions`（全局目录，无 tenant_id）、`roles`/`role_permissions`/`user_roles`/`data_scopes`（带 tenant_id + 复合外键）、`platform_roles`/`platform_role_permissions`/`platform_user_roles` — `backend/app/db/models/rbac.py:28-182`，全部由 M1-A 迁移 `779f731db342` 创建。
- 权限目录只种了 **20 个码**：`backend/app/db/models/rbac.py:187-208`（注释写明"实际授权在 M1-E 细化"）；其中 **9 个码与矩阵重复但叫法不同**（口径 ⑧ 要删），矩阵 §3 的 59 个码里有 48 个尚未落库。
- 平台三角色已在 `779f731db342:432-467` 种子并映射（超管=全量 CROSS JOIN）。
- 租户开通时 `provision_tenant()`（最新定义 `alembic/versions/e5c9d1f7a2b4:26-76`）已经建立**三个**内置角色：
  - `school_super_admin`（校方超管）：`WHERE p.module IN ('school','campus')` → 目录扩到全量后自动跟着扩，与矩阵"超管列全 √"一致，**本步不改这条 SQL 的语义**；
  - `school_admin`（授权管理员）、`teacher_b_end`（教师 B 端）：只建角色行，**权限映射为空**（本步要补出厂默认）；
  - 超管账号绑 `school_super_admin`，`users.user_type='school_admin'` 是账号类型，与角色码同名但**不改**。
- **C 端用户（学生/教师）没有任何角色**：`provision_tenant()` 不建、也没有别的开通路径 → 若给 `/campus/chat/turns` 挂 `campus.chat.use`，学生账号必 403（口径 ⑨：本步建 `student`/`teacher` 内置角色并给出厂默认）。
- 鉴权依赖已存在：`require_permission(code, realm=...)` — `backend/app/auth/principal.py:291-317`，拒绝时写审计（`:320-356`）；`Principal` 每请求从库读 `roles`/`permissions`（`:101-187`），令牌内不含权限快照。
- 实际使用只有一处：`backend/app/api/platform_tenants.py:23-24`。其余接口（`auth.py` 14 个、`campus_chat.py` 1 个、`health.py` 2 个）无权限声明。
- 启动自检已校验"租户角色不得引用 `platform.*`"：`backend/app/db/selfcheck.py:161-187`，数据库层由触发器（迁移 `b7d1e3f5a9c2`）强制。
- 测试助手不对称：平台侧 `seed_platform_user(..., permissions=[...])` 可用（`tests/support/auth.py:27`），租户侧 `seed_tenant_user`（`:102-153`）**不建角色映射**。

## 3. 候选方案

### 3.1 权限目录与角色映射的落库载体

| 方案 | 做法 | 优点 | 缺点/成本 | 风险 | 对文档的影响 |
|---|---|---|---|---|---|
| **A（选，已按 ⑥ 收窄）** | Git 里一份**权限项目录** `app/rbac/catalog.py`（码=动作，必须与代码里真实存在的校验点对应）+ 一份**出厂默认** `app/rbac/seeds.py`（内置角色→码）；迁移调用幂等 `sync_rbac_definitions()`，**只补齐缺失项、绝不覆盖库里已有的角色映射**（人工改动是运营配置，不是代码的副本）；配 `scripts/rbac_check.py` 报告"库里有码不在目录中"的孤儿映射 | 权限项不可能与代码脱节（凭空造一个没有实现的权限码会变成假开关）；同时满足"角色权限可前台配置 + 热更新"；老租户自动补齐新权限项 | 出厂默认与运营改动长期分叉，需要"重置为出厂默认"接口（M2）；同步逻辑要区分 insert-if-missing 与 upsert | 同步误覆盖人工改动 → 用"改一次映射、再跑一次同步、断言未变"的回归钉住 |
| B | 纯数据迁移：在 `alembic/versions/xxx.py` 里写死 INSERT 语句 | 最短路径，无新模块 | 每个后续里程碑都要再写一个数据迁移；无法在应用侧校验；与 `permissions` 现有 Python 目录（`rbac.py:187`）形成两套真相 | 迁移与模型目录漂移，且历史迁移不可改（已入库的不会重跑） | 需新增"权限目录只在迁移里维护"的约定，违背现有代码风格 |
| C | 权限与映射由管理后台在库里维护（`permissions` 当配置数据） | 运营可自助调整，不需发版 | MVP 没有校管后台（M2）；无版本化审计；矩阵变成"初始建议" | 高危：权限成为可被写坏的数据，且与 spec 的矩阵验收冲突 | 需改 spec 的"矩阵写入 design.md"口径 |

### 3.2 超管角色码改名 → **已作废（2026-10-22 核对库内实际）**

核对结果：`provision_tenant()` 从 M1-B 起就用 `school_super_admin`/`school_admin`/`teacher_b_end` 三个码（`alembic/versions/c688c44dec81:101-103`、`e5c9d1f7a2b4:47-50`），库里不存在"超管叫 `school_admin`"的存量数据 → **无需改名迁移，本小节三个方案均不执行**。用户口径 ② 的目标状态已经达成；`users.user_type='school_admin'`（账号类型）仍不改。

保留的决策影响：`school_admin`（授权管理员）与 `teacher_b_end` 角色行已存在但**映射为空**，本步的迁移内容改为"删旧码 + 补权限目录 + 补出厂默认映射 + 补 C 端角色"（§5.1）。

### 3.3 让"每个接口都必须声明权限"落地

| 方案 | 做法 | 优点 | 缺点/成本 | 风险 |
|---|---|---|---|---|
| **A（选）** | 显式 `require_permission(...)` 依赖 + 一条"路由清单"测试：遍历 FastAPI 路由，非白名单（公开：注册/审核提交、登录、MFA、健康、登出/刷新）都必须挂上声明了权限码与 realm 的依赖，否则测试失败 | 白名单是显式列表，新增接口忘挂鉴权会在 CI 直接红；对现有代码侵入最小 | 需要给每个路由建一份权限元数据（可用依赖对象自带标记，避免装饰器双写） | 白名单被随手扩大 → 白名单条目必须带理由注释，审查清单核对 |
| B | 全局中间件按路径前缀推断权限（`/api/v1/school/**` → `school.*`） | 覆盖率高，无需逐路由改 | 权限与真实动作脱节（同一路径 GET/DELETE 需要不同码），容易出现"看起来有鉴权其实粒度错" | 隐蔽放行，属安全类缺陷 |
| C | 只在业务分支里手写 `if not principal.has_permission(...)` | 灵活 | 无强制覆盖，必漏；审计格式不统一 | 与「服务端必须再次校验」的红线冲突 |

### 3.4 每请求查权限的性能债（TD-100）

TD-100 原处置是"已延后｜目标 M1-E（矩阵落地后按需加短 TTL 缓存 + 变更失效）"。本步实测后再定：`Principal` 每次请求 1~2 条 SQL（`principal.py:101-187`），在 60 码量级仍是索引内小 join。

- **结论**：本步**不做缓存**，把 TD-100 改判为「已延后｜M6 性能专项」，并新增一条基准测试（一次带鉴权请求的 SQL 次数与耗时上界）作为将来引入缓存的判定依据。理由：缓存 + 即时失效是"权限变更立即生效"这条约束（矩阵 §5-8）最容易做坏的地方，MVP 没有接口规模压力，先用测试钉住成本。

### 3.5 角色权限改动后怎么"热更新"（对应已确认口径 ⑥）

| 方案 | 做法 | 优点 | 缺点/成本 | 风险 |
|---|---|---|---|---|
| **A（选）** | 保持"每次请求从库读角色与权限"（现状 `principal.py:101-187`），改动**下一次请求即生效**；**不踢在线会话**，已签发的访问令牌在其 15 分钟寿命内仍按新权限判定（因为权限不落令牌） | 真正即时生效，无需重启、无需通知；收紧权限不会把全校老师突然踢下线；实现零改动 | 每请求 1~2 条 SQL（小 join）；若将来加缓存必须同步做失效 | 无 |
| B | Redis 短 TTL 缓存权限集 + 变更时主动失效 | 高频接口省查询 | 失效事件必须覆盖"角色改映射/用户改角色/数据范围改"三条路径，漏一条就是"改了不生效"或"旧权限残留" | 缓存与库不一致 = 越权或误拒，且难复现；属安全类风险 |
| C | 权限快照写进访问令牌 | 完全免查库 | 改权限必须递增 `token_version` 强制全员重签，等于每次调整都把在线用户踢下线 | 与"不影响正在使用的用户"直接冲突 |

**热更新语义定稿**：放宽权限立即生效；收紧权限同样下一次请求生效，**不作废在线会话**（会话轮换只用于账号级安全事件：改密、MFA 变更、管理员踢设备）。改角色映射、给用户增删角色、改数据范围三类动作全部写审计（`auth.role.changed` / `auth.user_roles.changed` / `auth.data_scope.changed`，本步只落审计动作名，接口在 M2）。

## 4. 选择结论

选 **3.1-A（收窄版）、3.2-A、3.3-A、3.4 不缓存、3.5-A**，全部是用户 2026-10-22 已确认口径①~⑥的展开。不选 B/C 的核心原因：把"系统里存在哪些权限项"也做成可配数据，会出现界面能勾、后端没有对应逻辑的假开关；把权限写进令牌则每次调整权限都要全员重签，与"不影响正在使用的用户"冲突；路径推断式鉴权粒度错且不可验证。

需要改文档：`docs/rbac-matrix.md`（v1.1 + 实现位置 + §6 待确认逐条给结论）、`docs/design.md`（§9 认证与 RBAC 补"权限码来自 Git 目录 + 迁移同步"）、`docs/handoff.md`、`docs/tech-debt.md`（TD-082/TD-100/TD-103 处置）。

### 4.1 矩阵 §6 待确认项的定稿（2026-10-22 用户确认；第 3 条为改后口径）

| # | 待确认项（`rbac-matrix.md:186-192`） | 本简报建议 |
|---|---|---|
| 1 | 校方管理员是否含 KB 审批/发布 | 建议：矩阵保持 △（可授予但非默认），即 `school_admin` 默认**不含** `school.kb.approval`/`school.kb.publish`，超管显式授予 |
| 2 | 班主任/辅导员是否自动获得请假审批 | 建议：不自动（需显式授予 `school.approval.leave`），且实际授予动作 E2/M2 做 |
| 3 | 教师 B 端角色能否上传 KB | **已改口径（2026-10-22 用户定稿）**：默认允许——`teacher_b_end` 出厂映射含 `school.kb.upload`；配套约束＝上传者永不自批（E2 实现）＋ 上传后设置查阅人群默认限本人/自己任课范围，扩到全校需管理员二次确认并审计（M3 实现，`school.kb.acl.manage` 对教师仍为 △） |
| 4 | 平台运营能否改模型密钥 | 建议：不能（`platform.config.edit` 仅超管），运营只给 view |
| 5 | 审计导出仅平台审计/超管？ | 建议：`platform.audit.export` 给超管 + 审计员，不给运营 |
| 6 | 是否需要"校方审计员"只读角色 | 建议：本期内置角色不加第四个校方角色（矩阵只列三个），M2 有真实需求再开自定义角色 |
| 7 | 数据范围是否支持年级/院系层级 | 建议：本步不扩，`class`/`course` 之外的层级留待 M2 组织模型确认（已确认部门不进 `data_scopes`） |

## 5. 数据与接口设计

### 5.1 数据

1. `app/rbac/catalog.py`：`PERMISSION_CATALOG`（矩阵 §3 全量码，含 SC-01 新增的 4 个：`school.kb.acl.manage`/`school.kb.chunk.edit`/`school.kb.chunk.confirm`/`school.kb.retrieval_test.unfiltered`；按口径 ⑦⑧ 归一后共 **59 码**：platform 12 + school 34 + campus 9 + observability 2 + eval 2）；每项 `(code, module, action, description)`；`module` 由码前缀派生（`platform`/`school`/`campus`/`observability`/`eval`），`action` 取末段。
2. `app/rbac/seeds.py`：`TENANT_BUILTIN_ROLES` 五个内置角色的出厂默认（每条映射注释矩阵 §4 行）：
   - `school_super_admin`＝矩阵"校方超管"列 √ 的全量，语义与 `provision_tenant()` 的 `module IN ('school','campus')` 保持一致（一致性测试钉住两者相等）；
   - `school_admin`＝"校方管理员"列 √ 的码：`school.dashboard.view`、`school.personnel.view`、`school.kb.view`、`school.message.view/send/read`；△ 行一律不进默认（含 KB 审批/发布/上传/人员增删改/敏感字段/角色管理/审批/审计，对应口径 ①②）；
   - `teacher_b_end`＝`school.kb.upload`（口径 ③⑤ 改后：默认放开上传）；其余 △ 行不进默认，"看自己文档的构建进度"依赖 E2 数据范围（登记 TD）；
   - `student`＝`campus.*` 全部 9 码；`teacher`＝`campus.*` 去掉 `campus.leave.create`/`campus.leave.withdraw`（矩阵 C 端请假列：学生 √、教师 —）；
   `PLATFORM_BUILTIN_ROLES` 三角色按矩阵列展开（超管=全量、运营=租户四码+`platform.config.view`+`platform.message.view/send`、审计=`platform.tenant.view`+`platform.audit.view/export`），不缩小 `779f731db342` 已种集合。
3. `app/rbac/__init__.py`：`sync_rbac_definitions(session)` —
   - `permissions`：按 code **upsert**（权限项目录跟代码走，新增码要补齐，描述可更新）；**删除**：口径 ⑧ 的 9 个旧码只在这些行没有 `role_permissions`/`platform_role_permissions` 引用时删除（当前引用为零，迁移内先删映射再删码并断言）。
   - 内置角色与其映射：**只在缺失时插入**（insert-if-missing），已存在的映射视为运营配置，同步不覆盖；`is_system=true` 只表示"出厂角色不可删"，不表示"权限集固定"。
     出厂默认的补写分两种口径（实现期由 `test_sync_is_idempotent_and_keeps_manual_edits` 逼出，见 `app/rbac/seeds.py:FULL_SCOPE_ROLES`）：
     **超管角色**（`tenant/school_super_admin`、`platform/platform_super_admin`）语义是"跟着目录全量走"，所以**只补不撤**——新增权限码要自动到位，运营撤销的那条保持撤销；
     **其他内置角色**只在该角色**尚无任何映射**时写入出厂默认，一旦运营动过（有行）就不再补，避免把撤销的码塞回去。
   - 内置角色与其映射的定义数据放在两张**代码持有的表** `rbac_builtin_roles` / `rbac_builtin_role_defaults`，由 `public.seed_builtin_roles(uuid)`（SECURITY DEFINER）读取；`provision_tenant()`（开发直建）与 `review_registration_request()`（生产审核通过）都改为调用它，避免把 Python 清单抄进 plpgsql 形成第二套真相。
   - 租户内置角色（含新增 `student`/`teacher`）对**存量租户逐个补齐**；新租户由 `provision_tenant()` 一次建齐。
4. 迁移 `e5f7a9c1b3d5_m1e1_rbac_matrix.py`（head 变 `e5f7a9c1b3d5`）：
   - ① 删除口径 ⑧ 的 9 个旧码（先删映射，断言无残留引用）；② 建两张定义表 + `seed_builtin_roles(uuid)` 函数，并把 `provision_tenant()`/`review_registration_request()` 改为调用它；③ 调用 `sync_rbac_definitions` 补齐 59 码目录、平台映射、各租户内置角色与出厂默认映射；④ 收尾断言"每个租户都至少有一个内置角色"，防止静默漏补；⑤ downgrade：删除新增角色与映射、删除新增码、把 9 个旧码连同平台角色原有的映射插回、恢复旧版 `provision_tenant()` 与 `review_registration_request()`、撤销 SELECT 权限并 DROP 两张定义表与 `seed_builtin_roles`。
   - upgrade 前置校验：若某租户存在与内置角色码同名但 `is_system=false` 的运营自建角色 → 抛 `RuntimeError` 中止（沿用 D2 的 fail-fast 风格），不覆盖运营数据。

### 5.2 接口（本步只开"读"，写接口留 M2 校管后台）

- `GET /api/v1/auth/me/permissions`（或复用 `/me`，二选一：本步**复用 `/me` 已有的 `roles`/`permissions`**，不新增接口，避免为不存在的 UI 造 API）。
- 现有接口挂权限：`platform_tenants.py`（已有 2 处）；`campus_chat.py` 加 `campus.chat.use`（realm=`tenant`，并把 trace 里的占位身份换成 `principal.tenant_id`/`principal.user_id`）；`auth.py` 的自助接口（改密/登出/刷新/MFA/会话列表）保持"仅需已认证"，公开接口进白名单。
- **交付结果**：路由清单落在 `app/rbac/routes.py`（`route_entries()` 展开 FastAPI 惰性路由，`PERMISSION_FREE_ROUTES` 是"路径 → 理由"的显式映射），由 `tests/test_route_permission_manifest_unit.py`（10 条）钉住：
  - 清单确实读到了全部路由（内部结构变化先在这里报警，而不是让其他用例静默通过）；
  - 非白名单接口必须声明权限码与 realm；白名单条目必须写理由；
  - 白名单与权限声明不得同时存在（口径漂移）；白名单不得留已删除路由（过期条目＝无人复核的放行口子）；
  - 白名单只能落在"认证自助 / 健康探针 / 公开登记"三类里，防止随手扩大；
  - 声明的权限码必须存在于 `PERMISSION_CATALOG`（引用幽灵码＝永远 403 的死接口）；
  - realm 与路径归属一致：`/platform/**` 用 `platform`，其余一律 `tenant`，不留空；
  - 已交付的平台审核接口仍按 `platform.tenant.review` 鉴权（改造时不能降级成 view）。
  权限元数据直接打在 `require_permission` 返回的依赖函数上（`__rbac_permission__`/`__rbac_realm__`），避免装饰器双写漂移。
- `DEV_SKELETON_LOOP_ENABLED` 的拒绝理由更新：生产仍拒绝开启该开关，但原因不再是"空壳无认证"（现在必须带 `campus.chat.use` 的租户会话），而是"空壳不产生回答，上线没有业务意义"。

### 5.3 与 E2 的接缝

`data_scopes` 本步**一行都不写**：模型与矩阵语义（`school`/`self`）已在 M1-A 建表，但读取方（服务端范围过滤、自批拒绝）在 E2 交付，先写行会留下"配了没用"的假状态。`require_data_scope` 与自批拒绝在 E2 交付；`school.kb.upload` 对 `teacher_b_end` 的默认放开同样在 E2 才具备"只能看自己上传的文档"的范围保证。

## 6. 实现顺序（TDD 红→绿→重构）

1. **红**：先写"矩阵与代码一致性"测试（解析 `docs/rbac-matrix.md` §3/§4，断言 catalog 覆盖全部权限点）→ 必红。
2. 绿：`catalog.py` + `seeds.py` 落地；`sync_rbac_definitions` 幂等（重复执行两次结果相同）。
3. 红→绿：目录同步迁移（删旧码 + 补 59 码 + 补内置角色出厂默认）+ 存量租户回归（升级前开通的租户，升级后超管权限集合不缩水、管理员/教师/C 端角色按出厂默认可查、能登录能调用受保护接口；downgrade 往返）。
4. 红→绿：路由清单测试（遍历 `app.routes`，非白名单必须带 `require_permission` 元数据）→ 逐个接口补声明。
5. 绿：`rbac-matrix.md` 升 v1.1、design.md §9 补口径、TD 台账改判。
6. 全量门禁 + 独立审查（Blocker/Major 修复后复审）。

## 7. 测试策略（含交付结果）

- **单元**：
  - `tests/test_rbac_matrix_unit.py`（第三轮审查后扩到 37 条）：catalog 无重复码、`module`/`action` 由码前缀派生正确、每个内置角色映射的码都存在（不引用幽灵权限）、租户角色不得含 `platform.*` 码、超管映射等于 `module IN ('school','campus')`、矩阵 §3 与目录逐码相等、§6 七条定稿逐条钉住；**§4 矩阵表本身也被解析成断言**（见下条）。
  - **§4 矩阵 ↔ 出厂默认的机器比对**（第三轮审查 M1：§4 此前只是人读的表格，改默认不会报警）：矩阵新增「对应权限码」列后，四条断言成立——
    ① 各行权限码**不重不漏**等于目录 59 码（补齐此前只在 §3 出现、没进矩阵的 `school.kb.delete`、`school.account_termination.request/appeal/freeze`、`campus.conversation.clear`、`campus.nav`）；
    ② 每个角色列的 √ 集合 = `seeds.py` 的出厂默认（`platform_super_admin` 列按 §4 说明取"平台运维域全量"，与"持有目录全量"分别断言）；
    ③ △ 集合与默认不相交（△=可授予但默认不给，同一格不得二义）；
    ④ 租户五个角色列的 √/△ 只能是 `code_realm()=="tenant"` 的码 —— 把 2026-10-23 用户定稿"观测/评估不下放租户角色"钉成断言。
  - `tests/test_route_permission_manifest_unit.py`（10 条，见 §5.2）。
  - `tests/test_principal_denial_audit_unit.py`（2 条，Minor 4）：`_audit_denied` 写审计失败时**拒绝仍然生效**、日志只落异常类型（不得出现 SQL 参数/令牌片段）；成功路径断言 `action`/`resource_id`/`result`/`metadata` 的实际内容。
  - `tests/test_skeleton_loop.py`：改为**直接调用处理器 + 合成租户主体**，覆盖开关关闭 404、事件契约、trace 必发/不含原文、**trace 身份取自主体**、缺 tenant_id 403、断线仍落 trace、无 sink 显式失败。HTTP 层鉴权边界不在这里测（认证依赖先于处理器执行，未认证只会得到 401，测不到内部行为）。
- **集成（真实 PG）**：
  - `tests/integration/test_rbac_sync.py`：同步后目录与代码逐码相等、9 个旧码消失、每个租户五个内置角色齐备且默认映射正确；幂等**且不覆盖人工改动**（撤销的映射不补回、额外授予的保留——正是这条测试逼出 §5.1 的两种补写口径）；同名非系统角色 → `RuntimeError` 且不落任何语句。
  - `tests/integration/test_migration_rbac_rollback.py`：在**临时库**上做 `head ↔ c4d6e8f0a2b3` 往返——升级后目录/定义表/`seed_builtin_roles` 出现在 `provision_tenant()` 与 `review_registration_request()` 两个函数体里、`provision_tenant(jsonb)` 产出五个角色与精确默认映射（含 `teacher_b_end` 仅 `school.kb.upload`、学生有 `campus.leave.create` 而教师没有、无 `platform.*`）；回滚后两张定义表与函数消失、目录退回 20 个旧码、只有 3 个角色、超管映射退回 `school`+`campus` 旧口径；再次升级可重放。
  - `tests/integration/test_campus_chat_rbac.py`（7 条）：未认证 401 无 trace；缺 `campus.chat.use` → 403 + `auth.permission.denied`（`reason=missing_permission`、含路径）且无 trace；放行 → SSE 走通且 trace 的 `tenant_id`/`user_id` 就是登录主体；另一租户学生用自己的 `tenant_id`（占位身份回归防线）；平台令牌 → 403 + `reason=realm_mismatch`；**权限热更新**（收回映射后同一未过期令牌下一次请求即 403、`/auth/me` 仍 200、`token_version` 仍为 0，重新授予即恢复）；已鉴权但请求体非法 → 422 且 trace 条数不变。
  - `tests/integration/test_rbac_sql_baseline.py`（2 条，TD-100 依据）：一次带鉴权请求 SQL 在 4~8 之间（实测 5；下限是第三轮审查 Minor 5 补的——降到 4 以下说明鉴权不再读库，"每请求从库读"的热更新口径被悄悄换掉了）；`require_permission` 的依赖本体在真实引擎上**零查询**。
- **业务场景（HTTP 全链路）**：`tests/business/test_bs_m0_01_health_to_trace.py` 就绪探活 → 用**生产同步入口**补齐的 `student` 内置角色登录 → 对话 SSE → trace 身份与内容边界 → 只读工具 HMAC 审计链可校验；补一条"未登录不能再打开对话流"。`test_bs_m1b` 的角色断言扩到五个内置角色。
- **前端/E2E**：本步不改前端（`/me` 已有 roles/permissions 未被消费），故不新增 Playwright 用例；此项在 §8 登记。
- **支撑改动**：`tests/support/auth.py` 的 `seed_tenant_user(..., permissions=...)` 现在会建一个 `is_system=false` 的测试角色并走真实 `user_roles → role_permissions → permissions` 链路（与运行时鉴权同源，不自建特权）；新增 `login_tenant()` 助手统一走真实登录。

## 8. 预判技术债（实现后登记进 `docs/tech-debt.md`）

| 条目 | 台账 | 计划处置 | 目标 |
|---|---|---|---|
| 权限码全量落库但部分码尚无接口使用，"码存在≠功能存在" | TD-136 | 已采纳（随接口收口） | M2/M3 |
| 角色/权限分配无管理接口与 UI（只能靠 SQL/迁移） | TD-137 | 已延后 | M2（校管后台 `school.role.manage`） |
| `data_scopes` 仅落 `school`/`self` 语义，`class`/`course` 未过滤 | TD-138 | 已采纳 | M1-E2（school/self）/ M2（班级、任课）/ M3（KB ACL） |
| 每请求查库未缓存（TD-100 改判，基线测试已交付） | TD-100 | 已延后 | M6 性能专项 |
| 路由权限清单测试的公开白名单需随 M2 持续复核 | TD-139 | 已采纳 | M2/M6 安全评审 |
| 权限矩阵变更缺少"变更审批"流程（改代码即改权限） | TD-140 | 已延后 | M6（配置双人确认同源 R7） |
| 前端未消费 `/me` 的 roles/permissions，菜单仍全量展示 | TD-141 | 已延后 | M2 |
| 权限项目录与"接口实际校验点"的一致性只有清单测试，没有自动比对（新增码但无处使用=空转） | TD-142 | 已采纳 | M2（随校管后台一并做 `rbac_check`） |
| 内置角色"重置为出厂默认"无接口，误配只能手工改库 | TD-143 | 已延后 | M2 |
| 教师上传后"人群限本人/任课班级、扩全校需管理员确认"依赖班级与任课关系（M2）与 KB ACL（M3） | TD-144 | 已采纳 | M2/M3 |
| 权限变更未做双人确认（改映射即时生效，无复核） | TD-145 | 已延后 | M6（R7 配置双人确认同源） |
| 全量型角色「只补不撤」：人工从两个超管收回的权限会被下一次同步补回 | TD-146 | 已延后 | M2（如需长期压制某码，改为库内"超管例外表"） |
| 迁移 `downgrade` 重置为出厂状态而非回滚前状态（系统角色人工增授、C 端角色 `user_roles` 绑定丢失） | TD-147 | 已延后 | M1 尾/部署 SOP（回滚前备份 `role_permissions`/`user_roles`） |
| 路由清单测试依赖 FastAPI 内部结构，框架升级可能静默漏读 | TD-148 | 已采纳（已设下限断言） | M6 依赖升级评审 |
| DB 触发器 `role_permissions_realm_guard` 仍只挡 `module='platform'`，`observability`/`eval` 可被直接 INSERT 进租户角色（代码侧已按 `code_realm()` 白名单收紧） | TD-149 | 已采纳 | M1-E2（与数据范围守卫迁移合并做一次触发器替换） |
| 出厂默认补授用"角色映射为空"当"未配置过"的启发式：撤销内置角色最后一条会被下一次同步补回 | TD-150 | 已延后（行为已被测试钉住） | M2（前台配置界面前必须改为 `defaults_applied` 标记） |
| 热更新语义与"立即断开某人"的边界：Redis 故障时 `is_active` fail-open，被踢设备最长 15 分钟仍可用 | TD-111（既有） | 已延后；本期只在 `docs/rbac-matrix.md` §5-8 与 `docs/design.md` §RBAC 落地口径写明例外 | M6 Redis 依赖专项 |

## 9. 高风险确认（已确认）

确认结论（2026-10-22，用户逐项回复）：

1. **同意**改存量租户的角色码（`school_admin` → `school_super_admin`），迁移含回滚。
2. 矩阵 7 条**按 §4.1 定稿**；其中第 3 条由用户改口径为"默认放开教师上传"。
3. **权限配置分两层**（用户明确否决"全部写死在代码"）：权限项目录跟代码；角色↔权限映射与数据范围存库、可前台增删改、热更新，配置界面 M2 交付。
4. **同意**本步不做前端、不做角色分配接口。
5. **同意** TD-100 从 M1-E 改判 M6，本期只加"一次带鉴权请求的 SQL 次数上界"基线测试。
6. 用户附加要求：**热更新不得影响正在使用的用户** → 定为"下一次请求生效，不作废在线会话/Cookie"（§3.5）。
7. **命名归一**（2026-10-22 追加确认）：`campus.chat` → `campus.chat.use`；矩阵 §3.4 的 `audit.view`/`audit.export` 与 `school.audit.view`/`platform.audit.export` 重复，从矩阵与目录中删除（`rbac-matrix.md` 升 v1.1）。
8. **旧码清理**（追加确认）：无引用的旧码（核对后 **9 个**）从目录与库中删除，矩阵为唯一口径；迁移带 downgrade 复原。
9. **C 端内置角色**（追加确认）：本步一起建 `student`/`teacher`（含 `provision_tenant()` 与存量租户补齐），否则对话接口挂权限后学生 403。
10. **改名迁移作废**（核对后自纠）：库里超管角色码从 M1-B 起就是 `school_super_admin`，口径 ② 无需动数据；测试与文档以 §2 的核对结果为准。

原始待确认清单（保留备查）：
1. 角色码改名会动**存量租户数据**（UPDATE + 函数重写），downgrade 也要能来回 — 是否同意。
2. 矩阵 §6 的 7 条按 §4.1 的建议口径定稿（尤其"校方管理员默认不含 KB 审批/发布""运营不能改密钥"）。
3. 权限目录放 Git 代码 + 迁移同步（3.1-A），意味着**改权限必须发版**，不能在线调。
4. 本步不做前端，不做角色分配接口（M2 做）。
5. TD-100 从 M1-E 改判为 M6。

确认后我按 §6 顺序开始 TDD 第一步（先写一致性测试）。

## 10. 交付结果与实现期偏差（2026-10-23 回填）

### 10.1 落地的文件

- 新增：`app/rbac/catalog.py`（59 码项目录）、`app/rbac/seeds.py`（内置角色与出厂默认 + `FULL_SCOPE_ROLES`）、`app/rbac/routes.py`（路由清单与免鉴权白名单）、`app/db/models/rbac.py` 追加 `RbacBuiltinRole`/`RbacBuiltinRoleDefault`、`alembic/versions/e5f7a9c1b3d5_m1e1_rbac_matrix.py`。
- 改动：`app/rbac/__init__.py`（`sync_rbac_definitions` 及其 6 条显式补写语句）、`app/auth/principal.py`（`require_permission` 文档与 realm 口径）、`app/api/campus_chat.py`（挂 `campus.chat.use`，trace 身份取主体）、`app/db/models/__init__.py`。
- 测试：`tests/test_rbac_matrix_unit.py`、`tests/test_route_permission_manifest_unit.py`、`tests/test_skeleton_loop.py`（改造）、`tests/integration/test_rbac_sync.py`、`tests/integration/test_migration_rbac_rollback.py`、`tests/integration/test_campus_chat_rbac.py`、`tests/integration/test_rbac_sql_baseline.py`、`tests/support/migrations.py`（临时库往返助手）、`tests/support/auth.py`（`seed_tenant_user(permissions=…)`、`login_tenant`）。

### 10.2 与简报的偏差（均为实现期发现，已在对应小节记录）

1. **默认映射的补写口径从"一条规则"变成"两种角色两类口径"**。原写"只在缺失时插入"，实现后被 `test_sync_is_idempotent_and_keeps_manual_edits` 证伪：超管语义是"跟着目录全量走"，若也只补缺失的新角色，目录加码后超管会漏新权限；若一律"缺失即补"，运营撤销的码会被塞回去。定稿见 §5.1（`FULL_SCOPE_ROLES` 只补不撤，其余角色仅在"该角色无任何映射"时写默认）。
2. **新增两张代码持有的定义表 + `seed_builtin_roles(uuid)`**，而不是把角色清单抄进 plpgsql。原因：`provision_tenant()`/`review_registration_request()` 是 SQL 函数，若在 Python seeds 与 SQL 各写一份默认映射就是两套真相；改为两个函数都调用同一个 SECURITY DEFINER 函数读定义表。迁移里的新函数体**取自已入库的历史迁移文件**（`importlib` 读取 + 锚点唯一性校验的定点替换），不重抄 SQL。
3. **口径 ⑧ 的旧码数量 7 → 9**（`school.profile.view/edit` 与 `school.user.view/create/edit/delete` 加 `school.role.assign`、`school.leave.approve`、`campus.leave.submit`），目录总数相应为 59（campus 9 而非 8）。
4. **`DEV_SKELETON_LOOP_ENABLED` 的存在理由换了**：不再是"空壳无认证所以生产禁开"，而是"空壳不产生回答，开它没有业务意义"（认证已由本步补上，见 §5.2）。

### 10.3 门禁

`ruff check` / `mypy --strict` / `alembic check` / `REQUIRE_TEST_SERVICES=1 pytest`（全量，见 §7）/ `bandit`（app 与 alembic）——结果与用例数在 `docs/handoff.md` §1 与提交说明里按实测回填。

实测（第五轮 Minor 修复后复跑）：后端 **481 passed**（7 分 02 秒）、`ruff check app tests` 全绿、`mypy --no-incremental app tests` 128 文件无问题、`alembic check` 无漂移且 head=`e5f7a9c1b3d5`、`bandit -r app -x tests` 与 `bandit -r alembic -s B608` 退出码 0。

### 10.4 独立审查轮次

五轮（逐轮 findings、修复、取证与"已核实不成立"清单见 `docs/review-log.md` 的 M1-E1 小节）：第一轮 1 Blocker + 5 Major + 8 Minor、第二轮通过（1 条信息项）、第三轮 2 Major + 5 Minor（矩阵 §4 从未被机器校验 + 种子自检接线无取证）、第四轮 1 Major + 3 Minor（启动自检仍按 `module='platform'` 判越界）、第五轮通过（0 Blocker / 0 Major / 3 Minor，其中 2 条随手修复、1 条并入 TD-139）。

