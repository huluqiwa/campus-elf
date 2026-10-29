# M1-D2 — AUTH-04 MFA（TOTP）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-22 |
| 状态 | 已确认（2026-10-22） |
| 关联功能点 | M1-D2 / AUTH-04（`docs/milestone.md` §4.2） |
| 验收引用 | `docs/spec.md` §3.3：`Given MFA 开启，When 登录，Then 需要 TOTP；错误验证码计入失败次数`；AUTH-05 锁定；`docs/design.md` §5 预留的 `/auth/mfa/*` |

## 0. 大白话说明

- **MFA**：登录时除了密码，再要一次手机验证器 App 上显示的 6 位动态码；密码泄露时多一道锁。
- **TOTP**：这种 6 位动态码的标准算法，Google Authenticator、Microsoft Authenticator 等都支持。
- **恢复码**：手机丢了、验证器没了时，用来登录的 10 个一次性备用码；每个只能用一次。
- **本期做**：用户先绑定验证器（扫二维码/输密钥），之后登录先输密码、再输动态码；平台运营账号可以设为"必须绑定才能登录"；学校老师/学生可选绑定。

## 1. 目标与不解决

**目标**：为租户用户与平台账号提供可选 TOTP MFA；生产可通过配置对**平台 realm** 强制 MFA；登录在密码校验通过后进入短期挑战，验证 TOTP 或恢复码后才签发 access/refresh；启用/关闭 MFA 需重新认证并轮换全部会话；TOTP 密钥加密落库；错误验证码计入 AUTH-05 失败计数；全链路审计且不记录密钥、验证码、恢复码。

**不解决**（明确边界）：

- 不做 WebAuthn/Passkey、短信/邮件动态码；不做"记住此设备"；不做租户角色强制 MFA（依赖 M1-E 权限矩阵与校管重置能力）。
- 不做 MFA 密钥轮换/批量重加密工具；不做租户校管的 MFA 重置后台（平台账号提供 CLI 破窗）。
- MFA 只作为**登录时**的第二因素，不改变业务接口的权限模型；不把 MFA 状态作为 RBAC 条件。
- 不把 TOTP 密钥、验证码、恢复码写入日志、审计、Langfuse 或错误响应。

**验收**（本功能点自验）：

1. MFA 已开启的账号，密码正确时不直接返回令牌，而返回短期 MFA 挑战；正确 TOTP 后才返回令牌并建立会话。
2. 错误 TOTP 返回统一凭证错误，并计入失败次数；达到阈值后按 AUTH-05 锁定，正确密码/TOTP 在锁定期内仍拒绝。
3. 同一 TOTP 时间步不能重放；同一挑战不能在成功后再次兑换。
4. 生产开启 `MFA_REQUIRE_PLATFORM` 后，未绑定的平台账号登录只能进入"强制绑定"流程；未完成绑定时不能访问任何业务 API。
5. 启用/关闭 MFA 会自增 `token_version` 并撤销该账号全部刷新令牌；当前流程返回新令牌，其他设备需重新登录。
6. 恢复码一次性使用；启用时只明文展示一次，库内只存 HMAC。
7. 数据库中的 `mfa_secret_enc` 不是明文；跨用户/跨租户搬移密文无法解密。
8. 租户隔离、RLS、审计、迁移升降级、前端 MFA 登录/绑定页面均有测试。

## 2. 现状核对（2026-10-22）

- `users`、`platform_users` 已有 `mfa_secret_enc`、`mfa_enabled` 字段（迁移 `779f731db342`），但没有 TOTP 逻辑、没有 `mfa_last_used_step`、没有恢复码表。
- `backend/pyproject.toml` 已声明 `pyotp>=2.9,<3`、`cryptography>=49,<51`，当前未被认证代码使用。
- `.env.example` 有 `ENCRYPTION_KEK=change-me` 占位，但 `app/config.py` 未读取；没有任何字段加密工具。
- 登录入口：`backend/app/auth/service.py` 的 `login_tenant`（约 681 行）与 `login_platform`（约 875 行）；成功路径直接清失败计数、执行会话上限、签发 `TokenPair`；密码正确即返回令牌。
- `_issue_tokens`（约 442 行）签发 access JWT（含 `sid`/`token_version`/`must_change_password`）和 refresh；`refresh_tokens`/`platform_refresh_tokens` 存哈希与家族。
- 首登改密门禁在 `backend/app/auth/principal.py`；登录/刷新/撤销/改密共享用户级 advisory lock。
- Redis 已用于会话标记与限流；`redis>=7.4` 支持 `GETDEL`。
- 租户新表的 RLS 模板见 `b2d4f6a8c0e1_m1d1_password_lockout.py`：`ENABLE/FORCE ROW LEVEL SECURITY` + `tenant_isolation` 策略 + 最小授权。
- 平台账号生命周期脚本为 `backend/app/platform_admin.py`，可扩展 `--reset-mfa`。

## 3. 候选方案

### 3.1 第二因素选型

| 方案 | 做法 | 优点 | 缺点/风险 |
|---|---|---|---|
| A. TOTP（推荐） | `pyotp`，30s/6 位/SHA1，验证窗口 ±1 步 | 标准、离线、无短信成本、Authenticator 普及；符合 spec "TOTP" | 依赖用户保管密钥；无抗钓鱼能力 |
| B. 邮件/短信 OTP | 复用 SMTP/短信发送一次性码 | 无 App 门槛 | 邮件即密码重置通道，第二因素强度弱；短信成本/合规；投递失败影响登录 |
| C. WebAuthn/Passkey | 浏览器公钥凭证 | 抗钓鱼、体验好 | 前端/设备/证明链复杂，超出 M1 范围 |

