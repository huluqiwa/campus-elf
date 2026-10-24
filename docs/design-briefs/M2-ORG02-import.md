# M2-ORG02 — CSV/XLSX 异步导入（ORG-02）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-24 |
| 状态 | 已确认（2026-10-24：D1-D4 均选方案 A；D4 澄清——文件即校方上传的师生数据 CSV/XLSX 本身，对象存储仅作上传暂存供 worker 读取） |
| 关联功能点 | M2 第 2 点：ORG-02 手动/批量导入（错误行报告、重复策略、失败重试） |
| 验收引用 | docs/spec.md §3.5（重复学号→错误行含行号/原因/不产生半成品；1000 行→进度/结果可查、失败行可重试）、docs/milestone.md §5.1/§5.4、docs/design.md §4.2（import_jobs/import_row_errors）/§5.4（POST /school/imports） |

## 1. 目标

1. 校方上传 CSV/XLSX 人员文件 → **异步**导入教师/学生档案（含 D4 的 1:1 账号）→ 进度与结果可查 → 失败行可重试。
2. 与已交付的 ORG-01/03 闭环：导入写入五表 + 触发 class 范围行同事务同步（TD-174 的对账决策点就在本点）；复用现有权限码（`school.personnel.import`，矩阵 §5 行 174）。

**不含**：ORG-05 部门列（后续加列扩展导入模板）、身份证列（D3 预留列位继续零读写）、凭据分发（见 D2）、模板下载接口（错误行报告即事实规范）。

## 2. 候选方案

### 决策 D1：任务执行机制

- **方案 A（推荐）：Arq worker**——M0 已验证 Arq 3.13 兼容，`app/workers/` 有清理任务先例；导入任务入队（Redis）→ worker 执行，进程崩溃可重入、天然异步。上传接口只落对象存储 + 建 job 行 + 入队，立即返回 job_id。
  - 缺点：开发/测试需考虑 worker 进程（测试直接调任务函数，业务场景测试加一条「经真实 worker」可选）。
- 方案 B：FastAPI BackgroundTasks——同进程执行，零新基建；但无持久化队列、进程重启任务丢失、与 M5 outbox/重试形态不一致，后续要返工。

### 决策 D2：导入建档的账号与初始凭据

- **方案 A（推荐）**：每行建档 = 建 `users` 行（不可猜测随机口令 + `must_change_password=true`，与手动建档 D4 同构）。**凭据分发不在本点**：有邮箱者可走既有邮件重置流程自助取回；无邮箱者由校管重置后台分发（TD-126，M2 后段功能点）。导入响应/报告**不含**任何口令。
  - 优点：1000 行不发 1000 封邮件（SMTP 时序不可控）、审计不含口令、红线安全；缺点：用户首登依赖重置流程，需 TD-126 尽快跟上。
- 方案 B：邮件分发初始密码——多数学生无邮箱，发送失败处理复杂，且批量发信时序会拖垮导入验收。
- 方案 C：导入不建账号（纯档案）——与 D4 矛盾，「谁能登录」分裂，M5 审批人解析复杂化。

### 决策 D3：重复/冲突策略

- **方案 A（推荐）**：job 级参数 `conflict=error`（默认）| `skip`；**不做 overwrite**（覆盖会静默改写账号/范围行，安全面大）。
  - `error`：学号/工号/登录名与库内现有行冲突 → 该行进错误报告（重复是显式决策，符合验收「按重复策略处理」）；
  - `skip`：文件内或库内重复 → 跳过该行并在报告标注 skipped；
  - 文件**内部**重复学号/工号：一律报错行（无论策略——同文件两行互相矛盾没法跳）。
- 方案 B：库内 overwrite/upsert——拒绝（见上）。

### 决策 D4：解析与文件处理

- **方案 A（推荐）**：新增依赖 `openpyxl`（XLSX 解析，纯 Python、无系统依赖）+ CSV 用标准库 `csv` + `charset-normalizer` 编码探测（已有依赖）。上传走既有对象存储（local/MinIO 同一接口，onboarding 先例），文件不落本地磁盘。行数上限 5000（验收 1000 行留 5 倍余量）、文件 ≤10MB，超限 422。
- 方案 B：pandas——依赖重（numpy 已有但 pandas 引入面大），只为本功能不值。

