# M3-KB-01 — 知识库上传 / ClamAV / 元数据（审批前不解析）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-31 |
| 状态 | 已确认（2026-10-31：D1-D8 均选方案 A；教师维持只有 upload；`.ppt`/`.pptx` 都收；整批原子 + 必填幂等键） |
| 关联功能点 | M3 第 1 点：KB-01 上传（后端 KB-01A；前端 KB-01B 单列一点） |
| 验收引用 | `docs/spec.md` §3.6 KB-01（:236-245）、`docs/milestone.md` §6.2/§6.4、`docs/test-plan.md` BS-09（:66）、`docs/state-machines.md` §3（:49-75）、`docs/design.md` §4.3（:215-232）/§5.5（:444-451） |

## 1. 目标

校方/教师把 PDF、MD、DOCX、PPT(X)、TXT 上传到本校知识库：**只做**落对象存储、ClamAV 扫描、文档与版本元数据登记；扫描通过后版本进入 `PENDING_APPROVAL`（可提交审批），扫描失败进入 `SCAN_FAILED`（隔离）。**不解析、不切块、不嵌入、不建构建任务、不实现审批接口**（审批是 KB-02）。

本点交付后端 KB-01A：4 个接口（上传、列表、详情、版本列表）、2 张核心表 + 1 张上传批次表、扫描 worker 与看门狗、配置与非开发环境守卫。前端上传页/列表页为 KB-01B（另一点，TD-259/260/261 在首个前端点处理）。

## 2. 候选方案

### D1 扫描执行机制

- **方案 A（推荐）：Arq worker 异步扫描**。上传事务提交后入队 `scan_kb_version`；worker 读对象、校验哈希、走 ClamAV、按结果迁移状态。规格明确要求「ClamAV 异步 fail-closed」，且 `app/workers/` 已有 arq 先例（`run_import_job`、看门狗）。
  - 优点：上传响应快；扫描故障不拖垮接口；可重试/可观测；与后续构建 worker 同构。
  - 缺点/成本：多一个 worker 函数与看门狗；测试需假 clamd 服务。
  - 风险：队列不可用时文档停在 `UPLOADED`——由看门狗每 5 分钟补投，fail-closed 不放大。
- 方案 B：请求内同步扫描。实现最简单，但 50MB 文件 + 120s ClamAV 超时会把上传接口拖死，违反「异步」规格；排除。
- 方案 C：独立扫描守护进程轮询对象存储。解耦最好，但需要新部署单元与状态协调，M3 成本过高；排除。

### D2 ClamAV 客户端

- **方案 A（推荐）：自研最小 async INSTREAM 客户端**（`app/security/clamav.py`，约 80 行）。TCP 发送 `zINSTREAM\0` + 64KB 分块 + 零长度结束，按 NUL 读回 `stream: OK` / `stream: <签名> FOUND`；整体 `asyncio.timeout` 受 `CLAMAV_TIMEOUT_SECONDS` 约束。
  - 优点：无新依赖；协议面窄、可单测；超时/断连语义完全可控。
  - 缺点/成本：协议代码要自己维护（仅 INSTREAM 一种）；需假 clamd 服务覆盖正常/感染/乱码/超时。
- 方案 B：PyPI `clamd` + `asyncio.to_thread`。少写协议代码，但引入一个维护活跃度一般的依赖，且阻塞调用需要线程池；供应链面变大。
- 方案 C：`clamdscan` 子进程。依赖系统安装与临时文件落盘，Windows 开发环境不可用；排除。

### D3 批量语义与幂等

- **方案 A（推荐）：整批原子 + 必填 `Idempotency-Key` 头**。
  - 先对 15 个文件全部做类型/大小/ZIP/文本校验，**任一失败整批 422**（带逐文件错误），不落任何对象/行；
  - 全部通过后在单个 DB 事务写 1 条 `kb_upload_batches` + 15 条文档 + 15 条版本；对象先写存储，DB 失败则逐个删除已写对象；
  - `(tenant_id, idempotency_key)` 唯一。重复键：请求指纹（按顺序的 `filename|size|sha256` 规范化）一致 → 返回原批次结果；指纹不同 → 409 `IDEMPOTENCY_KEY_REUSED`。
  - 优点：一次上传 15 个 = 15 条记录，不会出现「一半成功」；网络重试不产生重复文档；符合 AGENTS §3「写操作幂等键 + 唯一约束」。
