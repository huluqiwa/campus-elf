# 校园陪伴精灵 — 功能规格与验收标准（spec.md）

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-10-19 |
| 状态 | 待用户确认；确认前不进入 M0 |
| 上游 | `docs/requirements.md` |
| 下游 | `docs/design.md`、`docs/milestone.md`、`docs/test-plan.md` |
| 事实来源 | 用户最新确认 > requirements.md > 本文件 > design.md |

> 约定：功能点 ID 用于 spec / design / test-plan / milestone / commit 互相追踪。验收标准优先使用 Given/When/Then；无法用 GWT 表达的用“输入-输出断言”。

---

## 1. 范围与角色

### 1.1 MVP 范围

多租户注册与审核、学校编号、三端登录、RBAC+数据范围、人员导入与 B 端人员管理、知识库上传→审批→构建→测试→发布→热更新、C 端 AI 问答/流式/引用/反馈、请假、账号删除/撤权 5 天状态机、统一审批中心、消息中心、SaaS 租户审核开关/基础配置、统一审计、租户隔离、内容安全基础、校方看板与反馈查看。

### 1.2 MVP 不做

微信小程序、MCP/SKILLS 市场、盲盒、词频榜、自动心理诊断、学生创作工具（创作中心整体三期）、完整心理告警、校园墙、积分任务、个人知识库、个人课表、长期记忆、技能中心、皮肤中心、知识库定制化、计费、公告、AI 写操作、定制人设、C 端已读、版本间分块 diff、完整视图监控/趋势分析/反馈导出。

### 1.3 角色

| 角色 | 说明 |
|---|---|
| SaaS 超管 | 平台账号/角色、租户审核、配置、审计、平台消息 |
| SaaS 运营 | 学校审核、租户开通、平台消息、基础配置（按权限） |
| SaaS 审计 | 只读审核记录/审计/租户状态 |
| 校方超管 | 本校全部权限；权限账号、人员、知识库、审批、消息、审计 |
| 校方授权管理员 | 按权限矩阵与数据范围获得查看/新增/编辑/删除/审批/导出/配置 |
| 教师（B 端权限） | 按授权执行；如班主任/辅导员请假审批、知识库测试 |
| 教师（C 端） | AI 问答、消息、反馈、账号删除；不默认有 B 端权限 |
| 学生（C 端） | AI 问答、消息、请假、反馈、账号删除 |

---

## 2. 功能点总览

| 模块 | 功能点 ID | 名称 | MVP |
|---|---|---|---|
| 基座 | SYS-01 | 配置/日志/错误码/健康检查 | 是 |
| 租户 | TEN-01 | 学校注册与资质提交 | 是 |
| 租户 | TEN-02 | SaaS 审核/开通/学校编号/超管账号 | 是 |
| 租户 | TEN-03 | 生产放行开关关闭 | 是 |
| 认证 | AUTH-01 | 三端登录 | 是 |
| 认证 | AUTH-02 | JWT/Refresh 轮换与撤销 | 是 |
| 认证 | AUTH-03 | 密码策略/历史/重置 | 是 |
| 认证 | AUTH-04 | MFA 可选 | 是 |
| 认证 | AUTH-05 | 失败锁定 | 是 |
| 认证 | AUTH-06 | 会话设备管理（限量 + 退出其它设备）（A31） | 是 |
| 权限 | RBAC-01 | 角色与权限矩阵 | 是 |
| 权限 | RBAC-02 | 数据范围（全校/班级/课程/个人） | 是 |
| 权限 | RBAC-03 | 审批职责分离 | 是 |
| 组织 | ORG-01 | 教师/学生档案 | 是 |
| 组织 | ORG-02 | 手动录入与 CSV/XLSX 异步导入 | 是 |
| 组织 | ORG-03 | 班级/课程/任职/班主任/辅导员关系 | 是 |
| 组织 | ORG-04 | 身份证加密与掩码 | 是 |
| 组织 | ORG-05 | 部门/科室与教师归属（A30，人群选择器数据源） | 是 |
| 知识库 | KB-01 | 上传/ClamAV/元数据 | 是 |
| 知识库 | KB-02 | 单文档版本审批与构建门禁 | 是 |
| 知识库 | KB-03 | 构建进度 SSE+轮询 | 是 |
| 知识库 | KB-04 | 测试沙箱与校方测试集 | 是 |
| 知识库 | KB-05 | 发布/蓝绿/回滚/下线/删除 | 是 |
| 知识库 | KB-06 | 分块预览/统计/搜索 | 是 |
| 知识库 | KB-07 | 检索测试 | 是 |
| 知识库 | KB-08 | 版本切换 | 是 |
| 知识库 | KB-09 | 文档查阅人群 ACL 与检索强制过滤（K4） | 是 |
| 知识库 | KB-10 | 切块编辑与派生重建（K5） | 是 |
| AI | AI-01 | 快路径/ReAct/多意图 | 是 |
| AI | AI-02 | SSE 流式输出 | 是 |
| AI | AI-03 | 思考摘要/开关/脱敏 | 是 |
| AI | AI-04 | 引用来源 | 是 |
| AI | AI-05 | 反馈采集 | 是 |
| AI | AI-06 | 近 10 轮短期记忆+摘要 | 是 |
| AI | AI-07 | 清空当前会话 | 是 |
| AI | AI-08 | 能力缺失兜底 | 是 |
| AI | AI-09 | 输入/输出护栏 | 是 |
| AI | AI-10 | 身份→人群解析与检索过滤（K4） | 是 |
| 审批 | APV-01 | 通用审批引擎 | 是 |
| 审批 | APV-02 | 知识库审批 | 是 |
| 审批 | APV-03 | 请假审批 | 是 |
| 审批 | APV-04 | 账号删除/撤权审批 | 是 |
| 审批 | APV-05 | 待办/已办/抄送/超时 | 是 |
| 请假 | LEAVE-01 | 学生请假草稿/提交/撤回 | 是 |
| 请假 | LEAVE-02 | 审批/驳回/只读预览 | 是 |
| 消息 | MSG-01 | 站内信+SMTP 邮件 | 是 |
| 消息 | MSG-02 | outbox/重试/死信 | 是 |
| 消息 | MSG-03 | 平台/租户通道隔离 | 是 |
| 消息 | MSG-04 | B 端已读/C 端二期 | 是 |
| 消息 | MSG-05 | 反馈答复 | 是 |
| 账号删除 | DEL-01 | 用户自助删除 | 是 |
| 账号删除 | DEL-02 | 管理员撤销登录权限 | 是 |
| 账号删除 | DEL-03 | 5 天只读+禁写 | 是 |
| 账号删除 | DEL-04 | 冻结/申诉 | 是 |
| 账号删除 | DEL-05 | 删除/匿名化/证明 | 是 |
| 看板 | DASH-01 | 校方基础运营看板 | 是 |
| 看板 | DASH-02 | 反馈查看/标记/答复 | 是 |
| SaaS | SAAS-01 | 模型/检索/限流配置 | 是 |
| SaaS | SAAS-02 | 配置变更审批/双人确认 | 是 |
| SaaS | SAAS-03 | 租户审核记录查询/导出 | 是 |
| SaaS | SAAS-04 | 平台消息中心 | 是 |
| SaaS | SAAS-05 | 租户审核开关 | 是 |
| 审计 | AUD-01 | 仅追加+哈希链+WORM | 是 |
| 观测 | OBS-01 | 本地 trace 表/JSONL | 是 |
| 观测 | OBS-02 | Langfuse 观测/评估/Prompt | 可选/生产 |
| 观测 | OBS-03 | Ragas 评估 | 是 |
| 非功能 | NFR-01 | 性能/并发 | 是 |
| 非功能 | NFR-02 | 安全/隔离 | 是 |
| 非功能 | NFR-03 | 备份/恢复 | 是 |
| 非功能 | NFR-04 | 隔离自动化测试 | 是 |

