# M1-C — 三端登录与令牌（AUTH-01/02）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-21 |
| 状态 | **待确认**（§3 六项决策需用户拍板后编码） |
| 关联功能点 | M1-C：AUTH-01 三端登录（平台/校方 B 端/学生教师 C 端）、AUTH-02 JWT/Refresh 轮换与撤销 |
| 验收引用 | `docs/spec.md` §3.1 AUTH-01/02、A2（15m/7d、bcrypt cost 12、首登改密、登出/改密/删除撤销家族）、`docs/milestone.md` §4 M1 退出标准、`docs/rbac-matrix.md` 平台权限码 |
| 顺带收口 | TD-067（过期令牌清理）、TD-074（realm↔连接断言）、TD-078（删除开发审核令牌）、TD-081（真实平台审核人）；TD-092/094 是否并入见 §3.6 |

## 1. 目标与不解决

**目标**：平台运营/超管、校方 B 端用户、C 端学生/教师都能登录；访问令牌 15 分钟 + 刷新令牌 7 天轮换；刷新令牌重用被检测后撤销整个令牌家族；登出/改密/账号删除即撤销；`must_change_password=true` 的账号在改密前只能用改密/登出/查看自己；平台与租户 realm 与会话所用数据库连接严格绑定；生产不再依赖 `DEV_PLATFORM_REVIEW_TOKEN`。

**不解决（后续功能点）**：密码强度/历史/重置策略与失败锁定（M1-D）、MFA（M1-D）、RBAC 数据范围与权限矩阵细化（M1-E）、审计哈希链（M1-F）、消息/通知（M5）。本期不做登录页美化与"记住我"以外的 UX 增强。

## 2. 数据与密钥

- 复用 `refresh_tokens`（已含 `jti/family_id/user_id/token_hash/issued_at/expires_at/rotated_to_jti/revoked_at/revoked_reason/ua_hash/ip_hash` 与 `(tenant_id,user_id)`、`expires_at`、`family_id` 索引）——**本期预计无需新表**；若并发刷新需要，新增 `refresh_tokens.rotated_at` 一次小迁移。
- 复用 `users.token_version / must_change_password / status / password_changed_at`，`platform_users` 同构字段；平台审核接口改读 `platform_users` 与 `platform_roles`（权限码 `platform.tenant.review/activate/view` 已在 M1-A 种子中）。
- 密钥：`JWT_SECRET`（≥32 字节，仅 `.env`/密钥管理）、`JWT_ISSUER`、`JWT_AUDIENCE_TENANT/PLATFORM`、`ACCESS_TOKEN_TTL=15m`、`REFRESH_TOKEN_TTL=7d`，生产缺失即启动失败；签名密钥支持 `kid` 便于轮换。
- Redis：仅做撤销广播与短期缓存（`auth:revoked:jti:<jti>`、`auth:user-token-version:<realm>:<id>`），键带 realm 前缀，不使用 Redis 作为令牌唯一存储。

## 3. 需要拍板的决策（候选方案）

### 3.1 令牌形态与撤销语义

- **方案 A（推荐）**：访问令牌 = JWT（`sub/tenant_id/realm/aud/iss/jti/token_version/must_change_password/exp`，15 分钟）；刷新令牌 = 256 位随机串，**只存 SHA-256 哈希**，每次刷新轮换并把旧行 `rotated_to_jti` 指向新 jti；检测到使用已轮换/已撤销的刷新令牌 → `revoked_reason='reuse_detected'` 并撤销**整个 family**；JWT 撤销靠 `token_version` 比对 + Redis jti 黑名单（登出/改密后立即失效）。
  - 优点：无状态校验为主（每请求一次 Redis 查询可缓存）、可立即撤销、数据库只存哈希。
  - 缺点：需要在 PG 与 Redis 之间保持一致（Redis 丢失可由 PG 兜底重建）。
- 方案 B：access/refresh 都是 JWT、纯无状态。
  - 缺点：无法满足"登出/改密立即失效"，且刷新令牌泄漏窗口大，不推荐。
- 方案 C：Redis 会话 id，无 JWT。
  - 缺点：Redis 成为单点强依赖；与 M4/M6 横向扩容的既有假设（Redis 非唯一状态源）冲突。

### 3.2 realm 分离与连接绑定（TD-074）

