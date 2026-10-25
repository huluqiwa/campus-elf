# 校园陪伴精灵（Campus Companion Elf）

面向高校的多租户 SaaS 平台，提供 AI 陪伴、校园知识库、校务审批、消息通知、轻社交与心理关怀辅助能力。

## 当前状态

**当前阶段：M2 组织与人员进行中（ORG-01/03、ORG-02 导入、ORG-05 部门树、ORG-04 身份证加密已交付；简报见 `docs/design-briefs/`）。** 交接入口：**`docs/handoff.md`（接手手册：状态表/环境/门禁/文档地图）**，规则源头：`AGENTS.md`。

已完成：

- **M0 基座**（M0-A~M0-F）：Git/后端骨架/健康检查、Compose 依赖栈（PG 16 / Redis 7.4 / MinIO / MailHog / ClamAV / Qdrant 1.19.2 / TEI 1.9.4）、Harness 骨架、前端脚手架、兼容性与外部组件验证（MinerU 4.0.12 三档真实解析、Langfuse 4.63.0 自托管冒烟、Arq 3.13 兼容）、独立测试栈与 4 条 GitHub Actions、CPU 嵌入/重排基准。验收报告：`docs/m0-acceptance.md`。
- **M1-A 租户基座**：13 张表 + 复合外键、RLS（FORCE + 按角色策略 + fail-closed）、三连接与双应用角色分离、仓储/请求上下文/中间件、审计仅追加；两轮独立审查通过。
- **M1-B 学校注册与平台审核（TEN-01/02/03）**：公开注册（multipart 资质凭证）、平台审核与原子开通（`SECURITY DEFINER` 审核函数）、学校编号、超管账号与初始密码邮件、审核记录与审计；五轮独立审查通过（含跨租后门、TEMP 表遮蔽、PUBLIC 授权等修复）。
- **M1-C 三端登录与令牌后端（AUTH-01/02）**：JWT 15 分钟 + 刷新令牌 7 天轮换、重用检测撤销家族、登出/改密即时失效、realm↔数据库连接强制绑定、首登改密门禁、平台审核改真实账号鉴权（删除开发令牌）、注册提交收进函数、过期令牌清理任务；三轮独立审查通过。
- **M1-C-UI 三端登录页与强制改密页**：admin（身份切换）+ campus 登录页、强制改密页、路由守卫、HttpOnly 刷新 Cookie（生产强制 Secure / 不回显 refresh / 禁 SameSite=none）、统一 401/403 处理；两轮独立审查通过。前端 20 单测 + 12 Playwright 全绿。
- **M1-C2 会话设备管理（AUTH-06，策略 C）**：访问令牌 `sid` + Redis 会话标记实现被踢即时失效；租户 3 / 平台 2 台设备上限、超限踢最久未用；设备列表、单台/退出其它设备；跨用户/跨租统一 404 并写 `auth.session.revoke_denied`；登出按 family 去重审计、Redis 故障 best-effort；五轮独立审查通过（迁移 `d4e6f8a0b2c1`）。
- **M1-D1 密码与锁定（AUTH-03 员工/平台、AUTH-05）**：密码策略（10~128 字符且 ≤72 UTF-8 字节、3 类字符、不含登录名）、最近 5 次历史、bcrypt cost 12；5 次失败锁 15 分钟；员工/平台邮件重置（30 分钟单次令牌、fragment 链接、申请/确认 IP 限流、后台发信、并发单次消费）；平台账号用 `python -m app.platform_admin` 初始化/维护邮箱；admin/campus 忘记密码与重置页；迁移 `b2d4f6a8c0e1`。学生校管重置按计划 M2 交付。
- **M1-D2 两步验证（AUTH-04）**：TOTP + 10 枚一次性恢复码；登录改为「密码 → 动态码 → 首登改密」三态（未过第二步不签发任何令牌或 Cookie）；强制策略仅平台运营账号（`MFA_REQUIRE_PLATFORM`，默认关闭）；TOTP 密钥以 AES-256-GCM + realm/user 绑定密文入库，恢复码只存 HMAC 摘要；防重放水印 + 单挑战尝试上限 + 账号锁定；启用/关闭 MFA 轮换全部会话；admin 端绑定页（二维码本地生成）与安全设置页、双端两步验证登录；迁移 `c4d6e8f0a2b3`；两轮独立审查通过（第二轮 0 Blocker / 0 Major）。学生自助绑定按计划 M2 随校管后台交付。
- **M1-E1 鉴权内核（RBAC-01 部分）**：权限矩阵 v1.2 的 59 个权限码（platform 12 / school 34 / campus 9 / observability 2 / eval 2）与 8 个内置角色出厂默认落库，删除 9 个无引用旧码；`app/rbac` 提供权限目录、realm 单一口径（`TENANT_MODULES`/`code_realm()`）、幂等同步（只补不撤、不覆盖运营改动）与路由清单守卫；迁移 `e5f7a9c1b3d5` 建代码持有的定义表与 `seed_builtin_roles(uuid)`（SECURITY DEFINER、EXECUTE 已收回），两条开通路径同源，并补齐 C 端 `student`/`teacher`；`require_permission(code, realm=)` 先判 realm 再判权限，接入 `campus.chat.use`、`platform.tenant.view`、`platform.tenant.review`，拒绝写 `auth.permission.denied`（只带 path+reason）；权限增删下一次请求即生效且不作废在线会话。矩阵 §3/§4 由测试解析并逐列比对，改一格 √/△ 即有用例变红。数据范围服务端过滤与审批职责分离见下一条 M1-E2。

