# 校园陪伴精灵 — Langfuse 观测/评估/Prompt 规范（observability-langfuse.md）

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-10-19 |
| 状态 | 待用户确认 |
| 定位 | LLM 观测 + 评估实验 + Prompt 草稿/实验；非审计日志、非聊天运行时依赖 |

## 0. 版本锁定

| 组件 | 版本 | 说明 |
|---|---|---|
| Langfuse Server | **4.63.0** | 2026-10-16 最新稳定版；OSS v4 当前 GA |
| Langfuse Web / Worker 镜像 | `langfuse/langfuse:4.63.0`、`langfuse/worker:4.63.0` | 固定 tag，不使用 `latest` |
| Langfuse Python SDK | **5.0.2** | 2026-10-16；要求 Server ≥4.59.0；支持 Python 3.11–3.15 |
| 接入方式 | OpenTelemetry（SDK v5） | v4 移除旧 read API；使用 Observations API v2 / Metrics API v2 / Scores API v3 |

- **不用 v3**：v3 已 deprecated，安全补丁只到 2027-01-31；v4 是当前 GA。
- **不用 `latest`**：发布节奏快（v4.50–v4.63 两周内多次发布）；升级必须跑 trace/评估回归。
- **升级策略**：先在开发/测试验证 4.63.0 → 目标版本；确认 OTel 映射、dataset/experiment、score、Prompt 回写、访问控制无回归后再升级生产。
- **Docker Compose 定位**：官方将其定位为单 VM/测试；校内单机可用，但无 HA/自动扩缩。若要求高可用，改用 Helm/K8s 或托管部署；不影响当前 MVP。
- **实时性**：v4 的 OTel 数据默认可能延迟；SDK ≥4.7.0 自动带 ingestion header；自托管可设 `LANGFUSE_MIGRATION_V4_NATIVE_OTEL_BEHAVIOUR=direct` 强制实时入库（M0 验证）。

---

## 1. 边界

| 是 | 不是 |
|---|---|
| LLM trace/session/generation/retrieval/tool/guard 观测 | 合规审计日志（审计在 PostgreSQL 哈希链/WORM） |
| Ragas/评估分数、dataset/experiment | 业务 trace 唯一存储（本地 trace 表/JSONL 是事实来源） |
| Prompt 草稿、实验、版本对比 | 生产 prompt 唯一事实来源（Git 是唯一来源） |
| 成本/延迟运营看板 | 租户授权/隔离组件 |
| 失败/降级/注入复盘 | 聊天运行时强依赖（异步上报，故障不影响聊天） |

---

## 2. 部署拓扑

```text
应用主机                         观测主机（独立）
┌──────────────┐                ┌─────────────────────────┐
│ FastAPI      │  async batch   │ Langfuse Web/Worker     │
│ Worker       ├───────────────►│ PostgreSQL              │
│ Harness      │   HTTPS/内网    │ ClickHouse              │
└──────────────┘                │ Redis                   │
                                │ S3/MinIO                │
                                └─────────────────────────┘
```

- 生产：独立主机/VM；固定 **Langfuse Server 4.63.0**；按官方 self-hosting 组件部署（Web + Worker + PostgreSQL + Redis + ClickHouse + S3）。
- 开发/测试：`observability` optional profile，默认关闭；需要 trace UI 时再启。
- 网络：仅后端/Worker 可访问观测主机；Langfuse UI 仅平台/运维访问。
- 资源：不与应用主机共用 16GB；Langfuse 组件资源按官方建议配置。

---

## 3. 采集架构

```text
Harness TraceRecorder
  → LocalTraceExporter（PostgreSQL trace 表 + JSONL，必选）
  → RedactionFilter（tenant_hash/user_hash、PII、系统提示词、密钥、内网地址）
  → LangfuseExporter（可选，异步有界队列批量上报）
       → Langfuse SDK / OpenTelemetry → 自托管 Langfuse
```

- `TraceExporter` 接口；本地 exporter 必选；Langfuse exporter 可插拔。
- 异步、批量、有界队列；失败重试/丢弃；不阻塞聊天。
- `trace_id` 与 API 响应、审计、`usage_log` 关联。
- 默认只上报元数据；内容采集按场景白名单。

---

## 4. 数据映射

| 本地 | Langfuse | 说明 |
|---|---|---|
| `chat_traces` | Trace | trace_id、session_id=conversation_id、user_id=hash |
| `chat_trace_spans` (generate) | Generation | model、prompt name/version/hash、tokens、latency |
| `chat_trace_spans` (retrieve/rerank) | Span | query、top-k、chunk_id、scores（默认不含原文） |
| `chat_trace_spans` (tool) | Span | tool name、status、duration；不含原始参数 |
| `guard_events` | Event/Span | rule/severity/action；不含敏感原文 |
| `llm_usage` | Generation usage | tokens/cost/latency |
| `eval_runs/results` | Dataset/Experiment/Score | Ragas 指标、通过率、失败样例 |

默认字段：`tenant_hash`、`user_hash`、`environment`、`app_version`、`prompt_name/version/hash`、`model/provider`、`tokens_in/out`、`first_token_ms`、`latency_ms`、`chunk_ids`、`tool_names`、`guard_flags`、`stop_reason`、`error_code`。

---

## 5. 内容采集场景

### 5.1 默认