- 方案 B：逐文件部分成功。响应要带 15 种状态，前端/审计/后续审批都复杂化；且规格的「超限拒绝并提示」更像整批语义；排除。
- 方案 C：不做幂等键。重试即重复建档，后续版本管理会出现同名重复文档；排除。

### D4 对象存储布局与隔离

- **方案 A（推荐）**：`kb/<tenant_id>/<document_id>/<version_id>/source<ext>`；感染后复制到 `.../quarantine/source<ext>` 并删除原对象，`source_object_key` 指向隔离区。
  - 优点：租户边界进 key（AGENTS §3）；文档/版本级可审计；隔离区一眼可辨。
  - 缺点/成本：key 较长；感染时多一次 copy+delete。
  - 影响：`docs/design.md` §3/§4.3 补 KB key 约定。
- 方案 B：`kb/<document_id>/...` 不带租户。省几个字符，但对象层失去租户边界，违反红线；排除。
- 方案 C：`kb-quarantine/<tenant_id>/...` 独立前缀。隔离更醒目，但同一版本的对象分散两棵树，清理/排查更麻烦；不选。

### D5 扫描状态表达

- **方案 A（推荐）：不新增 `SCANNING` 状态**。`kb_document_versions.status` 保持 `UPLOADED`，扫描过程写 `scan_attempts/scan_error_code/scan_engine/scanned_at/scan_signature`；通过 → `PENDING_APPROVAL`，感染/完整性失败 → `SCAN_FAILED`，瞬时故障留在 `UPLOADED`（fail-closed）。接口派生 `scan.state = pending|error|passed|infected|skipped`。
  - 优点：与 `docs/state-machines.md` 完全一致，无新状态迁移；「未扫描」与「扫描中」都不满足可审批条件。
  - 缺点/成本：前端要读派生字段区分「排队中」与「重试中」。
- 方案 B：新增 `SCANNING` 状态。语义直观，但要改状态机文档、CHECK 约束与后续所有审批守卫；收益仅是少一个派生字段；不选。

### D6 文件校验实现

- **方案 A（推荐）：标准库校验**。扩展名白名单 + 魔数：PDF `%PDF-`；DOCX/PPTX 必须是 ZIP 且含 `[Content_Types].xml` + `word/document.xml` / `ppt/presentation.xml`；PPT（97-2003）OLE 头 + `PowerPoint Document` 流名；TXT/MD 按 `utf-8-sig` → `gb18030` 严格解码且无 NUL。ZIP 炸弹：条目 ≤2048、单条目解压 ≤100MB、总解压 ≤500MB、总压缩比 ≤300、拒绝加密条目与 `..`/绝对路径条目。声明 MIME 只作快速拒绝，服务端按魔数落 canonical MIME。
  - 优点：零新依赖、Windows/CI 行为一致、可精确单测。
  - 缺点/成本：`python-magic` 那种「全格式识别」没有；但白名单只有 6 种，够用。
- 方案 B：`python-magic`/libmagic。识别更广，但依赖系统库，Windows 开发环境要额外安装；且 MIME 仍不能替代结构校验；不选。

### D7 超大请求防护

- **方案 A（推荐）：给 KB 上传路径加「流式计数」上限**。`BodySizeLimitMiddleware` 扩展一个不缓冲的模式：`Content-Length` 预检 + 逐块累计，超过 `300MB + 8MB`（multipart 余量）即中止并 413；业务层再按「文件字节总和 ≤300MB」精确校验。
  - 优点：不把 300MB 读进内存；chunked 请求也拦得住；现有注册/导入的缓冲模式保持不变。
  - 缺点/成本：要处理「应用解析中途被打断」的响应边界（仅在未开始响应时发 413），需单测覆盖。
