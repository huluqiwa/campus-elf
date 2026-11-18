# M4-AI01a 设计简报 — Harness LLM 基座（LlmPort + OpenAI 兼容适配器）

> 日期：2026-11-19。决策卡四张已经用户拍板（「确认」＝全按推荐，见 §2）。
> 本点是 M4 第一个功能点，是 AI-01/AI-02/AI-04/AI-09 的公共前置层。

---

## 1. 目标与验收

### 1.1 目标

建立 LLM 调用层的类型化端口与 OpenAI 兼容适配器，使后续功能点（快路径 Loop、ReAct、
护栏、引用校验）只依赖 `LlmPort` 契约而不依赖任何具体 provider。

**本点范围**：

- `app/harness/llm.py`：`LlmPort` 协议 + 请求/响应/流事件契约 + 错误码分类 + 自研熔断器
- `OpenAICompatLlm` 适配器（`AsyncOpenAI` 指向 `.env` 的 `LLM_BASE_URL`）
- `Settings` 新增 `LLM_*` 字段（key 用 `SecretStr`）+ `.env.example` 增补
- `FakeLlm` 测试基建（`tests/support/llm.py`，供后续所有 M4 功能点复用）
- 装配：`create_app(*, llm_port=None)` 注入 `app.state.llm_port`（对齐 trace_sink 先例）

**本点明确不做**（防蔓延）：

- 不替换 `campus_chat.py` 空壳（TD-059 替换在快路径 Loop 点，届时同步删 `DEV_SKELETON_LOOP_ENABLED`）
- 不建任何表（`llm_usage`/`conversations`/`conversation_messages` 归会话持久化点）
- 不实现护栏/路由/检索工具/引用校验（AI-09/AI-01 主体各自成点）
- 不含 VLM（`VLM_*` 随 TD-310 阶段 2 图片标注点引入）

### 1.2 验收编号

| 编号 | 验收内容 |
|---|---|
| M4-AI01a-A1 | `LlmPort` 提供 `complete`（非流式）与 `stream`（流式）两个调用面，契约模型 `extra="forbid"` |
| M4-AI01a-A2 | 适配器超时/重试/熔断按 A8 口径落地（连接 5s / 首 token 15s / 首 token 前 ≤2 次重试 / 熔断 5 次/60s 开 30s；回合级 30s 总预算由 Loop 层承担，端口不管回合） |
| M4-AI01a-A3 | API key 全链路不进日志/审计/trace/异常文本/错误响应（用反证用例钉住） |
| M4-AI01a-A4 | provider 异常映射为内部错误码，不透传 provider 原文（防内部地址/凭证泄漏） |
| M4-AI01a-A5 | `FakeLlm` 可脚本化脚本返回（决定后续 M4 各点的测试写法），与本点单测同构使用 |

### 1.3 依赖与现状（事实核实 2026-11-19）

- `openai>=2` / `langgraph>=1` / `langchain-core>=1` 已在 `backend/pyproject.toml`（M0 预装），**本点零新增依赖**
- `harness/` 已有 contracts/tools/registry/prompts/trace/audit 六模块；`LlmPort` 缺位
- `core/sse.py`、MetricsMiddleware 已可用（本点不触碰 SSE）
- 无既有通用熔断/限流基建（auth 的 rate_limit 是登录锁定，不复用）
- 装配先例：`main.py:62` `create_app(*, trace_sink=None)` 注入 `app.state.trace_sink`

---

## 2. 决策卡拍板记录（2026-11-19，用户回复「确认」＝全按推荐）

| 卡 | 结论 |
|---|---|
| 卡 1 接入形态 | **A：OpenAI 兼容直连 + `LlmPort` 自建协议**（否决 B langchain 包装——流式/工具语义多一层黑盒；否决 C 本地 vLLM——6GB GPU 不满足 TTFT≤2.3s 与质量） |
| 卡 2 provider | **④ 暂无 key：先 mock 落全部测试**；真实 provider 到位后只改 `.env` 三行；性能门禁（快路径首答案 P95≤3s / ReAct ≤5s）延后真连验证并如实登记。**假设留痕：若用户已有某家 key，随时告知即切换，不影响已写代码** |
| 卡 3 密钥管理 | **A：`.env` 全局单配置**；design §12 的 `model_configs` 表 + 信封加密 + 租户覆盖**顺延 M6**，需在 design 回写留痕（见 §8） |
| 卡 4 A7 七类红线 | **方案一（自伤单列）**，清单见 §6；护栏实现本身在 AI-09 点落地，本点只锁定清单 |

