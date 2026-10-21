# M1-C-UI — 三端登录页与强制改密页（AUTH-01/02 前端）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-21 |
| 状态 | **已确认并实现**（2026-10-21：三项全部按推荐——HttpOnly Cookie、admin 单页身份切换 + campus 登录页、统一 401/403 拦截；后端已补 Cookie 支持） |
| 关联功能点 | M1-C-UI（M1-C 的前端部分，TD-103）：三端登录页、首登强制改密页、路由守卫与令牌存储、401/403 统一处理 |
| 验收引用 | `docs/spec.md` §3.3 AUTH-01/02（登录返回 access/refresh、tenant_id、角色、must_change_password；首登未改密 403）；`docs/test-plan.md` §8 E2E（三端登录主路径） |
| 不在本简报范围 | **会话设备管理（AUTH-06 / M1-C2）**：其"数据 → 会话服务 → 3 个接口"属 M1-C2；"登录设备页"在 M1-C2 后端完成后再并入前端（见 §6 顺序建议） |

## 1. 目标与不解决

**目标**：三端能用界面完成登录与首登改密，并在未登录/令牌失效/权限不足时得到正确引导。

- 校方 B 端与平台运营端共用一个 admin 应用；C 端学生/教师用 campus 应用（Vant，移动端 H5）。
- 登录页 → （首登）强制改密页 → 首页；刷新页面/刷新令牌过期/被踢出时自动回到登录页并给出原因。
- 后端接口已全部就绪（`/api/v1/auth/login|refresh|logout|change-password|me`），本期**不改后端契约**（除非选择 §3.1 方案 A，需要后端补 Cookie 支持）。

**不解决**：登录页视觉增强、记住我、第三方登录、设备管理页（M1-C2 后）、 playground/E2E 之外的性能指标。

## 2. 现状（可复用）

- `frontend/apps/admin`（Element Plus）与 `frontend/apps/campus`（Vant 4）已各有 Vue3 + Vite + Router + Pinia 骨架，目前只有 `HomeView.vue`；`packages/api-client` 已生成类型化客户端并有 Vitest 用例；Playwright 已有 `admin.spec.ts` / `campus.spec.ts` / `admin-health.spec.ts` 三条基线。
- 后端已实现：登录失败统一文案、首登改密门禁（403）、`/auth/me` 返回角色与权限、刷新轮换与重用检测、登出即时失效。

## 3. 需要拍板的三项细节

### 3.1 刷新令牌存哪里（安全 vs 工作量）

- **方案 A（推荐）**：access 令牌存**内存**（刷新页面即丢），refresh 令牌存 **HttpOnly + Secure + SameSite=Lax Cookie**（JS 读不到，XSS 拿不到长期凭证）。
  - 需要后端小改动：登录/刷新响应增加 `Set-Cookie`，刷新接口允许从 Cookie 取 refresh，登出清 Cookie；前端 `withCredentials`。
  - 风险：微信内置浏览器/移动 Safari 对跨站 Cookie 行为有差异，需在 M4 前实测；若不可行退回方案 C。
- 方案 B：access + refresh 都存 `localStorage`。零后端改动，但任一处 XSS 即可拿到 7 天凭证，安全债大。
- 方案 C（折中）：access 存内存、refresh 存 `localStorage`，并限制 refresh 仅用于 `/auth/refresh`。
  - 优点：纯前端、当期不改后端；缺点：仍有 XSS 面。

**我的建议**：选 A（安全优先，且后端改动很小）；若你希望 M1-C-UI 严格"只做前端"，则先用 C 并在 M4 前端阶段前切 A（登记 TD）。

### 3.2 登录页形态（三端怎么分）

- **方案 A（推荐）**：admin 应用内**一个登录页**，顶部切换"平台运营 / 校方"两种身份（分别调 `realm=platform` 与 `realm=tenant`；校方表单含学校编号）；campus 应用一个登录页（学校编号 + 学号/工号）。
  - 优点：三端各一张页，改动少；平台与校方共用 admin 但身份显式选择，避免"拿校方账号试平台"的混淆。
- 方案 B：三个独立登录页（平台、校方、C 端各一）。缺点：平台与校方 UI 高度重复。
- 方案 C：一个表单自动判断（先按平台账号试，失败再按租户试）。缺点：错误文案混乱、审计口径不清，且要两次请求。

