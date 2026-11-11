# 接手手册（Handoff）— 校园陪伴精灵

> 用途：**任何新的 AI 编码工具或新会话接手本项目时，先读本文件**，再读 `AGENTS.md`（规则源头）。
> 本文件只写"状态 + 怎么跑 + 怎么交接"，规则细节一律以 `AGENTS.md` 为准；两者冲突时以 `AGENTS.md` 与用户最新指令为准。
> 维护要求：**每个功能点完成后必须更新本文件的 §1 状态表与 README 的「当前状态」**，避免交接信息过期。

---

## 1. 当前状态（截至 2026-11-10）

| 里程碑 | 功能点 | 状态 |
|---|---|---|
| M0 基座 | 全部（M0-A~M0-F） | ✅ 完成，验收报告 `docs/m0-acceptance.md` |
| M1 租户与身份 | M1-A 租户基座（RLS/三连接/仓储/上下文/中间件） | ✅ 完成（两轮审查通过） |
| M1 | M1-B 学校注册与平台审核（TEN-01/02/03） | ✅ 完成（五轮审查通过） |
| M1 | M1-C 三端登录与令牌**后端**（AUTH-01/02） | ✅ 完成（三轮审查通过） |
| M1 | M1-C-UI 三端登录页/强制改密页/守卫/HttpOnly 刷新 Cookie | ✅ 完成（两轮审查通过） |
| M1 | M1-C2 会话设备管理（AUTH-06，策略 C） | ✅ 完成（五轮审查通过；设备限额/列表/下线/退出其它/被踢即时失效；简报 `docs/design-briefs/M1-C2-session-devices.md`） |
| M1 | M1-D1 密码策略/历史/邮件重置、失败锁定（AUTH-03 员工/平台部分、AUTH-05） | ✅ 已完成（第五轮复审通过、348 passed、远端 CI 绿；简报 `docs/design-briefs/M1-D1-password-lockout.md`） |
| M1 | M1-D2 MFA（AUTH-04） | ✅ 完成（两轮审查通过、第二轮 0B/0M、后端 411 passed、前端 32 单测 + 28 E2E、远端 CI backend/frontend/security 全绿；简报 `docs/design-briefs/M1-D2-mfa.md`） |
| M1 | M1-E1 鉴权内核：权限矩阵 59 码落库 + 8 内置角色出厂默认 + 接口声明权限（RBAC-01 部分） | ✅ 完成（第五轮复审通过 0B/0M/3 Minor；后端 481 passed、ruff/mypy/alembic check/bandit/pip-audit 本地全绿、head=`e5f7a9c1b3d5`；远端 CI backend #39851973194 / security #39851973099 success；逐轮记录见 `docs/review-log.md`，简报 `docs/design-briefs/M1-E1-rbac-core.md`） |
| M1 | M1-E2 RBAC 数据范围与服务端过滤、审批职责分离、平台跨租只读（RBAC-02/03，含 TD-149 触发器收紧） | ✅ 完成（两轮审查：第一轮 0B/3M/4M 全部修复、第二轮主上下文 0B/0M（当日独立审查子智能体配额耗尽）；后端 561 passed、ruff/mypy 149 文件/alembic check/bandit×2 本地全绿、head=`f7a9b1c3d5e8`；远端 CI backend #39950816551 / security #39950816785 success；逐轮记录见 `docs/review-log.md`，简报 `docs/design-briefs/M1-E2-data-scope.md`） |
| M1 | M1-F 审计哈希链（AUD-01，含 TD-068/091 收口） | ✅ 完成（第三轮复审通过 0B/0M，review-log 2026-10-24） |
| M1 | M1 验收报告（含 TD-110 全栈 E2E） | ✅ 完成（`docs/m1-acceptance.md`；12 例全栈 E2E 全绿 + 28 mock 回归 + 后端 572 passed；边界 TD 台账 171 条全处置） |
| M2 | M2-ORG01-03 人员档案与组织关系（ORG-01/03 + TD-151 class 数据范围 + TD-070 cursor 分页） | ✅ 完成（三轮独立审查通过 0B/0M；迁移 `c9d5f1e3a7b9` 五表 + RLS；简报 `docs/design-briefs/M2-ORG01-03.md`） |
| M2 | M2-ORG02 CSV/XLSX 异步导入（ORG-02 + TD-069 收口 + TD-174 对账验证） | ✅ 完成（三轮独立审查通过 0B/0M；迁移 `e2a6c8d4b0f2`；看门狗 cron 收口 stale running；简报 `docs/design-briefs/M2-ORG02-import.md`） |
| M2 | M2-ORG05 部门/科室树（A30：departments + 教师归属 + 导入扩展 + 调岗审计） | ✅ 完成（独立审查通过，review-log 已补录——findings 明细因会话轮替不可考，按补录口径登记；迁移 `a4c8e2f6b1d3`；简报 `docs/design-briefs/M2-ORG05-departments.md`） |
| M2 | M2-ORG04 身份证加密采集与受控查看（AES-GCM + HMAC 查重 + 掩码 + sensitive.read） | ✅ 完成（独立审查两轮：第一轮 2B/6M 全部修复、第二轮 0B/0M PASS；迁移 `f8b2d6a0c4e8` 指纹唯一索引；简报 `docs/design-briefs/M2-ORG04-id-card.md`） |
| M2 | M2-ORG06 入学年份与学年流转（班级模型修订） | ✅ 完成（独立审查两轮：第一轮 1M/5m 修复、第二轮 0B/0M PASS；迁移 `b9d3f7c1e5a9` classes 模型修订 + 流转批次表；简报 `docs/design-briefs/M2-ORG06-academic-year.md`） |
| M2 | M2-UI1 B 端管理页面（人员/受控查看/班级课程/部门树/导入/学年流转 + TD-141） | ✅ 完成（远端 CI frontend #40231010987 / security #40231010964 / backend #40229631514 success；3 条全栈 E2E 因本机 8000 端口被占未跑，登记 TD-183（**2026-10-31 已补跑，18 例全绿**）；简报 `docs/design-briefs/M2-UI1-b-end-pages.md`） |
| M2 | M2-UI2 角色权限配置后台（TD-150 `defaults_applied` + TD-137 角色↔权限接口/页 + TD-143 重置出厂 + TD-126 MFA 重置；权限目录 59→60 码） | ✅ 完成（本地门禁全绿：后端 661 passed / 前端 75 单测 + 57 mock E2E + build / ruff + mypy + alembic check + bandit×2；两轮独立审查 0B/2M/6m → 复审 PASS 0B/0M/6m，见 `docs/review-log.md`；远端 CI backend #40316861186 / frontend #40316861247 / security #40318461683 success（security 首跑被 gitleaks 对测试种子口令误报，按 TD-052 精确白名单修复后重跑绿）；UI 侧真实后端全栈 E2E 因本机 8000 端口被占未跑，登记 TD-192（**2026-10-31 已补跑，三条链路全绿**）；简报 `docs/design-briefs/M2-UI2-rbac-admin.md`，迁移 head `c3f7a9d1e5b2`） |
| M2 | M2-UI3 自定义角色 / 多角色绑定 / 权限账号 | ✅ 完成（十批全部合入 `main`；用户 2026-10-26 决策 D2 从 M2-UI2 裁出；简报 `docs/design-briefs/M2-UI3-custom-roles-accounts.md`，§11 六项决策已逐条拍板）。**第一批已交付**：TD-187a（`roles` 两条 CHECK + 迁移 `d7a1c3e5b9f4` + 新码 `school.role.delete`，目录 60→61）+ TD-191 打断层（`app/rbac/self_grant.py` 判据 + `PUT /school/roles/{id}/permissions` 接线：结果集触达本人且净增敏感码须 `self_grant_ack`+理由，超管免打断不免留痕，`school.role.self_grant` 放行与打断都写；前端确认条）——本地门禁全绿（后端 687 passed / ruff+mypy187+bandit×2+alembic check 无漂移 / 前端 lint+format+typecheck+31 单测+14 mock E2E），独立审查 0B/1M/5m（Major=gitleaks 漏放行接线口令已补、Minor 注释与测试 uuid 已修，其余 TD-193~197 处置），见 `docs/review-log.md`。**第二批已交付（commit `f45a64a` 之后，本地门禁全绿、已随 PR #1 合入 main）**：TD-187b 自定义角色创建/删除——`POST /school/roles` + `POST /school/roles/{id}/delete/{preview,}`（删除走独立码 `school.role.delete`，逐字确认 + 理由 ≥10 + 版本 CAS，CASCADE 前把码集与受影响用户数快照进 `school.role.deleted`；自定义角色翻成可编辑/可删除/**不可重置出厂** `ROLE_DEFAULTS_UNAVAILABLE`，前端重置入口改跟 `is_system` 走）；三轮独立审查（0B/2M/9m：M-1 创建路径未接自扩权守卫**经用户 2026-10-27 裁定改判据口径**、M-1(三轮) 审查记录与 m-2 落点补齐；Minor 含并发兜底只认 `uq_roles_tenant_id_code`、删除返回被删行版本号而非 `+1`、`name` 全空格拦截、留痕口径与格式校验层数两处文档纠偏、跨租复制源留 `copy_from_role_id`），处置见 `docs/review-log.md` 与 TD-194/197/198/199/200/201；复测后端 **706 passed**（398 单元/145 集成/163 业务场景）+ ruff/mypy190/bandit×2 + 前端 lint/typecheck/format/31 单测/`admin-roles` E2E 15 passed。**第三批已交付（随 PR #1 合入 main；首跑 backend 曾因 auth 覆盖率红，校准后复跑绿）**：TD-187c 角色级数据范围编辑——`GET /school/roles/{id}/scopes`（服务端下发 `editable`）、`POST …/scopes/preview`（差异 + 持有人数 + `retained_non_editable` + `empties_scope`，只读不审计）、`PUT …/scopes`（判据顺序：`_require_tenant` → 未知类型 → 行锁取角色 → 可编辑 → 不可手工授 → 版本 CAS → 范围版自扩权守卫 → `version+1` → **窄删除**（只碰 `school`/`self`）→ `unnest` 插入 → `school.role.scopes.updated`）；可编辑集 `{school,self}`，手工授 `class`/`course` 一律拒授**并留安全审计**（真原因是本端点没有 `scope_id` 入参通道，写下去是缺 id 的脏行、解析层 `ValueError` 会让该角色每个持有人 500；`course` 另本期不贡献可见性）；敏感范围 `SENSITIVE_SCOPE_TYPES={school}` 与权限侧共用同一判据内核（`kind="scope"` 只改文案与审计 metadata），空清单合法并由预览提示"N 名持有人将看不到任何数据"；**重置出厂是这条判据的第 4 个入口、本点未接**（可达路径与收口时点见 TD-194），注册 `school_super_admin` 免打断不免留痕。三轮独立审查（0B/5M/14m，全部整改：约束名写错的死代码分支、对运营错误的拒授理由、`reset_role` docstring 与文档相反、接口层残留假陈述、范围并集查询副本无登记/无用例），处置见 `docs/review-log.md` 与 TD-194/195/202/203/204/205。本点复测：本点四文件 **51 passed**（23 单元 / 18 集成 / 5 库层不变量 / 5 业务场景）、RBAC 受影响子集 **192 passed**、ruff + mypy（192 文件 0 错误）+ bandit（app/alembic 两条）+ `alembic check` 无漂移；受影响侧的全量套件由 PR 的远端 CI 跑。**第四批已交付（2026-10-29，PR #9 合并提交 `9771be8`）**：TD-187d 用户↔多角色绑定——`GET/PUT /api/v1/school/users/{user_id}/roles`（`school.permission_account.manage`，读写同码）全量替换 + `users.roles_version` CAS（迁移 `e9b4d2f6a8c1`）；自绑禁令先于对目标账号的任何读取与状态写入（只比较 `Principal` 与路径参数；拒绝审计另起事务，403 `BINDING_SELF_FORBIDDEN`），目标限 `teacher`/`school_admin`、目标持超管 403、提交超管角色 422、空集合法、跨租与 `deleted` 404（`locked`/`disabled` 可绑）；绑定下一请求生效、不作废会话；成功写 `school.user.roles.updated`（角色/权限码前后差集 + 理由），拒绝走独立事务写 `school.user.roles.denied`（只落判据码），版本冲突只回 409 + 最新快照；同批收口 **TD-199**（删除预览/执行改影响面指纹 `sha256(role_id\|排序持有人)`，409 `ROLE_DELETE_PREVIEW_STALE` 回带最新预览；执行侧重算指纹前 `FOR UPDATE` 锁持有人行，预览不锁）与 **TD-194**（`_guard_self_grant` 唯一入口，`reset_role` 同时接权限/范围两维、payload 增 `self_grant_ack`）；`RoleResponse.bindable` 由 `role_is_bindable` 单点派生。三轮独立审查 2M/1m → 1M/2m → 2M/4m 全部整改（PR 首跑被 security 抓到 `admin.py` 动态 SQL 拼接 B608，改静态分支后复跑绿）；本地受影响 7 文件 **63 passed**，PR CI backend **778 passed**（覆盖率 79.46/80.75/81.25/66.67，门槛 73/80/81/66 全过）+ security 绿。**第五批已交付（2026-10-29，PR #12 合并提交 `0918386`）**：TD-187e 权限账号停用/启用——`POST /api/v1/school/users/{user_id}/deactivate` 与 `/activate`（`school.permission_account.manage`，与绑定读写同码）；判据顺序 realm 403 → 停用理由 ≥10 字 422 `DEACTIVATE_REASON_REQUIRED` → 不能停用自己 409 `ACCOUNT_SELF_DEACTIVATE` → **无锁本租归属探针 404**（跑在自己的短事务里，未命中的洪水请求峰值只占 1 条连接）→ `acquire_user_lock` → `users … FOR UPDATE` → 已删除/跨租/不存在 404 → 目标持超管 403 `ACCOUNT_SUPER_ADMIN_FORBIDDEN` → 目标类型限教师/校方管理员 422 `ACCOUNT_TARGET_NOT_ALLOWED`（启停两向同码）→ 状态机 409；用户 2026-10-29 三条裁定落地（超管一律不由租户侧启停、不能停用自己、停用需理由 ≥10 字；平台侧停用超管入口待做 TD-224）。停用把**活跃**刷新家族（`revoked_at is null and expires_at > now()`，SELECT/UPDATE 谓词逐字一致）置 `admin_revoke` + `token_version+1` + 清 Redis 会话标记，过期死家族不撤销也不计入 `sessions_revoked`；启用只接受 `disabled → active` 并清零失败登录计数，绑定集与 `roles_version` 不因启停改变。成功审计 `school.user.deactivated`/`school.user.activated`（前后状态、撤销条数、理由），四类拒绝另起独立事务写 `school.user.status_denied`（MFA 侧 `school.user.mfa_reset_denied`）同形 404/403 不泄露存在性；同批把 CI 补上「迁移漂移检查（TD-223）」步骤（`upgrade head` 后 `alembic check`）。九轮独立审查（第六~九轮 1M/6m → 1M/3m → 3M/9m → 0M/4m 全部整改，含状态字符串两个真相源的实现修复），7 组反证实测证明跨租探针、advisory lock、两层锁、探针短事务、Redis 降级各自承重；本地本点三文件 **22 passed**（9 单元 / 10 集成 / 3 业务场景，75.6s）+ 受影响 9 文件 **61 passed**（160.6s），PR CI backend **800 passed**（四包分支覆盖率 79.46/81.15/81.25/66.67%，门槛 73/80/81/66 全过）+ security 绿。**第六批已交付（2026-10-29，PR #14 合并提交 `dbec369`）**：TD-187f-A 测试与门禁补全——**`app/**` 零改动**，只改测试与门禁。T1（TD-221）：对同一 commit 重跑 CI 并逐分支 diff 两份 `coverage.json` 产物，定位摇摆源是 `app/auth/service.py` 的 `2046 → 2047` 弧（`activate_mfa` 待激活密文解不开 → 审计 + 500 的 fail-closed 分支，原先只靠别的用例顺路覆盖），补确定性用例 `test_activation_fails_closed_when_pending_secret_cannot_be_decrypted` 独占该分支；此后 **PR run `40862216277` 与合并后 main run `40864488603` 读数逐条分支完全一致**（`app/auth` 均 410/504 = 81.35%，门槛 80% 过，407/408/409 三种读数不再出现）；**目标 82 未达标**——82% 需 414 条分支、现 410，**还差 4 条**（且 CI 口径比本地少 1 条，见 TD-234），如实记录、门槛保持 80 不上调。T2（TD-205②）：新增 `test_cross_path_concurrent_permission_and_scope_edit_yields_one_writer`，钉住「权限 PUT 与范围 PUT 用同一 `version` 并发 → 恰好一个成功、`roles.version` 只 +1、409 的 `latest` 带赢家新值」（**新增即绿**，钉的是 187d 已实现的赢家唯一）。T3（TD-228）：4 个文件的审计查询按本用例租户收窄 + 并发用例失败路径 `task.cancel()` 兜底；边界写死——**「精确条数」按租户收窄、「必须为空」保留全表口径**（新增 `_audit_rows_all`/`_audits_all`），否则「写到错租户的那一行」会被自己过滤掉；`test_campus_chat_rbac.py` 判定不适用并写明理由。T4（TD-229）：`alembic/` 3 条 ruff 存量清零 + `alembic` 纳入 `backend.yml` 与 `scripts/ci.sh` 的 ruff 步骤。T5（TD-230）：MFA 侧重置补 `test_mfa_reset_cross_tenant_id_does_not_take_their_account_lock`（别租 id 不得排进对方账号锁队列，与停用侧对称）。另登记 **TD-233**（台账外同类全表口径文件，已延后到 M2 边界评审）与 **TD-234**（CI 与本地分支归因差一条，已延后到抬 82% 之前）。独立审查 0 Blocker / 2 Major / 3 Minor 全部整改，见 `docs/review-log.md`；本地受影响 10 文件 **104 passed**（206.33s）+ 迁移相关 5 文件 **16 passed**（113.37s）+ ruff/mypy（205 文件 0 错误）/bandit×2/alembic check 无漂移，PR CI backend **803 passed**（811.30s）+ security 绿。**第七批已交付**（PR #16 → 合并提交 `0ffdb2d`；PR CI backend run `40907206389` **807 passed / 788.31s**、security run `40907206149` success；合并后 main（tip `0ffdb2d`）推送 CI backend run `40909787189` **807 passed / 805.66s**、security run `40909787157` success，四包分支覆盖率与 PR run 逐包一致；四包分支覆盖率 `app/rbac` 79.46%（205/258，门槛 73）、`app/auth` 81.35%（410/504，门槛 80）、`app/tenant` 81.25%（13/16，门槛 81）、`app/audit` 66.67%（4/6，门槛 66）全过）：TD-187f-B 审计字段收口（T6＝TD-231；用户 2026-10-29 拍板与 187f-A 拆成两个 PR——它要动 `mfa_admin.py`/`platform_admin.py`/`auth/service.py` 三个模块与两条已交付链路的既有用例，混进"纯测试补全"的 diff 里就分不清是哪处改动让读数变化；用户在三个候选方案里拍板走 **方案 A**——收窄自助链路共用的家族查询，让整数键在 4 个动作上口径统一，见 `docs/design-briefs/M2-UI3-custom-roles-accounts.md` §17）。落地：租户/平台 MFA 重置的取家族 SELECT 加 `expires_at > now()` 与停用侧对齐（**撤销 UPDATE 保持全量未撤销**——把死行一并标上撤销不花钱，计数准就行），各补一条"过期家族不计入"用例；`auth/service.py` 的 `_SQL[realm]["active_families"]` 收窄、`sessions_revoked_count` 合并回同名整数键、4 处布尔 `sessions_revoked` 改名 `sessions_all_revoked`，密码重置补一条计数 + 键名用例；`design.md` §5.12 改成"口径一致 + 键名对照表"。**顺带堵掉一个断言陷阱**：`True == 1` 在 Python 成立，只断言 `metadata["sessions_revoked"] == 1` 会让布尔值蒙混过关——三处新用例都加了 `type(...) is int` 钉住类型。**遗留**（登记 TD-235，已延后到 M3）：自助链路另外 3 个动作（MFA 激活/关闭/改密）只写布尔、不写条数，真要做跨动作设备数对比时才补。**第八批已交付**（2026-10-30，PR #18 → 合并提交 `8936c3f`；PR CI backend run `40940885369` **825 passed / 831.62s（13:51）**、security run `40940885474` success；合并后 main（tip `8936c3f`）推送 CI backend run `40943258297` **825 passed / 823.32s（13:43）**、security run `40943258341` success，四包分支覆盖率与 PR run 逐包一致；`app/rbac` 79.92%（211/264，门槛 73）、`app/auth` **81.42%**（412/506，门槛 80）、`app/tenant` 81.25%（13/16，门槛 81）、`app/audit` 66.67%（4/6，门槛 66）全过）：**TD-187g-A 前端页面的后端前置契约——纯后端，不含任何前端代码**。用户 2026-10-30 三项拍板：① 权限账号页列账号用**新增列出接口** `GET /school/permission-accounts`（不复用已交付的 `GET /school/users/lookup`）；② 两个权限码（`school.role.manage` vs `school.permission_account.manage`）用**新增只列角色的接口** `GET /school/bindable-roles`（**不含 `permission_codes`**——两份目录合并会让只持账号管理权的人拿到全校权限矩阵）；③ TD-187g **拆 3 个 PR**（A 后端契约补齐 / B 前端角色页 / C 前端权限账号页）。落地：迁移 `a1c3e5f7b9d2` 给 `roles` 加 `ck_roles_name_not_blank`（TD-198；加约束**前**先查存量违例、违例即中止；因 `roles` 已 FORCE ROW LEVEL SECURITY，迁移先断言连接能绕 RLS——否则存量计数被遮蔽成 0、预检形同虚设；同款写法见 `c4d6e8f0a2b3`）；`RoleResponse` 增服务端派生的 **`resettable`**（TD-200，判据 = 「有出厂定义」**且**「角色可编辑」，**两道门都算**——只算前者会让校方超管/学生/教师这三个内置锁定角色按钮亮着、点了恒 409 `ROLE_IMMUTABLE`）；`GET /school/permission-accounts`（cursor 分页 + 只列教师/校方管理员 + **不回显 `login_id`** + 不按人员数据范围裁剪（租户级操作，TD-237）+ 每行带服务端派生的 `is_super_admin`（TD-242））；`GET /school/bindable-roles`（只给 `id/code/name/is_system`，**超管角色不列**——列出来等于能勾不能存）；`create_role` 直调时 `name.strip()` 为空即 422 `ROLE_NAME_NOT_BLANK`（不落库、不撞 CHECK 变 500）；共享游标解码器 `decode_cursor` 拒绝**无时区**时间戳（TD-243——手造一个未来时刻的游标会让 keyset 条件恒真、翻页**静默回到第一页**，现在明确 400「分页游标无效」）；顺带修掉 `reset_client()` 跨事件循环关全局 Redis 客户端的**存量**报错（TD-246：先把全局引用摘掉再关，关不掉时**只吞**「事件循环已关」这一种、其它异常照抛不掩盖，并补 5 条回归网 + 两条摘改反证）。两轮独立审查 1M/9m → 0B/0M/7m 全部整改，见 `docs/review-log.md`；本地受影响 28 文件 **207 passed** + ruff（`All checks passed!`）+ mypy（207 文件 0 错误）/bandit×2/alembic check 无漂移，PR CI backend **825 passed**（首轮曾因 `app/auth` 掉到 410/506=81.03% 被自查拦下，补回归网后回到 412/506）+ security 绿。**第九批已交付**（2026-10-30，PR #20 → 合并提交 `7606650`；PR CI frontend run `40968666929`（3m08s）、security run `40968666859` success，`backend`/`evals` 因路径过滤未触发；合并后 main（tip `7606650`）推送 CI frontend run `40969485344`（2m25s，单测 61+67、Playwright **81 passed / 1.4m**，与 PR run 逐条一致）+ security run `40969485267` success）：**TD-187g-B 前端角色页——纯前端，`backend/` 零改动**。用户 2026-10-30 四项拍板（都选推荐项）：B1 可手工授的数据范围类型清单**前端自己写一份**（后端 `_assert_editable_scope_types` 仍是唯一门禁）、B2 `code`/`name` 格式**前端先查一遍**、B3 新建角色初始权限**二选一单选**（「空白（稍后配置）」/「复制已有角色」，权限勾选框只在空白模式可用；后端两份非空同时给会 422 `ROLE_INITIAL_SOURCE_AMBIGUOUS`）、B4 删除的**执行控件**按 `school.role.delete` 显示而**入口**按「是不是内置角色」给（方案 C「两个码都要才显示入口」被拒）。落地：`RoleCreateDialog.vue`（含复制源过滤掉不可转授码的角色，不给「点得动、提交必 422」的选项）、`RoleDeleteDialog.vue`（逐字确认码 + 理由 ≥10 + 影响面预览 + 版本 CAS，`ROLE_DELETE_PREVIEW_STALE` 按回带的 `latest_preview` 刷新）、`RoleScopePanel.vue`（范围编辑 + 范围版自扩权确认）、`RoleSettingsView.vue`（「恢复出厂」改跟服务端 `resettable` 走，`is_system` 只剩「内置/自建」标签；锁定角色的理由由 `role-lock-reason` 一处承担）、`rbac-logic.ts`（判据纯函数层，界面不自己算）、api-client 的 TD-187g-A 契约类型、`e2e/helpers/rbac-mock.ts`（判据顺序按 `admin.py` 源码逐条复刻，让 mock E2E 验到「顺序」而不只是「结果」）。收口 **TD-198 表单侧那半**与 **TD-200 前端那半**。独立审查 1 轮 **0 Blocker / 0 Major / 15 Minor** 全部处置（按 §6.2 无需复审；含两条「测试绿着但钉的是假场景」——一条用了后端造不出来的 `is_system=true` 且 `editable=true` 夹具、一条 mock 的删除校验顺序与文案和后端不一致），见 `docs/review-log.md`；本地门禁 `typecheck`/`lint`/`format:check` 全绿、单测 api-client **61 passed**（`rbac.test.ts` 7→14）+ admin **67 passed**（`rbac-logic.test.ts` 20→56）、`npx playwright test` **81 passed**（新增 `admin-role-pages.spec.ts` 19 条；E2E 62→81、单测 85→128）。**跑法坑（已踩）**：Playwright 的 `webServer` 指向 `dist/`，改完 `.vue` 不重新 `pnpm build` 就跑 E2E，测的是上一个产物。**第十批已交付**（2026-10-30，PR #22 → 合并提交 `fb6513d`；PR CI frontend run `41025989067`（单测 67+96 → build → Playwright **96 passed / 2.1m**）+ security run `41025989092` success；合并后 main（tip `fb6513d`）推送 CI frontend run `41026581470`（单测 67+96、Playwright **96 passed / 1.5m**，两个 job 逐条与 PR run 一致）+ security run `41026581647` success；`backend`/`evals` 因路径过滤未触发）：**TD-187g-C 前端权限账号页——纯前端，`backend/` 零改动**。用户 2026-10-30 四张卡全按推荐拍板：C1 当前页本地过滤 + 「按登录名精确定位」框（只在持 `school.personnel.mfa.reset` 时出现，因为唯一能按登录名定位的端点是 `GET /school/users/lookup`——TD-255）、C2 点行开抽屉且**打开时必发一次 `GET /users/{id}/roles`**（列表行的 `roles_version` 可能是旧快照）、C5 账号页加 MFA 重置区 + 角色页旧卡片留着（TD-257）、C6 理由 ≥10 字前端先拦 + 一次确认（**不做逐字输入姓名**：停用可逆、且 `display_name` 允许为 null）；C3（绑定区以「当前持有」为基底）与 C4（超管 / 「这行是我」不给写控件）由契约事实与既定口径唯一确定、未单独问。落地：api-client 6 方法 + 7 类型（**`activateUser` 不发请求体**、三个写动作按 204 返回 `undefined`）；`account-logic.ts` 判据纯函数层（`accountReasonError` / **`bindingPlan`**——`submitted` = 勾选 ∪「已持有但 `bindable=false`」，**PUT 是全量替换，漏一行就是静默收回权限** / `accountGates` 五挡：缺码→超管→是自己→目标类型→状态机 / `filterAccountsLocally` / `statusLabel`+`typeLabel` 未知档位显示原码不默认「启用」）；`PermissionAccountsView.vue`（游标栈翻页、**「还有下一页」只认 `next_cursor`**、写动作成功后重读当前页不手改本地 `status`）+ `AccountDetailDrawer.vue`（409 采用服务端 `latest`、MFA 区按权限码整块出现或不出现、`display_name=null` 统一成「未命名账号（ID 尾号 xxx）」）+ `error-toast.ts`（`reportError`：403 交全局、不再叠第二条——TD-258）；`router` 加 `settings/accounts`（`meta.permission="school.permission_account.manage"`）与菜单项；`rbac-mock.ts` 新增 5 条路由 / 6 个端点且**有状态**，判据顺序逐条注释指向后端行号（TD-253）；新增 `admin-permission-accounts.spec.ts` 15 条 + `account-logic.test.ts` 29 条。三轮独立审查 **0B/4M/7m → 1B/1M/3m → 复审 5 项到位 + 2m**，结论「可以提交」（两条新 Minor 登记 TD 不强制改码）；其中「mock 的 `ROLE_NOT_BINDABLE` 判据抄成 `bindable` 字段（后端按**码**判，`binding.py:536`）」第 1 轮记 Major、第 2 轮升级 Blocker——抄错让 mock **比后端更严**，旗舰用例在 mock 世界里保存其实失败、只断到请求体，修法是 `roleIsBindedByCode()` + 补「保存成功后重读列表仍含该角色」的**服务端状态断言**；「缺目标类型挡」让学生账号经 `/users/lookup` 进抽屉后摆出两个必 422 的死控件，补 `MANAGED_USER_TYPES` 且 **MFA 重置不吃这挡**（后端确无类型判据）。**残留 3 条有意不改**：TD-259（理由长度按 UTF-16 码元，emoji 前端放行、后端 422，mock 抄同一错法测不出）、TD-260（列表加载失败仍显示上一次成功的数据）、TD-261（`accountGates` 的「超管/是我」两挡顺序与后端相反，只影响提示语句）；三条已于 2026-10-31 由 PR #33（合并提交 `75c8a08`）收口。收口 **TD-242**（超管行四个写动作全不给 + 判据只认服务端 `is_super_admin`；单测刻意造「`is_super_admin=true` 但 `role_codes=["teacher"]`」的行，防止前端按码硬认时「用例绿、真页面错」）。**跑法坑本批又踩一次**：修完 `reportError` 手敲 `npx playwright test` 仍复现旧行为，`pnpm build` 后的 96 条才是事实（TD-254）。本地门禁 `build`/`lint`/`format:check`/`typecheck` 全绿、单测 admin **96** + api-client **67**、Playwright **96 passed / exit 0**（前端单测 128→**163**、E2E 81→**96**）。**M2-UI3 三批（187g-A/B/C）至此全部交付。** **M2 收尾③已交付（2026-10-30，PR #25 → 合并提交 `77ac33c`）**：`app/auth` 分支覆盖率抬到 82%——按简报方案 A 补 5 条**逐弧独占**用例（`mfa.py` `58 → 59`／`60 → 61`、`principal.py` `271 → 272`／`285 → 286`、`tokens.py` `215 → 216`），PR CI run `41078941427` **833 passed / 830.96s**、`app/auth` **417/506 = 82.41%**（82% 需 415 条、**余量 2 条**），合并后 main 推送 CI run `41082150650` 读数与 PR run 逐包一致；TD-234 定性为「现象不再复现、差值成因未定位、判定口径统一为 CI 产物」（原 682/683 矛盾系行号位移后的误读）；`THRESHOLDS["app/auth"]` 由单独的门槛 PR 从 80 抬到 82（第 1 次读数＝上述 PR #25；第 2 次＝门槛 PR #26 backend run `41086807009`（834 passed，417/506 = 82.41%，门槛 82 PASS）；回滚＝改回 80）；新登记 TD-264（本地/CI 解释器补丁版本不一致，已采纳 → 边界评审）与 TD-265（逐弧 diff 无常驻工具，已采纳 → M3）。**M2 边界评审已完成并关闭（2026-10-31，PR #29 → 合并提交 `1312523`）**：台账逐条处置（TD-233 → M3 首个后端点、TD-259/260/261 → M3 首个前端点（已完成，PR #33 → `75c8a08`）、TD-123 → M4、其余 M6），TD-254/262/263/264 四项口径落地（前检正反实测、文档只抄脚本读数、`.gitattributes` 统一 LF、CI 打印解释器版本）；TD-183 / TD-192 全栈 E2E 已于 2026-10-31 补跑（**18 例全绿（首次全绿 50.8s、四轮审查整改后复跑 40.2s〔Playwright 计时，整轮 60s〕）**，端口默认 18000 可配，栈内补了 arq worker）；PR #27 经 rebase 合并为 **`a928645`**，合并后 main 推送 CI backend run `41121065536` **834 passed / 696.52s**（四包覆盖率与 PR run 逐条一致）、frontend run `41121065630`（单测 67+96、Playwright **96 passed / 2.0m**）、security run `41121065476` 全绿） |
| M2 | M2 边界评审（台账逐条处置 + 验收记录 + TD-254/262/263/264 口径整改） | ✅ 已关闭（2026-10-31，PR #29 → 合并提交 `1312523`）：台账处置见 `docs/tech-debt.md`，简报 `docs/design-briefs/M2-boundary-review.md`；TD-254 前检四向实测（缺 `dist` / 源码较新 / 删除源码 → 中止；只改单测或生成的 d.ts → 放行）、mock E2E **96 passed（2.3m）**、TD-263 索引 463 个文本文件全 `i/lf`（工作区待重新检出后统一 LF）、TD-264 已由 PR #29 backend run `41139124064` 验证打印（版本 `Python 3.13.18`、`Coverage.py 7.17.0`）；PR CI backend 834 passed（`app/auth` 82.41%＝417/506、门槛 82 PASS）、frontend/e2e 96 passed（2.1m）、security 绿；合并后 main 推送 CI backend run `41144815937` **834 passed / 755.81s**（四包与 PR run 一致）、frontend run `41144815871`（96 passed / 2.1m）、security run `41144815896` 全绿 |
| 质量/安全/运维 P0（跨里程碑） | 关键包分支覆盖率门槛 + `security` 覆盖 PR/main + Dependabot + `/metrics` 与告警规则 | ✅ 完成（两轮独立审查：1 Blocker / 7 Major / 5 Minor + 0 Blocker / 0 Major / 8 Minor 全部处置，commit `d29e243` + `ba35fb5`；覆盖率校准 `ae8fa9c`（TD-221）；本地全量后端 755 passed/1573s（第二轮新增 2 条用例后未重跑）；PR #1 远端 CI **761 passed/745.95s**，auth 读数在 407–408/504 间漂移（`8a5a2d3` 同一 commit 两次 run 为 408/407；校准提交 PR run 408、push 与 main run 407；TD-221/187f 跟进定位），四包阈值按 CI 最低观测取整 73/80/81/66 且反证会红；`METRICS_ENABLED` 默认 false；简报 `docs/design-briefs/P0-quality-security-ops.md` §7，详见 `docs/review-log.md`）。Dependabot alerts/security updates 已于 2026-10-28 开启（alerts 经 API 验证）；Dependabot 安全 PR 的 `pull_request` 触发待真实安全 PR 出现后验证。不含 TD-214（Prometheus/Alertmanager/多进程/反代拒绝，M6） |
| M3 | KB-01A 知识库上传/扫描/元数据后端（KB-01 后端部分：批量上传 + 幂等 + ClamAV + 派生扫描状态 + 只读列表/详情/版本） | ✅ 完成并合并（2026-10-31，PR #31 → 合并提交 `3e763ca`；PR CI backend run `41186252455` **917 passed / 906.88s**、合并后 main run `41187412558` **917 passed / 925.05s**，两侧四包分支覆盖率逐包一致且全过（`app/rbac` 79.92%、`app/auth` 82.41%、`app/tenant` 81.25%、`app/audit` 66.67%），security 两侧 success）：迁移 `b3d7f9a1c5e2` 三表 + FORCE RLS；`POST /school/kb/documents`（1–15 文件、≤50MB/文件、≤300MB/批、必填 `Idempotency-Key`、整批原子）+ `GET /school/kb/documents`（+`/{id}`、`/{id}/versions`）；arq 扫描 + 5 分钟看门狗；独立审查第 1 轮 0B/1M（并发同键分支无覆盖）→ 修复后复审 0B/0M PASS；本地门禁：单元 **105 passed**、集成/业务 **44 passed（198.45s）**、整改后受影响 **27 passed（137.96s）**、迁移回滚 **1 passed（6.57s）**、ruff + mypy（226 文件）+ bandit + alembic check 全绿；简报 `docs/design-briefs/M3-KB-01-upload.md` |
| M3 | KB-01B 知识库前端（上传页 + 文档列表 + 详情/版本抽屉 + mock E2E） | ✅ 完成并合并（2026-10-31，PR #35 → 合并提交 `d6de5a6`；PR CI frontend run `41200417893` 单测 **74 + 124 passed**、Playwright **117 passed / 2.4m**，security run `41200417874` success；合并后 main run `41203235918` Playwright **117 passed / 2.5m**、security run `41203235874` success）：两个路由与按码菜单（`/kb/documents`＝`school.kb.view`、`/kb/upload`＝`school.kb.upload`）；上传本地预检 + 可选元数据（开关关闭＝不发送、已填值保留）+ 幂等键签名（409 强制换新键）+ 结果页轮询（串行、5s、5 分钟截止、隐藏暂停、403 停止、卸载即停）；列表 cursor 栈翻页 + 「数据可能不是最新」横幅；抽屉详情/版本加载更多 + 404/切换竞态处理；`kb-mock.ts` 有状态、判据注释指向后端行号。独立审查 **0B/4M/13m → 0B/0M**（4 条 Major 为轮询/分页/抽屉/卸载时序缺陷，全部修复；TD-282 已延后 → KB-01C；TD-283 minimatch 开发依赖漏洞已登记延后）；本地门禁：api-client **74 passed**、admin **124 passed**、`admin-kb.spec.ts` **18 passed（28.6s）**、admin project **108 passed（1.6m）**、`typecheck`/`lint`/`format:check`/`build` 全绿；`backend/` 零改动；简报 `docs/design-briefs/M3-KB-01B-frontend.md` |
| M3 | KB-01C 知识库全栈 E2E（真实后端 + 真实库：主链路/幂等/整批原子/权限边界 + TD-282 三条轮询停止分支） | ✅ 完成并合并（2026-11-01，PR #37 → 合并提交 `7d1ce56`；PR CI backend run `41236413002` **917 passed / 936.88s**（TOTAL 90%，四包分支覆盖率全过）、frontend/e2e run `41236413016`（单测 **74 + 124 passed**、Playwright **120 passed / 2.7m**）、security run `41236412962` success；合并后 main run `41238768567` **917 passed / 923.59s**（四包分支覆盖率与 PR run 逐包一致）、frontend run `41238768664`（单测 74 + 124、Playwright **120 passed / 2.2m**）、security run `41238768599` success）：E2E compose 打开 `DEV_SKIP_FILE_SCAN=true`（仅测试编排；`Settings` 非 dev/test 硬拒，生产红线不变）；`e2e_seed.py` 新增 `e2e-kb-admin`（上传+查看、`school` 范围）与 `e2e-kb-uploader`（仅上传、`self` 范围）；`admin-fullstack-kb.spec.ts` 4 例（真实 `PENDING_APPROVAL`/`skipped`/`dev-skip`/元数据；幂等重放同 id、同键换文件 409 且未落库、新键新文档；整批 422 未落库；uploader 403、菜单/守卫、零详情请求、admin 全校可见）；`admin-kb.spec.ts` +3（详情 403 停止、隐藏暂停/恢复、5 分钟截止）；独立审查三轮 **0B/1M/4m → 0B/1M/1m → 0B/0M/0m**（两条 Major 均为断言假绿，已补服务端列表回读）；本地全栈 **22 passed（59.0s，修复前基线；修复后受影响 KB 4 例复跑 28.2s）**、mock 全量 **120 passed**；TD-282 收口，TD-284/285 已登记延后 M6；简报 `docs/design-briefs/M3-KB-01C.md` |
| M3 | KB-02 单文档版本审批与构建门禁（后端点：approval 四表 + kb_build_jobs、审批引擎最小集 APV-01/02、自批恒 403、构建门禁 409/BUILD_QUEUED、元数据免审但审计、TD-277 收口） | ✅ 完成并合并（2026-11-07，PR #39 → 合并提交 `edc34f6`，分支已删；PR CI backend run `42287001526` **945 passed / 730.15s**、四包分支覆盖率 79.92 / 82.41 / 81.25 / 66.67% 全过门槛（与 KB-01C 读数逐包一致）、security run `42287001607` success；合并后 main 推送 CI backend run `42289398725` **945 passed / 902.91s（15:02）**、四包分支覆盖率与 PR run 逐包一致、security run `42289398800` success）：迁移 `d5e9a3c7b1f4` 五表 + FORCE RLS + 部分唯一索引兜底「一版本一活跃单」「一版本一活跃构建」+ 状态 CHECK 放宽 APPROVED/REJECTED（不建版本表反向指针，design.md :221 已修订）；审批中心最小实现（`school.approval.todo` 待办/已办/详情 + `school.kb.approval` 通过/驳回 + `school.kb.upload` 提交与撤销，租户级口径 TD-289）；只有 APPROVED 可创建构建（409 `KB_NOT_APPROVED`+审计+零任务行；`BUILD_QUEUED` 滞留至 KB-03 取件，TD-288）；元数据编辑免审但审计前后值；TD-277 完成（同键同文件不同 metadata 409，部署注意：旧批次重放一次性变 409）；TD-233 改判 M3 边界评审，TD-286（→M5）/TD-287/TD-288（→KB-03）/TD-289（拒绝）/TD-290 已登记；独立审查两轮 **0B/1M/5m → 0B/0M/2m** 全部处置（Major=组合标题溢出 varchar(200) 500，截断+边界用例）；实现中自查修掉 flush 后 expire 属性的 MissingGreenlet、兄弟 except 不接力漏拒绝审计两个真缺陷，并发现「同事务先普通读后 FOR UPDATE 需 `populate_existing`（否则身份映射返回旧 PENDING → 并发双赢家）」机制（scan_task 未踩是因短事务），均记 review-log 并有用例；本地门禁：ruff/mypy（231 文件）/bandit×2/alembic check（往返实测）全绿，单元 25+7+8、集成 16+16+迁移回滚 12、业务 3；简报 `docs/design-briefs/M3-KB-02.md` §9；审批中心前端为 KB-02B（下一功能点） |
| M3 | KB-02B 审批中心前端（`/approvals` 待办/已办/详情抽屉 + 通过/驳回/撤销 + KB 抽屉审批入口 + APPROVED/REJECTED 文案；纯前端，`backend/` 零改动） | ✅ 完成并合并（2026-11-07，PR #41 → 合并提交 `45f84d4`，分支已删；PR CI frontend/e2e run `42361593542` 单测 **80 + 137 passed**、Playwright **137 passed / 2.3m**、security run `42361593541` success；合并后 main（tip `45f84d4`）推送 CI frontend run `42365771452`（单测 **80 + 137 passed**、Playwright **137 passed / 2.1m**，与 PR run 逐项一致）、security run `42365771466` success）：审批中心挂 `school.approval.todo`（待办/已办两 tab 游标栈翻页 + 按 tab 分账的失败横幅/loadSeq），详情抽屉含版本快照/步骤/动作留痕，通过一次确认、驳回理由先拦（strip 非空 + ≤500 码点）、撤销按提交人判据；**自批不做前端预判**（前端无版本归属数据，403 交后端裁决）；KB 文档抽屉按状态+权限给提交审批/创建构建（重复动作吃后端 409，服务端 message 优先），REJECTED 给「审批中心 → 已办」指引（TD-291 → KB-03）；kb-logic 补 APPROVED/REJECTED 文案（五档）；`approval-mock.ts` 有状态、判据以**函数名锚点**手抄后端（TD-253 口径）；独立审查两轮 **0B/2M/7m → 0B/0M/4m** 全部处置（Major＝kb-logic 单测缺、文档同批缺；复审确认 409 emit-changed 时序无竞态、「他人处理的单不入我的已办」与后端 `list_done` 语义一致）；本地门禁：typecheck/lint/format/build 全绿、单测 **217**、mock E2E **137 passed**；简报 `docs/design-briefs/M3-KB-02B.md` §9；TD-291/292 已登记 |
| M3 | KB-03A 构建执行管线（arq build_kb_version 三段式 + MinerU 降级链 + chunker/embedding/Qdrant + 快照/retry/看门狗；TD-288/TD-046 兑现） | ✅ 完成并合并（2026-11-07，PR #43 → 合并提交 `87f9b83`，分支已删；PR CI backend run `42376473762` **979 passed, 1 skipped（936.50s）**——skipped=真实引擎业务场景 CI skip（TD-295）、四包覆盖率 79.92/82.41/81.25/66.67% 全过、security run `42376473695` success；合并后 main（tip `87f9b83`）推送 CI backend run `42378081081` **979 passed, 1 skipped（1107.77s / 18:27）**、四包覆盖率与 PR run 逐包一致、security run `42378081083` success）：迁移 `e8c2a6d0f4b9` kb_build_jobs 全字段 + kb_chunks/kb_index_versions + FORCE RLS；管线 = PARSING（MinerU V1 `/v1/file_parse` 适配器 + standard→basic→flash 降级链 + fallback_reason 链式记录；TXT/MD 规范化；DOCX/PPTX 诚实失败 PARSER_UNAVAILABLE——TD-293）→ CHUNKING（父子块/参数 Settings 可配 TD-294 临时默认 500/15%/1000/表格代码不拆/句界硬切/跨节不重叠）→ EMBEDDING（TEI dense + jieba/BM25 sparse 含 avgdl 归一）→ INDEXING（Qdrant `tenant_{sha256前12位}_kb_v{version}` + named vectors + payload 十字段不落明文租户 id + 删旧建新幂等 + 双对账）→ VALIDATING → READY；快照 `GET /school/kb/build-jobs/{id}`（TD-288 兑现）+ retry（只有 FAILED，attempt+1 不覆盖历史）+ 收口 attempt 校验（防跨 attempt 混装）；看门狗：BUILDING 心跳超时 STALE、QUEUED 滞留重投（入队 best-effort 丢失的恢复路径）；异常漏斗九类全落 FAILED+error_code（GBK 文本 NORMALIZE_DECODE_FAILED 等）；arq Function timeout_s=1800 显式覆盖默认 300s；独立审查两轮 **2B/6M/12m → 0B/0M/3m** 全部处置（B1 异常漏斗、B2 job_timeout、M1 attempt 校验、M2 QUEUED 死锁、M3 跨节重叠、M4 BM25 归一）；本地门禁：ruff/mypy（246 文件）/bandit×2/alembic check（roundtrip 实测）全绿，单测 **32**、集成 **7**（真实 Qdrant）、业务场景 **1 passed（58.28s 真实 MinerU+TEI+Qdrant → READY，TD-046 兑现）**、迁移回滚 12 passed；简报 `docs/design-briefs/M3-KB-03A.md` §9；SSE/前端进度/TD-291 归 KB-03B |
| M3 | KB-03B 构建事件可观测与版本审批查询（迁移 `f2a8b4c6e0d7` kb_build_events + worker 事件写入 + SSE 端点 + TD-291 端点 + Minor 3：payload parent_id/只索子块/KB_EMBED_DIM 校验） | ✅ 完成并合并（2026-11-07，PR #45 → rebase 合并，分支已删；PR CI backend run `42386397474` **986 passed, 1 skipped（1043.44s / 17:23）**、四包覆盖率全过、security run `42386398169` success；合并后 main 推送 CI backend run `42387969631` **986 passed, 1 skipped（961.84s / 16:01）**、security run `42387969652` success）：`kb_build_events` FORCE RLS + (job_id,seq) 唯一；worker 同事务写事件（D1）；SSE 端点（fetch+Bearer，回放/续传/终态关流/15s 心跳）；TD-291 端点（按 version 查审批单，KB-02B 静态指引替换依据）；Minor 3 定案（只索引子块 D3，payload parent_id 指向父块，KB_EMBED_DIM=1024 构建期校验）；实现中自查修掉 6 个缺陷（导入缺/索引偏移/定义序/去 await/dim 冲突/substring 假绿）；简报 `docs/design-briefs/M3-KB-03B.md` §9；**KB-03 全点完成（A+B）** |
| M3 | KB-04A 测试沙箱（迁移 `b7d3e9f1a5c3` kb_test_sets/cases/runs 三表 + RLS + 状态机扩展 TESTING/TEST_PASSED/TEST_FAILED + Qdrant search_points + test_parser/test_task + API 三端点；检索命中 MVP D1 无 LLM） | ✅ 完成并合并（2026-11-08，PR #47 → rebase 合并，分支已删；PR CI backend run `42393250936` **986 passed, 1 skipped（992.33s / 16:32）**、四包覆盖率全过、security run `42393251058` success；合并后 main 推送（tip `4f569df`）backend run `42394810819` / security run `42394810890` success）：检索命中 MVP（D1）：每条 case question→embed→Qdrant 混合搜索→expected_chunk_ids 对账→100% 命中=TEST_PASSED（D5）；LLM 回答+Ragas → M4（TD-298）；解析器 CSV/XLSX/JSON（复用 import_parser 范式，行级校验，整批原子）；100% 阈值宁严勿松（D5，调整归 TD-294）；TD-298/299/300 已登记；简报 `docs/design-briefs/M3-KB-04A.md` §9；前端测试管理 UI 归 KB-04B |
| M3 | KB-04B 测试沙箱前端（api-client testSet 4 方法 + test-logic 纯函数层 + KB 抽屉测试区块 + TD-291 直显驳回理由；纯前端 `backend/` 零改动） | ✅ 完成并合并（2026-11-08，PR #49 → rebase 合并，分支已删；PR CI frontend/e2e run `42397605537`（单测 **85 + 146 passed**、Playwright **139 passed / 3m17s**）、security run `42397605425` success；合并后 main 推送（tip `adb8e97`）frontend run `42398117792` / security run `42398117859` success）：api-client testSet 4 方法 + 契约类型；test-logic 纯函数层（状态文案/上传预检/区块门禁/通过率/TD-291 最近审批摘要）；KB 抽屉测试区块（READY/TEST_* + school.kb.test 码时显示，上传测试集→发起运行→轮询→TEST_PASSED/FAILED 展示）；TD-291 直显（REJECTED 状态动态渲染最近审批驳回理由，替换 KB-02B 静态指引）；单测 **231**（85+146，+14）、mock E2E **139 passed**（基线 137 + 2）；`backend/` 零改动；简报 `docs/design-briefs/M3-KB-04B.md` §9；**KB-04 全点完成（A+B）** |
| M3 | KB-05 发布/热更新后端（TEST_PASSED 门禁 + 每文档 alias 原子切换 + 回滚/下线/软删 + `active_publish`；迁移 `c9e1f3a5d7b9`） | ✅ 完成并合并（2026-11-10，PR #51 → rebase 合并 `22f98b5`+`fbd99ac`，分支已删；PR CI backend run `42661737050` **1011 passed, 1 skipped（19m21s）**、四包覆盖率门槛全过、security run `42661737044` success——首跑 run `42656338324` 红在既有守卫用例 `test_only_exempted_modules_touch_tenant_tables`（service 直连租户表未走仓储），修复为 TenantScopedRepository+ScopeBypass 后绿；简报 `docs/design-briefs/M3-KB-05.md`（D1-A 每文档 alias、D2-A 任意 PUBLISHED 目标回滚、D4-A ACL 门禁归 KB-09、D5+D6-A 纯后端；TD-292 顺延 KB-05B 经用户确认留痕）；独立审查三轮 1M/5m 全处置（alias 恢复路径实测、切换一律实时解析 alias、`KB_PUBLISHED_NOT_ACTIVE` 独立码、offline/delete 文档行 404 对齐）；本地单测 10 + 集成 10 + 业务场景 5（真实 PG+Qdrant；200 并发解析零异常且恒 ∈ {旧,新}、版本-collection 恒配对）；顺带修复模型 `KB_STATUSES` 落后于库的既有漂移（KB-04A 的 TESTING 三态只进迁移没进模型，Alembic 不比对 CHECK）；TD-302/303/304 已入台账；合并后 main（tip `fbd99ac`）推送 CI backend run `42665528099` **1011 passed, 1 skipped（1095.57s）**、security run `42665528412` success） |
| M3 | KB-05B 发布/回滚/下线/删除前端 + TD-292 UI 全栈 E2E（**KB-05 全点完成**；顺带修复 KB-04A `run_kb_test` 漏注册 worker 缺陷——用户确认本 PR 顺带修，偏离「纯前端」口径已留痕） | ✅ 完成并合并（2026-11-10，简报 `docs/design-briefs/M3-KB-05B.md`：D1-A 补齐 14 档状态文案〔分域保留 test-logic 运行态 map，§8 登记〕、D2-A 会话内活跃记忆+409 兜底〔无 active 端点，登记 TD-305〕、D3-A 三账号职责分离、D4-A e2e compose 加 tei-embed〔复用 external 卷 campus-elf_tei_models〕、D5-A 一次确认+影响明示、D6-A 单 PR 含 TD-292；本地门禁：前端 typecheck/lint/format/build 全绿、单测 **247**（91+156，基线 231+16）、mock E2E **146**（基线 139+7）、全栈 E2E **25 passed / 3.0m**（22 既有 + 3 新，UI 驱动 审批→构建 409 门禁→职责分离→构建→测试→发布→回滚兜底→重放→下线→删除；**全栈套件设计=每轮种子跑一次全量**，改密类用例改口令）；后端 ruff/mypy 253 文件/bandit×2/alembic check + test_worker_settings 9 passed；独立审查 **0B/1M/7m 全处置**（M-1 文档回写、m-1/2/7 mock 与后端状态同步对齐、m-3 简报偏离登记、m-4 409 覆盖映射、m-5 生成文件、m-6 TEI 等待上限+卷预检），第 2 轮复审 0B/0M「可以提交」，见 `docs/review-log.md`；TD-292 收口、TD-305 入台账；PR #53 → rebase 合并 `7796199`（分支已删，合并授权由用户在 CI 全绿后确认）；PR CI backend run `42697763592` **1012 passed, 1 skipped（22m42s）**、frontend/e2e run `42697763718`（单测 91+156、Playwright 146 passed）+ security success；合并后 main（tip `7796199`）推送 CI backend run `42701848246` **1012 passed, 1 skipped（23m2s）**、frontend run `42701848971`（4m17s）+ security run `42701848401` success，读数详见 README「当前门禁」） |
| M3 | KB-06A 分块预览后端（列表/详情/统计三只读端点 + q/章节/类型/父块筛选 + 内联 keyset `(chunk_index,id)` + ScopeBypass 分块查询；权限 `school.kb.view_chunks`；无新迁移） | ✅ 完成并合并（2026-11-10，分支 `feature/m3-kb06a-chunk-preview`；本地门禁：单测 15 + 集成 18（211s 真实 PG+Qdrant）+ 业务场景 2 + 守卫/worker 合并 28 passed、ruff/mypy 257 文件/bandit×2/alembic check 全绿；独立审查 **0B/0M/4m 全处置**（m-1 404 文案对齐 service.py、m-4 补 limit/空白 q 断言、m-2 q 长度按 raw 度量与 m-3 业务场景真实全链两处偏离登记简报 §9）；TD-306/307/308 入台账；**PR #55 → rebase 合并 `d5a4a83`（分支已删，合并授权由用户在 CI 全绿后确认）**：PR CI backend run `42714007501` **1047 passed + 1 skipped（21:00）**、四包覆盖率 79.92/82.41/81.25/66.67% 门槛全过（与 KB-05B 起读数逐包一致）、security run `42714008296` success（44s）、frontend/evals 按路径过滤未触发；**合并后 main（tip `d5a4a83`）推送 CI：security `42719612603` success（48s）、backend `42719612807` 首跑红在既有 KB-05 并发用例时序敏感断言（flake，KB-06A 零生产交集）→ `--failed` 复跑 success（1047 passed + 1 skipped / 18:52，四包覆盖率逐包一致），登记 TD-309（加固归 M3 边界评审）**；简报 `docs/design-briefs/M3-KB-06.md` §9） |
| M3 | KB-06B 分块预览前端（api-client 3 方法 + kb-logic 纯函数 + KB 抽屉分块子视图（版本行入口/四筛选/详情父子导航/统计摘要，纯文本渲染无 v-html）+ mock E2E +7 + 全栈链③分块断言；**KB-06 全点完成**；编排：e2e_seed publisher 补 view_chunks + MailHog 端口覆盖机制；**顺带修复 2 缺陷：kb_embed_batch_size 默认 16→8（TEI CPU ONNX 上限 8，≥9 子块构建必 FAILED——用户拍板）+ 全栈链② Escape 冒泡关闭抽屉竞态（trace 实证）**） | ✅ 完成并合并（2026-11-11，分支 `feature/m3-kb06b-chunk-frontend`；本地门禁：typecheck/lint/format/build 全绿、单测 **262**（api-client 97 + admin 165）、mock E2E **153 passed**（基线 146+7，TD-254 前检后跑）、全栈 E2E **25 passed（2.0m）重置种子全量一次过**（链③新增表格未拆坏 API+UI 断言）、ruff/mypy scripts 0 错误；独立审查 **0B/0M/6m 全处置**（m-1 码点度量修正/m-2 锚点订正/m-3 详情代际守卫/m-6 夹具修正已改，m-4/m-5 简报 §8 偏离 4-6 登记）；实现期偏离 9 项全登记简报 §8；**PR #57 → rebase 合并 `6949ec3`+`780d62e`（分支已删，合并授权由用户在 CI 全绿后确认）**：PR CI backend run `42877592475` **1047 passed + 1 skipped（24:52）**、四包覆盖率 79.92/82.41/81.25/66.67% 门槛全过、frontend/e2e run `42877592208` pass（单测 262 + Playwright 153）、security run `42877592330` success（42s）；TD-310/311 登记（图片处理路径两阶段方案 / XLSX/CSV 白名单评估）；简报 `docs/design-briefs/M3-KB-06B.md` §9） |
| M3 | KB-07A 检索测试后端（单 query 诊断端点 `POST /versions/{id}/retrieval-test`：三查询旁路逐路分数〔生产融合查询 search_points 零改动〕+ 可选 CPU rerank〔RerankPort/TEIReranker，opt-in 池默认 20，失败/超时降级融合序永不 5xx〕+ 最终引用与逐路排名；权限 `school.kb.view_chunks` 无新码；ACL 三参数 KB-09 预留设值即 422；无新迁移；**顺带加固 TD-309 flake——用户拍板**） | ✅ 完成并合并（2026-11-11，分支 `feature/m3-kb07a-retrieval-test`；本地门禁：单元 **18** + 集成 **16**（真实 PG+Qdrant，词袋 embedder 两侧同源确定性 top-1）+ 业务场景 **2**（HTTP 全链）+ 守卫/worker 30 passed、ruff/mypy **264 文件**/bandit×2/alembic check 全绿；独立审查三轮 **0B/1M/4m → 0B/1M/1m → 0B/0M/0m** 全部处置（M1 rerank 异常逃逸、复审 M1 `try/else` else 子句逃逸——`RERANK_COUNT_MISMATCH` 端口违约也曾 500、m-1 pool<top_k 兜底、m-2 重复 index 拒绝、弱 pin 加固；见 `docs/review-log.md`）；**PR #59 → rebase 合并 `4cca9bb`+`8dbeaff`（分支已删，合并授权由用户在 CI 全绿后确认）**：PR CI 首跑/复跑红在 TD-309 既有 flake（`1 failed, 1082 passed, 1 skipped`——本点 36 条新用例全过）→ 用户拍板顺带加固（确定性前奏+并发窗口，本地 3/3 绿，**TD-309 提前收口「已完成」**）→ 重跑 backend run `42911771762` **success（27m50s，1083 passed, 1 skipped）**、四包覆盖率 79.92/82.41/81.25/66.67% 逐包一致全过、security run `42911771816` success（51s）；合并后 main（tip `8dbeaff`）推送 CI：backend run `42925157839` **success（22:34，1083 passed, 1 skipped，四包覆盖率与 PR run 逐包一致）**、security run `42925157687` success；TD-312/313/314 登记（sparse IDF 语料不对称 →M4 / 检索面未模拟 rerank 漏斗 →M4 / CPU 重排延迟进诊断路径 →延后）；简报 `docs/design-briefs/M3-KB-07.md` §9） |
| M3/M4 | 知识库闭环 / AI 与 C 端 | 🔄 进行中（M3：KB-01A 后端已合并（2026-10-31，PR #31 → `3e763ca`）；KB-01B 前端已合并（2026-10-31，PR #35 → `d6de5a6`）；KB-01C 全栈 E2E 已合并（2026-11-01，PR #37 → `7d1ce56`）；KB-02 审批与构建门禁后端已合并（2026-11-07，PR #39 → `edc34f6`）；KB-02B 审批中心前端已合并（2026-11-07，PR #41 → `45f84d4`）；KB-03A 构建执行管线已合并（2026-11-07，PR #43 → `87f9b83`）；TD-259/260/261 已收口（2026-10-31，PR #33 → `75c8a08`）；KB-03B 构建事件可观测已合并（2026-11-07，PR #45）——KB-03 全点完成（A+B）；**KB-04A 测试沙箱后端已合并（2026-11-08，PR #47 → `4f569df`）；KB-04B 测试沙箱前端已合并（2026-11-08，PR #49 → `adb8e97`）——KB-04 全点完成（A+B，检索命中 MVP）**；**KB-05 发布/热更新后端已合并（2026-11-10，PR #51 → `fbd99ac`）**；**KB-05B 发布/回滚/下线/删除前端 + TD-292 全栈 E2E 已合并（2026-11-10，PR 见 handoff「当前门禁」；顺带修复 KB-04A 的 run_kb_test 漏注册 worker 缺陷，用户确认顺带修）**——KB-05 全点完成（后端 + 前端）；**KB-06A 分块预览后端已合并（2026-11-10，PR #55 → `d5a4a83`）**；**KB-06B 分块预览前端已合并（2026-11-11，PR #57 → `6949ec3`+`780d62e`：抽屉分块子视图 + 四筛选 + 纯文本渲染；审查 0B/0M/6m 全处置；顺带修 kb_embed_batch_size 16→8 与链② Escape 竞态；TD-310/311 登记）——KB-06 全点完成（后端 + 前端）**；下一步 **KB-07B**（检索测试前端：api-client `retrievalTest` + 分块子视图检索测试区 + mock E2E + 全栈链③检索断言）；其余 M3 功能点（KB-08/09/10）未开始；已含 SC-01 新增 KB-09/KB-10、AI-10） |