- 方案 B：只信 `Content-Length` + 反向代理 `client_max_body_size`。开发/测试无代理，绕过后可打满临时磁盘；不够。
- 方案 C：复用现有整块缓冲。300MB 进内存，直接 OOM；排除。

### D8 交付拆分

- **方案 A（推荐）**：KB-01A 后端（本点）+ KB-01B 前端（另一点）。与 M2「后端点/前端点分开」节奏一致；本点用集成/业务场景测试验收，不夹带前端门禁。
- 方案 B：后端 + 前端一个 PR。diff 大、审查面散，且 TD-259/260/261 会混进来；不选。

## 3. 选择结论

- D1 A（arq 异步）、D2 A（自研 INSTREAM）、D3 A（整批原子 + 幂等键）、D4 A（租户前缀 + quarantine）、D5 A（不加 SCANNING）、D6 A（标准库校验）、D7 A（流式 body cap）、D8 A（先后端）。
- 需要改 spec/design 的地方（实现时回写）：
  - `docs/design.md` §3/§4.3：补 KB 对象 key 约定、`kb_document_versions` 的扫描字段、`kb_upload_batches` 表、`kb_documents.upload_batch_id/upload_file_index`；
  - `docs/design.md` §5.5：上传接口的 `Idempotency-Key` 与 `metadata` 表单字段、列表/详情/版本列表的响应形状；
  - `docs/state-machines.md` §3：明确「瞬时扫描故障保持 UPLOADED（fail-closed）」与「感染 → SCAN_FAILED + 隔离区」；
  - `docs/spec.md` KB-01 不改口径。
- 不改：权限目录（`school.kb.*` 已存在，`catalog.py:71-82`）、审批模块（KB-02 才接）。

### 3.1 范围内子决策（随简报一并确认）

- **白名单与 canonical MIME**：`.pdf` → `application/pdf`；`.md` → `text/markdown`；`.txt` → `text/plain`；`.docx` → `application/vnd.openxmlformats-officedocument.wordprocessingml.document`；`.pptx` → `application/vnd.openxmlformats-officedocument.presentationml.presentation`；`.ppt` → `application/vnd.ms-powerpoint`。声明 MIME 为 `application/octet-stream`/空/带 charset 的同族值可放行，**与扩展名冲突的已知类型拒绝**；落库的是服务端按魔数判定的 canonical MIME。
- **限制**：15 个/批、50MB/文件、300MB/批（文件字节总和）；transport cap 308MB 由 body cap 兜底；0 字节文件拒绝。
- **元数据表单**：multipart 可带一个 `metadata` 字段（JSON 数组，长度=文件数），元素 `{title?, tags?, description?}`；缺省 `title=文件名去扩展名`（≤200 字）、`tags=[]`（≤10 个，每个 ≤32 字）、`description=null`（≤1000 字）。文件名只用于展示与缺省标题：去路径、去控制字符后入库，**不参与对象 key**（key 全用 UUID）。标签/描述的「免审修改」在 KB-02，本点只做上传时登记。
- **状态与派生**：版本/文档 status 只用 `UPLOADED`（扫描中/待重试）、`PENDING_APPROVAL`（通过）、`SCAN_FAILED`（感染或完整性失败）；`scan_error_code` 区分瞬时故障；接口派生 `scan.state`。
- **数据范围**：上传要求 `school.kb.upload`，不因 scope 收窄（上传者建自己的文档）；列表/详情/版本列表要求 `school.kb.view`，用 `principal.scope_context` + `owner_attr="created_by"`——`school` 范围看全校，`self` 范围只看自己上传的，跨范围/跨租一律 404。
- **分页**：`created_at desc, id desc` 游标，默认 20、上限 100；`PageResponse` 从新共享模块引用。
- **审计动作**：`school.kb.documents.uploaded` / `school.kb.upload.denied` / `school.kb.scan.passed` / `school.kb.scan.infected` / `school.kb.scan.failed`；拒绝审计独立事务，metadata 只落判据码与数量，不落文件内容。`scan.failed` 只记永久失败（完整性不符），瞬时故障只写 `scan_error_code` 与日志，避免每次重试都产一行审计。
- **开发旁路**：`DEV_SKIP_FILE_SCAN=true` 仅在 development/test 可用，上传后直接置 `PENDING_APPROVAL` 并标 `scan_engine=dev-skip`；`CLAMAV_FAIL_CLOSED=false` 也只在 development/test 可用，扫描故障降级为 `PENDING_APPROVAL` + `scan_engine=dev-degraded`；staging/production 由配置校验强制 `DEV_SKIP_FILE_SCAN=false`、`CLAMAV_FAIL_CLOSED=true`。