### 3.3 401/403 统一处理策略

- **方案 A（推荐）**：API 层拦截器：401 → 尝试刷新一次（前端"单飞"，并发 401 只刷一次）→ 成功则重放原请求，失败则清空令牌跳登录页并提示原因（"登录已过期"或"该设备已被退出登录"）；`403 首登必须改密` → 强制跳改密页（不可跳过）；其它 403 → 提示无权限，不跳转。
- 方案 B：每个页面各自处理。缺点：重复逻辑、容易漏。
- 方案 C：只处理 401，不区分 403 原因。缺点：首登用户会被"只是提示无权限"卡住。

## 4. 实现顺序与 TDD 清单

1. **api-client**：请求拦截器注入 `Authorization`，响应拦截器实现 401 刷新单飞 / 403 分流；`withCredentials` 取决于 §3.1 选择；补 Vitest 用例（拦截器状态机、并发 401 只刷新一次、刷新失败跳登录）。
2. **状态**：Pinia `authStore`（access 内存、用户信息与权限、`mustChangePassword`），登录/登出/改密 action。
3. **页面**：admin 登录页（身份切换 + 学校编号 + 账号 + 密码）、admin 强制改密页；campus 登录页、campus 强制改密页（Vant 表单与提示）。
4. **路由守卫**：未登录 → 登录页；`mustChangePassword` → 改密页（白名单：改密/登出）；已登录访问登录页 → 首页。
5. **接口联调**：登录成功后按 `must_change_password` 与角色跳转；登出调用 `/auth/logout` 后清空本地状态。
6. **TDD 红→绿清单**：
   - Vitest：拦截器（401→刷新成功重放、401→刷新失败清空并跳转、403 首登改密分流、并发 401 单飞）、守卫（未登录/待改密/已登录三种走向）、authStore（登录/登出/改密状态变化）；
   - Playwright：平台登录 → 审核列表可见；校方登录 → 首登强制改密 → 改密后进入首页；C 端登录 → 首页；错误密码 → 统一文案且不跳转；刷新令牌失效 → 自动回登录页。
   - 现有 3 条 e2e 基线保持通过（页面结构变化后同步更新选择器）。

## 5. 测试策略

单元（Vitest）覆盖拦截器与守卫状态机；E2E（Playwright，真实前后端）覆盖三端主路径与两条失败路径；不新增后端测试（后端契约已在 M1-C 覆盖）。CI 沿用 frontend 工作流（lint/format/typecheck/unit/build/audit + e2e job）。

## 6. 预判技术债与顺序建议

| 预判条目 | 处置建议 |
|---|---|
| Cookie 方案在微信内置浏览器/移动 Safari 的行为差异 | M4 前端阶段实测；不可用则回退 localStorage 方案并登记 |
| refresh 存 localStorage 的 XSS 面（若选方案 C） | 登记 TD，M4 前切换 HttpOnly Cookie |
| 前端并发刷新"单飞"实现细节（多标签页） | 本期用内存 + 单飞；多标签同步（BroadcastChannel/存储事件）登记 TD |
| 登录设备页（AUTH-06） | 并入 M1-C2 完成后的前端小项，复用本简报的拦截器与 store |

**建议顺序**：① 本简报（M1-C-UI：登录页/改密页/守卫/令牌存储）→ ② M1-C2 后端（会话设备管理：数据、服务、3 个接口）→ ③ M1-C2 前端设备页（小项，复用 ①② 的基建）→ ④ M1-D（密码策略/失败锁定/MFA）。每个功能点都走同一门禁：测试全绿 → 独立审查 → 修复复测 → 提交推送 → 远端 CI 绿。

## 7. 高风险确认

认证前端涉及令牌存储与跳转逻辑，属高风险：编码前需确认 §3.1–3.3。审查重点：令牌是否可能落日志/URL/本地明文（按选定方案核对）、401/403 分流是否会造成"假登录"或死循环、刷新单飞是否正确、守卫是否存在绕过路径（直接输入 URL）、是否有敏感信息渲染。

## 8. 实现结果（2026-10-21）