### 范围内子决策（无需单独确认，随简报确认）

- **失败行重试**：`POST /school/imports/{job_id}/retry` 基于**原文件 + 失败行号集合**生成派生任务（只导失败行），产出新 job_id 并在原 job 上挂 `retried_by`；错误行报告含原始行数据（`import_row_errors` 加 `raw_json` 列，偏离 design 表定义，回写 design.md）。
- **导入列**：教师=工号/姓名/年龄/性别/职位/邮箱(可选)；学生=学号/姓名/性别/年级+班名（定位已建班级，找不到→错误行）/邮箱(可选)；`login_id` 可选列，缺省=工号/学号。
- **TD-174 落地**：导入路径的学生/教师建档与任教无关（任教关系不在导入范围——班级指派后续在 B 端页面做），学生带 class_id 建档会触发范围行同步（复用 `sync_class_scope_rows`，批末统一同步一次 + 差量入审计）。对账任务视本点实测决定是否需要。

## 3. 选择结论

- D1 选 A（Arq）、D2 选 A（随机口令+重置分发）、D3 选 A（error|skip，无 overwrite）、D4 选 A（openpyxl+对象存储）。
- 是否需要改 spec/design：`import_row_errors` 加 `raw_json` 列与 `import_jobs.retried_by` 列（回写 design.md）；其余一致。

## 4. 实现顺序与 TDD 测试清单

1. 依赖与迁移：openpyxl 依赖；import_jobs/import_row_errors 建表迁移（含 raw_json/retried_by）。
2. 解析器：CSV/XLSX → 行记录流（编码探测、表头校验、行号从 2 起）。
3. 任务：`app/org/import_task.py`——逐行事务（建档/学生班级定位/范围行批末同步）→ 进度落库 → 错误行落库 → 状态机（pending/running/completed/completed_with_errors/failed）。
4. 接口：POST /school/imports（multipart，校验+落存储+建 job+入队）、GET /school/imports/{job_id}（进度+汇总）、GET /school/imports/{job_id}/errors（错误行分页）、POST /school/imports/{job_id}/retry。权限 `school.personnel.import`。
5. 界面：本点无前端（B 端页面功能点统一做导入 UI）。

红→绿清单：
- [ ] 单元：解析器（CSV/XLSX/编码/表头错/超限）、行校验（重复/缺列/非法值）、状态机
- [ ] 集成：真实 PG——1000 行导入（性能与时序）、错误行报告内容、retry 只导失败行、范围行同步批末一致（TD-174 对账断言）、对象存储文件读取
- [ ] 业务场景 BS-M2-02：上传含重复学号+错误行+合法行的文件 → 校管查进度 → 错误报告含行号/原因 → 库内只 commit 合法行（无半成品）→ 修正后 retry → 全部成功；权限：无 import 码 403
- [ ] E2E：本点无 UI，不新增

## 5. 测试策略

| 层级 | 内容 |
|---|---|
| 单元 | 解析器/校验/状态机/行定位 |
| 集成 | 真实 PG + 对象存储：1000 行、错误报告、retry、对账、权限 |
| 业务场景 | BS-M2-02 全链路（断言库状态/报告内容/审计，非 HTTP 200） |
| E2E | 无新增；既有回归照跑 |
| 性能 | 1000 行导入耗时落报告（无 k6 要求，M6 统一压测） |

## 6. 预判技术债

| TD | 条目 | 处置 |
|---|---|---|
| TD-069 | updated_at 非 ORM 更新不刷新 | 本点收口：导入进度更新统一走 ORM（批量进度写也用 ORM update） |
| TD-174 | 范围行双写对账 | 本点实测后决策：导入批末同步 + 对账断言通过 → 视为已验证，不加常驻任务 |
| TD-NEW | openpyxl 新依赖（版本锁 + pip-audit 跟踪） | 登记为常规依赖，无处置负担 |
| TD-NEW（如触发） | 1000 行导入耗时超预算 | 执行时实测，超 30s 再议分片 |

## 7. 高风险确认

- 是否高风险：**中**——批量写账号与范围行（权限类），但复用已验收的单行建档路径（ORG-01 服务函数），本点主要是编排与解析；无加密/删除/发布类操作。
- 需要用户确认：上面 4 项决策（D1-D4）+ 本简报整体。
- 确认结论：**已确认（2026-10-24）**——D1 Arq worker、D2 随机口令+重置分发（TD-126 承接）、D3 conflict=error|skip（无 overwrite）、D4 openpyxl+对象存储。

