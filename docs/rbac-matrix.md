# 校园陪伴精灵 — 角色权限矩阵（rbac-matrix.md）

| 项 | 内容 |
|---|---|
| 版本 | v1.2（§4 新增「对应权限码」列并接入机器校验；补齐 3 个漏项；观测/评估不下放租户角色 + 全量超管口径定稿） |
| 日期 | 2026-10-23（v1.1 = 2026-10-22 的 §6 七条口径定稿与权限码命名归一） |
| 状态 | 已确认（`docs/design-briefs/M1-E1-rbac-core.md` §4.1/§9） |
| 实现位置 | 权限项目录 `backend/app/rbac/catalog.py`；内置角色出厂默认 `backend/app/rbac/seeds.py`；落库同步 `backend/app/rbac/__init__.py:sync_rbac_definitions()`；一致性由 `backend/tests/test_rbac_matrix_unit.py` 钉住（§3 逐码相等 + §4 表格分区与逐列 √ 相等，见 §4 说明） |
| 规则 | 服务端鉴权；前端菜单/按钮不作为安全边界；跨租默认拒绝 |
| 两层口径 | 本文件 §3 的**权限项目录**跟代码走（每个码必须有真实校验点，否则是假开关）；§4 的角色↔权限映射与数据范围是**出厂默认**，入库后由运营在界面增删改（M2），同步只补缺失、不覆盖人工改动 |

---

## 1. 角色

### 1.1 平台方

| 角色 code | 名称 | 说明 |
|---|---|---|
| `platform_super_admin` | SaaS 超管 | 平台账号/角色、租户审核、配置、审计、平台消息 |
| `platform_operator` | SaaS 运营 | 学校审核、租户开通、平台消息、基础配置（按权限） |
| `platform_auditor` | SaaS 审计 | 只读审核记录/审计/租户状态 |

### 1.2 校方

| 角色 code | 名称 | 说明 |
|---|---|---|
| `school_super_admin` | 校方超管 | 本校全部权限；唯一超管删除需 SaaS 复核 |
| `school_admin` | 校方授权管理员 | 按权限矩阵与数据范围；可被授予审批/知识库/人员权限 |
| `teacher_b_end` | 教师（B 端权限） | 按授权；如班主任/辅导员请假审批、知识库测试 |
| `student` / `teacher` | C 端用户 | 仅 C 端能力；不默认有 B 端权限 |

> 教师 C 端身份与 B 端权限账号分离；B 端权限通过 `teacher_b_end` 角色或自定义角色授予。
>
> （2026-10-22 定稿）`student`/`teacher` 作为**租户内置角色**与三个 B 端角色一起入库：C 端接口按 `campus.*` 鉴权，没有角色就会被 403 挡住。本期内置角色就这五个，"校方审计员"只读角色与自定义角色 M2 再开（§6-6）。

---

## 2. 数据范围

| code | 范围 | 说明 |
|---|---|---|
| `school` | 全校 | 校方超管/授权管理员 |
| `class` | 班级 | 班主任/任课教师 |
| `course` | 课程 | 任课教师 |
| `self` | 个人 | 学生/教师本人 |

- 数据范围通过 `data_scopes` 绑定角色/用户；服务端查询强制过滤。
- 一个用户可有多个范围；取并集，但不得跨租。
- 敏感字段额外权限 `school.personnel.sensitive.read`，需理由+审计。
- （2026-10-22 定稿）交付节奏：M1-E1 只落 `school`/`self` 的模型与默认语义，`class`/`course` 的服务端过滤与"教师按任教班级/课程"关系在 M1-E2/M2 实现；年级/院系层级不进 `data_scopes`（§6-7，部门作为人群选择器见 `docs/spec-changes/SC-01-kb-acl-chunk-edit.md`）。

---

## 3. 权限点（模块.动作）

### 3.1 平台

| 权限 | 说明 |
|---|---|
| `platform.tenant.view` | 查看租户/审核记录 |
| `platform.tenant.review` | 审核通过/拒绝 |
| `platform.tenant.activate` | 开通/暂停 |
| `platform.tenant.export` | 导出审核记录 |
| `platform.config.view` | 查看配置（密钥掩码） |
| `platform.config.edit` | 编辑配置 |
| `platform.config.approve` | 审批配置变更 |
| `platform.audit.view` | 查看审计 |
| `platform.audit.export` | 导出审计 |
| `platform.message.view/send` | 平台消息 |
| `platform.cross_tenant.read` | 跨租只读（需二次授权+审计） |