**结论**：A。B 登记为拒绝项（TD-130）；C 延后（TD-124）。

### 3.2 密钥落库方案

| 方案 | 做法 | 优点 | 缺点/风险 |
|---|---|---|---|
| A. AES-256-GCM + KEK（推荐） | `ENCRYPTION_KEK`（32 字节随机）加密 TOTP secret；AAD 绑定 realm+user_id；密文格式 `v1.<nonce>.<ct>` | 认证加密、可绑定上下文、密钥不入库 | 需新增必填密钥与轮换工具；KEK 丢失则所有 MFA 不可用 |
| B. Fernet | `cryptography.fernet` | 简单、带时间戳 | 无 AAD，无法绑定用户；格式冗余 |
| C. PostgreSQL pgcrypto | DB 内 `pgp_sym_encrypt` | 应用无密钥 | 密钥/明文进入 SQL、日志风险更高；RLS 之外再受 DB 权限约束 |

**结论**：A；B/C 不作为后续方案（TD-131 记录拒绝理由）。KEK 轮换延后（TD-125）。

### 3.3 登录挑战方式

| 方案 | 做法 | 优点 | 缺点/风险 |
|---|---|---|---|
| A. 短期 JWT + Redis 一次性挑战（推荐） | 密码通过后签发 `aud=campus-elf-mfa`、`purpose=mfa_challenge` 的 JWT；Redis 记录 jti/尝试次数；验证成功后 `GETDEL` | 无需重传密码；签名/受众隔离；单次兑换；可限次 | 多一个 Redis 依赖；挑战令牌本身是 bearer 凭证（短 TTL、内存保存） |
| B. 不透明随机挑战串存 Redis | 随机串为 key，服务端查状态 | 可随时撤销、无 JWT 解析 | 与 A 复杂度相近，无法携带 realm/token_version 等签名声明 |
| C. 直接签发完整令牌并加 `mfa_pending` claim | 业务接口统一拦截 | 改动少 | 令牌已存在，任何漏检接口都绕过 MFA；否决 |
| D. 登录请求带可选 `totp_code`，缺码返回 401 后重提密码+码 | 无挑战态 | 实现最少 | 前端需保存密码并二次提交；密码校验两次；UX/安全都差 |

**结论**：A。挑战 JWT 只用于 MFA 兑换，不能访问业务接口；Redis 不可用时 fail-closed（503）。

### 3.4 强制绑定的临时凭证

| 方案 | 做法 | 优点 | 缺点/风险 |
|---|---|---|---|
| A. 受限 `mfa_enroll` JWT（推荐） | 密码通过且策略要求但未绑定时签发 10 分钟 JWT；仅 `/mfa/enroll`、`/mfa/activate` 接受；激活后签发完整令牌 | 不提前发业务令牌；边界清晰 | 多一个令牌类型与依赖 |
| B. 完整 access token + `mfa_enrollment_required` 门禁 | 复用首登改密门禁 | 复用现有机制 | 业务令牌已存在，漏检即绕过；否决 |
| C. 先回登录页，绑定后再登录 | 无临时凭证 | 最简单 | 用户需重复输入密码；绑定中断率高 |

**结论**：A；`mfa_enroll` 与 `mfa_challenge` 使用同一独立 audience，`purpose` 区分。

### 3.5 恢复码

| 方案 | 做法 | 优点 | 缺点/风险 |
|---|---|---|---|
| A. 10 个一次性恢复码（推荐） | 激活时生成并只展示一次；HMAC-SHA256（KEK 派生 subkey）落库；登录时可替代 TOTP | 平台强制 MFA 的破窗；换手机可自救 | 用户需妥善保存；全部用尽需管理员/CLI 重置 |
| B. 不做恢复码，仅管理员重置 | 减少本期范围 | 实现少 | 平台强制后一旦丢设备只能运维介入；体验差 |
| C. 仅 TOTP + 平台 CLI 重置 | 与 B 类似但提供 CLI | 可运维 | 租户用户无破窗；不满足可选 MFA 的自助场景 |

**结论**：A + 平台 CLI 重置；恢复码再生成延后（TD-129）。

### 3.6 MFA 变更后的会话处理

| 方案 | 做法 | 优点 | 缺点/风险 |
|---|---|---|---|
| A. 自增 `token_version` + 撤销全部 refresh + 当前流程重签（推荐） | 与改密一致；旧 access 立即失效，旧 refresh 全部撤销 | 安全边界清晰；其他设备强制重新认证 | 当前设备需要替换令牌；实现需复用改密事务模式 |
| B. 保留现有会话 | 仅写审计 | 改动少 | 被盗会话可在启用 MFA 后继续使用；否决 |

**结论**：A。revoked_reason 新增 `mfa_change`；当前流程返回新令牌。

### 3.7 强制范围

