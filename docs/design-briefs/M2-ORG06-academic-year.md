# M2-ORG06 — 入学年份与学年流转（班级模型修订）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-25 |
| 状态 | **已确认**（2026-10-25 用户确认方向与三项决策：唯一键=入学年份+班名 / 归档=行保留+审计 / 毕业学生账号本点不动） |
| 关联功能点 | M2 ORG-06 入学年份与学年流转（spec §3.5，2026-10-25 增补） |
| 验收引用 | docs/spec.md ORG-06 验收；docs/milestone.md §5.2 |
| 背景 | TD-181：现行 (年级,班名) 唯一键下每年开学年级标签失真；本方案为 TD-181 的**精简采纳版**（无编号、无毕业状态机，原全量方案留档于 git 历史 8c2820d） |

## 1. 目标与验收

1. 班级增 `entry_year`（入学年份，建班必填，**建档后不可改**——改等于换届，删了重建）；原 `grade` 字段语义改为**当前年级**（可随学年流转刷新）。
2. 唯一键改为 `(tenant_id, entry_year, name)`：同届内班名唯一；跨届同名自然共存（新高一 1 班与升上去的旧 1 班），升年级与建新届的先后顺序无关。**(当前年级, 班名) 不设唯一**（正是为避开上述顺序坑）。
3. 学年流转：校方触发，按"年级映射"批量刷新当前年级（高一→高二…），毕业年级班级打**归档标记**（`status='archived'`，行永不删除）；学生、班主任/辅导员指派、数据范围行**全部不动**。
4. 留级/跳级/插班 = 把学生重指派到目标年级的班（沿用既有学生编辑，无新机制）；学生年级口径始终跟班。
5. "历史可查看"落地：班级行永久保留 + 归档标记 + 流转操作审计（单条审计含完整映射与影响班级清单）；标准升学的年级沿革可由入学年份推算，**不建历史表**。
6. 毕业年级**学生账号本点不动**（仍可登录，档案照常）——毕业禁登录状态机从本方案裁剪，未来需要时随 TD-181 原方案扩展。

**验收**（spec ORG-06）：
- 同届同名班 409；跨届同名班可建（含"先建新届再流转"顺序）。
- 流转后：匹配班级当前年级更新、毕业年级归档；学生/指派/范围行逐项不变；重复提交同幂等键 → 409 不二次执行。
- dry_run 预览返回将刷新/归档的班级清单（含入学年份），不写库。
- 留级学生跟新班口径；审计含操作者/映射/影响计数。

## 2. 候选方案（关键分叉）

- **D1 唯一键（已确认 A）**：(入学年份, 班名) 唯一 ✅；备选 (当前年级, 班名) 因"先建新届再流转会撞名"否决。
- **D2 归档（已确认 A）**：行保留 + 归档标记 + 审计 ✅；独立年级历史表否决（标准沿革可由入学年份推算，多数数据冗余）。
- **D3 毕业学生（已确认 A）**：本点只归档班级、学生账号不动 ✅；毕业禁登录从原 TD-181 方案裁剪，登记见 §6。
- **D4 流转防重（随简报确认）**：小表 `academic_year_rollovers`（id/tenant_id/idempotency_key 唯一/映射 jsonb/影响计数/操作者），重复键 409——**必要安全件**而非镀金：若校方"先建新届（高一）再流转"，二次执行会把新届也升上去，幂等键是唯一防线。dry_run 是操作主防线（预览清单含入学年份供人工核对）。

## 3. 选择结论

按已确认三项 + D4-A 执行；spec/milestone/design 同步回写（本文档提交内）。接口：`POST /school/classes/rollover`（payload：advances[{from_grade,to_grade,(可选 entry_year 过滤)}]、archive_grades[]、dry_run、idempotency_key）；班级 CRUD 增 entry_year（必填/不可改/可筛选）；`status` 列（active/archived）。迁移一张新表 + classes 三处调整（加列/换唯一键/加 status）。

## 4. 实现顺序与 TDD 清单

1. 迁移：classes 加 `entry_year`（必填，测试造数回填）+ `status`（默认 active）+ 唯一键切换；建 `academic_year_rollovers`。
2. 班级 CRUD：create（entry_year 必填）、update（entry_year 不可改）、list（按 entry_year/status 筛选）；红→绿：同届同名 409 / 跨届同名可建 / entry_year 不可改。
3. 流转服务 + 端点：dry_run / 幂等键 409 / 单事务（批量刷新 + 归档）/ 审计；红→绿：范围行与学生行**逐项不变量**断言、重复键 409、中途失败全回滚。
4. 文档与守卫：design.md 端点表、bare-SQL 守卫豁免（如需）、导入定位回归（年级=当前年级语义不变，两列定位照旧可用）。

测试策略：单元（流转计算/幂等/映射校验 ≥8）+ 集成（迁移/唯一键/事务原子性/不变量）+ 业务场景 BS-M2-06（建班带入学年份→同届重名 409→跨届同名可建→dry_run→流转→不变量→归档→幂等重放→留级重指派→审计）。

## 5. 预判技术债

