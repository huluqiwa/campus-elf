# M1 验收报告（租户与身份）

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-24 |
| 范围 | M1-A ~ M1-F（租户基座、注册审核、三端登录、登录 UI、会话设备、密码/锁定、MFA、RBAC 内核与数据范围、审计哈希链）+ TD-110 全栈 E2E 收口 |
| 结论 | **M1 全部退出标准达成**；TD-110 已按处置承诺以专用 compose 栈跑通真实后端全栈 E2E；边界技术债台账 171 条全部有处置（169 既有 + 本次新增 TD-170/171），无阻塞项进入 M2 |
| 依据 | `docs/milestone.md` §4.2/§4.4/§12、`docs/tech-debt.md` TD-110/TD-147、`docs/review-log.md`、`docs/handoff.md` §1、各功能点 `docs/design-briefs/M1-*.md` |

## 1. 退出标准逐条核对（milestone.md §4.4）

| # | 退出标准 | 状态 | 证据 |
|---|---|---|---|
| 1 | 学校注册→SaaS 审核→学校编号/超管账号/邮件→首登改密业务场景通过 | 达成 | `test_bs_m1b_tenant_onboarding.py`（注册/通过/拒绝/越权审计/体积护栏，真实 HTTP + MailHog + 对象存储断言）；全栈 E2E 首登强制改密真实链路（§2） |
| 2 | 三端登录；首次登录强制改密；错误/锁定/token 轮换/撤销测试通过 | 达成 | `test_bs_m1c_auth.py` + `tests/test_auth_tokens_unit.py`/`test_auth_password_unit.py` 等；全栈 E2E 三端真实登录 + 错误口令 + 首登强制改密（§2） |
| 3 | 教师/学生跨班级、跨租访问被拒绝；RLS 生效 | 达成 | `test_bs_m1e2_scope_and_sod.py`（数据范围 + 平台跨租只读 + fail-closed）；RLS 集成用例；全栈 E2E 教师令牌调平台接口被拒（§2） |
| 4 | 权限矩阵初版；审批自批被拒绝；超管例外默认关闭 | 达成 | M1-E1：59 权限码落库 + 8 内置角色出厂默认 + 接口权限声明（`6e916af`）；`test_approval_sod_unit.py`、`test_rbac_matrix_unit.py`、`test_bs_m1e2_scope_and_sod.py` 职责分离用例 |
| 5 | 生产环境跳过审核开关无法开启；审计记录可查、哈希链校验通过 | 达成 | M1-B TEN-03（配置校验强制）；M1-F 审计哈希链（`070e9c7`：触发器单链 + `backend/scripts/verify_audit_chain.py` + 查询接口 + 启动自检；第三轮复审通过，篡改检测负路径单测补齐）；全栈 E2E 经真实栈查到 `auth.login.success` 审计事件（§2） |
| 6 | 会话设备管理：超限登录踢最久未用设备（被踢方下一请求 401）；设备列表/下线/退出其它设备可用且审计留痕 | 达成 | `test_bs_m1c2_sessions.py`（AUTH-06 策略 C）；全栈 E2E 双设备真实会话 + revoke-others 后浏览器侧被踢回登录页、设备列表剩 1（§2） |
| 7 | 业务场景测试覆盖注册/审核/登录/权限/跨租；代码审查通过 | 达成 | `backend/tests/business/` 6 个业务场景文件（BS-M0-01、M1-B、M1-C、M1-C2、M1-D2、M1-E2）；逐功能点审查轮次与结论见 `docs/handoff.md` §1 与 `docs/review-log.md`（M1-A 两轮、M1-B 五轮、M1-C 三轮、M1-C-UI 两轮、M1-C2 五轮、M1-D1 五轮、M1-D2 两轮、M1-E1 五轮、M1-E2 两轮、M1-F 三轮（复审通过）） |

补充（超出原清单的 M1 交付）：M1-C-UI 三端登录页/强制改密页/守卫/HttpOnly 刷新 Cookie（`6af6f65`）；M1-D2 MFA 全套（enroll/activate/verify/disable/恢复码，前端 32 单测 + 28 mock E2E）；M1-F 审计哈希链收口 TD-068/091。

## 2. TD-110 收口：真实后端全栈 E2E（本次新增）