| 方案 | 做法 | 优点 | 缺点/风险 |
|---|---|---|---|
| A. 仅平台 realm 可强制（推荐） | `MFA_REQUIRE_PLATFORM` 默认 false；生产按需开启；租户用户始终可选 | 与 spec "SaaS 员工/超管"一致；不依赖未交付的校管重置 | 租户管理员无法被强制 |
| B. 平台 + 租户 `school_admin` 强制 | 增加 `MFA_REQUIRE_TENANT_ADMIN` | 覆盖更广 | 依赖 M1-E 权限矩阵/校管重置；否则丢设备即锁死 |
| C. 所有用户强制 | 全局开关 | 最强 | 学生换机成本高，超出 spec |

**结论**：A；B 延后 M2（TD-126）。

## 4. 选择结论

- TOTP + AES-256-GCM(KEK, AAD) + 短期挑战 JWT/Redis + 受限绑定 JWT + 10 个恢复码 + 会话轮换。
- 强制范围仅平台 realm，配置 `MFA_REQUIRE_PLATFORM` 默认 false，生产部署文档建议开启；租户用户可自助启用（管理端 UI），学生绑定 UI 延后。
- 需要回写 `docs/spec.md`、`docs/design.md`、`docs/deployment.md`、`.env.example`：补充挑战/绑定/恢复码/会话轮换/密钥管理细节；不改变既有 spec 结论。

## 5. 数据、配置与契约

### 5.1 数据模型

| 表 | 变更 | 说明 |
|---|---|---|
| `users` | `mfa_last_used_step BIGINT NULL`；CHECK `NOT mfa_enabled OR mfa_secret_enc IS NOT NULL`；CHECK `mfa_last_used_step >= 0` | 防 TOTP 重放；待激活 secret 允许存在但 `mfa_enabled=false` |
| `platform_users` | 同上 | 平台 realm |
| `refresh_tokens` | `mfa_verified BOOLEAN NOT NULL DEFAULT false`；CHECK 增加 `mfa_change` 撤销原因 | 刷新时校验强制策略；旧会话默认 false |
| `platform_refresh_tokens` | 同上 | 平台 realm |
| `mfa_recovery_codes`（新，租户） | `id, tenant_id, user_id, code_hash, used_at, created_at`；唯一 `(tenant_id,user_id,code_hash)`；复合 FK `(user_id,tenant_id)->users(id,tenant_id) ON DELETE CASCADE`；RLS + 最小授权 | 租户隔离；只存 HMAC |
| `platform_mfa_recovery_codes`（新，平台） | `id, platform_user_id, code_hash, used_at, created_at`；唯一 `(platform_user_id,code_hash)`；FK CASCADE；仅平台连接授权 | 平台表无 RLS |

迁移：先校验存量 `mfa_enabled=true AND mfa_secret_enc IS NULL`，存在则报错中止迁移（实现为 Python 层 `RuntimeError`，不静默关 MFA）；再建列/表/约束。downgrade 删除新表与列，并把 `mfa_change` 归一化为 `admin_revoke` 后再恢复旧 CHECK（与 M1-D1 的回滚经验一致）。迁移往返用临时库测试。

### 5.2 配置

| 配置 | 默认 | 说明 |
|---|---|---|
| `ENCRYPTION_KEK` | 必填 | 标准 base64 的 32 字节随机密钥；所有环境校验长度；staging/production 拒绝占位值 |
| `MFA_REQUIRE_PLATFORM` | `false` | 生产可开启；只影响平台 realm |
| `MFA_TOTP_ISSUER` | `CampusCompanionElf` | otpauth issuer |
| `MFA_TOTP_DIGITS` / `MFA_TOTP_INTERVAL` / `MFA_TOTP_WINDOW` | `6` / `30` / `1` | 兼容主流 Authenticator；窗口 ±1 步 |
| `MFA_CHALLENGE_TTL_SECONDS` | `300` | 挑战 JWT/Redis TTL |
| `MFA_ENROLL_TOKEN_TTL_SECONDS` | `600` | 强制绑定临时凭证 TTL |
| `MFA_CHALLENGE_MAX_ATTEMPTS` | `5` | 单次挑战最多尝试数；失败仍计入账号锁定 |
| `MFA_RECOVERY_CODE_COUNT` | `10` | 激活时生成数量 |
| `JWT_AUDIENCE_MFA` | `campus-elf-mfa` | 挑战/绑定令牌独立 audience |

`ENCRYPTION_KEK` 生成：`openssl rand -base64 32`。实现时把本地 `.env` 补上随机值（不提交）；`.env.example` 只保留注释与生成命令。

### 5.3 加密与恢复码

- TOTP secret：`pyotp.random_base32(length=32)`；`AESGCM(KEK).encrypt(nonce, secret, aad)`；AAD = `mfa:{realm}:{user_id}`；密文 `v1.<b64url(nonce)>.<b64url(ct)>`。
- 解密失败视为密钥/数据损坏：fail-closed 500，审计 `auth.mfa.secret_invalid`，不返回 secret。
- 恢复码：10 个 `XXXXX-XXXXX`（Base32，去 `0/1` 混淆），只展示一次；`code_hash = HMAC-SHA256(HKDF(KEK, info="mfa-recovery-codes-v1"), f"{realm}:{user_id}:{normalized}")`；查询/消费均带用户作用域，消费用 `UPDATE ... WHERE used_at IS NULL RETURNING id` 保证单次。
- TOTP 重放：验证匹配到 step 后，在用户 advisory lock 内更新 `mfa_last_used_step`，要求 `step > last_used_step`；激活时也写入当前 step。
- 不记录：secret、otpauth URI、TOTP 码、恢复码、挑战/绑定令牌。

