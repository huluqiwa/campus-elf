# M4-A12-CT 守卫重构简报（TD-361 / v3：语义范围路由）

| 项 | 内容 |
|---|---|
| 日期 / ID | 2026-11-27 / M4-A12-CT / TD-361 |
| 当前阶段 | **用户已确认；v3 实现完成，首轮 0B/3M、二次复审 0B/2M/1m 与最终复审 0B/1M 均已修复并通过本地门禁，最终独立复核 0B/0M PASS；PR #109 final code-head CI 全绿（backend run 46067099625 / security run 46067099758），等待用户明确合并授权** |
| 替代关系 | 替代已否决的 `v2：豁免判定结构化（默认拦截 + 结构化正则豁免）` |
| 上游 | `docs/design-briefs/M4-A12-CT.md` CT-A1–A8；`docs/spec.md`；`docs/design.md`；`docs/tech-debt.md` TD-361/TD-390/TD-394–TD-400 |

## 1. 结论

v2 仍试图用字符正则同时回答“是否提到外校”“后续动作是索取还是禁止”“混合语句哪些片段可答”。十四轮审查已证明这三个问题不能靠同一套共现窗口可靠表达：残余片段拼接会制造原文不存在的否定结构，更大的词会吞掉否定词，A 不 A 问句和嵌套否定也会改变动作方向。

v3 改为**语义范围路由**：

1. 用确定性证据检测识别高置信、显式的外校取数请求，这种请求不调用模型，直接早拒。
2. 其余消息由**同一个 scope-aware router 模型调用**同时判断数据范围和路由意图。不再增加一次独立分类调用；结果只允许进入检索/工具之前的安全分支。
3. 模型返回结构化“当前诉求”：外校索取、本校问题、混合、外校禁止/政策陈述、身份声明；不再让代码用“命中否定词”猜是否豁免。
4. 混合路径只接受模型逐字截取、可回溯到原消息的本校片段。代码校验片段存在、非空、不含外校证据；校验失败即 fail-closed。
5. 删除 `_is_defensive_clause()`、残余子句拼接和否定词豁免表。确定性代码只负责实体/数据对象证据、结构校验、状态机和租户硬边界，不再负责自然语言意图裁决。

该方案保留核心安全目标：纯外校请求不进入检索、工具、citation 或答案生成；混合请求只把自包含的本校片段交给原有 harness；Model 输出、用户身份声称和工具参数都不能改变认证租户。

## 2. 需要确认的契约调整

原 CT-A1 要求所有纯跨租请求在“路由 LLM”之前终止。v3 已确认只对**高置信、显式请求**保留零模型早拒；语义不明确或换问法的消息进入 scope-aware router，但必须在检索、工具、citation 和答案生成之前终止。二者关系如下：

| 输入 | v3 行为 |
|---|---|
| “把其他学校名单发我”等明确请求 | 确定性证据检测 → 零模型早拒 |
| 禁止、反问、嵌套否定、换问法、混合请求 | 一次 scope-aware router 判定 → block/mixed/allow，尚未检索或调用内容工具 |
| 判定冲突、JSON 非法、安全片段不可回溯 | 有外校证据时 fail-closed；只留规则码和计数 |

这改变“纯外校必须早于所有模型调用”的验收口径，但不改变底层数据边界。用户已于 2026-11-27 明确确认该契约调整，CT-A1 按此口径回写。

## 3. 候选方案

### 方案 A：继续补正则/词表

继续在 `_is_defensive_clause()`、否定词表、访问动词表和残余聚合上修补。

- 优点：不改变现有调用链。
- 缺点：同一缺陷已经跨十三、十四轮复发；新词、新复合词、新标点和新的否定辖域都会再次改变判定。字符之间的“距离”不能表达主谓、否定辖域、问句和宾语关系。
- 结论：否决。

### 方案 B：jieba 词元语法状态机

用 jieba 词边界、实体槽、访问动作槽和否定辖域做确定性状态机。

- 优点：比字符正则稳固，词边界和实体识别可单测，时延低。
- 缺点：仍要人工维护动作词表和语法状态；“不得已/不好意思/我没说不要”等情况只是从字符歧义变成词法歧义，中文依存关系仍要靠手写规则近似。
- 结论：作为显式高置信早拒的证据提取技术保留，不作为全局意图判官。