- **M1-E2 数据范围与职责分离（RBAC-02/03）**：`school`/`self` 数据范围出厂默认落库（`rbac_builtin_role_scope_defaults`），`Principal` 每请求解析、仓储基类统一注入谓词，解析不到范围 = 空集（fail-closed），显式 id 越权 → 404 + `data_scope.denied` 审计；新增 `GET /api/v1/school/personnel`(+详情)（`school.personnel.view`，响应不含 `login_id`）；提交人不能自批的三道锁（应用层 403/422、数据库函数、同表三条款 CHECK）+ `system_settings` 例外开关（`platform.config.edit`、必填理由、审计、热生效）；平台跨租只读最小通道（只读事务 + 单租户 + 必填理由 + 审计失败即拒绝，工单化延后 TD-153）；`role_permissions_realm_guard` 收紧到 `TENANT_MODULES`；迁移 `f7a9b1c3d5e8`。
- **M1-F 审计哈希链（AUD-01）**：审计仅追加升级为跨表单链（触发器计算 `prev_hash`/`hash`，密钥经 GUC 注入不入日志）、哈希链查询接口与 `backend/scripts/verify_audit_chain.py` 校验脚本、应用启动自检；TD-068/091 收口。
- **M1 验收报告（含 TD-110 收口）**：`docs/m1-acceptance.md`。专用 compose 栈（`deploy/backend/Dockerfile` + `deploy/docker-compose.e2e.yml` + `scripts/e2e-stack.sh`）跑真实后端全栈 E2E：`frontend/e2e-fullstack/` **12 例全绿**（三端真实登录、首登强制改密、MailHog 真实邮件重置、平台 tenants/audit 业务接口、数据范围、越权 403、会话设备被踢 401）；边界技术债台账 171 条全部有处置（169 既有 + 本次新增 TD-170/171），TD-147 部署注意已写入 handoff §5.1。
- **M2 组织与人员（后端）**：人员档案与组织关系（教师/学生建档、班级/课程/任教、`class` 数据范围 + cursor 分页，迁移 `c9d5f1e3a7b9`）；CSV/XLSX 异步导入（Arq 任务、行级错误报告、失败行重试 + 看门狗，迁移 `e2a6c8d4b0f2`）；部门/科室树（id 物化路径、子树移动、教师归属 + 调岗审计，迁移 `a4c8e2f6b1d3`）；身份证加密采集与受控查看（AES-256-GCM 三件套 + 同租 HMAC 查重唯一索引 + 掩码列表 + `sensitive.read` 理由查看，迁移 `f8b2d6a0c4e8`）。B 端页面待做。

当前门禁（M2-ORG04 时点，2026-10-25）：后端 **637 passed**、前端 **32 单测 + 28 mock E2E + 12 全栈 E2E（TD-110）**、`ruff`/`mypy`/`alembic check`/`bandit` 本地全绿、迁移 head `f8b2d6a0c4e8`（无漂移）；M1 全部功能点与 M2-ORG01/03、ORG-02、ORG-05、ORG-04 均已交付并通过独立审查（远端 CI backend / security）。

进行中/待做：

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