- **后端**：`app/api/auth.py` 增加 HttpOnly 刷新 Cookie（登录/刷新/改密下发、刷新与登出可从 Cookie 读取、登出无条件清除）；`app/config.py` 新增 `AUTH_COOKIE_*` 与 `AUTH_REFRESH_TOKEN_IN_BODY`：**staging/production 强制 `AUTH_COOKIE_SECURE=true`、`AUTH_REFRESH_TOKEN_IN_BODY=false`（响应体省略 refresh，只走 Cookie）**，开发/测试保留 body 回显供自动化测试使用。
- **api-client**：`packages/api-client/src/auth.ts` 新增 `createAuthSession`（内存 access、`credentials: include`、401 单飞刷新后重放、403 分流"首登必须改密"/其它、restore 静默恢复、登录/登出/改密/me）；`auth.test.ts` 8 条用例覆盖单飞、重放、失败清空、403 分流、restore 静默。
- **admin**：`session.ts` 单例（回调在 main.ts 绑定，避免循环依赖）、Pinia `stores/auth.ts`、`LoginView.vue`（身份切换 校方/平台运营）、`ChangePasswordView.vue`、`HomeView.vue`（身份/权限、改密入口、登出）、路由守卫（Cookie 静默恢复 → 未登录进登录页 → 首登强制改密）。
- **campus**：同等结构（Vant）：登录页、强制改密页、首页（健康/身份/改密/登出）、守卫。
- **E2E（Playwright，接口 mock，**有状态**）**：`admin-login.spec.ts`（未登录跳转、校方首登改密全链路、平台登录、错误文案）、`admin-logout.spec.ts`（登出回到登录页）、`campus-login.spec.ts`（登录、首登改密）、`helpers/auth-mock.ts`（登录后才返回 /auth/me、改密后解除门禁、登出后回到 401）、`helpers/locators.ts`（兼容 Element Plus/Vant 的 testid 落点）；既有 admin/campus/health 基线适配受保护首页。**12 条 E2E 全绿**；CI/本地新增"用例数下限"守卫（防 testMatch 写错导致静默不收集）。
- **单测/构建**：api-client **20 passed**（含改密必带 Authorization、仅 Cookie 登出、/auth/me 刷新重放、X-Auth-Error 分流、令牌事件）；`pnpm lint/format:check/typecheck/build` 全绿；后端全量 **238 passed**。
- **第一轮独立审查修复（2026-10-21）**：Blocker B1（改密请求未带 Authorization → 首登改密不可用）→ `changePassword` 走带鉴权请求并补回归单测；MA1（改密未下发新 Cookie → 改密后刷新即掉线）→ 后端改密响应 Set-Cookie + 测试；MA2（登出仅在有 access 时才调后端 → 过期后退出无效）→ 登出支持仅凭 Cookie、任何路径都清 Cookie、审计标注 self_revoked + 测试；MA3（响应体回显 refresh 抵消 HttpOnly）→ 新增 `AUTH_REFRESH_TOKEN_IN_BODY`，staging/production 强制 false 且响应省略该字段；MA4（campus 用例未被收集）→ testMatch 修正 + 用例数下限守卫；Minor：store 以库值判断门禁、令牌刷新事件同步 store、/auth/me 允许刷新重放、403 按 `X-Auth-Error` 机器码分流、提交防重、去掉未用表单 ref、Cookie 路径与 API_PREFIX 联动校验、非开发环境禁止 SameSite=none、`components.d.ts` 入库、登出 E2E 与 mock 语义对齐。
- **第二轮独立审查（通过，0 Blocker/0 Major，6 Minor）**：Minor 1（改密未接入 401 刷新重放）→ 改密改走 `request()`；Minor 2/3（设计文档与 `.env.example` 未随 MA3 更新）→ 已补 `AUTH_REFRESH_TOKEN_IN_BODY` 说明与示例项；Minor 4（登出 E2E 重复 route 遮蔽状态迁移）→ 去掉重复 route 并补"登出后再访问受保护页被拦回登录页"断言；Minor 5（有状态 mock 的 must_change_password/tenant_id 与后端不一致）→ mock 按状态生成、平台场景省略 tenant_id；Minor 6（网络错误露出技术文案、`OptionalPrincipal = None` 冗余默认值）→ 新增 `toUserMessage` 兜底并用于四个视图、去掉冗余默认值。
- **未包含（登记 TD-110）**：真实后端的全栈 E2E（当前 E2E 用接口 mock 保证确定性；全栈版本放到 M1 验收时以专用 compose 栈运行）。
