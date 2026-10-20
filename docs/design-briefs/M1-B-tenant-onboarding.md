# M1-B — 学校注册与平台审核（TEN-01/02/03）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-20 |
| 状态 | 已确认（2026-10-20：随机编号 / 邮箱唯一+校名提示 / 先存后扫 / 临时开发令牌） |
| 关联功能点 | M1-B（学校注册、SaaS 审核开通、学校编号、超管账号与初始密码、邮件、审核记录） |
| 验收引用 | `docs/spec.md` §3.2 TEN-01/02/03；`docs/milestone.md` §4.4；`docs/rbac-matrix.md` 的 `platform.tenant.review/activate/view/export` |

## 1. 目标

让一所学校能**自己提交注册**，平台运营/超管**审核**；通过后系统自动：生成全局唯一学校编号 → 创建校方超管账号（随机初始密码）→ 发邮件 → 租户状态变为 `active`；拒绝必须填理由。全程留下不可篡改的审计与审核记录。

不解决：校方后台功能（M2）、登录/JWT（M1-C，审核端点本期用临时开发令牌，见 §3.4）、密码策略/锁定/MFA（M1-D）、消息中心 outbox（M5，本期邮件失败重试用 Arq 简版）。

## 2. 数据模型（新增 2 张表，复用 M1-A 的 tenants/users/roles/audit_logs）

- `tenant_registration_requests`：`id`、`school_name`、`address`、`leadership`、`email`、`qualification_object_key`、`qualification_sha256`、`status(pending/approved/rejected)`、`dedup_key`（规范化邮箱）、`submitted_at`、`reviewed_at`、`tenant_id`（通过后回填）、`reject_reason`。
- `school_review_records`：`id`、`request_id`、`tenant_id`、`reviewer_id`、`result(approved/rejected)`、`reason`、`snapshot_object_key`、`reviewed_at`。
- 通过时（**同一事务**）：`tenants`（active、school_code）→ 校方内置角色（`school_super_admin` 等，缺则建）→ `users`（超管，`must_change_password=true`）→ `user_roles` → `tenant_registration_requests.status` → 审计事件；**事务提交后**再入队发邮件（避免事务内 IO）。

## 3. 需要拍板的四个决策（候选方案）

### 3.1 学校编号格式

**方案 A：`SCH-<年份>-<4位序号>`**，如 `SCH-2026-0001`；按年用数据库序列递增，唯一约束兜底。
- 优点：人眼可读、便于口头/客服沟通；年份分段便于统计。
- 缺点：会暴露"今年新开了多少所"（对 SaaS 运营通常无妨）。
- 风险：并发下用序列 + 唯一约束重试即可。

**方案 B（用户确认采用）：`SCH-` + 8 位随机字母数字**（如 `SCH-7K3M9Q2P`），
字符集排除易混淆字符（0/O、1/I/L），唯一约束 + 冲突重试。
- 优点：不泄露规模；难以猜测。
- 缺点：不好口头念、客服核对易错。

**方案 C：审核人手工填写 / 校方自带编号**。
- 缺点：易重复、易冲突，需要额外校验流程；不推荐。

### 3.2 重复注册怎么判定

**方案 A（推荐）：邮箱唯一 + 校名重复只提示**。
- 同邮箱再次提交 → `409`，返回已有申请的状态（避免同一学校刷多条）。
- 同校名 → 允许提交，但在审核列表标"疑似重复"（可能存在分校/改名），由审核人决定。
- 优点：既不误拦合理情况，又能挡住最明显的重复。
- 缺点：审核人需要多看一眼标记。

**方案 B：邮箱或校名任一重复就直接拒绝**。
- 优点：实现最简单。
- 缺点：分校/改名会被误拦（例如"XX 大学附属中学"与"XX 大学"）。

**方案 C：全部交人工，不做自动判定**。
- 缺点：重复数据会污染审核队列。

### 3.3 资质凭证是否本期接入病毒扫描

