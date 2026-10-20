# M1 — 租户与身份 实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-20 |
| 状态 | 已确认（2026-10-20 用户确认：拆分顺序 / refresh 存储 / TOTP / 审计落库 均按推荐） |
| 关联功能点 | M1（TEN-01~03、AUTH-01~05、RBAC-01~03、AUD-01、租户中间件 + RLS + 仓储过滤） |
| 验收引用 | `docs/milestone.md` §4.4；`docs/spec.md` §3.2/§3.3/§3.4 + AUD-01；`docs/rbac-matrix.md` v1.0（当前状态"待用户确认"） |

## 1. 目标

把 M0 的"空壳"变成**可登录、可鉴权、可隔离、可审计**的系统骨架：学校能注册、平台能审核开通、三端能登录、RBAC 与数据范围生效、跨租默认拒绝、审计链可校验。

不解决：组织与人员 CRUD/导入（M2）、知识库与 AI（M3/M4）、审批/请假业务流（M5）、配置中心与双人确认（M6）、完整 WORM 归档与外部锚点（M6）。

## 2. 拆分为 6 个功能点（单点推进，每点：测试 → 独立审查 → 提交 → 推送 → CI 绿）

| 点 | 内容 | 对应验收 |
|---|---|---|
| **M1-A 数据模型与租户基座** | Alembic 基线迁移（tenants/users/roles/permissions/role_permissions/user_roles/refresh_tokens/audit_events 等）；`tenant_id` 全表覆盖；**PostgreSQL RLS** 策略；租户上下文中间件；Repository 强制过滤；角色/权限种子数据 | 隔离红线、RBAC-01 基础 |
| **M1-B 租户注册与审核** | 学校注册与资质提交、去重策略、审核通过/拒绝（含理由）、学校编号生成、超管账号 + 随机初始密码 + 邮件（MailHog/SMTP）、审核记录与凭证快照；`DEV_SKIP_SCHOOL_REVIEW` 生产禁用 | TEN-01/02/03 |
| **M1-C 三端登录与令牌** | 平台/校方（学校编号+工号/学号）/C 端登录；JWT access 15m；refresh 7d 轮换 + **复用检测撤销 token 家族**；登出/改密/删除撤销；`must_change_password` 强制改密门禁 | AUTH-01/02 |
| **M1-D 密码策略、锁定与 MFA** | 策略校验（≥10 位、≥3 类字符、≤128、禁含学号/工号、最近 5 次历史）；bcrypt cost 12；5 次失败锁 15 分钟；员工邮件重置 / 学生校管重置；TOTP MFA（可选/可强制） | AUTH-03/04/05 |
| **M1-E RBAC 与数据范围** | 角色/权限矩阵落库与校验；数据范围 school/class/course/self；审批职责分离基础（不能自批、超管例外默认关闭需理由+审计）；跨租 404 不暴露存在性 | RBAC-01/02/03 |
| **M1-F 审计落库** | `audit_events` 仅追加表 + HMAC 哈希链（现内存实现 → PG 实现）、链校验脚本/接口、按租户/时间查询；为 M6 的 WORM 归档与外部锚点留接口 | AUD-01 |

## 3. 候选方案（需要拍板的四处）

### 3.1 租户隔离落地（RLS 与仓储过滤）

**方案 A（推荐）：应用层仓储强制 + PostgreSQL RLS 双保险**
- 做法：所有业务表带 `tenant_id`；仓储层查询/写入强制注入 `tenant_id`；每个请求在事务开始时 `SET LOCAL app.tenant_id = '<uuid>'`（SQLAlchemy `after_begin` 事件），RLS 策略用 `current_setting('app.tenant_id', true)::uuid` 判定；**未设置上下文时策略 fail-closed（查不到任何行、写入被拒）**；平台级表（tenants 等）不用租户 RLS，用平台权限控制。
- 优点：满足 A6/A23 的"双保险"；即使业务代码漏了过滤，数据库仍拦住；迁移可测（两租户交叉断言）。
- 缺点/成本：迁移里每张表要写 policy；连接池下 `SET LOCAL` 必须绑事务（漏绑会跨请求串租户，是最大风险点）。
- 风险：**连接复用导致串租户** → 用"事务内 SET LOCAL + 事务结束自动失效"并写专门的并发/复用回归测试；另需为迁移账号说明 BYPASSRLS 例外。
- 对 spec/design 影响：无（落实既有决策），但 design.md 需补"RLS 实现细则"。

