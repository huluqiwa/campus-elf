# M4-TD354 — LLM 超时/重试/熔断参数放宽（慢模型适配）

> 状态：**已确认，实现中**（用户 2026-11-25 拍板：方案 A 开做 + SLO 保留目标注明 dev 不达标）。
> 触发：M4-EVAL Ragas 全量跑批 cascade（TD-354）。用户 2026-11-25 拍板方向＝「放宽超时/重试/熔断参数」（非换模型、非修记账逻辑）。
>
> **实现注记（2026-11-25）**：
> - `config.py` 新增 `llm_turn_timeout_seconds`（default 30, gt0/le300）；`chat_assembly.py` 给 FastPathLoop/ReactLoop 传 `turn_timeout_s=settings.llm_turn_timeout_seconds`。
> - e2e 放宽剖面落 `deploy/docker-compose.e2e.yml`（读 90/回合 120/重试 2/熔断 10 次 180s 开 10s，宿主 E2E_LLM_* 可覆盖）；`.env.example` 补可配项 + 注释式 dev 放宽剖面；生产代码默认紧不变。
> - 测试：`test_config.py::test_llm_turn_timeout_bounds` + 默认断言；`tests/harness/test_chat_assembly.py`（接线 120/默认 30）；`test_fast_path_loop.py::test_relaxed_turn_timeout_lets_slow_first_token_complete`（慢首字 1s + 超时 2s → done，对照原 0.2s 超时弧）。
> - 文档回写：design §A8/§ReAct TURN_TIMEOUT 注记、spec AI-01 验收 SLO 注记、tech-debt TD-354。
> - **评估侧配套（留 eval 分支）**：`qa_runner.collect_all` timeout 60→150s，待 TD-354 合并后回 eval 分支改 + 重跑全量基线。
> - 待验证：e2e 栈重启（拾取新 env）→ 重跑全量 195 → cascade 消除（基础设施失败 <5%）→ TD-354 改已完成。

## 1. 目标与验收

- **目标**：让当前 `.env` 慢模型（首字实测中位 9.7s / p90 51.6s / max≈60s）能可靠完成回合，消除批量负载下的熔断 cascade，使 Ragas 全量 195 条能产出**有效质量基线**（而非基础设施失败噪声）。
- **引用**：
  - `docs/spec.md:347-348`（快路径首答案 P95≤3s；ReAct 首状态≤3s/首答案≤5s）、`:542`（性能 SLO）。
  - `docs/design.md:935`（A8 映射：连接 5s / 读首 token 15s / 回合 30s / 重试≤2 / 熔断 5 次/60s 开 30s）、`:874`/`:878`（`TURN_TIMEOUT=30s`）。
  - `docs/tech-debt.md` TD-354（根因台账）。
- **验收**：
  1. 真实栈并发 1 跑批，`LLM_BREAKER_OPEN` + `LLM_TIMEOUT` 失败数显著下降（目标：基础设施失败 <5%，剩余失败归因于 AI 质量而非超时/跳闸）。
  2. 慢首字（>30s）回合不再被 30s 硬超时误杀。
  3. 真死 provider（连接拒绝/持续 5xx）仍能在合理时间内 fail-closed 报错（守卫不失效，只放宽）。
  4. 配置项有上下限校验；装配接线有测试；受影响单元/集成/业务场景全绿。

## 2. 根因（照代码 + 实测，2026-11-25）

- 模型首字慢：done 回合 `first_delta_ms` p50=9723 / p90=51579 / max=59951；`latency_ms` p50=9882 / p90=51518 / max=60140（collected-full-20261124-181255.json，195 回合）。
- 放大机制：
  1. 单次读超时 `first_token_timeout_s=15s`（`config.py:225`、`llm.py:322/363`）→ 慢模型首字常超 15s → 触发重试（`max_attempts=3`）。
  2. **每次重试各记一次熔断失败**（`llm.py:485` `record_failure`）→ 两三个慢回合即记满 5 次/60s。
  3. 熔断跳闸 30s（`config.py:229-231`、`llm.py:233-235`）→ 窗口内顺序回合被 `before_call` 快速失败（`llm.py:205`，不记失败但牺牲用例）→ 并发 1 仍 87 条 `LLM_BREAKER_OPEN`。
  4. 回合首字硬超时 `turn_timeout_s=30`（`loop.py:106/388`、`react_loop.py:109/322`）**写死**，装配未从配置传（`chat_assembly.py:47-68`）→ 生成段首字 >30s 的回合被 `LLM_TIMEOUT` 误杀（7 条）。
  5. 首字前还串一次路由模型调用（`chat_assembly.py:69` `LlmRouter`）→ 路由+生成两段慢叠加；30s 只卡生成段，不卡路径全程。
- 流式实现无缓冲 bug（`llm.py:474` 真逐块 yield）；「首字≈总耗时」是 provider 行为（憋久后一次性倒出）。
- 并发 2 基线 116/195 失败、并发 1 基线 94/195 失败，均无效。

## 3. 候选方案（≥2）

### 方案 A（推荐）：参数化 turn_timeout + 全套放宽到贴合实测

- **代码改动（小）**：
  - `config.py` 新增 `llm_turn_timeout_seconds: float = Field(default=30.0, gt=0, le=300)`（命名对齐 `llm_*` 族；默认仍 30，生产保持紧）。
  - `chat_assembly.py` 装配时给 `FastPathLoop` 与 `ReactLoop` 传 `turn_timeout_s=settings.llm_turn_timeout_seconds`。
