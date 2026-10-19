# 校园陪伴精灵（Campus Companion Elf）

面向高校的多租户 SaaS 平台，提供 AI 陪伴、校园知识库、校务审批、消息通知、轻社交与心理关怀辅助能力。

## 当前状态

**当前阶段：M0 进行中。**

已完成：

- 需求对齐与逐项澄清（A1–A29、H1–H4、S1–S2、R1–R8、L1–L6、K1–K3）
- 第二阶段文档
- 配置模板与 Git 忽略规则
- 文档确认
- **M0-A：Git 初始化、Python 3.13 后端最小骨架、健康检查、测试、代码审查**
- **M0-B：Docker Compose core/rag 依赖栈**——PostgreSQL 16、Redis 7.4、MinIO（源码构建固定 RELEASE）、MailHog、ClamAV、Qdrant 1.19.2、TEI 1.9.4 embed/rerank 全部 healthy；TEI 返回 1024 维向量与重排分数。
- **M0-C：Harness 骨架**——契约、能力/工具注册表、工具执行器（租户/权限/只读/可用性/数据范围/schema/超时）、trace 脱敏、HMAC 审计链、版本化 Prompt 注册表；48 个测试全绿，两轮独立审查 Major 已修复。

- **M0-D：前端脚手架**——pnpm workspace、apps/admin（Element Plus）、apps/campus（Vant 4）、packages/api-client/ui/config；`pnpm build`/`typecheck` 通过，Playwright 冒烟 2 passed。

待完成：MinerU/Langfuse/Arq 兼容性验证、CPU 嵌入/重排基准、M0 验收报告。

## 技术栈（已锁定）

- Python 3.13.x、FastAPI、SQLAlchemy 2 async、Alembic
- LangGraph + 自定义 Harness（LangGraph 只做图执行）
- PostgreSQL 16、Qdrant、Redis 7.4、MinIO
- MinerU 4.0.12（Standard → Basic → Flash 自动降级）
- BGE-M3 dense + jieba/BM25 sparse + bge-reranker-v2-m3（TEI CPU）
- Vue 3 + TypeScript + Vite；管理端 Element Plus，C 端 Vant 4（移动端 H5）
- Langfuse Server 4.63.0 + Python SDK 5.0.2（生产独立主机自托管，开发可选）
- Docker Compose + Nginx + HTTPS + GitHub Actions

## 文档索引

| 文档 | 内容 |
|---|---|
| `docs/requirements.md` | 整理后的需求基线、编号、矛盾标注 |
| `docs/spec.md` | 功能点、Given/When/Then 验收标准、决策记录、留存矩阵 |
| `docs/design.md` | 架构、API/SSE 契约、数据模型、RBAC、状态机、隔离、RAG、Langfuse |
| `docs/milestone.md` | M0–M6 里程碑、范围、风险、DoD |
| `docs/test-plan.md` | 测试分层、业务场景矩阵、A23 隔离场景 |
| `docs/state-machines.md` | 知识库、请假、账号删除、审批、消息等状态机 |
| `docs/rbac-matrix.md` | 角色权限矩阵与数据范围 |
| `docs/tenant-isolation.md` | 多租户隔离规范与测试 |
| `docs/compliance.md` | 数据留存、PII、删除证明、Langfuse 合规 |
| `docs/observability-langfuse.md` | Langfuse 部署、采集场景、脱敏、Prompt 治理 |
| `docs/deployment.md` | 环境、Compose、Nginx、HTTPS、备份、Langfuse 独立主机 |
| `docs/code-review-checklist.md` | 代码审查清单与分级 |
| `docs/review-log.md` | 功能点审查记录模板 |
| `docs/tech-debt.md` | 更好做法/技术债台账与闭环处置 |
| `docs/design-briefs/` | 实现前设计简报（候选方案、选择理由、测试策略） |
| `evals/` | LLM 评估集、rubric、runner 设计 |
| `benchmarks/` | k6/locust 压测脚本与报告规范 |

## 本地环境（已核对）

- Windows 11，16 核 CPU / 16GB 内存 / RTX 4050 Laptop 6GB
- WSL2 + Docker Desktop（当前 Docker 引擎需启动）
- 默认 Python 3.11.9，使用 uv 安装并锁定 Python 3.13.x
- Node 24.11 / pnpm 11 可用
- Playwright 未安装，M0 安装

## 安全红线

- `.env` 已有真实模型密钥，**绝对不要提交**；仓库只提交 `.env.example`。
- 禁止提交 API Key、密码、私钥、数据库连接串。
- 禁止 `git add .` / `git add -A`；提交前必须 `git status` + `git diff --staged`。
- 禁止向共享分支 force push、直接 push；禁止 `git reset --hard`、`git clean -fd`。
- 生产环境 `DEV_SKIP_SCHOOL_REVIEW`、`DEV_SKIP_FILE_SCAN` 必须为 `false`。

## 下一步

1. 用户确认 `docs/` 下的第二阶段文档。
2. 确认后进入 M0：仓库初始化、Compose core、FastAPI/Worker 骨架、Harness contracts、MinerU/Langfuse 兼容性验证、测试基础设施。
3. 按 M0 → M1→ … 顺序逐功能点 TDD；每个功能点执行“测试 → 代码审查 → 修复 → 复测 → 复审 → 提交”。
