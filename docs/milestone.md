# 校园陪伴精灵 — 里程碑计划（milestone.md）

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-10-19 |
| 状态 | 待用户确认；确认前不进入 M0 |
| 上游 | `docs/spec.md`、`docs/design.md` |
| 规则 | 严格按 M0→M6 顺序逐功能点开发；一个功能点完成后汇报，不并行铺开 |

---

## 1. 里程碑原则

1. **纵向切片**：每个里程碑交付可运行、可验收的闭环，不按“先写完所有表再写所有接口”铺开。
2. **依赖优先**：后续里程碑只依赖前序里程碑已验收能力；跨里程碑依赖必须显式列出。
3. **一次一个功能点**：每个功能点走“数据→接口→前端→测试→审查→修复→复测→复审→提交”。
4. **门禁统一**：所有功能点必须通过单元/集成/业务场景测试 + 代码审查；AI 功能加评估；涉及性能的里程碑加压测。
5. **不提前实现二期/三期**：只预留扩展点，不写 UI 与业务逻辑。
6. **Spec 变更**：实现中发现矛盾，先更新 spec/design 并经确认；不得静默改范围。

---

## 2. 里程碑总览

| 里程碑 | 主题 | 目标 | 主要功能点 |
|---|---|---|---|
| M0 | 基座与兼容性 | 一键启动、依赖验证、Harness/测试骨架 | SYS-01、OBS-01、测试基础设施、MinerU 4.0.12/Langfuse 4.63.0+SDK 5.0.2/Arq 验证 |
| M1 | 租户与身份 | 学校能注册、平台能审核、三端能登录、权限隔离 | TEN-01～03、AUTH-01～05、RBAC-01～03、AUD-01 基座 |
| M2 | 组织与人员 | 校方导入人员、配权限、敏感字段合规 | ORG-01～06 |
| M3 | 知识库闭环 | 上传→审批→构建→测试→发布→分块预览（含文档人群 ACL、块编辑） | KB-01～10、APV-02、OBS-02/03（可选） |
| M4 | AI 与 C 端 | 流式问答、引用、思考摘要、反馈、多意图（含人群过滤） | AI-01～10、DASH-02 采集侧 |
| M5 | 审批/请假/消息/删除 | 校务闭环与 5 天状态机 | APV-01/03/04/05、LEAVE-01/02、MSG-01～05、DEL-01～05 |
| M6 | SaaS 运营与硬化 | 平台配置、看板、审计、备份、压测、部署 | SAAS-01～05、DASH-01/02、NFR-01～04、OBS-02 生产 |

> 说明：M3 的 KB 审批先使用通用审批引擎的最小实现（APV-01 基座），M5 再扩展请假/删除类型；避免 M3 与 M5 重复造审批。

---

## 3. M0 — 基座与兼容性

### 3.1 目标

建立可运行、可测试、可观测的工程基座；验证 Python 3.13、MinerU 4.0.12、Arq、Qdrant、Langfuse optional 等关键依赖。

### 3.2 范围

- 仓库初始化：先 `.gitignore` 再 `git init`；`.env.example`、README、AGENTS.md。
- Docker Compose core：PostgreSQL 16、Redis 7.4、MinIO、MailHog、ClamAV。
- Qdrant、MinerU 4.0.12（GPU，`mineru[torch]`）、TEI CPU（嵌入/重排）。
- FastAPI 主服务骨架、Arq Worker 骨架、健康检查、配置、结构化日志、统一错误码、request_id。
- Harness contracts/trace/audit/prompt 注册表空壳；空壳 Loop 能发 SSE 并记录 stop_reason。
- admin/campus 前端脚手架（Vite + Element Plus / Vant 4）。
- 测试基础设施：pytest、httpx、Playwright、测试数据库/Redis/MinIO/Qdrant、测试数据清理。
- Langfuse **Server 4.63.0 + Python SDK 5.0.2** optional profile（默认关闭）；本地 trace 表/JSONL；验证 OTel 接入与 v4 read API。
- 依赖兼容性验证：FastAPI/SQLAlchemy/asyncpg/greenlet/Alembic/LangGraph/Ragas/Arq。
- CPU 嵌入/重排基准；MinerU 6GB 显存 Standard/Basic/Flash 降级验证。

### 3.3 退出标准

- `docker compose up` 一键启动 core；健康检查通过。
- MinerU V1 API `/v1/health` 通过；PDF 在 6GB 下跑通 Standard（M0-E 实测：三档均可用、显存 <500MiB、未触发降级；Standard→Basic→Flash 降级链由 M3 执行器实现并验证）。
- 空壳 Loop 发 `meta/step/done` SSE，记录 stop_reason。
- 本地 trace 必发；Langfuse 关闭不影响运行，开启后映射正确（mock 端点）。
- Python 3.13 下核心依赖可导入/启动；不兼容项已报告，不静默替换。
- 前端 admin/campus 可启动；Playwright 可跑通一个空页面。
- 业务场景测试骨架跑通一条示例链路（如“健康检查→trace”）。

### 3.4 风险

| 风险 | 应对 |
|---|---|
| Docker 引擎未启动 | 启动 Docker Desktop/WSL2；验证 11GB/16CPU/4GB swap |
| Arq 不支持 3.13 | M0 验证；不兼容则提出 Taskiq 等替代，等确认 |
| MinerU 6GB OOM | Standard→Basic→Flash；记录降级率；必要时默认 Basic |
| Langfuse 组件重 | 默认 optional；生产独立主机；Server 4.63.0 固定、不跟 latest |
| 16GB 内存不足 | 资源限额；按需 profile；不启 Langfuse |

### 3.5 DoD

基座可一键启动、依赖验证报告、测试基础设施可用、空壳 Loop 可观测、文档同步、原子提交。

---

## 4. M1 — 租户与身份

### 4.1 目标

学校能注册、平台能审核开通、三端能登录、RBAC+数据范围生效、审计基座可用。

### 4.2 范围

- TEN-01 学校注册/资质提交。
- TEN-02 SaaS 审核/开通/学校编号/超管账号/邮件。
- TEN-03 生产放行开关关闭；开发跳过开关默认 false。
- AUTH-01 三端登录；AUTH-02 JWT/Refresh 轮换/撤销；AUTH-03 密码策略/历史/重置；AUTH-04 MFA 可选；AUTH-05 失败锁定；AUTH-06 会话设备管理（限量 + 退出其它设备，A31）。M1-D 拆为 D1（AUTH-03/05，含员工/平台邮件重置）与 D2（AUTH-04 MFA）；学生校管重置在 M2 交付（依赖 M1-E 权限矩阵）。
- RBAC-01 角色/权限矩阵；RBAC-02 数据范围；RBAC-03 审批职责分离基础。
- AUD-01 审计基座（哈希链、仅追加）。
- 租户中间件 + RLS + 仓储过滤（**M1-A 已落地**：三连接/双应用角色、策略按角色限定、复合外键、覆盖度门禁）。
- M1-A 遗留项锚点：TD-066/073（dev/prod 登录角色与启动自检，M1-B）、TD-067（refresh token 清理任务，M1-C）、TD-074（realm 与连接类型断言，M1-C）、TD-072（平台跨租通道，M1-E）、TD-068（审计链校验，M1-F）。

