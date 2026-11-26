# M4-A12-INPUT — TD-369 评估收集 ID 完整性门禁设计简报

| 项 | 内容 |
|---|---|
| 日期 / ID | 2026-11-26 / M4-A12-INPUT / TD-369 |
| 当前阶段 | 精确集合门禁已实现；首轮 0B/2M/2m、第二轮 0B/2M/0m 与第三轮 0B/1M/2m 均已修复，当前四文件 103 passed、evals 全套 199 passed / ruff/mypy/bandit / 227 条完整性绿；第四轮复审 0B/0M/1m PASS、Minor 已修；实现提交 `8d5e1c2`，PR #107；evals run `45802814061` 与 security run `45802814089` 均成功 |
| 授权依据 | 用户确认按既定 TD-369 方案继续，并明确临时额度风险不再阻塞后续开发；本点仍按独立分支/PR、测试、独立审查与 CI 门禁交付 |
| 上游 | requirements A12；spec AI-08/AI-09；milestone §7.2；test-plan §6.2；`docs/design-briefs/M4-A12-EVAL.md`；`docs/tech-debt.md` TD-369 |
| Git 基线 | `feature/td-369-a12-input` 从 TD-362 合并后的 `799e58f` 分出；当前改动未 commit/push |
| 本点非目标 | 不改 A12 五线阈值/分母、不修 judge 语义稳定性、不重评产品答案、不实现 backend 或 Langfuse 功能 |

## 1. 目标与验收

当前 `qa_runner._run` 的 score-only 分支先用 `case.id in turns` 过滤用例，再进入聚合。缺失 ID 会在聚合前从分母消失；`--full` 仅表示选了当前文件集，不能证明收集恰好覆盖该集合。TD-369 要让不完整或含未知内容的收集在评分前失败，保留原目标分母，杜绝缩集合后假绿。

| 编号 | 完成证据 |
|---|---|
| INPUT-A1 | full 收集 ID 必须与当前 `load_cases` 的完整 ID 集合精确相等；当前应为 195 条但不硬编码为唯一事实 |
| INPUT-A2 | smoke 收集 ID 必须与 `_smoke_subset(load_cases)` 的 ID 集合精确相等；缺一条、多一条均失败 |
| INPUT-A3 | JSON 根必须是非空对象；顶层或嵌套对象出现重复键必须失败，不能依赖 `json.loads` 的静默覆盖 |
| INPUT-A4 | 缺失、额外/未知 ID 必须失败，不进入 judge/Ragas/HTTP；不生成可能被误认作正式结果的 JSON/Markdown 报告 |
| INPUT-A5 | 完整 full 与完整 smoke 正常放行；既有五分母、门槛、报告字段和业务结论不变 |
| INPUT-A6 | 收集后的同进程路径也执行同一集合门禁；collect-only 不保存内部集合不匹配的错误收集 |
| INPUT-A7 | 错误诊断有界且稳定：只给异常类别、缺失已知 ID 和额外 ID 数量/受限样本；不回显回答、judge 内容、凭证、完整路径或任意大段输入 |
| INPUT-A8 | 受影响单测、模拟业务链、ruff、用例完整性与独立审查通过；远端 CI 全绿后才能交付 PR |

## 2. 候选方案

| 方案 | 做法 | 优点 | 缺点 / 风险 | 结论 |
|---|---|---|---|---|
| A：精确集合拒绝 | full/smoke 都用目标 QA ID 集合做双向差集；缺失或额外直接失败，读取在 judge/报告前完成 | 分母不可缩；错误显式；full/smoke 共用一条规则；不污染历史产物 | 旧的不完整收集不能再“部分评分”，需要补采或修复输入 | **选择** |
| B：不完整输入进入报告 | 缺项仍评分，在报告标 `incomplete` 并保持完整分母 | 可保留部分诊断；分母声明完整 | runner 仍会在缺少 outcome 的情况下聚合；报告 schema/消费方需识别未定论，容易误用为正式结果；实现面和测试面更大 | 不选；诊断工具可另立点 |
| C：维持交集并缩分母 | 保留当前过滤，仅把实际分母写进报告 | 改动最小 | 正是 TD-369 要修的假绿入口；`--full` 语义不再可信 | 明确拒绝 |
| D：只检查数量 | 仅要求 full=195、smoke=18 | 实现简单 | 用错误 ID 替换缺失 ID 仍可通过；无法保证覆盖目标集合 | 不选 |

