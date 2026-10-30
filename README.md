# 校园陪伴精灵（Campus Companion Elf）

面向高校的多租户 SaaS 平台，提供 AI 陪伴、校园知识库、校务审批、消息通知、轻社交与心理关怀辅助能力。

## 当前状态

**当前阶段：M2 组织与人员收尾中（ORG-01/03、ORG-02 导入、ORG-05 部门树、ORG-04 身份证加密、ORG-06 学年流转、M2-UI1 B 端页面、M2-UI2 角色权限后台已交付并过 CI；M2-UI3 自定义角色 / 多角色绑定 / 权限账号的十批已全部交付并过 CI——最后一批是**前端权限账号页 TD-187g-C**（2026-10-30，PR #22 → 合并提交 `fb6513d`）；**M2 只剩收尾三件**：TD-183/192 全栈 E2E 补跑、M2 边界评审（台账逐条过处置）、`app/auth` 分支覆盖率抬到 82%；简报见 `docs/design-briefs/`）。** 交接入口：**`docs/handoff.md`（接手手册：状态表/环境/门禁/文档地图）**，规则源头：`AGENTS.md`。

已完成：

- **M0 基座**（M0-A~M0-F）：Git/后端骨架/健康检查、Compose 依赖栈（PG 16 / Redis 7.4 / MinIO / MailHog / ClamAV / Qdrant 1.19.2 / TEI 1.9.4）、Harness 骨架、前端脚手架、兼容性与外部组件验证（MinerU 4.0.12 三档真实解析、Langfuse 4.63.0 自托管冒烟、Arq 3.13 兼容）、独立测试栈与 4 条 GitHub Actions、CPU 嵌入/重排基准。验收报告：`docs/m0-acceptance.md`。
- **M1-A 租户基座**：13 张表 + 复合外键、RLS（FORCE + 按角色策略 + fail-closed）、三连接与双应用角色分离、仓储/请求上下文/中间件、审计仅追加；两轮独立审查通过。
- **M1-B 学校注册与平台审核（TEN-01/02/03）**：公开注册（multipart 资质凭证）、平台审核与原子开通（`SECURITY DEFINER` 审核函数）、学校编号、超管账号与初始密码邮件、审核记录与审计；五轮独立审查通过（含跨租后门、TEMP 表遮蔽、PUBLIC 授权等修复）。
- **M1-C 三端登录与令牌后端（AUTH-01/02）**：JWT 15 分钟 + 刷新令牌 7 天轮换、重用检测撤销家族、登出/改密即时失效、realm↔数据库连接强制绑定、首登改密门禁、平台审核改真实账号鉴权（删除开发令牌）、注册提交收进函数、过期令牌清理任务；三轮独立审查通过。
- **M1-C-UI 三端登录页与强制改密页**：admin（身份切换）+ campus 登录页、强制改密页、路由守卫、HttpOnly 刷新 Cookie（生产强制 Secure / 不回显 refresh / 禁 SameSite=none）、统一 401/403 处理；两轮独立审查通过。前端 20 单测 + 12 Playwright 全绿。
- **M1-C2 会话设备管理（AUTH-06，策略 C）**：访问令牌 `sid` + Redis 会话标记实现被踢即时失效；租户 3 / 平台 2 台设备上限、超限踢最久未用；设备列表、单台/退出其它设备；跨用户/跨租统一 404 并写 `auth.session.revoke_denied`；登出按 family 去重审计、Redis 故障 best-effort；五轮独立审查通过（迁移 `d4e6f8a0b2c1`）。
- **M1-D1 密码与锁定（AUTH-03 员工/平台、AUTH-05）**：密码策略（10~128 字符且 ≤72 UTF-8 字节、3 类字符、不含登录名）、最近 5 次历史、bcrypt cost 12；5 次失败锁 15 分钟；员工/平台邮件重置（30 分钟单次令牌、fragment 链接、申请/确认 IP 限流、后台发信、并发单次消费）；平台账号用 `python -m app.platform_admin` 初始化/维护邮箱；admin/campus 忘记密码与重置页；迁移 `b2d4f6a8c0e1`。学生校管重置按计划 M2 交付。
- **M1-D2 两步验证（AUTH-04）**：TOTP + 10 枚一次性恢复码；登录改为「密码 → 动态码 → 首登改密」三态（未过第二步不签发任何令牌或 Cookie）；强制策略仅平台运营账号（`MFA_REQUIRE_PLATFORM`，默认关闭）；TOTP 密钥以 AES-256-GCM + realm/user 绑定密文入库，恢复码只存 HMAC 摘要；防重放水印 + 单挑战尝试上限 + 账号锁定；启用/关闭 MFA 轮换全部会话；admin 端绑定页（二维码本地生成）与安全设置页、双端两步验证登录；迁移 `c4d6e8f0a2b3`；两轮独立审查通过（第二轮 0 Blocker / 0 Major）。学生自助绑定按计划 M2 随校管后台交付。
- **M1-E1 鉴权内核（RBAC-01 部分）**：权限矩阵 v1.2 的 59 个权限码（platform 12 / school 34 / campus 9 / observability 2 / eval 2）与 8 个内置角色出厂默认落库，删除 9 个无引用旧码；`app/rbac` 提供权限目录、realm 单一口径（`TENANT_MODULES`/`code_realm()`）、幂等同步（只补不撤、不覆盖运营改动）与路由清单守卫；迁移 `e5f7a9c1b3d5` 建代码持有的定义表与 `seed_builtin_roles(uuid)`（SECURITY DEFINER、EXECUTE 已收回），两条开通路径同源，并补齐 C 端 `student`/`teacher`；`require_permission(code, realm=)` 先判 realm 再判权限，接入 `campus.chat.use`、`platform.tenant.view`、`platform.tenant.review`，拒绝写 `auth.permission.denied`（只带 path+reason）；权限增删下一次请求即生效且不作废在线会话。矩阵 §3/§4 由测试解析并逐列比对，改一格 √/△ 即有用例变红。数据范围服务端过滤与审批职责分离见下一条 M1-E2。