### 5.4 接口契约

| 方法 | 路径 | 输入 | 输出 |
|---|---|---|---|
| POST | `/api/v1/auth/login` | 同现有 | 200：完整令牌（未开 MFA）或 `{mfa_required:true, mfa_token, expires_in, methods:["totp"]}` 或 `{mfa_enrollment_required:true, enroll_token, expires_in}`；MFA 分支**不设置 refresh Cookie** |
| POST | `/api/v1/auth/mfa/verify` | `{mfa_token, code}` | 200 TokenResponse + refresh Cookie；401 统一凭证错误；挑战过期/已用 401 |
| POST | `/api/v1/auth/mfa/enroll` | Bearer access token + `{current_password}`；或 Bearer enroll token + `{}` | 200 `{secret, otpauth_uri, expires_in}`；已启用 409 |
| POST | `/api/v1/auth/mfa/activate` | Bearer access/enroll token + `{code}` | 200 TokenResponse + `recovery_codes: string[]` + Cookie；错误码 400/401；错误计入锁定 |
| POST | `/api/v1/auth/mfa/disable` | Bearer access token + `{current_password, code}` | 200 TokenResponse + Cookie；策略强制时 403；错误 401 |
| GET | `/api/v1/auth/me` | — | 增加 `mfa_enabled`、`mfa_required`（平台策略） |

- `mfa_token`/`enroll_token` 均为 HS256 JWT：`aud=campus-elf-mfa`，`purpose=mfa_challenge|mfa_enroll`，含 `sub/realm/tenant_id/jti/token_version`；access token 解码器按 realm audience 校验，天然拒绝 MFA audience。
- access token 增加标准 `amr` claim：普通密码登录 `["pwd"]`，完成 MFA 后 `["pwd","totp"]`（恢复码同为 `totp`）；refresh 行同步存 `mfa_verified`。旧令牌无 `amr` 视为 `false`，便于策略上线后的刷新校验。
- 登录成功后才设置 refresh Cookie；`mfa_token` 不写 Cookie/localStorage，前端只放内存。
- `code` 字段最大 32；TOTP 6 位数字，恢复码格式自动识别；422 全局处理器不回声输入。
- 所有 MFA 失败对外使用统一凭证文案（`INVALID_CREDENTIALS`）；挑战/绑定令牌无效使用"登录状态已失效，请重新登录"，不泄露账号状态。

### 5.5 登录状态机

1. 密码校验（现有逻辑）；租户/平台均适用。
2. 读取 `mfa_enabled`（登录 SQL 增加该列）。必要 = 已开启，或平台 realm 且 `MFA_REQUIRE_PLATFORM`。
3. **不必要**：走原路径（清失败计数 → 会话上限 → 签发令牌 → 审计 success）。
4. **必要且已绑定**：签发 MFA challenge（Redis 记录 jti/尝试次数），**不清失败计数、不做会话上限、不写 success 审计**。
5. **必要且未绑定**：签发 `mfa_enroll` 受限令牌，同上不签发会话。
6. `/mfa/verify`：校验 JWT/Redis → 加用户锁 → 重读状态/version/锁定/MFA secret → 验证 TOTP 或恢复码 → 失败则 `_apply_login_failure` + 审计（reason `invalid_mfa_code`）→ 成功则清失败、会话上限、签发令牌（`mfa_verified=true`）、审计 `auth.login.success`（`mfa=true`）。
7. `/mfa/activate`：校验绑定令牌/access token → 加锁 → 验证 pending secret → 写 `mfa_enabled=true`/`mfa_last_used_step` → 生成恢复码 → 自增 version、撤销全部 refresh（`mfa_change`）、签发新令牌 → 审计 `auth.mfa.activated`。
8. `/mfa/disable`：校验密码+TOTP/恢复码 → 策略强制则 403 → 清 secret/恢复码、`mfa_enabled=false` → 自增 version、撤销全部 refresh、签发新令牌 → 审计 `auth.mfa.disabled`。
9. 首登改密顺序：密码 → MFA/绑定 → 签发令牌（携带 `must_change_password`）→ 现有改密门禁。MFA 变更与改密都会轮换会话，互不绕过；改密重签时保留当前会话的 `amr`/`mfa_verified`，避免在策略上线窗口内被降级为仅密码会话。
10. 密码重置**不关闭 MFA**：重置后仍需 TOTP；重置只撤销会话与重置令牌。
11. 刷新：`refresh_tokens.mfa_verified=false` 且平台强制策略开启 → 撤销家族并 401，要求重新登录（覆盖策略上线前签发的会话；access 最长 15 分钟窗口，登记 TD-132）。

### 5.6 审计

| action | 时机 | metadata |
|---|---|---|
| `auth.login.failed` | MFA 错误/锁定 | `reason=invalid_mfa_code|locked`, `failed_attempts`, `lock_applied`, `ip_hash`, `ua_hash` |
| `auth.login.success` | MFA 验证/绑定激活成功 | `mfa=true`, `recovery_code_used?`, `ip_hash`, `ua_hash` |
| `auth.mfa.enrollment_started` | 生成/轮换 pending secret | `method=totp`（不含 secret/URI） |
| `auth.mfa.activated` | 绑定完成 | `recovery_codes_issued=10`, `sessions_revoked=true` |
| `auth.mfa.activation_failed` | 激活码错误 | `reason`, `failed_attempts`, `lock_applied` |
| `auth.mfa.disabled` | 主动关闭 | `sessions_revoked=true` |
| `auth.mfa.recovery_used` | 恢复码成功 | `phase=login|disable`（不含码） |
| `auth.mfa.secret_invalid` | 解密失败 | `reason`（不含密文） |
| `platform.user.mfa_reset` | CLI 破窗 | `account_masked`, `sessions_revoked=true` |