**方案 A（推荐）：本期只做"安全落盘"，扫描留到 M3**。
- 做法：接收 PDF/JPG/PNG（≤10MB），存 MinIO 的 `onboarding/<request_id>/` 前缀，记录 sha256；**不做病毒扫描**；登记 TD，M3 接入 ClamAV 时统一补扫（历史凭证补扫一遍）。
- 优点：不阻塞 M1；凭证只在平台审核时人工查看，不进入任何解析链路。
- 缺点：恶意文件理论上可被审核人下载打开（风险与普通邮件附件同级）。

**方案 B：本期就接 ClamAV**（复用现有容器，扫描未通过直接拒绝提交）。
- 优点：更安全、与 M3 知识库门禁一致。
- 缺点：范围扩大（异步扫描/超时/失败重试），且 M1 内尚无文件上传的通用组件。

### 3.4 审核端点的临时鉴权（M1-C 之前）

**方案 A（推荐）：开发/测试专用静态令牌**。
- 做法：`DEV_PLATFORM_REVIEW_TOKEN`（仅 dev/test 可用；**production 下配置了也拒绝服务**）；审核接口要求 `Authorization: Bearer <token>`；M1-C 用 JWT + `platform.tenant.review` 权限替换并删除该开关。
- 优点：本期就能跑通"注册→审核→开通→邮件"的完整业务场景；生产无后门。
- 缺点：是一段有明确删除时点的临时代码（登记 TD）。

**方案 B：本期只做服务层与测试，审核 HTTP 端点等 M1-C 再暴露**。
- 优点：零临时鉴权代码。
- 缺点：M1-B 的验收只能到服务层，端到端演示要等 M1-C。

**方案 C：现在就把平台 JWT 提前实现** —— 属于 M1-C 范围，范围蔓延，不推荐。

## 4. 选择结论

- 用户确认（2026-10-20）：**3.1 方案 B（随机 8 位）**、**3.2 方案 A（邮箱唯一+校名提示）**、**3.3 方案 A（先存后扫）**、**3.4 方案 A（临时开发令牌）**。
- 实现补充决策（技术细节，随本轮落地并登记）：
  - 校方管理员账号落在 `users` 表：`user_type` 扩展为 `student/teacher/school_admin`，`login_id` 使用**注册邮箱**（租户内唯一；M1-C 校方登录用"学校编号+邮箱+密码"）；
  - 凭证存储用 `ObjectStore` 抽象：开发/测试用**本地目录**（CI 可跑），生产用 MinIO/S3；两者同接口，避免 CI 依赖本地自建 MinIO 镜像（登记 TD）；
  - 初始密码邮件在事务提交后**同步发送**（不在事务内做 IO，也不把明文密码放进 Redis/任务队列）；发送失败写审计并返回 `email_sent=false`，重发功能排 M1-C/M5（登记 TD）。
- 是否需要改 spec/design：需要补 design.md 的"注册审核状态机与幂等""审核端点临时鉴权与移除时点"，不改已确认的对外行为。

## 5. 实现顺序与 TDD 测试清单

1. **契约/模型**：2 张新表 + 迁移（含 RLS：`tenant_registration_requests` 无 tenant_id → 平台级表、平台连接读写；`school_review_records` 同）；`app/auth/passwords.py`（bcrypt cost 12、12 位随机初始密码生成）。
2. **服务层**：`register_school()`、`approve_request()`、`reject_request()`、学校编号生成、内置角色初始化、超管账号、邮件入队（Arq）。
3. **接口层**：`POST /platform/tenants/register`（公开）、`POST /platform/tenants/{request_id}/review`（临时令牌）、`GET /platform/tenants`、`GET /platform/tenants/{id}/review-records`（只读）。
4. 红→绿→重构测试：
   - [ ] 单元：编号格式/年份切换/唯一性重试；邮箱规范化与去重；初始密码 12 位且 `must_change_password=true`；状态机（pending 只能被审一次；approved 后不可再拒）；拒绝缺理由 → 422。
   - [ ] 集成（真实 PG/MinIO/MailHog/Redis）：注册落库 + 凭证对象与 sha256；通过 → `tenants.status=active` + 学校编号 + 超管账号（密码可登录性用哈希校验）+ 校方角色 + 审计事件 + MailHog 收到邮件且正文含初始密码；拒绝 → 状态与理由、可查审核记录；重复邮箱 → 409；并发两次审核 → 只有一次成功；`DEV_SKIP_SCHOOL_REVIEW` 在 production 启动失败。
   - [ ] 业务场景 BS-M1-B-01：注册 → 审核通过 → 开通 → 邮件收到 → 审核记录可查（断言 DB 状态/审计/邮件/对象存储，而非仅 HTTP 码）。
   - [ ] E2E：本期不涉及（校方/平台页面未做）。
   - [ ] 性能：不涉及（M6 压测）。