TD-110（M1-C-UI 时登记）处置承诺：「M1 验收阶段用专用 compose 栈（后端 + 测试库 + 前端 preview）跑全栈 E2E，覆盖登录→改密→业务接口的真实链路」。本次按方案 A（`docs/design-briefs/M1-acceptance.md`）完成：

**基础设施（全部为测试资产，零生产代码改动）：**

- `deploy/backend/Dockerfile`：python:3.13-slim + uv `--frozen`（105 包）、非 root 运行、uvicorn 启动——生产容器化路径首次真实演练（生产硬化另行登记 TD-171）。
- `deploy/docker-compose.e2e.yml`：专用 compose 栈（project `campus-elf-e2e`），经 external 网络 `campus-elf-test-net` 复用既有测试依赖栈；默认发布 `127.0.0.1:18000`（`E2E_BACKEND_PORT` 可配，2026-10-31 起不再占用 8000）；M2 收尾① 增加 arq `worker` 与共享对象卷 `e2e_objects`（导入任务由 worker 消费，API 与 worker 需读同一份上传文件）。
- `scripts/e2e-stack.sh`：up（重置测试库/Redis → 迁移 → 起 backend/worker → seed）｜down｜status；E2E 专用 JWT/KEK 随机生成写入 git 忽略的 `deploy/.env.test`。
- `backend/scripts/e2e_seed.py`：平台账号直写 + **真实 HTTP 注册→审核→开通**（与 BS-M1-B 同链路）+ 服务层造教师/学生/运营账号；初始密码从 MailHog 真实邮件提取；全部账号登录烟测 fail-fast。
- `frontend/playwright.fullstack.config.ts` + `frontend/e2e-fullstack/`：独立 Playwright 配置与用例，不引入接口 mock，不影响既有 28 个 mock E2E。

**12 个用例全部通过（29.4s，2026-10-24 实测）：**

> 2026-10-31 回填：M2 收尾① 把全栈栈扩到 **18 例**（+3 TD-183 学年流转/导入、+3 TD-192 权限/重置/MFA 真实后端链路），并补了 arq worker 与共享对象卷；当前复现命令与读数见 §6。

| 用例 | 验证的业务结果（非 HTTP 200） |
|---|---|
| 平台运营真实登录并登出 | 真实 JWT 会话建立；登出后回到登录页 |
| 校方运营管理员真实登录 | 租户 realm 登录 + 身份展示 |
| 错误口令提示统一文案且不跳转 | 后端 401 反枚举文案，不签发会话 |
| 首登强制改密（校方超管） | 开通邮件真实初始密码登录 → 强制改密 → **旧口令失效**（密码历史）→ 新口令可用 |
| 忘记密码真实邮件链路 | 真实 SMTP → MailHog 收信 → 令牌重置 → 新口令登录 |
| 平台业务接口 | 种子租户在 `/platform/tenants` 在列（active）；`/platform/audit-logs` 可查本次 `auth.login.success` 事件 |
| 数据范围：运营管理员看全校、教师仅本人 | 同一接口按 data_scopes 过滤（≥5 人 vs 恰 1 人） |
| 越权拒绝：教师调平台接口 | 403（realm 判定先于权限判定，恒定） |
| 会话设备管理 | 双设备会话列表 ≥2；revoke-others 后**被踢方下一请求失效、被踢回登录页**；列表剩 1 |
| 教师真实登录 C 端并登出 | C 端经真实后端健康检查（不再显示「后端未连接」） |
| 学生首登强制改密 | 种子 must_change_password 生效 → 改密 → 进首页 |
| 学生错误口令提示且不跳转 | 同反枚举口径 |

**过程中发现并修复（自测轮次）：** 首轮 9/12：① 学生新口令包含登录名被密码策略拒（换 `E2E-Learner-New-Pass1`）；② MailHog 正文为 MIME 编码需解码后再取重置令牌（对齐 `tests/support/mailhog.py` 口径）；③ `/auth/sessions` 响应字段为 `sessions`。复测 12/12 通过。

## 3. 测试与门禁总览（M1 收口时）

