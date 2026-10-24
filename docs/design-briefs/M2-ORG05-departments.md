# M2-ORG05 — 部门/科室树（ORG-05，A30）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-25 |
| 状态 | 已确认（2026-10-25：D1-D4 均选方案 A） |
| 关联功能点 | M2 第 3 点：ORG-05 部门树 + 教师部门归属 + 导入扩展（复用 ORG-02） |
| 验收引用 | docs/spec.md §3.5 ORG-05（未知部门→错误行；调岗审计前后值+人群按新部门生效；父部门人群含子部门=层级包含；未分班学生仅自身/角色）、docs/design.md §4.2/§5.4、docs/rbac-matrix.md 行 175（`school.org.manage`） |

## 1. 目标

1. 部门树 `departments`（层级 + path）+ 教师单一部门归属（`teachers.department_id` 加列——M2-ORG01/03 §9.7 已预告）+ CRUD/树查询接口。
2. **导入扩展**（复用 ORG-02）：教师导入增加可选部门列，未知部门 → 错误行（不产生半成品）。
3. 为 M3 KB-09 人群选择器供数：部门是人群维度之一，**层级包含**（父部门人群命中子部门成员）靠 path 前缀查询支撑。
4. 调岗审计：教师改部门 → `org.teacher.updated` 审计含变更前后值。

**不含**：学生部门归属（spec：学生沿用班级+年级）；KB-09 人群判定与缓存（M3——本项目范围/组织读取均为每请求查库，无应用层缓存，「缓存失效」验收天然满足，届时仅验证）；B 端页面。

## 2. 候选方案

### 决策 D1：树存储与子树查询

- **方案 A（推荐）：邻接表 + id 物化路径（design §4.2 原样）**——`departments` 含 `parent_id` 与 `path`（形如 `/` + 各级 id 以 `/` 连接，根为空串或 `/root_id`）。子树查询 `path LIKE '/{id}/%'`（索引友好）；改名不改路径（id 稳定）；调岗/人群判定（M3）用 path 前缀。
  - 缺点：移动子树需重写整棵子树的 path（事务内批量 update，本点实现并测试）。
- 方案 B：名称路径——人类可读，但改名即失效、重名（不同父下同名）歧义，需级联改写，劣。
- 方案 C：PostgreSQL ltree 扩展——查询强，但引入扩展依赖与迁移复杂度，本规模（校级部门树，几十~几百节点）不值。

### 决策 D2：教师部门归属数量

- **方案 A（推荐）：单一部门**（design：`teachers.department_id` 单列）。校内语义=教师的行政归属科室/院系；多部门归属无 spec 依据。
- 方案 B：多归属（中间表）——无需求支撑，M3 人群判定复杂化。

### 决策 D3：导入的部门引用格式（教师导入可选列 `department`）

- **方案 A（推荐）：名称全路径**（如 `计算机学院/软件工程系`，以 `/` 分层）——校方用户按名称思考，导入文件可读；解析按 path 逐级匹配（全路径天然无歧义）；未知/歧义 → 错误行 `DEPARTMENT_NOT_FOUND`（验收口径）。
- 方案 B：部门 `code`——精确但要求导入前先录 code，校方准备数据负担高。
- 两者都收？不——双格式增加歧义面（`/` 在名称内的转义问题），只收名称全路径。

### 决策 D4：删除与停用语义

- **方案 A（推荐）**：`status=active/disabled`（design 有 status 列）；**删除（硬删）仅允许叶子且无在职教师归属**——有子部门或有教师 → 409；停用不受限（停用后不可接新归属，已有归属保留并提示）。部门不参与范围行/审计哈希链外的东西，硬删无级联风险。
- 方案 B：一律软删（deleted 状态）——departments 无隐私残留（纯组织结构），软删徒增树查询过滤复杂度。

### 范围内子决策（随简报确认）

- **教师调岗**：新增 `PUT /school/teachers/{id}`（本点交付，ORG-01 时按 design 只做了 GET/POST）——body 仅收 `department_id`（调岗语义），变更前后值入审计；调岗不影响 class 范围行（班级范围来自任教关系，与部门正交，TD-174 对账不受影响）。
- **树查询**：`GET /school/departments` 返回整棵树（校级规模一次拉全量，扁平+parent_id 由前端组树；不用 cursor——树接口天然全量，cursor 无意义，记录偏差）。
- **导入扩展**：教师 CSV/XLSX 可选列 `部门`（alias `department`/`department_path`）；空=不归属；解析后按名称全路径逐级匹配；students 导入不变。
- **KB-09 层级包含**：本点只保证数据形态（path 前缀可判定祖先）；人群命中逻辑在 M3 KB-09 交付时验证。

## 3. 选择结论

- D1 选 A（id 物化路径）、D2 选 A（单一部门）、D3 选 A（名称全路径）、D4 选 A（叶子+无归属才可硬删）。
- 是否需要改 spec/design：无需（全部按 design §4.2 原样；`path` 格式细节为实现口径）。

## 4. 实现顺序与 TDD 测试清单