---

## 3. 功能规格与验收标准

### 3.1 系统基座 SYS-01

- 配置从 `.env`/配置中心读取；密钥不入库、不入日志；启动时校验必填项。
- 结构化日志含 request_id/tenant_id/user_id/action/latency；敏感字段脱敏。
- 统一错误码/错误信封；生产不暴露堆栈/内部地址。
- `/health/live`、`/health/ready` 健康检查。

**验收**
- Given 缺少必填配置，When 启动，Then 进程失败并输出明确缺失项（不输出密钥值）。
- Given 请求触发异常，When 返回，Then 错误信封含 code/message/request_id，无堆栈/内网地址。
- Given 日志采集，When 查看，Then 无明文密码/Token/API Key/身份证。

### 3.2 租户 TEN-01/02/03

- 学校提交学校名、地址、校领导信息、学校邮箱、资质凭证。
- 开发/测试可通过配置跳过资质与审核；生产默认关闭跳过，生产不得默认放行。
- 审核通过生成全局唯一学校编号、超管账号、随机初始密码并发送学校邮箱；审核记录含审核人/时间/结果/理由/凭证快照。
- 审核拒绝需理由；记录可查询/导出/审计。

**验收**
- Given 学校提交完整资质，When SaaS 审核通过，Then 租户状态 active、学校编号唯一、超管账号创建、初始密码邮件发送、审核审计写入。
- Given 审核拒绝，When 未填理由，Then 请求失败；填写理由后可拒绝，学校可看到理由。
- Given 生产环境，When `DEV_SKIP_SCHOOL_REVIEW=true`，Then 启动失败或健康检查 red（禁止生产跳过）。
- Given 同一学校重复提交，When 审核，Then 按配置的去重策略处理，不产生重复租户。

### 3.3 认证 AUTH-01～06

- 学生：学校编号+学号+密码；教师：学校编号+工号+密码；SaaS：平台初始化账号+密码。
- JWT access 15 分钟；refresh 7 天轮换；复用检测撤销 token 家族；登出/改密/删除撤销全部 refresh token。
- 密码：12 位随机初始（平台账号按账号长度加长以避免包含账号）、首登强制改密；最小 10 位、至少 3 类字符、最大 128 字符且不超过 72 UTF-8 字节（bcrypt 输入上限）、禁止含学号/工号、最近 5 次历史；bcrypt cost 12。
- 5 次失败/15 分钟锁定 15 分钟；MFA 可选，生产可对 SaaS 员工/超管强制。
- 密码找回：员工/平台账号邮件重置（M1-D1；恒 202、令牌 30 分钟单次使用、重置后要求重新登录；申请/确认分别按 IP 限流）；平台账号由初始化脚本创建并维护邮箱；学生由校管重置并审计（M2 交付，依赖 M1-E 权限矩阵）。

**验收**
- Given 正确凭证，When 登录，Then 返回 access/refresh、tenant_id、角色、must_change_password（如首次）。
- Given 首次登录，When 调用业务 API，Then 除改密/登出外返回 403，直到改密完成。
- Given 连续 5 次错误，When 第 6 次登录，Then 账号锁定 15 分钟，对外统一返回凭证错误文案，审计记录真实原因（locked）。
- Given refresh token 被复用，When 刷新，Then 该 token 家族全部撤销，要求重新登录。
- Given 改密/登出/账号删除，When 操作完成，Then 所有 refresh token 失效。
- Given 弱密码/含学号，When 设置密码，Then 拒绝并返回策略原因。
- Given 员工/平台账号请求重置，When 提交学校编号+登录名（或平台账号），Then 恒返回 202；账号存在且邮件可用时发送一次性链接，不存在时不发送且不泄露。
- Given 密码重置令牌，When 确认重置，Then 新密码满足策略且不在最近 5 次历史；旧密码/全部会话失效，令牌只能使用一次，需重新登录。
- Given 同一 IP 连续提交无效重置令牌，When 超过确认限流阈值，Then 返回 429 且不写失败审计（避免未认证请求放大审计）。
- Given 同一 IP 连续申请重置，When 超过申请限流阈值，Then 仍返回 202 且不发信/不写审计（静默丢弃，避免放大写入）。
- Given 部署在反向代理后，When 客户端伪造 `X-Forwarded-For`，Then 只有可信代理白名单内的直连对端可提供该头，否则按直连地址限流/审计。
- Given MFA 开启，When 登录，Then 需要 TOTP；错误验证码计入失败次数。