## 6. 预判技术债

| 条目 | 处置 |
|---|---|
| 凭证病毒扫描（方案 A 时） | 登记 TD，M3 统一接 ClamAV 并补扫历史凭证 |
| 审核端点临时开发令牌 | 登记 TD，M1-C 删除并换 JWT |
| 邮件重试/死信/退信 | 本期 Arq 简单重试；完整 outbox 在 M5 |
| 学校编号回滚产生空洞 | 接受（编号只需唯一，不要求连续） |
| 资质凭证留存/清理策略 | M6（合规留存矩阵） |
| 审核人账号来源（平台初始化账号） | M1-C 登录落地前用迁移种子/脚本创建；登记 TD 到 M1-C |

## 7. 高风险确认

- 是否属于高风险功能：**是**（租户开通、初始密码、邮件、平台审核权限、对象存储写入）。
- 需要用户确认：§3 四个决策。
- 确认结论：待填。

## 8. 审查对照

- 审查时重点验证：公开注册接口的滥用防护（限流/大小限制/字段校验）；审核端点在 production 是否绝对不可用（配置了令牌也拒绝）；初始密码只在邮件中出现，**不得**出现在日志/审计/响应体；审核的并发与幂等（同一申请只能通过一次）；对象存储 key 不可跨租枚举；审计事件覆盖注册/审核/开通；`DEV_SKIP_SCHOOL_REVIEW` 生产 fail-fast；业务场景断言到 DB/邮件/对象存储。
- 允许的偏差：凭证病毒扫描延后（§3.3 A）、审核端点临时令牌（§3.4 A）。

## 9. 实现结果（2026-10-20）

- 迁移：`c688c44dec81`（新增 2 张平台表；`users.user_type` 扩展 `school_admin`；`login_id` 加宽到 254、`display_name` 到 200；平台开通改为 `SECURITY DEFINER provision_tenant()`；审核记录仅追加触发器；申请状态机触发器）。
- 新增代码：`app/auth/passwords.py`、`app/storage/objects.py`、`app/integrations/mail.py`、`app/audit/writer.py`、
  `app/onboarding/{codes,service}.py`、`app/api/platform_tenants.py`；`app/config.py` 增加 SMTP/对象存储/开发开关与生产校验。
- 测试：单元 15 条（编号/密码/邮箱/资质）、集成 9 条（提交/重复/提示/通过/拒绝/并发/跳过审核/列表/角色非超级用户）、
  业务场景 2 条（HTTP 全链路 + 拒绝流程）→ 后端全量 **156 passed**。
- 证据：邮件正文的初始密码可校验登录哈希；凭证与审核快照对象存在且内容一致；审计含 `tenant.register.submitted`/`tenant.approve`；
  并发审核仅一次成功；响应体不含密码；审核接口无令牌/错令牌均 401。
- 偏差与登记：公开接口限流（TD-076）、邮件重发/outbox（TD-077）、临时审核令牌（TD-078）、凭证病毒扫描（TD-079）、
  S3 路径无 CI 集成测试（TD-080）、平台真实审核人账号（TD-081）、校方角色权限映射（TD-082）。
- 审查结论：第一轮**不通过**（1 Blocker + 14 Major + 16 Minor）。

### 9.1 审查修复（第一轮 → 第二轮）