- **方案 A（推荐）**：同一 API，令牌 `aud/iss` 区分 `tenant` / `platform`；认证依赖按 realm **强制**选择 `db_session(tenant_id=...)`（租户）或 `db_session(platform=True)`（平台），错配即 401/403；测试固定"平台令牌不能读租户业务表、租户令牌不能读平台表/审计全量"。
  - 优点：不拆服务也能把隔离钉在数据库角色上（延续 M1-A 双连接设计）。
  - 缺点：依赖代码纪律，必须靠测试兜底（因此 TD-074 标记为必做）。
- 方案 B：平台与租户各一套 API 前缀 + 独立应用实例。
  - 优点：物理隔离更强；缺点：MVP 运维与部署成本翻倍，且与现有单应用架构冲突。
- 方案 C：平台账号与租户账号共用 users 表，用布尔列区分。
  - 缺点：违反已确认的 realm 分离与跨租红线，直接排除。

### 3.3 C 端登录标识

- **方案 A（推荐）**：学校编号 + 学号/工号 + 密码（A2 已锁定）。登录前用**平台连接**按 `school_code` 解析 `tenant_id`（租户表只读），随后切租户连接校验用户与密码；前端传入的 `tenant_id` 一律忽略。
  - 优点：符合锁定决策；学生无邮箱也能用。
  - 缺点：多一次平台连接查询（可缓存 school_code→tenant_id，TTL 短）。
- 方案 B：全局唯一用户名。
  - 缺点：与 A2 冲突，且跨租用户名可枚举。
- 方案 C：手机号/邮箱登录。
  - 缺点：学生手机号属敏感 PII，采集与合规面更大。

### 3.4 `must_change_password` 门禁

- **方案 A（推荐）**：`must_change_password` 写入访问令牌 claim，认证依赖对除"改密/登出/我的信息"之外的接口返回 `403 PASSWORD_CHANGE_REQUIRED`；改密成功后自增 `token_version`，旧令牌全部失效。
  - 优点：无额外查询；行为明确可测。
  - 缺点：claim 与库不一致的窗口 ≤15 分钟（改密接口内同时更新库，claim 由 token_version 兜底）。
- 方案 B：每次请求查库判断。
  - 缺点：每请求多一次数据库往返；收益仅是更短的窗口，不划算。
- 方案 C：前端跳转强制。
  - 缺点：可绕过，直接排除。

### 3.5 平台审核接口替换（TD-078 / TD-081）

- **方案 A（推荐）**：删除 `DEV_PLATFORM_REVIEW_TOKEN`；平台账号（`platform_users` + `platform_roles`，权限码 `platform.tenant.review`）登录后调用审核接口，审计 `actor_id` 为真实平台用户；平台账号用一次性初始化脚本创建（随机初始密码 + `must_change_password=true`）。
  - 优点：满足"生产不放行开发开关"与"真实审核人"两条既有要求。
  - 缺点：需要新增脚本与平台登录入口（admin 应用加平台登录页）。
- 方案 B：保留开发令牌到 M6。
  - 缺点：生产禁止配置该令牌（配置校验已强制），等于审核功能在生产不可用，不可接受。

### 3.6 TD-092 / TD-094 是否并入本期

- **方案 A（推荐）**：本期并入 **TD-094**（把公开注册提交收进 SECURITY DEFINER 函数，杜绝平台凭据绕过接口插入伪造申请）；**TD-092 延后到 M1-E**（审核函数不再接受调用方传入的密码哈希/快照 key，需与权限矩阵一起改，避免本期范围膨胀）。
- 方案 B：两项都并入本期。
  - 缺点：改动审核函数签名与邮件流程（需要函数返回一次性明文密码），范围与风险显著增加，M1-C 会变成认证 + 知识库前门两块。
- 方案 C：两项都延后。
  - 缺点：TD-094 的"平台凭据可伪造申请"在 M1-C 落地后仍然存在，而平台凭据在本期开始真实可用，风险窗口不合理。

## 4. 实现顺序（数据 → 接口 → 界面）与 TDD 清单