1. 迁移 `f3b7d1e5a9c3`：`departments` 表（含 path/status，RLS+授权）+ `teachers.department_id` 加列（FK 复合、ondelete SET NULL）+ selfcheck/清理清单更新。
2. 模型 `app/db/models/org.py` 增 `Department`。
3. 服务 `app/org/service.py` 增部门树（创建/改名改码/停用/删除/移动含子树 path 重写/树查询）+ `update_teacher_department`（调岗+审计）。
4. 解析器扩展：教师导入 `department` 可选列。
5. 接口：`GET/POST /school/departments`、`PUT/DELETE /school/departments/{id}`、`PUT /school/teachers/{id}`（调岗）；导入任务接入部门解析。
6. 界面：无（B 端页面功能点统一）。

红→绿清单：
- [ ] 单元：path 构建与子树前缀、移动重写、名称全路径解析（歧义/未知）
- [ ] 集成：唯一 `(tenant_id, parent_id, name)`、RLS、移动后 path 重写正确性、调岗审计前后值、导入未知部门错误行、删除 409（有子/有教师）
- [ ] 业务场景 BS-M2-03：建树（院→系）→ 教师导入带部门 → 调岗（审计前后值）→ 删除有教师部门 409 → 未知部门导入错误行 → 未分班学生导入正常
- [ ] E2E：无新增

## 5. 测试策略

| 层级 | 内容 |
|---|---|
| 单元 | path 构建/前缀/移动重写/路径解析 |
| 集成 | 真实 PG：唯一约束、RLS、移动、审计、导入扩展、删除 409 |
| 业务场景 | BS-M2-03 全链路（断言库状态/审计前后值/错误报告，非 HTTP 200） |
| E2E / 性能 / AI 评估 | 不涉及 |

## 6. 预判技术债

| TD | 条目 | 处置 |
|---|---|---|
| （ORG-01/03 §9.7 预告项） | teachers.department_id 加列迁移 | 本点**已完成** |
| TD-NEW | 部门树移动的 path 重写为大事务（子树行数线性） | 校级规模可忽略；登记观察项，M6 评审 |
| KB-09 层级包含/人群缓存 | 本点只保数据形态 | M3 交付时验证（路径前缀查询已就绪） |

## 7. 高风险确认

- 是否高风险：**否**（组织结构 CRUD，不涉删除级联/加密/发布；调岗仅改一个可空外键并审计）。
- 需要用户确认：4 项决策（D1-D4）+ 本简报整体。
- 确认结论：**已确认（2026-10-25）**——D1 邻接表+id 物化路径、D2 单一部门、D3 名称全路径、D4 叶子+无归属才可硬删。

## 8. 审查对照

- 审查重点：移动子树 path 重写的完整性（漏改=层级包含错判，直接影响 M3 人群）；导入路径解析的歧义处理；删除 409 条件；审计前后值；RLS。
- 允许偏差：列名/错误码文本；用例数 ±3。

## 9. 实现结果

**交付物**：迁移 `a4c8e2f6b1d3`（departments 表 + RLS + 授权 + 根部门部分唯一索引 + path 索引 + teachers.department_id 加列）；`app/db/models/org.py` 增 Department；`app/org/departments.py` 独立树服务模块（建/改名改码停用/删除/移动含子树 path 重写/整树查询/调岗/名称全路径解析）；`app/org/import_task.py` 教师行部门解析（未知 → DEPARTMENT_NOT_FOUND 错误行，field=department）；`app/api/school_org.py` 增 4 端点（GET/POST/PUT/DELETE departments + PUT teachers/{id} 调岗）；`create_teacher` 增可选 department_id。

**测试**：集成 8→9（移动重写+环检测、调岗、唯一约束）；BS-M2-03（建树→导入带部门（1 合法 1 未知错误行 field=department）→调岗审计前后值（部门名口径）→删除 409（有教师/有子）→调离后删叶子→未分班+部分提供错误行）。

**实现期偏差/要点（审查对照用）**：
1. 根部门同级唯一：唯一约束视 NULL 为互异，以部分唯一索引 `uq_departments_tenant_root_name` 补齐；
2. 成环检测沿 parent_id 链上溯（path 在移动事务内重写，旧祖先 id 不再出现于新父 path——path 判环失效）；
3. 学生导入年级/班级为**可选列**（未分班合法，spec ORG-05 验收口径），同时提供才解析定位，部分提供 → 错误行（VALUE_INVALID）；
4. 调岗接口权限用 `school.org.manage`（组织结构语义；矩阵行 174 人员编辑为 personnel.edit——调岗语义归组织结构，与 D3 班级/课程/任教同口径）；
5. 裸 SQL 守卫：departments.py 登记豁免（子树 path 重写无仓储原语，同 scopes_sync 类），上限 8→9；
6. 「人群按新部门生效（缓存失效）」验收：无应用层缓存需失效（范围/组织读取每请求查库），M3 KB-09 交付时验证层级包含（path 前缀查询已就绪）；
7. `teachers.department_id` 未随 M2-ORG01/03 迁移预留：FK 目标 departments 表本点才建，无法提前建 FK（§9.7 预告已兑现，本点加列）；
8. 班级/课程/任教维护取 `school.org.manage`（矩阵 §5 行 175 描述扩展为「组织结构维护」）；端点实际 15 条；
9. POST /school/teachers 增可选 department_id（M4 复审：design §5.4「含 department_id 归属」原样落地）；
10. 解析器部门列别名：部门/department/部门路径 三者均归一为 canonical department。
