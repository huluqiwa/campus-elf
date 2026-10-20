# 校园陪伴精灵 — 角色权限矩阵（rbac-matrix.md）

| 项 | 内容 |
|---|---|
| 版本 | v1.0（初版，待逐项确认） |
| 日期 | 2026-10-19 |
| 状态 | 待用户确认 |
| 规则 | 服务端鉴权；前端菜单/按钮不作为安全边界；跨租默认拒绝 |

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
- 敏感字段额外权限 `personnel.sensitive.read`，需理由+审计。

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
| `campus.chat` | AI 对话 |
| `campus.feedback.create` | 反馈 |
| `campus.conversation.clear` | 清空当前会话 |
| `campus.message.view/send` | 消息（系统/审批+师生 1:1） |
| `campus.leave.create/withdraw` | 请假 |
| `campus.account_termination.request` | 自助删除 |
| `campus.nav` | 业务入口导航 |

### 3.4 审计/观测

| 权限 | 说明 |
|---|---|
| `audit.view` | 审计查看（按租户） |
| `audit.export` | 审计导出（平台） |
| `observability.trace.view` | 本地 trace 查看（平台/运维） |
| `observability.langfuse.access` | Langfuse 访问（平台/运维） |
| `eval.run` | 运行评估 |
| `eval.report.view` | 查看评估报告 |

---

## 4. 角色-权限矩阵（初版）

> √=允许；△=需额外授权/条件；—=不允许。所有允许均受数据范围与租户约束。

| 权限组 | 平台超管 | 平台运营 | 平台审计 | 校方超管 | 校方管理员 | 教师 B 端 | 学生 C 端 | 教师 C 端 |
|---|---|---|---|---|---|---|---|---|
| 平台租户审核 | √ | √ | — | — | — | — | — | — |
| 平台配置 | √ | △ | — | — | — | — | — | — |
| 平台审计 | √ | — | √ | — | — | — | — | — |
| 平台消息 | √ | √ | — | — | — | — | — | — |
| 校方看板 | — | — | — | √ | √ | — | — | — |
| 反馈查看/答复 | — | — | — | √ | △ | — | — | — |
| 人员查看 | — | — | — | √ | √ | △ | — | — |
| 人员增删改/导入 | — | — | — | √ | △ | — | — | — |
| 部门树维护 | — | — | — | √ | △ | — | — | — |
| 敏感字段完整查看 | — | — | — | √ | △ | — | — | — |
| 权限账号/角色 | — | — | — | √ | △ | — | — | — |
| KB 查看/构建进度 | — | — | — | √ | √ | △ | — | — |
| KB 上传 | — | — | — | √ | △ | — | — | — |
| KB 审批 | — | — | — | √ | △ | — | — | — |
| KB 构建/测试 | — | — | — | √ | △ | △ | — | — |
| KB 发布/回滚/下线 | — | — | — | √ | △ | — | — | — |
| KB 分块预览/检索测试 | — | — | — | √ | △ | △ | — | — |
| KB 人群 ACL 配置/确认 | — | — | — | √ | △ | — | — | — |
| KB 块编辑提交 | — | — | — | √ | △ | — | — | — |
| KB 块编辑确认/派生 | — | — | — | √ | — | — | — | — |
| KB 沙箱忽略 ACL | — | — | — | √ | — | — | — | — |
| 审批待办/请假/删除 | — | — | — | √ | △ | △ | — | — |
| 校方消息 | — | — | — | √ | √ | △ | — | — |
| 校方审计 | — | — | — | √ | △ | — | — | — |
| C 端对话/反馈/消息 | — | — | — | — | — | — | √ | √ |
| C 端请假 | — | — | — | — | — | — | √ | — |
| C 端删除申请 | — | — | — | — | — | — | √ | √ |
| 本地 trace/Langfuse | √ | △ | — | — | — | — | — | — |
| 评估运行/报告 | √ | △ | — | — | △ | — | — | — |

---

## 5. 关键约束

1. **租户隔离**：所有校方/C 端权限只在 token tenant_id 内生效；跨租 404。
2. **数据范围**：权限允许但数据范围外不可见；班主任/辅导员按关系；教师按任教班级/课程。
3. **敏感字段**：`personnel.sensitive.read` 额外授权；完整查看需理由+审计；导出默认脱敏。
4. **审批分权**：提交人不能自批；超管例外默认关闭；SaaS 审核员不得审核自己提交学校。
5. **发布/删除**：KB 发布/回滚/下线、账号删除/撤权为高风险权限，建议只授予校方超管/指定管理员。
6. **平台跨租**：`platform.cross_tenant.read` 需二次授权（工单/审批）+ 审计；只读最小必要。
7. **C 端与 B 端分离**：C 端教师不自动拥有 B 端审批/知识库权限；需创建 B 端权限账号。
8. **权限变更**：立即生效；现有 token 不携带权限快照的，服务端每次查权限；如缓存，需失效。
9. **审计**：权限变更、敏感查看、审批、发布、删除、配置变更全部审计。
10. **前端**：菜单/按钮按权限隐藏，但服务端必须再次校验。

---

## 6. 待确认

- 校方管理员默认权限集是否包含 KB 审批/发布，还是仅超管？
- 班主任/辅导员是否自动获得 `school.approval.leave`，还是需显式授权？
- 教师 B 端角色是否允许上传 KB，还是仅测试？
- 平台运营是否可以修改模型密钥，还是仅超管？
- 审计导出是否仅平台审计/超管？
- 是否需要“校方审计员”只读角色？
- 数据范围是否支持"年级/院系"层级（当前只有全校/班级/课程/个人）。
- （已确认 2026-10-21）部门/科室组织单元与文档人群 ACL 模型见 `docs/spec-changes/SC-01-kb-acl-chunk-edit.md`：部门作为**人群选择器**数据源，不进入 `data_scopes`。
