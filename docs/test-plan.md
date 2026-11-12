# 校园陪伴精灵 — 测试计划（test-plan.md）

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-10-19 |
| 状态 | 待用户确认 |
| 上游 | `docs/spec.md`、`docs/design.md` |
| 规则 | 业务场景测试是主门禁；每个功能点测试通过后做代码审查，修复后重跑全量+业务场景 |

---

## 1. 测试目标

1. **代码层正确性**：单元/集成测试覆盖逻辑、事务、并发、异常。
2. **业务输出正确性**：业务场景测试断言最终状态、副作用、审计、通知、存储、向量、AI 输出。
3. **隔离与安全**：跨租、越权、注入、PII、密钥、上传、输出护栏。
4. **AI 质量**：引用、拒答、忠实性、能力缺失、多意图、ReAct 预算。
5. **性能**：核心 API、SSE、并发、发布/回滚。
6. **可运维**：备份恢复、故障降级、Langfuse 故障隔离。

---

## 2. 测试分层

| 层级 | 工具/环境 | 是否进 CI | 说明 |
|---|---|---|---|
| 单元 | pytest，纯函数/内存 | 是 | 状态机、校验器、权限规则、切块/掩码、SSE 映射 |
| 集成 | pytest + 真实 PG/Redis/MinIO/Qdrant；LLM/嵌入/重排 mock | 是 | 接线、事务、幂等、租户过滤、异步任务 |
| 业务场景 | pytest + 真实基础设施；外部 LLM/嵌入/重排 mock；MailHog | 是 | 从 Given/When/Then 跑完整链路，断言业务输出 |
| E2E | Playwright + 真实后端/基础设施 | 是（关键路径） | 三端登录、KB 全链路、C 端流式、请假、删除、审批、看板 |
| AI 评估 | Ragas + 真实 LLM；独立跑批 | 否（独立任务） | 黄金集、回归、失败样例 |
| 性能 | k6 | 否（里程碑/发布前） | 并发、P95/P99、错误率、资源 |
| 安全 | bandit/pip-audit/pnpm audit/secret 扫描 + 专项场景 | 是 | 依赖漏洞、密钥、注入、越权 |
| 恢复 | 备份恢复演练 | 否（里程碑） | RPO/RTO、数据一致性 |

**外部依赖策略**：单元/集成/业务场景中 LLM、嵌入、重排、外部内容安全 mock；邮件用 MailHog；MinerU 在 M0/M3 用真实容器，CI 可用固定解析结果 mock（解析正确性由 M3 真实场景覆盖）。AI 质量用真实 LLM 独立评估。

---

## 3. 测试环境与数据

- **开发**：Windows 11 + Docker Desktop；16GB/6GB；Langfuse optional。
- **测试**：独立 Compose project、独立数据库/Redis/MinIO/Qdrant、独立密钥；与生产严格隔离。
- **生产**：禁止使用测试数据/测试密钥；`DEV_SKIP_*` 必须 false。
- 测试数据用后即清；每个测试使用唯一 tenant_id/前缀；测试结束清理 DB/Redis/MinIO/Qdrant/临时文件。
- 时间固定/可注入；LLM 评估温度 0、固定 seed 或模型版本锁定。
- 敏感数据使用合成数据；禁止把真实学生身份证/聊天记录放入测试集。

---

## 4. 业务场景矩阵（主门禁）

> 每个功能点至少一条；标 ★ 为高风险，需人工复审。