### 4.3 依赖

M0 完成；PostgreSQL/Redis/MailHog 可用；`docs/rbac-matrix.md` 初版确认。

### 4.4 退出标准

- 学校注册→SaaS 审核→学校编号/超管账号/邮件→首登改密业务场景通过。
- 三端登录；首次登录强制改密；错误/锁定/token 轮换/撤销测试通过。
- 教师/学生跨班级、跨租访问被拒绝；RLS 生效。
- 权限矩阵初版；审批自批被拒绝；超管例外默认关闭。
- 生产环境跳过审核开关无法开启；审计记录可查、哈希链校验通过。
- 会话设备管理：超限登录踢最久未用设备（被踢方下一请求 401）；设备列表/下线/退出其它设备可用且审计留痕。
- 业务场景测试覆盖注册/审核/登录/权限/跨租；代码审查通过。

### 4.5 风险

- 邮件服务未配置：开发用 MailHog；生产 SMTP 待提供。
- 单平台管理员双人确认：M1 只记录策略；M6 配置中心实现 break-glass。
- RLS 与连接池：需验证 `SET LOCAL` 事务上下文；测试覆盖。

---

## 5. M2 — 组织与人员

### 5.1 目标

校方可以维护教师/学生/班级/课程/关系，批量导入可用，敏感字段合规。

### 5.2 范围

- ORG-01 教师/学生档案（含年龄/性别/职位/任教班级/课程/班主任/辅导员）。
- ORG-02 手动录入 + CSV/XLSX 异步导入、错误行报告、重复策略、失败重试。
- ORG-03 班级/课程/教师任职/班主任/辅导员关系。
- ORG-04 身份证可选采集、AES-GCM+HMAC、前 3 后 4、权限+理由+审计。
- ORG-05 部门/科室树（`departments`）与教师部门归属；部门/归属纳入导入与错误行报告（A30，供 KB-09 人群选择器）。
- ORG-06 入学年份与学年流转（2026-10-25 用户确认）：入学年份=班级届身份（唯一键成分、不可改）、当前年级可流转刷新；学年流转（批量刷新+毕业班级归档+幂等键+dry_run）；学生账号本点不动。B 端页面前落地。
- 权限账号/多角色；可选 HTTP 人员核查适配器（默认关闭）。
  - **M2-UI3 批次登记**（简报 `docs/design-briefs/M2-UI3-custom-roles-accounts.md`；一个功能点一个 PR）：
    187a 库层不变量与自扩权守卫、187b 自定义角色创建/删除、187c 角色级数据范围编辑、187d 用户↔多角色绑定、
    187e 权限账号停用/启用（前五批已完成并过 CI）；
    **187f-A 测试与门禁补全**（只改测试与门禁，不动生产行为）——**已交付（PR #14 → 合并提交 `dbec369`）**：
    **T1** TD-221 定位并消除 `app/auth` 分支覆盖率读数的非确定性（摇摆源为 `service.py` 的 `2046 → 2047` 弧，
    已补确定性用例独占；PR 与合并后 main 两次 run 读数**同为 410/504 = 81.35%**，门槛 80% 过；**目标 82 未达标**
    ——需 414 条分支、现 410，还差 4 条；**2026-10-30 后续已达成**：PR #25 补 5 条逐弧用例后读数 417/506 = 82.41%，
    门槛由 PR #26 抬到 82，回滚＝改回 80，上面的 410/504 是 187f-A 交付时点读数）；**T2** TD-205② 补「同角色权限 PUT 与范围 PUT 用同一
    `version` 并发 → 恰好一个成功、`roles.version` 只 +1、409 的 `latest` 带赢家新值」；**T3** TD-228 其余测试
    文件的审计/会话查询按用例租户收窄 + 并发用例补任务取消兜底；**T4** TD-229 迁移目录纳入 ruff 门禁（先清零
    3 条存量）；**T5** TD-230 MFA 侧补「别租 id 不得排进对方账号锁队列」同形用例；
    **187f-B 审计字段收口**——验收编号 **T6** TD-231 `sessions_revoked` 三处口径与「布尔 vs 条数」类型分叉统一
    （动 `mfa_admin.py`/`platform_admin.py`/`auth/service.py` 与两条已交付链路的既有用例）——**已交付**（PR #16 →
    合并提交 `0ffdb2d`，2026-10-29）：
    整数键 `sessions_revoked` **全仓只写整数**，含义统一为「本次真正断开的**活跃**会话家族数」
    （未撤销且未过期，与设备列表取数一致），四个写它的动作（停用 `school.user.deactivated` /
    租户 MFA 重置 `school.user.mfa_reset` / 平台 MFA 重置 `platform.user.mfa_reset` /
    密码重置 `auth.password.reset`）口径一致；自助链路的布尔语义改用新键 `sessions_all_revoked`，
    `sessions_revoked_count` 并入同名整数键后消失；两条 MFA 重置链路各补一条「过期家族不计入」用例，
    密码重置补一条计数 + 键名用例；`design.md` §5.12 改成「口径一致 + 键名对照表」；
    187g 前端角色页与权限账号页（含 TD-198 `name` 非空白、TD-200 `resettable` 下发）。
- B 端人员管理页面。

### 5.3 依赖

M1 的身份/RBAC；租户上下文。

### 5.4 退出标准

- 手动/批量导入业务场景；重复/错误行报告正确；失败可重试。
- 身份证加密存储、默认掩码、完整查看权限+理由+审计。
- 数据范围正确：教师只看所任课班级/本人；班主任/辅导员范围正确。
- 删除权限账号后无法登录、token 失效、审计写入。
- M2-UI3 测试与门禁补全 T1–T5 逐项有「改前红 / 改后绿」证据或写明不适用理由；覆盖率类验收只认 CI 产物
  `coverage.json` 的读数，不认本地数字（简报 §16）。**T1–T5 已于 2026-10-29 随 PR #14 交付**（交付时点读数
  `app/auth` 410/504 = 81.35%）；**T1 的目标随后达成**：PR #25 补 5 条逐弧用例后，CI run `41078941427`
  与门槛 PR #26 run `41086807009` 均为 **417/506 = 82.41%**，`THRESHOLDS["app/auth"]` 由 80 抬到 82，回滚＝改回 80。
  T6（TD-231）在
  187f-B 单独收口——**已交付**（PR #16 → 合并提交 `0ffdb2d`，2026-10-29）：整数键 `sessions_revoked` 只写「断了几台活跃会话」、布尔语义改键
  `sessions_all_revoked`、`sessions_revoked_count` 消失；口径与键名的对照表以 `docs/design.md` §5.12 为准。
  T3 的两条边界
  （「精确条数」按租户收窄、「必须为空」保留全表口径；`test_campus_chat_rbac.py` 判定不适用并写明理由）
  以简报 §16.1 的表格为准。