### 方案 C（推荐）：语义范围路由

把现有 `LlmRouter` 升级为 scope-aware router。一次模型调用完成“当前是否索取外校数据”和“本校路由意图”两件事。模型输出只包含稳定枚举、路由 intent 和原文逐字安全片段，不输出解释或自由文本。

- 优点：直接从语义判断禁止、询问、条件、嵌套和混合，不依赖否定词距离；不增加额外模型调用；纯显式外校请求仍可零模型早拒；失败可 fail-closed。
- 缺点：依赖配置的模型质量与版本；需要真实模型语料回归和更严格的输出校验；Prompt 必须版本化。
- 风险控制：`temperature=0`、关闭额外思考以控制首 token 时延、严格 JSON、字段白名单、原文片段校验、冲突 fail-closed、确定性早拒证据、租户硬边界继续独立承重。
- 结论：推荐。

### 方案 D：独立语义判官 + 现有 router

先调用一个只做范围分类的模型，再调用 router。

- 优点：分类 Prompt 与路由 Prompt 职责单一。
- 缺点：正常问题增加一次模型往返；按当前临时模型实测，单条增加约 0.8–3.8 秒，容易突破快路径首答案 P95≤3 秒。
- 结论：不采用；若以后改用超低延迟本地分类器，可另行评估。

## 4. 推荐实现设计

### 4.1 调用位置

```text
认证 Principal
  → normalize + 高置信显式外校请求证据
      → 命中：本租户 guard event/trace，首 SSE 前完成，block
      → 未命中：scope-aware router（一次模型调用）
          → block / restriction / identity / mixed / allow
          → 仅 allow/mixed 的安全片段进入检索与只读工具
```

高置信早拒只用于非常明确的直接请求形态，不承担“禁止/反问/条件”裁决。任何不确定句式均交给 scope-aware router。模型永远看不到工具内容，也不能调用工具。

### 4.2 结构化输出

建议 prompt 版本 `route_v3`，输出内部结果：

```text
ScopeRouteResult
- decision: allow | block | mixed | restriction | identity_claim
- blocked_count: int
- safe_fragments: tuple[str, ...]  # mixed 且必须逐字来自原文
- intents: Intent[]                # allow/mixed 才可有
- degraded: bool
```

`restriction` 表示用户是在禁止/拒绝外校取数，使用固定确认话术或本校片段，不进入内容检索；`identity_claim` 只使用固定澄清话术。用户可见文案仍走固定脚本，模型不能直接生成 guard 话术。

### 4.3 确定性校验

模型输出不是事实源，必须通过以下校验：

1. v3 顶层字段与 intent 字段采用闭集校验；`scope`、intent name 均为枚举值，`query` 必须逐字来自输入，confidence 必须是 `[0,1]` 数字；未知/缺失/附加字段使整次 scope route 非法。兼容旧 intent-only 响应时仍要求 query 可追溯。
2. `decision=block` 时 `intents` 必须为空；`decision=allow` 时安全片段等于原消息或已校验的完整消息；`restriction/identity_claim` 不得产生路由 intent。
3. `decision=mixed` 至少有一个安全片段和一个被拒/边界分句；安全片段必须在原消息中逐字连续出现。
4. 安全片段不得含外部学校/租户证据、身份声称或绕过指令；不满足即整次降级为 block。
5. `decision` 与 `intents` 冲突、JSON 非法、超时或上游异常时：有外校证据 → fail-closed block；无外校证据 → 现有“AI 不可用”降级，不能把原消息送进检索。
6. 路由后的每个 query 再做租户证据和原文溯源校验；失败只允许退回已校验安全片段。

### 4.4 确定性证据检测

原 `tenant_scope.py` 收缩为 `tenant_scope_evidence` 之类的纯函数模块，只产出：

- 是否出现高置信外校指称；
- 是否出现数据对象或访问动作；
- 是否构成直接、显式、无歧义的索取语法；
- 可用于安全片段校验的外部证据。