- **M1-E2 数据范围与职责分离（RBAC-02/03）**：`school`/`self` 数据范围出厂默认落库（`rbac_builtin_role_scope_defaults`），`Principal` 每请求解析、仓储基类统一注入谓词，解析不到范围 = 空集（fail-closed），显式 id 越权 → 404 + `data_scope.denied` 审计；新增 `GET /api/v1/school/personnel`(+详情)（`school.personnel.view`，响应不含 `login_id`）；提交人不能自批的三道锁（应用层 403/422、数据库函数、同表三条款 CHECK）+ `system_settings` 例外开关（`platform.config.edit`、必填理由、审计、热生效）；平台跨租只读最小通道（只读事务 + 单租户 + 必填理由 + 审计失败即拒绝，工单化延后 TD-153）；`role_permissions_realm_guard` 收紧到 `TENANT_MODULES`；迁移 `f7a9b1c3d5e8`。
- **M1-F 审计哈希链（AUD-01）**：审计仅追加升级为跨表单链（触发器计算 `prev_hash`/`hash`，密钥经 GUC 注入不入日志）、哈希链查询接口与 `backend/scripts/verify_audit_chain.py` 校验脚本、应用启动自检；TD-068/091 收口。
- **M1 验收报告（含 TD-110 收口）**：`docs/m1-acceptance.md`。专用 compose 栈（`deploy/backend/Dockerfile` + `deploy/docker-compose.e2e.yml` + `scripts/e2e-stack.sh`）跑真实后端全栈 E2E：`frontend/e2e-fullstack/` **12 例全绿**（三端真实登录、首登强制改密、MailHog 真实邮件重置、平台 tenants/audit 业务接口、数据范围、越权 403、会话设备被踢 401）；边界技术债台账 171 条全部有处置（169 既有 + 本次新增 TD-170/171），TD-147 部署注意已写入 handoff §5.1。
- **M2 组织与人员（后端）**：人员档案与组织关系（教师/学生建档、班级/课程/任教、`class` 数据范围 + cursor 分页，迁移 `c9d5f1e3a7b9`）；CSV/XLSX 异步导入（Arq 任务、行级错误报告、失败行重试 + 看门狗，迁移 `e2a6c8d4b0f2`）；部门/科室树（id 物化路径、子树移动、教师归属 + 调岗审计，迁移 `a4c8e2f6b1d3`）；身份证加密采集与受控查看（AES-256-GCM 三件套 + 同租 HMAC 查重唯一索引 + 掩码列表 + `sensitive.read` 理由查看，迁移 `f8b2d6a0c4e8`）；入学年份与学年流转（班级届身份唯一键、当前年级批量刷新、毕业归档、幂等键+dry_run，迁移 `b9d3f7c1e5a9`）；B 端管理页面（Element Plus 校管后台：人员/受控查看/班级课程任教/部门树/导入/学年流转，菜单按权限渲染）。
- **M2 角色权限配置后台（M2-UI2，TD-126/137/143/150）**：迁移 `c3f7a9d1e5b2` 给 `roles`/`platform_roles` 加 `defaults_applied` 出厂标记、给 `roles` 加 `version` 乐观锁（内置角色的出厂默认从此只在未初始化时补种一次，运营主动清空不再被"补回来"）；`GET /school/roles`(+`/school/permissions`) 与 `PUT /school/roles/{id}/permissions` 全量替换（目录外/平台域码 422、版本冲突 409 并回带最新快照、变更写 `school.role.permissions.updated` 审计含 added/removed/reason、下一请求即生效且不作废在线会话）；`POST /school/roles/{id}/reset/preview` + `/reset` 重置为出厂（同时恢复权限与数据范围、需角色 code 逐字确认 + 理由 ≥10、锁定角色一律 409 `ROLE_IMMUTABLE`（绕过预览直连也被拒；自定义角色在本期同样被拒，M2-UI3 TD-187b 起改判为 `ROLE_DEFAULTS_UNAVAILABLE`——自建角色没有出厂定义，是结构性不可重置而非"尚未开放"））；`GET /school/users/lookup` + `POST /school/users/{id}/mfa/reset` 管理员 MFA 重置（专用码 `school.personnel.mfa.reset`，清绑定/锁定/恢复码 + 撤刷新令牌家族 + `token_version+1` + Redis 清会话，密码不变）；权限目录 59→60 码；admin 端「角色权限」页（按模块分组、增删差异预览、二次确认、409 自动采用最新、菜单与守卫随权限热更新、重置弹窗对「本人也在被重置角色且出厂清单不含 `role.manage`」给出自锁提示）；`school.role.manage` 只能由校方超管写入（非超管主体提交含该码的集合 → 422 + 拒绝审计），禁令的真实边界与残余风险登记在 design.md §5.12 与 TD-191。
- **M2-UI3 第一批：库层不变量 + 自扩权显式确认（TD-187a/TD-191，2026-10-27）**：迁移 `d7a1c3e5b9f4` 给 `roles` 加两条 CHECK（自定义角色码格式 `^[a-z][a-z0-9_]{2,62}$`、内置码只有 `is_system=true` 可占），新码 `school.role.delete`（权限目录 60→61，删除与配置分授）；`app/rbac/self_grant.py` 判据 + `PUT /school/roles/{id}/permissions` 接线——结果权限集触达操作者自己所在角色且**净增**敏感码时必须 `self_grant_ack=true` + 非空理由（超管免打断不免留痕），拒绝排在版本 CAS 之前，`school.role.self_grant` 审计**放行与打断都写**（打断走独立事务，报警因此看得到未遂）；前端按后端下发的敏感清单做本地预检，服务端 422 时强制展开确认条并保留草稿。
- **M2-UI3 第二批：自定义角色创建与删除（TD-187b，2026-10-27）**：`POST /school/roles`（初始权限二选一：显式清单或复制同租户可编辑角色；保留码/同码/目录外/平台域/转授禁令/复制源锁定各有独立人读错误码；并发同名由 `uq_roles_tenant_id_code` 兜底，且**只有这一条约束**翻 409 `ROLE_CODE_TAKEN`——约束名取自驱动回报的原生异常对象（PG 协议字段 `n`），不做错误文本子串匹配，其余完整性错误原样抛出，不把库层故障伪装成业务冲突）；`POST /school/roles/{id}/delete/preview` + `/delete`（`school.role.delete`、逐字确认码 + 理由 ≥10 + 版本 CAS，CASCADE 之前把权限码集与受影响用户数快照进 `school.role.deleted`，响应 `version` 是被删行的最后版本号）；自定义角色从此 `editable=true` 可删但**不可重置出厂**（`ROLE_DEFAULTS_UNAVAILABLE` 是结构性不可重置，非"尚未开放"），配置页重置入口改跟 `is_system` 走；创建路径**有意不接**自扩权守卫（判据对新角色 id 结构上不可触发，用户 2026-10-27 裁定改口径，能力流动由 PUT 与 TD-187d 的绑定路径承担），见简报 §12 与 TD-194。