## 8. 审查对照

- 审查重点：行级原子性（无半成品）；错误报告含行号/字段/原因且不含敏感信息；任务可重入（进度不倒退）；retry 只导失败行；对象存储凭据不泄漏；权限声明齐全；TD-174 对账断言真实。
- 允许偏差：列名微调、状态机命名、错误码文本；用例数 ±3。

## 9. 实现结果

**交付物**：openpyxl 依赖（3.1.5，uv.lock 同步）；迁移 `e2a6c8d4b0f2`（import_jobs/import_row_errors + RLS）；`app/db/models/imports.py`；`app/org/import_parser.py`（CSV/XLSX/编码链/gb18030/别名表头/上限）；`app/org/import_task.py`（`run_import_job`：逐行独立事务、错误行含原始行 JSON、每 50 行进度回写、TD-174 对账、幂等仅 pending 执行）；`app/org/service.py` 引入 `OrgActor` 只读协议（Principal 与导入执行者结构化满足）；`app/api/school_imports.py` 4 端点（上传即 422 拒绝解析致命错误/错误行整数游标/retry 派生 job）；worker `functions` 注册。

**测试**：解析器单测 14（CSV/XLSX/gb18030/BOM/表头错/超限/伪装扩展名/空行行号）；BS-M2-02 三例（错误报告与 retry 全链路 / 无权限 403 / 1000 行全成功，约 8.5 分钟——bcrypt cost 12 建号为主成本，`--timeout=900` 单独放宽）。

**实现期偏差与要点**：
1. 行错误 record 曾被「预填 INTERNAL_ERROR + or 链」掩盖真实错误码（审查前自测发现，已修正为校验通过即不预填）；
2. `import_jobs` 增 `created_by/row_filter_json/retried_by/started_at/finished_at/message/conflict/skipped_rows`、`import_row_errors` 增 `raw_json`（design §4.2 已回写）；
3. 凭据分发不在本点（D2）：导入账号口令为不可猜测随机值 + must_change_password，学生/教师首登走重置流程（TD-126 承接）；
4. 教师范围行与导入无关（任教关系不导入），TD-174 对账在任务收尾执行且恒一致，无需常驻对账任务；
5. 登录名可选列缺省=工号/学号；表头支持中文别名；**文件内重复学号/工号一律报错行**
   （错误码 `FILE_DUPLICATE`，不受 conflict 策略影响——同一文件内互相矛盾的两行不可跳过）；
   库内重复按 conflict：error=报错行（建档函数原子检查给出精确字段），skip=跳过并标注。

## 10. 复审补充（2026-10-24，审查 M1-M6 落地记录）

- M1 列位错位：`_normalize_headers` 返回 (原始下标, canonical) 映射，未命名列整列丢弃、重复列名拒绝（`HEADER_DUPLICATE`）；
- M2 错误行 field：OrgError 携带 field，建档冲突/班级定位 raise 点标注字段；
- M3 文件内重复：任务内 `seen_numbers` 集合，FILE_DUPLICATE；
- M4：上传体积限制接入 BodySizeLimitMiddleware（/school/imports 11MiB）+ 读取后 MAX_UPLOAD_SIZE 双保险；
- M5：retry 入队失败补偿清除源 job retried_by（不再锁死）+ retry CAS（并发双击仅一个成功）；
- M6：stale running 看门狗 cron `fail_stale_import_jobs`（每 15 分钟一次，started_at 超 60 分钟收口 failed）；
  收口后的 failed 任务可经 retry **整文件重跑**（方案 b：不带 row_filter，已导入行以冲突错误行现形，
  覆盖收敛；崩溃点之后未处理的行不会丢失）；`_finish`/进度回写仅 running 可写（与看门狗竞态不复活终态）；
- Minor：错误行走仓储、空行占行号（与电子表格对齐）、解析下放线程、job 生命周期审计（org.import.created/retried）、_http_error 死代码删除；
- TD 登记：对象存储无生命周期（导入文件含 PII 永久保留）→ 随 M6 存储生命周期统一；性别/编码值级别名 → 按真实文件反馈再加。