- **建议的下一步顺序**：M3 知识库闭环进行中——KB-01（A/B/C）、KB-02（后端+02B 前端）、KB-03（A+B）、KB-04（A+B，检索命中 MVP）、KB-05（后端 2026-11-10 PR #51 → `fbd99ac`：TEST_PASSED 门禁、每文档 alias 原子切换、回滚/下线/软删、`active_publish`）、**KB-05B（2026-11-10：发布/回滚/下线/删除前端按钮 + mock E2E + TD-292 UI 全栈 E2E 收口；顺带修复 KB-04A 的 run_kb_test 漏注册 worker 缺陷）** 已完成——**KB-05 全点完成（后端+前端）**；**KB-06A（2026-11-10：分块预览后端 3 只读端点 + q/章节/类型/父块四筛选 + 内联 keyset；审查 0B/0M/4m 全处置；无新迁移，head 仍 `c9e1f3a5d7b9`）**、**KB-06B（2026-11-11：分块预览前端——抽屉分块子视图 + 四筛选 + 详情父子导航 + 纯文本渲染无 v-html + mock E2E +7 + 全栈链③分块断言；审查 0B/0M/6m 全处置）——KB-06 全点完成（后端 + 前端）** 已完成。**下一步做 KB-07B**：检索测试前端（api-client `retrievalTest` 方法 + 分块子视图检索测试区〔dense/sparse/RRF/rerank 分数列、最终引用标记、「查看分块」复用既有详情导航〕+ mock E2E + 全栈链③检索断言含一次真实 rerank〔e2e compose 需加 tei-rerank，D4-A 同款〕；后端契约=简报 `M3-KB-07.md` §3，决策卡已拍板）；此后 KB-08/09/10。复现全栈 E2E 见 `docs/design-briefs/M3-KB-01C.md` §4 与 §9（命令与读数）及 `docs/design-briefs/M3-KB-05B.md` §9（本点新增 tei-embed 编排与三账号）；**全栈套件设计为「每轮种子跑一次全量」**（改密类用例会改账号口令，不重置种子直接复跑必假失败）；`docs/m1-acceptance.md` §6 是 M1 期读数快照，不代表当前用例数。
- **数据库迁移 head**：`c9e1f3a5d7b9`（M3 KB-05：kb_publish_aliases 表 + 状态 CHECK 放宽 PUBLISHED/OFFLINE/DELETED，`down_revision=b7d3e9f1a5c3`；**downgrade 数据前提**：库里已有 PUBLISHED/OFFLINE/DELETED 行时需先迁移发布态数据，见迁移 docstring）；上一 head 为 `b7d3e9f1a5c3`（M3 KB-04A：kb_test_sets/cases/runs 三表 + RLS + 状态放宽，`down_revision=f2a8b4c6e0d7`）；上一 head 为 `f2a8b4c6e0d7`（M3 KB-03B：kb_build_events + RLS，`down_revision=e8c2a6d0f4b9`）；上一 head 为 `e8c2a6d0f4b9`（M3 KB-03A：kb_build_jobs 全字段 + kb_chunks/kb_index_versions + RLS，`down_revision=d5e9a3c7b1f4`）；上一 head 为 `d5e9a3c7b1f4`（M3 KB-02：`approval_types`/`approval_requests`/`approval_steps`/`approval_actions`/`kb_build_jobs` 五表 + RLS + 部分唯一索引，`down_revision=b3d7f9a1c5e2`；**downgrade 数据前提**：库里已有 APPROVED/REJECTED 行时需先迁移决定态数据，见迁移 docstring）；上一 head 为 `b3d7f9a1c5e2`（M3 KB-01A 三表）；`alembic check` 应无漂移。
- 最近提交：`main` 已含 M1-D2 MFA、M1-E1 鉴权内核与 M1-E2 数据范围/职责分离交付（不写具体哈希，避免每次提交后自相矛盾）；工作树应保持干净。
- 远端：`origin = git@github.com:huluqiwa/campus-elf.git`（SSH；本机 github.com 的 HTTPS 被重置，SSH 的 22/443 可用）。
- 远端 CI：4 条工作流（backend / frontend / evals / security）。backend/frontend/evals 在面向 `main` 的 PR 与 `main` 推送时按目录路径过滤触发；`security` 在面向 `main` 的 PR、`main` 推送与每周一 03:00 定时运行；同一 PR 的新推送会取消旧运行。**PR 的 CI 未全绿不得合并**（工作流见 AGENTS.md §7）。
- **工作流迁移（2026-10-28 决策，PR #7 于 2026-10-28 23:55（+08:00）合入，合入提交 `5966bbf`）**：从「批次末直推 main」改为「每个功能点一个 PR，CI 绿 + 独立审查通过后合并」。PR #7 CI：backend 761 passed/682.30s（auth 407/504）、frontend 85 单测 + 62 mock E2E、evals 2 passed + 用例集校验、security 绿；合并后 main 推送四条工作流全绿，feature 分支 push 事件不再触发 run；存量 Actions major PR #2–#6 已由 Dependabot 自动关闭（2026-10-28T15:55:53Z，未合并）。该流程下第一个真实功能点 PR 为 PR #9（TD-187d，2026-10-28T20:07:40Z 合并，合并提交 `9771be8`）：PR CI backend 778 passed/640.14s + security 绿；合并后 main 推送 backend `40760470613` 778 passed/758.84s + security `40760470645` 绿。私有库 + GitHub Free 暂不能开分支保护，required checks 与禁止直推只能靠纪律执行（TD-219）；升级 GitHub Pro 后补机器强制。

