# M1-acceptance — M1 验收报告 + TD-110 全栈 E2E 实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-24 |
| 状态 | 已确认（2026-10-24，方案 A / MFA 不纳入 / 两笔 commit） |
| 关联功能点 | M1 验收报告（milestone §4.4 退出标准 + §12 DoD）；TD-110 收口 |
| 验收引用 | docs/milestone.md §4.4、§12；docs/tech-debt.md TD-110、TD-147 |

## 1. 目标

1. **TD-110 收口**：用专用 compose 栈（后端容器 + 测试依赖库 + 前端 preview）跑真实全栈 E2E，覆盖「登录 → 改密 → 业务接口」的真实链路（浏览器 → vite preview 代理 → 真实 FastAPI 容器 → 真实 PG/Redis/MailHog），断言业务结果而非 HTTP 200。
2. **M1 验收报告** `docs/m1-acceptance.md`：逐条核对 milestone §4.4 退出标准并给证据（复用 m0-acceptance.md 格式）。
3. **M1 边界技术债评审**（milestone §12 DoD）：核对 `docs/tech-debt.md` 全部条目处置，产出处置汇总表。

**不做**：不改任何后端业务代码与既有接口；不把现有 28 个 mock E2E 改造成真实链路；不把全栈 E2E 接入远端 CI（登记 TD，M6 决策）。

## 2. 候选方案

### 方案 A（推荐）：测试栈加 e2e profile 的后端容器 + 独立 Playwright fullstack 配置

- 做法：
  - 新增 `deploy/backend/Dockerfile`（python:3.13-slim + uv `--frozen` 同步、非 root 运行、CMD uvicorn），本验收期仅本地构建使用。
  - 专用于全栈 E2E 的 compose 编排（见 §9 实现结果：最终落为独立 `deploy/docker-compose.e2e.yml`，经 external 网络加入 campus-elf-test-net；原计划在 test 文件加 profile，但 `:?` 必填变量会让 `test-stack.sh` 的所有 compose 调用在插值阶段失败，故拆分），backend 对外发布 `127.0.0.1:8000`；凭据复用 `deploy/.env.test`（git 忽略），E2E 专用 `JWT_SECRET`/`ENCRYPTION_KEK` 由脚本随机生成追加到该文件，不入库。
  - 新增 `scripts/e2e-stack.sh`：`up`（复用/确保 campus-elf-test 依赖栈 healthy → 按 handoff 坑 #4 文档化流程重置测试库：`drop schema public cascade` → 容器内 `alembic upgrade head` → 容器内 seed）、`down`、`status`；连接串在容器内用服务名、宿主机视角不变。**不 `down -v`**，避免动正在运行的测试栈（数据在 tmpfs，重置无损失）。
  - 新增 `backend/scripts/e2e_seed.py`：复用业务测试同款服务层函数造确定性数据——平台超管、1 个已开通租户（学校编号）、校方超管（`must_change_password=true`）、教师、学生；口令经环境变量传入（测试域固定值，不写死在脚本常量、不入 git）。
  - 前端：沿用 `pnpm --filter admin/campus preview`（proxy 已指向 127.0.0.1:8000，零改动）；新增 `frontend/playwright.fullstack.config.ts` 与 `frontend/e2e/fullstack/*.spec.ts`（不引入 auth-mock，`pnpm test:e2e:fullstack` 单独跑）。
- 优点：完全符合 TD-110 处置原文（compose 后端 + 测试库 + preview）；顺带完成生产容器化启动路径的首次真实演练（M6 部署前置）；既有 mock E2E 与后端代码零改动；数据 tmpfs 用后即弃。
- 缺点/成本：新增镜像 + compose 服务 + seed 脚本 + 新 Playwright 配置约 5 个文件；本机需构建 Python 镜像。
- 风险：宿主 8000 端口与手动 dev 后端冲突（脚本跑前预检）；容器内/外连接串差异（脚本统一翻译）；Windows 下 Chrome 多 context 资源（workers=2 已与 CI 对齐）。
- 内存评估（2026-10-24 实测，Docker Desktop 11GB）：campus-elf-test 依赖栈实际约 220MB；新增 backend 容器限额 512m 封顶；宿主 Chrome ~1–1.5GB。总增量 <2GB；同时运行 dev 栈（约 5.8GB，TEI/ClamAV/MinerU 为主，M1 验收用不到）也可容纳，执行期间可选择性停 dev 栈腾余量。
- 对 spec/design 的影响：无业务变更；`docs/deployment.md` 增补「后端镜像与 E2E 栈」一节。

