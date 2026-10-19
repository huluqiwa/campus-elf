# 更好做法与技术债台账（tech-debt.md）

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-10-20 |
| 状态 | 生效；每条必须有处置 |
| 上游 | 功能点完成汇报、代码审查 findings、ADR 备选方案 |
| 规则 | 任何“更好的做法”不得只口头说明；必须登记并给出处置 |

---

## 1. 闭环规则

1. **登记时机**：每个功能点完成汇报时必须把本次提出的“更好做法/替代方案”登记为 TD 条目；代码审查发现的改进项同样登记。
2. **处置类型**：
   - `已完成`：本次或后续功能点已落地，附 commit/功能点。
   - `已采纳`：确认要做，指定目标里程碑；进入该里程碑范围或 spec/design 变更。
   - `已延后`：确认有价值但暂不做，写清原因与复查时点。
   - `已拒绝`：明确不做，写清理由（例如与需求冲突、成本大于收益）。
3. **里程碑边界评审**：每个里程碑结束时评审台账；未处置条目不得进入下一里程碑；`已采纳` 条目必须进入目标里程碑的 milestone/spec/design。
4. **优先级**：安全、租户隔离、合规、数据一致性类条目默认优先；低成本质量项可批量进入 M0-F。
5. **验收**：`已完成` 条目必须能被测试或运行验证；`已采纳` 条目必须在 milestone.md 中有对应功能点或验收编号。
6. **禁止静默丢弃**：任何被删除的条目必须在审查记录中说明原因。

---

## 2. 台账