选择 A。精确集合是评估结果可信的前置条件；不完整输入要么修复后重跑，要么使用独立的诊断工具，不能混入正式门禁。

## 3. 技术设计

### 3.1 输入校验顺序

1. 加载当前 QA cases，并根据 `--full` 选择完整集合或固定 smoke 子集。
2. 读取 collected JSON，先用严格 JSON 解析器拒绝重复键，再验证根为对象并加载 `TurnResult`。
3. 对 expected IDs 与 collected keys 做双向集合差集；任一方向非空即失败。
4. score-only 路径在校验通过后才读取评分配置、创建 judge/Ragas/HTTP 客户端并评分。
5. 新收集路径在 `collect_all` 后、`save_collected` 与任何评分前执行同一校验。
6. 校验失败返回退出码 1，向 stderr 输出稳定摘要；不调用 `write_reports`。

### 3.2 错误诊断

- JSON 语法错误：只显示行、列等解析位置，不回显原文。
- 重复键：固定为“collected JSON 含重复对象键”，不打印键名；防止任意输入污染日志。
- 根类型：固定为“根必须是对象”。
- 集合不匹配：列出仓库内已知的缺失 ID；额外 ID 只显示数量与最多 5 个经 `repr` 限制的样本，避免恶意无限日志。
- 单条结构错误：显示该 ID 的受限表示和异常类别，不回显 `TurnResult` 内容。

### 3.3 full/smoke 口径

- full 期望集合来自 `load_cases(...)` 当前完整结果，当前为 195 个 QA ID；`chunk_grid` 继续由既有脚本单独校验，不进入本跑批。
- smoke 期望集合来自同一 cases 上的 `_smoke_subset`，当前为每类前 2 条，共 18 个 ID。
- 不添加 `--allow-partial` 或自动忽略额外 ID 的开关。正式 CLI 只有完整放行；局部评分需求另立诊断命令。

## 4. 实现顺序

1. 新增本简报。
2. 先写失败测试：严格 JSON、根类型、full 缺失/额外、smoke 缺失/额外、完整放行、失败路径 zero judge/zero report。
3. 最小实现严格加载与 `_validate_case_id_set`，接入 score-only 和新收集路径。
4. 补错误诊断边界与文档，运行受影响测试、ruff、完整性检查。
5. 独立代码审查；修复 Blocker/Major 后复跑并复审。
6. 提交、推送、开 PR；等待远端 evals/security CI。

## 5. 测试策略

- 单元：重复键检测、非对象根、集合双向差集、摘要长度。
- 集成：伪造 195 条完整收集正常进入评分；删除一个 ID 和增加一个未知 ID 均在 judge 创建前失败。
- 集成：smoke 精确 18 条放行；替换/缺少一条失败。
- 业务链：非法输入不得调用轮换 judge、不得写报告；合法 full 保持 195/60/46/20/69 分母。
- 静态：evals ruff、227 条源用例完整性检查、`git diff --check`。

## 6. 预判技术债与处置

| 改进项 | 处置 |
|---|---|
| 报告 schema 增加 `input_integrity` 审计字段 | 本轮拒绝非法输入且正式报告只由完整集合生成，无需新增字段；若以后需要保留失败审计文件，另立诊断报告 TD |
| CI 增加真实运行到临时文件的非法输入命令 | 单元/集成已覆盖核心行为；为 CI 增加进程级用例会增加 fixture 维护，暂不采纳，PR 采用直接函数集成测试 |
| 支持校方定制 QA 集 | 使用当前 cases 基线；租户定制集属于 M6/真实语料范围，不在本点扩大权限或数据边界 |