- **M2-UI3 第三批：角色级数据范围编辑（TD-187c，2026-10-27）**：`GET /school/roles/{id}/scopes`（含服务端下发的 `editable`，前端不用按类型名猜）、`POST …/scopes/preview`（差异 + 持有人数 + `retained_non_editable` + `empties_scope`，只读、不审计、不推版本）、`PUT …/scopes`（全量替换：realm 判据 → 未知类型 → 行锁取角色 → 可编辑 → 不可手工授 → 版本 CAS → 范围版自扩权守卫 → `version+1` → **窄删除**（只碰 `school`/`self`，不替不存在的写路径清理角色级 `class` 行）→ `unnest` 插入 → `school.role.scopes.updated` 审计）；可编辑集 `{school,self}`，手工授 `class`/`course` 一律拒授**并留安全审计**——真原因是本端点没有 `scope_id` 入参通道（写入即缺 id 的脏行，解析层抛 `ValueError` 会让该角色每个持有人 500），`course` 另本期不贡献可见性（TD-151）；范围敏感项 `{school}` 与权限侧共用同一判据内核（净增且触达本人 → `self_grant_ack` + 理由，超管免打断不免留痕，`school.role.self_grant` 的 `metadata.kind="scope"` 区分），**空清单合法**并由预览提示"N 名持有人将看不到任何数据"；**重置出厂是这条判据的第 4 个入口、本点未接**（"收窄 → 恢复出厂 → 静默拿回全校"是可达路径，已随 TD-187d 收口，TD-194）；三轮独立审查 0B/5M/14m 全部整改，处置见 `docs/review-log.md` 与 TD-195/202/203/204/205。
- **M2-UI3 第四批：用户↔多角色绑定 + 删除影响面指纹（TD-187d/199/194，2026-10-29，PR #9 合并提交 `9771be8`）**：`GET/PUT /api/v1/school/users/{user_id}/roles`（`school.permission_account.manage`，读写同码）全量替换绑定，`users.roles_version` 做绑定集 CAS（迁移 `e9b4d2f6a8c1`），绑定**下一请求生效、不作废会话**；自绑禁令先于对目标账号的任何读取与状态写入（比较 `Principal` 与路径参数；拒绝审计另起事务，403 `BINDING_SELF_FORBIDDEN`），目标限 `teacher`/`school_admin`（学生 422）、目标持超管 403、提交超管角色 422、空集合法、跨租与 `deleted` 404（`locked`/`disabled` 可绑）；成功写 `school.user.roles.updated`（角色码与权限码前后差集 + 理由，不含明文身份信息），拒绝走独立事务写 `school.user.roles.denied`（只落判据码与 UUID），版本冲突只回 409 + 最新绑定快照、不写拒绝审计；同批收口 TD-199：删除预览/执行改**影响面指纹** `sha256(role_id|排序后的持有人 id 集)`，不符回 409 `ROLE_DELETE_PREVIEW_STALE` 并回带最新预览，执行侧重算指纹前对持有人行取 `FOR UPDATE`（预览不锁），锁序 `roles → user_roles` 与绑定的 `users → roles → user_roles` 无环；TD-194：`_guard_self_grant` 成为唯一入口（权限替换/范围替换/`reset_role` 权限+范围共 4 处判定），`reset_role` payload 增 `self_grant_ack`；`RoleResponse.bindable` 由 `role_is_bindable` 单点派生。三轮独立审查 2M/1m → 1M/2m → 2M/4m 全部整改（首跑 security 抓到 `admin.py` B608 动态 SQL 拼接，改静态分支后复跑绿）；本地受影响 7 文件 **63 passed**，PR CI backend **778 passed** + security 绿。
- **M2-UI3 第五批：权限账号停用/启用（TD-187e，2026-10-29，PR #12 合并提交 `0918386`）**：`POST /api/v1/school/users/{user_id}/deactivate` 与 `/activate`（读写同码 `school.permission_account.manage`）——停用**必须填理由 ≥10 字**（启用不需要），判据顺序 realm 403 → 理由 422 `DEACTIVATE_REASON_REQUIRED` → 自停用 409 `ACCOUNT_SELF_DEACTIVATE` → **无锁本租归属探针 404**（探针跑在自己的短事务里，未命中的洪水请求全程只占 1 条连接）→ `acquire_user_lock` → `users … FOR UPDATE` → 已删除/跨租/不存在 404 → 目标持超管 403 `ACCOUNT_SUPER_ADMIN_FORBIDDEN` → 目标类型不是教师/校方管理员 422 `ACCOUNT_TARGET_NOT_ALLOWED`（启停两向同码）→ 状态机 409（`ACCOUNT_ALREADY_DISABLED` / `ACCOUNT_NOT_DISABLED` / `ACCOUNT_ALREADY_ACTIVE`）；三条用户裁定：**超管账号一律不由租户侧启停**（平台侧兜底入口待做，TD-224）、**不能停用自己**、**停用需理由 ≥10 字**。停用把该账号**活跃**的刷新家族（`revoked_at is null and expires_at > now()`，SELECT 与 UPDATE 谓词逐字一致）置 `admin_revoke`、`token_version+1`、清 Redis 会话标记，旧 access 下一请求 401、旧 refresh 401、重新登录 401「账号不可用」；**已过期的死家族不撤销也不计入 `sessions_revoked`**（"这次断了几台设备"不虚报）。成功写 `school.user.deactivated`/`school.user.activated`（前后状态 + 撤销条数 + 理由，绑定集不因启停改变、`roles_version` 不变），四类拒绝走独立事务写 `school.user.status_denied`/`school.user.mfa_reset_denied`（只落判据码，不泄露存在性）；启用只回 `disabled → active`，其余状态 409、已删除/跨租/不存在 404，并清零失败登录计数与锁定窗口。同批把 CI 补上**迁移漂移检查**（`backend.yml` 新增「迁移漂移检查（TD-223）」步骤，`upgrade head` 后 `alembic check`，首跑即产出 `No new upgrade operations detected.`）。九轮独立审查（第六轮 0B/1M/6m、第七轮 0B/1M/3m、第八轮 0B/3M/9m、第九轮 0B/0M/4m 全部整改，含 1 处两个真相源修复：状态字符串改由状态机返回值参数化写库），7 组反证/摘锁实测证明跨租探针、advisory lock、探针短事务、Redis 降级各自承重；本地本点三文件 **22 passed**（9 单元 / 10 集成 / 3 业务场景，75.6s）、受影响 9 文件 **61 passed**（160.6s），PR CI backend **800 passed**（四包分支覆盖率 79.46 / 81.15 / 81.25 / 66.67%，门槛 73/80/81/66 全过）+ security 绿。

