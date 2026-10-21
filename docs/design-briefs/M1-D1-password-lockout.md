# M1-D1 — 密码策略/历史/重置与失败锁定（AUTH-03/05）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-22 |
| 状态 | 已确认（2026-10-22；结论见 §8） |
| 关联功能点 | M1-D 拆分后的 D1；D2 为 AUTH-04 MFA，另立简报 |
| 验收引用 | `docs/spec.md` §3.3 AUTH-03/05；`docs/design.md` §5.2、§9；`docs/milestone.md` §4.2 |

## 1. 目标与不解决

**目标**

1. 落地 AUTH-03：密码策略（10~128 字符、至少 3 类字符、禁止含学号/工号、最近 5 次历史、bcrypt cost 12）、员工/平台账号邮件重置、首登强制改密沿用现有门禁。
2. 落地 AUTH-05：同一账号 15 分钟内连续 5 次认证失败 → 锁定 15 分钟；锁定与解锁自动进行，成功登录清零；全过程审计。
3. 新增配置化参数、迁移、RLS 覆盖、MailHog/SMTP 邮件链路与最小重置页面。
4. 为 D2 MFA 预留失败计数语义（TOTP 错误也计入同一计数器）。

**不解决**

- AUTH-04 MFA（D2）：TOTP 注册/登录二次验证/强制策略另立简报。
- 学生由校管重置：当前 `users` 中尚无学生/教师（M2 才创建），且 M1-E 才落地权限矩阵；本简报建议延后到 M2（见 §3.6），登记 TD，不假装 D1 已完成该子项。
- 基于 IP/设备的通用登录限流：D1 只做账号锁定 + 重置邮件冷却；IP 级限流仍归 TD-116/M1-D 后续或 M6。
- 密码泄露库比对（HIBP）、密码强度可视化等增强项：不在 spec 内。

## 2. 数据、配置与契约

**迁移**（down_revision = `d4e6f8a0b2c1`，revision 实现时生成）

- `users` / `platform_users` 增加：
  - `email varchar(254) NULL`（重置邮件收件地址；校管先由 `login_id` 回填，教师/学生由 M2 写入）。
  - `failed_login_attempts integer NOT NULL DEFAULT 0`
  - `failed_login_window_started_at timestamptz NULL`
  - `locked_until timestamptz NULL`
- 新表 `password_history`（租户）与 `platform_password_history`：
  - `id uuid pk`、`tenant_id`（仅租户表）、`user_id`、`password_hash`、`created_at`；
  - 复合外键 `(user_id, tenant_id) → users(id, tenant_id)`，禁止跨租嫁接；
  - 每个用户只保留最近 5 条历史，插入时裁剪。
- 新表 `password_reset_tokens`（租户）与 `platform_password_reset_tokens`：
  - `id`、`tenant_id`（仅租户）、`user_id`、`token_hash`（SHA-256，唯一）、`expires_at`、`used_at`、`created_at`、`request_ip_hash`；
  - 单次使用；新请求作废旧未用令牌；TTL 30 分钟。
- 新租户表全部 `ENABLE/FORCE RLS` + 租户策略 + 授权，并加入 `backend/app/db/selfcheck.py` 的 `REQUIRED_RLS_TABLES`；平台表只授予平台连接。
- 旧数据回填：`school_admin` 的 `email` 取 `login_id`（M1-B 以注册邮箱作为登录名）；新开通学校在 onboarding 插入时同步写 `email`。

**配置**（`.env.example` 只放非密钥默认值）

- `AUTH_MAX_LOGIN_FAILURES=5`
- `AUTH_LOGIN_FAILURE_WINDOW_SECONDS=900`
- `AUTH_LOCKOUT_SECONDS=900`
- `AUTH_PASSWORD_HISTORY_SIZE=5`
- `PASSWORD_RESET_TOKEN_TTL_SECONDS=1800`
- `PASSWORD_RESET_REQUEST_COOLDOWN_SECONDS=60`
- `PASSWORD_RESET_IP_LIMIT=20`（申请接口，按来源 IP 固定窗口）
- `PASSWORD_RESET_CONFIRM_IP_LIMIT=30`（确认接口独立计数）
- `PASSWORD_RESET_IP_WINDOW_SECONDS=900`
- `PASSWORD_RESET_URL_BASE`（邮件链接前缀；开发默认前端地址）
- `TRUSTED_PROXY_IPS`（可信反代地址白名单；仅白名单对端可用 `X-Forwarded-For`）
- 复用现有 `SMTP_*`；测试用 MailHog。

**重置令牌格式**