## 4. 实现顺序与 TDD 测试清单

1. **配置**：`Settings` 增 `clamav_host/clamav_port/clamav_timeout_seconds/clamav_fail_closed/dev_skip_file_scan`（名称沿用 `.env.example:140-144,185`）；非 dev 环境 `DEV_SKIP_FILE_SCAN=false`、`CLAMAV_FAIL_CLOSED=true`，否则启动即拒绝。
2. **ClamAV 适配器**：`app/security/clamav.py`（异步 INSTREAM；错误分类 `SCAN_UNAVAILABLE/SCAN_TIMEOUT/SCAN_PROTOCOL_ERROR`；感染返回签名）。
3. **数据**：`app/db/models/kb.py`（`kb_upload_batches`、`kb_documents`、`kb_document_versions`）+ 迁移（复合外键、循环 FK、CHECK、RLS + GRANT）+ `tests/support/db.py` 清理清单。
4. **校验与状态机**：`app/kb/validation.py`、`app/kb/status.py`（仅两条迁移：UPLOADED→PENDING_APPROVAL / UPLOADED→SCAN_FAILED；非法迁移拒绝；导出 `assert_submittable` 供 KB-02 复用，本点只测谓词、不开提交接口）。
5. **服务**：`app/kb/service.py`（整批校验 → 写对象 → 单事务落 1 batch + N 文档/版本 → 失败清理；幂等键命中/复用；列表/详情/版本查询走数据范围）。
6. **接口**：`app/api/school_kb.py`（`POST/GET /school/kb/documents`、`GET /school/kb/documents/{id}`、`GET /school/kb/documents/{doc_id}/versions`），`school.kb.upload` / `school.kb.view`；body cap 登记；`app/api/pagination.py` 提供共享 `PageResponse`（仅新接口使用，既有模块不动，TD-241 仍按 M3 契约批次迁移）。
7. **Worker**：`scan_kb_version`（短事务读版本 → 扫对象 → 短事务条件更新；哈希不符 → `SCAN_FAILED/HASH_MISMATCH`）+ `retry_pending_kb_scans` 看门狗（每 5 分钟，`UPLOADED` 且 `updated_at < now()-5min`，arq job_id 去重）。
8. **审计**：`school.kb.documents.uploaded`（batch_id/数量/总字节/扩展名）、`school.kb.upload.denied`（独立事务，只落判据码）、`school.kb.scan.passed/infected/failed`（状态迁移才写）；不写文件名之外的用户内容，不写对象内容。

红→绿清单：

- [ ] 单元：扩展名/魔数/MIME/ZIP 炸弹/文本编码（含 `.docx` 与 `.pptx` 互认失败、加密 ZIP、路径穿越 ZIP）
- [ ] 单元：状态机（合法/非法迁移）、幂等指纹规范化、错误码映射
- [ ] 单元：ClamAV 客户端（OK / FOUND / 乱码 / 断连 / 超时，假 asyncio server）
- [ ] 集成：真实 PG——1 batch + 15 文档/版本、复合 FK、RLS 跨租不可见、唯一键重放、对象失败清理、body cap 413（含 chunked）
- [ ] 集成：worker——通过/感染/ClamAV 不可用/哈希不符；感染后对象移入 quarantine；文档状态与版本状态一致
- [ ] 业务场景 BS-09：上传合法文件 → `UPLOADED`（无 build job）→ 假 ClamAV 通过 → `PENDING_APPROVAL`；感染文件 → `SCAN_FAILED` 且 `assert_submittable` 拒绝；超 15 个/51MB/ZIP 炸弹 → 整批 422 且库/存储无残留；跨租 404；无权限 403；审计逐条断言
- [ ] E2E：本点无 UI，不新增
- [ ] 评估/性能：本点无；50MB 单文件扫描耗时记录在报告中