### 方案 B：不进容器，宿主机直起 uvicorn 连测试栈

- 做法：`DATABASE_URL` 等指向测试栈，`uv run uvicorn` 起 8000，其余同 A。
- 优点：最快，零镜像构建。
- 缺点：不符合 TD-110 处置承诺（明文要求「专用 compose 栈（后端 + …）」）；未验证容器路径，部署风险后移；验收证据打折扣。
- 风险：验收报告被质疑「全栈不真」。

### 方案 C：把 28 个既有 mock E2E 全部改造为真实后端

- 做法：删除 auth-mock，全部 spec 直连真实后端，远端 CI 起依赖栈。
- 优点：覆盖最大化，一步到位。
- 缺点：范围爆炸，违背「单点推进」；MFA/邮件时序依赖重；远端 frontend workflow 必须起全套栈，CI 时长与脆弱性大增。
- 风险：验收本身失控，M2 被阻塞。

## 3. 选择结论

- 选择：**方案 A**。
- 理由：TD-110 原文即此方案；成本可控（新增均为测试基础设施，无生产代码改动）；同时提前验证容器化启动路径，为 M6 部署铺路。
- 为什么不选其他方案：B 不满足处置承诺且证据弱；C 范围失控。
- 是否需要改 spec/design：不需要；`docs/deployment.md` 增补一节（记录性）。

## 4. 实现顺序与 TDD 测试清单

1. 基础设施：`deploy/backend/Dockerfile` → compose `backend` 服务 → `scripts/e2e-stack.sh`（up/migrate/seed/down/status）。
2. 种子数据：`backend/scripts/e2e_seed.py`（服务层复用；TD-147 前置项：本步同时把 RBAC 回滚约束写入 `docs/handoff.md` 部署注意）。
3. 前端 E2E：`playwright.fullstack.config.ts` → `e2e/fullstack/` specs → `pnpm test:e2e:fullstack`。
4. 报告与回写：`docs/m1-acceptance.md` → handoff §1 / README 当前状态 / tech-debt / review-log / deployment.md。

红→绿→重构测试清单：

- [ ] E2E（新 fullstack，约 12 例，均为真实链路）：
  - 三端登录：平台超管 / 校方超管 / 教师 各自真实登录成功；错误口令显示错误且不签发会话。
  - 首登强制改密：校方超管（seed `must_change_password=true`）登录被重定向改密页 → 真实改密 → 旧口令登录失败、新口令成功。
  - 忘记密码真实邮件链路：发起重置 → 从 MailHog API（58025）取真实邮件提取令牌 → 重置成功 → 新口令登录。
  - 会话设备管理（Security UI）：设备列表真实展示；下线其它设备后，另一 browser context 的会话下一请求 401 被踢回登录页。
  - 业务接口（真实会话调用）：校方超管调 school_personnel 列表（数据范围生效）；平台超管调 `/platform/tenants`、`/platform/audit` 列表，且审计中可见本次登录/改密事件（AUD-01 真实可查）；教师调用平台接口 403（跨权限拒绝）。
- [ ] 既有 28 个 mock E2E 回归通过（确认无回归）。
- [ ] 后端全量 561 passed 复跑确认基线（无代码改动，属验收证据）。
- [ ] ruff/mypy：新增 `backend/scripts/e2e_seed.py` 纳入检查；alembic check 无漂移。

## 5. 测试策略

| 层级 | 内容 |
|---|---|
| 单元 | 无新增（无业务逻辑；seed 走服务层） |
| 集成/业务场景 | 后端全量复跑（561 passed）作为验收证据，不断言逻辑变更 |
| E2E | 新 fullstack 约 12 例（见 §4）；断言业务结果：设备列表变化、被踢 401、MailHog 收到真实邮件、审计 API 可查事件、403 拒绝——不以 HTTP 200 了事 |
| AI 评估 | 不涉及 |
| 性能 | 不涉及（M1 无 k6 要求） |

## 6. 预判技术债

| TD 编号 | 条目 | 处置 |
|---|---|---|
| TD-110 | E2E 用接口 mock，未跑真实后端全栈链路 | 本功能点**已完成**（收口） |
| TD-147 | RBAC 回滚仅承诺结构可来回，角色映射需备份 | 验收前把约束写入 `docs/handoff.md` 部署注意（本功能点前置项） |
| TD-NEW-1 | 全栈 E2E 未纳入远端 CI（仅验收期本机执行） | 已采纳 → M6（随部署硬化决策是否入 CI） |
| TD-NEW-2 | 后端 Dockerfile 首版仅服务 E2E，生产硬化（镜像审计、healthcheck、资源限额）未做 | 已采纳 → M6 |
| TD-NEW-3（如有） | seed 脚本 E2E 专用、非通用 fixture | 执行时按实际情况登记 |