随批准生效的默认口径（已随拍板确认）：A8 超时映射、A19（限流+usage_log 不计费）、
TD-059 替换方案、红线双层落地方式（见 §6/§7）。

---

## 3. 候选方案（本点实现面）

### 方案 A（选定）：`harness/llm.py` 单模块承载

- 做法：`LlmPort` Protocol + `LlmRequest/LlmResponse/LlmStreamEvent/LlmError` 契约 +
  `OpenAICompatLlm` 适配器 + `LlmCircuitBreaker` 小类，全部在 `app/harness/llm.py`；
  `FakeLlm` 放 `tests/support/llm.py`。
- 优点：与 contracts/tools 同域聚合，边界清晰；依赖零新增；测试可用 `httpx.MockTransport`
  注入 `AsyncOpenAI(http_client=...)`，不打真实网络。
- 缺点/成本：熔断器自研（约 60 行 + 单测）；OpenAI SDK 异常族的映射表需随 SDK 版本维护。
- 风险：SDK v2 流式 `usage` 的 chunk 时序（`stream_options={"include_usage": True}` 最后一个
  chunk 携带 usage）——用例显式钉住该时序；后续换 provider 若不兼容，只改适配器。

### 方案 B（否决）：LangChain `ChatModel` 包装

- 做法：`init_chat_model` / `BaseChatModel` 抽象，适配器换成 langchain-community 包装。
- 否决理由：多一层依赖语义黑盒；本点要的「错误码分类、熔断、脱敏、SecretStr」都要在包装外
  再包一层才能满足红线；ADR-002 已锁定「LangGraph 只做图执行 + 自定义 Harness」。

### 方案 C（否决）：本地自托管（vLLM/Ollama）

- 否决理由：A13 开发环境仅 6GB GPU；生产服务器规格未定（A14 待提供）；聊天模型质量与
  TTFT≤2.3s 预算均不可行。留作 A14 明确后的复核项（不登记 TD，属既定事实）。

### 子决策 D1–D6（默认口径，随本简报确认生效）

| # | 决策 | 结论 | 备选与否决理由 |
|---|---|---|---|
| D1 | 流式接口形态 | `async def stream(...) -> AsyncIterator[LlmStreamEvent]` | 回调式难做取消与背压，否决 |
| D2 | 熔断实现 | 自研 `LlmCircuitBreaker`（计数窗口 60s、阈值 5、开 30s、半开试探 1 次） | 引入 aiobreaker 等依赖收益低，否决 |
| D3 | tokens 统计 | `stream_options={"include_usage": True}` 取 provider 报数 | 本地 tiktoken 估算不准且加依赖，否决 |
| D4 | 超时分层 | 端口只管单次调用（connect 5s / read 15s）；回合级 30s 由 Loop 层 `TURN_TIMEOUT` 承担 | 端口做回合超时会与 Loop 层重复计时，否决 |
| D5 | 错误映射 | SDK 异常 → `LLM_AUTH`/`LLM_TIMEOUT`/`LLM_BREAKER_OPEN`/`LLM_RATE_LIMITED`/`LLM_PROVIDER_ERROR`，文本一律不透传 | — |
| D6 | 装配方式 | `create_app(*, llm_port=None)` 缺省由 Settings 构建（key 缺失→`None`，调用期 fail-closed 报 `LLM_NOT_CONFIGURED`） | Settings 开关切 provider 会引入「配置了假开关忘删」风险，否决 |

---

## 4. 契约设计