#### AUTH-06 会话设备管理（A31，2026-10-21 用户确认：策略 C）
- 每个账号可同时保持的登录设备数有上限（默认：C 端学生/教师 3 台、校方 B 端 3 台、平台端 2 台；按 realm 配置）。
- 超过上限再次登录：**踢掉该账号最久未使用的那台设备**（作废其登录链并立即失效其短期票），新登录成功；写审计。
- 用户可自助查看自己的登录设备（设备标识、最近使用时间、是否当前设备）并**下线指定设备**或**一键退出其它设备**；操作写审计。
- 被踢/被下线的设备在下一次请求即收到 401 并提示重新登录（不等 15 分钟自然过期）。
- 与既有语义保持一致：改密/停用/撤权/删除仍**全部设备**下线；登出只影响当前设备。

**验收**
- Given 账号已在上限台设备登录，When 第 N+1 台登录，Then 最久未使用的那台下一次请求 401，新设备可正常使用，审计含被踢设备标识。
- Given 用户查看登录设备，Then 列出全部在线设备（含时间与设备标识），当前设备被标记。
- Given 用户下线某台设备，Then 该设备下一请求 401，其它设备不受影响。
- Given 用户点"退出其它设备"，Then 仅保留当前设备。
- Given 设备标识，Then 不落明文 IP/UA（只存哈希/粗粒度标签），响应也不回显明文。

### 3.4 权限 RBAC-01～03

- 角色：SaaS 超管/运营/审计；校方超管/授权管理员；教师（B 端权限）；C 端教师/学生。
- 权限：查看/新增/编辑/删除/审批/导出/配置；数据范围：全校/班级/课程/个人。
- 所有鉴权在服务端；不信任前端；跨租默认拒绝。
- 审批职责分离：提交人不能审批自己提交的事项；超管例外默认关闭，开启需理由+审计；SaaS 审核员不得审核自己提交的学校。

**验收**
- Given 教师只有班级 A 数据范围，When 查询班级 B 学生，Then 403/404，且无数据泄露。
- Given 校方管理员无导出权限，When 调用导出，Then 403。
- Given 提交人尝试审批自己的知识库/请假/删除申请，When 审批，Then 拒绝；超管例外未开启同样拒绝。
- Given 超管开启例外，When 审批自己的事项，Then 必须填理由、写审计、通知平台审计。
- Given 跨租请求，When 访问他校资源，Then 404（不暴露存在性）并记录安全事件。

### 3.5 组织 ORG-01～05

- 教师字段：员工号、姓名、年龄、性别、职位、任教班级、任教课程、身份证号等。
- 学生字段：学号、姓名、班级、性别、班主任、辅导员、身份证号等。
- 手动录入 + CSV/XLSX 异步导入；错误行报告、重复策略、失败重试。
- 身份证可选采集；AES-GCM 加密 + HMAC 查重；默认掩码前 3 后 4；完整查看需权限+理由+审计。
- 可选 HTTP 人员核查适配器（默认关闭，超时降级，人工确认）；MCP 二期。

**验收**
- Given 导入文件含重复学号，When 导入，Then 按重复策略处理，错误行报告含行号/原因，不产生半成品数据。
- Given 导入 1000 行，When 异步任务完成，Then 进度/结果可查，失败行可重试。
- Given 查看身份证，When 无 `personnel.sensitive.read` 权限，Then 只返回前 3 后 4；有权限也需填理由并审计。
- Given 删除权限账号，When 操作完成，Then 该账号无法登录，既有 token 失效，审计写入。
- Given 外部核查接口超时，When 录入，Then 降级为人工确认，不阻塞导入。

#### ORG-05 部门/科室与教师归属（A30）
- 新增部门树 `departments`（支持层级）；教师任职关系增加部门归属；学生沿用班级 + 年级（`classes.grade`）。
- 部门与学生归属纳入 CSV/XLSX 异步导入（复用 ORG-02），错误行报告；维护权限 `school.org.manage`。
- 该数据是 KB-09 人群选择器（部门/角色/班级/年级/课程/指定人）的唯一数据源；前端不参与人群判定。

**验收**
- Given 导入文件含未知部门名，When 导入，Then 错误行报告含行号/原因，不产生半成品数据。
- Given 教师调岗改部门，When 保存，Then 审计记录变更前后值，且该教师的检索人群集合按新部门生效（缓存失效）。
- Given 已用父部门配置文档人群，When 子部门成员提问，Then 命中（层级包含）。
- Given 学生未分班，When 提问，Then 其人群集合仅含自身/角色，除"全体/指定人"外不召回。

### 3.6 知识库 KB-01～10

#### KB-01 上传
- 白名单 PDF/MD/DOCX/PPT(X)/TXT；15 个/批、50MB/文件、300MB/批；魔数+MIME+ZIP 炸弹校验；ClamAV 异步 fail-closed。
- 上传后只落库、扫描、元数据登记；不解析、不切块、不嵌入。

**验收**
- Given 未扫描/扫描失败文件，When 提交审批，Then 拒绝，状态 SCAN_FAILED/隔离。
- Given 超过 15 个或超大小，When 上传，Then 拒绝并提示限制。
- Given 合法文件，When 上传完成，Then 状态 UPLOADED→PENDING_APPROVAL（扫描通过后）。

#### KB-02 审批与构建门禁
- 按单个文档版本审批；一次上传 15 个生成 15 条记录。
- 只有 APPROVED 能创建构建任务；未审批调用返回 409 并审计。
- 审批记录绑定 document_version_id，含提交人/审批人/时间/结果/理由/附件/版本快照；驳回必填理由；提交人不能自批。
- 内容修改生成新版本并重审；标签/描述免审但审计。