- 租户：`v1rt.<tenant_id>.<random>`；平台：`v1rp.<random>`。
- 数据库只存 `random` 的 SHA-256；前缀用于确认时选择连接与 realm，不把 secret 写日志/审计。

**接口**

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/auth/password-reset/request` | `realm` + `school_code/login_id`（租户）或 `account`（平台）；恒返回 202，不暴露账号是否存在 |
| POST | `/api/v1/auth/password-reset/confirm` | `token` + `new_password`；成功 204，要求重新登录（含 MFA 用户） |

- `change-password` 沿用现有接口，补策略/历史校验；成功后仍撤销全部家族并返回新令牌。
- 所有失败响应不区分“账号不存在/无邮箱/令牌不存在”，审计记录真实原因。

## 3. 候选方案

### 3.1 范围拆分

- **A. 一次性做完 AUTH-03/04/05**：一个迁移 + 一套测试。优点是只走一次审查；缺点是 MFA 的挑战令牌、加密密钥、门禁与密码/锁定耦合，diff 与回归面过大，违背“单点推进”。
- **B. 拆为 D1（AUTH-03/05）与 D2（AUTH-04）**：D1 先稳定密码与锁定原语，D2 复用失败计数器与登录流程。代价是多一次简报/审查/提交。

### 3.2 bcrypt 72 字节与 spec“最大 128 字符”

- **A. 保留 bcrypt cost 12，按 72 UTF-8 字节拦截**：与现有哈希兼容、无迁移风险；代价是 spec 的 128 字符上限无法对 ASCII 完整成立，需要把 spec 改成“最大 128 字符且不超过 72 UTF-8 字节（bcrypt 限制）”。
- **B. 切换 Argon2id**：支持任意长度、现代推荐；代价是偏离 spec 的 bcrypt 决策，需要双算法验证 + 登录时惰性重哈希，迁移与审查面显著扩大。
- **C. SHA-256 预哈希 + bcrypt**：保留 bcrypt cost 12 且支持长口令；代价是自定义哈希格式、旧哈希兼容与额外安全论证，复杂度接近 B。

### 3.3 锁定状态存储

- **A. `users`/`platform_users` 行内字段 + 现有每用户 advisory lock**：登录已按用户串行化，计数与锁定更新原子、无额外表；代价是两张用户表加列，历史尝试只能靠审计追溯。
- **B. 独立 `login_attempts` 表**：可保留每次尝试；代价是每次登录多写一张表、窗口聚合查询与清理，且仍需用户锁防并发。

### 3.4 重置令牌

- **A. 数据库存哈希、单次使用、按 realm 分表**：可撤销、可审计、天然支持“新请求作废旧令牌”；代价是新表 + RLS + 清理。
- **B. 无状态签名 JWT**：实现轻；但无法可靠单次使用/提前作废（除非再加 Redis），令牌泄露窗口大。

### 3.5 重置请求的账号标识

- **A. 租户填“学校编号 + 学号/工号”，平台填账号；系统发往库内 `email`**：不要求用户记住邮箱，学生也无邮箱；需新增 `email` 列并为校管回填。
- **B. 用户直接填邮箱**：实现简单；但学生无邮箱、校管登录名虽为邮箱也不应强制用户记住，且更容易被用于探测邮箱是否存在。

### 3.6 学生由校管重置

- **A. D1 立即实现**：需要新增/借用权限码并给校方管理员授权；但 M2 才有学生、M1-E 才定权限矩阵，当前只能造测试数据，容易与 M1-E 返工。
- **B. D1 只实现员工/平台邮件重置；校管重置学生延后到 M2（依赖 M1-E）并登记 TD**：符合依赖顺序，但 AUTH-03 的该子项在 M2 才闭环。

### 3.7 重置确认后的响应

- **A. 204，不回令牌，要求重新登录**：避免绕过 MFA、避免与设备上限/会话策略重复；UX 多一步登录。
- **B. 直接签发令牌**：少一步；但重置后立即建立会话会绕过 D2 的 MFA 二次验证，且要重复登录的设备上限/审计逻辑。

## 4. 选择结论

- **范围**：选 B，拆 D1/D2；D1 先交付密码与锁定，D2 再做 MFA。
- **哈希**：选 A，保留 bcrypt cost 12 + 72 字节硬上限；**需要你确认把 spec 的“最大 128”改为“最大 128 字符且 ≤72 UTF-8 字节”**。TD-107 处置改为“已评估：D1 保留 bcrypt 上限，Argon2id 留待后续统一迁移”。
- **锁定**：选 A，行内字段 + advisory lock；登录失败在同一事务内更新计数，提交后再写独立失败审计。
- **重置令牌**：选 A，数据库哈希 + 单次使用 + 按 realm 分表。
- **请求标识**：选 A，学校编号 + 登录名 / 平台账号，发往库内邮箱；恒 202。
- **校管重置学生**：选 B，延后到 M2（依赖 M1-E），登记 TD；D1 不宣称 AUTH-03 该子项完成。
- **确认响应**：选 A，204 + 重新登录。
- **锁定提示**：客户端统一收到现有“学校编号、账号或密码不正确”（防止账号枚举）；审计记录 `locked` 真实原因。是否需要更明确的“已锁定”提示，需你确认。

**需要改 spec/design**

- spec：bcrypt 72 字节上限的明确表述；重置接口的请求/响应与“恒 202、确认后 204”；锁定期间统一错误文案。
- design：§5.2 补重置接口契约；§9 补失败计数/锁定字段与重置令牌表；M1-D 拆为 D1/D2。
- milestone/handoff：D1 完成后只标记 AUTH-03（除校管重置学生）/05；MFA 与校管重置保持未完成。

## 5. 实现顺序与 TDD 清单

**数据 → 服务 → 接口 → 界面**

1. 迁移：列/表/索引/RLS/回填；更新 RLS 覆盖门禁与迁移回滚测试。
2. `passwords.py`：`validate_password_strength`、`is_password_reused`、初始密码与登录名冲突重生成。
3. `auth/lockout.py`（或 service 内聚）：失败窗口、阈值、锁定、过期清理、成功清零。
4. 登录接入：租户/平台登录在用户锁内读取并更新计数；失败提交后审计；锁定期间仍做 bcrypt 等时校验。
5. 改密接入：策略/历史校验、写历史、保持现有撤销全部家族与 token_version 自增。
6. 重置：请求（恒 202、冷却、邮件、审计）→ 确认（令牌校验、用户锁、策略/历史、改密、撤销、清锁、单次消费）。
7. onboarding：校管创建时写 `email`；旧数据迁移回填。
8. 前端：admin/campus 重置申请页与确认页（最小表单、错误文案、成功后跳登录）；不泄露账号是否存在。
9. 文档：spec/design/handoff/README/tech-debt/review-log。

**红→绿→重构测试**

- [ ] 单元：长度/字符类/含登录名/72 字节/历史 5 次/初始密码合规；失败窗口与锁定状态机；重置令牌哈希、过期、单次使用。
- [ ] 集成：迁移升降级与 RLS；5 次失败锁定、正确密码仍拒绝、过期解锁；并发失败不丢计数；改密历史与撤销；重置请求/确认全链路；跨租令牌不可用；邮件失败审计且不泄露令牌。
- [ ] 业务场景：BS-03 扩展（锁定 + 审计）、密码重置端到端（MailHog 取信）、弱密码/历史复用拒绝、旧密码失效、全部设备下线。
- [ ] E2E：admin/campus 重置页与登录页联动；锁定提示统一。
- [ ] 评估/性能：不适用；bcrypt cost 12 保持现状，记录登录延迟基线。

## 6. 测试策略

| 层级 | 内容 |
|---|---|
| 单元 | 策略校验纯函数、历史比对、锁定时间计算、令牌格式与哈希 |
| 集成 | 真实 PG/Redis/MailHog；迁移/RLS、并发登录失败、改密/重置事务、审计真值 |
| 业务场景 | 从“连续输错→锁定→解锁→重置→重新登录”跑完整链路，断言 DB/Redis/审计/邮件 |
| E2E | Playwright：重置申请/确认、弱密码提示、成功后旧会话失效 |
| AI 评估 | 不适用 |
| 性能 | 记录 bcrypt cost 12 下登录/改密 P95 基线；不做门槛 |

## 7. 预判技术债（实现后登记）

| 候选条目 | 处置 |
|---|---|
| TD-107 bcrypt 72 字节 vs 128 字符 | D1 评估后保留 bcrypt 上限；更新为“已评估/已延后”，Argon2id 统一迁移另立 |
| TD-116 bcrypt/标记在用户锁内 | D1 增加历史校验后锁内 bcrypt 次数更多；需更新风险描述，登录限流仍未做 |
| 重置邮件 IP 级限流/防轰炸 | D1 仅做单账号 60s 冷却；IP 级限流登记新 TD（目标 M6 或安全加固） |
| 重置令牌/历史过期清理 | 新 TD：定时清理任务（可并入 TD-067 令牌清理任务） |
| 学生校管重置 | 新 TD：目标 M2（依赖 M1-E 权限矩阵） |
| 同步 SMTP 在请求内发送 | 邮件超时 10s；失败审计；异步队列登记 TD（低优先级） |

## 8. 高风险确认（需你拍板）

1. 是否同意拆 D1/D2，先做 D1（AUTH-03/05），MFA 另立简报？
2. 是否同意保留 bcrypt cost 12 并接受 **72 UTF-8 字节** 实际上限，同时修改 spec 的“最大 128”表述？若坚持 128 字符，需要选 Argon2id 或 SHA-256 预哈希，工期与风险会上升。
3. 是否同意“学生由校管重置”延后到 M2（依赖 M1-E），D1 先交付员工/平台邮件重置？
4. 是否同意锁定期间统一返回“账号或密码不正确”，不向未认证方暴露“已锁定”？
5. 是否同意重置确认返回 204、要求重新登录（MFA 用户不会被绕过）？
6. 是否把 admin/campus 重置页面纳入 D1；若希望后端先行，可拆出 D1-UI。

确认结论（2026-10-22）：

1. 拆 D1/D2，先交付 D1（AUTH-03/05）。
2. 保留 bcrypt cost 12 + 72 UTF-8 字节上限；同步修改 spec 表述。
3. 学生校管重置延后到 M2（依赖 M1-E），D1 登记 TD。
4. D1 包含 admin/campus 最小重置页面。

其余按本简报推荐执行：重置确认 204 并要求重新登录、重置申请恒 202、令牌 30 分钟单次使用、锁定期间统一凭证错误文案。

## 9. 审查对照（预登记）

- 重点验证：账号枚举（文案/时序/恒 202）、锁定并发与提交语义、重置令牌单次使用与跨租隔离、历史哈希不泄露、邮件正文/日志/审计无令牌或明文密码、RLS 覆盖、迁移可回滚、失败路径审计不随回滚丢失。
- 允许的偏差：无；若实现中发现新取舍，先更新本简报并登记 TD。

## 10. 第一轮独立审查与修复（2026-10-22）

第一轮结论：**不通过**（1 Blocker + 5 Major + 2 Minor）。修复与决策变更：

| 审查问题 | 修复 |
|---|---|
| Blocker：downgrade 先恢复旧 `revoked_reason` CHECK，存量 `password_reset` 行导致回滚失败 | 回滚前先把两表 `password_reset` 归一化为 `password_change`；新增临时库升降级回归用例 |
| Major：学生自助重置与“延后到 M2”的确认结论矛盾 | 租户申请命中 `user_type='student'` 时不发信/不建令牌，审计 `student_reset_deferred`；补用例 |
| Major：改密后未使用的重置链接仍可改密 | 改密事务内同时作废该用户未使用重置令牌；补“改密→旧链接失效”用例 |
| Major：同步 SMTP 可用响应耗时枚举账号；无 IP 限流；伪造 tenant_id 污染租户审计 | 申请接口改用 `BackgroundTasks`（202 先返回）；新增 Redis 固定窗口 IP 限流（默认 20 次/15 分钟，fail-open；第四轮起超限不再写审计）；失败审计仅在令牌确实存在于该租户时写租户审计，否则写平台审计并带 `attempted_tenant_id` |
| Major：重置令牌放 query string，会进代理日志/Referer | 邮件链接改为 `#token=` fragment；前端从 `route.hash` 读取；两个入口加 `<meta name="referrer" content="no-referrer">` |
| Major：design/TD/handoff/README 未回填 | 已补数据模型、D1 落地说明、TD 处置与交接状态 |
| Minor：单测恒真断言；缺跨租/并发/422 用例 | 改为真实 secret 断言；补跨租、跨 realm 语义、并发单次消费、改密失效、学生延后、IP 限流、422 不回显、迁移回滚用例 |
| Minor：客户端只校验长度；FastAPI 422 回显 `input` | api-client 增加与后端同口径 `passwordPolicyError` 并在改密/重置页使用；全局 422 处理器仅返回 `loc/msg/type` |

