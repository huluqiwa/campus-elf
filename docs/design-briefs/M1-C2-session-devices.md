# M1-C2 — 会话设备管理（AUTH-06，A31）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-21 |
| 状态 | **已确认**（2026-10-21：§3 四项细节全部按推荐执行——Redis 会话标记实现即时失效；超限踢最久未用；设备标识用粗粒度 UA 标签 + IP/UA 只存哈希；上限 C 端 3 / 校方 3 / 平台 2，配置化） |
| 关联功能点 | M1-C2 / AUTH-06：按 realm 限制同时登录设备数、超限踢最久未用、设备列表与下线、退出其它设备 |
| 验收引用 | `docs/spec.md` §3.3 AUTH-06 与决策 A31；`docs/milestone.md` M1 范围/退出标准；`docs/test-plan.md` BS-48 |
| 依赖 | M1-C 已有：登录链（family）、`sid` 可扩展、Redis 撤销标记、审计基座；M1-C-UI（前端入口，可后置） |

## 1. 目标与不解决

**目标**：一个账号同时最多在 N 台设备上保持登录（默认 C 端学生/教师 3 台、校方 B 端 3 台、平台端 2 台，配置化）；超出时踢掉"最久未使用"的那台；用户能查看自己的登录设备、下线单台或一键退出其它设备；被踢/被下线的设备**下一次请求立即失效**。以上操作全部留审计。

**不解决**：新设备登录通知/异常登录提醒（属 M5 消息中心）、设备指纹与风控（二期）、同账号跨租户共享（跨租红线不放开）、前端页面（归 M1-C-UI，本简报只定义契约）。

## 2. 数据与机制（现状可复用度）

- 登录链已是现成的"会话"：`refresh_tokens.family_id`（租户）/ `platform_refresh_tokens.family_id`（平台），`family_id` 上已有索引；每台设备 = 一条 family。
- 访问令牌当前 claim：`sub/realm/tenant_id/jti/token_version/must_change_password/iss/aud/exp`；**本功能新增 `sid`（= family_id）**，使每次请求能判定"这条会话还在不在"。
- 撤销标记已有 Redis 客户端（`app/auth/revocation.py`，键带 `{prefix}` 与 realm，含 socket 超时）。

## 3. 需要拍板的四项细节（候选方案）

### 3.1 被踢后如何"立即"失效

- **方案 A（推荐）**：Redis 会话标记。登录/刷新时写 `{prefix}:auth:session:{realm}:{sid}`（TTL = 刷新令牌有效期）；登出/被踢/下线即删除；鉴权依赖在现有的一次 Redis 查询里顺带判断"会话是否存在"。
  - 优点：被踢方**下一次请求**即 401（秒级），不改变现有每请求一次 Redis 的开销；实现简单且可观测。
  - 缺点：依赖 Redis 可用；Redis 故障时需降级策略（见 §6）。
- 方案 B：每次请求查库校验会话。优点：不依赖 Redis。缺点：每请求多一次数据库往返，且热点集中（M1-C 已登记 TD-100 讨论权限缓存）。
- 方案 C：不管访问令牌，仅作废刷新令牌。优点：零改动。缺点：被踢设备仍能用最多 15 分钟，不满足"立即失效"的验收。

### 3.2 超限时的行为

- **方案 A（推荐）**：踢"最久未使用"的会话（按最近一次刷新/使用时间），新登录成功。符合已写入 spec 的策略 C。
- 方案 B：拒绝新登录并提示"该账号已在 N 台设备登录，请先退出其它设备"。优点：不会让无辜设备掉线。缺点：换手机/清缓存后可能把用户锁在外面，校园场景投诉多。
- 方案 C：允许超限但每台新登录都提示用户主动清理。缺点：等于没有上限。

### 3.3 设备标识怎么显示

- **方案 A（推荐）**：服务端按 UA 粗粒度解析出"浏览器 / 操作系统"标签（如 `Chrome / Windows`、`Safari / iOS`），加"最近使用时间"与"是否当前设备"；IP/UA **只存哈希**，接口不回显明文，未知时显示"未知设备"。
  - 优点：够用（用户能认出"这是我办公室电脑"），且不引入新的 PII 面。
- 方案 B：显示 IP 前两段 + UA 原文。缺点：IP/UA 属可识别信息，与"最小采集"原则冲突。
- 方案 C：只显示"设备 1/2/3"。缺点：用户无从判断该踢哪台。

### 3.4 上限台数与配置方式