| 编号 | 问题 | 修复 |
|---|---|---|
| B1 | 平台角色可向**存量租户** INSERT users/roles（后门） | 撤销 4 张表 INSERT 授权与 `platform_provision` 策略；改为 `provision_tenant(jsonb)` SECURITY DEFINER，函数内部生成新租户 id 并原子创建租户/角色/超管/绑定；新增"平台不能写存量租户"回归测试 |
| M1 | 校方超管获得 `platform.*` 权限 | 开通函数只取 `module IN ('school','campus')`；测试断言权限模块子集 |
| M2 | `login_id`(64)/`display_name`(100) 与可接受输入不匹配 → 审核 500 | 迁移加宽到 254/200；显示名截断；64 字符 local-part + 200 字校名端到端测试 |
| M3 | 邮箱校验过弱（双 @、CRLF） | 严格正则 + 控制字符/空白拒绝 + local ≤64 + 总长 ≤254；单测覆盖 |
| M4 | 邮件失败留下"已激活但无密码" | 邮件移入事务；失败抛 502 回滚开通（申请保持 pending），恢复 SMTP 后可重试；集成测试断言租户/审核记录/审计均未产生 |
| M5/M6 | 生产未强制平台/迁移连接与 SMTP；密钥明文 | 生产强制 `PLATFORM_DATABASE_URL`/`MIGRATION_DATABASE_URL`、真实 SMTP + 用户名/密码 + TLS/STARTTLS；`SecretStr` 保存 SMTP/S3/开发令牌；新增 `SMTP_START_TLS` |
| M7 | 开发令牌非恒定时间比较、无失败审计 | `secrets.compare_digest` + `platform.review.denied` 审计（含 request_id） |
| M8/M9 | 审计含明文邮箱、request_id 全空 | 邮箱掩码（`a***@domain`）+ 理由截断；接口把 `request.state.request_id` 传入审计，业务测试断言与响应头一致 |
| M10 | 审核记录可改/删；申请可重复迁移 | 平台角色仅 SELECT/INSERT + 仅追加触发器；申请状态机触发器禁止审结后再改；无 DELETE 授权 |
| M11 | 先写对象后查重产生孤儿；无请求体上限 | 先查重再写对象并在失败时删除；ASGI 层 11MB Content-Length 限制（413）；重复提交无孤儿对象测试 |
| M12 | 有 school_admin 数据时 downgrade 失败 | downgrade 先删除本功能创建的超管账号再恢复旧约束；独立临时库回滚测试（升级→写超管→降级→再升级） |
| M13 | TD-073 启动自检未实现 | `verify_runtime_database()` + lifespan（staging/production），校验角色/FORCE RLS/fail-closed；测试覆盖拒绝与通过 |
| M14 | 缺失败路径测试 | 新增 10 条集成/业务回归（后门、权限 realm、长字段、SMTP 失败回滚、审计脱敏、不可篡改、孤儿、自检、413、拒绝审计） |
| Minor | 魔数校验、Windows key 逃逸、bcrypt 阻塞、地址长度、文档编号 | 魔数校验（PDF/PNG/JPEG）、对象 key 拒绝盘符/冒号/反斜杠并校验根目录、bcrypt 放线程池、address=300、design.md 小节重编号；其余登记 TD-083~089 |

- 复测：`ruff` + `mypy --no-incremental` 全绿；后端全量 **178 passed**；`bash scripts/ci.sh` 待跑。
- 第二轮复审：**不通过**（1 Blocker B2 + 8 Major M15–M23）。
- 第二轮修复（§9.2）：见下。

### 9.2 第二轮审查修复