### 3.2 校方

| 权限 | 说明 |
|---|---|
| `school.dashboard.view` | 基础看板 |
| `school.feedback.view` | 反馈查看 |
| `school.feedback.update` | 标记处理/答复 |
| `school.org.manage` | 部门树维护（A30） |
| `school.personnel.view` | 人员查看 |
| `school.personnel.create/edit/delete` | 人员维护 |
| `school.personnel.import` | 批量导入 |
| `school.personnel.sensitive.read` | 身份证等完整查看（理由+审计） |
| `school.permission_account.manage` | 权限账号/角色 |
| `school.role.manage` | 角色/权限分配 |
| `school.kb.view` | 文档/构建进度 |
| `school.kb.upload` | 上传 |
| `school.kb.approval` | 知识库审批 |
| `school.kb.build` | 构建/重试 |
| `school.kb.test` | 测试/测试集 |
| `school.kb.publish` | 发布/回滚/下线 |
| `school.kb.delete` | 删除 |
| `school.kb.view_chunks` | 分块预览/检索测试 |
| `school.kb.acl.manage` | 文档查阅人群配置与确认（K4） |
| `school.kb.chunk.edit` | 提交块编辑（K5） |
| `school.kb.chunk.confirm` | 确认块编辑并派生版本（K5，仅超管） |
| `school.kb.retrieval_test.unfiltered` | 沙箱"忽略 ACL"对比视图（K4，仅超管，强制审计） |
| `school.approval.todo` | 待办/审批 |
| `school.approval.leave` | 请假审批 |
| `school.approval.account_termination` | 删除/撤权审批 |
| `school.message.view/send` | 校方消息 |
| `school.message.read` | B 端已读 |
| `school.audit.view` | 本校审计 |
| `school.account_termination.request` | 发起撤权 |
| `school.account_termination.appeal/freeze` | 申诉/冻结处理 |

### 3.3 C 端

| 权限 | 说明 |
|---|---|
| `campus.chat.use` | AI 对话（原写作 `campus.chat`，v1.1 归一为「模块.动作」格式） |
| `campus.feedback.create` | 反馈 |
| `campus.conversation.clear` | 清空当前会话 |
| `campus.message.view/send` | 消息（系统/审批+师生 1:1） |
| `campus.leave.create/withdraw` | 请假 |
| `campus.account_termination.request` | 自助删除 |
| `campus.nav` | 业务入口导航 |

### 3.4 审计/观测

| 权限 | 说明 |
|---|---|
| `observability.trace.view` | 本地 trace 查看（平台/运维） |
| `observability.langfuse.access` | Langfuse 访问（平台/运维） |
| `eval.run` | 运行评估 |
| `eval.report.view` | 查看评估报告 |

> （2026-10-22 归一）审计查看/导出**不设无 realm 的通用码**：本校审计用 `school.audit.view`，平台侧审计用 `platform.audit.view` / `platform.audit.export`。原 `audit.view`/`audit.export` 与上述码重复、且无法判定 realm，已从目录与库中删除。

---

## 4. 角色-权限矩阵（初版）

> √=允许；△=需额外授权/条件；—=不允许。所有允许均受数据范围与租户约束。
>
> √/△ 的两层含义（2026-10-22 定稿）：√ 是**出厂默认**（内置角色入库即有），△ 是**可授予但默认不给**；入库后运营可在管理界面增删映射（M2），同步只补缺失不覆盖人工改动。
>
> KB 上传（2026-10-22 改口径）：对持后台账号的角色默认开放（含教师 B 端）。配套约束：上传者**永不自批**（M1-E2 实现）；上传后设置查阅人群默认限本人/本人任课范围，**扩到全校需管理员二次确认并留审计**（M3，依赖 `school.kb.acl.manage`）。