- 部门树层级正确；教师调岗后审计含变更前后值，人群判定按新部门生效（缓存失效）。
- M2 边界评审须对 **TD-264**（本地与 CI 的解释器补丁版本是否钉版）给出处置；`app/auth` 分支覆盖率「抬到 82%」的验收只认 CI 产物（本地 417/506 = 82.41%；PR #25 CI run `41078941427` 与合并后 main run `41082150650` 读数均为 417/506 = 82.41%；`THRESHOLDS["app/auth"]` 随之抬到 82（第 2 次读数＝门槛 PR #26 backend run `41086807009`：834 passed、417/506 = 82.41%），回滚＝改回 80）。
- 业务场景测试+代码审查通过。

### 5.5 风险

- 导入文件格式/编码差异：提供模板、行级错误、编码检测。
- 身份证可选与业务核验冲突：按确认的可选策略；完整查看受控。
- 关系数据错误导致请假审批人选错：导入后校验班主任/辅导员存在。

### 5.6 M2 边界评审记录（2026-10-31）

依据 AGENTS §10.3 与本节退出标准，本轮把 **M2 期间登记（来源列含 M2）的 78 条台账条目**逐条过完；每条的状态、去向与
证据以 `docs/tech-debt.md` 为准，下表是按处置归类的索引（同一条目只出现一次；TD-258 跨两档，见对应条目注明）：

- **已完成（M2 交付，28 条）**：TD-174、TD-181、TD-183、TD-187、TD-192、TD-194、TD-197、TD-198、TD-199、TD-200、
  TD-202、TD-205、TD-223、TD-227、TD-228、TD-229、TD-230、TD-231、TD-234、TD-242、TD-243、TD-244、TD-246、
  TD-252、TD-254、TD-262、TD-263、TD-264。
- **已完成（跨期条目本轮复核，5 条，不计入上述 78 条）**：TD-069、TD-070、TD-141、TD-152、TD-172（来源早于 M2，本轮确认已交付）。
- **已完成（M2 台账条目，M3 提前收口，3 条，计入 78 条）**：TD-259、TD-260、TD-261（2026-10-31，PR #33 → 合并提交 `75c8a08`）。
- **已拒绝（3 条）**：TD-182（不加二次确认接口，dry_run 预览 + 审计为最终防线）、TD-204、TD-267。
- **已采纳 → M3（7 条 + TD-258）**：TD-195、TD-226、TD-232、TD-245、TD-251、TD-253、TD-265；
  TD-258 的「本点新增代码」部分已随 187g-C 交付，其余既有页面同形问题按 M3 清理。
- **2026-10-31 更新**：TD-259/260/261 的完成事实见上方「已完成（M2 台账条目，M3 提前收口）」；「已采纳 → M3」档剩余条目与 TD-258 的既有页面部分尚未开始，下方「已延后 → M3」17 条按原去向。
- **已延后 → M3（17 条）**：TD-184、TD-193、TD-203、TD-233、TD-235、TD-237、TD-238、TD-239、TD-240、TD-241、
  TD-247、TD-248、TD-249、TD-250、TD-255、TD-256、TD-257；其中 TD-233 的落点写死为「M3 首个动那 4 个测试文件的
  后端点」，TD-240/241/247～250 与 TD-226 的 API 契约统一同批。
- **已采纳 → M6（7 条）**：TD-175、TD-176、TD-177、TD-180、TD-189、TD-191、TD-224。
- **已延后 → M6（11 条）**：TD-179、TD-185、TD-186、TD-188、TD-196、TD-201、TD-225、TD-266、TD-268、TD-269、TD-270。
- **已延后 → M5/M6（1 条）**：TD-178。
- **跨期条目去向维持**：TD-136、TD-144（已采纳 → M3）；TD-123（已延后 → M4）；TD-129、TD-134、TD-139、TD-142、
  TD-146、TD-155、TD-157、TD-158、TD-160、TD-161、TD-166、TD-168、TD-169（M6；其中 TD-139 为已采纳，其余已延后）。

四项口径整改的落点：TD-254（Playwright 配置加载时校 `dist` 新鲜度，两个配置共用 `e2e/helpers/dist-freshness.ts`）、
TD-262（文档只抄脚本输出的 `percent` / `covered/total` / 门槛，不再手算目标缺口）、
TD-263（`.gitattributes` 加 `* text=auto eol=lf`）、TD-264（backend CI 固定打印 `python -VV` 与 coverage 版本；首次打印已由 PR #29 backend run `41139124064` 验证：`Python 3.13.18`、`Coverage.py 7.17.0`）。

验收证据核对：M2-UI3 T1–T5 的改前红/改后绿证据在 `docs/review-log.md` 187f-A 段（T2 为新增即绿，已在简报写明），T6 在 187f-B 段；
`app/auth` 82.41% 取自 CI 产物（门槛验收两次读数：PR #25 run `41078941427`、PR #26 run `41086807009`；PR #29 与合并后 main 亦为 417/506，见下）；
TD-183/192 全栈 E2E 18 例全绿（2026-10-31，PR #27 → 合并提交 `a928645`）；
TD-254 前检正反两向实测（缺 `dist` / 源码较新 / 删除源码 → 中止；重建、只改单测文件或只改生成的 d.ts → 放行）、mock E2E **96 passed（2.3m）**。

交付事实（合并后回填，2026-10-31）：PR #29 触发的三套 CI 全绿（`evals` 按路径过滤未触发）——backend run `41139124064` **834 passed / 827.59s**
（版本 `Python 3.13.18`、`Coverage.py 7.17.0`；`app/auth` 82.41%＝417/506、门槛 82 PASS）、
frontend/e2e run `41139124170`（单测 67+96、Playwright **96 passed / 2.1m**）、security run `41139124119` success；
PR 经 **rebase 合并**为 `1312523`（2026-10-30T23:29:56Z，UTC；北京时间 2026-10-31 07:29:56），分支已删除。合并后 main 推送 CI 同批全绿（同样只有 backend/frontend/security 触发）：
backend run `41144815937` **834 passed / 755.81s（12:35）**（四包读数与 PR run 逐包一致，`app/auth` 82.41%＝417/506 PASS；
版本打印 `Python 3.13.18 (main, Oct 26 2026, 14:08:20) [Clang 23.1.3]`、`Coverage.py 7.17.0 with C extension`）、
frontend run `41144815871`（单测 67+96、Playwright **96 passed / 2.1m**）、security run `41144815896` success。

M2 已于 2026-10-31 随边界评审 PR #29 合并（提交 `1312523`）关闭；此后不得再把**未处置**的 M2 条目带入 M3——已处置条目按上表去向落位，不静默遗留。

---

## 6. M3 — 知识库闭环

### 6.1 目标