**验收**
- Given PENDING_APPROVAL，When 创建构建任务，Then 409，无构建任务，审计记录。
- Given REJECTED，When 查看看法，Then 显示理由/审批人；不能构建；编辑后生成新版本并回到 PENDING_APPROVAL。
- Given APPROVED，When 创建构建任务，Then 允许，状态 BUILD_QUEUED。
- Given 已发布文档修改正文，When 保存，Then 新版本 PENDING_APPROVAL，旧版本仍在线。
- Given 仅修改标签/描述，When 保存，Then 不重审，但审计记录变更前后值。

#### KB-03 构建进度
- 状态：BUILD_QUEUED→PARSING→NORMALIZING→CHUNKING→EMBEDDING→INDEXING→VALIDATING→READY/FAILED。
- 字段：status/stage/progress/processed_units/total_units/tier/fallback_reason/started_at/updated_at/finished_at/retry_count/error_code/message。
- SSE 事件 `build.stage/build.progress/build.log/build.done/build.error`；断线 Last-Event-ID/since；轮询 3s 兜底；心跳 15s；Nginx 关闭缓冲。
- 权限 `school.kb.view` + 数据范围；跨租 404；启动/重试/取消审计。

**验收**
- Given 构建进行中，When B 端打开详情，Then 显示当前阶段、进度、已处理/总数、耗时、tier、降级原因；SSE 实时更新。
- Given 浏览器断线重连，When 重新连接，Then 从 Last-Event-ID/since 续传，不丢最终状态。
- Given SSE 不可用，When 打开页面，Then 3s 轮询快照，状态仍正确。
- Given Worker 崩溃，When 心跳超时，Then 任务标记 FAILED/STALE，可重试。
- Given 同一文档版本已有活跃构建，When 再次创建，Then 拒绝重复任务。

#### KB-04 测试沙箱
- 校方上传测试集（CSV/XLSX/JSON：问题、期望答案/要点、期望引用、类别、对抗标记）。
- 使用草稿配置+标记会话，同一运行时；禁止另写 Agent。
- 测试不通过不能发布；失败样例入回归。

**验收**
- Given 未通过测试，When 点击发布，Then 拒绝。
- Given 测试集含 20 条，When 运行，Then 返回通过率、失败样例、引用命中、延迟；报告可查。
- Given 标记会话，When 测试，Then 不影响生产会话/统计。
- Given 测试集格式错误，When 上传，Then 返回行级错误，不启动评估。

#### KB-05 发布/热更新
- 发布前必须 TEST_PASSED；alias 原子切换；新会话新版本、旧会话固定；旧 collection 保留至会话结束/7 天；回滚只切 alias；缓存 Key 含 tenant+kb+version。
- 下线/删除不影响在线会话。

**验收**
- Given 新版本测试通过，When 发布，Then alias 指向新 collection；新会话用新版本，旧会话仍用旧版本。
- Given 发布/回滚期间，When 200 并发请求，Then 零 5xx，引用版本正确。
- Given 下线/删除，When 旧会话继续提问，Then 仍可引用旧版本；7 天后清理。
- Given 回滚，When 新会话提问，Then 使用回滚后的版本，缓存失效。

#### KB-06/07/08 分块预览/检索测试/版本切换
- 父/子块树、详情、统计、搜索筛选；检索测试展示 dense/sparse/RRF/rerank/最终引用；版本切换 MVP。
- 权限 `school.kb.view_chunks`；跨租拒绝；渲染净化；大文档分页/虚拟滚动。

**验收**
- Given 构建 READY，When 打开分块预览，Then 列表/详情/统计正确，表格/代码未被拆坏。
- Given 搜索关键词/章节，When 筛选，Then 只返回匹配块，分页正确。
- Given 检索测试 query，When 运行，Then 展示 top-k 分数与最终引用，能定位“为什么召回”。
- Given 切换到旧版本，When 查看分块，Then 展示该版本分块，不混入新版本。
- Given 跨租 chunk_id，When 访问，Then 404。

#### KB-09 文档查阅人群 ACL 与检索强制过滤（K4）
- 版本级 ACL 表 `kb_document_audiences`：`audience_type ∈ {department, role, class, grade, course, user, all}`，多条为**并集**；`all` 必须显式选择。
- **fail-closed**：版本 ACL 记录为 0 条时构建/发布返回 `409 ACL_REQUIRED`；已配置未确认返回 `409 ACL_UNCONFIRMED`。
- 新版本默认继承上一版本 ACL 但需重新确认；派生版本（KB-10）沿用源版本已确认 ACL。
- ACL 变更即时生效，**不受"旧会话固定版本"保护**（安全策略优先）；权限 `school.kb.acl.manage`，审计 `kb.acl.updated` / `kb.acl.confirmed`。
- 构建时把 ACL 写入 Qdrant payload（token 形如 `department:<uuid>`）；检索按登录身份解析的人群集合过滤，要求至少命中一个 token（`min_should: 1`）。
- 检索测试支持"以某用户/人群视角"运行；校方超管可用"忽略 ACL"对比视图（`school.kb.retrieval_test.unfiltered`，强制审计）。
- 无权行为：不召回、不引用、逐项兜底话术，不提示"存在但无权"。

**验收**
- Given 版本未配置 ACL，When 构建或发布，Then 409 并审计；Given 已配置未确认，Then 409 `ACL_UNCONFIRMED`。
- Given 财务部门文档，When 非财务用户提问，Then 召回集为空、引用为空、回答为兜底话术；财务用户提问则召回并给出引用。
- Given 教师任教两个班且其中一个班在 ACL 内，When 提问，Then 命中（并集语义）。
- Given ACL 收紧（移除某部门），When 旧会话继续提问，Then 下一次请求立即按新 ACL，不被会话固定版本放宽。
- Given 跨租 `version_id`/`audience_id`，When 访问或检索，Then 403/404，无跨租命中。
- Given 构建完成，When 检查 Qdrant payload，Then 每个 chunk 携带该版本 ACL token 列表（无 token 的版本不可发布）。