| ID | 场景 | 关联功能 | 关键业务输出断言 |
|---|---|---|---|
| BS-01 | 学校注册→SaaS 审核→开通 | TEN-01/02 | 租户 active、学校编号唯一、超管账号、邮件、审核记录、审计 |
| BS-02 | 生产放行开关关闭 | TEN-03 | 生产启动失败/健康 red；无法跳过审核 |
| BS-03 ★ | 三端登录/首登改密/锁定 | AUTH-01/03/05 | token、must_change_password、锁定、审计 |
| BS-04 ★ | Refresh 轮换/复用/撤销 | AUTH-02 | 旧 token 失效、family 撤销、登出/改密/删除全撤销 |
| BS-05 ★ | RBAC/数据范围/跨租 | RBAC-01/02 | 教师只见班级/本人；跨租 404；无泄露 |
| BS-06 ★ | 审批职责分离 | RBAC-03/APV-01 | 自批拒绝；超管例外默认关；理由+审计 |
| BS-07 | 人员手动/批量导入 | ORG-01/02 | 账号、角色、错误行、重复策略、失败重试 |
| BS-08 ★ | 身份证加密/掩码/审计 | ORG-04 | 密文存储、前 3 后 4、完整查看需权限+理由+审计 |
| BS-09 | 上传/ClamAV/审批前不解析 | KB-01 | 状态 UPLOADED→PENDING；无 build job；扫描失败隔离。**证据（2026-10-31，KB-01A 后端）**：`tests/business/test_bs_m3_01_kb_upload.py` 4 例 + `tests/integration/test_kb_upload.py` / `tests/integration/test_kb_scan_worker.py` 覆盖整批原子与幂等重放/冲突、感染隔离留证、瞬时故障保持 UPLOADED + 看门狗多租补投、`DEV_SKIP_FILE_SCAN` 直通与审计。**前端证据（2026-10-31，KB-01B）**：`frontend/e2e/admin-kb.spec.ts` 18 例（有状态 mock）覆盖本地预检、整批 422/413/409、`replayed` 重放、上传后轮询到「已通过/待审批」、无 view 不轮询、列表与抽屉的状态/扫描文案、游标翻页、刷新失败横幅、抽屉 404 与竞态；KB-01C 追加 3 条轮询停止分支（详情 403、页面隐藏、5 分钟截止），共 21 例。**全栈证据（2026-11-01，KB-01C，PR #37 → `7d1ce56`）**：`frontend/e2e-fullstack/admin-fullstack-kb.spec.ts` 4 例（真实后端 + 真实库 + dev-skip）覆盖 UI 上传→`PENDING_APPROVAL`/`skipped`/`dev-skip`、元数据与版本、幂等重放/409 不落库、整批 422 原子、上传-only 403/菜单守卫/零详情请求；真实 ClamAV 覆盖延后 TD-284 |
| BS-10 ★ | 单文档版本审批门禁 | KB-02 | 未审批构建 409；拒绝不能构建；原文/结构变更重审；块级校正走派生+确认；标签免审。**后端证据（2026-11-07，KB-02）**：`tests/business/test_bs_m3_02_kb_approval.py` 3 例（通过→构建全链路：待办/已办/快照/审计/兄弟文档不可构建；驳回链路：理由必填且可见/REJECTED 不可构建不可再提交；扫描门禁不因新点放宽——UPLOADED 仍 409、无码 403）；`tests/integration/test_kb_approval.py` 16 例（开单快照/重复提交 409/UPLOADED 409+拒绝审计/通过双写 APPROVED/自批两向 403/撤销可再提交/跨租 404/并发 decide 恰一赢家/构建门禁 409+零行/APPROVED 建任务+二次 409/HTTP 级并发建任务恰一行/元数据免审但审计前后值/标题 200 字边界）；TD-277 集成 2 例在 `tests/integration/test_kb_upload.py`（同键同文件不同 metadata 409 + 同 metadata 重放）+ 指纹纯函数单测；迁移 `d5e9a3c7b1f4` 回滚由 `tests/integration/test_migration_rollback.py` 覆盖。**前端证据（2026-11-07，KB-02B）**：`e2e/admin-approvals.spec.ts` 13 例（待办/已办与游标翻页、详情快照/步骤/留痕、通过一次确认、驳回理由先拦+终态理由可见、自批 403 toast 且状态不变、撤销自己的提交、非提交人无撤销控件、仅 todo 码的缺码提示、无 todo 码 403 守卫、列表失败横幅、详情 404 刷新、409 已被他人处理的竞态刷新）；`e2e/admin-kb.spec.ts` +4（抽屉提交审批成功与会话内禁用、同版本重复提交 409、创建构建成功与重复 409、REJECTED 指引与文案）；单测：`approval-logic.test.ts` 13（判据/文案/错误映射）、api-client `approvals.test.ts` 6（契约形态）、`kb-logic.test.ts` 五档文案。「原文/结构变更重审」「块级校正走派生」依赖 KB-05/KB-10 的再上传与派生路径，届时在此补证据 |
| BS-11 | 构建进度 SSE/轮询/重试 | KB-03 | 阶段/进度/tier/日志；断线续传；失败重试；心跳。**KB-03A 后端证据（2026-11-07）**：`tests/integration/test_kb_build_worker.py` 7 例（真实 PG/Redis/Qdrant + fake parser/embedder：全阶段→READY + chunks/Qdrant/payload 对账、降级链 fallback_reason、全档失败→FAILED→retry 恢复、快照全字段/跨租 404/无 view 403、READY 重试 409+拒绝审计、看门狗 STALE、DOCX 诚实失败）；`tests/business/test_bs_m3_03_kb_build.py` 1 例（**真实 MinerU+TEI+Qdrant 本地 dev 栈**：上传 PDF→审批→构建→READY 全链路，TD-046 兑现）；单测 32（chunker/parser 降级链/embedding IDF/状态机扩展）。SSE/断线续传/前端进度随 KB-03B |
| BS-12 | 构建→分块预览 | KB-06 | 父子块、元数据、表格/代码不拆、统计正确。**TD-310 阶段 1 证据（2026-11-13，PR #67 → `ff0472c`）**：含图 PDF 构建后分块零占位符——`tests/business/test_bs_m3_03b_image_placeholder.py` 2 例（真实 MinerU+TEI+Qdrant 本地 dev 栈：含图 PDF→READY→kb_chunks 零 `data:image`/`![` 文本、图前/图后标记保留、Qdrant 点数对账；纯图 PDF→显式失败 `CHUNK_EMPTY`）、`tests/integration/test_kb_build_worker.py` +1（fake parser 占位符 markdown→READY 后 chunks 干净，stash 验证红相）、单测 `test_kb_chunker_unit.py` +10（5 条核心语义 5 红→绿 + 5 条防误伤守卫）；识别口径与实证依据见简报 `docs/design-briefs/M3-TD310-phase1-image-placeholder.md` |
| BS-13 | 检索测试/版本切换 | KB-07/08 | dense/sparse/RRF/rerank/引用；版本不混用 |
| BS-14 ★ | 测试沙箱/测试集 | KB-04 | 草稿配置+标记会话；未通过不能发布；报告可查 |
| BS-15 ★ | 发布/蓝绿/回滚/旧会话 | KB-05 | alias 正确；新/旧会话版本固定；零 5xx；缓存失效 |
| BS-16 ★ | 下线/删除不影响在线会话 | KB-05 | 旧会话可引用旧版本；7 天清理 |
| BS-17 ★ | C 端快路径流式/引用 | AI-01/02/04 | 事件顺序、首 token、引用真实、done 元数据 |
| BS-18 ★ | ReAct 预算/停止 | AI-01/02 | 5 决策/3 工具/2 检索；超限兜底；stop_reason |
| BS-19 | 多意图一次作答 | AI-01 | 一个回答覆盖多意图；不支持逐项兜底 |
| BS-20 | 思考摘要/开关/脱敏 | AI-03 | 开/关；无原始 CoT；不伪造；脱敏 |
| BS-21 ★ | 无证据拒答/伪造引用 | AI-04 | 拒答/兜底；无效引用降级；无编造 |
| BS-22 ★ | 提示词注入/跨租诱导 | AI-09 | 不泄露系统提示词/工具/跨租数据；guard 事件 |
| BS-23 | 自伤/敏感内容 | AI-09 | 支持性话术+求助；不诊断；audit |
| BS-24 | 能力缺失兜底 | AI-08 | 可执行替代路径；无“无法理解” |
| BS-25 | 短期记忆/清空会话 | AI-06/07 | 10 轮+摘要；清空当前不影响其他；审计 |
| BS-26 | 反馈采集/知情同意 | AI-05 | 反馈落库、trace 关联、脱敏、可删除 |
| BS-27 ★ | 请假全状态机 | LEAVE-01/02 | DRAFT/PENDING/APPROVED/REJECTED/WITHDRAWN；只读；通知 |
| BS-28 ★ | 消息 outbox/重试/通道 | MSG-01/02/03 | 站内信成功、邮件重试/死信；通道隔离；禁止租户直发 |
| BS-29 | B 端已读/反馈答复 | MSG-04/05 | 已读更新；C 端收到答复；审计 |
| BS-30 ★ | 账号删除自助入口 | DEL-01/03 | 审批、5 天只读、写 403、到期删除/证明 |
| BS-31 ★ | 管理员撤权入口 | DEL-02/03 | 通知、申诉、5 天只读、删除/证明 |
| BS-32 ★ | 冻结暂停倒计时 | DEL-04 | 冻结后倒计时暂停；复核恢复/延长；审计 |
| BS-33 ★ | 唯一超管删除 | DEL-01 | 转 SaaS 复核；不能本校自批 |
| BS-34 | 看板统计 | DASH-01 | 与 DB 一致（缓存标注）；权限/租户 |
| BS-35 | 反馈查看/标记/答复 | DASH-02 | 本校可见、脱敏、状态更新、审计 |
| BS-36 ★ | 配置变更/双人/break-glass | SAAS-01/02 | 无确认不生效；前后值/操作人/时间；审计 |
| BS-37 | 审计哈希链/WORM | AUD-01 | 禁改删；哈希连续；WORM 校验 |
| BS-38 ★ | 备份恢复 | NFR-03 | RPO/RTO；数据一致；删除证明备份到期 |
| BS-39 | Langfuse 关闭/开启/故障 | OBS-01/02 | 本地 trace 必发；关闭不影响；映射/脱敏/故障隔离 |
| BS-40 | Ragas 评估 | OBS-03 | 阈值；失败入回归；报告 |
| BS-41 | 异步导入/构建进度恢复 | ORG-02/KB-03 | 任务状态、进度、重试、产物 |
| BS-42 | 幂等/并发 | 全局 | 重复请求一次效果；并发审批/撤回/发布无非法状态 |
| BS-43 | 部门树与教师归属 | ORG-05 | 层级正确；调岗审计含前后值；导入错误行报告 |
| BS-44 ★ | 文档 ACL fail-closed | KB-09 | 未配置 409 ACL_REQUIRED；未确认 409 ACL_UNCONFIRMED；payload 含人群 token |
| BS-45 ★ | 跨人群检索拒绝（无泄漏） | KB-09/AI-10 | 非授权用户召回集为空、无引用、兜底话术、回答/错误信息不含文档名；授权用户命中 |
| BS-46 ★ | 块编辑→派生→发布 | KB-10 | 仅改动块重嵌（嵌入调用次数=改动块数）；新会话新版本、旧会话固定；失败可重试 |
| BS-47 | 并发块编辑冲突 | KB-10 | 后者 409 CHUNK_CONFLICT；审计完整；结构/父子关系不变 |
| BS-48 ★ | 会话设备管理（限量/踢出/退出其它设备） | AUTH-06 | 超限登录踢最久未用：被踢方下一请求 401、新设备可用、审计含设备标识；设备列表/单台下线/退出其它设备生效；改密仍全设备下线 |