校方完成“上传→单文档版本审批→构建→测试→发布→分块预览”的完整闭环，构建进度可实时查看，热更新不中断在线会话。

### 6.2 范围

- KB-01 上传/ClamAV/元数据；**不解析**。
- KB-02 单文档版本审批与构建门禁；审批中心最小实现 APV-01/02。
- KB-03 构建进度：状态机、字段、SSE `build.*`、轮询兜底、失败重试、tier/降级记录。
- KB-04 测试沙箱 + 校方测试集（CSV/XLSX/JSON）；Ragas 报告；Langfuse dataset/experiment 可选。
- KB-05 发布 alias 蓝绿/回滚/下线/删除；新会话新版本、旧会话固定；缓存失效。
- KB-06 分块预览：列表/详情/统计/搜索筛选。
- KB-07 检索测试：dense/sparse/RRF/rerank/最终引用。
- KB-08 版本切换；版本间 diff 二期。
- KB-09 文档查阅人群 ACL（版本级、fail-closed、检索强制过滤、沙箱人群视角）（K4）。
- KB-10 切块编辑与派生重建（仅子块文本、轻量确认、局部重嵌 + 向量复用）（K5）。
- MinerU 4.0.12 V1 适配器；DOCX/PPTX→PDF；TXT/MD 直接 Markdown。
- Qdrant collection/alias；BGE-M3 + jieba/BM25；CPU 重排。
- 引用元数据（doc/version/page/section/chunk）。
- **实施进度（2026-11-01）**：KB-01 后端（KB-01A：批量上传/ClamAV/元数据，不含审批与构建）已交付并合并（PR #31 → 合并提交 `3e763ca`；两侧 CI 917 passed），见 `docs/design-briefs/M3-KB-01-upload.md`；KB-01 前端（KB-01B：上传页/文档列表/详情抽屉 + mock E2E）已交付并合并（PR #35 → 合并提交 `d6de5a6`；frontend CI 单测 74+124、Playwright 117 passed，security success），见 `docs/design-briefs/M3-KB-01B-frontend.md`；KB-01C 全栈 E2E 已交付并合并（PR #37 → 合并提交 `7d1ce56`；本地全栈 22/22、mock 120/120，PR CI frontend+e2e 120 passed / 2.7m、backend 917 passed / 936.88s、security success），见 `docs/design-briefs/M3-KB-01C.md`；；KB-02 单文档版本审批与构建门禁（后端点：迁移 `d5e9a3c7b1f4` approval 四表 + kb_build_jobs + RLS、审批中心最小实现 APV-01/02、自批恒 403、构建门禁 409/`BUILD_QUEUED`、元数据免审但审计、TD-277 收口）已交付并合并（2026-11-07，PR #39 → 合并提交 `edc34f6`；PR CI backend 945 passed / 730.15s、四包覆盖率全过、security success），见 `docs/design-briefs/M3-KB-02.md`；；KB-02B 审批中心前端（`/approvals` 待办/已办/详情抽屉 + 三动作 + KB 抽屉审批入口 + APPROVED/REJECTED 文案；纯前端，`backend/` 零改动）已交付并合并（2026-11-07，PR #41 → 合并提交 `45f84d4`；PR CI 单测 80+137、Playwright 137 passed、security success），见 `docs/design-briefs/M3-KB-02B.md`；；KB-03A 构建执行管线（迁移 `e8c2a6d0f4b9`、arq build_kb_version 三段式、MinerU 降级链、chunker/embedding/Qdrant、快照/retry/看门狗；TD-288/TD-046 兑现）已交付并合并（2026-11-07，PR #43 → 合并提交 `87f9b83`；PR CI backend 979 passed/936.50s、四包覆盖率全过），见 `docs/design-briefs/M3-KB-03A.md`；；KB-03B 构建事件可观测与版本审批查询（迁移 `f2a8b4c6e0d7` kb_build_events + worker 事件写入 + SSE 端点 + TD-291 端点 + Minor 3：payload parent_id/只索子块/KB_EMBED_DIM 校验）已交付并合并（2026-11-07，PR #45；PR CI backend 986 passed/1043.44s），见 `docs/design-briefs/M3-KB-03B.md`；**KB-03 全点完成（A+B）**；KB-04A 测试沙箱（迁移 `b7d3e9f1a5c3` 三表 + 状态机扩展 + Qdrant search_points + test_parser/test_task + API 三端点；检索命中 MVP D1，TD-298/299/300 登记）已交付并合并（2026-11-08，PR #47；PR CI backend 986 passed/992.33s），见 `docs/design-briefs/M3-KB-04A.md`；；KB-04B 测试沙箱前端（api-client testSet 4 方法 + test-logic 纯函数层 + KB 抽屉测试区块 + TD-291 直显驳回理由；纯前端 `backend/` 零改动）已交付并合并（2026-11-08，PR #49；PR CI 单测 85+146、Playwright 139 passed），见 `docs/design-briefs/M3-KB-04B.md`；**KB-04 全点完成（A+B）**；KB-05 发布/热更新后端（迁移 `c9e1f3a5d7b9` kb_publish_aliases + 状态放宽、TEST_PASSED 门禁、每文档 Qdrant alias 原子切换〔design §7.4 D1-A 修订〕、回滚/下线/软删、`active_publish`；TD-292 顺延 KB-05B、TD-302/303/304 登记）已交付并合并（2026-11-10，PR #51 → 合并提交 `fbd99ac`；PR CI backend 1011 passed + 1 skipped / 19m21s、四包覆盖率全过、security success），见 `docs/design-briefs/M3-KB-05.md`；KB-05B 发布/回滚/下线/删除前端 + TD-292 UI 全栈 E2E 已交付并合并（2026-11-10，PR #53 → 合并提交 `7796199`；顺带修复 KB-04A run_kb_test 漏注册 worker 缺陷），见 `docs/design-briefs/M3-KB-05B.md`；KB-06A 分块预览后端（3 只读端点 + 四筛选 + 内联 keyset；`school.kb.view_chunks`；无新迁移）已交付并合并（2026-11-10，PR #55 → 合并提交 `d5a4a83`），见 `docs/design-briefs/M3-KB-06.md`；KB-06B 分块预览前端（抽屉分块子视图 + 四筛选 + 纯文本渲染 + mock E2E +7 + 全栈链③分块断言；顺带修 kb_embed_batch_size 16→8 与链② Escape 竞态；TD-310/311 登记）已交付并合并（2026-11-11，PR #57 → 合并提交 `6949ec3`+`780d62e`），见 `docs/design-briefs/M3-KB-06B.md`；**KB-06 全点完成（A+B）**；KB-07A 检索测试后端（单 query 诊断端点：三查询旁路逐路分数〔生产融合查询零改动〕+ 可选 CPU rerank〔RerankPort/TEIReranker，opt-in〕+ 最终引用；`school.kb.view_chunks`；无新迁移；TD-312/313/314 登记；顺带加固 TD-309 flake 提前收口——用户拍板）已交付并合并（2026-11-11，PR #59 → 合并提交 `4cca9bb`+`8dbeaff`；PR CI 重跑 backend 1083 passed + 1 skipped / 27m50s、四包覆盖率全过），见 `docs/design-briefs/M3-KB-07.md`；KB-07B 检索测试前端（api-client `retrievalTest` + 分块子视图常驻检索测试区〔分数列/最终引用/查看分块导航〕+ mock E2E +7 + e2e compose 加 tei-rerank + 链③两组检索断言〔含一次真实 rerank〕；顺带修 KB-07A 的 TEI rerank 批量上限缺陷〔分批 + 超时 300s〕——全栈首跑暴露）已交付并合并（2026-11-12，PR #61 → 合并提交 `b2776c9`..`27d8708`；PR CI backend 1085 passed + 1 skipped、Playwright 160 passed），见 `docs/design-briefs/M3-KB-07B.md`；**KB-07 全点完成（A+B）**；KB-08A 版本切换后端（TD-305 `active_version_id` 详情契约〔详情回包升级 `KbDocumentDetailResponse`，列表/上传不带；动作面零新增〕+ 业务场景「切换→查看分块不混入」验收链；TD-305 后端半收口、TD-300 口径落定；审查 0B/0M/3m 全处置）已交付并合并（2026-11-12，PR #63 → 合并提交 `9b3a669`；PR CI backend 1092 passed + 1 skipped / 26:58、四包覆盖率全过），见 `docs/design-briefs/M3-KB-08.md`；KB-08B 版本切换前端（TD-305 前端半：活跃标识服真化〔详情初始化 + 动作响应就地更新 + 409 兜底〕+ 版本表「当前活跃」tag + mock/全栈 E2E；审查 0B/0M/0m）已交付并合并（2026-11-12，PR #65 → 合并提交 `7055b43`；PR CI 单测 276 + Playwright 162 / 2.7m）——**KB-08 全点完成（A+B）**；TD-310 阶段 1 图片占位符过滤已交付并合并（2026-11-13，PR #67 → 合并提交 `ff0472c`：chunker 整行空占位符〔MinerU 实证形态 data:image base64 + 相对路径兼容〕不进切块 + 段界分支；第 0 步真实栈实证经用户随简报确认修正卡 1 口径；本地单元 16〔5 红→绿+5 守卫〕/集成 58〔stash 验证红相〕/业务 8〔真实 MinerU 含图→块零占位符 + 纯图→`CHUNK_EMPTY`〕；独立审查 0B/0M/3m 全处置；PR CI backend 1103 passed + 3 skipped / 23:08、四包覆盖率与基线逐包一致，main push CI 同读数全绿；TD-310 处置改「已完成（阶段 1）」，阶段 2 维持 M4/M3 边界评审口径；简报 `docs/design-briefs/M3-TD310-phase1-image-placeholder.md`）；KB-09A 文档查阅人群 ACL 后端基座已交付并合并（2026-11-13，PR #69〔2 commits〕→ rebase 合并 `abf142a`+`d725e86`：迁移 `a5c7e9f1b3d7`〔kb_document_audiences + versions 确认态两列〕+ API GET/PUT/confirm 三端点 + 构建/发布 fail-closed 双门禁 409 `ACL_REQUIRED`/`ACL_UNCONFIRMED` **含幂等重放路径** + 拒绝审计 + 执行期二次校验 + payload `acl_tokens` 即时同步；审查两轮 1B/3M/4m 全处置 → 复审 0B/0M/3m；TD-302 收口；简报 `docs/design-briefs/M3-KB-09.md`）；下一步 KB-09B（检索强制过滤+人群解析器+沙箱视角）/KB-09C（前端），其余 M3 功能点（KB-10）未开始；KB-09B 检索面已交付并合并（2026-11-14，PR #71 → 合并提交 `028e3a1`：人群解析器〔教师部门祖先链/任教∪班主任/课程/学生班级年级/管理员 role+self+all/空集 fail-closed，`audience.py` 纯函数 + `audience_source.py` 仓储实装〕+ `search_points`/`search_dense`/`search_sparse` 三处 `query_filter` 下沉〔min_should:1、空集不发查询〕+ 检索测试视角三参数〔as_user_id/as_audiences 二选一/ignore_acl 仅 unfiltered 码+强制审计〕+ 响应 `acl_matched_tokens`/`resolved_audience_tokens` + 批量判分视角〔迁移 `b8d4e6f0a2c4` `kb_test_runs.as_audiences`，发起后删号→空集 FAIL〕+ KB-09A 复审遗留收口〔GET 读不加锁/非法 UUID 422〕；审查 0B/0M/4m 全处置；PR CI backend 1163 passed + 3 skipped / 36:52、四包覆盖率与基线逐包一致、security 绿；**M3 退出标准「检索按身份过滤，跨人群/跨租不召回不引用」随 BS-M3-10 验收核验通过**；deps CVE 由 PR #72〔opentelemetry-api 1.45.2，CVE-2026-126360〕先行收口；简报 `docs/design-briefs/M3-KB-09.md` §3.2/§10）；KB-09C 人群 ACL 前端已交付并合并（2026-11-14，PR #74 → 合并提交 `d61ecfa`：api-client audiences 三方法 + 检索测试视角三参数类型；kb-logic 权限码纯函数 + `audienceEditError` 镜像后端 422 族；抽屉版本行「人群」入口 → 配置子视图〔确认态 tag/未确认横幅/radio 编辑对话框/全量替换明示〕+ 检索测试区视角三控件〔ignore_acl 仅 unfiltered 码〕+ 命中 token 列；`aclGeneration` 三处守卫〔审查 M1——迟响应跨版本误写〕+ 抽屉重开复位；mock audiences 有状态路由 + 检索 ACL 语义镜像；全栈链① 构建前配置改 UI 驱动〔配置→确认→构建 READY 真值化〕；审查 1M/4m 全处置 → 复审 0B/0M/1m；PR CI 单测 300 + Playwright 170 / 2.8m、security 绿）——**KB-09 全点完成（A+B+C），K4/TD-302 全链收口**；KB-10A 切块编辑与派生重建后端已交付并合并（2026-11-15，PR #76 → 合并提交 `28f8466`：迁移 `c7e9f1a3b5d7`〔versions `parent_version_id`/`derivation_type` + `kb_chunk_edits` 三态提交表 + FORCE RLS + submitted 部分唯一〕+ POST edits/GET chunk-edits/POST derive 三端点 + 派生管线〔COPY→仅改动块 EMBED→未改块源 collection dense 复制→INDEX→READY〕+ ACL 继承 + payload `is_human_edited`；审查第 1 轮 3M/8m 全处置，合并后复审第 2 轮 5M+3 邻接 → hotfix PR #77 全修 → 0B/0M/6m → 合并 `319d25d`）；KB-10B 前端已交付并合并（2026-11-17，PR #79 → 合并提交 `7d8fefd`：api-client 三方法 + 抽屉编辑对话框〔动态上限/409 回填/E6 已派生横幅〕+ 提交记录 diff 区 + 版本行派生列〔码+role 双判首例〕+ 人工修订 tag；后端小改 `chunk_hash` 透出〔卡 E2〕+ TD-320 上限 `max(500, 原块码点)`〔卡 E5-A，spec :329 回写〕；复审 0B/0M/6m 全处置；PR CI backend 1205 passed、vitest 203、mock E2E 177、全栈链①②③④ 4/4）——**KB-10 全点完成（A+B），K5 收口**；TD-294 切块参数实验已交付并合并（2026-11-18，PR #80 → 合并提交 `4c61661`：检索直评估 32 case × 12 格 + rerank 双组 + 分词器对照，70KB 合成语料；结论 recall@5 全饱和、p@3 300 组 0.733 ≈ 500 组 0.723 持平、800 组 0.372 显著劣化、rerank 最大杠杆——**参数维持默认 500/15%/1000**〔design §7.2 已回写〕；复审两轮 1B/2M/13m + 0B/1M/5m 全处置；TD-325 picomatch 审计豁免由 PR #81 → `dab8b8d` 先行收口）。**M3 全部功能点完成，边界评审通过（2026-11-18，见 §6.6）。**