## 5. 测试策略

| 层级 | 内容 |
|---|---|
| 单元 | 文件校验、ZIP 炸弹、编码、状态机、幂等指纹、ClamAV 协议、错误映射 |
| 集成 | 迁移/RLS、批量原子性、对象存储清理、body cap、worker 状态迁移、quarantine |
| 业务场景 | BS-09 全链路（状态、无 build job、隔离、审计、跨租/权限） |
| E2E | 无（KB-01B 前端点时补上传/列表 E2E） |
| AI 评估 | 无 |
| 性能 | 50MB 文件扫描耗时落报告；不做 k6 |

## 6. 预判技术债

| TD 编号 | 条目 | 处置 |
|---|---|---|
| TD-271（新） | Dependabot 两条告警：`diskcache` 不安全反序列化（medium）、`ragas` SSRF（low） | 已采纳 → M3 依赖治理：评估升级/替换；在 M3 内给出结论 |
| TD-272（新） | `ObjectStore` 只有整块 `bytes` put/get，50MB 文件上传与扫描各需整块进内存 | 已延后 → M6（改流式 put/get；当前单文件顺序处理，峰值 50MB 可接受） |
| TD-079（既有） | 资质凭证未做病毒扫描 | 本点交付可复用的 `app/security/clamav.py`；凭证接入与历史补扫仍按 TD-079 在 M3 单列 |
| TD-241（既有） | 分页信封手抄 | 本点新增 `app/api/pagination.py` 供 KB 使用；既有模块迁移仍按 M3 契约批次 |

## 7. 高风险确认

- **是否属于高风险功能**：是（上传、租户隔离、权限、状态机、审计、恶意文件处理）。
- **需要用户确认的点**（推荐项在前）：
  1. **ClamAV 不可用时宁愿「卡住」也不放行**：文档停在 `UPLOADED`，不可提交审批，后台每 5 分钟自动重试；生产环境禁止跳过扫描。确认采用 fail-closed。
  2. **整批同生共死**：一次传 15 个，只要 1 个不合格（类型/大小/ZIP 炸弹），整批 422、一个都不落库；不做「传成功几个算几个」。
  3. **上传必须带幂等键**：前端每次上传生成一个 `Idempotency-Key`；同键重试返回原结果，同键换文件返回 409。后续 KB-01B 前端会按此实现。
  4. **教师默认只有 upload、没有 view**：当前权限矩阵里 `teacher_b_end` 只有 `school.kb.upload`，上传后看不到列表/扫描结果，需超管另行授予 `school.kb.view`。保持最小权限（推荐）还是把 view 加进教师默认角色？（后者要改权限矩阵种子 + 迁移 + 矩阵测试，单列一点。）
  5. **`.ppt` 旧格式**：规格写的是 PPT(X)，本简报按「`.ppt` + `.pptx` 都收」设计（`.ppt` 只能做 OLE 头 + 流名级校验）；若只想支持 `.pptx`，实现前告知。
- **确认结论**：2026-10-31 用户确认「按推荐来」——5 项均按推荐：fail-closed；整批原子；必填幂等键；教师维持最小权限（只有 upload，不加 view）；`.ppt`/`.pptx` 都收。

## 8. 审查对照

- 审查时重点验证：租户/数据范围（跨租 404、self 只见自己）、幂等键、整批原子与失败清理、fail-closed（扫描未通过绝不进 `PENDING_APPROVAL`）、无解析/无 build job、审计不含文件内容、对象 key 不含 PII、body cap 对 chunked 生效、RLS 与复合 FK、quarantine 后原对象确已删除、状态与文档/版本一致。
- 允许的偏差：新增扫描字段与 `kb_upload_batches` 表（实现时回写 design.md）；`.ppt` 校验深度受格式限制；列表分页使用新共享 `PageResponse` 但既有模块不动。