> **矩阵如何被机器校验（2026-10-23 补）**：每个权限组一行「对应权限码」，行与行不重不漏、并集等于
> §3 的 59 码；8 个角色列与 `backend/app/rbac/seeds.py` 的**出厂默认**逐列相等（√ 集合 = 该角色默认
> 权限集，△ 集合 ∉ 默认）。两个「范围内全量」角色按范围比对，不按 `seeds` 里的字面量：
> 校方超管 = 全部租户域码；平台超管列只覆盖平台运维域码（16 个）——它的 `seeds` 默认按 §5-11 持有
> 目录全量，但对租户域行仍是 —，因为平台主体走校方接口会被 realm 守卫拒绝（`require_permission(..., realm=)`），
> 「持有码」≠「用得上」。断言在 `backend/tests/test_rbac_matrix_unit.py::test_matrix_section4_*`。

| 权限组 | 对应权限码 | 平台超管 | 平台运营 | 平台审计 | 校方超管 | 校方管理员 | 教师 B 端 | 学生 C 端 | 教师 C 端 |
|---|---|---|---|---|---|---|---|---|---|
| 平台租户查看 | `platform.tenant.view` | √ | √ | √ | — | — | — | — | — |
| 平台租户审核/开通/导出 | `platform.tenant.review/activate/export` | √ | √ | — | — | — | — | — | — |
| 平台配置查看 | `platform.config.view` | √ | √ | — | — | — | — | — | — |
| 平台配置编辑/审批 | `platform.config.edit/approve` | √ | — | — | — | — | — | — | — |
| 平台审计 | `platform.audit.view/export` | √ | — | √ | — | — | — | — | — |
| 平台消息 | `platform.message.view/send` | √ | √ | — | — | — | — | — | — |
| 平台跨租只读 | `platform.cross_tenant.read` | √ | — | — | — | — | — | — | — |
| 本地 trace/Langfuse | `observability.trace.view` `observability.langfuse.access` | √ | △ | — | — | — | — | — | — |
| 评估运行/报告 | `eval.run` `eval.report.view` | √ | △ | — | — | — | — | — | — |
| 校方看板 | `school.dashboard.view` | — | — | — | √ | √ | — | — | — |
| 反馈查看/答复 | `school.feedback.view/update` | — | — | — | √ | △ | — | — | — |
| 人员查看 | `school.personnel.view` | — | — | — | √ | √ | △ | — | — |
| 人员增删改/导入 | `school.personnel.create/edit/delete/import` | — | — | — | √ | △ | — | — | — |
| 部门树维护 | `school.org.manage` | — | — | — | √ | △ | — | — | — |
| 敏感字段完整查看 | `school.personnel.sensitive.read` | — | — | — | √ | △ | — | — | — |
| 权限账号/角色 | `school.permission_account.manage` `school.role.manage` | — | — | — | √ | △ | — | — | — |
| KB 查看/构建进度 | `school.kb.view` | — | — | — | √ | √ | △ | — | — |
| KB 上传 | `school.kb.upload` | — | — | — | √ | △ | √ | — | — |
| KB 审批 | `school.kb.approval` | — | — | — | √ | △ | — | — | — |
| KB 构建/测试 | `school.kb.build/test` | — | — | — | √ | △ | △ | — | — |
| KB 发布/回滚/下线 | `school.kb.publish` | — | — | — | √ | △ | — | — | — |
| KB 删除 | `school.kb.delete` | — | — | — | √ | △ | — | — | — |
| KB 分块预览/检索测试 | `school.kb.view_chunks` | — | — | — | √ | △ | △ | — | — |
| KB 人群 ACL 配置/确认 | `school.kb.acl.manage` | — | — | — | √ | △ | — | — | — |
| KB 块编辑提交 | `school.kb.chunk.edit` | — | — | — | √ | △ | — | — | — |
| KB 块编辑确认/派生 | `school.kb.chunk.confirm` | — | — | — | √ | — | — | — | — |
| KB 沙箱忽略 ACL | `school.kb.retrieval_test.unfiltered` | — | — | — | √ | — | — | — | — |
| 审批待办/请假/删除 | `school.approval.todo/leave/account_termination` | — | — | — | √ | △ | △ | — | — |
| 校方消息 | `school.message.view/send/read` | — | — | — | √ | √ | △ | — | — |
| 校方审计 | `school.audit.view` | — | — | — | √ | △ | — | — | — |
| 账号撤权申请/申诉/冻结 | `school.account_termination.request/appeal/freeze` | — | — | — | √ | △ | — | — | — |
| C 端对话/反馈/消息 | `campus.chat.use` `campus.feedback.create` `campus.message.view/send` | — | — | — | √ | — | — | √ | √ |
| C 端清会话/导航 | `campus.conversation.clear` `campus.nav` | — | — | — | √ | — | — | √ | √ |
| C 端请假 | `campus.leave.create/withdraw` | — | — | — | √ | — | — | √ | — |
| C 端删除申请 | `campus.account_termination.request` | — | — | — | √ | — | — | √ | √ |