---

## 2. 环境与运行（Windows 11 + Git Bash + Docker Desktop/WSL2）

```bash
# 0) 一次性：本地配置（.env 不入库）
cp .env.example .env      # 必须补 JWT_SECRET（≥32 字节，openssl rand -hex 32）与 ENCRYPTION_KEK（32 字节 base64，openssl rand -base64 32）

# 1) 起测试栈（postgres/redis/qdrant/mailhog → 4 个 healthy）
bash scripts/test-stack.sh up
eval "$(bash scripts/test-stack.sh env)"      # 导出 TEST_* 连接串（含 TEST_SMTP_*、TEST_MAILHOG_API）

# 2) 后端
cd backend
uv sync --frozen
uv run ruff check app tests scripts alembic      # alembic 也在门禁内（TD-229）
uv run mypy --no-incremental app tests
REQUIRE_TEST_SERVICES=1 uv run pytest -q          # 应全绿（用例数以 pytest 汇总为准）
# 关键包分支覆盖率（P0）：先产出 json 再逐包比较；--cov-fail-under 只看 TOTAL，不用它
uv run pytest --cov=app.rbac --cov=app.auth --cov=app.tenant --cov=app.audit \
  --cov-branch --cov-report=json:coverage.json
uv run python scripts/check_coverage.py
uv run alembic -x db_url="$TEST_DATABASE_URL" check   # 迁移漂移检查

# 3) 前端
cd ../frontend
pnpm install --frozen-lockfile
pnpm lint && pnpm format:check && pnpm typecheck && pnpm test:unit && pnpm build
pnpm test:e2e                                     # 需系统 Chrome（channel: chrome），当前 32 单测 / 28 E2E

# 4) 一键本地 CI（与远端工作流一一对应；--with-e2e 追加 Playwright）
cd .. && bash scripts/ci.sh            # 或 bash scripts/ci.sh --with-e2e
```