#### KB-10 切块编辑与派生重建（K5）
- 仅支持编辑**子块文本**；不支持增删块、改父块归属或调整父子结构（此类走"重传文档/重切块 + 重审"）。
- 编辑提交带 `base_hash` 乐观锁，冲突返回 `409 CHUNK_CONFLICT`；编辑理由必填；权限 `school.kb.chunk.edit`。
- 派生版本：`parent_version_id` + `derivation_type='chunk_edit'`，复制全部分块，仅改动块重新嵌入，未改块复用向量缓存（以 `chunk_hash` 为键）。
- 轻量确认：校方超管 `school.kb.chunk.confirm` + 审计，不重走平台审批；允许编辑人=确认人（审计分别记录）。
- 派生构建跳过解析与切块阶段（COPY → EMBED（改动块）→ INDEX → VALIDATE → READY）；失败可重试，不影响源版本与线上 alias。
- 发布后 alias 切流；旧会话继续固定源版本；`is_human_edited` 写入 payload 与分块详情。
- 校验：非空、长度上限、Markdown 结构完整（表格/代码块闭合）、`chunk_hash` 与 token 数重算、统计与检索测试重跑。

**验收**
- Given 已发布版本，When 编辑子块并确认派生，Then 派生版本 READY 且**仅改动块重嵌**（嵌入调用次数 = 改动块数），未改块向量复用。
- Given 派生版本发布，When 新会话提问，Then 使用新版本；旧会话仍引用源版本（不漂移）。
- Given 两处并发编辑同一子块，Then 后者 409 `CHUNK_CONFLICT` 且审计完整。
- Given 编辑内容破坏表格/代码结构或超出长度，Then 422 拒绝保存。
- Given 派生版本未确认，When 触发构建，Then 409。
- Given 派生构建失败，When 重试，Then 线上 alias 与源版本不受影响。
- Given 编辑后的 chunk，When 查看分块详情，Then 显示人工编辑标记与编辑人/时间。

### 3.7 AI AI-01～10

#### AI-01 快路径/ReAct/多意图
- 快路径：route→retrieve→answer；ReAct：最多 5 次模型决策、3 次工具、2 次检索、30s、12k token。
- `RouteResult.intents[]` 支持多意图；一次回答覆盖多个意图；不支持意图逐项兜底。

**验收**
- Given 简单 KB 问题，When 提问，Then 走快路径，首答案 token P95≤3s。
- Given 复杂多跳问题，When 提问，Then ReAct 在预算内完成；首状态≤3s、首答案≤5s。
- Given 同一请求包含“请假流程+学校规定”，When 提问，Then 一个回答覆盖两个意图，不整题拒答。
- Given 第 4 次工具调用/第 6 次决策，When 继续，Then 停止并兜底，记录 stop_reason。

#### AI-02 流式
- SSE 事件 `meta/step/tool_call/tool_result/reasoning_summary/delta/citation/done/error`。
- 支持停止生成、断线重连、部分回答标记；流中失败不把半截答案当完整答案。

**验收**
- Given 正常回答，When 流式，Then 事件顺序正确，`delta` 增量拼接等于最终答案，`done` 含 tokens/耗时/stop_reason。
- Given 用户点击停止，When 中断，Then 下游 LLM/工具取消，前端保留已生成部分并标记停止。
- Given 流中上游失败，When 错误，Then 发 `error` 且标记部分回答，可重试。
- Given 引用出现，When 推送，Then `citation` 含编号/chunk_id/文档名/页码/章节。

#### AI-03 思考摘要
- 只展示脱敏步骤/摘要；开关可关闭；不展示原始 CoT/系统提示词/工具参数/内网地址/PII/密钥；不伪造。
- 原始 reasoning 默认不落库、不外送 Langfuse。

**验收**
- Given 开关开启，When 回答，Then 显示“正在检索/核对引用”等摘要。
- Given 开关关闭，When 回答，Then 不显示 `reasoning_summary`，只显示状态。
- Given provider 不返回 reasoning，When 回答，Then 只显示状态，不生成虚假思考内容。
- Given 包含系统提示词/内网地址的输出，When 护栏，Then 替换为安全话术并记录事件。

#### AI-04 引用
- 引用来源随答复展示；引用 chunk_id 必须真实存在、属于当前租户/版本、与答案相关。
- 无证据时拒答/兜底，不编造。

**验收**
- Given 有证据，When 回答，Then 至少一个有效引用，点击可查看文档名/页码/章节。
- Given 无证据，When 回答，Then 拒答/兜底，不生成假引用。
- Given 模型生成不存在 chunk_id，When 校验，Then 修复或降级为安全话术，记录事件。

#### AI-05 反馈
- 有用/无用 + 原因标签 + 可选文本；关联 conversation/turn/trace；提交即知情同意；脱敏；可删除匿名化。

**验收**
- Given 用户反馈，When 提交，Then 落库、关联 trace、返回成功；重复提交幂等。
- Given 反馈内容，When 校方查看，Then 仅本校授权用户可见；标记处理/答复留审计。
- Given 用户删除账号/申请删除反馈，When 到期，Then 反馈内容匿名化。

#### AI-06/07 记忆/清空
- MVP 近 10 轮短期记忆+滚动摘要；固定 kb_version；不做长期记忆。
- 清空当前会话记录；不删除审计/统计。

**验收**
- Given 第 11 轮，When 提问，Then 使用最近 10 轮+摘要，不丢失关键上下文。
- Given 清空当前会话，When 确认，Then 当前会话消息清空/标记删除，审计记录，其他会话不受影响。
- Given 新会话，When 提问，Then 不继承旧会话上下文。

#### AI-08 兜底
- 不回复“无法理解”；给出切换知识库范围、联系校方管理员、提交反馈、可用功能入口。
- 多意图逐项兜底。