## 7. 高风险确认

- 是否属于高风险功能：**否**（不改认证/租户/权限生产代码，纯测试基础设施 + 文档）。
- 需要用户确认的点：
  1. 方案 A（compose 后端容器 + 独立 fullstack Playwright 配置）——内存评估见 §2，11GB 可承载；
  2. fullstack E2E 范围：MFA 真实 enroll/verify 链路**不纳入**（用户已确认 2026-10-24）；
  3. 提交拆分：两笔 commit（用户已确认 2026-10-24）；
  4. TD-147 前置项：按台账原文把 RBAC 回滚约束写入 `docs/handoff.md` 部署注意。
- 确认结论：2、3 已确认；方案 A 待用户对内存评估确认后开工。

## 8. 审查对照

- 审查时重点验证：`.env.test`/`JWT_SECRET`/`ENCRYPTION_KEK` 不入库不回显；compose 凭据仅经环境文件引用；seed 脚本无明文口令硬编码、无后门；E2E 断言业务结果；零生产代码路径改动；报告证据与实际执行一致。
- 允许的偏差：端口选择、seed 账号数量、用例数 ±3 以内（在汇报中说明）。

## 9. 实现结果

**基础设施（与 §2 方案 A 的偏差已标注）：**

- `deploy/backend/Dockerfile`：python:3.13-slim + uv 0.11.17 `--frozen`（105 包）、非 root、uvicorn 启动；`backend/.dockerignore` 排除 .venv/tests。
- **偏差**：backend 未塞进 `docker-compose.test.yml`（`:?` 必填变量会让 `test-stack.sh` 的所有 compose 调用在插值阶段失败），改为独立 `deploy/docker-compose.e2e.yml`（project `campus-elf-e2e`），经 external 网络 `campus-elf-test-net` 复用依赖栈；发布 127.0.0.1:8000；`PASSWORD_RESET_URL_BASE` 指向 admin preview。
- `scripts/e2e-stack.sh`：up（清 e2e 容器 → 重置测试库/Redis → 迁移 → backend → seed）｜down｜status；E2E_JWT_SECRET/E2E_ENCRYPTION_KEK 首次随机生成入 `.env.test`（git 忽略）。
- `backend/scripts/e2e_seed.py`：平台账号直写 + 真实 HTTP 注册→审核→开通 + 服务层造 6 个租户账号（强制改密超管/运营/教师/学生/重置专用/设备专用，避免设备限额互踩）+ MailHog 清空与初始密码提取 + 登录烟测 fail-fast；产出 `deploy/.e2e-accounts.json`（git 忽略）。
- `frontend/playwright.fullstack.config.ts` + `e2e-fullstack/`（helpers + 5 spec 文件 12 例）+ `pnpm test:e2e:fullstack`。

**执行记录：**

- `bash scripts/e2e-stack.sh up`：全绿（重置→迁移 head=`a8c3e9f1b5d7`→backend healthy→seed 完成，school_code=SCH-M35Z3HZ2）。
- `pnpm test:e2e:fullstack`：首轮 9/12（3 处缺陷，见下）；修复后 **12/12 passed（29.4s）**。
- 回归：mock E2E **28 passed**（47.8s）；后端全量 **572 passed**（exit 0）；前端 lint/format/typecheck/32 单测全绿；`alembic check` 无漂移；ruff/mypy 通过（e2e_seed.py）。
- 过程缺陷与修复：① 学生新口令含登录名被密码策略拒 → 改口令；② MailHog 正文 MIME 编码 → TS 侧解码（对齐 mailhog.py）；③ `/auth/sessions` 字段为 `sessions` → 修断言；④ 编排层：compose v2 无 `inspect` 子命令（改用 `ps --format`）、重跑需先清自己的 e2e 容器（8000 端口预检）、alembic 不能在事件循环内调用（迁移挪到循环外）。

**文档产物**：`docs/m1-acceptance.md`（验收报告）、TD-110→已完成、TD-170/171 登记（M6）、TD-147 前置项写入 handoff §5.1、handoff §1 与 README 状态更新。

**审查**：独立审查完成——验收审查 0B/1M/9M 全部修复复测（结论「可提交」）；M1-F 第三轮复审 1 Major（verify_chain 负路径测试缺失）修复后复审通过。详见 `docs/review-log.md`（2026-10-24 两条记录）。