- **方案 A（推荐）**：配置项 `MAX_SESSIONS_PER_USER_TENANT=3`、`MAX_SESSIONS_PER_USER_PLATFORM=2`（平台端更严），`.env` 可调，重启生效；后续如按租户差异化，走平台配置（M6）。
- 方案 B：统一 3 台（不分 realm）。缺点：平台运营账号权限最大，反而更宽松。
- 方案 C：按角色区分（学生 2/教师 3/校方 5）。缺点：规则复杂、解释成本高，收益不明显。

## 4. 实现顺序（数据 → 接口 → 界面）与 TDD 清单

1. **数据/配置**：访问令牌加 `sid`；`refresh_tokens` / `platform_refresh_tokens` 增 `last_seen_at`（刷新时更新，用于"最久未使用"排序）与 `device_label`（粗粒度标签，写入时计算）；新增配置项与生产校验（≥1 的整数）。
2. **会话服务**（`app/auth/sessions.py`）：`register_session`（写 Redis 标记）、`touch_session`（刷新时更新 last_seen）、`list_sessions`（按用户/realm 聚合 family：未撤销且有标记）、`revoke_session(sid)`、`revoke_others(sid)`、`enforce_limit(user, realm)`（超限挑最久未用踢出 + 审计）。
3. **鉴权依赖**：`get_current_principal` 校验 `sid` 对应会话标记存在（与 jti 撤销检查合并为一次 Redis 读）；缺失 → 401「该设备已被退出登录」。
4. **接口**：`GET /auth/sessions`、`DELETE /auth/sessions/{session_id}`、`POST /auth/sessions/revoke-others`；登录响应继续返回令牌与 `must_change_password`。
5. **界面**（并入 M1-C-UI）：登录设备列表页（当前设备标记、最近使用、下线按钮、"退出其它设备"），以及超限登录后的提示文案。
6. **TDD 红→绿清单**：
   - 单元：设备标签解析（Chrome/Windows、Safari/iOS、未知 UA → "未知设备"）、超限挑选"最久未用"的排序函数、`sid` claim 签发与解析；
   - 集成：第 4 台登录踢第 1 台（Redis 标记删除 + 家族作废 + 审计 `auth.session.kicked`）；被踢设备下一请求 401；`list_sessions` 只列本人且标记当前设备；单台下线不影响其它设备；`revoke-others` 只保留当前；改密仍全设备下线；刷新会更新 `last_seen_at` 并延长会话标记 TTL；
   - 业务场景：BS-48（HTTP 全链路：四台设备登录 → 最旧设备 401 → 查看设备列表 → 退出其它设备 → 新设备可用）；
   - E2E（M1-C-UI 阶段）：设备列表页与"退出其它设备"。
7. **并发与边界**：同一设备并发刷新不重复注册会话；踢出与"设备正在刷新"竞争时以条件更新（`revoked_at is null`）判定，输者按被踢处理（401 而非 reuse_detected，避免误报盗用）。已轮换旧令牌若**自身已过期**，即使家族仍活跃也按已失效处理、不触发家族撤销（过期凭证不可用；避免正常设备长期使用后的旧令牌被误报为泄漏）；未过期重放仍按 reuse 撤销家族。

### 4.1 开工核对后的实现细节（2026-10-21，不改变 §3 已确认策略）