| 层级 | 命令 | 结果 |
|---|---|---|
| 后端全量（单元/集成/业务场景） | `REQUIRE_TEST_SERVICES=1 uv run pytest -q` | **578 passed**（exit 0，2026-10-24 实测；含 M1-F 第三轮复审补充的 6 例——verify_chain 负路径单测 5 例 + M1-F downgrade 回归 1 例；历史文档曾记 561/574，以本次实测为准） |
| 后端静态 | `ruff check app tests`、`mypy --no-incremental app tests`、`alembic check` | 全绿；新增 `scripts/e2e_seed.py` 单独 ruff/mypy 通过；迁移无漂移（head=`a8c3e9f1b5d7`） |
| 前端质量门禁 | `pnpm lint` / `format:check` / `typecheck` / `test:unit` / `build` | 全绿（32 单测 passed） |
| E2E（mock，回归） | `pnpm test:e2e` | **28 passed**（47.8s，M1 收口时读数；2026-10-31 已扩到 96） |
| E2E（全栈，TD-110 新增） | `pnpm test:e2e:fullstack` | **12 passed**（29.4s，M1 收口时读数；2026-10-31 已扩到 18） |
| 一键本地 CI | `bash scripts/ci.sh` | 由上述各项构成；远端 CI 以最后推送为准（§5） |

## 4. M1 边界技术债评审（milestone §12）

台账共 **171 条**（169 既有 + 本次新增 TD-170/171），全部有处置：**已完成 41、已采纳 61、已延后 61、已拒绝 6、已缓解 1、已取代 1**；无未处置条目，满足「未处置不得进入下一里程碑」。

- **已完成（M1 内收口，抽样）**：TD-066/067/068/072/073/074/075/078/081/082/091/094/105/106/107/138/149/156 等；其中 TD-110 于本次收口（状态改「已完成」）。
- **已采纳（60 条）**：均在台账「目标里程碑」列指明去向；M2 立项时按台账拣选（如 TD-083/084、TD-136、TD-142、TD-144、TD-148、TD-151/152、TD-154/155、TD-157、TD-159~165、TD-167、TD-169）。
- **本次新增并登记**：
  - **TD-170**：全栈 E2E 未纳入远端 CI（当前仅验收期本机执行）→ 已采纳，目标 M6（随部署硬化决策是否入 CI 及依赖栈供给方式）。
  - **TD-171**：后端 Dockerfile 首版仅服务 E2E 验收，生产硬化（基础镜像 digest 锁定、镜像审计、healthcheck 资源口径、日志/信号处理）未做 → 已采纳，目标 M6。
- **TD-147 前置项**：RBAC 回滚约束（`downgrade` 重置 `role_permissions`/`user_roles` 数据为出厂状态）已按台账要求写入 `docs/handoff.md` §5.1「部署注意」。

## 5. 遗留与下一步

1. **M1-F 复审**：✅ 已随本次验收完成第三轮复审（1 Major 修复 + 复测 + 复审通过），handoff §1 已移除「待复审确认」标注。
2. **本次验收产物的独立审查**：全栈 E2E 基础设施 + 文档按两笔 commit 提交，推送后确认远端 CI 全绿：`e2e_seed.py` 命中 backend workflow 的 `backend/**` 路径过滤必触发（无生产代码改动，预期绿）；security workflow 随 main 推送必跑；frontend workflow 会跑 mock E2E 回归（`e2e-fullstack/` 不在其收集范围）。
3. **下一步里程碑**：M2 组织与人员（ORG-01~05）。立项时先做 M2 各功能点设计简报，并从台账拣选目标为 M2 的「已采纳」条目（TD-083/084 等）纳入对应功能点。

## 6. 复现命令

```bash
# 全栈 E2E（TD-110 起，M2 收尾① 扩到 18 例）
bash scripts/e2e-stack.sh up                     # 重置库→迁移→backend+worker 容器→seed
# 每次跑前都必须 up：用例会改口令/角色绑定/MFA，seed 同时重置这些状态
cd frontend
export E2E_ACCOUNTS_FILE=../deploy/.e2e-accounts.json
pnpm test:e2e:fullstack                          # 18 passed（2026-10-31 实测；首次全绿 50.8s、整改后复跑 1.3m，默认端口 18000）
pnpm test:e2e                                    # 96 passed（mock 回归）

# 后端基线
cd ../backend && eval "$(bash ../scripts/test-stack.sh env)"
REQUIRE_TEST_SERVICES=1 uv run pytest -q         # 572 passed
uv run alembic -x db_url="$TEST_DATABASE_URL" check   # 无漂移
```