> 「KB 删除」与「账号撤权申请/申诉/冻结」「C 端清会话/导航」三行是补齐分区时加的：§3 有码、§4 却没有
> 对应行，等于矩阵口径漏了这些权限点（分区测试会直接报「行并集 ≠ 目录」）。KB 删除按 §5-5 高风险口径
> 处理：超管 √、授权管理员 △，不默认给。

**两处定稿（2026-10-23，M1-E1 第三轮审查提出、用户确认）**：

- **观测与评估不下放租户角色**：`observability.*` 与 `eval.*` 只属平台运维域。代码口径是
  `app/rbac/catalog.py:code_realm()` 的租户模块白名单 `('school','campus')`，出厂默认、种子自检与
  同步守卫都据此拒绝；这两类码**不给**任何 `school_*`/`teacher_b_end` 角色，矩阵此前给「校方管理员」
  打 △ 与实现冲突，已按本条改为 —。将来校方确实需要自评能力，新增 `school.eval.*` 专用码（一个码
  只属于一个 realm，同一码跨 realm 会让"码=服务端校验点"失去意义），而不是改这两个码的归属。
- **校方超管 = 本校 school+campus 两模块全量**（与 §1.2「本校全部权限」、`provision_tenant()` 与
  `seeds.py:TENANT_SUPER_ADMIN_PERMISSIONS` 同一口径）：C 端四行对超管也是 √，超管账号可直接使用
  本校 C 端能力，无需再挂 `student`/`teacher`。平台超管同理持有目录全量（§5-11）。

---

## 5. 关键约束

1. **租户隔离**：所有校方/C 端权限只在 token tenant_id 内生效；跨租 404。
2. **数据范围**：权限允许但数据范围外不可见；班主任/辅导员按关系；教师按任教班级/课程。
3. **敏感字段**：`school.personnel.sensitive.read` 额外授权；完整查看需理由+审计；导出默认脱敏。
4. **审批分权**：提交人不能自批；超管例外默认关闭；SaaS 审核员不得审核自己提交学校。
5. **发布/删除**：KB 发布/回滚/下线、账号删除/撤权为高风险权限，建议只授予校方超管/指定管理员。
6. **平台跨租**：`platform.cross_tenant.read` 需二次授权（工单/审批）+ 审计；只读最小必要。
7. **C 端与 B 端分离**：C 端教师不自动拥有 B 端审批/知识库权限；需创建 B 端权限账号。
8. **权限变更**：**下一次请求即生效，且不作废在线会话**（2026-10-22 定稿）。实现口径：权限不写进访问令牌，`Principal` 每请求从库读角色与权限（`backend/app/auth/principal.py`）；因此放宽/收紧权限都即时生效，正在使用的用户不会被踢下线。会话轮换（`token_version` 自增 + 撤刷新令牌族）只用于账号级安全事件：改密、MFA 开关、管理员踢设备。若将来引入缓存，必须同时覆盖"角色改映射/用户改角色/数据范围改"三条失效路径（TD-100 → M6）。
   - **例外（不要读成"任何时候都能立刻断开某人"）**：本条只保证权限判据每请求从库读。踢下线/设备撤销依赖 Redis 会话标记，Redis 不可用或标记删除失败时鉴权对该检查 fail-open，被踢设备的访问令牌最长仍可用 15 分钟（TD-111 → M6）。"立即撤权 + 立即断会话"是两件事，需要后者时按账号级安全事件走会话轮换。