与 §7 预登记的偏差：IP 级限流由“登记 TD”改为 **D1 内实现**（审查要求；固定窗口/fail-open 的限制登记 TD-121）；同步 SMTP 改为“响应后进程内 BackgroundTasks”，持久化队列/重试仍登记 TD-119。

## 11. 第二轮独立审查与修复（2026-10-22）

第二轮结论：**不通过**（0 Blocker + 7 Major）。Major 与修复：

| 审查问题 | 修复 |
|---|---|
| 文档门禁矛盾：提前写“两轮通过”、计数过期 | 统一改为“复审待执行”，计数以实际门禁为准，最终复审后再回填 |
| 平台账号无邮箱，重置闭环缺失 | 新增 `python -m app.platform_admin`（创建账号/维护邮箱，审计 `platform.user.*`，初始密码只打印一次），并补平台邮件重置全链路用例 |
| 重置邮件在事务提交前发送：SMTP 失败会发信但令牌回滚 | 改为“令牌先提交、成功后才发信”；投递失败独立事务作废已提交令牌并写 `email_delivery_failed` |
| 改密接口可绕过失败锁定 | 改密校验旧密码失败时提交失败计数并检查锁定；成功改密清零；补 5 次失败→锁定→正确旧密码仍拒绝用例 |
| 并发登录失败测试缺口 | 新增 6 并发错误登录：恰 5 次 `invalid_password` + 1 次 `locked`，DB 计数与锁定终态一致 |
| 前端策略与后端 Unicode 口径不一致 | api-client 按码点计长度、UTF-8 字节计上限，中文按 alnum 处理；后续补齐二元属性/上下标数字 |
| C 端学生入口死路 | 先提示联系管理员，第三轮进一步改为“后续版本开放”，不指向未交付能力 |