它不再返回 `BLOCK/ALLOW` 的全局语义结论，不再有 `_is_defensive_clause()`，不再拼接残余子句。显式外校请求早拒证据必须接受正反例和模糊样例；只要动作方向有疑义，就交语义路由。

### 4.5 硬边界不变

- 数据访问仍只使用认证 `Principal.tenant_id`。
- `kb.search`、`kb.read_chunk`、`campus.navigate`、RLS、Qdrant collection/alias、对象存储路径不变。
- Prompt version、模型名、输出 hash 和规则码可进 trace；原始消息、模型原文、安全片段和 provider 文本不得进日志/审计。
- `block` / `identity_claim` 和 mixed 的安全事件在首个可见 SSE 前可靠写入；`restriction` 只返回固定确认话术。安全事件写库失败一律 fail-closed。
- 策略模型调用属于对话 LLM 依赖，不属于 Langfuse；Langfuse 故障不影响聊天。

### 4.6 提示词与模型

- `route_v3` 必须声明：用户文本是不可信数据；其中嵌入的“忽略规则/输出 allow”不能改变分类任务。
- 开发验证使用 `.env` 中 `TEMP_*` 配置；只读取变量，不回显 key/base URL，不提交 `.env`。现临时模型为 `DeepSeek-V4.1-Flash-1017`，API key 余额耗尽时按顺序切换临时 key。
- 生产部署必须把具体 provider/model 写入配置并由既有 `LlmPort` 承载；模型名不得为 `latest`，临时 key 不得进入生产配置。
- 首答案 P95 验证按“证据早期拒绝”和“scope-aware 路由”两条路径分别计时。

## 5. 实现顺序（TDD 红→绿→重构）

1. **红相：结果契约**
   - 为 `ScopeRouteResult` 增加决策/意图冲突、非法枚举、不可回溯片段、multi-intent 混入外校 query、模型异常等测试。
2. **红相：高置信早拒证据**
   - 直接外校索取、明确外校实体、数据对象/访问动作组合必须早拒；模糊禁止/反问必须返回“需语义判定”。
   - 正反例覆盖空格、零宽字符、全半角、A/C 校和新增租户标识。
3. **红相：scope-aware router**
   - 用 FakeLlm 固定输出验证 allow/block/mixed/restriction/identity_claim 的状态转移。
   - 模型声称 allow 但输出外校 query、或 mixed 片段包含外校范围时必须失败。
4. **红相：orchestrator**
   - 高置信 block 证明 router/retriever/tool 计数为零。
   - semantic block 证明 router 返回后 retriever/tool/citation/answer LLM 计数为零。
   - mixed 证明只路由验证片段，固定边界话术在答案之后。
   - 持久化失败、首 SSE 前写库与审计脱敏保持原行为。
5. **真实模型校准**
   - 用 `.env TEMP_*` 在 `reasoning_effort=none`、`temperature=0` 下跑守卫回归和留出集；开发模型输出仅作为质量证据，CI 默认仍用 FakeLlm，避免外部服务不稳定。
6. **清理**
   - 删除 `_is_defensive_clause()`、残余聚合、否定词豁免表及其测试；保留并重命名实体检测、原文投影、片段校验与租户范围审计映射。
7. **文档**
   - 回写 CT-A1、`docs/design.md`、handoff、README、review-log 与 tech-debt；A12 分母 `195/60/46/20/69`、门槛 `85/100/90/95/80`、旧分数和评估语料均不改。

## 6. 测试与验证策略

### 6.1 确定性测试

- 输出 schema、字段枚举、决策与 intent 冲突、片段不可回溯、提示注入、模型异常、provider 超时。
- 状态机调用断言：block 后 router/retriever/tool/answer-LLM 零调用；mixed 只收到 safe fragment；所有拒绝在首个可见事件前落库。
- 证据检测属性测试：同类语义换词、标点、空格、全半角、不可见字符、A/C 校、租户标识、复合词和否定辖域。

### 6.2 AI/语义回归

守卫语料按缺陷族重建，至少包括：