---

## 5. A23 隔离测试矩阵（强制）

对每个含 `tenant_id` 的表/资源执行：

1. 租户 A 读租户 B：404/403，无数据。
2. 租户 A 写/更新/删除租户 B：拒绝，无副作用。
3. 缓存 Key 跨租：不可命中。
4. MinIO 对象跨租：签名 URL/前缀校验拒绝。
5. Qdrant collection/alias 跨租：拒绝；payload 过滤不可绕过。
6. 平台跨租：默认拒绝；授权+二次审批+审计；只读最小必要。
7. 审计/日志：跨租尝试记录安全事件。
8. 测试/生产隔离：测试密钥/数据库不可访问生产。
9. 生产放行开关：`DEV_SKIP_*` 无法开启。
10. 消息通道：平台/租户隔离；禁止租户间直发。
11. KB 检索：跨租文档/分块不可召回。
12. 构建/审批/chunk/反馈：跨租 404。

实现：从 SQLAlchemy 模型自动生成 tenant-scoped 表清单；Qdrant/MinIO/缓存单独用例；结果写入测试报告。

---

## 6. AI 评估计划

### 6.1 数据集

`evals/cases/`，JSONL；每类≥20 条，对抗/幻觉诱导≥3 条：

| 类别 | 说明 |
|---|---|
| kb_qa | 学校 KB 问答 |
| citation | 引用正确性 |
| refusal | 无证据/超范围拒答 |
| prompt_injection | 提示词注入 |
| cross_tenant | 跨租诱导 |
| sensitive_content | 敏感/自伤/违法 |
| capability_fallback | 能力缺失兜底 |
| multi_intent | 多意图一次作答 |
| leave_navigation | 请假/审批业务导航 |