| TD | 条目 | 处置 |
|---|---|---|
| TD-181 | 原全量方案（编号 + 毕业状态机 + 跨表查重式防重） | 已采纳（精简版，本点）；编号与毕业禁登录裁剪项留档，未来需要时扩展 |
| TD-NEW | 流转单事务在万级学生校的性能 | 已延后 → M6 压测（班级行数仅几十~几百，预估可控） |

## 6. 高风险确认

中风险：批量写 + 唯一键迁移，无认证/租户/加密红线。三项用户决策 2026-10-25 已确认（见状态行）；D4 防重表为安全件随简报落定。

## 9. 实现结果

**交付物**：
- 迁移 `b9d3f7c1e5a9`：classes 增 `entry_year`（NOT NULL，存量回填 2024）+ `status`（active/archived）+ CHECK×2，唯一键 `(tenant_id, grade, name)` → `(tenant_id, entry_year, name)`；新表 `academic_year_rollovers`（`(tenant_id, idempotency_key)` 唯一 + RLS ENABLE/FORCE/policy/grant）；
- 模型：`OrgClass` 增列换约束；新增 `AcademicYearRollover`；启动自检 `REQUIRED_RLS_TABLES` 纳入批次表；
- 班级 CRUD：`create_class` 增 entry_year（必填、1970~2100 服务层预检）、`update_class` 不收 entry_year（路由对出现即 422）、`list_classes` 默认过滤归档 + entry_year 筛选；ClassResponse 增 entry_year/status；
- 流转服务 `app/org/academic_year.py`：`plan_rollover` 纯函数（校验 + 按原年级同时匹配，链式映射无级联）+ `execute_rollover`（dry_run / 幂等键批次行先行 / 按 id 集合应用 / 单条全量审计）；
- 端点：`POST /school/classes/rollover`（dry_run 缺省 True fail-safe）；**学生转班端点 `POST /school/students/{id}/transfer`**（见偏差 1）；
- 导入定位：`_locate_class` 加 `status='active'` 过滤 + 多命中 `CLASS_AMBIGUOUS` 行错误；
- 裸 SQL 守卫：`app/org/academic_year.py` 豁免登记（上限 10→11）。

**测试**：单元 12（规划分支全覆盖：正常升级/归档/先归档后升级/链式无级联/年份过滤/7 条拒绝路径）；BS-M2-06 全链路（三届建班→同届重名 409→entry_year 不可改 422→无权限 403→dry_run 预览不写库→执行→学生/任教/范围行**逐项不变量**→归档默认列表退出+显式可见→重复幂等键 409→流转审计→新届建班→留级转班→转入归档班 422）；存量用例更新（org_structure 造班补 entry_year/status、唯一约束用例改跨届语义、BS-M2-01/02 造班补 entry_year）。

**实现期偏差/要点**：
1. **学生转班能力缺口**：简报假设"沿用学生编辑"重指派——实现时发现 M2-ORG01 未交付学生编辑的分类能力（PUT students 仅身份证）。补建 `POST /school/students/{id}/transfer`（service.update_student_class：目标班存在且活跃校验、同班 no-op、审计 `org.student.transferred`），与"转班=个体操作不混批量流转"的确认口径一致；
2. **Pydantic 未知字段陷阱**：ClassUpdateRequest 原无 entry_year 字段时 PUT 携带该键被静默丢弃、守卫不触发——模型显式增字段（仅拦截用）+ 路由 422，并有 BS 用例钉住；
3. **导入定位歧义防线**：留届场景下两个活跃班可同（当前年级，班名）——定位加活跃过滤 + 多命中 CLASS_AMBIGUOUS 结构化行错误；
4. `grade` 列名不改、语义改当前年级（避免无谓的重命名迁移面）；迁移 entry_year 存量回填 2024（测试库无生产语义）；
5. 规划/执行分离与 dry_run 缺省 True：预览清单带入学年份是防"先建新届再流转误伤"的主防线，幂等键是数据库兜底。

## 10. 独立审查记录（2026-10-25）

- **第一轮：FAIL——Major 1 + Minor 5**。Major：归档态未回头收口既有班级写路径（建学生/排课可写归档班、归档班可硬删、历史 grade/name 可改写）。
  修复：新增 `_require_active_class` 统一探针（create_student / create_assignment 归档封闭 422 `CLASS_ARCHIVED`）；`delete_class` 归档拒删 409 `ORG_CLASS_ARCHIVED`；`update_class` 对归档行禁改 grade/name（422），班主任/辅导员档案纠错仍可（BS 断言钉住）。
- Minor 处置：① 不同幂等键并发分歧——加租户级 `pg_advisory_xact_lock` 串行化，残余风险登记 **TD-182**；② rollover 补 `_require_school_scope`（与 list_classes 对称，class 范围角色不得执行全校流转）；③ transfer 端点与审计动作回写 design.md、handoff/README 随提交更新；④ "中途失败全回滚"补测试（注入审计故障 → 班级与批次行零残留）；⑤ archive_grades 元素加 1~16 长度约束；⑥ 重放区分性错误码**不采纳**（幂等键 409 语义已足，避免错误码面膨胀）。
- 复测：BS-M2-06 两用例 14 passed（含新增归档封闭与回滚断言）；受影响集成/全量见提交前门禁。
- **第二轮（复审）：见 review-log**。