- **质量与安全运维 P0（2026-10-28）**：关键包（rbac/auth/tenant/audit）**分支覆盖率门槛**——CI 实测四包 73.87 / 80.95 / 81.25 / 66.67%（auth 读数在 407–408/504 间漂移：同一 commit `8a5a2d3` 两次 run 为 408 与 407，校准提交 PR run 408、push 与 main run 407；阈值按最低观测取整为 73/80/81/66，TD-221），阈值写在 `backend/scripts/check_coverage.py`，`backend.yml` 与 `scripts/ci.sh` 产出 `coverage.json` 后逐包比较（`--cov-fail-under` 只看 TOTAL，不用；失败时打印逐文件缺口并上传产物）；`security` 工作流覆盖面向 main 的 PR 与 main 推送；Dependabot 包生态只开安全更新、Actions 周更分组 minor/patch（**仓库级 alerts/security updates 已于 2026-10-28 开启**）；`GET /metrics`（`METRICS_ENABLED` 默认 false，私有注册表只输出 `campus_*` 计数器，标签有界低敏）+ 4 类 5 条 Prometheus 告警规则（`deploy/observability/prometheus/alerts.yml`，接入随 TD-214）。两轮独立审查 1B/7M/5m + 0B/0M/8m 与覆盖率校准两轮 1M/5m + 1M/5m 全部处置；本地全量后端 **755 passed/1573s**，PR #1 远端 CI **761 passed/745.95s**。TD-216（标签值收敛）已登记 M3；提交 `d29e243` + `ba35fb5` + `ae8fa9c`。

