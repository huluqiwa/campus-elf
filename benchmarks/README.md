# 性能压测（benchmarks/）

> 性能“写完即算达标”不成立；涉及性能的里程碑必须附 k6 脚本与报告。

## 1. 目录

```text
benchmarks/
├─ README.md
├─ k6/
│  ├─ README.md
│  ├─ smoke.js
│  ├─ non_ai_200.js
│  ├─ sse_50.js
│  ├─ publish_rollback.js
│  └─ build_queue.js
└─ results/            # 报告（git 忽略）
```

> 脚本在 M6/M3 落地；当前只保留规范与目录。

## 2. 目标

| 场景 | 目标 |
|---|---|
| 核心 API | P95≤500ms（不含 AI） |
| 快路径 SSE | 首答案 token P95≤3s |
| ReAct SSE | 首状态≤3s；首答案 token≤5s |
| 非 AI 并发 | 200 |
| AI 流 | 50（受 LLM API 限流约束，压测后回调） |
| KB 发布/回滚 | 200 并发零 5xx |
| 构建队列 | MinerU 单并发；排队/进度可观测 |

## 3. 场景

### 3.1 smoke
- 登录→看板→列表→健康检查；验证脚本与环境。

### 3.2 non_ai_200
- 200 VU：登录、人员列表、审批待办、消息列表、KB 文档列表。
- 断言 P95/P99、错误率、DB/Redis CPU/内存。

### 3.3 sse_50
- 50 并发流：快路径与 ReAct 分开。
- 记录首状态、首答案 token、总时长、中断率、token 成本。
- LLM 可用 mock/固定响应做基线；真实 LLM 单独小并发验证。

### 3.4 publish_rollback
- 200 并发查询/聊天，同时执行 alias 发布/回滚。
- 断言零 5xx；新会话版本正确；旧会话固定旧版本；缓存失效。

### 3.5 build_queue
- 多文档提交构建；MinerU 单并发；观察队列深度、进度、失败重试、降级 tier。
- 不断言高并发解析；断言队列稳定/可恢复。

## 4. 报告字段

- 环境：CPU/内存/GPU/显存、Docker/WSL 配置、服务版本。
- 数据规模：租户数、文档数、chunk 数、会话数。
- 脚本版本/commit。
- 并发数、持续时间、P50/P95/P99、错误率、吞吐。
- 资源：CPU/内存/GPU/显存、队列深度、DB 连接数。
- 结论：达标/不达标；降级/调优建议。
- 报告存 `benchmarks/results/<date>-<scenario>.md` + JSON。

## 5. 规则

- 测试数据用后即清；独立测试租户。
- 生产压测需审批；不在业务高峰。
- 不把压测数据写入生产审计/反馈。
- 失败/降级必须记录，不隐藏。
- 结果与阈值写入里程碑汇报。