1. **`sid` 为必需 claim**：访问令牌必须携带 `sid`（= `family_id`），缺失/非法一律 401。升级后旧令牌（无 `sid`）需重新登录；当前仅开发环境，可接受。
2. **迁移**：`refresh_tokens` / `platform_refresh_tokens` 增加 `last_seen_at timestamptz`、`device_label varchar(64)`；存量行 `last_seen_at = issued_at`。`revoked_reason` 新增 `session_kicked`（超限踢出）与 `session_revoked`（用户下线/退出其它）。
3. **family 级聚合**：轮换时新行写入 `last_seen_at=now()`、`device_label`（按本次 UA 重新解析）；设备列表按 `family_id` 取最新有效行，按 `last_seen_at` 倒序；只列 `revoked_at is null and expires_at > now()` 且 Redis 标记存在的 family。
4. **并发串行化**：同一账号的登录/刷新/撤销/改密共享事务级 `pg_advisory_xact_lock(hashtextextended('{realm}:session:{user_id}', 0))`；撤销提交后，等待中的刷新获得锁并**重新读取**数据库，从而看到已撤销的 family（不以登录前的旧快照判定）。登录按活跃 family 数计算需踢数量 `count - limit + 1`（配置下调时可一次踢多台），按非空 `last_seen_at`（存量已回填 `issued_at`）升序踢最久未用；审计逐台写 `auth.session.kicked`。实现中未使用 `SELECT ... FOR UPDATE`（advisory lock 同样串行化，且避免与空结果行锁的边界）。
5. **Redis 语义**：鉴权时用一次 `MGET` 同时检查 jti 撤销键与会话标记键；jti 已撤销或会话标记缺失 → 401。Redis 不可用时按 §6 降级：跳过会话存在性检查（仍校验 token_version/账号状态），记录告警；标记删除失败会留下最长 15 分钟的失效窗口，登记 TD 跟踪。登录/刷新/改密的会话标记在**数据库事务内**登记（随提交生效），避免“提交后补写”把并发撤销的会话复活；撤销提交后再删除标记，删除失败仅产生最长 15 分钟的窗口。
6. **接口契约**：
   - `GET /api/v1/auth/sessions` → `{"sessions": [{"session_id": UUID, "device_label": str, "last_seen_at": datetime, "current": bool}]}`；不含明文 IP/UA。
   - `DELETE /api/v1/auth/sessions/{session_id}` → `200 {"status":"ok"}`；仅本人会话，不存在/非本人一律 404；删除当前设备时清除刷新 Cookie。
   - `POST /api/v1/auth/sessions/revoke-others` → `200 {"status":"ok","revoked_count": int}`；保留当前 `sid`。
   - 三个接口均要求已登录；首登改密门禁期间仍按 spec 只放行改密/登出/me，会话管理接口需改密后使用。
7. **审计动作**：`auth.session.kicked`（metadata 含被踢 family/设备标签/原因）、`auth.session.revoked`（单台下线）、`auth.session.revoked_others`（数量与当前 family）。审计与数据库撤销同事务；不记录明文 UA/IP。

## 5. 测试策略

按 project 分层：单元（标签/排序/claim）、集成（真实 PG + Redis：限额、踢出、列表、下线、退出其它、与改密/停用的交互）、业务场景 BS-48（HTTP）、E2E（M1-C-UI 阶段）。断言业务输出：`refresh_tokens` 行状态、Redis 会话标记、审计动作（`auth.session.kicked` / `auth.session.revoked`）、响应状态码与文案，而不只是 200/401。

## 6. 预判技术债

| 预判条目 | 处置建议 |
|---|---|
| 会话标记依赖 Redis（故障时被踢设备最长 15 分钟后失效） | 沿用 TD-102 的降级说明；Redis 不可用时按"仍校验 token_version + 刷新令牌"降级，登记监控项 |
| `last_seen_at` 只在刷新时更新（约 15 分钟粒度） | 可接受；若要更准需每请求写入（放弃），登记为取舍 |
| 多标签浏览器/隐私模式被当成"新设备"占额度 | 文案提示 + 提供"退出其它设备"；不做指纹识别（避免隐私争议） |
| 上限调整需要重启 | M6 平台配置中心收敛为按租户可配 |
| 新设备登录提醒 | M5 消息中心实现 |

## 7. 高风险确认

认证/会话属高风险功能：编码前需确认 §3 四项细节。实现后按门禁执行：单元/集成/业务场景全绿 → 独立代码审查（重点：越权查看/下线他人会话、跨租隔离、Redis 键边界、审计完整性、并发踢出）→ 修复复测 → 复审通过后提交推送，并把结果回填本简报。

## 8. 实现结果（2026-10-21）

**交付内容**

- **迁移** `d4e6f8a0b2c1`（down_revision `c8e2f4a6b1d3`）：两张刷新令牌表新增 `last_seen_at`（存量回填 `issued_at`、NOT NULL）与 `device_label`；`revoked_reason` 扩展 `session_kicked` / `session_revoked`。downgrade 把会话类原因归一化为 `logout`（保留 `rotated`）后删除新列，`upgrade → downgrade → upgrade` 往返测试通过；`alembic check` 无漂移。
- **令牌**：访问令牌新增必需 `sid`（= `family_id`），缺失/非法一律 401；升级后旧令牌需重新登录。鉴权用一次 `MGET` 同时检查 jti 撤销键与会话标记。
- **会话服务**：按 realm 配置上限（租户 3 / 平台 2，`.env` 可调）；登录超限踢最久未用（`count - limit + 1`），逐台审计 `auth.session.kicked`；设备标签为粗粒度 UA（未知 → “未知设备”），IP/UA 仍只存哈希。
- **接口**：`GET /auth/sessions`、`DELETE /auth/sessions/{id}`（他人/不存在统一 404，撤当前设备清刷新 Cookie）、`POST /auth/sessions/revoke-others`（`returning` 计数、保留当前 sid）；首登改密门禁期间三个接口均 403。
- **登出**：带 Bearer 时即使没有刷新令牌也按 `sid` 撤销数据库 family；Redis jti 撤销改为 best-effort（故障不产生 500，审计记录真实 `access_revoked`）；同一 family 的 Bearer+刷新令牌只写一条 `auth.logout`，不同 family 各记一条。
- **并发/一致性**：登录、刷新、撤销、改密共享用户级事务 advisory lock，锁后重读；reuse 家族撤销同样取锁并在提交后删除会话标记，避免在途刷新插入的行逃过撤销。会话标记在数据库事务内登记（随提交生效），撤销/登出后删除；Redis 故障按 TD-102/TD-111 降级。
- **审计**：`auth.session.kicked` / `auth.session.revoked` / `auth.session.revoked_others` 与数据库撤销同事务，metadata 含 family/设备标签/数量，不含明文 UA/IP。