当前门禁（最近一个功能点＝TD-187g-C 前端权限账号页；2026-10-30 合并后回填，这里只留一组当前读数，历史批次读数在 `docs/review-log.md`）：

- **PR #22 远端 CI**（纯前端 PR，路径过滤只跑 frontend + security 两条）：frontend run `41025989067` 单测 api-client **67 passed** + admin **96 passed** → `pnpm build` → Playwright **96 passed / 2.1m**；security run `41025989092` success；`backend` / `evals` 因路径过滤未触发
- **合并后 `main`（tip `fb6513d`）推送 CI**：frontend run `41026581470` success（单测 api-client 67 + admin 96、Playwright **96 passed / 1.5m**，两个 job 逐条与 PR run 一致）；security run `41026581647` success
- **后端最近一次读数**（不是本点——TD-187g-A 是最后一个改后端的批次，PR #18 合并后**当时的** main tip 是 `8936c3f`；当前 main tip 已是 `fb6513d`）：backend run `40943258297` **825 passed / 823.32s（13:43）**，关键包分支覆盖率 `app/rbac` 79.92%（211/264，门槛 73）、`app/auth` **81.42%**（412/506，门槛 80）、`app/tenant` 81.25%（13/16，门槛 81）、`app/audit` 66.67%（4/6，门槛 66）全部通过，「迁移漂移检查（TD-223）」打印 `No new upgrade operations detected.`；迁移 head `a1c3e5f7b9d2`（本点无新迁移）。**`app/auth` 距 82% 目标还缺 3 条分支**（412/506，82% 需 415 条；TD-221/234）
- **点级本地门禁（TD-187g-C，2026-10-30 实测）**：`pnpm build` / `pnpm lint` / `pnpm format:check` / `pnpm typecheck` 全绿；单测 admin **96 passed**（本点新增 `account-logic.test.ts` 29 条）+ api-client **67 passed**；`npx playwright test`（先 build）**96 passed / exit 0**，本点新增 `admin-permission-accounts.spec.ts` 15 条。**跑法坑（TD-254，本点再次踩到）**：Playwright 的 `webServer` 服务 `dist/`，改完 `.vue` 不重新 build 就手敲 `npx playwright test`，测的是上一个产物
- **前端规模（2026-10-30）**：单测 **163**（api-client 67 + admin 96）、mock E2E **96** 条（`frontend/e2e/`，全部走 `page.route()` 有状态 mock，见 TD-253「mock 判据是手抄的，抄错不会红」）。**12 条全栈 E2E 仍是 M2-UI2 时点的数字**，M2-UI1/UI2/UI3 新增用例因本机 8000 端口被另一项目容器占用待补跑（TD-183/192）
- 覆盖率门槛脚本 `backend/scripts/check_coverage.py`（`--cov-fail-under` 只看 TOTAL，不用）；取用例数要加 `-o addopts=""`；每个功能点一个 PR，受影响侧的全量套件由该 PR 的远端 CI 跑，合并条件＝CI 全绿 + 独立审查无 Blocker/Major

进行中/待做：

- **M2 收尾三件**（M2-UI3 十批已全部合入 `main`，最后一个功能点是 **TD-187g-C 前端权限账号页**，PR #22 → 合并提交 `fb6513d`）：① **TD-183 / TD-192 全栈 E2E 补跑**——本机 8000 端口被另一项目容器占用（不可强杀），`bash scripts/e2e-stack.sh up` 后按 `docs/m1-acceptance.md` §6 跑；② **M2 边界评审**——`docs/tech-debt.md` 台账逐条过处置，重点 TD-233（台账外同类全表口径）与 TD-254（前端 E2E 跑法口径）；③ **`app/auth` 分支覆盖率抬到 82%**（现 81.42%＝412/506，82% 需 415 条、**缺 3 条分支**；先把 TD-234 的 CI/本地口径差定性）。**M3/M4**（知识库闭环、AI 与 C 端）未开始；SC-01 变更已把 ORG-05（部门/科室）、KB-09（文档查阅人群 ACL）、KB-10（切块编辑与派生重建）、AI-10（身份→人群过滤）写入计划。