## 12. 第三轮独立审查与修复（2026-10-22）

第三轮结论：**不通过**（1 Blocker + 3 Major + 3 Minor）。Blocker 为文档仍提前写“通过”与计数过期；Major 为确认接口无 IP 限流（伪造令牌可放大平台审计）、租户校管邮箱触发器无测试、学生提示依赖未交付的管理员重置。修复见 `docs/review-log.md` 第三轮条目；新增 TD-123（按 realm 分流重置链接）。

## 13. 第四轮独立审查与修复（2026-10-22）

第四轮结论：**不通过**（0 Blocker + 2 Major + 5 Minor）。修复：

| 审查问题 | 修复 |
|---|---|
| Major：反代后所有请求同源，按 IP 限流退化为全局限流；直接信任 XFF 又可被伪造绕过 | 新增 `app/api/client_ip.py::resolve_client_ip`：仅直连对端命中 `TRUSTED_PROXY_IPS` 时读取 `X-Forwarded-For`，且取首个合法 IP；Nginx 必须覆盖写该头；补 6 条单元测试 |
| Major：申请接口超限仍逐次写平台审计，未认证请求可放大审计写入 | 超限静默返回（仍 202），与确认接口一致；测试断言只有前两次成功发送写 `sent`，无 `ip_rate_limited` 事件 |
| Minor：确认限流的“不写审计”断言用畸形令牌，恒真且随机 IP 可能碰撞 | 改用合法格式的未知租户令牌、显式计数失败审计（前 2 次各 1 条，第 3 次 429 后不增长）；新增 `revocation.clear_rate_limit` 清理计数，IP 取 198.18/15 文档网段 |
| Minor：429 无 `Retry-After` | 统一错误映射对 429 返回窗口秒数 |
| Minor：平台账号固定 12 位初始密码无法覆盖 13+ 字符账号 | 初始密码长度改为 `max(12, len(account)+4)`；既有平台 CLI 用例改用 28 字符账号 |
| Minor：简报配置清单缺限流变量 | 已补 4 个重置/IP 变量与 `TRUSTED_PROXY_IPS` |

