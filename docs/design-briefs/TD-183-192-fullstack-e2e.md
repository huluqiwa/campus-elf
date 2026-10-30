# TD-183 / TD-192 — M2 全栈 E2E 补跑与端口解耦 实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-31 |
| 状态 | 已确认（2026-10-31 用户选择「改成本项目换端口」；全栈 E2E 补跑为 M2 收尾①既定项） |
| 关联功能点 | M2 收尾①：TD-183（M2-UI1 三条全栈用例欠跑）、TD-192（M2-UI2 三条真实后端链路缺 UI 用例）、TD-207（夜间化，另点） |
| 验收引用 | `docs/m1-acceptance.md` §6 复现命令；`docs/handoff.md`「建议的下一步顺序」① |
| 上游缺口 | 本机 `127.0.0.1:8000` 长期被另一项目容器占用，`scripts/e2e-stack.sh` 固定 8000 且声明不做端口改配 |

## 1. 目标与验收

1. **TD-183**：`frontend/e2e-fullstack/admin-fullstack-m2ui.spec.ts` 的 3 条用例在真实后端栈上跑绿（学年流转全链路 / 流转页浏览器可达 / 导入上传轮询）。
2. **TD-192**：新增 3 条「UI × 真实库」链路并跑绿：
   - 角色权限被移除后，持有人**下一个请求即 403**，但**会话不断**（`/auth/me` 仍 200）；
   - 角色恢复出厂后，权限**下一请求热更新**（无需重新登录）；
   - 管理员 MFA 重置后，目标用户**旧 access 立即 401**（刷新家族撤销 + `token_version` 递增）。
3. **端口解耦**：全栈栈默认不再占用 `127.0.0.1:8000`，可通过环境变量改端口；浏览器侧 preview 代理必须跟随同一个后端地址，避免把请求打到别的项目。

## 2. 候选方案

### 2.1 端口

| 方案 | 做法 | 优点 | 缺点/风险 |
|---|---|---|---|
| A 等 8000 空闲 | 维持现状 | 零改动 | 已被现实证伪：占用方长期存在；用户已否 |
| **B 可配端口（选）** | compose 用 `${E2E_BACKEND_PORT:-18000}`；脚本读环境变量/`.env.test`；preview 代理经 `VITE_API_PROXY_TARGET` 跟随 `deploy/.e2e-accounts.json` 的 `e2e_base_url` | 一劳永逸；默认值避开常见 8000；不改生产 compose | 需要同步改脚本、compose、两个 vite config、Playwright 全栈配置 |
| C 每次随机空闲端口 | 脚本探测空闲端口并回写 accounts | 永不撞车 | 端口不可预期，失败复现与排障成本高；多文件联动易漂移（否） |

### 2.2 TD-192 用例形态

| 方案 | 做法 | 优点 | 缺点/风险 |
|---|---|---|---|
| A 纯 API 用例 | Node fetch 全程 | 快 | 不验证 TD-192 要求的「UI 侧与真实库联动」 |
| **B 操作方走浏览器、被影响方走 Node API（选）** | 管理员在角色页/安全工具卡完成写操作；被影响用户用登录得到的真实 access 调接口断言 | 真正覆盖 UI → 真实后端 → 另一会话；实现量适中 | 需要专用种子账号与并发隔离（用 serial） |
| C 双浏览器上下文 | 两个 `browser.newContext()` | 最贴近用户 | 慢、会话/设备计数互相干扰，M2 无必要 |

### 2.3 种子数据

| 方案 | 做法 | 优点 | 缺点/风险 |
|---|---|---|---|
| A 复用现有账号 | 用 school_ops/teacher | 零改动 | school_ops 缺 `school.role.manage`；无 MFA 已绑定目标，链路不可达 |
| **B 新增 3 个专用账号（选）** | `role_admin`（role.manage + mfa.reset + personnel.view）、`role_target`（绑内置 `school_admin`）、`mfa_target`（教师 + MFA 已绑定，secret 进 git 忽略账号文件） | 链路可达；账号互不踩踏；secret 不出测试域 | seed 脚本需支持内置角色绑定与 MFA 密文写入 |
| C 用例内直写数据库 | Playwright 用 psql | 灵活 | 违背「全栈=E2E 走真实链路」口径（否） |

## 3. 选择结论

- 端口走 **B**：默认 `18000`，可用 `E2E_BACKEND_PORT` 覆盖；前端 preview 代理由 Playwright 全栈配置按账号文件里的 `e2e_base_url` 注入，**不允许**回落到 8000 去误伤别的项目。
- TD-192 走 **B**：3 条用例串行（`test.describe.configure({ mode: "serial" })`），因为都会改同一个内置角色 `school_admin` 的状态。
- 种子走 **B**：只扩 `backend/scripts/e2e_seed.py` 与 git 忽略的账号文件，不碰生产代码；`backend/app/**` 零改动。

## 4. 实现顺序

1. 端口解耦：`deploy/docker-compose.e2e.yml`、`scripts/e2e-stack.sh`、`frontend/apps/admin/vite.config.ts`、`frontend/apps/campus/vite.config.ts`、`frontend/playwright.fullstack.config.ts`。
2. 种子：新增 3 个账号；MFA secret 用 `E2E_ENCRYPTION_KEK` 加密后写库，明文只进 `deploy/.e2e-accounts.json`。
3. 测试支撑：`frontend/e2e-fullstack/helpers/totp.ts`（RFC 6238，30s/6 位/SHA-1）+ `support.ts` 类型扩展。
4. 新增 `frontend/e2e-fullstack/admin-fullstack-td192.spec.ts` 3 条串行用例。
5. 跑：`bash scripts/e2e-stack.sh up` → `cd frontend && E2E_ACCOUNTS_FILE=../deploy/.e2e-accounts.json pnpm test:e2e:fullstack`（预期 12 旧 + 3 TD-183 + 3 TD-192 = 18 条）。
6. 文档与门禁：TD-183/192 处置、`docs/m1-acceptance.md` §6 默认端口、`docs/handoff.md`、README 规模行；`pnpm typecheck/lint/format:check`、前端单测与 mock E2E 不受影响（CI 跑）。