> **TD-231 之后的键名（2026-10-29 收口）**：上表里的 `sessions_revoked=true` 已不是现在的写法——
> 布尔语义改名 `sessions_all_revoked`，整数键 `sessions_revoked` 只放「本次真正断开的活跃会话家族数」。
> 现状键名与口径对照表见 `docs/design.md` §5.12；本表保留 M1-D2 当时的设计原貌。

平台 realm 写平台审计；租户写租户审计并由 RLS 约束。失败审计在事务外独立写，避免随回滚丢失。

### 5.7 前端

- `packages/api-client`：`LoginResult` 判别联合（`tokens|mfaRequired|mfaEnrollmentRequired`）；新增 `verifyMfa`、`enrollMfa`、`enrollMfaWithToken`、`activateMfa`、`disableMfa`；`/me` 映射 `mfaEnabled/mfaRequired`。现有登录调用与测试同步改造。
- admin：登录页增加 TOTP 步骤与强制绑定步骤（secret/otpauth + 6 位码 + 恢复码一次性展示）；新增"安全设置"页（启用/关闭 MFA、恢复码提示）；二维码用本地 `qrcode` 依赖渲染，禁止调用第三方二维码服务。
- campus：登录页增加 TOTP/恢复码步骤（若学生被 API 绑定可登录）；本期不提供学生自助绑定入口。
- 恢复码只存页面内存，展示时要求用户确认已保存；不下载、不写 localStorage。
- E2E：复用现有 mock 结构增加 `mfaRequired`/`mfaEnrollmentRequired` 分支；覆盖登录 TOTP、错误码、强制绑定、安全设置启用/关闭。

### 5.8 平台 CLI

`python -m app.platform_admin --account <账号> --reset-mfa`：清 `mfa_secret_enc`/`mfa_enabled`/`mfa_last_used_step`、删除恢复码、清失败计数/锁定、`token_version+1`、撤销全部平台 refresh（`admin_revoke`）、审计 `platform.user.mfa_reset`；不打印任何密钥。强制策略下用户下次登录重新绑定。`--email` 在 `--reset-mfa` 时改为可选。

## 6. 实现顺序与 TDD 清单

1. **数据/加密**：迁移（列、表、CHECK、RLS、revoked_reason）、模型、RLS 覆盖、AES-GCM 工具、KEK/配置校验；单测 + 迁移往返。
2. **服务层**：TOTP/恢复码工具；登录 SQL 增加 `mfa_enabled`；`_issue_tokens` 增加 `mfa_verified`/`amr`；challenge/enroll token 工具；login 分支；verify/activate/disable/enroll；refresh 策略校验。
3. **接口**：DTO、路由、Cookie 分支、`/me` 字段、错误映射。
4. **CLI**：`--reset-mfa` 与测试。
5. **前端**：api-client → admin/campus 登录 → 安全设置页 → E2E。
6. **文档**：spec/design/deployment/.env.example/tech-debt/handoff/README。

TDD 红→绿：

- [ ] 单元：TOTP 当前/±1 通过、±2 拒绝；step 重放拒绝；AES-GCM 往返/AAD 错位/篡改失败；恢复码归一化、单次消费；KEK 长度/占位校验。
- [ ] 集成：绑定→激活→登录挑战→验证成功；错误码计数/锁定；挑战单次/并发；恢复码使用/复用；跨租/跨 realm；refresh 策略拒绝；会话轮换。
- [ ] 业务场景：强制平台 MFA 全链路（密码→绑定→激活→业务 API→重新登录→TOTP）；密码重置后仍需 MFA；CLI 重置后按策略重新绑定。
- [ ] E2E：admin 登录 TOTP/绑定/安全设置；campus TOTP 步骤。
- [ ] 性能：TOTP 校验单次开销可忽略，不单独跑 k6；记录登录 P95 不显著回退。

## 7. 测试策略

| 层级 | 内容 |
|---|---|
| 单元 | `app/auth/totp.py`、`app/security/encryption.py`、挑战令牌、恢复码、配置校验 |
| 集成（真实 PG/Redis） | 绑定/激活、登录挑战、TOTP 窗口/重放、失败锁定、恢复码、会话轮换、refresh 策略、RLS/跨租、审计、422 脱敏 |
| 业务场景 | 平台强制 MFA 全链路、可选 MFA 租户用户、密码重置不绕过 MFA、CLI 破窗、并发验证 |
| E2E | admin 登录 TOTP + 强制绑定 + 安全设置；campus TOTP |
| AI 评估/性能 | 不涉及；登录性能做一次冒烟对比 |

关键用例（必须断言业务结果，不只 HTTP）：