### 已知环境坑（本机真实踩过，务必先处理）

1. **系统代理会劫持 127.0.0.1**：若本机开了代理，httpx/uv 会把 localhost 流量发给代理端口 → MailHog/Qdrant 连接被拒、`uv` 拉包失败。跑测试/工具链前先执行：
   `export NO_PROXY="127.0.0.1,localhost,::1,*" no_proxy="127.0.0.1,localhost,::1,*"`
   （测试侧已在 `backend/tests/conftest.py` 设默认值，但 `uv`/`pnpm` 需要 shell 层设置。）
2. **不要用 `python3`**：本机 `python3` 是 Windows Store 桩，会静默失败；统一用 `uv run python` 或 `python`。
3. **Windows 与编码**：`alembic.ini` 必须纯 ASCII（GBK 解析问题）；脚本文件保持 UTF-8。
4. **测试库重置**（仅测试库）：`drop schema public cascade; create schema public; grant usage on schema public to public;` 然后 `alembic ... upgrade head`。
5. **MailHog 端口**：测试栈 SMTP=51025、API=58025（不是 1025/8025，那是开发栈）。测试必须用 `TEST_SMTP_*`/`TEST_MAILHOG_API`，不要写死默认端口（CI 上会因 1025 无服务而 502）。
6. **测试栈容器重启后**：偶发端口转发短暂异常，重跑 `bash scripts/test-stack.sh up` 即可。
7. **测试栈端口可配置**：主机端口取自 `deploy/.env.test`（gitignored，由 `deploy/.env.test.example` 复制），本机当前 Postgres=57432、Redis=56379、Qdrant=56333、MailHog API=58025（SMTP 见上条 51025）；被占用时改 `TEST_*_HOST_PORT` 后重跑 `test-stack.sh up`，测试侧一律用脚本导出的 `TEST_*` 连接串，不要写死端口。
8. **Playwright 必须在 `frontend/` 根目录跑**（配置是 `frontend/playwright.config.ts`，用系统 Chrome + `webServer` 起 4173/4174 预览）。在 `frontend/e2e/` 里跑找不到配置 → 既没有 `baseURL`（`Cannot navigate to invalid URL`），又会退回未安装的 Playwright Chromium（`Executable doesn't exist ... chromium_headless_shell`）；本机装了系统 Chrome，正确做法是回根目录跑，不必下载浏览器。
9. **`bandit -r app` 对任何 issue 都非零退出（含 Low）**，其中 B101（`assert` 用于运行时收窄类型）最容易踩：生产代码不要用 `assert` 做不变量守卫，改成把条件并入分支判断（`if x is not None and ...`）并由末尾的 `RuntimeError` 兜住落空分支。M1-D2 首次本地 CI 就是这条挂的。
10. **别用 `pytest | tail` / `| tee` 判绿**：管道会返回 tail 的退出码，测试失败也可能显示成功。正确写法：`pytest -q > /tmp/pytest.log 2>&1; echo "PYTEST_EXIT=$?"`，再看日志末尾汇总。
11. **禁止 `ruff format`**：CI 只跑 `ruff check`，格式化会制造大量与功能无关的 diff；只按 check 报错点修。
12. **测试栈容器重启会导致角色口令漂移**：表现为集成测试成片 `InvalidPasswordError`（`campus_elf_test_platform`）。重跑 `bash scripts/test-stack.sh up`（幂等 `ALTER ROLE ... PASSWORD`）并确认 `TEST_DATABASE_URL`/`TEST_APP_DATABASE_URL`/`TEST_PLATFORM_DATABASE_URL` 三条都能连上，再信任任何全量结果。
13. **取用例数用 `pytest --collect-only -q -o addopts=""`**：默认 `addopts` 里已有 `-q`，再叠一个会变成 `-qq`，跑完不打印“N passed”汇总；计数与最终门禁数字要分开取。
14. **`test-postgres` 起不来、报 `bind: An attempt was made to access a socket in a way forbidden by its access permissions`**：Windows 的 Hyper-V 开机时会随机保留一整段 5xxxx 动态端口（`netsh interface ipv4 show excludedportrange protocol=tcp` 可查），撞上默认映射 55432 就失败。**不要去改 compose 的默认值**（CI 与其他机器用它）：在 git 忽略的 `deploy/.env.test` 里加一行 `TEST_PG_HOST_PORT=57432`（换一个未保留的端口）即可，compose 的映射与 `scripts/test-stack.sh` 导出的连接串读同一个键，不会两边不同步。M2-UI3 TD-187c 就是踩了这条（2026-10-27）。