### 6.6 M3 边界评审（2026-11-18）

**触发**：KB-01~10 全点完成 + TD-294 切块参数实验收口（PR #80 → `4c61661`）。

**退出标准（§6.4）逐条核对**：第 1-8/10 条有验收证据（各功能点简报 + BS/集成/全栈链 + review-log）；第 9 条条件未触发（见 §6.4 内嵌标注）。

**范围裁定**（用户拍板 G0-G5 全按推荐）：
- **G0**：TD-293 DOCX/PPTX 解析顺延 M4（当初 KB-03A D2 分批决策；上传校验已做、解析诚实失败）。
- **G1**：TD-265 改「已拒绝（条件未触发）」。
- **G2**：TD-045/055/079 顺延 M4（部署硬化批）。
- **G3**：TD-311 改已完成（KB-04A 实装）；TD-273 改已延后 → M4 部署硬化。
- **G4**：条件触发式/未兑现条目批量顺延（计数对账：M2 评审「已延后→M3」批 17 条〔触发未发生→M4/M6〕+ M2 评审「已采纳→M3」批 6 条未触发〔→M6〕+ M3 期延后 7 条〔→M6〕+ M3 期已采纳未兑现 8 条〔→M4：293/045/055/079/273/323/096/301〕+ TD-258 残留半〔→M6〕+ TD-311〔B1 勘误→M4〕= 40 行处置更新；逐条去向见 tech-debt.md §3 M3 边界评审行）。
- **G5**：TD-310 阶段 2（图片标注）定 M4（模型选型同批）。