| 编号 | 问题 | 修复 |
|---|---|---|
| B2 | 自检只查租户连接、全程 RLS 用全局 count、空表 count=0 误判 | 自检同时检查租户+平台连接角色；逐表校验 FORCE RLS + 策略引用 `app.tenant_id/app.platform_context`；校验审核函数 owner 可绕过 RLS；保留无上下文不可见探针；新增"平台 DSN 为超级用户""users 关闭 FORCE RLS 且为空"测试 |
| M15 | 平台可直接 UPDATE 申请状态、INSERT 伪造审核记录 | 审核整体收进 `review_registration_request()`（SECURITY DEFINER，锁申请+状态机+租户/角色/超管+审核记录+审计+状态迁移）；平台撤销申请 UPDATE、审核记录 INSERT；回归测试覆盖 |
| M16 | 平台对 tenants 完整 DML、可直接调用内部分通函数 | 平台仅保留 tenants SELECT + `UPDATE(status,status_reason)`；`provision_tenant` 对平台/PUBLIC 撤销 EXECUTE；测试覆盖直接写/直接调用均 permission denied |
| M17 | 邮件已发但提交失败的反向窗口 | 该窗口不产生可登录账号（事务回滚），重试生成新密码覆盖旧邮件；设计/TD-088 明确接受并记录测试结论 |
| M18 | 校名/地址含 CRLF 导致审核永久 502 | 所有文本字段拒绝 C0/控制字符；单测 + 集成回归 |
| M19 | chunked 请求绕过 Content-Length 限制 | ASGI 中间件在进入应用前缓冲并累计字节，超 11MB 直接 413；413 带 X-Request-ID；业务测试覆盖 chunked |
| M20 | 原地改 revision，旧 schema 不生效；downgrade 缺 IF EXISTS | 新增 `d9f4a2b7c3e1_m1b_review_hardening` 幂等硬化 revision（旧/新 schema 都适用）；独立临时库测试"第一轮 schema → head"；函数删除用 `to_regprocedure`/`IF EXISTS` |
| M21 | 运行时强制 MIGRATION_DATABASE_URL（超级用户凭据进应用） | 从运行时生产校验移除；该连接只由迁移 Job 注入；改为 `SecretStr` |
| M22 | SECURITY DEFINER 依赖迁移角色超级用户/BYPASSRLS 未强制 | 硬化迁移显式检查 `rolsuper OR rolbypassrls`，不满足直接失败；自检校验函数 owner；`search_path=pg_catalog,public` |
| M23 | 关键失败路径无测试 | 新增：平台直接写全路径、chunked、控制字符、旧 schema 升级、自检平台/逐表 RLS、生产配置开关；见各测试文件 |
| Minor | 回滚列宽/租户状态、失败审计、拒绝理由 PII、.env 重复、邮箱点号、TLS 组合、413 request-id | 已修：downgrade 删除超管+停用租户+恢复列宽；邮件失败补 `tenant.approve.email_failed`；拒绝审计只存 reason_sha256；.env 去重；邮箱拒绝首尾/连续点；TLS 二选一；413 带 request-id |
| 其余 Minor | 拒绝审计可刷、平台可 INSERT 审计（M1-F 前）、列表无分页等 | 登记 TD-084/085/086/087/089/090/091 |

- 复测：`ruff`/`mypy` 全绿；后端全量 **186 passed**；`alembic check` 无漂移；`bash scripts/ci.sh` 待跑。
- 第三轮复审：**不通过**（1 Blocker B3 + 4 Major M24–M27）。
- 第三轮修复（§9.3）：见下。

### 9.3 第三轮审查修复

| 编号 | 问题 | 修复 |
|---|---|---|
| B3 | `review_registration_request` 可被同名 TEMP 表遮蔽（平台可伪造申请数据创建影子租户） | 函数内所有表名显式 `public.` 限定（含 `%ROWTYPE`），`search_path=pg_catalog,public,pg_temp`；新增回归测试：平台建同名 TEMP 表并伪造行后调用，真实申请被处理、临时行保持 pending |
| M24 | 自检 `bool_or` 只要一条策略合规即通过，`USING (true)` 宽松策略可绕过 | 改为要求**每条**策略都引用上下文且只授予 `campus_elf_tenant/campus_elf_platform`；新增"users 增加 USING (true) 后自检必须失败"测试 |
| M25 | 旧 schema（无 provision_tenant）升级后降级仍失败；测试未真实模拟第一轮 | c688 downgrade 去掉非幂等 `REVOKE`（仅 `DROP FUNCTION IF EXISTS`）；测试改为真实模拟第一轮（删除函数 + 重建 platform_provision 策略），并增加"升级后降级到 c688"往返断言 |
| M26 | d9 downgrade 重新授予 users/roles 全量 DML，超出目标 revision | downgrade 精确恢复 c688：身份表只恢复 INSERT + `platform_provision` 策略，不授予 UPDATE/DELETE；断言降级后权限矩阵 |
| M27 | 全局 11MB 体积限制会阻断后续 KB 50MB/300MB 上传 | 中间件改为**按路由**限制（仅注册路由 11MB），未登记路由不做缓冲原样透传；新增"其他路由 12MB 请求返回 404 而非 413"测试 |
| Minor | 失败审计断言、函数参数长度/邮箱掩码、测试命名、审计刷写/租户状态更新无审计等 | 邮件失败测试补断言（tenant_id NULL、masked email、request_id）；函数内截断 reason/request_id 并安全掩码；`_school_admin_count` 按 user_type 过滤；其余登记 TD-091/093/094 |