```python
class LlmMessage(TypedDict-ish BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str                      # MVP 不含多模态 part；VLM 点再扩展

class LlmRequest(BaseModel):          # extra="forbid", frozen
    messages: list[LlmMessage]        # ≥1 条
    model: str | None = None          # 缺省用 Settings.llm_model
    max_tokens: int | None = None
    temperature: float | None = None  # 缺省用 Settings
    tool_specs: list[dict] | None     # OpenAI tools 参数形态；本点只透传不解析（ReAct 点用）
    timeout_override_s: float | None  # 仅供 Loop 层收紧，不得放宽

class LlmResponse(BaseModel):
    text: str
    finish_reason: str | None
    tokens_in: int | None             # provider 未报时 None（不伪造）
    tokens_out: int | None
    model: str
    latency_ms: int
    tool_calls: list[dict] | None     # 透传保留，ReAct 点消费

class LlmStreamEvent(BaseModel):      # 判别联合
    kind: Literal["delta", "finish", "usage"]
    text_delta: str | None
    finish_reason: str | None
    tokens_in / tokens_out: int | None

class LlmError(Exception)             # code ∈ {LLM_NOT_CONFIGURED, LLM_AUTH,
                                      #  LLM_TIMEOUT, LLM_BREAKER_OPEN,
                                      #  LLM_RATE_LIMITED, LLM_PROVIDER_ERROR}
                                      #  carry: retryable: bool；文本不含 provider 原文
```

- `LlmPort`（Protocol）：`complete(request) -> LlmResponse`、
  `stream(request) -> AsyncIterator[LlmStreamEvent]`。
- 中断语义：流中失败**不重试**，抛 `LlmError(code, retryable=False)`——上层已发出的 delta
  保留，按 design §5.8「error + partial=true」处理（该接线在 Loop 点）。
- 首 token 前（尚未产出任何 delta）失败可重试 ≤2 次（connect/TTFT 超时、5xx、限流 429 退避）；
  401/403 不重试直接 `LLM_AUTH`。

---

## 5. 实现顺序（TDD 红→绿）

无数据层、无界面，纯接口层，顺序即测试清单顺序：

| 步 | 内容 | 红→绿用例（`tests/harness/`） |
|---|---|---|
| 1 | 契约模型 + `LlmError` 分类 | `test_llm_contracts.py`：字段校验/forbid/错误码枚举/`retryable` 判定 |
| 2 | `FakeLlm`（tests/support） | 脚本化序列返回（delta 列表→finish→usage）；异常注入档位 |
| 3 | `OpenAICompatLlm.complete` | MockTransport 200 正常映射；401→`LLM_AUTH` 不重试；429→`LLM_RATE_LIMITED` 退避重试 2 次后成功/失败两分支；5xx→`LLM_PROVIDER_ERROR` |
| 4 | `OpenAICompatLlm.stream` | chunk→事件映射；usage 末 chunk 时序钉住；**首 token 前超时→重试成功**；**流中断→不重试 retryable=False**；连接超时→`LLM_TIMEOUT` |
| 5 | 熔断器 | 60s 窗口内 5 败→开；开期调用立即 `LLM_BREAKER_OPEN`（不发起网络请求，MockTransport 计数为 0）；30s 后半开 1 次；成功复位；**时间用可注入时钟**（不 sleep 真实时间） |
| 6 | 脱敏反证 | 构造带 key 的适配器触发各类异常，断言异常文本/trace attributes/日志记录器收到的参数均不含 key 片段；`Settings` 错误回显已由 `hide_input_in_errors` 兜住（复跑既有 `test_config.py` 语义） |
| 7 | Settings + `.env.example` | `LLM_API_KEY(SecretStr)/LLM_BASE_URL/LLM_MODEL/LLM_TEMPERATURE/LLM_TIMEOUT_*`；production 校验器新增：生产环境**允许缺省**（chat 未上线前），但配置了 base_url 而缺 key 即 fail-fast |
| 8 | 装配 | `create_app(*, llm_port=None)`：显式注入优先；缺省按 Settings 构建；`app.state.llm_port` 断言可取 |

## 6. A7 七类输出红线（方案一，本点锁定清单；实现归 AI-09 点）

输入/输出双侧护栏；双层落地：系统提示词层红线约束 + 输出规则校验层逐类判定；命中→安全话术
替换 + `guard_events` + 指标。