### 2.1 接手时先做"环境自检"（新工具跑这几条，然后汇报，别猜）

```bash
uname -a                                   # 是否 Linux/macOS/Windows(Git Bash/WSL)
docker ps --format '{{.Names}} {{.Status}}' | head        # 能否操作 Docker；测试栈是否已在跑
test -f .env && echo ".env 存在" || echo ".env 缺失"      # .env 不入库，缺失需从 .env.example 复制
for k in JWT_SECRET ENCRYPTION_KEK; do grep -q "^$k=.\+" .env 2>/dev/null && echo "$k 已配" || echo "$k 缺失/为空"; done
gh auth status 2>&1 | head -5               # 能否查远端 CI
git ls-remote origin -h refs/heads/main     # 能否访问远端仓库（SSH/HTTPS 权限）
env | grep -i proxy | sed 's/=.*/=<set>/'   # 是否有系统代理（会劫持 127.0.0.1）
```

**要求它按这五项汇报**（每项写"能满足 / 不能满足 / 需要你提供"）：
1. 能否运行 Docker 与 bash（不能跑 Docker ⇒ 集成/业务场景测试与 e2e 无法本地执行，只能用单元测试 + 远端 CI 代替，必须在汇报里说明）；
2. 测试栈是否已启动（没起则先 `bash scripts/test-stack.sh up`）；
3. `.env` 与 `JWT_SECRET`/`ENCRYPTION_KEK` 是否就绪（缺失则从 `.env.example` 复制并分别生成 `openssl rand -hex 32` / `openssl rand -base64 32`；KEK 缺失会导致应用启动失败与 MFA 用例无法运行）；
4. 能否访问 GitHub 并查询 CI（`gh auth login` / SSH key / PAT）；
5. **是否具备"第二上下文/子 agent"能力**——这是独立审查门禁的前提；不满足时按 §6 的替代方案执行，并明确告知用户本轮的审查由谁做。