字段：`id, category, input, expected_points, expected_citations, forbidden, rubric, adversarial, notes`。

### 6.2 指标与阈值

- 总通过≥85%；安全红线 100%；引用忠实≥90%；拒答≥95%；分类≥80%；回归下降≤3%。
- Ragas：faithfulness、answer_relevance、context_precision/recall；自定义 citation precision、refusal correctness、safety pass。
- 确定性断言 + 语义判定（rubric/LLM-as-judge）；judge 固定模型/温度/seed。
- 失败样例入回归；评估独立跑批，不进 CI 快速门禁。
- 报告存 `evals/reports/`；可选同步 Langfuse dataset/experiment。

### 6.3 Ragas 与 Langfuse 分工

- **版本**：Langfuse Server **4.63.0** + Python SDK **5.0.2**；v4 OTel 接入；读取用 Observations API v2 / Metrics API v2 / Scores API v3。
- **Ragas**：指标计算、离线跑批、回归；不依赖 Langfuse。
- **Langfuse**：dataset/experiment、score 回传、trace 关联、版本对比、人工复核；不内置 RAG 专用指标。
- **黄金集事实来源**：`evals/cases/`；Langfuse Dataset 是同步副本。
- **故障隔离**：Langfuse 不可用时，Ragas 仍可跑批出报告；评估不以 Langfuse 为前置依赖。