**台账过账**：tech-debt.md §3 新增 M3 边界评审行（新增/已完成/进入 M4/顺延/拒绝五栏）；逐条处置/目标已更新（45 行）。

**顺延 M4 的重点条目**：TD-293（DOCX/PPTX）、TD-294a（faithfulness）、TD-294b（真实语料复核）、TD-298（LLM 回答 + Ragas）、TD-312/313（BM25/rerank 评估对齐）、TD-323/096（源 collection 生命周期）、TD-045/055/079/273（部署硬化）、TD-056（rerank 预算）、TD-310 阶段 2（图片标注）、TD-301（KB-04 补审）、TD-098（人群缓存）。

**结论**：M3 退出标准全部满足（第 9 条条件未触发），**M3 关闭，进入 M4**。

### 6.3 依赖

M2 的权限/数据范围；M0 的 MinerU/Qdrant/TEI；审批引擎基座。

### 6.4 退出标准

- 审批门禁全场景：未审批构建 409；拒绝不能构建；原文/结构变更重审；块级文本校正走派生+轻量确认；标签/描述/ACL 免审但审计。
- ACL fail-closed：未配置 409、未确认 409；构建 payload 携带人群 token；检索按身份过滤，跨人群/跨租不召回不引用。
- 块编辑：仅改动块重嵌（嵌入调用次数断言）、并发冲突 409、派生发布后新旧会话版本固定。
- 构建进度 SSE 实时/断线续传/轮询兜底/失败重试/tier 降级可观测。
- 分块预览列表/详情/统计/搜索/检索测试/版本切换通过；表格/代码不拆。
- 上传→审批→构建→测试→发布全链路业务场景；发布/回滚零 5xx；旧会话固定旧版本。
- 跨租 KB/审批/构建/Qdrant 全部拒绝。
- 切块参数有评估依据（300/500/800 字、10/15/20% 重叠实验结果）。**✅ 已核验（2026-11-18 边界评审）**：TD-294 检索直评估交付（32 case × 12 格 + rerank/分词器对照），结论维持 500/15%/1000（design §7.2 已回写）。
- 若 M3 触及覆盖率门槛或读数判定，先落地 **TD-265**（两份 `coverage.json` 逐弧 diff 的常驻脚本），再调门槛。**✅ 条件未触发（2026-11-18 边界评审裁定）**：M3 全程 PR 覆盖率与基线逐包一致、从未调门槛——TD-265 改「已拒绝（条件未触发）」留痕。
- 业务场景测试+代码审查通过；高风险人工复审（KB 审批/发布/分块）。

### 6.5 风险

| 风险 | 应对 |
|---|---|
| MinerU Standard 6GB OOM | 自动降 Basic/Flash；统计降级率 |
| MinerU 4.x 发布快 | 锁 4.0.12；升级跑回归 |
| 解析质量/表格 | 测试集+分块预览；必要时调整 tier |
| 构建耗时长 | 异步队列+进度+重试；不做同步解析 |
| alias/collection 命名 | 统一命名规范；清理 retired |
| 切块参数未定 | M3 实验后定，不提前写死 |

---

## 7. M4 — AI 与 C 端

### 7.1 目标

C 端可用：学校知识库问答、流式输出、引用、思考摘要、反馈、多意图、短期记忆、只读导航。

### 7.2 范围

- AI-01 快路径 + 多步 ReAct + 多意图 `intents[]`。
- AI-02 SSE 流式：`meta/step/tool_call/tool_result/reasoning_summary/delta/citation/done/error`；停止/重连/部分回答。
- AI-03 思考摘要/开关/脱敏；不展示原始 CoT；不伪造。
- AI-04 引用来源；引用校验；无证据拒答/兜底。
- AI-05 反馈采集（知情同意、脱敏、trace 关联）。
- AI-06 近 10 轮短期记忆+滚动摘要。
- AI-07 清空当前会话。
- AI-08 能力缺失兜底（切换 KB/联系管理员/反馈/入口）。
- AI-09 输入/输出护栏；注入/跨租/PII/XSS/自伤。
- AI-10 身份→人群解析与检索过滤（K4）：服务端解析部门/角色/班级/年级/课程/自身，检索按人群 token 过滤，解析失败按空集处理。
- Harness 完整模块；只读工具 `kb.search`/`kb.read_chunk`/`campus.navigate`。
- C 端 H5 聊天 UI：流式正文、折叠思考、引用抽屉、反馈、停止、重试；轻量 3D 待机。
- Ragas 评估；Langfuse 可选导出。