- **M2-UI3 第七批：审计字段 `sessions_revoked` 收口（TD-231，2026-10-29，PR #16 → 合并提交 `0ffdb2d`）**：整数键 `sessions_revoked` **全仓只写整数**，含义统一为「本次真正断开的**活跃**会话家族数」（未撤销且未过期，与设备列表 `list_sessions` 取数一致）——租户 MFA 重置、平台 MFA 重置、密码重置三条链路的取家族 SELECT 都加了 `and expires_at > now()`（**撤销语句保持全量未撤销**：把已过期的死行一并标上撤销不花钱，计数准就行），与停用侧口径对齐；自助链路 4 个动作（`auth.mfa.activated`/`auth.mfa.disabled`/`auth.password.changed`/`auth.password.reset`）的布尔语义改用新键 **`sessions_all_revoked`**，`auth.password.reset` 原先另开的并行整数键 `sessions_revoked_count` 并入同名整数键后消失——改名前布尔与整数混写同一个键，巡检 SQL `sum((metadata_json->>'sessions_revoked')::int)` 在布尔行上直接报错（PG 不能把 `true` cast 成 int）。四条「过期家族不计入」用例补齐（租户 MFA 重置 / 平台 MFA 重置 / 租户密码重置 / **平台密码重置**——最后一条是独立审查的唯一阻塞项：审查实测「只把平台侧 SQL 改回旧写法，全部 57 条相关用例照样全绿」，即平台侧那处收窄改错了也没有用例会红），且用 `type(...) is int` 钉住类型——`True == 1` 在 Python 成立，只断言值会让布尔蒙混过关。口径与键名对照表见 `docs/design.md` §5.12。**遗留**（TD-235，已延后到 M3）：自助链路另外 3 个动作只写布尔不写条数，真要做跨动作设备数对比时才补。

