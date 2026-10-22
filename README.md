# 校园陪伴精灵（Campus Companion Elf）

面向高校的多租户 SaaS 平台，提供 AI 陪伴、校园知识库、校务审批、消息通知、轻社交与心理关怀辅助能力。

## 当前状态

**当前阶段：M1 租户与身份进行中。** 交接入口：**`docs/handoff.md`（接手手册：状态表/环境/门禁/文档地图）**，规则源头：`AGENTS.md`。

已完成：

- **M0 基座**（M0-A~M0-F）：Git/后端骨架/健康检查、Compose 依赖栈（PG 16 / Redis 7.4 / MinIO / MailHog / ClamAV / Qdrant 1.19.2 / TEI 1.9.4）、Harness 骨架、前端脚手架、兼容性与外部组件验证（MinerU 4.0.12 三档真实解析、Langfuse 4.63.0 自托管冒烟、Arq 3.13 兼容）、独立测试栈与 4 条 GitHub Actions、CPU 嵌入/重排基准。验收报告：`docs/m0-acceptance.md`。
- **M1-A 租户基座**：13 张表 + 复合外键、RLS（FORCE + 按角色策略 + fail-closed）、三连接与双应用角色分离、仓储/请求上下文/中间件、审计仅追加；两轮独立审查通过。
- **M1-B 学校注册与平台审核（TEN-01/02/03）**：公开注册（multipart 资质凭证）、平台审核与原子开通（`SECURITY DEFINER` 审核函数）、学校编号、超管账号与初始密码邮件、审核记录与审计；五轮独立审查通过（含跨租后门、TEMP 表遮蔽、PUBLIC 授权等修复）。
- **M1-C 三端登录与令牌后端（AUTH-01/02）**：JWT 15 分钟 + 刷新令牌 7 天轮换、重用检测撤销家族、登出/改密即时失效、realm↔数据库连接强制绑定、首登改密门禁、平台审核改真实账号鉴权（删除开发令牌）、注册提交收进函数、过期令牌清理任务；三轮独立审查通过。
- **M1-C-UI 三端登录页与强制改密页**：admin（身份切换）+ campus 登录页、强制改密页、路由守卫、HttpOnly 刷新 Cookie（生产强制 Secure / 不回显 refresh / 禁 SameSite=none）、统一 401/403 处理；两轮独立审查通过。前端 20 单测 + 12 Playwright 全绿。
- **M1-C2 会话设备管理（AUTH-06，策略 C）**：访问令牌 `sid` + Redis 会话标记实现被踢即时失效；租户 3 / 平台 2 台设备上限、超限踢最久未用；设备列表、单台/退出其它设备；跨用户/跨租统一 404 并写 `auth.session.revoke_denied`；登出按 family 去重审计、Redis 故障 best-effort；五轮独立审查通过（迁移 `d4e6f8a0b2c1`）。
- **M1-D1 密码与锁定（AUTH-03 员工/平台、AUTH-05）**：密码策略（10~128 字符且 ≤72 UTF-8 字节、3 类字符、不含登录名）、最近 5 次历史、bcrypt cost 12；5 次失败锁 15 分钟；员工/平台邮件重置（30 分钟单次令牌、fragment 链接、申请/确认 IP 限流、后台发信、并发单次消费）；平台账号用 `python -m app.platform_admin` 初始化/维护邮箱；admin/campus 忘记密码与重置页；迁移 `b2d4f6a8c0e1`。学生校管重置按计划 M2 交付。
- **M1-D2 两步验证（AUTH-04）**：TOTP + 10 枚一次性恢复码；登录改为「密码 → 动态码 → 首登改密」三态（未过第二步不签发任何令牌或 Cookie）；强制策略仅平台运营账号（`MFA_REQUIRE_PLATFORM`，默认关闭）；TOTP 密钥以 AES-256-GCM + realm/user 绑定密文入库，恢复码只存 HMAC 摘要；防重放水印 + 单挑战尝试上限 + 账号锁定；启用/关闭 MFA 轮换全部会话；admin 端绑定页（二维码本地生成）与安全设置页、双端两步验证登录；迁移 `c4d6e8f0a2b3`；两轮独立审查通过（第二轮 0 Blocker / 0 Major）。学生自助绑定按计划 M2 随校管后台交付。

当前门禁：后端 **411 passed**、前端 **32 单测 + 28 E2E 全绿**、`ruff`/`mypy`/`alembic check` 全绿；M1-D1 已通过第五轮独立复审与远端 CI，M1-D2 待推送后回填远端 CI 结果。

进行中/待做：

- **M1-E** RBAC 与数据范围、**M1-F** 审计哈希链；随后出 **M1 验收报告**（含真实后端全栈 E2E，TD-110）。
- **M2/M3/M4** 未开始；其中 SC-01 变更已把 ORG-05（部门/科室）、KB-09（文档查阅人群 ACL）、KB-10（切块编辑与派生重建）、AI-10（身份→人群过滤）写入计划。

## 技术栈（已锁定）

- Python 3.13.x、FastAPI、SQLAlchemy 2 async、Alembic
- LangGraph + 自定义 Harness（LangGraph 只做图执行）
- PostgreSQL 16、Qdrant、Redis 7.4、MinIO
- MinerU 4.0.12（实际 tier 写入构建元数据；Standard→Basic→Flash 降级链在 M3 执行器落地）
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