1. DB 中 `mfa_secret_enc` 非明文、解密等于 enrollment secret；审计/日志无 secret/码。
2. 密码正确 + MFA 开启：响应无 access/refresh、无 Cookie；verify 成功后有会话、refresh 行 `mfa_verified=true`、`amr` 含 `totp`。
3. 5 次错误 TOTP → 第 6 次锁定；DB 计数/`locked_until` 正确；审计 `invalid_mfa_code`/`locked`；正确码在锁定期拒绝。
4. 同一 TOTP step 第二次验证失败；同一 challenge 第二次兑换失败。
5. 恢复码登录成功、`used_at` 置位、复用失败、其余码可用。
6. 启用/关闭 MFA：`token_version` 自增、旧 refresh 全部 `revoked_reason=mfa_change`、旧 access 下一请求 401、当前返回新令牌。
7. 强制策略：未绑定登录只拿到 enroll token；该 token 访问 `/auth/me`/业务 API 401；激活后拿到完整令牌；未激活不能建立会话。
8. 策略上线前的平台 refresh 行 `mfa_verified=false` → 刷新 401 且家族撤销。
9. 跨租：租户 A 的 challenge/enroll token 不能操作租户 B；恢复码跨租不可用；RLS 直接查询被拒。
10. 迁移 downgrade 在存在 `mfa_change` 行时成功；重新 upgrade 成功。
11. `ENCRYPTION_KEK` 缺失/错误长度/占位值在 staging 启动失败；本地测试有固定测试密钥。
12. CLI `--reset-mfa` 清密钥/恢复码/锁定、自增版本、撤销 refresh、审计，且输出不含 secret。

## 8. 预判技术债（实现后登记）

| TD 编号 | 条目 | 处置 | 复查时点 |
|---|---|---|---|
| TD-124 | 未提供 WebAuthn/Passkey 等抗钓鱼第二因素 | 已延后 | M6 |
| TD-125 | MFA secret 的 KEK 轮换/批量重加密工具未实现 | 已延后 | M6 |
| TD-126 | 租户角色强制 MFA 与校管 MFA 重置依赖 M1-E 权限矩阵 | 已延后 | M2 |
| TD-127 | 无"记住此设备"可信 Cookie，每次登录均需 TOTP | 已延后 | M6 |
| TD-128 | MFA 验证无独立 IP/全局限流，依赖账号锁定与单挑战尝试上限 | 已延后 | M6（网关统一限流） |
| TD-129 | 恢复码再生成/查看 API 与 UI 未提供 | 已延后 | M2 |
| TD-130 | 邮件/短信 OTP 作为备选第二因素 | 已拒绝 | —（第二因素强度弱、成本/合规高） |
| TD-131 | Fernet/pgcrypto 等替代密钥落库方案 | 已拒绝 | —（无 AAD/密钥进 DB，均不如 AES-GCM+KEK） |
| TD-132 | 强制 MFA 策略上线时，已签发 access token 最长 15 分钟仍有效；refresh 已拒绝 | 已延后 | M6（策略变更即时踢出） |
| TD-133 | 待激活密钥只存 Redis（TTL、无库内记录），Redis 故障会打断绑定流程 | 已延后 | M6（与 TD-111/102 一并收口） |
| TD-134 | TOTP 重放防护按用户全局单调，同一 30 秒窗口内二次登录失败 | 已延后 | M2（改按挑战作用域记录已用码） |
| TD-135 | 恢复码熵偏低（10 字符 × 32 字母表 ≈ 50 bit），脱离上下文可离线爆破摘要 | 已延后 | M6（随 TD-129 加长并评估 argon2 摘要） |

## 9. 高风险确认（需你拍板）

- 是否属于高风险：**是**（认证、加密、会话撤销、租户隔离）。
- 需确认：
  1. **范围**：本期只做 TOTP + 恢复码；WebAuthn、短信/邮件 OTP、记住设备不做。
  2. **强制范围**：仅平台 realm 可强制（`MFA_REQUIRE_PLATFORM` 默认 false，生产部署建议开启）；租户用户可选，租户强制与校管重置延后 M2。
  3. **会话策略**：启用/关闭 MFA 一律 `token_version+1` + 撤销全部 refresh + 当前流程重签；接受其他设备重新登录。
  4. **密钥**：`ENCRYPTION_KEK` 为必填 32 字节 base64；实现时向本地 `.env` 写入随机值（不提交），`.env.example` 只留生成命令。
  5. **UI 范围**：admin 登录 MFA + 强制绑定 + 安全设置页；campus 仅登录 TOTP 步骤；学生自助绑定延后。
  6. **顺序**：登录顺序为密码 → MFA/绑定 → 首登改密门禁。
- 确认结论（2026-10-22）：范围 = TOTP + 恢复码；强制范围 = 只强制平台运营账号（`MFA_REQUIRE_PLATFORM` 默认 false）；会话 = 启用/关闭一律全部轮换；前端 = admin 绑定 + 双端登录（campus 仅登录 TOTP，学生自助绑定延后）；密钥 = `ENCRYPTION_KEK` 必填，实现时补本地 `.env`（不提交）；登录顺序 = 密码 → MFA/绑定 → 首登改密门禁。开始编码。

## 10. 审查对照（预登记）

- 重点验证：密码正确但未完成 MFA 时不得有任何业务令牌/Cookie；challenge/enroll token 不能当 access token；TOTP 重放/并发/挑战重放；错误码计入锁定且锁定期间不泄露；恢复码只存哈希且单次；KEK/AAD/审计/日志无敏感信息；RLS 与跨租；迁移可回滚；强制策略对 refresh 的覆盖；会话轮换后旧 refresh/access 立即失效；前端不持久化敏感值；文档与 TD 计数一致。
- 允许的偏差：无；实现中发现新取舍，先更新本简报并登记 TD。