1. **数据/配置**：`JWT_*` 配置与生产校验（缺失即失败）；可选 `refresh_tokens.rotated_at` 迁移；平台管理员初始化脚本 `scripts/seed_platform_admin.py`（密码从环境变量读取，禁止默认值入库）。
2. **令牌与应用服务**（`app/auth/{tokens,service,principal}.py`）：签发/校验/轮换/重用检测/家族撤销/`token_version` 比对/Redis 黑名单写入与读取；`get_current_principal` 依赖含 realm 校验、连接绑定断言与 `must_change_password` 门禁。
3. **接口**：`POST /auth/login`（`realm=tenant|platform`，C 端 body 含 `school_code`）、`POST /auth/refresh`、`POST /auth/logout`、`POST /auth/change-password`、`GET /auth/me`；平台审核接口切换到正式鉴权并删除开发令牌依赖。
4. **界面**：平台登录页与校方登录页（admin 应用）、C 端登录页（Vant，学校编号 + 学号/工号）、强制改密页；access token 存内存，refresh token 用 `HttpOnly + Secure + SameSite=Lax` Cookie（C 端需兼容微信内置浏览器，M4 前只做静态验证）。
5. **定时任务**：Arq `cleanup_refresh_tokens`（删除已过期/已撤销且超出保留期的行，TD-067），保留审计事件。
6. **TDD 红→绿清单**（每条先写失败测试）：
   - 令牌：签发/校验、过期、错误 audience、错误 realm、`kid` 轮换、`token_version` 自增即刻失效；
   - 登录：平台账号登录、校方超管首登（`must_change_password`）、C 端学校编号+学号登录、错误学校编号/密码同一错误文案（不泄漏账号存在）、disabled/locked 用户拒绝；
   - 刷新：正常轮换、旧令牌重用 → 家族全撤销 + `reuse_detected`、并发刷新只成功一次、过期刷新拒绝；
   - 撤销：登出、改密、管理员撤销（`admin_revoke`）、账号删除（`account_deleted`）后刷新令牌不可用；
   - 门禁：未改密用户访问业务接口 `403 PASSWORD_CHANGE_REQUIRED`，改密接口可用；
   - realm/连接：平台令牌访问租户接口 403、租户令牌访问平台接口 403、两种令牌分别走 `platform=True` / `tenant_id` 连接（断言连接角色）；
   - 清理任务：过期/撤销行被清理、保留期内不被清理；
   - 审计与脱敏：登录成功/失败、刷新重用、登出、改密均写审计；日志与审计不含令牌明文、Cookie 值、密码原文。

## 5. 测试策略

- **单元**：令牌编解码与 TTL、claim 校验、门禁判定、错误文案统一、Redis key 构造。
- **集成（真实 PG + Redis）**：登录/刷新/登出/改密全链路、并发刷新、家族撤销、`token_version` 失效、清理任务、realm 与连接绑定断言。
- **业务场景**：`BS-M1-C-01` 三端登录（平台/校方/C 端）→ 首登改密 → 业务接口门禁解除；`BS-M1-C-02` 刷新令牌重用 → 家族撤销且重新登录才可用；`BS-M1-C-03` 平台审核接口用真实平台账号 + 权限，无权限账号 403，审计 `actor_id` 为真实用户；`BS-M1-C-04` 跨租/跨 realm 拒绝（平台令牌读租户业务表失败、租户 A 令牌读租户 B 数据为空）。
- **E2E（Playwright）**：平台/校方/C 端登录页与强制改密页；M1 阶段只覆盖主路径，异常路径归集成测试。
- **性能**：本期不做（M6 k6 统一压测）。

## 6. 预判技术债（实现后登记 `docs/tech-debt.md`）

| 预判条目 | 处置建议 |
|---|---|
| PG 与 Redis 双写一致性（撤销广播丢失） | 以 PG 为准，Redis 读穿透回填；键带 TTL |
| 访问令牌撤销窗口（黑名单依赖 Redis） | 15 分钟内以 `token_version` 兜底；Redis 不可用时的降级策略需明确 |
| C 端 refresh Cookie 在微信内置浏览器的行为 | M4 前静态验证，必要时改"内存 + 静默刷新" |
| 多设备会话上限与"退出其它设备" | 本期不做，登记到 M5 |
| 平台账号初始化（无 UI 注册） | 脚本 + 运维手册；M6 配置双人确认时复核 |
| 登录失败锁定/验证码 | M1-D（AUTH-05），本期不做但接口预留计数位 |
| TD-092 审核函数参数最小化 | M1-E 与权限矩阵一起改 |

## 7. 高风险确认

认证/令牌属**高风险**功能，编码前需确认 §3 六项决策；确认后按"数据 → 令牌 → 接口 → 界面 → 清理任务"顺序实现，逐条走 TDD 与业务场景门禁；实现中若发现与 spec 冲突，先提变更再改代码。

## 8. 审查对照（预登记）

独立审查重点关注：令牌明文是否出现于日志/审计/响应；Redis key 是否带租户/realm 边界；刷新重用的并发正确性；`token_version` 与黑名单的一致性；realm 与数据库连接是否真的绑定（不是只校验 claim）；Cookie 安全属性；错误文案是否泄漏账号存在性；审计是否覆盖登录/刷新/登出/改密/重用。

## 9. 实现结果

待编码后回填。