**M4 A12 收口（2026-11-25 用户已确认 D1–D3；2026-11-26 严格重复 judge 已实现且真实验证未通过）**：安全与评分可信度优先于 Langfuse，逐点独立简报/PR。总简报 `docs/design-briefs/M4-A12-safety-triage.md`（TRI-A1–A6），裁定表 `docs/evals/M4-A12-safety-triage-2026-11-25.md`；TD-362 的 `strict-repeat-v1` 只是失败门禁，不改阈值或历史分数。

- M4-A12-EVAL / TD-362：通用名词断言、内部术语必答、直接拒绝引用/可选预约口径；D2/D3 已确认。细化简报 `docs/design-briefs/M4-A12-EVAL.md`、稳定协议简报 `docs/design-briefs/M4-A12-JUDGE-STABILITY.md`，EVAL-A1–A8。2026-11-26 离线实现并新增 `strict-repeat-v1`（5 轮一致才稳定、最低分、红线并集、错误/冲突即失败）；受影响 141 tests、ruff、227 条完整性绿。最终临时模型 A8：31 条最多 155 次、实际 152 次，20/31 稳定，16 条合成对照 12/16；报告 SHA-256 `974410e2c577fca29f9104d8cb36b525b5c6279f778383f1cbbb44269a399fce`。最终代码复审 0B/0M/1m 并已修复，实现提交 `471f01f`，PR #106 已合并为 `799e58f`，PR 与 main push 的 evals/security CI 全绿；EVAL-A8 和 TD-362 仍不验收，key 供应商侧撤销记录保持开放。
- M4-A12-CT / TD-361：v3 语义范围路由已按用户 2026-11-27 的确认实现。高置信、显式外校取数请求由确定性证据零模型早拒；其余消息只调用一次 scope-aware `route_v3`，在检索、工具、citation 与答案生成前返回 `allow/block/mixed/restriction/identity_claim`。`block` 固定拒绝；`mixed` 仅接受原文逐字存在、可独立回答且不含外校证据/身份声称/悬空代词的本校片段；非法字段、缺 `scope`、重复 JSON 键、scope/intent 冲突、不可追溯片段与 provider 异常在外校证据存在时 fail-closed。被拦截回合只以 `partial` 保留历史展示，模型上下文只读取 `completed`；semantic router 的 provider/model/prompt hash/tokens/latency 写入 trace/span/usage，ALLOW、输入护栏、路由不可用与拒绝路径均有真实断言。认证 Principal、PG RLS、Qdrant alias 与工具证据白名单仍是独立硬边界。取消发生在路由等待期间时，先提交保守 mixed guard/span，随后传播取消，不发送可见事件也不访问数据面。首轮独立审查 0B/3M、二次复审 0B/2M/1m 已修；最终复审 0B/1M 指出引述净化成功回合可能被误存 completed，已用不可变 `quote_redacted` 标志让 user/assistant 都以 partial 保存，历史可见但不进入模型上下文。最终本地门禁：夹具修复后 harness 307 passed、三条受影响业务/集成文件 18 passed；此前真实 PG/Qdrant/SSE 47 passed / 9 skipped（302.46 秒）、租户范围数据库专项 13 passed（178.53 秒）、临时模型 87/87 且 0 fragment failures；Ruff、Mypy、Bandit、Alembic check 与 diff check 通过。最终独立复核 0B/0M PASS；代码提交 79b84ab 的 PR #109 final code-head CI 已全绿（backend 1530 passed / 19 skipped，security success），等待用户明确合并授权；A12 分母 `195/60/46/20/69`、门槛 `85/100/90/95/80`、旧分数和评估语料不变，M4 仍不可退出。
- M4-A12-SC / TD-363：敏感内容分类/拒绝与合规支持，SC-A1/SC-A2；M4-A12-PI / TD-364：注入触发/来源复现与合法审批引导，PI-A1/PI-A2；各自独立点。
- M4-A12-JUDGE / TD-365：46 条 faithfulness 有效评分完整性，9 条未评的有界诊断/恢复；不把缺失当作通过。
- M4-A12-FALLBACK / TD-366、M4-A12-MULTI / TD-367、M4-A12-REFUSAL / TD-368：分别按 AI-08、AI-01c/AI-08、AI-04/AI-08 及 A12 原类别门槛验收，逐点设计；TD-353 保留为有证据拒答的独立修复点。
- M4-A12-INPUT / TD-369：score-only 收集 ID 完整性自动门禁，独立 evals 点；发布简报 `docs/design-briefs/M4-A12-INPUT.md`，采用源 manifest 精确 ID/逐 ID 类别与重复 ID + collected full/smoke 双向集合 + 严格 JSON/字段类型 + 快照哈希。首轮 0B/2M/2m、第二轮 0B/2M/0m 与第三轮 0B/1M/2m 均已修复，当前四文件 103 passed、evals 全套 199 passed、ruff/mypy/bandit、227 条完整性绿；第四轮复审 0B/0M/1m PASS、Minor 已修；实现提交 `8d5e1c2`，PR #107 已 squash 合并为 `48d6cf0`；PR final-head evals run `45803145499` / security run `45803145507` 与 main push evals run `45803368154` / security run `45803368297` 全绿，TD-369 已验收。
  - M4-A12-EVAL / TD-370：临时供应商额度错误契约复核；非标准状态/结构/关键词错误不得静默继续消耗或误判，下次切换供应商或 full195 前完成。
- 最终产品验证：受影响类别重收、安全 60 条回归、full195 重收重评；Langfuse 仍独立简报，M4 边界评审复核全部质量/安全/性能门槛与台账。TD-294b 的真实语料批次仍为 M6/运营。

### 7.3 依赖

M3 已发布 KB；M1 登录/权限；M0 SSE/观测。

### 7.4 退出标准

**实测状态（2026-11-25，M4-EVAL / PR #103 → `9371241`）**：Ragas 跑批与报告已交付，**退出门槛未通过，M4 不关闭**。A12 五线均未达标：overall 102/195=52.31%、safety 30/60=50%（安全场景验收 Blocker，非已证 30 次泄露）、faithfulness 37/46 成功评分（均值 0.9184，另 9 条未评，未定论、不达标）、refusal 13/20=65%、classification 24/69=34.78%；93 条失败 case ID 已进入报告回归清单。完整证据见 `docs/evals/M4-ragas-2026-11-25.md`。下列阈值保持不变；Langfuse 导出仍为待新简报与用户确认的独立功能点，M4 边界评审须复核未满足的门槛与台账，不能据跑批完成宣告 M4 验收通过。TD-294b 真实租户语料复核仍指向首个真实租户语料批次（M6/运营），本批合成语料不能关闭该项。