## 7. 决策记录

- D1：正式 full/smoke 输入必须精确相等，缺失、额外和重复均失败。
- D2：所有校验在 judge、Ragas、HTTP 和报告写入前完成；失败路径零外联、零报告。
- D3：不提供部分评分降级开关，避免调用方把诊断结果当作正式 A12 结果。
- D4：源案例必须精确覆盖仓库固定 manifest；collected 目标仍从 `load_cases` 选择，但选择前后均受 manifest ID 与类别分布约束，195/18 只作为测试和运行证据。
- D5：collected 字段在构造 `TurnResult` 前做运行时类型校验；JSON 任意层级重复键直接失败。
## 8. 实现、首轮审查与修复

### 8.1 红相与最小实现

- 首批 10 个 TD-369 测试先失败，覆盖集合函数缺失、重复键被静默覆盖、非对象根、非法 score-only 仍继续及 collect-only 保存坏集合。
- 最小实现新增严格 JSON 解析、双向 ID 差集、full/smoke 精确门禁与 judge 前失败。
- 首轮独立审查发现 2 个 Major：只比较当前 `load_cases` 仍可因整类文件缺失把 195 缩成 175；`TurnResult` 只校验字段名，`answer=123` 等错误类型仍可进入 judge。

### 8.2 复审修复

- 第二轮复审指出仅比较类别汇总仍可被等量互换绕过。源案例先与 `fixtures/m4_eval_v1_manifest.json` 的 195 个固定 ID 做双向比较，再逐 ID 校验 `category == id.rsplit("-", 1)[0]`，并拒绝重复 ID；整类删除、类别重分配或平衡互换均失败。
- collected 进一步校验 `answer: str`、`citations: list[dict]`、`done/error/meta: dict|null`、`transport_error: str|null`；`first_delta_ms` 为非负整数或 null，`latency_ms` 必须存在且为非负整数。结构错误在账户读取、HTTP、judge、Ragas、报告前失败。
- `load_collected_snapshot` 在解析同一次字节时返回 SHA-256；新收集由 `save_collected` 返回写入哈希；`build_run_meta` 使用该快照值，不再于报告阶段重读 collected。
- 第三轮复审确认原两个 Major 已关闭，但指出 collect-only 旧测试在 manifest 校验阶段提前退出，未触达采集后保存门禁；另发现非字符串 ID 会抛未封装 TypeError，CLI 丢失有界 source 诊断。现用真实 195 条源集和 194 条收集结果执行完整采集后校验，禁止 save/report；显式拒绝非字符串 ID，并把 CLI 诊断压成单行、完整摘要总长上限 500 字符。
- 新补整类丢失、类别重分配、平衡互换、重复 ID、非字符串 ID、source missing/extra/duplicate CLI 摘要、`latency_ms` 的 null/缺失/非法值、错误字段、非法输入零账户/HTTP/judge/score/report、有效 194/195 collect-only 不保存和哈希窗口回归。
- 第四轮复审判定 0 Blocker / 0 Major / 1 Minor，PASS；上一轮 Major 与两个 Minor 均已关闭。剩余 Minor 为设计简报格式和截断后缀未计入 500 上限，现已把异常正文截取改为“正文 + 后缀总长 ≤ 500”，并补边界测试。

### 8.3 当前证据

- 受影响四文件 `test_qa_runner` / `test_qa_collect` / `test_qa_report` / `test_qa_cases`：**103 passed**；evals 全套 **199 passed**。
- evals `ruff check .` 全绿；改动生产文件 `qa_runner.py` 经隔离 mypy 冷跑无错误、bandit 无发现；`check_eval_cases.py` 核对 **227 条**（195 QA + 32 chunk_grid）通过；`git diff --check` 通过。
- 第四轮独立复审已 PASS；实现提交 `8d5e1c2`，PR #107 的 evals run `45802814061`（199 passed、227 条完整性）与 security run `45802814089` 均成功。最终文档状态提交与合并完成后 TD-369 才可交付。