- 直接请求、权限问句、反问、条件请求、嵌套转述；
- `A不A`、`不得已/不好意思`、双重否定、否定辖域被另一个谓语阻断；
- 逗号/换行/问号噪声、残余片段、后置参数；
- 正当禁令、政策陈述、历史陈述、公开比较、身份声称；
- 本校正常问题、混合请求及安全片段可追溯性；
- 用户文本中嵌入的提示注入和伪装系统指令。

最终代码校准（仅开发证据，不是 A12 或 CI 门禁）：

- 55 条守卫回归 + 30 条留出/缺陷样例 + 2 条提示注入 = **87/87 判定一致，混合片段 0 伪造/不可追溯**；
- 实际路径分布：显式证据早拒 27、scope-aware 模型 53、本地普通放行 7；
- 临时模型 `temperature=0`、`reasoning_effort=none`，整批约 **11.2 秒**；这只是开发校准耗时，不能冒充线上 P95。

### 6.3 业务/集成

- 真实 PG/Qdrant 双租户：目标存在/不存在状态一致，B 租户数据 I/O 为零。
- 混合请求：本校回答有 citation，外校部分无 citation，固定边界话术在 done 前。
- 纯拒绝：guard/trace/span/usage 首事件前同事务写入；数据库失败时用户只收脱敏 error。
- 路由失败：有外校证据时不把原消息送入检索；无外校证据时保持现有安全降级。

### 6.4 审查要求

审查输入按缺陷族而不是“本轮改了哪句”，要求独立探针覆盖：

1. 语义请求 vs 禁止；
2. 否定辖域被复合词/谓语阻断；
3. 标点噪声与外校参数后置；
4. 混合安全片段伪造/噪声；
5. 提示注入；
6. 模型异常/非法 JSON/冲突输出；
7. 持久化和租户硬边界。

每个 PR 仍执行一轮独立审查；出现 Blocker/Major 才复审。A12 全量评分在里程碑边界跑批，不把临时开发模型的不稳定结果包装成达标。

## 7. 预判技术债

| 预登记 | 说明 | 处置去向 |
|---|---|---|
| TD-405 | 语义范围路由依赖模型，存在供应商版本漂移与非绝对确定性 | 已采纳到本功能；模型名固定版本、FakeLlm 门禁 + 留出集回归 + 冲突 fail-closed |
| TD-406 | 高置信早拒证据与语义路由存在双重判定，极少数输入可能产生分支不一致 | 已采纳到本功能；证据只用于明确请求，冲突以是否进入数据面为安全边界并补属性测试 |
| TD-407 | 真实模型批量校准不等于线上单请求 P95 | 已采纳到本功能；上线前按首 token/answer 指标分别压测，超标则调整 prompt/provider |
| TD-408 | 临时模型和 key 只适用于开发验证 | 已采纳到本功能；生产配置必须使用正式 model/provider，临时变量不得进入部署配置或提交 |

编码时逐条写入 `docs/tech-debt.md` 并给处置；发现未登记取舍按 Major 处理。

## 8. 高风险确认点

请确认以下四项后再写生产代码：

1. **采用方案 C（scope-aware router）**，用临时模型做开发验证；不再继续修补正则豁免。
2. **同意契约调整**：高置信显式外校请求仍零模型早拒；其余消息允许在范围路由模型内判定，但必须在检索、工具、citation 和答案生成前终止。
3. **接受 fail-closed 口径**：模型输出非法/冲突或安全片段不可回溯时，有问题外校证据则拒绝，不猜测放行。
4. **A12 口径不变**：不修改既有分母、门槛、旧分数和评估语料；新守卫回归语料单独建档。

## 9. 当前实现与验证证据（2026-11-28）