**验收**
- Given 无可用知识库，When 提问，Then 返回可执行替代路径，不裸报错。
- Given 一个意图不支持、另一个支持，When 回答，Then 支持的部分正常回答，不支持的部分给兜底。
- Given 外部依赖超时，When 回答，Then 友好降级话术+重试建议。

#### AI-09 护栏
- 输入：SQL 注入、XSS、提示词注入、命令注入、长度边界；输出：跨用户/跨租 PII、系统提示词、工具/密钥/内网地址、XSS、伪造引用、违法/自伤不当内容。
- 命中：安全话术+事件+指标；自伤给支持性话术+线下求助，不诊断。

**验收**
- Given 提示词注入“忽略以上指令”，When 提问，Then 不泄露系统提示词/工具，记录 guard 事件。
- Given 跨租诱导，When 提问，Then 拒绝并记录安全事件。
- Given 自伤内容，When 回答，Then 支持性话术+求助渠道，不做诊断。
- Given 输出含系统提示词/内网地址，When 护栏，Then 替换并记录。

#### AI-10 身份→人群解析与检索过滤（K4）
- 服务端按登录身份解析人群集合：B 端 = 部门 + 角色 + 任课/班主任/辅导员班级 + 授课课程 + 自身；C 端学生 = 班级 + 年级 + 自身；C 端教师 = 部门 + 任课班级 + 自身；平台 realm 不参与租户知识库检索。
- 请求体中的任何人群/部门/租户字段一律忽略（不信任前端）；解析结果短 TTL 缓存，组织/权限变更时失效。
- 解析失败或结果为空 → 视作空集（fail-closed），宁可不召回也不放行。

**验收**
- Given 非财务用户提问财务文档，Then 无召回、无引用、兜底话术，且回答/引用/错误信息中不出现文档名。
- Given 教师调岗，When 组织变更后提问，Then 按新部门过滤（缓存已失效）。
- Given 请求体携带伪造 `audience_ids`/`tenant_id`，Then 被忽略且不影响过滤结果。
- Given 学生未分班，Then 仅能命中"全体/指定人"文档。

### 3.8 审批 APV-01～05

- 通用审批引擎：类型注册、提交人/租户/审批人/时间/结果/理由/附件/状态流转。
- 类型：KB 文档、请假、账号删除/撤权（MVP）；MCP/SKILLS、公告、皮肤、定制化、创作（二期/三期）。
- 待办/已办/抄送/消息通知/超时提醒；驳回必填理由；所有操作审计；禁止自批；超管例外默认关闭。
- 超时默认 24h 提醒、48h 升级，按类型可配。

**验收**
- Given 提交人，When 审批自己的事项，Then 拒绝；例外未开启同样拒绝。
- Given 驳回，When 未填理由，Then 失败；填写后可驳回，提交人收到通知。
- Given 审批超时 24h，When 定时任务，Then 提醒；48h 升级；审计。
- Given 审批通过，When 状态流转，Then 相关业务动作执行（KB 可构建、请假生效、删除进入保留期），通知/审计。
- Given 审批记录，When 查看，Then 含附件元数据、审批人、时间、结果、理由。

### 3.9 请假 LEAVE-01/02

- 学生选择班主任/辅导员，填写缘由≥50 字、姓名、班级、学号、天数。
- 草稿→待审批→通过/拒绝；草稿可编辑/删除；待审批可撤回；通过后只读预览。
- 审批在 B 端审批中心；班主任/辅导员需 B 端审批权限。
- 审批记录含审批人/时间/理由；通知/审计。

**验收**
- Given 学生提交，When 通过校验，Then 状态 PENDING；班主任/辅导员收到待办+通知。
- Given 待审批，When 撤回，Then 状态 WITHDRAWN，不可再审批。
- Given 通过，When 再次编辑，Then 拒绝；只能只读预览。
- Given 拒绝，When 学生查看，Then 显示理由/审批人/时间。
- Given 非班主任/辅导员，When 尝试审批，Then 403。
- Given 请假天数/缘由非法，When 提交，Then 返回字段级错误。

### 3.10 消息 MSG-01～05

- 站内信+SMTP 邮件；outbox+3 次指数退避+死信+后台重发。
- 平台/租户通道隔离；禁止租户间直发；跨通道记录来源租户。
- B 端已读 MVP；C 端已读二期。
- 反馈支持标记处理+站内信答复；C 端消息接收。

**验收**
- Given 发送消息，When 站内信成功、邮件失败，Then 站内信可见，邮件进入 outbox 重试 3 次，最终死信可重发。
- Given 平台消息，When 校方查看，Then 通道标识为 PLATFORM，不能回复到其他租户。
- Given 租户 A 向租户 B 直发，When 请求，Then 拒绝。
- Given B 端打开消息，When 查看，Then 已读状态更新并审计。
- Given 校方回复反馈，When 发送，Then C 端收到站内信，反馈状态更新，审计写入。

### 3.11 账号删除 DEL-01～05

- 双入口：用户自助 + 管理员撤销登录权限；共用状态机。
- 审批通过后 5 天保留期内可登录，但只能申诉/只读，所有写操作 403。
- 冻结=暂停删除倒计时、转人工复核；账号仍只读。
- 到期删除/匿名化；删除证明；拒绝恢复；唯一超管由 SaaS 复核。
- 业务数据按 A5 留存矩阵保留/匿名化。

**验收**
- Given 用户自助申请，When 提交，Then 进入审批；不能自批。
- Given 管理员撤权，When 提交，Then 被撤权人收到通知；可申诉/冻结。
- Given 审批通过，When 保留期内登录，Then 现有会话失效，可重新登录但只签发只读会话；只能申诉/只读，写接口 403。
- Given 保留期内冻结，When 冻结，Then 倒计时暂停；管理员复核后恢复/延长。
- Given 5 天到期，When 任务执行，Then 账号删除/匿名化，生成删除证明；审计保留。
- Given 唯一超管申请，When 本校无人可批，Then 转 SaaS 复核。
- Given 拒绝，When 流程结束，Then 账号恢复原状态，通知/审计。