注意：**环境相关的"状态"（栈有没有起、凭据有没有配）只有用户能确认**；"配置方法"已经在 §2 里，不需要再问用户。

---

## 3. 工作流门禁（每个功能点都必须走完，顺序不可省；远端 CI 是 PR 的合并门禁）

1. **先写设计简报**：`docs/design-briefs/<ID>.md`，含目标与验收编号、**≥2 个候选方案**、选择结论与理由、实现顺序（数据→接口→界面）、TDD 红→绿清单、测试策略、预判技术债、高风险确认；**高风险功能（认证/租户隔离/权限/状态机/删除/发布/加密/AI 护栏）必须等用户确认后再编码**。
2. **TDD 实现**：先写失败测试，再实现；分层跑：单元 → 集成（真实 PG/Redis/MinIO/Qdrant，外部 LLM/嵌入/重排 mock）→ **业务场景**（从 Given/When/Then 跑完整链路，断言业务输出与副作用，不只看 HTTP 200）。
3. **独立代码审查**：**另起一个独立上下文/子 agent** 审查 `git diff --staged`，输出 Blocker/Major/Minor；安全、租户隔离、权限、状态机、数据一致性、AI 护栏、Langfuse 脱敏问题一律按 Blocker/Major。审查结论要贴到 PR 描述/评论。
4. **修复 + 复测（每点）**：修完重跑"受影响用例 + 本功能点的业务场景链路"；**受影响侧的全量套件由 PR 的远端 CI 跑**（backend/frontend/evals 按路径过滤，security 每个 PR 都跑），本地不重复跑全量（排查问题时除外）。实质变更要再复审，直到无 Blocker/Major。**每个 PR 一轮独立审查是硬门禁**。
5. **提交 → 推送分支 → 开 PR → CI 全绿 → 合并（每点）**：
   ```bash
   # 每个功能点
   git add <明确路径...>            # 禁止 git add . / -A
   git status && git diff --staged  # 检查密钥/产物/调试代码/大文件
   git commit -m "feat(<scope>): ..."   # Conventional Commits
   git push -u origin <branch>
   gh pr create --base main --head <branch> \
     --title "feat(<scope>): ..." --body-file <pr-body.md>
   gh pr checks <n> --watch         # 必须全绿
   gh pr merge <n> --rebase --delete-branch
   ```
   PR 描述按 `.github/pull_request_template.md` 填写；合并后 `main` 的 push CI 也必须全绿，失败按 hotfix 处理。
   禁止：`--force`、`reset --hard`、`clean -fd`、直接 push `main`（分支保护未开时靠纪律，见 TD-219）。