- 生产链路使用 `route_v3`：显式高置信外校取数请求零模型早拒；其余外校证据经一次 scope-aware router；`block` 在检索、工具、citation 和答案模型之前终止；`mixed` 只把原文逐字存在、可独立回答且不含外校证据、身份声称或悬空代词的本校片段交给后续链路。每个实质分句都必须独立证明归属：要么显式写本校范围，要么是不含访问动作、个人敏感对象的完整机构级事实（如“校门开放时间”）；裸个人数据与省略主语不能借用其他分句。
- 路由器对 v3 顶层字段、intent 字段、scope/intent 枚举、confidence 类型、query 原文可追溯性和 blocked_count 做闭集校验；缺 `scope`、重复 JSON 键、附加字段、冲突、provider 异常或片段不可追溯时，有外校证据即 fail-closed，普通本校消息以 `LLM_ROUTER_UNAVAILABLE` 安全终止，均不调用 retriever/tool/答案模型。intent-only legacy 响应只在显式选择 `route_v1/v2` 时接受，运行中的 `route_v3` 不会把旧响应误当完整范围结论。
- 跨租拒绝、身份澄清、输入护栏、MIXED 与引述净化回合的用户原文及固定回答以 `status=partial` 保存；`quote_redacted` 在语义计划覆盖前冻结，成功生成的 user/assistant 也都只作普通历史展示，永不以 `completed` 进入模型上下文：普通历史接口仍可展示，模型上下文查询只读取 `status=completed`，避免被拦截文本在下一轮重新进入模型。
- semantic router 每次模型调用记录不含正文的 provider、实际或配置 model、prompt name/version/hash、tokens_in/out 与 latency；纯拒绝写入同事务 trace/span/usage，MIXED 写入 guard/scope span，测试校验真实值而非只查行数。
- 所有消息先完成 scope-aware router，再发送首个可见 `meta`。客户端在路由等待期间取消时先提交保守 mixed guard/span，标记 `degraded=true`、`scope_finalized=false`，随后继续传播取消；不发送 `meta/step/delta`，不进入检索、工具或答案生成。
- 公开比较豁免必须同时满足真实比较结构、公开信息类别、本校参照或“仅用已知信息”限定，且不含敏感对象、取数动作、外校访问或提示注入；任一外校分句不可证明为公开比较时，整条消息只能使用经原文校验的 `mixed` 安全片段，否则拒绝。
- 二次复审又发现 2 Major / 1 Minor：路由不可用与 MIXED 生成失败原先把用户原文落成 `completed`；router usage 漏记 ALLOW、输入护栏、路由不可用及部分拒绝分支；router span 属性与真实断言不足。修复后只有已确认安全的 ALLOW 失败保留 `completed`，其余失败落 `partial`；guard path 带 `LLM_ROUTER_UNAVAILABLE` 和 usage，ALLOW 单独写 `scope_router` span，输入护栏在 `meta` 前完成并记录 usage。审计脱敏只精确放行固定格式的路由名称/版本、SHA-256 摘要和 token 计数，真实 prompt 与非法伪摘要继续遮蔽。
- 开发校准最终为 **87/87 matched / 0 fragment failures**，实际路径分布为显式证据早拒 27、scope-aware 模型 53、本地普通放行 7；这是开发证据，不冒充线上 P95 或 A12 评估结果。
- 最终复审修复后的本地生产代码门禁：完整 `tests/harness` **306 passed**；真实 PG/Qdrant/SSE 目标批次 **47 passed / 9 skipped，302.46 秒**；租户范围数据库专项 **13 passed，178.53 秒**（重置测试库后以 `--timeout=300` 运行）；临时模型校准 **87/87 matched / 0 fragment failures**（早拒 27、模型 53、本地 7）；Ruff、Mypy（`app tests` 340 个源文件 + `scripts` 3 个源文件）、Bandit、测试库 Alembic check（No new upgrade operations detected）与 `git diff --check` 全部通过。首轮 0B/3M、二次复审 0B/2M/1m 与最终复审 0B/1M 均已修复；最终 Major 是引述净化成功回合被后续语义计划覆盖而误存 `completed`，现由不可变 `quote_redacted` 标志封住，并补内存、真实 PostgreSQL 状态与上下文断言。最终独立复核为 0 Blocker / 0 Major，PASS；最终测试夹具修复后，本地完整 `tests/harness` 为 **307 passed**、三条受影响业务/集成文件为 **18 passed**。代码提交 `79b84ab` 的 PR #109 final code-head CI 全绿（backend run `46067099625`：**1530 passed / 19 skipped**；security run `46067099758`），等待用户明确合并授权；A12 分母 `195/60/46/20/69`、门槛 `85/100/90/95/80`、旧分数和评估语料未修改，M4 仍未达到退出条件。
