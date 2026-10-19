# 校园陪伴精灵 — 代码审查日志（review-log.md）

> 每个功能点完成后追加一条；没有审查记录的功能点不得标记完成。
> 详细清单见 `docs/code-review-checklist.md`。

| 功能点 | 日期 | commit/diff | 审查人/agent | Blocker | Major | Minor | 修复/复测 | 结论 |
|---|---|---|---|---|---|---|---|---|
| 文档阶段（非代码） | 2026-10-19 | docs/* | 待用户确认 | 0 | 0 | 0 | N/A | 已确认 |
| M0-A 后端最小骨架 | 2026-10-19 | feature/M0-foundation | 独立 Explore review agent | 0 | 1 | 12 | Major 已修复；低成本 Minor 已修；其余记录 | 已提交 |
| M0-B Compose 依赖栈 | 2026-10-19 | feature/M0-foundation | 独立 Explore review agent | 1 | 3 | 14 | Blocker/Major 已修；关键 Minor 已修；MinerU 留 M0-E | 已提交 |
| M0-C Harness 骨架 | 2026-10-19 | feature/M0-foundation | 两轮独立审查（第二轮补做） | 0 | 8+1 | 20 | 两轮 Major 已修；关键 Minor 已修；其余记录 | 已提交 |
| M0-D 前端脚手架 | 2026-10-19 | feature/M0-foundation | 独立 Explore review agent | 0 | 1 | 7 | Major 已修；关键 Minor 已修；其余记录 | 可提交 |

### M0-A 后端最小骨架

- 日期：2026-10-19
- 范围：`backend/pyproject.toml`、`app/config.py`、`app/core/logging.py`、`app/api/health.py`、`app/main.py`、`tests/test_health.py`、`backend/README.md`
- 审查发现：
  - **Major**：健康检查挂在 `/api/v1` 下，与 spec/部署契约 `/health/live|ready` 不一致，探针会 404。已修复为根路径挂载，并新增回归测试 `test_health_not_under_api_prefix`。
  - **Minor 已修**：配置 `env_file` 锚定项目根目录；日志级别显式校验 + `force=True`；健康响应 `Literal["ok"]`；`docs/redoc` 可按环境关闭；测试 fixture 类型、完整响应断言、根路径断言；README 同步；mypy 覆盖 `app tests`。
  - **Minor 记录/后续**：uvicorn/httpx 标准库日志暂未统一 JSON（M0-B 处理）；ready 依赖检查在 M0-B 接入；版本号重复、`extra="ignore"`、`ignore_missing_imports` 全局放宽、`create_app` 工厂化留待后续收紧。
- 复测：
  - `uv run ruff check .`：通过
  - `uv run pytest`：3 passed
  - `uv run mypy app tests`：通过
  - 健康检查：`/health/live`、`/health/ready` 200；`/api/v1/health/live` 404
- 结论：可提交。

## 记录模板

```markdown
### <功能点 ID / 名称>
- 日期：
- commit/diff：
- 审查人/agent：
- 范围：
- Findings：
  - Blocker：
  - Major：
  - Minor：
- 修复：
- 复测：
  - 单元：
  - 集成：
  - 业务场景：
  - E2E：
  - AI 评估：
- 复审：
- 更好做法与 TD 处置：TD-xxx（已采纳/已延后/已拒绝/已完成）
- 结论：可提交 / 阻塞
```

### M0-B Compose 依赖栈

- 日期：2026-10-19
- 范围：`deploy/docker-compose.yml`、`deploy/.env.example`、`deploy/README.md`、`deploy/minio/Dockerfile`、`deploy/minio/README.md`、`deploy/mineru/README.md`、`deploy/initdb/README.md`、`docs/deployment.md`、`.gitignore`、根 `.env.example`
- 审查发现：
  - **Blocker**：MinIO 只有本地 tag，干净 checkout 无法启动。已修复：`deploy/minio/Dockerfile` 在构建阶段克隆官方固定 RELEASE，Compose 增加 `build`，`docker compose up -d` 可自动构建。
  - **Major**：MinIO 二进制版本未注入 ldflags，`minio --version` 显示 `DEVELOPMENT.GOGET`。已修复：注入 `Version/ReleaseTag/CommitID/ShortCommitID`；实测显示 `RELEASE.2025-10-15T17-29-55Z` 与 commit `9e49d5e7...`。
  - **Major**：`vendor/` 未被忽略，63MB 源码/嵌套 `.git` 可能入库。已修复：`.gitignore` 增加 `vendor/`。
  - **Major**：文档默认 `core+rag+parser` 与 16GB 资源上限矛盾。已修复：默认只开 core，rag/parser 分开；MinerU 3584m；移除不存在的 `--profile core`。
  - **Major**：MinerU 镜像/端点未验证。已明确标记为 M0-E 待验证，parser profile 不作为 M0-B 验收项。
  - **Minor 已修**：Postgres healthcheck 加 `-h 127.0.0.1` + `shm_size`；Qdrant 改 HTTP `/healthz` 200 校验；ClamAV `start_period` 360s；TEI `start_period` 600s 且固定模型 revision；Redis 增加 `maxmemory/policy`；根 `.env.example` 的 Redis URL/MinIO 变量命名对齐；部署文档去除 `depends_on` 漂移；补充 TEI 冒烟命令。
  - **Minor 记录/后续**：Redis 密码仍通过 argv 传入（后续用 config/secret）；MinIO 运行时复用 Go 镜像体积偏大（后续换最小镜像）；Postgres shared_buffers 未显式调优；MinerU 构建/端点 M0-E 验证。
- 复测：
  - `docker compose --profile rag config --quiet`：通过
  - 8 个服务全部 healthy：PostgreSQL/Redis/MinIO/MailHog/ClamAV/Qdrant/TEI embed/TEI rerank
  - PostgreSQL `select 1`、Redis `PONG`、MinIO `/minio/health/live` 200、Qdrant 建删集合、ClamAV 扫描 OK、MailHog SMTP 投递 1 封
  - TEI embed 返回 1024 维；TEI rerank 返回分数 0.997518
  - MinIO 以非 root 运行，版本正确
- 结论：可提交；MinerU 留 M0-E。

### M0-C Harness 骨架

- 日期：2026-10-19
- 范围：`backend/app/harness/{contracts,registry,tools,trace,audit,prompts,__init__}.py`、`backend/tests/harness/*.py`、`backend/pyproject.toml`
- 第一轮独立审查：0 Blocker / 8 Major / 14 Minor；Major 清单与修复：
  1. 工具策略可绕过 → `ToolExecutor` 强制校验 tenant_status、permission、side_effect、availability_flag、data_scope（含范围层级）。
  2. 无 schema 校验 → 用 `jsonschema` 校验 input/output schema，新增 `INVALID_ARGS`/`INVALID_RESULT`/`INVALID_SCHEMA`。
  3. spec 可变可绕过 → `ToolSpec`/`PromptSpec`/`AuditEvent` 冻结 + `extra=forbid`；注册默认拒绝写工具，`allow_write=True` 才可注册且执行层仍拒绝。
  4. trace/audit 可选 → `ToolExecutor` 构造参数改为必需；测试显式注入。
  5. `TenantContext` 缺状态/KB 版本 → 增加 `tenant_status`、`kb_version_ids`。
  6. 审计链可重算/截断/重放 → HMAC-SHA256 + 序号 + event_id + 深拷贝 + `head_hash`/`count` + `verify(expected_head, expected_count)`；测试覆盖篡改、重放、截断。
  7. 脱敏不完整、trace 未脱敏 → 扩展敏感 key 与值正则（邮箱/手机/证件/Bearer），trace span attributes 写入前脱敏。
  8. 测试缺口 → 扩展到 41 个用例，覆盖权限/写工具/可用性/租户状态/数据范围/schema/超时/异常/取消/缺失工具/审计篡改/重放/截断/脱敏/Prompt 严格变量/CRLF。
- 第二轮独立复审（余额恢复后补做）：8 个 Major 中 7 项已实质修复；新增 **F1 Major** 与 F2/F3 Minor。已修复：
  - **F1（Major）**：`InMemoryTraceSink.write_turn` 未脱敏嵌套 `spans[].attributes`，存在明文旁路。已修复为对嵌套 span attributes 递归脱敏，并新增断言。
  - **F2**：handler 返回非 `ToolResult` 时降级为 `TOOL_ERROR`；input/output schema 解析异常（含 `$ref`）统一降级为 `INVALID_SCHEMA`；两者都保证 trace/audit 落盘。
  - **F3**：`ToolRegistry.register` 用 `jsonschema.Draft202012Validator.check_schema` 校验 schema 合法性，非法直接 `ValueError`。
  - **F5**：`ToolRegistry.available`/`CapabilityRegistry.available` 增加 tenant_status 与 data_scope 过滤。
  - **F8**：`AuditEvent` 增加 `trace_id`，工具审计与 trace 建立关联。
  - **F9**：`PromptSpec` 增加 `sha256` 与 template 一致性校验。
  - **F10**：脱敏补充 15 位证件号、带空格手机号，以及 credential/session/otp/verify_code/student_no/employee_no 等 key。
- 剩余 Minor（M0-C 不阻塞，留后续）：
  - F4：`audit_level`/`idempotent`/`redacted_summary` 尚未被消费，声明与执行仍有漂移。
  - F6：`input_schema`/`output_schema` 默认空，未强制只读工具显式声明。
  - F7：`frozen=True` 对嵌套 dict（如 `feature_flags`）无效，安全上下文需改不可变映射。
  - F11：`AuditSink`/`TraceSink` 为同步接口，接 DB 时需评估 async/worker。
  - `verify()` 空链返回 True；生产需外部锚点/WORM 配合。
  - `PromptRegistry.versions` 按字符串排序；`E501` 仍 ignore；handler 自抛 TimeoutError 与 wait_for 超时未区分；trace/audit sink 故障未隔离；`CapabilitySpec.permission` 默认公开；`AgentRequest` 内嵌 TenantContext 需认证层注入；审计/trace 仍为内存实现。
- 复测：
  - `uv run pytest`：48 passed
  - `uv run ruff check .`：通过
  - `uv run mypy app tests`：通过
- 更好做法与 TD 处置：TD-015~TD-028 已登记 `docs/tech-debt.md`，其中 TD-027/028 已完成，TD-016/018/025/026 已采纳，TD-021 已拒绝（理由见台账），其余延后。
- 结论：两轮审查的 Major 均已修复，可提交；进入 M0-D 时把剩余 Minor 纳入技术债清单。

### M0-D 前端脚手架

- 日期：2026-10-19
- 范围：`frontend/**`（pnpm workspace、apps/admin、apps/campus、packages/api-client/ui/config、Playwright 配置与 e2e）、根 `.env.example` 前端段
- 审查发现：
  - **Major**：前端直连 `127.0.0.1:8000/health/live`，而后端无 CORS、Vite 代理只含 `/api`，浏览器下健康检查必然失败。已修复：Vite 代理增加 `/health`；前端默认 `VITE_API_BASE_URL` 留空走同源相对路径；新增 `frontend/.env.example`；根 `.env.example` 说明 Vite 不读根 `.env`。
  - **Minor 已修**：api-client 规范化尾斜杠 + 5s 超时；Element Plus 配置 zh-CN locale；Playwright `reuseExistingServer` 改为 `!CI`；README 代理/环境变量说明。
  - **Minor 记录/后续**：缺 ESLint/Prettier/lint 脚本；缺 Vitest；Element Plus 全量引入导致 admin JS 1.03MB、CSS 361KB；Vant 全量 CSS；e2e 只有静态渲染断言、无失败态/接口 mock；`playwright.config.ts`/`e2e` 未纳入 tsconfig；未跑 pnpm audit。
- 复测：
  - `pnpm install`：成功（esbuild 构建已批准）
  - `pnpm build`：admin/campus 均成功
  - `pnpm typecheck`：通过
  - `pnpm test:e2e`：2 passed（admin + campus，系统 Chrome + vite preview）
  - 代理行为：`/health` 转发到 127.0.0.1:8000；后端未启动时页面显示“后端未连接”，冒烟不受影响
- 更好做法与 TD 处置：TD-029~TD-038 已登记 `docs/tech-debt.md`，其中 TD-037/038 已完成，TD-029~031/033~036 已采纳，TD-032 延后。
- 结论：Major 已修，可提交；剩余 Minor 记入 M0-F/M1 技术债。

## 高风险功能复审记录

| 功能点 | 复审人 | 日期 | 结论 |
|---|---|---|---|
| M1 认证/令牌 | 用户 | 待定 | 待定 |
| M1 租户隔离/RLS | 用户 | 待定 | 待定 |
| M3 KB 审批/发布 | 用户 | 待定 | 待定 |
| M4 AI 护栏/SSE | 用户 | 待定 | 待定 |
| M5 账号删除状态机 | 用户 | 待定 | 待定 |
| M6 配置双人确认 | 用户 | 待定 | 待定 |
| M6 Langfuse 外送/脱敏 | 用户 | 待定 | 待定 |