**测试与门禁**

- 新增/更新单元（标签、`sid`、配置、Redis 失联 fail-open）、集成（限额、踢出、列表、单台/他人/跨租下线、平台 realm、刷新续期、并发登录、撤销/改密与在途刷新竞态、踢出后 rotated 重放、reuse 标记删除）、业务场景 BS-48（HTTP 全链路、强制改密门禁、平台限制、无刷新令牌登出、Redis 故障登出、重复审计）与迁移回滚测试。
- 最终门禁：后端 **290 passed**、`ruff check app tests` 全绿、`mypy --no-incremental app tests`（99 文件）无问题、`alembic check` 无漂移、head=`d4e6f8a0b2c1`。

**独立审查**

- 第一轮：**不通过**（1 Blocker + 4 Major + 3 Minor），集中在撤销与在途刷新竞态（标记提交后补写可能复活已踢会话）、条件更新失败后的 reuse 误报、无刷新令牌登出未撤销 family、迁移 downgrade 约束不可往返、租户 SQL 纵深过滤；已逐项修复并补并发回归测试。
- 第二轮（聚焦复审）：**不通过**（0 Blocker + 3 Major + 2 Minor）：Redis 故障时 Bearer-only 登出 500 且未撤销 family；设备被踢后重放已 rotated 旧令牌误报 reuse；reuse 家族撤销未取锁/未删会话标记；Bearer+刷新令牌重复审计；文档未回填。对应修复：jti 撤销 best-effort + 先撤 DB family；family 级撤销状态判定；reuse 撤销取锁并删标记；按 family 去重审计；本 §8、handoff、README、tech-debt 回填。
- 第三轮（修复复审）：**不通过**（0 Blocker + 1 Major + 7 Minor）。Major 为 handoff/README/review-log 未回填；Minor 为租户 `family_revoked` 缺 `user_id`、已撤销 family 的登出审计误报 `family_revoked=true`、`session_scope` 静默回退平台连接、bcrypt 在锁内的放大面未登记、并发/迁移/安全回归缺口。修复：文档回填；SQL 补 `uid`；`revoke_refresh_family` 返回实际撤销结果；`session_scope` fail-closed；TD-116 扩写；新增自然过期重放、标记缺失过滤、踢出 vs 在途刷新（barrier）、改密后 rotated 重放、限额下调多踢、双 family 登出审计真值、迁移回填断言。
- 第四轮：**不通过**（0 Blocker + 1 Major + 5 Minor）。Major 为跨租/跨用户登出时审计落在操作者租户且丢失 subject；修复为 `FamilyRevocation` 携带归属（realm/tenant_id/subject_user_id），第二台设备审计写入归属租户并带 subject，新增跨租与同租他人用例。Minor 已收口：并发 barrier、`session_scope` 回归、过期 rotated 决策记录与用例、引用修正、TD-117、同 family 审计真值断言。
- 第五轮（最终复审）：**通过**（0 Blocker / 0 Major / 5 Minor）。剩余 Minor 已收口：barrier 按任务首次获取、审计 actor 断言、引用修正；跨 family 审计非原子与跨 realm 矩阵登记 TD-118。

**取舍与技术债**：TD-111（Redis 故障/标记删除失败的失效窗口）、TD-112（`last_seen_at` 刷新粒度）、TD-113（上限配置需重启）、TD-114（多标签/隐私模式占额度）、TD-115（新设备通知，M5）、TD-116（锁内 bcrypt/Redis 写入的排队放大面）、TD-117（Redis 键不含租户段的既有取舍，M6 统一收敛）、TD-118（跨 family 登出审计非原子与跨 realm 矩阵缺口）。
