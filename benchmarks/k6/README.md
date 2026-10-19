# k6 脚本规范

## 计划脚本

| 脚本 | 场景 | 关键指标 |
|---|---|---|
| `smoke.js` | 健康检查/登录/列表 | 可用性 |
| `non_ai_200.js` | 200 VU 非 AI API | P95/P99/错误率 |
| `sse_50.js` | 50 并发 SSE（快/ReAct） | 首状态/首 token/总时长 |
| `publish_rollback.js` | 发布/回滚期间 200 并发 | 零 5xx、版本正确 |
| `build_queue.js` | 构建队列/MinerU 单并发 | 队列深度/进度/降级 |

## 约定

- 使用测试租户/测试账号；密钥从环境变量读取，不写入脚本。
- 每个脚本支持 `BASE_URL`、`TENANT_CODE`、`USERNAME`、`PASSWORD` 环境变量。
- 输出 JSON summary 到 `benchmarks/results/`。
- 不打印 Authorization/密码/Token。
- 脚本变更需代码审查。