**方案 B：只做应用层仓储过滤** —— 拒绝：破坏"RLS 作为第二道防线"的既定要求。
**方案 C：每租户独立 schema/库** —— 拒绝：与"共享库 + tenant_id"已定决策冲突，迁移/运维成本高。

### 3.2 Refresh token 的存储与撤销

**方案 A（推荐）：PostgreSQL 为准 + Redis 加速标记**
- 做法：`refresh_tokens(jti, family_id, user_id, tenant_id, token_hash, issued_at, expires_at, rotated_to, revoked_at, revoked_reason, ua_hash, ip_hash)`；刷新时校验并轮换（旧 jti 标记 `rotated_to` 指向新 jti）；**复用检测**：若收到已轮换/已撤销的 jti → 撤销整条 family；登出/改密/删除 → 按 user 批量撤销；Redis 存短 TTL 的"已用 jti"与"family 已撤销"标记用于快速判定（Redis 丢失可从 PG 重建）。
- 优点：刷新语义可审计、可追溯（满足"改密/删除撤销全部 refresh token"的持久要求）；Redis 只是加速，不是唯一事实来源。
- 缺点/成本：多一张表 + 一个缓存层；需要一个过期清理任务（Arq 定时）。
- 风险：轮换竞态（并发刷新同一 jti）→ 用行级锁或唯一约束 + 幂等语义处理。

**方案 B：纯 Redis** —— 拒绝：重启/驱逐即丢，且"撤销全部 refresh token"与审计追溯缺持久事实来源。
**方案 C：纯 PostgreSQL** —— 可行且更简单，但每次刷新都查库；无缓存加速，高频刷新场景压力集中在 PG。若你希望"零新增缓存复杂度"，选 C 也成立。

### 3.3 MFA（TOTP）实现范围

**方案 A（推荐）：M1 实现完整 TOTP**
- 做法：`pyotp` 生成密钥（加密存储）+ 二维码 URI（B 端展示）、启用时校验一次、登录时校验、错误验证码计入失败次数；策略字段（按角色是否强制）落库，M6 由配置中心接管；恢复码本期不做（登记 TD）。
- 优点：AUTH-04 一期达成；生产可对 SaaS 员工/超管强制。
- 缺点/成本：多一套密钥生命周期（加密、重置流程）；测试要覆盖时钟偏移窗口。

**方案 B：M1 只落策略字段与接口预留，TOTP 实现挪到 M6** —— 优点是最小改动；代价是 AUTH-04 本期只做到"可选开关"，验收需相应改口径。

### 3.4 审计哈希链落库时机

**方案 A（推荐）：M1 落 PG 并收口 AUD-01**
- 做法：`audit_events(sequence, event_id, tenant_id, actor_id, action, resource_type, resource_id, metadata, prev_hash, hash, created_at)` 仅追加（数据库层禁止 UPDATE/DELETE：撤销权限 + 触发器/RLS 写策略）；HMAC 链与现有内存实现同算法；提供校验脚本与只读接口（平台审计角色）；M6 再加 WORM 归档与外部锚点。
- 优点：AUD-01"可查 + 哈希链校验通过"本期即可验收；M1 的登录/审核/权限事件马上有落点。
- 缺点/成本：迁移与写入路径要严格"只追加"；高频事件的写入性能依赖索引设计。
- 风险：审计写入失败不能静默（fail-closed 还是 fail-open 需明确：**本方案取"关键动作 fail-closed，非关键动作记录告警"**，并在实现处写清边界）。

**方案 B：M1 建表但仅写内存实现，M6 再落库** —— AUTH/审核的审计验收需顺延到 M6，与退出标准冲突。

## 4. 选择结论

- 选择（2026-10-20 用户确认）：
  - **§2 拆分与顺序**：按推荐 6 点顺序（M1-A 数据模型与租户基座 → M1-B 注册审核 → M1-C 三端登录与令牌 → M1-D 密码/锁定/MFA → M1-E RBAC 与数据范围 → M1-F 审计落库）；
  - **3.1**：方案 A（应用层仓储强制 + PostgreSQL RLS 双保险）；
  - **3.2**：方案 A（PG 为准 + Redis 加速标记）；
  - **3.3**：方案 A（本期实现完整 TOTP，恢复码延后）；
  - **3.4**：方案 A（本期落库并收口 AUD-01，M6 再补 WORM 与外部锚点）。
- 是否需要改 spec/design：需要补充 design.md 的"RLS 实现细则""令牌家族与撤销语义""平台角色权限映射""审计表只读约束"，不改任何已确认的对外行为。