## 11. 实现回填（2026-10-22，后端部分）

### 11.1 交付文件

| 类型 | 文件 |
|---|---|
| 迁移 | `backend/alembic/versions/c4d6e8f0a2b3_m1d2_mfa.py`（head：`c4d6e8f0a2b3`）：`mfa_last_used_step`、`refresh_tokens.mfa_verified`、`mfa_recovery_codes` / `platform_mfa_recovery_codes`、CHECK 与 `mfa_change` 撤销原因、RLS + 最小授权；upgrade 前校验存量 `mfa_enabled=true AND mfa_secret_enc IS NULL`（两表），存在则抛 `RuntimeError` 中止迁移 |
| 加密/工具 | `backend/app/security/encryption.py`（AES-256-GCM + HKDF 子密钥、`mfa_aad`、恢复码归一化与 HMAC）、`backend/app/security/totp.py`（`pyotp` 封装、±1 窗口、`current_step`） |
| 令牌 | `backend/app/auth/mfa.py`：`mfa_challenge` / `mfa_enroll` 两类 HS256 短期令牌（独立 `JWT_AUDIENCE_MFA`、`purpose` 校验） |
| 服务 | `backend/app/auth/service.py`：`login_tenant` / `login_platform` 的 MFA 分支、`verify_mfa`、`enroll_mfa`、`activate_mfa`、`disable_mfa`、`_consume_recovery_code`、`_apply_login_failure` 复用、refresh 的强制策略校验、`MfaActor/MfaEnrollment/MfaActivation` |
| Redis | `backend/app/auth/revocation.py`：挑战一次性消费与尝试计数、待激活密钥 `store/get/delete_mfa_pending_secret`（fail-closed） |
| 接口 | `backend/app/api/auth.py`：`/auth/mfa/verify|enroll|activate|disable`、`/me` 的 `mfa_enabled/mfa_required`、`_resolve_mfa_actor`（访问令牌或绑定令牌双入口）、含密钥/恢复码响应的 `Cache-Control: no-store` |
| CLI | `backend/app/platform_admin.py`：`--reset-mfa`（可选 `--email`）+ `reset_platform_mfa()`；拆出可 await 的 `run(argv)` 供测试在既有事件循环内调用 |
| 测试 | `tests/test_security_encryption.py`、`tests/test_security_totp.py`、`tests/integration/test_auth_d2_mfa.py`（8）、`tests/integration/test_auth_d2_mfa_enroll.py`（11）、`tests/business/test_bs_m1d2_mfa.py`（5）、`tests/support/mfa.py`（共用助手）、`tests/integration/test_migration_rollback.py`、`tests/integration/test_rls_policy_coverage.py` |

### 11.2 与简报的差异（均为实现选择，不改变结论）

0. **`MFA_TOTP_DIGITS` 未做成配置项**：§5.2 曾列出该项，实现按 RFC 6238 与主流 Authenticator 默认值固定 6 位（`verify_totp` 直接校验长度与数字），只把 `MFA_TOTP_INTERVAL_SECONDS`/`MFA_TOTP_WINDOW_STEPS` 配置化；改位长需同时改 `pyotp` 参数与恢复码格式，本期无需求。

1. **待激活密钥不落库，只进 Redis**：简报 §5.1 允许"待激活 secret 存在但 `mfa_enabled=false`"，实现改为 `{prefix}:auth:mfa:pending:{realm}:{user_id}`（值为 AES-GCM 密文，TTL = `MFA_ENROLL_TOKEN_TTL_SECONDS`）。理由：避免新增列与"半成品绑定"清理任务，且中断的绑定自然过期；解密失败/缺失分别 500（fail-closed）与 400 `MFA_ENROLL_EXPIRED`。已登记 TD-133。
2. **激活/关闭时同时删除 Redis 旧会话标记**：简报 §3.6 只要求 `token_version+1` + 撤销 refresh；实现追加 `delete_sessions(old_families)`，否则 ≤15 分钟的旧 access token 在轮换后仍可用。
3. **失败审计独立事务 + 租户不存在回落到平台审计**：`_append_realm_audit()` 在事务外写入，且当租户侧因 FK/RLS 无法落审计时改写平台审计，避免失败审计丢失（简报 §5.6 只说"事务外独立写"）。
4. **改密重签保留 `amr`/`mfa_verified`**：简报 §5.5-9 的约定落地为 `change_password(..., mfa_verified=principal.mfa_verified)`，防止策略上线窗口内会话被降级为仅密码。
5. **实际审计动作清单**（比简报 §5.6 多 3 项）：`auth.mfa.enrollment_started`、`auth.mfa.enrollment_failed`、`auth.mfa.activation_failed`、`auth.mfa.activated`、`auth.mfa.disabled`、`auth.mfa.disable_failed`、`auth.mfa.recovery_used`（`phase=login|disable`）、`auth.mfa.secret_invalid`、`platform.user.mfa_reset`，以及既有 `auth.login.success`（`mfa`/`recovery_code_used`）与 `auth.login.failed`（`reason=invalid_mfa_code|locked|password_mismatch`）。metadata 只含哈希、计数、掩码账号与布尔标记，不含 secret/码/令牌。
6. **`--reset-mfa` 的 CLI 结构**：`main()` 仅 `asyncio.run(run(argv))`，`run()` 返回退出码并负责打印，使集成测试可直接 await 而不触发"运行中事件循环"错误；输出不含任何密钥/验证码。