### 3.12 看板 DASH-01/02

- 看板：人员数、知识库文档状态、待办审批、未读消息、AI 反馈数量。
- 反馈查看：列表、时间/评分筛选、问题/答案摘要/引用（按权限）、标记已处理、站内信答复。
- 权限 `school.dashboard.view`、`school.feedback.view`、`school.feedback.update`；数据范围；访问审计。
- 不做词频/趋势/复杂 BI/反馈导出。

**验收**
- Given 人员/文档/审批/消息变化，When 打开看板，Then 统计与数据库一致（允许缓存 60s，需标注）。
- Given 反馈列表，When 校方查看，Then 只看到本校数据；用户标识脱敏；引用按权限展示。
- Given 标记已处理/答复，When 操作，Then 状态更新、C 端收到消息、审计写入。
- Given 跨租/无权限，When 查看看板/反馈，Then 403/404。

### 3.13 SaaS SAAS-01～05

- 模型配置：LLM/VLM/嵌入/重排 api-key/base-url/model-name，加密存储，界面/日志不回显。
- 全局/租户切块/检索/限流/配额；DB 连接走环境变量，不进 UI。
- 配置变更审批或双人确认，记录前后值/操作人/时间；生产至少两个平台特权账号；单账号走 break-glass。
- 租户审核记录查询/导出/审计；平台消息中心；租户审核开关。

**验收**
- Given 保存模型密钥，When 查看/日志，Then 只显示掩码，不显示明文。
- Given 配置变更，When 无第二人确认/审批，Then 不能生效（或 break-glass 需原因+审计+事后复核）。
- Given 审核记录，When 导出，Then 含审核人/时间/结果/理由/凭证快照，导出审计。
- Given 生产环境，When 尝试开启跳过审核，Then 拒绝。
- Given 平台消息，When 校方查看，Then 通道隔离、来源租户正确。

### 3.14 审计 AUD-01

- 审计仅追加；哈希链；DB 角色禁 UPDATE/DELETE；每日 WORM 导出；在线 365 天；敏感读审计。
- 记录 actor/tenant/action/resource/result/ip/ua/request_id；敏感值脱敏。

**验收**
- Given 任何写操作/审批/敏感读，When 完成，Then 审计记录写入，链哈希正确。
- Given 尝试 UPDATE/DELETE 审计，When 执行，Then 权限拒绝。
- Given 导出 WORM，When 校验，Then 哈希链连续；篡改可检测。
- Given 跨租审计查询，When 非平台授权，Then 拒绝并审计。

### 3.15 观测 OBS-01～03

- 本地 trace 表/JSONL 必选；Langfuse **Server 4.63.0 + Python SDK 5.0.2** 可选异步上报（OTel；Server ≥4.59.0）；Ragas 独立评估。
- Langfuse 默认元数据+哈希；内容采集仅 KB 测试/C 端反馈/定制化场景；平台/运维专属；Git 是 prompt 唯一事实来源。
- Langfuse 故障不影响聊天；非审计日志；v4 读取用 Observations API v2 / Metrics API v2 / Scores API v3。

**验收**
- Given 任意 LLM 回合，When 完成，Then 本地 trace 含 trace_id、模型、token、延迟、chunk_id、工具、guard、stop_reason。
- Given Langfuse 关闭，When 聊天，Then 正常回答，无报错。
- Given Langfuse 开启，When 回合完成，Then trace/generation/retrieval/tool/guard 映射正确；tenant_hash 正确。
- Given C 端反馈，When 提交，Then 知情同意记录、脱敏内容、trace 关联；删除后匿名化。
- Given KB 测试/定制化，When 运行 Ragas，Then 报告生成；Langfuse dataset/experiment 可选同步。
- Given Langfuse 故障，When 上报，Then 重试/丢弃，聊天不受影响。

### 3.16 非功能 NFR-01～04

- 性能：核心 API P95≤500ms（不含 AI）；快路径首 token≤3s；ReAct 首状态≤3s/首答案≤5s；非 AI 200 并发；AI 50 流；发布/回滚零 5xx。
- 安全：JWT/密码/幂等/审计/脱敏/上传/密钥/输入输出护栏/租户隔离/留存。
- 备份：每日全量+WAL；180 天；RPO 24h（WAL 15min）、RTO 4h；月度恢复演练。
- 隔离：逐表/Qdrant/MinIO/缓存跨租拒绝；A23 场景。
- 压测脚本与报告入 `benchmarks/`，含并发数/P50/P95/P99/错误率/资源占用。

**验收**
- Given k6 200 并发非 AI 接口，When 压测，Then P95≤500ms、错误率达标；报告含资源占用。
- Given SSE 50 流，When 压测，Then 首 token 指标按快路径/ReAct 分别报告。
- Given 发布/回滚，When 200 并发请求，Then 零 5xx。
- Given 备份，When 恢复演练，Then RPO/RTO 达标；恢复数据一致。
- Given 跨租测试矩阵，When 全量执行，Then 所有跨租读写删/缓存/对象/向量访问被拒绝。

---

## 4. 决策记录

### 4.1 技术/架构

| 决策 | 结论 | 理由 |
|---|---|---|
| Python 版本 | 3.13.x | 用户指定；MinerU/LangGraph 支持；M0 验证所有依赖 |
| 后端 | FastAPI + SQLAlchemy 2 async | 异步、SSE、OpenAPI、生态成熟 |
| Agent | LangGraph + 自定义 Harness | LangGraph 只做图执行；Harness 负责契约/护栏/审计/观测 |
| 向量库 | Qdrant | 原生 dense+sparse、每租户/版本 collection+alias |
| 解析 | MinerU 4.0.12 | 最新稳定；V1 API；质量档位；Python 3.13 支持 |
| 前端 | Vue3 + Element Plus + Vant4 H5 | 管理端组件成熟；H5 最快；小程序二期 |
| 观测 | Langfuse Server 4.63.0 + Python SDK 5.0.2，独立主机 | 数据不出域；不占应用主机；观测/评估/Prompt；v4 OTel |
| Prompt | Git 唯一事实来源 | 避免双源；Langfuse 只做草稿/实验 |