## 5. 测试策略

- 单元/集成：本点不改生产代码，无新增后端用例。
- 全栈 E2E（本点验收）：
  - 撤权：浏览器保存 → 目标旧 token 访问 `GET /school/personnel` 断言 403；`GET /auth/me` 断言 200 且权限集已不含该码。
  - 恢复出厂：先用 API（真实后端）把 `school.personnel.view` 移除 → 浏览器点「恢复出厂」→ 目标旧 token 下一请求 200。
  - MFA 重置：目标用密码 + TOTP 登录 → 浏览器安全工具卡按登录名定位并重置 → 目标旧 access `/auth/me` 401；重新登录无需 TOTP。
- 业务场景：三条链路本身就是业务场景（权限热更新/会话撤销），断言到 HTTP 状态与权限集合，不只看页面提示。
- 性能/评估：不适用。

## 6. 预判技术债

- `E2E_BACKEND_PORT` 默认 18000 仍可能被别的项目占用——但此时脚本会明确报「端口被占用」而不是静默连错；改端口只需一个环境变量（本次即为此解耦，不另开 TD）。
- 全栈 E2E 仍未进 CI/夜间（TD-207，另行处置；本点只补跑欠账）。
- worker 没有业务级健康检查（只采样容器 running + RestartCount），「进程稳定但不消费队列」只能靠导入用例兜底发现 → 登记 TD-266（已延后 M6，与 TD-207/TD-170 同批）。
- 种子账号口令是仓库内固定测试值（仅 127.0.0.1 测试域）→ 登记 TD-267（已拒绝改随机，理由见台账）。

## 7. 高风险确认

- 本点不改 `backend/app/**`、不改任何权限判据或会话撤销逻辑；只改测试基础设施、种子与前端构建期代理配置。
- TOTP secret 只写入 git 忽略的 `deploy/.e2e-accounts.json`；账号口令是种子脚本里的测试域固定值（仅 127.0.0.1 测试栈，不用于真实环境）；都不打印、不入库、不入日志。账号文件写入后尽力收紧到 `0600`：Linux/macOS 上生效；Windows 上 Python 的 `chmod` 只改只读位，实际访问控制由 NTFS ACL 决定（本机账号目录默认就是当前用户私有），失败也不中止种子。
- 不触碰用户另一项目的容器；换端口后与 8000 完全解耦。

## 8. 实施回填（2026-10-31，首跑后补记）

首跑未全绿（3 条 TD-183 全挂 + 首登改密 1 条超时 + TD-192 首条选择器断言失败），逐条定位后补了简报未预见的 4 处：

1. **`school_ops` 种子缺权限**：学年流转/建班要 `school.org.manage`（同时决定菜单可见），导入要 `school.personnel.import`；原种子只有 `personnel.view`，接口 403、菜单项不存在。已在 `backend/scripts/e2e_seed.py` 补齐（仍走自定义角色，不改生产代码）。
2. **全栈栈只有 API 容器，没有 arq worker**：导入任务由 `app.workers.settings.WorkerSettings` 消费，缺 worker 时 job 永远 `pending`。`deploy/docker-compose.e2e.yml` 新增 `worker` 服务（同镜像，`arq app.workers.settings.WorkerSettings`），`e2e-stack.sh up` 增加「worker running」等待。
3. **对象存储要跨容器共享**：API 把上传文件写到 `OBJECT_STORE_DIR`，worker 再按 key 读取；原 `tmpfs` 是每容器独立。改为具名卷 `e2e_objects` 双挂载；具名卷首次创建归 root，因此再加一次性 `object-init` 容器 `chown appuser`，`backend`/`worker` 以 `service_completed_successfully` 依赖它。
4. **两处用例断言问题**：Element Plus 原生 checkbox 输入是隐藏的，`openSchoolAdminRole` 改为断言外层勾选行可见（状态仍用 `toBeChecked()`）；首登改密实测改密请求 ~4s（bcrypt + 口令历史），默认 5s 断言窗口会误判，URL 断言放宽到 15s。

5. **独立审查后的加固**：`up` 开始即删除旧账号文件、Playwright 端拒绝非本机地址与 8000（防旧文件把口令发到别的项目）；`object-init` 每次清空对象卷再改属主（垃圾不跨运行累积）；worker 等待改为「连续两次 running + RestartCount=0」，拦住启动即崩的重启循环；账号文件写后尽力收紧到 `0600`（Linux/macOS 生效，Windows 仅清只读位，见 §7）。

另：导入用例补了业务结果断言——任务 `success_rows=1` 之外，还要求该教师能从 `/school/personnel` 查到（计数正确不等于档案真的建了）。

**实测结果（2026-10-31，本机）**：`18 passed`（首次全绿 50.8s；第一轮审查整改后复跑 1.3m；第二轮审查整改后复跑 **1.8m**——该次与 `pnpm typecheck/lint` 等门禁并行跑，耗时偏高，是当前代码的读数）；端口 `18000`（`photo-memory` 的 8000 全程未受影响）。