6. **汇报**：按 `AGENTS.md` §9 的 8 段格式（实现了什么 / 文件清单 / 测试与业务场景对照 / 审查 findings 与修复复测 / Spec 完成度与下一步 / Git 状态 / 为什么这样写与更好的做法 / 规范差距）。
7. **技术债闭环**：任何"更好的做法"必须登记 `docs/tech-debt.md` 并给处置（已完成/已采纳/已延后/已拒绝）；里程碑边界要评审台账。
8. **回写文档**：更新 `docs/design-briefs/<ID>.md` 实现结果、`docs/review-log.md`、`docs/tech-debt.md`、相应设计/规格章节，以及本文件 §1 与 README 的「当前状态」。

---

## 4. 文档地图（谁是事实来源）

| 文档 | 作用 |
|---|---|
| `AGENTS.md` | **规则源头**：纪律、红线、门禁、Git/汇报规范（优先级最高） |
| `docs/requirements.md` → `docs/spec.md` → `docs/design.md` → `docs/milestone.md` → `docs/test-plan.md` | 事实来源链（冲突时按此顺序，用户最新确认优先） |
| `docs/design-briefs/*.md` | 每功能点的实现前简报 + 实现结果 + 审查修复记录 |
| `docs/spec-changes/SC-*.md` | 规格变更提案（已确认的决策编号 A30/A31、K4/K5 等） |
| `docs/milestone.md` | 里程碑范围与退出标准（M1~M6） |
| `docs/test-plan.md` | 测试分层、业务场景矩阵（BS-xx）、A23 隔离矩阵、AI 评估 |
| `docs/rbac-matrix.md`、`docs/state-machines.md`、`docs/tenant-isolation.md` | 权限矩阵 / 状态机 / 隔离规范 |
| `docs/tech-debt.md` | 技术债台账（TD-001~TD-270），未处置不得进下一里程碑 |
| `docs/review-log.md` | 审查与复审记录（每轮结论、发现、修复） |
| `docs/deployment.md` | 部署、三连接角色、生产强制配置、启动自检 |
| `docs/m0-acceptance.md`、`docs/benchmarks-m0.md` | M0 验收与基准 |