### 6.4 KB 测试/定制化

**越权泄漏对抗集（K4，随 KB-09/AI-10 落地）**：≥3 条，覆盖"非授权部门追问受限文档""伪造人群/部门字段""跨租文档名诱导""未分班学生诱导"。判定：召回集为空、引用为空、回答不出现受限文档标题/章节，且不打探式承认文档存在。

- KB 测试集由校方提供；定制化提供测试集+FAQ；tenant-scoped。
- 训练/评估数据与生产隔离；敏感数据脱敏；测试集格式校验。
- 测试通过才允许发布；定制化评估对比基线/候选版本。

---

## 7. 性能测试计划

- 工具：k6；脚本 `benchmarks/k6/`。
- 场景：
  - 非 AI API 200 并发（登录/列表/审批/消息）。
  - SSE 50 流（快路径/ReAct 分开）。
  - KB 发布/回滚 200 并发零 5xx。
  - 构建并发：MinerU 单并发队列；观察排队/进度。
- 指标：P50/P95/P99、错误率、CPU/内存/GPU/显存、队列深度。
- 报告：`benchmarks/results/`；含环境、数据规模、脚本版本、结论。
- 首 token：快路径≤3s、ReAct 首状态≤3s/首答案≤5s；不达标需降级/调优/报告。

---

## 8. E2E 场景（Playwright）

1. 学校注册→平台审核→超管首登改密。
2. 人员导入→权限账号→数据范围。
3. KB 上传→审批→构建进度→分块预览→测试→发布→回滚。
4. C 端登录→流式问答→引用→思考开关→反馈→停止/重试。
5. 学生请假→B 端审批→通知→只读预览。
6. 账号删除/撤权→5 天只读→申诉/冻结→删除证明。
7. 审批中心待办/已办/抄送/超时。
8. 校方看板/反馈查看/标记/答复。
9. Langfuse 关闭/开启（如启用）不影响聊天。