### 4.2 产品/流程

| 决策 | 结论 |
|---|---|
| KB 审批粒度 | 单文档版本；**原文/结构变更**（重传、重切块、增删块）重审；**块级文本校正**走派生版本 + 校方超管轻量确认 + 审计（K5）；标签/描述/ACL 变更免审但强制审计 |
| 文档查阅人群（ACL） | 版本级 ACL（部门/角色/班级/年级/课程/指定人/全体，并集）；fail-closed；构建/发布前必须确认；检索层强制过滤（K4） |
| 切块编辑 | 仅子块文本；派生版本局部重嵌 + 向量复用；旧会话固定源版本（K5） |
| 部门组织单元 | 独立 `departments` 树 + `classes.grade`；教师归属部门；作为人群选择器数据源（A30） |
| 会话设备管理 | 策略 C：按 realm 限制同时登录设备数（默认 3/3/2），超限踢最久未用；用户可查看设备、下线单台、退出其它设备；被踢即时失效（A31） |
| KB 构建门禁 | APPROVED 才能构建；未审批 409 |
| 构建进度 | SSE+轮询；阶段/进度/tier/日志/重试 |
| 分块预览 | 列表/详情/统计/搜索/检索测试/版本切换；diff 二期 |
| C 端流式 | SSE；快路径 3s/ReAct 5s |
| 思考展示 | 脱敏步骤/摘要；开关；不展示原始 CoT |
| 账号删除 | 双入口；5 天只读；冻结暂停倒计时；删除证明 |
| C/B 已读 | C 端二期；B 端 MVP |
| 反馈答复 | B 端标记处理+站内信答复 |
| 多意图 | intents[]；合并作答；逐项兜底 |
| 看板/反馈 | MVP 基础看板+反馈查看/答复 |
| 创作中心 | 三期；教师+学生 |
| Langfuse 采集 | 默认元数据；KB 测试/反馈/定制化场景可开内容 |
| 代码审查 | 每功能点强制；修复复测复审 |
| 业务场景测试 | 主门禁；断言业务输出 |

### 4.3 待确认假设

- 新增决策编号：A30（部门组织单元）、K4（文档查阅人群 ACL）、K5（切块编辑与派生重建）、A31（会话设备管理策略 C：限量 + 退出其它设备），2026-10-21 用户确认；A30/K4/K5 详见 `docs/spec-changes/SC-01-kb-acl-chunk-edit.md`，A31 详见 `docs/design-briefs/M1-C2-session-devices.md`。
- A7/A8 默认值、A15 目录结构、A19 配额、A21 审计存储、A22 RPO/RTO、A25 超时、A27 预留边界、R7 配置双人确认、R8 MFA 策略：按计划默认值执行，用户有异议需指出。
- 服务器规格/域名/证书待提供。
- 切块参数 M3 实验后定。
- 词频榜/盲盒/账号删除完整流程阶段冲突待确认。
- 角色权限矩阵初版见 `docs/rbac-matrix.md`，需逐项确认。

---

## 5. 数据留存矩阵

| 数据 | 留存期 | 到期处理 | 备注 |
|---|---|---|---|
| 审计日志 | 365 天（最低 180） | WORM 归档/清除 | 仅追加、哈希链 |
| AI 会话/消息 | 180 天 | 删除/匿名化 | 账号删除时提前匿名化 |
| 审批/请假记录 | 3 年 | 匿名化 | 合规追溯 |
| 用户账号 | 删除审批通过后 5 天保留期 | 删除/匿名化 | 保留期内只读+申诉 |
| 备份 | 180 天 | 随生命周期删除 | 删除证明注明备份到期 |
| Langfuse trace | 180 天 | 删除 | 默认元数据+哈希 |
| KB 测试集 | KB/请求生命周期 | 删除 | tenant-scoped |
| 反馈内容 | 180 天或账号删除 | 匿名化 | 提交即知情同意 |
| 上传文件 | 文档生命周期 | 删除 | MinIO 租户前缀 |

> 删除证明 = 审计事件 + 删除范围哈希 + 时间戳 + 备份到期说明。

---

## 6. 术语表

| 术语 | 定义 |
|---|---|
| 租户 | 一所学校；以学校编号标识 |
| 平台方 | SaaS 运营/审核/审计人员 |
| B 端 | 校方管理后台 |
| C 端 | 学生/教师 H5 |
| KB | 学校知识库 |
| 审批门禁 | 未审批通过不得构建/发布 |
| Harness | 产品级 Agent 外壳：契约/工具/护栏/审计/观测/恢复 |
| Loop | 有界对话回合：快路径 + 多步 ReAct |
| Ragas | LLM 评估框架 |
| Langfuse | LLM 观测/评估/Prompt 实验平台 |
| SLO | 服务目标（P95/错误率等） |

---

## 7. 追踪矩阵

| 里程碑 | 功能点 |
|---|---|
| M0 | SYS-01、OBS-01、AUTH 基座、测试基础设施 |
| M1 | TEN-01/02/03、AUTH-01～05、RBAC-01～03、AUD-01 基座 |
| M2 | ORG-01～04 |
| M3 | KB-01～08、APV-02、OBS-02/03（可选） |
| M4 | AI-01～09、OBS-02/03、DASH-02 采集侧 |
| M5 | APV-01/03/04/05、LEAVE-01/02、MSG-01～05、DEL-01～05 |
| M6 | SAAS-01～05、DASH-01/02、NFR-01～04、OBS-02 生产 |

> 详细测试场景见 `docs/test-plan.md`；状态机见 `docs/state-machines.md`；权限矩阵见 `docs/rbac-matrix.md`。