- **M2-UI3 第八批：前端页面的后端前置契约（TD-187g-A，2026-10-30，PR #18 → 合并提交 `8936c3f`）**：**纯后端，不含任何前端代码**。用户 2026-10-30 三项拍板——① 权限账号页列账号用**新增列出接口** `GET /school/permission-accounts`（不复用已交付的 `GET /school/users/lookup`）；② 两个权限码（`school.role.manage` vs `school.permission_account.manage`）用**新增只列角色的接口** `GET /school/bindable-roles`（**不含 `permission_codes`**——合并会让只持账号管理权的人拿到全校权限矩阵）；③ TD-187g **拆 3 个 PR**（A 后端契约补齐 / B 前端角色页 / C 前端权限账号页）。落地：迁移 `a1c3e5f7b9d2` 给 `roles` 加 `ck_roles_name_not_blank`（TD-198；加约束**前**先查存量违例、违例即中止；因 `roles` 已 FORCE ROW LEVEL SECURITY，迁移先断言连接能绕 RLS，否则存量计数被遮蔽成 0、预检形同虚设）；`RoleResponse` 增服务端派生的 **`resettable`**（TD-200，判据 = 「有出厂定义」**且**「角色可编辑」，**两道门都算**——只算前者会让校方超管/学生/教师这三个内置锁定角色按钮亮着、点了恒 409）；`GET /school/permission-accounts`（cursor 分页 + 只列教师/校方管理员 + 不按人员范围裁剪 + **不回显 `login_id`** + 每行带服务端派生的 `is_super_admin`，TD-242）；`GET /school/bindable-roles`（只给 `id/code/name/is_system`，超管角色不列——列出来等于能勾不能存）；`create_role` 直调时 `name.strip()` 为空即 422（不落库、不撞 CHECK 变 500）；共享游标解码器 `decode_cursor` 拒绝**无时区**时间戳（TD-243——手造一个未来时刻的游标会让 keyset 条件恒真、翻页**静默回到第一页**，现在明确 400）；顺带修掉 `reset_client()` 跨事件循环关全局 Redis 客户端的**存量**报错（TD-246：只吞「事件循环已关」这一种、其它照抛，并补 5 条回归网）。两轮独立审查 1M/9m → 0B/0M/7m 全部整改；本地受影响 28 文件 **207 passed** + ruff/mypy/bandit×2/alembic check 全绿；PR CI backend **825 passed**（首轮曾因 `app/auth` 覆盖率掉到 410/506=81.03% 被自查拦下，补回归网后回到 412/506）、security 绿；合并后 main（tip `8936c3f`）推送 CI backend **825 passed / 823.32s（13:43）**、security 绿，四包分支覆盖率与 PR run **逐包一致**（`app/rbac` 79.92%（211/264，门槛 73）、`app/auth` **81.42%**（412/506，门槛 80）、`app/tenant` 81.25%（13/16，门槛 81）、`app/audit` 66.67%（4/6，门槛 66））。
- **M2-UI3 第九批：前端角色页（TD-187g-B，2026-10-30，PR #20 → 合并提交 `7606650`）**：**纯前端，`backend/` 零改动**。用户 2026-10-30 四项拍板（都选推荐项）——B1 可手工授的数据范围类型清单**前端自己写一份**（后端 `_assert_editable_scope_types` 仍是唯一门禁，复刻只影响可选项准确性，不构成安全缺口，登记 TD-247）；B2 `code`/`name` 格式校验**前端先查一遍**（后端那份照旧是最终门禁，登记 TD-248）；B3 新建角色的初始权限**二选一单选**（「空白（稍后配置）」/「复制已有角色」，权限勾选框只在空白模式可用——后端 `RoleCreatePayload` 就是二选一，两份非空同时给会 422 `ROLE_INITIAL_SOURCE_AMBIGUOUS`，界面不能表达出后端拒绝的组合）；B4 删除的**执行控件**按 `school.role.delete` 显示、**入口**按「是不是内置角色」给（方案 C「两个码都要才显示入口」被明确拒绝）。落地：`RoleCreateDialog.vue` + `RoleDeleteDialog.vue` + `RoleScopePanel.vue` + `RoleSettingsView.vue` + `rbac-logic.ts`（判据纯函数层）+ api-client 的 TD-187g-A 契约类型 + `e2e/helpers/rbac-mock.ts`（判据顺序按 `admin.py` 源码逐条复刻）。「恢复出厂」改跟服务端下发的 `resettable` 走（`is_system` 只剩「内置/自建」标签），复制源下拉过滤掉含不可转授码的角色，`ROLE_DELETE_PREVIEW_STALE` 按服务端回带的 `latest_preview` 刷新。收口 **TD-198 表单侧那半**与 **TD-200 前端那半**。独立审查 1 轮 **0B/0M/15m** 全部处置（两条 Minor 值得记：一条 E2E 用了后端造不出来的夹具 `is_system=true` 且 `editable=true`，测试绿着但钉的是假场景；一条 mock 的删除校验顺序与理由文案跟后端不同，按 mock 测出来的顺序设计界面会接真后端时对不上）；本地门禁 `pnpm typecheck` / `lint` / `format:check` 全绿，单测 api-client **61 passed**（`rbac.test.ts` 7→14）+ admin **67 passed**（`rbac-logic.test.ts` 20→56），`pnpm build` 成功后 `npx playwright test` **81 passed**（本点新增 `admin-role-pages.spec.ts` 19 条）；PR CI frontend run `40968666929`（3m08s：unit 61+67、build、Playwright **81 passed / 1.7m**）+ security run `40968666859` + `e2e` check-run 全 success，`backend`/`evals` 因路径过滤未触发（纯前端 PR，符合预期）；**合并后 main（tip `7606650`）推送 CI frontend run `40969485344`（2m25s，`81 passed (1.4m)`、单测 61+67，与 PR run 逐条一致）+ security run `40969485267` success**。**跑法坑（本点踩过）**：Playwright 的 `webServer` 服务的是 `dist/`，**改完 `.vue` 不重新 `pnpm build` 就跑 E2E，测的是上一个产物**——`pnpm test:e2e` 已串了 build，手动 `npx playwright test` 必须先自己 build。
- **M2-UI3 第十批：前端权限账号页（TD-187g-C，2026-10-30，PR #22 → 合并提交 `fb6513d`）**：**纯前端，`backend/` 零改动**。用户 2026-10-30 四张卡全按推荐拍板——C1 当前页本地过滤 + 「按登录名精确定位」框（只在持 `school.personnel.mfa.reset` 时出现，因为唯一能按登录名定位的端点是 `GET /school/users/lookup`，登记 TD-255）；C2 点行开抽屉且**打开时必发一次 `GET /users/{id}/roles`**（列表行的 `roles_version` 可能是旧快照，直接拿它做 CAS 会平白撞 409）；C5 账号页加 MFA 重置区 + 角色页旧卡片留着（TD-257）；C6 理由 ≥10 字前端先拦 + 一次确认（**不做逐字输入姓名**：停用可逆、且 `display_name` 允许为 null）。C3（绑定区以「当前持有」为基底）与 C4（超管 / 「这行是我」不给写控件）由契约事实与既定口径唯一确定，未单独问。落地：api-client 6 方法 + 7 类型（**`activateUser` 不发请求体**、三个写动作按 204 返回 `undefined`）；`account-logic.ts` 判据纯函数层（`accountReasonError` / **`bindingPlan`**——`submitted` = 勾选 ∪「已持有但 `bindable=false`」，**PUT 是全量替换，漏一行就是静默收回权限** / `accountGates` 五挡：缺码→超管→是自己→目标类型→状态机 / `filterAccountsLocally` / `statusLabel`+`typeLabel` 未知档位显示原码而不是默认「启用」）；`PermissionAccountsView.vue`（游标栈翻页、「还有下一页」只认 `next_cursor`、写动作成功后重读当前页不手改本地 `status`）+ `AccountDetailDrawer.vue`（409 采用服务端 `latest`、MFA 区按权限码整块出现或不出现、`display_name=null` 统一成「未命名账号（ID 尾号 xxx）」）+ `error-toast.ts`（`reportError`：403 交全局、不再叠第二条，TD-258）；`router` 加 `settings/accounts`（`meta.permission="school.permission_account.manage"`）与菜单项；`rbac-mock.ts` 新增 5 条路由 / 6 个端点且**有状态**，判据顺序逐条注释指向后端行号（TD-253）；新增 `admin-permission-accounts.spec.ts` 15 条 + `account-logic.test.ts` 29 条。三轮独立审查 **0B/4M/7m → 1B/1M/3m → 复审 5 项到位 + 2m**，结论「可以提交」；最重的一条：mock 的 `ROLE_NOT_BINDABLE` 判据抄成 `bindable` 字段（后端按**码**判，`backend/app/rbac/binding.py:536`），第 1 轮记 Major、第 2 轮升级 Blocker——抄错让 mock **比后端更严**，旗舰用例在 mock 世界里其实保存失败、只断到请求体；修法是 `roleIsBindedByCode()` + 补「保存成功后重读列表仍含该角色」的**服务端状态断言**。收口 **TD-242**（超管行四个写动作全不给 + 判据只认服务端 `is_super_admin`；单测刻意造「`is_super_admin=true` 但 `role_codes=["teacher"]`」的行，防止前端按码硬认时「用例绿、真页面错」）。**残留 3 条有意不改**：TD-259（理由长度按 UTF-16 码元，emoji 前端放行、后端 422，mock 抄同一错法测不出）、TD-260（列表加载失败仍显示上一次成功的数据）、TD-261（`accountGates` 的「超管/是我」两挡顺序与后端相反，只影响提示语句）。本批为 M2-UI3 收尾，**M2 只剩 TD-183/192 全栈 E2E、边界评审、`app/auth` 抬 82% 三件**。