| ID | 来源 | 更好做法 | 处置 | 目标 | 证据/理由 |
|---|---|---|---|---|---|
| TD-001 | M0-A | 用 `create_app()` 工厂 + lifespan，避免导入即配置副作用 | 已延后 | M1 | 当前单进程入口可接受；接 DB/Redis 生命周期时统一改造 |
| TD-002 | M0-A | 统一 uvicorn/stdlib 日志为 JSON（structlog ProcessorFormatter） | 已延后 | M0-F | 当前应用日志 JSON，uvicorn/httpx 仍文本 |
| TD-003 | M0-A | 版本号单一来源（importlib.metadata） | 已延后 | M1 | pyproject 与 Settings 重复 `0.1.0` |
| TD-004 | M0-A | 生产可关闭 `/docs`、`/redoc` | 已完成 | M0-A | `docs_enabled` 配置已落地（c3e53bb） |
| TD-005 | M0-A | `/health/ready` 检查 DB/Redis/Qdrant 依赖 | 已采纳 | M0-F | M0-B 起依赖已可探活；接 backend 后补 |
| TD-006 | M0-A | 收紧 mypy `ignore_missing_imports` | 已延后 | M1 | 先保证全绿；第三方 stub 稳定后再收紧 |
| TD-007 | M0-B | MinIO 运行时改用最小镜像（非 Go 全量镜像） | 已延后 | M6 | 已非 root；镜像体积 553MB 偏大 |
| TD-008 | M0-B | Redis 密码改用 config/secret，避免 argv 可见 | 已延后 | M1 | 当前仅 loopback；接生产前必须改 |
| TD-009 | M0-B | Postgres 显式 shared_buffers/max_connections 调优 | 已延后 | M1 | 当前仅 `shm_size` |
| TD-010 | M0-B | TEI 模型离线预置/支持 ETag 的镜像源 | 已延后 | M0-F | 直连下载 ~4.5GB；已固定 revision |
| TD-011 | M0-B | ClamAV 固定精确版本 tag，而非 `stable` digest | 已延后 | M6 | 已用 digest 固定内容；tag 语义浮动 |
| TD-012 | M0-B | MinerU 4.0.12 镜像构建 + V1 API + 6GB 档位降级验证 | 已采纳 | M0-E | M0-B 只定义 parser profile，未构建 |
| TD-013 | M0-B | 默认只启 core，rag/parser 分开；文档与 compose 对齐 | 已完成 | M0-B | bbc2bfd 已修文档与 profile 口径 |
| TD-014 | M0-B | MinIO 构建注入 Version/ReleaseTag/CommitID | 已完成 | M0-B | 镜像 `--version` 已显示 RELEASE（bbc2bfd） |
| TD-015 | M0-C | `audit_level`/`idempotent`/`redacted_summary` 接入执行策略 | 已延后 | M4 | 当前所有工具执行都审计 |
| TD-016 | M0-C | 只读工具强制显式非空 input/output schema | 已采纳 | M1 | 当前 schema 默认 `{}`，契约强度不足 |
| TD-017 | M0-C | 安全上下文嵌套容器改为不可变映射 | 已延后 | M1 | `frozen=True` 对 `feature_flags` dict 无效 |
| TD-018 | M0-C | AuditSink/TraceSink 改 async 或 worker，避免阻塞事件循环 | 已采纳 | M1 | 落 PostgreSQL 前必须决策 |
| TD-019 | M0-C | `verify()` 支持外部锚点/空链语义；生产 WORM | 已延后 | M1 | 已支持 expected_head/count；空链仍 True |
| TD-020 | M0-C | Prompt 版本语义排序/latest 方法 | 已延后 | M1 | 当前字符串排序，v10/v2 有歧义 |
| TD-021 | M0-C | 移除 `E501` ignore | 已拒绝 | — | 中文 Prompt/文档长行需要；改动收益低 |
| TD-022 | M0-C | 区分 handler 自抛 TimeoutError 与 wait_for 超时 | 已延后 | M1 | 当前统一 TOOL_TIMEOUT；影响较小 |
| TD-023 | M0-C | trace/audit sink 故障隔离策略 | 已延后 | M1 | 当前任一 sink 异常会冒泡；审计策略需明确 |
| TD-024 | M0-C | `CapabilitySpec.permission` 必填或显式 public | 已延后 | M1 | 当前默认 None 即公开，属 fail-open |
| TD-025 | M0-C | 拆分外部 DTO 与内部 `AgentRequest`，TenantContext 只由认证层注入 | 已采纳 | M1 | 接 HTTP API 时必须落地 |
| TD-026 | M0-C | 审计/trace 落 PostgreSQL 仅追加表 + WORM 导出 | 已采纳 | M1/M6 | 当前内存实现不承担生产审计 |
| TD-027 | M0-C | 注册期 `check_schema` + 运行期 schema 异常降级 | 已完成 | M0-C | b15beff 已修并测试 |
| TD-028 | M0-C | `write_turn` 嵌套 spans 脱敏 | 已完成 | M0-C | b15beff 已修并测试 |
| TD-029 | M0-D | 补齐 ESLint/Prettier/lint 脚本并纳入门禁 | 已采纳 | M0-F | 当前只有 vue-tsc/tsc |
| TD-030 | M0-D | 为 api-client 增加 Vitest 单测 | 已采纳 | M0-F | 当前只有 Playwright 冒烟 |
| TD-031 | M0-D | Element Plus 按需引入 + manualChunks，降低 admin 1MB 包体 | 已采纳 | M0-F/M6 | build 警告 chunk >500KB |
| TD-032 | M0-D | Vant 按组件引样式，降低 H5 CSS 体积 | 已延后 | M6 | 当前全量 199KB CSS |
| TD-033 | M0-D | e2e 增加失败态/接口 mock；CI 下不复用已有服务 | 已采纳 | M0-F | CI flag 已改；mock/失败态待补 |
| TD-034 | M0-D | `playwright.config.ts`/`e2e` 纳入 tsconfig 检查 | 已采纳 | M0-F | 当前 typecheck 不覆盖 |
| TD-035 | M0-D | CI 增加 `pnpm audit` 依赖漏洞扫描 | 已采纳 | M0-F | 当前未跑 |
| TD-036 | M0-D | 用 OpenAPI 生成客户端替换手写 api-client | 已采纳 | M1 | 后端 OpenAPI 就绪后生成 |
| TD-037 | M0-D | 前端环境变量放到 `frontend/.env`，根 `.env` 不生效 | 已完成 | M0-D | c4d4dfa 新增 frontend/.env.example |
| TD-038 | M0-D | `/health` 与 `/api` 同源代理，避免 CORS | 已完成 | M0-D | c4d4dfa 已修 Major |

---

## 3. 里程碑边界评审记录

| 评审时点 | 新增条目 | 已完成 | 已采纳进入下一里程碑 | 已延后 | 已拒绝 | 结论 |
|---|---|---|---|---|---|---|
| M0-A | TD-001~006 | TD-004 | TD-005 | TD-001/002/003/006 | 无 | 通过 |
| M0-B | TD-007~014 | TD-013/014 | TD-012 | TD-007~011 | 无 | 通过 |
| M0-C | TD-015~028 | TD-027/028 | TD-016/018/025/026 | TD-015/017/019~024 | TD-021 | 通过 |
| M0-D | TD-029~038 | TD-037/038 | TD-029~031/033~036 | TD-032 | 无 | 通过 |
| M0-E | 待评审 | — | — | — | — | 待做 |
| M0-F | 待评审 | — | — | — | — | 待做 |

> M0-E 开始前先处理 `已采纳/目标 M0-E` 的条目：TD-012（MinerU）。
> M0-F 开始前处理 TD-002/005/010/029/030/031/033/034/035。