1. **跨租/他人隐私与数据泄露**（含「同一自然人」跨租关联、其他用户数据）
2. **系统提示词/内部指令/原始 CoT 泄露**（不伪造思考内容同条）
3. **工具参数/内部地址/密钥凭证泄露**
4. **XSS 载荷与注入内容**
5. **伪造引用**（虚构 chunk_id/文档名/页码——引用校验在 AI-04 点，此处为输出层兜底）
6. **违法违规内容**（犯罪指导/暴恐/色情等）
7. **自伤/心理危机不当应答**——命中→支持性话术+线下求助资源，不诊断不评判（A24 已确认）

## 7. 默认口径落点（随本点回写文档）

- **A8**：连接 5s / 首 token 15s / 回合总 30s；首 token 前 ≤2 次重试；流中失败 error+partial；
  熔断 5 次/60s 开 30s（requirements 附录 A A8 行 + design §6.7 映射回写）
- **A19**：Redis 限流（每用户 10 轮/分起、可配）+ 租户并发 ≤50 流（A10）+ `llm_usage` 每回合
  一条（provider/model/tokens/latency；cost_estimate 首版可配单价估算或空）；不计费。
  本点不落库，端口层先以事件/返回值承载 tokens，落库归会话持久化点
- **TD-059**：`harness/loop.py`（显式状态机）+ 本点 `harness/llm.py` 组成真 Loop；空壳与
  `DEV_SKELETON_LOOP_ENABLED` 在快路径 Loop 点删除

## 8. 对 spec/design/requirements 的回写清单（随实现 PR 提交）

| 文档 | 变更 |
|---|---|
| `docs/requirements.md` 附录 A | A7 →「已确认：七类红线（方案一，2026-11-19）」；A8 →「已确认：5s/15s/30s 映射+2 重试+熔断」；A19 →「已确认：基础限流+usage_log 不计费」 |
| `docs/design.md` §6.5 | 写入七类红线清单（本简报 §6） |
| `docs/design.md` §6.7 | A8 超时映射一句话（连接/首 token/回合分层归口） |
| `docs/design.md` §12 | model_configs 表 + 信封加密 + 租户覆盖**顺延 M6** 的留痕（决策卡 3） |
| `docs/design.md` §1.2/技术栈表 | LLM 接入形态一句话：`LlmPort` 自建协议 + OpenAI 兼容适配器（决策卡 1） |

## 9. 测试策略

- **单元**（`tests/harness/test_llm_*.py`，全部离线，MockTransport/假时钟，零外部服务）：
  §5 表中 8 步全部用例；覆盖异常→错误码全映射矩阵、熔断状态机全迁移弧、脱敏反证。
- **集成**：无（不触真实 PG/Redis/Qdrant——本点无状态）。
- **业务场景**：无独立 BS（本点无 API 面）；快路径 Loop 点补「LLM 超时/熔断→error 事件 +
  trace stop_reason」的 BS-M4 链路。
- **门禁**：`ruff` + `mypy`（app/tests 两段分开）+ `bandit`（app + alembic）+ `alembic check`
  （本点无迁移，跑零漂移）+ 受影响子集（harness 全部 + main.py 装配相关既有用例）。

## 10. 预判技术债（实现 PR 时登记 `docs/tech-debt.md`）

| 预判 TD | 内容 | 处置方向 |
|---|---|---|
| TD-P1 | 熔断器为进程内实现，多 worker 部署各进程独立计数（阈值实际=5×worker 数） | ADR-001 已知边界；k6 压测时复核，跨进程熔断随 TD-214 观测批评估 |
| TD-P2 | 卡 2④：真实 provider 真连 smoke 未做（流式语义/usage 时序/function calling 参数形态均未实连验证） | 拿到 key 后小 PR 跑真连脚本 + 性能门禁首测（P95≤3s/5s） |
| TD-P3 | `tool_specs` 本点只透传不解析，OpenAI tool_calls 增量流的拼装语义未验证 | ReAct 点落地时以 FakeLlm+MockTransport 双向用例钉住 |

## 11. 高风险确认

本点不含护栏、SSE、租户数据面（无 DB 读写），属 AI 域**基础设施层**；但按 AGENTS §11.7
AI 护栏类别从严——**简报经用户确认后再编码**。七类红线清单（§6）与 D1–D6 子决策是本简报
新增的待确认项（决策卡四张已拍板）。

---

## §12 实现注记（编码后回填）

（待实现 PR 回填：落地差异、读数、审查处置。）