## 5. 实现顺序与 TDD 测试清单（以 M1-A 为例，其余点照此展开）

1. **数据/契约**：SQLAlchemy 模型 + Alembic 基线迁移 + RLS 策略 + 种子数据（角色/权限/平台初始账号）。
2. **接口/服务**：租户上下文（从 JWT 注入，绝不接受前端传 tenant_id）+ 仓储基类（强制 tenant_id）+ 事务与 `SET LOCAL` 接线。
3. **界面**：本期不做（B 端登录页在 M1-C 后接一次性骨架）。
4. 红→绿→重构测试：
   - [ ] 单元：仓储过滤（含"未带 tenant_id 直接拒绝"）、权限矩阵判定、`SET LOCAL` 事务绑定
   - [ ] 集成（真实 PG）：两租户交叉查询返回空/404；`SET LOCAL` 在连接复用后不残留（并发 20 请求交替租户）；RLS policy 覆盖度检查（迁移里每张业务表都有 policy）
   - [ ] 业务场景：注册→审核→开通→首登改密；三端登录；锁定；token 轮换与家族撤销；跨租拒绝；审计链校验
   - [ ] E2E：B 端登录页（M1-C 之后补）
   - [ ] 性能：登录接口与仓储基线的轻量压测（非门禁，仅记录）

## 6. 预判技术债（先列，落地时登记编号）

| 待登记项 | 处置 |
|---|---|
| TOTP 恢复码、密钥重置流程 | 延后（M6 或按需） |
| refresh token 过期清理任务 | 随 M1-C 落地（Arq 定时） |
| RLS policy 覆盖度自动检查 | M1-A 内做成迁移测试，防新表漏策略 |
| 审计 fail-closed/fail-open 边界的细化 | M1-F 记录为设计决策 + 测试用例 |
| 邮件模板 i18n、退信处理 | 延后 M6 |
| 登录风控（设备指纹/异常 IP） | 已拒绝（MVP 不做） |

## 7. 高风险确认

- 是否属于高风险功能：**是**（认证/令牌、租户隔离/RLS、权限矩阵、审计），按 AGENTS.md §11 与审查清单需**用户确认后才编码**，并在完成后做独立审查 + 人工复审。
- 需要用户确认：§2 拆分与顺序；3.1 RLS 双保险落地；3.2 refresh 存储（A 混合 / C 纯 PG）；3.3 TOTP 是否本期实现；3.4 审计是否本期落库。
- 确认结论（2026-10-20）：四项均按推荐执行（6 点顺序 / RLS 双保险 / PG+Redis / 完整 TOTP / 审计落库）。每个功能点完成后照常做独立审查 + 修复复测 + 推送。

## 8. 审查对照

- 审查时重点验证：
  - 前端传入的 `tenant_id`/角色是否被完全忽略（只信 JWT）；跨租是否 404 而非 403（不暴露存在性）；
  - `SET LOCAL` 是否绑事务、并发复用下是否串租户；未设置上下文是否 fail-closed；
  - refresh 轮换的并发与复用检测（同一 jti 并发刷新、旧 jti 复用撤销整族）；
  - 首登强制改密是否在业务 API 上真正生效（除改密/登出外 403）；
  - 锁定计数是否可绕过（多 IP、并发）；密码历史与"禁止含学号/工号"是否在服务端校验；
  - 审计表是否真的只追加（UPDATE/DELETE 被拒）、哈希链校验、失败不静默；
  - 生产开关（跳过审核/跳过扫描）在 production 是否 fail-fast；
  - 新增测试是否断言业务输出（DB 状态/审计/状态机）而非仅 HTTP 码。
- 允许的偏差：TOTP 恢复码与 WORM 归档不在本期（已在 §6 登记）。

## 9. 执行结果：M1-A 数据模型与租户基座（2026-10-20）