- **配置放宽（dev/e2e 的 `.env`，生产默认不动）**：
  | 参数 | 现值 | 拟值 | 依据 |
  |---|---|---|---|
  | `llm_first_token_timeout_seconds`（读） | 15 | 90 | 覆盖单次调用首字 worst≈60s + 余量（le=120） |
  | `llm_turn_timeout_seconds`（生成首字硬超时，新） | 30 | 120 | 覆盖慢生成段首字 |
  | `llm_max_attempts` | 3 | 2 | 减重试→减重复记失败（ge=1） |
  | `llm_breaker_failure_threshold` | 5 | 10 | 更宽容 |
  | `llm_breaker_window_seconds` | 60 | 180 | 拉长窗口稀释偶发慢 |
  | `llm_breaker_open_seconds` | 30 | 10 | 跳闸后更快恢复试探 |
- **评估侧配套**：`qa_runner.collect_all` 的 `timeout=60` 需抬到 ≥150s（否则后端放宽后慢回合被 runner 60s 截断）——属当前 eval PR 的小改。
- 优点：贴合实测 p90+余量，慢模型能答完；重试减到 2 次降低重复记失败；熔断更宽容+恢复快；生产默认仍紧（安全）。
- 缺点/成本：dev/e2e 聊天首字可能等很久（UX 慢，用户已接受）；**延迟 SLO P95≤3s 仍达标不了**（模型本身慢，放宽超时≠模型变快）；放松了 A8 守卫需 spec 回写。
- 风险：真死 provider 时单次等待更久（读 90s）才报错——但连接超时仍 5s、熔断仍在，fail-closed 不失效。
- 对 spec/design 影响：需回写 A8 超时值为「可配置 + dev/e2e 放宽剖面」；延迟 SLO 口径需用户裁定（见 §6）。

### 方案 B：只调 .env 现有参数，不动 turn_timeout 代码

- 放宽 `first_token_timeout`/`breaker`/`max_attempts`（同上），但 `turn_timeout_s=30` 写死不动。
- 优点：零代码改动，纯配置。
- 缺点：生成段首字 >30s 的回合（p90 51s → 约 10-20%）仍被 `LLM_TIMEOUT` 误杀，基线仍不干净。**治标不全**。
- 风险：无法达成验收 1/2。

### 方案 C（不在本次范围）：参数化 + 修「一回合重试多次记多次失败」记账

- 用户本次选「放宽参数」（方向卡 option2），未选「修记账」（option3）。记账逻辑改动留作后续 TD（见 §6），本次仅以 `max_attempts=2` 缓解。

## 4. 选择结论

- **选 A**。B 因 `turn_timeout_s` 写死无法根治慢尾（验收 2 达不到）。C 的记账修复用户未选，登记 TD 延后。
- **需改 spec/design**：A8 超时值改为「可配置；生产默认紧、dev/e2e 用放宽剖面适配慢模型」；延迟 SLO 口径待用户裁定（§6 卡）。
- 生产默认值保持紧（30s/15s/5 次），**不**把放宽值设为代码默认——避免生产继承慢超时；放宽只落在 dev/e2e 的 `.env`。

## 5. 实现顺序与 TDD 清单

数据/配置 → 装配接线 → 评估配套 → 文档回写：

1. **红**：`test_config` 加 `llm_turn_timeout_seconds` 校验用例（默认 30、上下限 gt0/le300、非法值拒）。
2. **红**：装配测试断言 `FastPathLoop`/`ReactLoop` 收到 `turn_timeout_s=settings.llm_turn_timeout_seconds`（注入 settings 改值→loop 实例属性随之变）。
3. **红**：loop 慢首字集成测试——mock provider 首字延迟 >30s 但 <新超时→不报 `LLM_TIMEOUT`（用注入 clock/短超时验证边界）。
4. **绿**：加配置项 + 接线 + （loop 已支持 turn_timeout_s 参数，无需改 loop 逻辑）。
5. **重构**：命名/注释对齐 `llm_*` 族。
6. eval 配套：`qa_runner` collect timeout 抬到 150s + 对应测试。
7. 文档回写：`design.md:935/874/878`、`spec.md:347-348/542` 注记、`tech-debt.md` TD-354 处置、`.env.example`（如有）放宽剖面说明。

## 6. 预判技术债 / 待用户裁定

- **延迟 SLO 与慢模型矛盾（需要用户拍板）**：spec P95≤3s 是「生产用足够快模型」的目标；当前 dev 模型 p90 首字 51s，放宽超时也达标不了。两种口径：
  - (a) SLO 数字不变，注明「当前 dev 模型不达标，生产须换快模型」——诚实保留目标。
  - (b) 放宽 SLO 数字到贴合慢模型——永久放松目标（不推荐）。
  - 推荐 (a)：SLO 不动，dev/e2e 放宽超时只为拿基线，生产换快模型才是根治（留 TD）。
- **记账逻辑**（一回合重试多次记多次失败）：登记 TD，延后独立点（用户本次未选）。
- **生产模型选择**：dev 慢模型根治靠换快模型，登记 TD 留用户后续决策。
- **路由段慢**：`LlmRouter` 首字前额外模型调用叠加延迟，是否合并/缓存留后续优化 TD。

## 7. 高风险确认

本点改 A8 超时守卫 + 触及延迟 SLO 口径，属高风险。**简报需用户确认后再编码**。确认项：
1. 方案 A 的参数拟值（§3 表）是否接受。
2. 延迟 SLO 口径选 (a) 保留目标 / (b) 放宽数字。
3. 放宽只落 dev/e2e `.env`、生产默认保持紧——是否认可。