- 快路径首答案 token P95≤3s；ReAct 首状态≤3s/首答案≤5s（压测/基准报告）。
- 流式事件顺序、增量拼接、停止、断线、部分回答测试通过。
- 思考开关/脱敏/无原始 CoT 测试通过；provider 不返回 reasoning 不伪造。
- 引用真实相关；无证据拒答；伪造引用降级。
- 多意图一次作答，逐项兜底。
- 注入/跨租/敏感内容/能力缺失用例通过。
- Ragas 达到 A12 阈值；失败样例入回归。
- 反馈落库/知情同意/脱敏/可删除；Langfuse 关闭不影响聊天。
- 人群过滤（AI-10）：财务文档对非财务用户不召回/不引用/无文档名泄漏；教师调岗后按新部门过滤；伪造人群字段被忽略。
- 业务场景测试+代码审查通过；高风险人工复审（AI 护栏/SSE）。

---

## 8. M5 — 审批/请假/消息/删除

### 8.1 目标

校务闭环：统一审批、学生请假、消息通知、账号删除/撤权 5 天状态机。

### 8.2 范围

- APV-01 通用审批引擎；APV-03 请假；APV-04 账号删除/撤权；APV-05 待办/已办/抄送/超时。
- LEAVE-01/02 学生请假提交/撤回/审批/驳回/只读预览。
- MSG-01 站内信+SMTP；MSG-02 outbox/重试/死信；MSG-03 通道隔离；MSG-04 B 端已读/C 端二期；MSG-05 反馈答复。
- DEL-01 用户自助删除；DEL-02 管理员撤权；DEL-03 5 天只读+禁写；DEL-04 冻结/申诉；DEL-05 删除/匿名化/证明。
- B 端审批中心/消息中心页面；C 端请假/消息页面。

### 8.3 依赖

M2 组织关系（班主任/辅导员）；M1 RBAC；M4 消息触发（可选）。

### 8.4 退出标准

- 请假全状态机；草稿/撤回/通过只读/拒绝理由；非审批人 403。
- 审批不能自批；超管例外默认关闭；驳回理由必填；超时提醒/升级。
- 消息 outbox 重试 3 次/死信/重发；B 端已读；通道隔离；反馈答复 C 端可见。
- 账号删除/撤权双入口；5 天只读+禁写；冻结暂停倒计时；申诉；删除证明；唯一超管 SaaS 复核。
- 审计覆盖审批/消息/删除；业务场景测试+代码审查通过；高风险人工复审（删除状态机）。

---

## 9. M6 — SaaS 运营与硬化

### 9.1 目标

平台可运营、可审计、可备份、可压测、可部署；校方看板/反馈可用。

### 9.2 范围

- SAAS-01 模型/检索/限流配置；密钥加密/掩码/轮换。
- SAAS-02 配置变更审批/双人确认/break-glass。
- SAAS-03 租户审核记录查询/导出；SAAS-04 平台消息；SAAS-05 租户审核开关。
- DASH-01 校方基础看板；DASH-02 反馈查看/标记/答复/简单统计。
- NFR-01 性能压测（k6）；NFR-02 安全自查；NFR-03 备份/恢复；NFR-04 隔离测试全量。
- AUD-01 审计查询/导出/WORM。
- OBS-02 Langfuse **Server 4.63.0 + Python SDK 5.0.2** 生产独立主机部署（若提供观测主机）；监控/日志。
- 部署文档、Nginx/HTTPS、资源限额、健康检查。

### 9.3 依赖

M0–M5 全部通过；生产服务器/域名/观测主机信息。

### 9.4 退出标准

- 配置变更审批/双人/break-glass 留痕；密钥不回显。
- 看板统计与 DB 一致（缓存标注）；反馈按权限/数据范围可见/标记/答复/审计。
- 审计查询/导出；WORM 校验；备份恢复演练 RPO/RTO 达标。
- k6 报告：非 AI 200 并发、AI 50 流、P95/P99/错误率/资源；发布/回滚零 5xx。
- 隔离测试全量通过；安全清单逐条结论。
- Langfuse 独立部署（如提供主机）：4.63.0 + SDK 5.0.2；OTel 映射、访问控制、脱敏、场景开关、故障隔离、v4 read API 验证。
- 部署演练通过；文档同步；代码审查通过。

---

## 10. MVP 之后（二期/三期）映射

| 阶段 | 功能 |
|---|---|
| 二期 | 个人知识库、个人课表、长期记忆、历史会话管理、任务中心/积分、基础皮肤、校园墙、心理告警人工复核、知识库定制化、技能中心、皮肤中心、公告审批、账号删除完整流程、C 端已读/举报/拉黑、版本间分块 diff |
| 三期 | MCP/SKILLS 市场、学生/教师创作中心、工具沙箱、盲盒/概率公示、词频热度榜、高级知识库定制化、租户配额成本运营计费 |

**阶段冲突待确认**：词频榜、盲盒、账号删除完整流程。公告已确认推迟。

---

## 11. 依赖与关键路径

```text
M0 基座
 └─ M1 租户/身份
     ├─ M2 组织/人员
     │   ├─ M3 知识库
     │   │   └─ M4 AI/C端
     │   └─ M5 审批/请假/消息/删除
     └─ M6 SaaS/看板/硬化（依赖 M1–M5）
```

- M3 的审批引擎基座与 M5 共用；M3 先实现 KB 类型，M5 扩展。
- M4 依赖 M3 已发布 KB；无 KB 时只能做空壳/导航。
- M5 请假依赖 M2 班主任/辅导员关系。
- M6 依赖全部功能点 DoD。

---

## 12. 里程碑 DoD

- 所有功能点通过单元/集成/业务场景/E2E（如涉及）。
- AI 功能通过 Ragas 阈值；涉及性能的里程碑有 k6 报告。
- 代码审查日志完整；无 Blocker/Major 遗留。
- 文档同步（spec/design/test-plan/rbac/state-machines）。
- 所有原子提交完成；分支状态干净。
- 里程碑汇报：范围完成度、风险、未决项、下一步。
- **技术债台账评审**：所有新增 TD 条目已登记并有处置；`已采纳` 条目进入下一里程碑或 spec/design；未处置条目不得进入下一里程碑。
- **实现前设计简报**：每个功能点/里程碑单元的 `docs/design-briefs/<ID>.md` 已完成；高风险项已经用户确认；代码与简报一致或偏离有 TD 处置。

---

## 13. 变更控制

- 新增/删除功能点：先改 spec/design/milestone，经用户确认后实施。
- 阶段冲突：在 spec 标“待确认”，不纳入当前里程碑。
- 技术替代（如 Arq→Taskiq、MinerU tier 调整）：先报告 + 兼容性/评估数据，经确认后改文档与版本锁。
- 任何“临时跳过测试/审查”的请求：拒绝；必须走门禁。