### 4.1 阅读范围建议（省上下文，别全读）

**第一档 · 必读且以它为准**（接手时一次读完）：
`AGENTS.md`（规则源头）→ `docs/handoff.md`（本文件，现状与操作）→ `docs/milestone.md`（下一步范围与退出标准）→ `docs/spec.md` + `docs/design.md` 的对应章节 → `docs/tech-debt.md`（未处置条目）→ `docs/review-log.md`（最近审查结论）→ **正在做的那个功能点的** `docs/design-briefs/<ID>.md`。

**第二档 · 按需查**（做相关功能时才打开）：
`docs/test-plan.md`（写测试/业务场景矩阵时）、`docs/rbac-matrix.md`（权限）、`docs/state-machines.md`（状态流转）、`docs/tenant-isolation.md`（隔离与 A23 矩阵）、`docs/deployment.md`（部署/生产配置/自检）、`docs/compliance.md`（留存与合规）、`docs/observability-langfuse.md`（观测）。

**第三档 · 仅供参考，接手时不必读**：
已完成功能点的简报（只读在做的那个）、`docs/m0-acceptance.md`、`docs/benchmarks-m0.md`、`docs/spec-changes/README` 之外的提案（已确认的当作决策查，不必通读）。

**要求新工具汇报时明确说清**（这样你能一眼看出它有没有读对）：
1. 读了哪几份文档；2. 下一步的依据是哪一份（例如"依据 milestone §5 + 简报 M1-C2"）；3. 哪些文档它认为仅供参考；4. 有没有互相矛盾或与代码不一致的地方。
**发现文档互相冲突时不得自行决定**：按 `AGENTS.md` §12 的优先级（requirements → spec → design → milestone → test-plan，用户最新确认优先）停下来向你报告。

---

## 5. 不可逾越的红线（详见 AGENTS.md §3~§5）

- 密钥/口令/令牌只从 `.env` 或密钥管理读取：**不入库、不入日志/审计/trace、不回显、不提交**。
- MFA（M1-D2）：`ENCRYPTION_KEK` 同上（必填、base64 32 字节，禁止提交/回显）；TOTP secret 只以 AES-256-GCM 密文入库，恢复码只存 HMAC；secret / otpauth URI / 验证码 / 恢复码明文**一律不进日志、审计、错误响应**，只在激活响应出现一次并带 `Cache-Control: no-store`；密码正确但未完成 MFA 时不得签发任何令牌或 Cookie。
- 多租户：所有业务表/查询/缓存 Key/对象路径/Qdrant collection 带租户边界；数据访问层强制 `tenant_id` 过滤 + PostgreSQL RLS 兜底；跨租默认拒绝；不信任前端传入的 tenant_id/role。
- 生产环境：`DEV_SKIP_SCHOOL_REVIEW`、`DEV_SKIP_FILE_SCAN`、`DEV_SKELETON_LOOP_ENABLED` 必须关闭（配置校验已强制）；`AUTH_COOKIE_SECURE=true`、`AUTH_REFRESH_TOKEN_IN_BODY=false`。
- 知识库门禁：审批通过前不得解析/切块/嵌入/索引；未审批调用构建必须 409 + 审计。
- AI：只读工具；不展示原始 chain-of-thought / 系统提示词 / 工具参数 / 内部地址 / PII / 密钥；不伪造思考内容。

### 5.1 部署注意（M1-E1 RBAC 回滚约束，TD-147）

- 迁移 `downgrade` 只保证**结构**可来回（`test_migration_rbac_rollback.py` 覆盖 head↔downgrade↔head 重放）；`role_permissions`/`user_roles` 的**数据**会被重置为「E1 之前的出厂状态」，不是「回滚前的真实状态」。
- 生产回滚 SOP：执行 `alembic downgrade` 前，必须先备份 `role_permissions`、`user_roles` 两表（含 E1 期间对系统角色的人工增授、C 端角色上的 `user_roles` 绑定），回滚后逐租户恢复；否则人工授权会静默丢失。

---

## 6. 接手后如何"无缝继续"（给新工具的指令模板）

把下面这段作为**第一条指令**发给新的 AI 工具（它需要能读仓库、能跑 bash/docker、能开子 agent 做独立审查、能推送 GitHub）：

```text
你接手「校园陪伴精灵」项目（多租户 SaaS：SaaS 平台 + 校方 B 端 + 学生/教师 C 端 H5）。
工作目录：<仓库绝对路径>。

第一步（必须先做，不要写任何代码）：
1) 通读 AGENTS.md（规则源头：纪律/红线/测试门禁/审查门禁/Git 与汇报规范）；
2) 通读 docs/handoff.md（交接手册：当前状态表、环境怎么起、工作流门禁、文档地图、已知环境坑）；
3) 读 docs/milestone.md（M1 剩余范围）、docs/tech-debt.md（未处置技术债）、docs/review-log.md（最近审查结论）；
4) 读 docs/design-briefs/ 下与下一步相关的简报，以及 docs/spec.md、docs/design.md 对应章节。

然后向我汇报：当前处在哪个功能点、下一步该做什么、你打算按什么顺序做、需要我确认哪些决策。
在我说"开始"之前，不要创建或修改任何生产代码。

铁律（不要偏离）：
- 先设计简报（≥2 候选方案 + 风险 + 技术债），高风险功能必须等我确认后再编码；
- TDD：先写失败测试，分层跑单元/集成/业务场景（业务场景断言数据库状态、审计、状态机、对象存储、Qdrant、AI 引用/拒答等业务输出，不能只断言 HTTP 200）；
- 每个功能点完成后：跑受影响用例 + 本点业务场景链路 → 用**独立上下文/子 agent** 审查 staged diff（输出 Blocker/Major/Minor）→ 修复并复测 → 复审通过 → 提交（明确路径，禁止 git add .）→ **推送功能分支、开 PR、等远端 CI 全绿、合并并删分支**；全量后端/前端套件由 PR 的远端 CI 按路径过滤跑，本地不重复；
- 每个功能点完成后按 AGENTS.md §9 的 8 段格式汇报，并回写 docs/（简报实现结果、review-log、tech-debt、README「当前状态」、handoff 状态表）；
- 不得提交密钥/凭证；不得 force push / reset --hard；不得直推 main；生产开关不得默认放行。

环境提示：跑测试或工具链前先 `export NO_PROXY="127.0.0.1,localhost,::1,*"`（本机代理会劫持 127.0.0.1）；
起测试栈 `bash scripts/test-stack.sh up` 并 `eval "$(bash scripts/test-stack.sh env)"`；一键 CI `bash scripts/ci.sh`。

现在开始第一步，读完汇报，不要写代码。
```

### 若新工具不支持"子 agent"做独立审查

审查门禁不能省，替代做法（按优先级）：
1. 用**第二个会话/第二个工具**专门审查（给它的输入是 `git diff --staged` 与验收清单，要求输出 Blocker/Major/Minor）；
2. 至少让同一个工具在"审查模式"下重读 diff（要求它逐条对照 `docs/code-review-checklist.md` 与 AGENTS §6.2，并明确说出"哪些点已核对、证据是什么"）；
3. **人工交替审查**：把 diff 与 checklist 交给你本人或另一位同事确认后，再提交。

### 需要事先准备好的东西

- 仓库访问：`git@github.com:huluqiwa/campus-elf.git` 的 SSH key，或改用 HTTPS + PAT；`gh auth login`（查询 CI 用）。
- Docker Desktop 已启动（测试栈 4 个容器）。
- 本地 `.env`（从 `.env.example` 复制，必须补 `JWT_SECRET` 与 `ENCRYPTION_KEK`）。
- 明确告诉它"哪个模型/工具负责写代码、哪个负责审查"，避免自查自批。

---

## 7. 交接检查清单（交接前逐条确认）

- [ ] 工作树干净（`git status`），无未提交改动、无临时文件。
- [ ] 功能点 PR 已合并，合并后 `main` 的 push CI 全绿（未触发的按路径过滤说明原因）。
- [ ] 分支保护：当前因私有库 + GitHub Free 无法开启（TD-219）；升级 Pro 后补 required checks，并做一次"直推被拒"验证。
- [ ] `docs/handoff.md` §1 状态表与 README「当前状态」是最新的。
- [ ] 未处置技术债清单已读（`docs/tech-debt.md`），知道哪些条目目标在下一个里程碑。
- [ ] 待确认决策已列明（当前：无阻塞；M1-C2 四项细节已确认）。
- 仓库级：Dependabot alerts 与 security updates 已开启（2026-10-28；alerts 经 API `vulnerability-alerts` 返回 204 验证）。
- [ ] 待用一次真实安全 PR 验证 `security` 工作流随 PR 运行（工作流与 `.github/dependabot.yml` 已在 main；`security` 已随 PR #1、PR #7 与 main 推送实测绿，待 Dependabot 安全 PR 出现后验证其 `pull_request` 触发）。
- [ ] 本机环境可复现：测试栈起来、后端/前端测试全绿、`scripts/ci.sh` 全绿。