9. **审计**：权限变更、敏感查看、审批、发布、删除、配置变更全部审计。
10. **前端**：菜单/按钮按权限隐藏，但服务端必须再次校验。
11. **realm/模块边界**：`permissions` 是全局目录（`platform.*` 与校方/C 端码同表）。
    - **realm 由码的模块派生，不设第二份标注**（M1-E1 定稿）：`catalog.code_realm()` 用租户域模块白名单 `('school','campus')` 判分，**`observability.*`/`eval.*` 属平台运维域**，不属于任何租户角色。
    - **租户角色不得引用平台域的权限**：应用层路由按 realm 断言（`require_permission(..., realm="platform")`），种子自检逐角色校验（`_self_check_seeds`），启动自检逐租户统计越界映射（`app/db/selfcheck.py:_check_realm_permission_matrix`，判据就是 `catalog.TENANT_MODULES`，`platform`/`observability`/`eval` 一并拒绝；第四轮审查 M1 前它只查 `module='platform'`）；数据库层由触发器 `role_permissions_realm_guard` 强制（迁移 `b7d1e3f5a9c2`）。注意该触发器目前只硬挡 `module='platform'`，`observability`/`eval` 仍可被直接 INSERT 进租户角色 —— 收紧为模块白名单在 M1-E2 与范围守卫迁移合并做一次（TD-149）。
    - 平台角色（运营方）**允许持有全量权限**（含校方/C 端模块）：平台是超集运营角色，且平台路由只接受平台 realm 主体；因此不限制平台角色引用的模块（迁移 `c8e2f4a6b1d3` 移除了平台方向守卫）。

---

## 6. 定稿（原"待确认"，2026-10-22 用户逐项确认）

| # | 事项 | 结论 |
|---|---|---|
| 1 | 校方管理员默认权限集是否包含 KB 审批/发布 | **不含**：`school_admin` 出厂默认只有矩阵"校方管理员"列 √ 的码，KB 审批/发布为 △，需超管显式授予 |
| 2 | 班主任/辅导员是否自动获得 `school.approval.leave` | **不自动**：需显式授予；"按任教关系授予"的动作依赖 M2 组织模型，服务端过滤在 M1-E2 |
| 3 | 教师 B 端角色是否允许上传 KB | **允许（用户改口径）**：`teacher_b_end` 出厂默认含 `school.kb.upload`；配套约束见 §4 说明（永不自批 + 人群默认限本人/任课，扩全校需管理员确认） |
| 4 | 平台运营是否可以修改模型密钥 | **不能**：`platform.config.edit`/`platform.config.approve` 仅超管；运营只给 `platform.config.view` |
| 5 | 审计导出是否仅平台审计/超管 | **是**：`platform.audit.export` 给超管 + 平台审计员，不给运营 |
| 6 | 是否需要"校方审计员"只读角色 | **本期不加**：内置角色固定为 §1.2 的五个；M2 有真实需求再开自定义角色 |
| 7 | 数据范围是否支持"年级/院系"层级 | **本期不扩**：`data_scopes` 只有 `school`/`class`/`course`/`self`；M1-E1 只落 `school`/`self` 语义，层级留待 M2 组织模型确认 |

- （已确认 2026-10-21）部门/科室组织单元与文档人群 ACL 模型见 `docs/spec-changes/SC-01-kb-acl-chunk-edit.md`：部门作为**人群选择器**数据源，不进入 `data_scopes`。
- （已确认 2026-10-22）权限码归一：`campus.chat` → `campus.chat.use`；删除与 realm 专用码重复的 `audit.view`/`audit.export`（§3.4 说明）；库里 **9 个**无引用的旧码（`school.profile.view/edit`、`school.user.view/create/edit/delete`、`school.role.assign`、`school.leave.approve`、`campus.leave.submit`）删除，本文件 §3 是唯一口径。
- （M1-E1 已交付 2026-10-23）§3 全量 **59 码**（platform 12 + school 34 + campus 9 + observability 2 + eval 2）已随 `e5f7a9c1b3d5` 迁移落库，`permissions` 与 `app/rbac/catalog.py` 逐码相等；§4 的出厂默认落 `rbac_builtin_roles`/`rbac_builtin_role_defaults` 两张**代码持有的定义表**，由 `seed_builtin_roles(tenant_id)` 在新租户开通/审核通过时写入，`sync_rbac_definitions()` 只补存量缺失、不覆盖人工改动。`data_scopes` 本期**不写行**（读取方在 M1-E2 数据范围过滤落地时接入）。