| 项 | 结果 | 证据 |
|---|---|---|
| 数据模型 | 13 张表（tenants/platform_users/platform_roles/platform_user_roles/permissions/users/roles/role_permissions/user_roles/data_scopes/refresh_tokens/audit_logs/platform_role_permissions） | `backend/app/db/models/**` |
| 迁移 | Alembic 基线（`779f731db342`）：建表 + RLS 策略 + 仅追加触发器 + CHECK 约束 + 权限目录/平台角色种子 + 应用组角色授权 | `backend/alembic/versions/` |
| 会话与上下文 | `db_session(tenant_id=..., platform=...)` 在事务内 `set_config(..., is_local=true)`；请求上下文 contextvar 生命周期由中间件收口 | `app/db/session.py`、`app/tenant/{context,middleware}.py` |
| 仓储强制 | `TenantScopedRepository`：查询强制 tenant_id、写入校验一致性、非租户模型拒绝使用 | `app/db/repository.py` |
| RLS 生效验证 | 30 条集成测试：无上下文 fail-closed、跨租读写被拒、连接池复用/并发不串租户、平台上下文不绕过业务表、审计表权限与策略 | `tests/integration/test_rls_*.py`、`test_audit_append_only.py` |
| 门禁 | 覆盖度守卫：任何带 tenant_id 的表必须 NOT NULL + FORCE RLS + 有策略；平台表不得带 tenant_id | 同上 |
| 关键发现 | ① **超级用户绕过 RLS**：运行时必须用非超级用户 → 迁移创建 `campus_elf_tenant`/`campus_elf_platform` 两个组角色（平台/租户分离，平台连接无业务表权限），测试栈创建登录角色并导出 `TEST_APP_DATABASE_URL`/`TEST_PLATFORM_DATABASE_URL`；② `fileConfig` 默认禁用既有 logger，会连带关掉 uvicorn 日志（已在 env.py 修正）；③ Windows GBK 读不了含中文的 alembic.ini（已改 ASCII + 显式 utf-8） | 测试与迁移实测 |
| 测试 | 后端全量 **131 passed**（M1-A 新增 45 条：仓储 7、上下文 4、中间件 3、RLS 隔离 14、覆盖度 4、审计仅追加 5、生产 db_session 7（含缺 PLATFORM_DATABASE_URL 用例）） | `uv run pytest` |


### 审查修复补充（M1-A 第一轮审查后）

- **Blocker**：集成用例用超级用户 TRUNCATE 关键表但没做「一次性库」护栏 → 所有集成 fixture 先
  `assert_test_database_dsn`，并对应用连接断言「非超级用户且无 BYPASSRLS」。
- **Major（门禁）**：缺 TEST_* 时安全用例会被静默 skip → 新增 `REQUIRE_TEST_SERVICES=1` 硬门禁（CI 与 `ci.sh` 均启用）。
- **Major（覆盖）**：生产函数 `db_session()` 零覆盖 → 新增 `tests/integration/test_db_session.py`（6 条：隔离、平台审计、互斥、非法 UUID、回滚、连接池复用）。
- **Major（接线）**：中间件未接入应用 → `create_app()` 已注册；实现改为**纯 ASGI**（`BaseHTTPMiddleware` 在另一个 Context 执行，ContextVar Token 无法还原，实测报错）。
- **Major（权限漂移）**：`ALTER DEFAULT PRIVILEGES` 会让迁移重放后审计表意外获得 UPDATE → 改显式逐表授权，并断言应用角色对审计表无 UPDATE/DELETE/TRUNCATE。
- **Major（角色隔离）**：单角色持平台表全量 DML → 拆租户/平台两个角色（平台连接读不到 `users`，租户连接读不到 `platform_users`，均有断言）。
- **Major（跨租父行）**：关联表可嫁接他租父行 → 复合外键 + `(id, tenant_id)` 唯一约束，并补测试。
- **Major（互斥与规范化）**：`db_session(platform=True, tenant_id=X)` 曾同时生效 → 改为互斥报错；非法 tenant_id 在应用层拒绝（不再落到数据库变 500）。
- **Major（策略健壮性）**：非法/空 `app.tenant_id` 会触发类型转换错误 → 策略改用 `pg_input_is_valid` + `case` 兜底（非法值退化为 0 行）；补测试。
- **Major（门禁覆盖面）**：覆盖度守卫漏掉「新表完全没有 tenant_id」与 `USING (true)` 式策略 → 增加白名单反向校验与「策略必须引用上下文变量」断言。
- **Major（审计占位）**：`prev_hash/hash` 曾为 NOT NULL 占位 → 改可空 + `hash_version=0` 表示未入链（避免占位哈希被永久固化）。
- **Minor 已修**：枚举 CHECK 约束、`list_all` 稳定排序、`request_id` 长度/字符集校验、`clean_test_data` try/finally、downgrade 残留说明（角色不随迁移回滚）、bandit 覆盖 `alembic/`（两处常量 SQL 标注 nosec 并写明理由）、`db_session` 与中间件补文档标注。
- **仍待处理（已登记）**：TD-069~073（`updated_at` 非 ORM 更新、cursor 分页、零 skip 断言、平台跨租通道、生产启动自检）。