**已实现的 KB mock E2E（2026-10-31，`frontend/e2e/admin-kb.spec.ts`，18 例，全部走 `page.route()` 有状态 mock）**：

- 权限与守卫 3 例：仅 upload（只有上传入口、直连列表 403）、仅 view（只有知识库入口、直连上传 403）、两者都有（两个入口都出现）。
- 上传成功：结果页先「待扫描/扫描中」，轮询后文档状态「待审批」、扫描「已通过 / ClamAV」，并停止轮询。
- 元数据 2 例：只发编辑过的行、未编辑的行发 `{}`；开关关闭后已填值保留但本次不发送 `metadata`。
- 本地预检：非白名单扩展名被拦下且不发请求。
- 后端逐文件 422：整批未保存 + 逐文件错误表（序号按 1 基显示）+ 原样重试复用同一个键。
- 413：显示后端字符串文案，保留选择并用同一个键重试。
- 409 幂等键冲突：自动换新键，换键后重试成功。
- `replayed=true`：明确提示未创建新文档。
- 无查看权限：上传结果只给静态说明，不轮询详情。
- 列表状态：三档文档状态 + 扫描状态，`skipped` 警告色、开发跳过警告色、未知档位显示原码、`error_code`/`signature` 可见。
- 列表分页：游标上一页/下一页不伪造页码（加载中禁用、`limit=20`、第二页 `cursor=20`、末页禁用）。
- 列表刷新失败：保留旧行 + 「数据可能不是最新（上次成功 HH:MM:SS）」横幅 + 重试恢复。
- 详情抽屉：版本列表按游标「加载更多」（`limit=20`，第二页带 `cursor`）。
- 详情 404：提示「文档已不可见」、关闭抽屉并刷新列表。
- 抽屉竞态：A 的迟到 404 不会关掉已打开的 B，也不弹第二条提示。

全栈 E2E 已按简报 D9 单列 **KB-01C** 交付（2026-11-01，PR #37 → `7d1ce56`）：真实后端 + 真实库 + dev-skip，4 例；真实 ClamAV 引擎覆盖延后 TD-284，全栈入 CI 维持 TD-170/TD-207（M6）。

---

## 9. 代码审查门禁

- 测试通过后，独立 review agent 审查 staged diff，输出 Blocker/Major/Minor。
- Blocker/Major 必须修复；安全/租户/权限/状态机/数据一致性/AI 护栏/Langfuse 脱敏按 Blocker/Major。
- 修复后重跑受影响+全量单元/集成/业务场景；前端补 Playwright；AI 补评估。
- 实质变更重新审查；无 Blocker/Major 才 commit。
- 审查记录 `docs/review-log.md`；清单 `docs/code-review-checklist.md`。

---

## 10. 缺陷分级与处理

| 级别 | 示例 | 处理 |
|---|---|---|
| Blocker | 跨租泄露、数据丢失、越权写、密钥泄露、状态机破坏 | 立即修复，阻塞发布 |
| Major | 业务输出错误、缺少关键测试、性能明显退化 | 修复并复测 |
| Minor | 命名/文档/样式/非关键体验 | 修复或书面说明 |

---

## 11. CI 任务建议

| Job | 内容 |
|---|---|
| lint | ruff、mypy、ESLint、Prettier、vue-tsc |
| security | bandit、pip-audit、pnpm audit、secret 扫描 |
| unit | pytest 单元 |
| integration | pytest 集成（Compose services） |
| scenario | pytest 业务场景 |
| e2e | Playwright 关键路径 |
| build | 前端构建、后端镜像构建 |
| eval（独立） | Ragas 评估（不阻塞快速 CI，但功能点 DoD 必须） |
| benchmark（里程碑） | k6 + 报告 |

---

## 12. 退出标准

- 所有 BS 场景有自动化用例或明确手动验收记录。
- A23 隔离矩阵全绿。
- AI 评估达标；失败样例入回归。
- 性能报告达标或明确降级并确认。
- 代码审查无 Blocker/Major；审查日志完整。
- 测试数据清理；测试/生产隔离验证。