### 11.3 TDD 清单完成状态（后端）

- [x] 单元：TOTP ±1 通过 / ±2 拒绝、step 重放拒绝、AES-GCM 往返与 AAD 错位/篡改失败、恢复码归一化与单次消费、KEK 长度/占位校验、配置校验。
- [x] 集成：绑定→激活→挑战→验证、错误码计数与锁定、挑战单次兑换、同步步码重放与下一步可用、恢复码登录/复用、token_version 变更后旧挑战失效、解密失败 fail-closed、平台强制策略、refresh 策略、改密保留 `amr`、会话轮换、迁移往返、RLS 覆盖。
- [x] 业务场景（HTTP 全链路）：挑战无 Cookie→验证下发 HttpOnly Cookie、`/me` 标记、未开 MFA 保持原形态、平台未绑定只拿绑定令牌、自助绑定→激活→旧令牌 401→关闭恢复原登录、平台强制链路（免密码绑定→激活→策略下禁止关闭）。
- [x] CLI：`reset_platform_mfa` 与 `platform_admin.run(["--reset-mfa", ...])` 契约（清绑定、`admin_revoke`、撤销会话、清恢复码、审计掩码、旧 refresh 失效、输出不含密钥）。
- [x] E2E 与前端（任务 #26）：admin 登录 TOTP / 强制绑定 / 安全设置页、campus TOTP 步骤。

### 11.4 前端交付与审查处置（2026-10-22）

前端文件（补 §11.1）：

| 类型 | 文件 |
|---|---|
| API 客户端 | `frontend/packages/api-client/src/auth.ts`（`LoginOutcome` 三态、`verifyMfa/enrollMfa/activateMfa/disableMfa`、`/me` 的 `mfaEnabled/mfaRequired`）、`src/index.ts`、`src/auth.test.ts`（27 条） |
| admin | `stores/auth.ts`（`mfaToken/enrollToken/recoveryCodes` 仅内存、`mustEnrollMfa`）、`views/LoginView.vue`（密码 → 动态码两步、可退回）、`views/MfaEnrollView.vue` + `components/MfaEnrollPanel.vue`（强制绑定，二维码本地 `qrcode` 渲染）、`views/SecurityView.vue`（状态、自助开启/关闭）、`views/HomeView.vue`（入口）、`router/index.ts`（`PUBLIC_WHILE_MFA_ENROLL` 守卫，先于首登改密门禁） |
| campus | `stores/auth.ts`、`views/LoginView.vue`（仅 TOTP 登录步骤；未绑定账号提示由管理员完成绑定，不自助入口） |
| E2E | `frontend/e2e/helpers/auth-mock.ts`（有状态 MFA 假后端，记录请求头/请求体）、`admin-mfa.spec.ts`（4）、`campus-mfa.spec.ts`（2） |

红线落实：未完成第二步不写会话/Cookie；secret、otpauth URI、恢复码只存内存且不入 `localStorage`；二维码本地生成，不调用任何第三方二维码服务；绑定/激活响应后页面不再回显密钥；路由守卫把「必须绑定」排在「必须改密」之前。

审查处置（详见 `docs/review-log.md` 的 M1-D2 小节）：

| 轮次 | 结论 | 问题与处置 |
|---|---|---|
| 第一轮 | 不通过（0B + 1 Major + 4 Minor） | Major：`mfa_recovery_codes` 缺真实跨租隔离用例 → 已补（跨租 select 为空、A 的 UPDATE 命中 0 行、B 的 `used_at` 仍 NULL）。Minor：`decode_mfa_token` 缺 realm/tenant 组合守卫 → 已补 + 伪造令牌单测；`verify_mfa` 注释与消费顺序不一致 → 已改写为「GETDEL 是投递前唯一闸门」；登录时挑战注册 fail-open（仅 warning）→ 已改为抛 `RedisUnavailableError` 并返回 503，补 fail-closed 回归；恢复码熵偏低 → TD-135 已延后 M6（与 TD-129 再生成能力一并处理） |
| 第二轮 | 通过（0B / 0M） | 确认 Major 闭环、三项 Minor 修复正确，无新缺陷 |

最终门禁：后端 411 passed、`ruff`/`mypy`（116 文件）/`alembic check` 全绿、head `c4d6e8f0a2b3`；前端 typecheck/lint/prettier/build 全绿、32 单测 + 28 E2E；本地 `scripts/ci.sh` 24 步全绿（首轮在 `bandit -r app` 因 `assert row is not None` 触发 B101 失败，改为分支内类型收窄后通过）。远端 CI（`5add509`）：backend #39771902296、frontend #39771902306、security #39771902298 全 success；evals 未触发。

未做并已登记的取舍：WebAuthn/Passkey（TD-124）、记住设备（TD-127）、MFA 独立限流（TD-128）、恢复码再生成与熵提升（TD-129/TD-135）、租户强制与校管重置（TD-126）、KEK 轮换（TD-125）、策略切换 15 分钟窗口（TD-132）、待激活密钥仅 Redis（TD-133）、同窗口重放 UX（TD-134）。