## 技术栈（已锁定）

- Python 3.13.x、FastAPI、SQLAlchemy 2 async、Alembic
- LangGraph + 自定义 Harness（LangGraph 只做图执行）
- PostgreSQL 16、Qdrant、Redis 7.4、MinIO
- MinerU 4.0.12（实际 tier 写入构建元数据；Standard→Basic→Flash 降级链在 M3 执行器落地）
- BGE-M3 dense + jieba/BM25 sparse + bge-reranker-v2-m3（TEI CPU）
- Vue 3 + TypeScript + Vite；管理端 Element Plus，C 端 Vant 4（移动端 H5）
- Langfuse Server 4.63.0 + Python SDK 5.0.2（生产独立主机自托管，开发可选）
- Docker Compose + Nginx + HTTPS + GitHub Actions

## 文档索引

| 文档 | 内容 |
|---|---|
| `docs/requirements.md` | 整理后的需求基线、编号、矛盾标注 |
| `docs/spec.md` | 功能点、Given/When/Then 验收标准、决策记录、留存矩阵 |
| `docs/design.md` | 架构、API/SSE 契约、数据模型、RBAC、状态机、隔离、RAG、Langfuse |
| `docs/milestone.md` | M0–M6 里程碑、范围、风险、DoD |
| `docs/test-plan.md` | 测试分层、业务场景矩阵、A23 隔离场景 |
| `docs/state-machines.md` | 知识库、请假、账号删除、审批、消息等状态机 |
| `docs/rbac-matrix.md` | 角色权限矩阵与数据范围 |
| `docs/tenant-isolation.md` | 多租户隔离规范与测试 |
| `docs/compliance.md` | 数据留存、PII、删除证明、Langfuse 合规 |
| `docs/observability-langfuse.md` | Langfuse 部署、采集场景、脱敏、Prompt 治理 |
| `docs/deployment.md` | 环境、Compose、Nginx、HTTPS、备份、Langfuse 独立主机 |
| `docs/code-review-checklist.md` | 代码审查清单与分级 |
| `docs/review-log.md` | 功能点审查记录模板 |
| `docs/tech-debt.md` | 更好做法/技术债台账与闭环处置 |
| `docs/design-briefs/` | 实现前设计简报（候选方案、选择理由、测试策略） |
| `evals/` | LLM 评估集、rubric、runner 设计 |
| `benchmarks/` | k6/locust 压测脚本与报告规范 |

## 本地环境（已核对）

- Windows 11，16 核 CPU / 16GB 内存 / RTX 4050 Laptop 6GB
- WSL2 + Docker Desktop（当前 Docker 引擎需启动）
- 默认 Python 3.11.9，使用 uv 安装并锁定 Python 3.13.x
- Node 24.11 / pnpm 11 可用
- Playwright 未安装，M0 安装

## 安全红线

- `.env` 已有真实模型密钥，**绝对不要提交**；仓库只提交 `.env.example`。
- 禁止提交 API Key、密码、私钥、数据库连接串。
- 禁止 `git add .` / `git add -A`；提交前必须 `git status` + `git diff --staged`。
- 禁止向共享分支 force push、直接 push；禁止 `git reset --hard`、`git clean -fd`。
- 生产环境 `DEV_SKIP_SCHOOL_REVIEW`、`DEV_SKIP_FILE_SCAN` 必须为 `false`。

## 下一步

1. 用户确认 `docs/` 下的第二阶段文档。
2. 确认后进入 M0：仓库初始化、Compose core、FastAPI/Worker 骨架、Harness contracts、MinerU/Langfuse 兼容性验证、测试基础设施。
3. 按 M0 → M1→ … 顺序逐功能点 TDD；每个功能点执行“测试 → 代码审查 → 修复 → 复测 → 复审 → 提交”。