## 14. 第五轮复审重点

- 代理：白名单外伪造 XFF 被忽略；白名单内取首个合法地址；畸形头回退直连地址；Nginx 覆盖写文档一致。
- 限流审计：申请/确认超限均不新增审计；429 带 `Retry-After`；合法格式令牌的失败审计计数确定。
- 平台 CLI：长账号可创建且初始密码不含账号；改邮箱作废旧令牌。
- 其余沿用 §13 清单：迁移往返、跨租审计归属、改密/改邮箱后旧链接失效、并发单次消费、422 脱敏、文档计数一致。

## 15. 第五轮独立审查（2026-10-22）——**通过**

- 结论：**通过**（0 Blocker / 0 Major / 3 Minor）。代理 IP 信任模型、申请/确认超限不写审计、429 `Retry-After`、长账号 CLI、迁移往返、令牌生命周期、锁定并发与文档计数均实测成立。
- Minor 收口：删除未使用的 bcrypt 长度辅助函数；spec/design 初始密码表述改为“默认 12 位、平台账号按账号长度加长”；`Retry-After` 精确剩余 TTL 记入 TD-121（M6 网关限流统一处理）。
- 最终门禁：后端 348 passed、ruff/mypy/alembic 全绿；前端 25 单测 + 22 E2E、lint/format/typecheck/build 全绿。
- 远端 CI：backend/frontend/security 均绿；security 首轮 bandit 将密码表 SQL 误报 B105，精确注解后通过。