- 复测：`ruff`/`mypy` 全绿；后端全量 **189 passed**；`bash scripts/ci.sh` 待跑。
- 第四轮复审：**不通过**（2 Blocker B4/B5 + 2 Major M28/M29）。
- 第四轮修复（§9.4）：见下。

### 9.4 第四轮审查修复

| 编号 | 问题 | 修复 |
|---|---|---|
| B4 | 旧 schema 上 `CREATE OR REPLACE provision_tenant` 新建函数默认授予 PUBLIC EXECUTE，迁移前置于创建之前使预撤销被跳过 → 平台仍可无审计开通 | 撤销移至创建**之后且无条件执行**（PUBLIC + 平台）；`_assert_hardened` 增加函数 ACL 断言（无 PUBLIC `=X/` 项）与"平台调用被拒"攻击断言 |
| B5 | `d9` downgrade 重建身份表 INSERT + `platform_provision` 策略，把 B1 攻击路径带回来（且与 c688 规范状态不符） | downgrade 只恢复 c688 规范状态：身份表 `REVOKE ALL` + 删除策略、申请单/审核记录/tenants 恢复既有授权、分通函数恢复平台 EXECUTE；`_assert_c688_state` 断言身份表 INSERT=false、`platform_provision=0`、平台向存量租户插账号被拒 |
| M28 | 第四轮修复仍写入已应用的 `d9`，旧 d9 环境不会生效 | 新增 revision `e5c9d1f7a2b4_m1b_hardening2`：幂等重放两个函数（同样全限定）并在创建后无条件补撤销、兜底收回身份表权限与遗留策略；downgrade 为空操作（目标状态与 d9 相同） |
| M29 | 回归测试未覆盖 B4/B5 | `_assert_hardened`/`_assert_c688_state` 增加函数 ACL、平台调用拒绝、降级后越权插入拒绝等断言 |
| Minor | 尾斜杠路径、自检空策略计数、邮件失败审计语义 | 体积限制对路径去尾斜杠后匹配；自检 filter 增加 `policyname is not null`；失败审计 resource_type 改为 `tenant_registration_request` |

- 复测：`ruff`/`mypy` 全绿；后端全量 **189 passed**；`alembic check` 无漂移，head=`e5c9d1f7a2b4`；`bash scripts/ci.sh` 待跑。
- 第五轮复审：**通过**（无 Blocker/Major；7 条 Minor + 4 条已登记 TD）。

### 9.5 第五轮复审后的 Minor 收口

- `e5c9d1f7a2b4` 增加幂等兜底：`_require_bypass_role` 自检、`users` 列宽、审核记录仅追加/申请状态机触发器、`tenants` 列级授权与申请单/审核记录写权限回收、触发器函数 `REVOKE ... FROM PUBLIC`。
- 迁移测试加固：断言租户角色不可执行审核函数；降级越权插入断言改为同一显式事务内设置平台上下文（忠实复现攻击环境）。
- 已知残余（登记 TD）：TD-091（平台可 INSERT 审计，M1-F 链校验）、TD-092（平台可直接调用审核函数并指定哈希，M1-C 拆分审核/通知）、TD-093（tenants 列级状态更新无审计，M1-C/M6）、TD-094（平台可 INSERT 申请单，M1-C）；新增 TD-095（降级到 c688 会恢复分通函数平台 EXECUTE，运维手册需标注）。
- 最终验证：`ruff`/`mypy` 全绿；后端全量 **189 passed**；`alembic check` 无漂移；head=`e5c9d1f7a2b4`；`bash scripts/ci.sh` 全绿。