- 只采集元数据+哈希；不采集原文、PII、系统提示词、工具 schema、密钥、内网地址。
- 适用于所有生产流量。

### 5.2 知识库构建后测试

- 触发：校方在 B 端上传测试集并运行测试。
- 内容：测试问题、期望要点/引用、实际答案、引用、Ragas 分数、失败样例（脱敏）。
- 存储：`kb_test_{tenant_hash}_{kb_version}` dataset/experiment。
- 生命周期：随 KB/测试集生命周期；tenant-scoped。

### 5.3 C 端反馈

- 触发：用户提交有用/无用+原因+可选文本。
- 同意：反馈表单展示隐私告知；提交即知情同意。
- 内容：关联问题、答案、引用（脱敏）；tenant_hash/user_hash。
- 校方：B 端反馈列表查看；平台：Langfuse 深入复盘。
- 删除：用户申请/账号删除后匿名化。

### 5.4 校方定制化

- 触发：校方提交定制化请求，附测试集+FAQ。
- 内容：基线/候选版本测试问题、答案、FAQ、Ragas 分数、配置、prompt 版本、检索参数（脱敏）。
- 存储：`customization_{request_id}` experiment。
- 用途：SaaS 优化、校方验收、回滚比较。

> 场景开关按租户/请求白名单；默认关闭内容采集；开启需审计。

---

## 6. 脱敏

- `tenant_id` → HMAC/hash；`user_id` → HMAC/hash。
- PII：身份证/手机/邮箱/地址/姓名按规则掩码或移除。
- 系统提示词、工具 schema、密钥、内网地址、数据库连接串：永不外送。
- 聊天原文：仅场景白名单；先脱敏；长度截断；不含附件原文。
- 输出护栏命中内容：只记录规则/动作/摘要，不记录完整敏感原文。
- 脱敏在 `RedactionFilter` 统一执行；单元测试覆盖。

---

## 7. Prompt 治理

- **Git 是生产 prompt 唯一事实来源**：版本化文件、代码评审、发布审批。
- Langfuse Prompt Management 用于：草稿、实验、版本对比、评审 UI。
- 评审通过的 prompt 导出/回写 Git；走发布审批后部署。
- 运行时读取 Git 部署产物，不直接依赖 Langfuse。
- Langfuse 记录 prompt 名称/版本/哈希；不出现“Git 一套、Langfuse 一套”。
- 禁止在生产 Langfuse UI 直接改 prompt 并生效。

---

## 8. 评估

### 8.1 Ragas 与 Langfuse 的分工

- **Ragas = 指标计算引擎**：faithfulness、answer_relevance、context precision/recall、自定义 citation precision、refusal correctness、safety pass；离线跑批、固定数据集、回归对比。
- **Langfuse = 评估与观测平台**：dataset/experiment、score 回传、trace 关联、版本对比、人工复核、成本/延迟看板；不内置 RAG 专用指标，主要消费 Ragas/自定义 evaluator 的分数。
- **黄金集事实来源**：`evals/cases/`；Langfuse Dataset 是同步副本，不是唯一存储。
- **故障隔离**：Langfuse 不可用时，Ragas 仍可本地跑批出报告；Langfuse 不是评估的前置依赖。
- **不重复实现**：Ragas 算分，Langfuse 展示/对比；不在 Langfuse 里重写一套 RAG 指标。

- Ragas 本地跑批：faithfulness、answer_relevance、context precision/recall；自定义 citation precision、refusal correctness、safety pass。
- 阈值：总通过≥85%、安全 100%、引用 90%、拒答 95%、分类 80%、回归下降≤3%。
- 评估集事实来源：`evals/cases/`；Langfuse dataset/experiment 用于对比。
- KB 测试/定制化：校方测试集；基线/候选对比；报告存 B 端/`evals/reports/`。
- 失败样例入回归；judge 固定模型/温度/seed。

---

## 9. 权限与隔离

- Langfuse 仅平台/运维访问；校方看 B 端报告，不直接登录。
- 按环境分项目；metadata `tenant_hash`；数据集/实验按租户标记。
- 访问审计：谁、何时、看了哪个租户的 trace/测试报告。
- 未来如需校方直接访问：每租户独立项目/角色，需重新评估。
- 不将 Langfuse 数据用于跨租比较/训练。

---

## 10. 留存与故障

- trace 180 天；测试集/定制化数据集按 KB/请求生命周期；反馈删除后匿名化。
- Langfuse 故障：异步队列重试/丢弃；本地 trace 仍写入；聊天不受影响。
- 定期检查队列积压/丢弃率/成本；告警。
- 升级：固定版本；升级前备份；回归测试；不自动跟随 latest。

---

## 11. 验收标准

- [ ] 每个 LLM 回合有本地 trace（必选）。
- [ ] Langfuse 关闭时聊天正常、无报错。
- [ ] 开启后 trace/generation/retrieval/tool/guard/score 映射正确。
- [ ] 默认无原文/PII/系统提示词/密钥外送。
- [ ] tenant_hash 隔离；平台/运维专属；访问审计。
- [ ] KB 测试/反馈/定制化场景开关正确；同意/删除/匿名化。
- [ ] Ragas 报告与 Langfuse experiment 可选同步。
- [ ] Langfuse 故障不影响聊天；队列/丢弃可观测。
- [ ] Prompt 以 Git 为源；回写/发布审批流程可用。
