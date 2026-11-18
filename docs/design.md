# 校园陪伴精灵 — 技术设计（design.md）

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-10-19 |
| 状态 | 待用户确认；确认前不进入 M0 |
| 上游 | `docs/spec.md`、`docs/requirements.md` |
| 相关 | `docs/state-machines.md`、`docs/rbac-matrix.md`、`docs/tenant-isolation.md`、`docs/observability-langfuse.md`、`docs/deployment.md` |

---

## 1. 架构总览

### 1.1 形态

**模块化单体 + 独立 Worker + AI Gateway 模块**。

```text
┌─────────────────────────────────────────────────────────────┐
│ Nginx (HTTPS, SSE no-buffer, rate limit)                    │
└───────────────┬───────────────────────────────┬─────────────┘
                │                               │
        ┌───────▼────────┐              ┌───────▼────────┐
        │ apps/admin      │              │ apps/campus     │
        │ Vue3+Element    │              │ Vue3+Vant H5    │
        └───────┬────────┘              └───────┬────────┘
                │ REST/SSE                      │ REST/SSE
        ┌───────▼───────────────────────────────▼────────┐
        │ FastAPI 主服务                                  │
        │ auth / tenant / rbac / org / kb / ai / approval │
        │ message / audit / dashboard / config            │
        │ Harness + LangGraph + AI Gateway                │
        └───────┬───────────────┬───────────────┬────────┘
                │               │               │
        ┌───────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐
        │ PostgreSQL16 │ │ Redis 7.4   │ │ MinIO       │
        │ + RLS        │ │ 缓存/限流    │ │ 对象存储     │
        └──────────────┘ │ 队列(Arq)    │ └─────────────┘
                         └──────┬───────┘
                                │
                    ┌───────────▼───────────┐
                    │ Worker               │
                    │ 构建/解析/嵌入/邮件   │
                    └───────┬───────┬───────┘
                            │       │
                    ┌───────▼───┐ ┌─▼──────────────┐
                    │ Qdrant    │ │ MinerU 4.0.12  │
                    │ 向量库     │ │ V1 API (GPU)   │
                    └───────────┘ └────────────────┘
                            │
                    ┌───────▼──────────┐
                    │ TEI CPU          │
                    │ embed + rerank   │
                    └──────────────────┘

观测：本地 trace 表/JSONL（必选）→ Langfuse（生产独立主机，可选/异步）
```

### 1.2 模块边界

| 模块 | 职责 | 不负责 |
|---|---|---|
| auth | 登录、令牌、密码、MFA、锁定、重置 | 业务权限判断 |
| tenant | 租户生命周期、学校编号、注册审核 | 平台配置 |
| rbac | 角色、权限、数据范围、审批分权 | 业务状态机 |
| org | 教师/学生/班级/课程/关系、导入 | 登录凭据 |
| kb | 上传、审批门禁、构建、测试、发布、分块、检索测试 | LLM 生成 |
| ai | Harness、Loop、RAG 编排、SSE、引用、反馈 | 业务审批 |
| approval | 通用审批引擎、类型注册、待办/超时 | 业务动作执行 |
| message | 站内信、邮件、outbox、重试、通道隔离 | 平台配置 |
| audit | 仅追加审计、哈希链、WORM | 调试 trace |
| dashboard | 校方统计、反馈查看/答复 | 词频/高级 BI |
| config | 全局/租户/用户配置、密钥加密、变更审批 | DB 连接 UI |
| observability | 本地 trace、Langfuse 导出、Ragas | 合规审计 |
| parser adapter | MinerU V1 API、tier 降级、LibreOffice | 业务逻辑 |

### 1.3 部署拓扑

- **应用主机**：Nginx、admin/campus 静态资源、FastAPI、Worker、PostgreSQL、Redis、MinIO、Qdrant、ClamAV、TEI、MinerU（GPU）。
- **观测主机**：Langfuse（Web/Worker + Postgres + ClickHouse + Redis + S3/MinIO）；生产独立，不与应用主机共用 16GB。
- **资源约束**：开发机 16GB/6GB；MinerU GPU 单并发；嵌入/重排 CPU；Langfuse 默认不启。
- 详细见 `docs/deployment.md`。

---

## 2. 工程结构

```text
LLM-05-CampusCompanionElf/
├─ README.md
├─ AGENTS.md
├─ .gitignore
├─ .env.example
├─ docs/
│  ├─ requirements.md
│  ├─ spec.md
│  ├─ design.md
│  ├─ milestone.md
│  ├─ test-plan.md
│  ├─ state-machines.md
│  ├─ rbac-matrix.md
│  ├─ tenant-isolation.md
│  ├─ compliance.md
│  ├─ observability-langfuse.md
│  ├─ deployment.md
│  ├─ code-review-checklist.md
│  └─ review-log.md
├─ evals/
│  ├─ README.md
│  ├─ rubric.md
│  ├─ cases/
│  └─ reports/
├─ benchmarks/
│  ├─ README.md
│  ├─ k6/
│  └─ results/
├─ backend/
│  ├─ pyproject.toml
│  ├─ uv.lock
│  ├─ .python-version          # 3.13
│  ├─ alembic.ini
│  ├─ app/
│  │  ├─ main.py
│  │  ├─ config.py
│  │  ├─ api/
│  │  ├─ core/                 # security, tenant, errors, logging, idempotency
│  │  ├─ db/
│  │  ├─ models/
│  │  ├─ schemas/
│  │  ├─ services/
│  │  ├─ harness/
│  │  ├─ agents/
│  │  ├─ rag/
│  │  ├─ ai_gateway/
│  │  ├─ workers/
│  │  ├─ integrations/
│  │  ├─ prompts/
│  │  └─ tests/
│  ├─ alembic/
│  └─ prompts/
├─ frontend/
│  ├─ pnpm-workspace.yaml
│  ├─ package.json
│  ├─ apps/admin/
│  ├─ apps/campus/
│  └─ packages/{api-client,ui,config}/
├─ deploy/
│  ├─ docker-compose.yml
│  ├─ nginx/
│  ├─ initdb/
│  ├─ backup/
│  └─ mineru/
└─ scripts/
```

---

## 3. 技术栈与版本

见 `docs/requirements.md` 第 4 节。锁定：

- Python 3.13.x，`requires-python = ">=3.13,<3.14"`，`.python-version`=3.13。
- Node 24.x LTS，pnpm 11.x。
- PostgreSQL 16、Redis 7.4、Qdrant 固定具体版本、MinIO 固定 tag。
- MinerU 4.0.12，`mineru[torch]`，GPU 单并发。
- Langfuse **Server 4.63.0 + Python SDK 5.0.2**，独立主机；锁定 tag，不跟 `latest`。
- Docker 镜像固定 tag/digest；`uv.lock`、`pnpm-lock.yaml` 入库。
- LLM 调用层（2026-11-19 决策卡 1）：自建 `LlmPort` 协议 + `OpenAICompatLlm` 适配器（`AsyncOpenAI` 指向 `.env` 的 `LLM_BASE_URL`，SDK 内建重试关闭）；`app/harness/llm.py` 承载契约/错误码/熔断；测试用 `FakeLlm`（`tests/support/llm.py`）。零新增依赖（`openai>=2` M0 已预装）。

---

## 4. 数据模型

> 所有业务表默认含：`id UUID PK`、`tenant_id UUID NOT NULL`（平台级表除外）、`created_at`、`updated_at`、`created_by`、`updated_by`、`deleted_at`（软删，按需）、`version`（乐观锁，按需）。

### 4.1 租户与平台

| 表 | 关键字段 | 说明 |
|---|---|---|
| `tenants` | id, school_code, name, status, address, email, leadership, qualification_ref, activated_at | 学校=租户；status: pending/active/suspended/rejected/deleted |
| `tenant_registration_requests` | id, school_name, address, leadership, email, qualification_object_key, status, submitted_at | 资质凭证存 MinIO |
| `school_review_records` | id, tenant_id, reviewer_id, result, reason, snapshot_object_key, reviewed_at | 审核记录/凭证快照 |
| `platform_users` | id, account, email, password_hash, status, mfa_secret_enc, must_change_password, failed_login_attempts, failed_login_window_started_at, locked_until | SaaS 账号；不开放注册；邮箱用于密码重置 |
| `platform_password_history` | id, platform_user_id, password_hash, created_at | 最近 5 次旧密码哈希（重置/改密拒绝复用） |
| `platform_password_reset_tokens` | id, platform_user_id, token_hash, expires_at, used_at, created_at, request_ip_hash | 平台重置令牌（SHA-256，30 分钟单次使用） |
| `platform_roles` / `platform_user_roles` | id, code/name, is_system, defaults_applied; user_id, role_id | 平台角色；`defaults_applied` 同 `roles`（TD-150），无版本锁（平台侧本期不开放管理界面） |
| `configs` | id, scope(global/tenant/user), tenant_id, user_id, key, value_json, is_secret, version, status | 配置中心 |
| `config_change_requests` | id, config_id, before_json, after_json, requester, approver, reason, status | 变更审批/双人确认 |
| `model_configs` | id, provider, model_type, base_url, api_key_enc, model_name, tenant_id, status | 密钥信封加密 |
| `tenant_quotas` | tenant_id, daily_requests, daily_tokens, concurrent_streams, storage_bytes | 配额 |
| `usage_logs` | id, tenant_id, user_id, model, input_tokens, output_tokens, latency_ms, cost_estimate, request_id | 用量 |
| `feature_flags` | key, scope, tenant_id, enabled | 生产放行/功能开关 |

### 4.2 用户、权限、组织

| 表 | 关键字段 | 说明 |
|---|---|---|
| `users` | id, tenant_id, user_type(student/teacher/school_admin), login_id, email, password_hash, status, must_change_password, mfa_secret_enc, last_login_at, token_version, failed_login_attempts, failed_login_window_started_at, locked_until | C 端/B 端共用身份；login_id=学号/工号；token_version 用于权限/状态变更时使现有会话失效；锁定字段供 AUTH-05 |
| `password_history` | id, tenant_id, user_id, password_hash, created_at | 最近 5 次旧密码哈希；复合外键保证与用户同租 |
| `password_reset_tokens` | id, tenant_id, user_id, token_hash, expires_at, used_at, created_at, request_ip_hash | 租户员工/校管邮件重置；只存 secret 的 SHA-256，30 分钟单次使用 |
| `roles` | id, tenant_id, code, name, is_system, **defaults_applied**, **version** | 校方角色；`defaults_applied`=出厂权限+范围是否已初始化（TD-150，同步判据），`version`=权限/范围全量替换的乐观锁（TD-137，M2-UI2 管理接口据此拒绝并发覆盖） |
| `permissions` | id, code, module, action | 权限点 |
| `role_permissions` | role_id, permission_id, tenant_id（复合外键保证与父行同租） | 角色-权限 |
| `user_roles` | user_id, role_id, tenant_id（复合外键保证与父行同租） | 多角色 |
| `data_scopes` | id, tenant_id, role_id/user_id, scope_type(school/class/course/self), scope_id | 数据范围 |
| `teachers` | id, tenant_id, user_id, employee_no, name, age, gender, position, **department_id**, id_card_enc, id_card_hmac, id_card_mask | 身份证加密/掩码（同租指纹部分唯一索引 `uq_teachers_idcard_hmac` 兜底查重）；部门归属供文档人群判定（A30） |
| `students` | id, tenant_id, user_id, student_no, name, class_id, gender, id_card_enc, id_card_hmac, id_card_mask | 档案（同租指纹部分唯一索引 `uq_students_idcard_hmac`）；班主任/辅导员权威在 `classes`（M2-ORG01-03 D2，读路径 join 聚合，不冗余落列） |
| `classes` | id, tenant_id, **entry_year**, grade, name, status, head_teacher_id, counselor_id | 班级（ORG-06：`entry_year` 入学年份=届身份、建档后不可改；`grade`=当前年级，学年流转批量刷新；唯一 `(tenant_id, entry_year, name)`；`status`=active/archived 行永不删除） |
| `departments` | id, tenant_id, parent_id, name, code, path, status | 部门/科室树（A30）；复合外键 `(parent_id, tenant_id)`；唯一 `(tenant_id, parent_id, name)` |
| `courses` | id, tenant_id, code, name | 课程 |
| `teacher_class_courses` | teacher_id, class_id, course_id, term | 任教关系 |
| `import_jobs` | id, tenant_id, type, conflict(error\|skip), file_object_key, status, total_rows, success_rows, failed_rows, skipped_rows, error_report_key, row_filter_json(重试行号), retried_by, retry_count, created_by, started_at, finished_at, message | 导入（M2-ORG02 实际形态；错误行重试=派生 job 只导失败行） |
| `import_row_errors` | id, tenant_id, job_id, row_no, field, error_code, message, raw_json(原始行，重试免二次解析) | 错误行 |

### 4.3 知识库

| 表 | 关键字段 | 说明 |
|---|---|---|
| `kb_upload_batches` | id, tenant_id, idempotency_key, request_hash, file_count, total_bytes, created_by | 批量上传（KB-01A）；唯一 `(tenant_id, idempotency_key)`；`request_hash` 是同键同指纹判定依据，同键换文件/换人 409 |
| `kb_documents` | id, tenant_id, title, tags, description, status, current_version_id, **upload_batch_id, upload_file_index**, created_by | 文档；`upload_file_index` 为批次内序号，唯一 `(tenant_id, upload_batch_id, upload_file_index)`；状态与当前版本一致 |
| `kb_document_versions` | id, tenant_id, document_id, version_no, source_object_key, source_hash, mime, size, status, **parent_version_id, derivation_type(initial/chunk_edit), acl_confirmed_by, acl_confirmed_at**, **source_filename, scan_attempts, scan_error_code, scan_signature, scan_engine, scanned_at**, created_by | 版本；status 同下；派生版本（K5）由 parent_version_id 追溯；KB-01A 落扫描结论（`scan_engine` ∈ `clamav`/`dev-skip`/`dev-degraded`）。**KB-02 修订**：不建本表的 `approval_request_id` 反向指针——提交可撤销再提交，同一版本有多行审批历史，改由 `approval_requests.document_version_id` 正向 FK + 索引查询（按 created_at 取最近一单） |
| `kb_build_jobs` | id, tenant_id, document_version_id, status, stage, progress, processed_units, total_units, tier, fallback_reason, retry_count, error_code, message, started_at, updated_at, finished_at | 构建任务；同版本仅一个活跃任务 |
| `kb_build_events` | id, job_id, seq, event_type, payload_json, created_at | SSE 事件持久化，支持 Last-Event-ID/since |
| `kb_chunks` | id, tenant_id, document_version_id, parent_id, chunk_type(parent/child), chunk_index, text, text_markdown, section_path, page_start, page_end, token_count, char_count, overlap_prev, overlap_next, chunk_hash, metadata_json, embedding_status, **is_human_edited, edited_by, edited_at** | 分块 |
| `kb_document_audiences` | id, tenant_id, version_id, audience_type(department/role/class/grade/course/user/all), audience_id, created_by, created_at | 版本级查阅人群（K4）；`all` 时 audience_id 为空；唯一 `(version_id, audience_type, audience_id)` |
| `kb_chunk_edits` | id, tenant_id, source_version_id, chunk_id, before_hash, after_hash, new_content, edited_by, reason, status(draft/submitted/confirmed/discarded), derived_version_id, created_at | 块编辑记录（K5） |
| `kb_embedding_cache` | tenant_id, model, chunk_hash, dense_object_key, sparse_object_key, created_at | 向量复用缓存（K5）；派生重建只对改动块重新推理 |
| `kb_index_versions` | id, tenant_id, kb_id/document_version_id, qdrant_collection, status, published_at, retired_at | 版本化索引 |
| `kb_publish_aliases` | tenant_id, **document_id**, alias, collection, version_id, updated_at | alias 指针（KB-05 落地修订：`kb_id` 落为 `document_id` 复合外键；唯一 `(tenant_id, document_id)` 兜底并发首发布恰一行） |
| `kb_test_sets` | id, tenant_id, document_version_id, name, file_object_key, status, created_by | 测试集 |
| `kb_test_cases` | id, test_set_id, question, expected_answer, expected_points, expected_chunk_ids, category, is_adversarial, row_no | 测试用例 |
| `kb_test_runs` | id, tenant_id, test_set_id, document_version_id, status, metrics_json, report_object_key, started_at, finished_at | 测试报告 |
| `kb_retrieval_tests` | id, tenant_id, document_version_id, query, top_k, result_json, created_by | 检索测试 |

**KB-01A 落地口径（2026-10-31）**：一次上传 = 1 条 `kb_upload_batches` + N 条文档 + N 条版本，**整批原子**；对象 key 由服务端生成 `kb/<tenant_id>/<document_id>/<version_id>/source<ext>`（数据库不存租户外路径）；感染文件先复制到同版本目录的 `quarantine/source<ext>` 留证、再删除原件，版本 `source_object_key` 指向隔离副本；本点只做落库/扫描/元数据，**不解析、不创建构建任务**。

### 4.4 AI、会话、反馈

| 表 | 关键字段 | 说明 |
|---|---|---|
| `conversations` | id, tenant_id, user_id, title, status, pinned_kb_version_id, created_at, last_message_at | 会话 |
| `conversation_messages` | id, tenant_id, conversation_id, role(user/assistant/system), content, reasoning_summary, citations_json, token_usage, trace_id, status(streaming/completed/partial/failed), created_at | 消息 |
| `conversation_summaries` | id, tenant_id, conversation_id, summary, covered_message_ids, created_at | 滚动摘要 |
| `ai_feedback` | id, tenant_id, user_id, conversation_id, message_id, trace_id, rating, reason_tags, comment, content_snapshot_enc, consent_at, status, handled_by, handled_at | 反馈 |
| `ai_feedback_replies` | id, feedback_id, sender_id, message_id, content, created_at | 反馈答复 |
| `chat_traces` | id, tenant_id, user_id, conversation_id, turn_id, intents_json, route_path, model, prompt_name, prompt_version, prompt_hash, tokens_in, tokens_out, first_token_ms, latency_ms, stop_reason, error_code, created_at | 本地 trace |
| `chat_trace_spans` | id, trace_id, span_type(route/retrieve/rerank/tool/guard/generate/verify), name, start_ms, duration_ms, attributes_json, status | span |
| `guard_events` | id, tenant_id, trace_id, direction(input/output), rule, severity, action, excerpt_redacted, created_at | 护栏事件 |
| `llm_usage` | id, tenant_id, trace_id, provider, model, tokens_in, tokens_out, latency_ms, cost_estimate, created_at | LLM 用量 |
| `eval_runs` | id, tenant_id, eval_type(kb_test/customization/regression), dataset_ref, status, metrics_json, report_object_key, started_at, finished_at | 评估 |
| `eval_cases` / `eval_results` | id, run_id, case_ref, input, expected, actual, scores_json, passed | 评估结果 |

### 4.5 审批、请假、消息、删除、审计

| 表 | 关键字段 | 说明 |
|---|---|---|
| `approval_types` | id, code, name, schema_json, enabled | 类型注册 |
| `approval_requests` | id, tenant_id, type, submitter_id, status, current_step, title, payload_json, result, reject_reason, created_at, completed_at, **document_version_id（KB-02，可空，仅 KB_DOCUMENT 类型；部分唯一索引 `(tenant_id, document_version_id) WHERE status='PENDING'` 兜底「一版本一活跃单」）** | 审批单；版本历史按 `document_version_id` 正向查询，不建版本表反向指针 |
| `approval_steps` | id, request_id, step_no, approver_role/approver_id, status, acted_at, comment | 步骤 |
| `approval_actions` | id, request_id, step_id, actor_id, action, reason, created_at | 动作审计 |
| `approval_attachments` | id, request_id, object_key, filename, mime, size, checksum | 附件 |
| `leave_requests` | id, tenant_id, student_id, approver_id, reason, days, start_date, end_date, status, submitted_at, decided_at | 请假 |
| `leave_status_history` | id, leave_id, from_status, to_status, actor_id, reason, created_at | 状态历史 |
| `message_threads` | id, tenant_id, channel(PLATFORM/TENANT), subject, created_by, created_at | 会话 |
| `messages` | id, tenant_id, thread_id, sender_id, receiver_id, content, read_at, created_at | 站内信 |
| `message_templates` | id, code, channel, subject, body, version | 模板 |
| `message_deliveries` | id, message_id, channel, status, attempts, next_retry_at, last_error, provider_message_id | 邮件/重试 |
| `outbox_events` | id, tenant_id, event_type, payload_json, status, attempts, next_retry_at, created_at | outbox |
| `account_termination_requests` | id, tenant_id, subject_user_id, initiator_type(SELF/ADMIN), requested_by, status, approved_by, retention_start_at, retention_end_at, freeze_started_at, freeze_reason, deletion_scope_json, proof_id | 双入口 |
| `account_termination_events` | id, request_id, event_type, actor_id, reason, created_at | 状态历史 |
| `deletion_proofs` | id, tenant_id, request_id, scope_hash, backup_expiry_at, generated_at, evidence_object_key | 删除证明 |
| `audit_logs` | sequence (PK, 递增), event_id, tenant_id (可空=平台事件), actor_realm, actor_id, action, resource_type, resource_id, result, ip, user_agent, request_id, before_json_redacted, after_json_redacted, prev_hash (可空), hash (可空), hash_version (0=未入链), anchor_ref, created_at | 仅追加哈希链（M1-A 只建表与触发器，链值 M1-F 计算） |
| `login_attempts` | id, tenant_id, login_id, ip, success, failure_reason, created_at | 锁定 |
| `refresh_tokens` | id (PK), jti (唯一), user_id, tenant_id, token_hash, family_id, issued_at, expires_at, rotated_to_jti, revoked_at, revoked_reason, ua_hash, ip_hash | 轮换与撤销的唯一事实来源 |
| `idempotency_keys` | key, tenant_id, user_id, endpoint, request_hash, response_json, status, created_at, expires_at | 幂等 |
| `content_safety_events` | id, tenant_id, trace_id, direction, rule, action, excerpt_redacted, created_at | 内容安全 |

### 4.6 ER 关系（核心）

```text
tenants 1─N users 1─N user_roles N─1 roles N─N permissions
tenants 1─N teachers/students
classes 1─N students; teachers N─N classes via teacher_class_courses
tenants 1─N kb_documents 1─N kb_document_versions 1─N kb_chunks
kb_document_versions 1─N kb_build_jobs 1─N kb_build_events
kb_document_versions 1─1 kb_index_versions (published) N─1 kb_publish_aliases
tenants 1─N conversations 1─N conversation_messages
approval_requests 1─N approval_steps/actions/attachments
leave_requests 1─N leave_status_history
message_threads 1─N messages 1─N message_deliveries
account_termination_requests 1─N events; 1─1 deletion_proofs
audit_logs: 仅追加，prev_hash/hash 链
```

---

### 4.6 租户隔离实现细则（M1-A 落地）

- **连接角色（三分离）**：DDL/迁移、租户侧运行、平台侧运行各用一个连接：
  迁移连接（`MIGRATION_DATABASE_URL`）持有表与策略；运行时用两个**非超级用户**角色
  `campus_elf_tenant`（租户侧）与 `campus_elf_platform`（平台侧，可读平台表与审计表）。
  超级用户连 FORCE RLS 也会绕过，因此运行时连接绝不能是超级用户；登录角色口令由部署/测试脚本注入，不入库。
- **授权方式**：**显式逐表授权**（不使用 `ALTER DEFAULT PRIVILEGES`——它会给未来所有表授全量 DML，
  且 `pg_default_acl` 不随 DROP TABLE 清理，迁移重放后 ACL 会漂移）；新增表在对应迁移里显式授权，
  由 `tests/integration/test_rls_policy_coverage.py` 守住；审计表对两个应用角色都只有 `SELECT, INSERT`。
- **RLS 策略**：租户级表统一 `ENABLE + FORCE ROW LEVEL SECURITY`；策略**按角色限定**
  （租户策略 `TO campus_elf_tenant`、平台策略 `TO campus_elf_platform`），避免任何角色自设 GUC 提权。
  表达式用 `pg_input_is_valid(current_setting('app.tenant_id', true), 'uuid')` + `case ... else null end`：
  未设置 / 空串 / 非法值一概退化为 NULL → 读取 0 行、写入被 `WITH CHECK` 拒绝（fail-closed，且不报错）。
- **上下文注入**：每个事务内 `select set_config('app.tenant_id', :tid, true)`（事务级，连接归还即失效），
  由 `app/db/session.py::db_session()` 统一封装；平台级代码路径需显式 `platform=True`
  （设置 `app.platform_context='on'`，仅用于平台表与审计表读取）。
- **平台不跨租**：平台上下文**不**绕过业务表隔离（平台连接在数据库层就没有业务表权限）；
  跨租访问必须走"二次授权 + 审计"的专门通道（M1-E，见 TD-072）。
- **tenants 也隔离**：`tenants` 无 `tenant_id`，但按 `id = app.tenant_id` 建策略（租户只见自己那一行），
  另有平台策略供租户审核/运营读取全部；避免"租户连接能列出所有学校"。
- **跨租父行引用**：`user_roles`/`role_permissions`/`data_scopes`/`refresh_tokens` 使用
  **复合外键** `(子列, tenant_id) → (父表.id, 父表.tenant_id)`，父子必须同租（防对象图嫁接）。
- **仓储强制**：租户数据只能经 `TenantScopedRepository` 访问（查询强制 `tenant_id`，写入校验行自身一致性；
  父子同租由复合外键保证），与 RLS 构成双保险；
  `tests/integration/test_rls_policy_coverage.py` 作为「新表必须带 tenant_id + 策略 + 策略引用上下文变量」的门禁。
- **关联表**：`role_permissions`、`user_roles` 也带 `tenant_id NOT NULL`（便于 RLS 统一生效）；
  父子同租由复合外键 `(子列, tenant_id) → (父表.id, 父表.tenant_id)` 保证（仓储只校验行自身 `tenant_id`）。
- **新增表（M1-A）**：`refresh_tokens`（jti/family_id/token_hash/rotated_to_jti/revoked_*：令牌家族与轮换链的唯一事实来源，
  Redis 仅做加速标记）、`platform_role_permissions`（平台角色权限映射）、`audit_logs.anchor_ref`/`hash_version`（为 M6 外部锚点预留）。
- **审计仅追加**：`audit_logs` 由触发器禁止 UPDATE/DELETE/TRUNCATE（对属主与超级用户同样生效），
  两个应用角色另只授予 `SELECT, INSERT` 权限（双层）。
- **哈希链状态**：M1-A 只建表与仅追加约束；`prev_hash/hash` 可空且 `hash_version=0` 表示"未入链"，
  链计算与校验由 M1-F 实现（TD-068）——当前**未计算、未校验**，不得视为已生效。

### 4.7 M1-D1 密码/锁定/重置落地（2026-10-22）

- **新增表**：`password_history` / `password_reset_tokens`（租户，FORCE RLS + `TO campus_elf_tenant` 策略 +
  显式授权）、`platform_password_history` / `platform_password_reset_tokens`（仅平台连接）；
  令牌表只存 secret 的 SHA-256，重置申请**先作废旧未用令牌再插新令牌**（同事务，失败一起回滚）。
- **锁定字段**：`users`/`platform_users` 增加 `email`、`failed_login_attempts`、
  `failed_login_window_started_at`、`locked_until`；登录成功或密码重置清零；
  锁定期间对外统一凭证错误文案，审计记录真实原因 `locked`。
- **校管邮箱回填**：`users.email` 对 `user_type='school_admin'` 由 BEFORE INSERT 触发器从
  `login_id`（注册邮箱）写入，存量数据迁移时回填；开通函数因此无需额外字段。
- **重置链接**：邮件 URL 使用 fragment `#token=`（不经服务器/代理日志、不进 Referer），
  两个前端入口 `<meta name="referrer" content="no-referrer">`；前端从 `route.hash` 读取令牌。
- **防枚举/防滥用**：申请接口在响应后由 FastAPI `BackgroundTasks` 发送邮件（202 不等待 SMTP）；
  申请与确认分别做同一 IP 固定窗口限流（申请默认 20 次/15 分钟、确认 30 次/15 分钟，
  Redis INCR+EXPIRE，故障 fail-open），**两者超限都不写审计**（未认证请求不得放大审计写入），
  确认超限返回 429 + `Retry-After`；来源 IP 由 `resolve_client_ip` 解析：仅直连对端命中
  `TRUSTED_PROXY_IPS` 时才读取 `X-Forwarded-For` 首个合法地址，Nginx 必须覆盖写该头；
  账号维度 60 秒冷却；并发确认由用户级 advisory lock + 条件消费保证单次使用。
- **审计归属**：未知账号的申请事件固定落平台审计（带 `attempted_tenant_id`），不向任意租户
  审计流灌数据；确认失败只有令牌确实存在于该租户时才写租户审计，伪造 tenant_id/随机令牌
  写平台审计并带 `attempted_tenant_id`。
- **平台账号邮箱（M2 缺口收口）**：`python -m app.platform_admin` 初始化平台账号（默认
  `platform_super_admin`、`must_change_password=true`，初始密码只打印一次）或维护邮箱；
  邮箱变更与作废未使用重置令牌在同一事务，并写 `platform.user.*` 审计。
- **邮件传输**：staging/production 均要求真实 SMTP + TLS/STARTTLS + 用户名/密码，
  禁止 MailHog 与 `@campus-elf.local`（重置令牌不得经明文链路）。

## 5. API 与 SSE 契约

### 5.1 约定

- Base path：业务 API 使用 `/api/v1`；健康检查例外，固定在根路径 `/health/live`、`/health/ready`，供 Compose/Nginx/探针使用；应用指标例外，固定在根路径 `/metrics`（`METRICS_ENABLED` 默认 false，关闭时 404；无业务鉴权，只允许抓取端经网络策略访问，见 §6.6）。
- 身份：`Authorization: Bearer <access_token>`；平台/租户分 realm/audience。
- 租户：只从 token 取 `tenant_id`；不接受 body/query 覆盖。
- 错误信封：`{ "code": "...", "message": "...", "details": {...}, "request_id": "..." }`。
- 分页：cursor（`cursor`, `limit`）；大列表虚拟滚动。
- 幂等：写接口支持 `Idempotency-Key`；服务端用 `idempotency_keys` + 唯一约束。
- 时间：UTC ISO8601；前端本地化。
- OpenAPI 3.1 自动生成；前端 `openapi-typescript` 生成类型。
- SSE：`Content-Type: text/event-stream`；Nginx `proxy_buffering off`、`X-Accel-Buffering: no`；心跳 15s；支持 `Last-Event-ID`/`since`。

### 5.2 认证（M1-C 已实现）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/auth/login` | `realm=tenant`：学校编号 + 学号/工号 + 密码；`realm=platform`：平台账号 + 密码；失败文案统一 |
| POST | `/api/v1/auth/refresh` | 刷新令牌轮换；旧令牌重用 → 撤销整个家族（`reuse_detected`） |
| POST | `/api/v1/auth/logout` | 撤销刷新令牌家族（可选 body 携带 refresh_token）+ 当前访问令牌 jti 立即进入撤销集合 |
| POST | `/api/v1/auth/change-password` | 首登改密/主动改密；自增 `token_version` + 撤销全部家族 + 返回新令牌 |
| GET | `/api/v1/auth/me` | 当前主体、realm、权限码（前端菜单用；服务端仍逐请求校验）；M1-D2 起增加 `mfa_enabled` 与 `mfa_required`（按 realm 强制策略） |
| GET | `/api/v1/auth/sessions` | 我的登录设备列表（设备标签、最近使用、是否当前；只显示自己的） |
| DELETE | `/api/v1/auth/sessions/{session_id}` | 下线指定设备（仅限本人会话） |
| POST | `/api/v1/auth/sessions/revoke-others` | 退出其它设备（保留当前） |
| POST | `/api/v1/auth/password-reset/request` | 员工/平台账号邮件重置；恒 202；令牌 30 分钟单次使用（M1-D1） |
| POST | `/api/v1/auth/password-reset/confirm` | 重置令牌；成功 204，要求重新登录；无效/过期/已用 400、弱密码 422、同 IP 超限 429（带 `Retry-After`，不写审计）（M1-D1） |
| POST | `/api/v1/auth/mfa/verify` | MFA 第二步：`{mfa_token, code}` 校验 TOTP 或恢复码；成功才签发令牌 + 刷新 Cookie，失败统一凭证错误并计入锁定（M1-D2） |
| POST | `/api/v1/auth/mfa/enroll` | 自助绑定：访问令牌需带 `current_password`；平台强制策略下改用登录返回的 `enroll_token`（免密码）；返回 `secret`/`otpauth_uri`（响应 `Cache-Control: no-store`）（M1-D2） |
| POST | `/api/v1/auth/mfa/activate` | 校验绑定验证码：启用 MFA、生成 10 枚一次性恢复码（仅此响应明文）、轮换全部会话并下发新令牌（M1-D2） |
| POST | `/api/v1/auth/mfa/disable` | 关闭 MFA：需当前密码 + TOTP/恢复码；`MFA_REQUIRE_PLATFORM` 开启时对平台 realm 返回 403；同样轮换全部会话（M1-D2） |

**令牌契约**
- 访问令牌：HS256 JWT，15 分钟；claim `sub/realm/tenant_id/jti/token_version/must_change_password/iss/aud/exp`；`aud` 按 realm 区分（`campus-elf-tenant` / `campus-elf-platform`），跨 realm 混用直接拒绝（TD-074）。
- 刷新令牌：`v1t.<tenant_id>.<jti>.<secret>`（租户）/ `v1p.<jti>.<secret>`（平台），仅存 SHA-256 哈希；7 天有效，每次刷新轮换。
- 浏览器端刷新令牌走 **HttpOnly + Secure(staging/production) + SameSite=Lax Cookie**（路径限定 `/api/v1/auth`，JS 不可读）；登录/刷新下发、刷新与登出可仅凭 Cookie。响应体**仅在开发/测试**回显 refresh（`AUTH_REFRESH_TOKEN_IN_BODY=true`）；staging/production 强制关闭，避免 XSS 读走长期凭证（M1-C-UI 审查 MA3）。
- 会话与设备（A31）：登录链即"会话"（family）；访问令牌增加 `sid`（会话 id）。服务端用 Redis 会话标记 `{prefix}:auth:session:{realm}:{sid}`（TTL=刷新令牌有效期）判定会话是否仍有效——登出/被踢/下线即删除标记，**下一次请求立刻 401**，不等 15 分钟。同账号同时在线设备数按 realm 受限（默认 C 端 3、校方 3、平台 2，配置化）；超限登录踢"最久未使用"的会话并审计 `auth.session.kicked`。设备标签由 UA 在服务端粗粒度解析（如"Chrome / Windows"），IP/UA 只存哈希，接口不回显明文。
- 撤销：登出/改密即时失效——`token_version` 自增使所有旧访问令牌失效，刷新令牌按家族撤销；登出的访问令牌另写 Redis 撤销标记（TTL=剩余有效期）。
- 密码策略与历史（M1-D1）：最小 10 / 最大 128 字符且 ≤72 UTF-8 字节（bcrypt 上限）、至少 3 类字符、不得包含学号/工号、最近 5 次历史；bcrypt cost 12；初始密码随机生成（默认 12 位，平台账号按账号长度加长）且首登强制改密。
- 登录失败锁定（M1-D1）：15 分钟内累计 5 次认证失败锁定 15 分钟；计数/窗口/锁定字段在 `users` 与 `platform_users`；成功登录清零，密码重置清锁；锁定期间对外统一凭证错误文案，审计记录 `locked` 真实原因。
- 密码重置（M1-D1）：租户按“学校编号+登录名”、平台按账号申请，发往库内邮箱；恒 202；令牌 `v1rt.<tenant_id>.<secret>` / `v1rp.<secret>`，库内只存 secret 的 SHA-256，30 分钟单次使用；确认成功 204，撤销全部会话与刷新家族，要求重新登录；学生校管重置在 M2 交付。
- MFA（AUTH-04，M1-D2）：第二因素为 TOTP（6 位 / 30 秒 / 校验窗口 ±1 步）+ 10 枚一次性恢复码；租户用户自助启用，平台 realm 由 `MFA_REQUIRE_PLATFORM` 强制（默认 false，生产建议开启，只影响平台 realm）。
- MFA 密钥：`pyotp.random_base32(32)` 生成，AES-256-GCM 加密后存 `users.mfa_secret_enc` / `platform_users.mfa_secret_enc`，密文格式 `v1.<b64url(nonce)>.<b64url(ct)>`，AAD = `mfa:{realm}:{user_id}`（跨用户/跨 realm 搬移密文解密失败）；密钥来自必填 `ENCRYPTION_KEK`（标准 base64 的 32 字节，staging/production 拒绝占位值）。解密失败视为数据/密钥损坏：fail-closed 500 + 审计 `auth.mfa.secret_invalid`。
- MFA 恢复码：`XXXXX-XXXXX`，归一化后以 `HMAC-SHA256(HKDF(KEK, info="mfa-recovery-codes-v1"), "{realm}:{user_id}:{code}")` 存 `mfa_recovery_codes`（租户，RLS）/ `platform_mfa_recovery_codes`；`UPDATE ... WHERE used_at IS NULL RETURNING id` 保证单次消费；只存哈希，明文仅在激活响应出现一次。
- MFA 待激活密钥不落库：AES-GCM 密文存 Redis `{prefix}:auth:mfa:pending:{realm}:{user_id}`（TTL=`MFA_ENROLL_TOKEN_TTL_SECONDS`），Redis 不可用即 fail-closed（503）；重复发起覆盖旧密钥，激活或过期后删除（TD-133）。
- 登录状态机（M1-D2）：密码 → 是否需要第二因素（已开启，或平台 realm 且策略强制）→ 未绑定则签发受限 `mfa_enroll` 令牌、已绑定则签发 `mfa_challenge` → 校验通过才 `_issue_tokens` 建立会话。挑战/绑定令牌为 HS256 JWT：`aud=campus-elf-mfa`、`purpose=mfa_challenge|mfa_enroll`，含 `sub/realm/tenant_id/jti/token_version`，**不能**当访问令牌使用（realm audience 不匹配）；挑战在 Redis 一次性消费（成功即删除）并限制单挑战尝试次数；两者都不写 Cookie/localStorage，前端只放内存。MFA 分支不签发任何令牌、不设刷新 Cookie、不写 `auth.login.success`。
- MFA 声明与会话标记：访问令牌增加 `amr`（仅密码 `["pwd"]`，完成第二因素 `["pwd","totp"]`，恢复码同记 `totp`）；`refresh_tokens.mfa_verified` / `platform_refresh_tokens.mfa_verified` 记录会话是否已完成 MFA。策略由关转开后，未完成 MFA 的旧会话在刷新时被拒绝并撤销家族（`mfa_change`）；`/auth/change-password` 重签时保留当前会话的 `amr`/`mfa_verified`，不降级。`GET /auth/me` 增加 `mfa_enabled` 与 `mfa_required`（按 realm 策略）。
- MFA 变更即轮换：`activate`/`disable` 一律 `token_version+1`、撤销该账号全部刷新家族（`revoked_reason=mfa_change`）、删除 Redis 旧会话标记（≤15 分钟的旧访问令牌下一请求立即 401），并为当前流程签发新令牌；密码重置**不关闭** MFA，重置后仍需第二因素。
- MFA 失败与锁定：错误 TOTP/恢复码、绑定验证码错误、关闭校验失败均复用 AUTH-05 计数（5 次/15 分钟锁 15 分钟），对外统一凭证错误文案；审计 `auth.mfa.enrollment_failed` / `activation_failed` / `disable_failed` 只记 reason/failed_attempts/lock_applied 与 IP/UA 哈希，`auth.mfa.activated` 记恢复码数量与 `sessions_all_revoked`（布尔键，TD-231 收口；整数键 `sessions_revoked` 只表示条数，键名对照见 §5.12），`auth.mfa.recovery_used` 记 `phase=login|disable`；任何审计/日志/响应不得出现 secret、otpauth URI、验证码、恢复码明文（恢复码明文只在该激活响应出现一次）。
- 身份/权限每次请求从数据库读取（不缓存快照）：停用账号、权限变更、`token_version` 变化立即生效。

- 首登改密门禁：`must_change_password` 为真时只放行 `/auth/change-password`、`/auth/logout`、`/auth/me`，其余接口 403。
- realm↔连接绑定：平台主体只使用平台连接、租户主体只使用租户连接（TD-074）；**平台路由必须显式声明 `realm="platform"`**（`permissions` 是全局目录，权限码本身不构成 realm 边界），权限/realm 不符写 `auth.permission.denied` 审计（含 reason）；数据库层另有 `role_permissions`/`platform_role_permissions` 的 realm/模块守卫触发器（`b7d1e3f5a9c2`）。
- 权限模块边界：租户角色禁止引用 `platform.*`（触发器 `role_permissions_realm_guard` + 启动自检逐租户校验）；平台角色允许持有全量权限（运营方超集角色，`c8e2f4a6b1d3` 移除平台方向守卫）。
- 已知限制：bcrypt 输入上限 72 字节（超长口令在登录按凭证错误、改密按 422 拒绝，不再 500）；改哈希方案见 TD-107。
- 平台审核接口使用 `platform.tenant.review` 权限（开发令牌已删除，TD-078）；审计 `actor_id` 为真实平台账号（TD-081）。

### 5.3 SaaS / 租户

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/platform/tenants/register` | 学校注册（公开/受限） |
| GET | `/platform/tenants` | 租户列表 |
| POST | `/platform/tenants/{id}/review` | 通过/拒绝（理由） |
| GET | `/platform/tenants/{id}/review-records` | 审核记录 |
| POST | `/platform/configs` | 配置创建/变更（审批/双人确认） |
| GET | `/platform/audit-logs` | 审计查询/导出 |
| GET | `/platform/messages` | 平台消息 |

### 5.4 组织与人员

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/school/departments` | 部门树维护（A30）；返回层级结构 |
| GET/POST | `/school/teachers` | 教师列表/创建（含 department_id 归属） |
| GET/POST | `/school/students` | 学生列表/创建 |
| POST | `/school/imports` | CSV/XLSX 异步导入 |
| GET | `/school/imports/{job_id}` | 导入进度/汇总 |
| GET | `/school/imports/{job_id}/errors` | 错误行报告 |
| PUT | `/school/teachers/{id}` | 教师编辑（调岗 department_id 必填可空；可选 id_card_number，D2-A） |
| PUT | `/school/students/{id}` | 学生编辑（可选 id_card_number，返回掩码；D2-A） |
| POST | `/school/classes/rollover` | 学年流转（ORG-06）：按年级映射批量刷新当前年级 + 毕业班级归档；幂等键防重、dry_run 预览、全量审计 |
| POST | `/school/students/{id}/transfer` | 学生转班（ORG-06：留级/跳级/分班=重指派到目标年级的班；null=未分班；目标班须活跃；审计 `org.student.transferred`） |
| POST | `/school/personnel/{id}/id-card` | 身份证完整查看（sensitive.read + 请求体理由 ≥10 字符 → 明文 + 审计 + no-store；D3-A。M2-UI1 起 POST：浏览器 fetch 禁止 GET body） |
| POST | `/school/imports/{job_id}/retry` | 失败行重试（派生任务） |
| POST | `/school/permission-accounts` | 权限账号 |
| GET | `/school/dashboard/overview` | 看板统计 |

### 5.5 知识库

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/school/kb/documents` | 上传（multipart，最多 15） |
| GET | `/school/kb/documents` | 列表/状态筛选 |
| GET | `/school/kb/documents/{id}` | 详情 |
| GET | `/school/kb/documents/{doc_id}/versions` | 版本列表 |
| GET/PUT | `/school/kb/document-versions/{id}/audiences` | 读取/全量设置版本查阅人群（K4；保存后置为"未确认"） |
| POST | `/school/kb/document-versions/{id}/audiences/confirm` | 确认人群（K4；`school.kb.acl.manage`，写审计） |
| POST | `/school/kb/document-versions/{version_id}/submit-approval` | 提交审批 |
| POST | `/school/kb/document-versions/{version_id}/build` | 创建构建（仅 APPROVED） |
| GET | `/school/kb/build-jobs/{job_id}` | 构建快照 |
| GET | `/school/kb/build-jobs/{job_id}/events` | SSE 构建进度 |
| POST | `/school/kb/build-jobs/{job_id}/retry` | 重试 |
| GET | `/school/kb/versions/{version_id}/chunks` | 分块列表（cursor/q/section/type） |
| GET | `/school/kb/chunks/{chunk_id}` | 分块详情 |
| POST | `/school/kb/versions/{version_id}/retrieval-test` | 检索测试（支持人群视角） |
| POST | `/school/kb/chunks/{chunk_id}/edits` | 提交块编辑（K5；带 `base_hash`，理由必填） |
| GET | `/school/kb/document-versions/{id}/chunk-edits` | 块编辑 diff 列表（K5） |
| POST | `/school/kb/document-versions/{id}/derive` | 确认并派生新版本（K5；校方超管确认） |
| POST | `/school/kb/versions/{version_id}/test-sets` | 上传测试集 |
| POST | `/school/kb/versions/{version_id}/tests` | 运行测试 |
| POST | `/school/kb/versions/{version_id}/publish` | 发布（需 TEST_PASSED） |
| POST | `/school/kb/versions/{version_id}/rollback` | 回滚 alias |
| POST | `/school/kb/versions/{version_id}/offline` | 下线 |
| DELETE | `/school/kb/versions/{version_id}` | 删除（软删+审计） |

**M3 KB-01A 已实现（后端，2026-10-31）**：

- `POST /school/kb/documents`（`school.kb.upload`）——multipart 1–15 个文件、单文件 ≤50MB、单批合计 ≤300MB（传输层另有 308MB 流式上限，超限 413 且不缓冲整包）；必填 `Idempotency-Key`（8–128 位 `[A-Za-z0-9._:-]`），同键同指纹同创建人重放原批次（`replayed=true`），同键换文件/换人 409 `IDEMPOTENCY_KEY_REUSED`；可选 `metadata` 表单字段（JSON 数组，逐文件 title/tags/description，缺省用文件名作标题）；任一文件不合格**整批 422** `BATCH_VALIDATION_FAILED`（逐文件 `index/filename/code/message`），不落库、不写对象；201 返回 `{batch_id, replayed, documents[]}`。
- 文件校验（D6）：扩展名白名单 PDF/MD/TXT/DOCX/PPTX/PPT + 魔数/结构校验（OOXML 必须含 `[Content_Types].xml` 与主文档、PPT 需 OLE 头与 `PowerPoint Document` 流名、TXT/MD 按 UTF-8→GB18030 严格解码且含 NUL 视为二进制）；ZIP 条目数/单条目/总量/压缩比上限 + 拒绝加密条目与路径穿越；声明的 MIME 只做快速拒绝，落库用服务端判定的 canonical MIME。
- `GET /school/kb/documents`、`GET /school/kb/documents/{id}`、`GET /school/kb/documents/{id}/versions`（`school.kb.view`）——按数据范围过滤（`school` 全校 / `self` 只看本人上传；解析不到范围 = 空集），cursor 分页默认 20 / 上限 100；范围外与跨租一律 404。
- 扫描：arq `scan_kb_version`（短事务：读版本 → 结束事务 → 事务外读对象并扫描 → 条件更新）；通过 → `PENDING_APPROVAL`，感染/完整性失败 → `SCAN_FAILED`（先隔离留证再删原件），瞬时故障**保持 `UPLOADED`** 并累计 `scan_attempts`/`scan_error_code`，看门狗 `retry_pending_kb_scans` 每 5 分钟按租户补投（`scan:<version_id>:<attempt>` 作 job_id）；响应中的 `scan.state` 是**派生字段**（pending/error/passed/infected/skipped），不新增 `SCANNING` 状态。
- 审计：`school.kb.documents.uploaded`、`school.kb.scan.passed`/`infected`/`failed`（系统执行者）、`school.kb.upload.denied`（校验失败/幂等冲突，独立事务）。本点不提供审批/构建接口，`assert_submittable` 只接受 `PENDING_APPROVAL`；开发旁路（`DEV_SKIP_FILE_SCAN` / `CLAMAV_FAIL_CLOSED=false`）只在 development/test 可用，staging/production 启动即拒绝。

**M3 KB-01B 已实现并合并（前端交互口径，2026-10-31，PR #35 → 合并提交 `d6de5a6`）**：

- 路由与菜单：`/kb/documents`（`meta.permission=school.kb.view`）与 `/kb/upload`（`school.kb.upload`）各一项菜单，位于「部门树」之后、「批量导入」之前；前端只做体验层，逐请求 403 仍由后端裁决。教师（只有 upload）不出现「知识库」入口、直连列表进 403；校方管理员（只有 view）不出现「上传文档」。
- 上传页：批量选择 → 本地预检（1–15 个、单文件 ≤50MB、合计 ≤300MB、0 字节、扩展名白名单；与 `backend/app/kb/validation.py` 同界，魔数/ZIP 炸弹/文本编码仍由后端判）→ 可选元数据（标题默认文件名去扩展名、标签逗号分隔、描述 ≤1000 字；**开关关闭＝整段不发送、已填值保留**；任一行有编辑时发送与文件等长的数组，未编辑行发 `{}`）→ 上传。
- 幂等键：签名 = 有序文件 `name|size|lastModified` + 元数据 JSON；签名不变的重试复用同一键（`crypto.randomUUID()`），文件或元数据变化换新键；409 `IDEMPOTENCY_KEY_REUSED` 强制下次换新键；422/413/网络错误保留键原样重试；201 `replayed=true` 明确提示「未创建新文档」。
- 错误分层：本地预检行内红字且不发请求；后端 422 顶部「整批未保存」+ 逐文件错误表（后端 0 基 `index` 显示为 `index+1`）；413 显示后端字符串文案；403 交全局只提示一次；网络错误保留选择与键可重试。
- 结果页轮询（仅 `school.kb.view`）：对本批次仍 `pending` 的文档每 5 秒串行请求一次详情——上一轮完成才排下一轮（同一时刻最多一轮、≤15 个请求）；页面隐藏暂停、5 分钟截止、403 停止、全部落定即停；404 的文档本轮内不再重试；组件卸载即停。无 view 的教师看到静态说明「扫描在后台进行」。扫描/引擎文案：`dev-skip`/`dev-degraded` 一律警告色，未知档位显示原码，绝不默认成「已通过」。
- 文档列表：cursor 栈翻页（`limit=20`，下一页只认 `next_cursor`），加载中禁用上一页/下一页/刷新；加载失败保留旧行并显示「数据可能不是最新（上次成功 HH:MM:SS）」横幅 + 重试（TD-260 的口径，不做静默保留）；扫描列同时显示 `error_code`/`signature`。
- 详情抽屉：打开即请求详情 + 第一页版本（`limit=20`，重新拉取，不用列表快照），版本「加载更多」按 `next_cursor` 追加；文档 404 提示「文档已不可见」一次、关闭抽屉并刷新当前页；切换文档后旧响应/旧 404 不写入新抽屉。
- 全栈 E2E 已由 **KB-01C** 交付（真实后端 + 真实库，扫描用 E2E 栈的 dev-skip；PR #37 → 合并提交 `7d1ce56`，4 例）；真实 ClamAV 引擎覆盖延后 TD-284。

**M3 KB-02 已实现（后端，2026-11-07）**：

- `POST /school/kb/document-versions/{version_id}/submit-approval`（`school.kb.upload`，数据范围 fail-closed）——只有 `PENDING_APPROVAL` 可提交（409 `KB_NOT_SUBMITTABLE`，KB-01A 的 `assert_submittable`）；同版本已有 PENDING 单 409 `APPROVAL_ALREADY_PENDING`（显式判据 + 部分唯一索引 `uq_approval_requests_tenant_version_pending` 兜底，按约束名窄化）；开单时把版本快照（version_no/source_hash/filename/mime/size/scan_engine/文档标题标签描述）写进 `approval_requests.payload_json`；审计 `school.kb.approval.submitted`。
- `POST /school/approvals/{id}/approve|reject`（`school.kb.approval`）——提交人与版本上传者自批恒 403（`sod.py::evaluate_self_approval` 判定内核，例外开关不接入，TD-286）；驳回理由必填（422）、理由落审批单与审计；通过/驳回在**同一事务**内把版本与文档状态迁到 `APPROVED`/`REJECTED`（锁序：审批单 → 版本 → 文档，与 scan_task 的 版本→文档 无环；行锁读带 `populate_existing`，防止身份映射缓存旧 PENDING 造成并发双赢家）。
- `POST /school/approvals/{id}/cancel`（`school.kb.upload` 自助）——提交人撤销 PENDING 单（引擎强校验 submitter），版本保持 `PENDING_APPROVAL` 可再提交；非提交人 403。
- `GET /school/approvals/todo|done|{id}`（`school.approval.todo`）——待办 = 全部 PENDING；已办 = 与本人有过动作往来的单；详情含步骤与动作留痕、版本快照。审批是租户级治理动作，按权限码鉴权、不按提交人数据范围裁剪（简报 D5，与 TD-237 同源；登记 TD-289）。
- `POST /school/kb/document-versions/{version_id}/build`（`school.kb.build`）——构建门禁：只有 `APPROVED` 可创建，未审批 409 `KB_NOT_APPROVED` + 审计 + **零任务行**；APPROVED 创建 → `kb_build_jobs` 一行 `BUILD_QUEUED`；同版本最多一个活跃任务（部分唯一索引 `uq_kb_build_jobs_tenant_version_active` 兜底并发）；本点任务滞留 QUEUED，快照接口与取件执行器是 KB-03 首个交付项（TD-288）。
- `PUT /school/kb/documents/{document_id}`（`school.kb.upload`，数据范围 fail-closed）——标题/标签/描述编辑，**免审**（版本状态不变）但审计 `school.kb.metadata.updated`（前后值；无变化不审计；描述显式 null = 清空）；strip 口径与上传侧 `FileMetadata` 一致。
- 审计：`school.kb.approval.submitted/approved/rejected/cancelled/denied`、`school.kb.build.created/denied`、`school.kb.metadata.updated`；403/409 业务拒绝走**独立事务**写 denied 审计（404 探测不审计，不泄露存在性）。
- 幂等指纹（TD-277 收口）：`_request_fingerprint` 并入 metadata 规范串（标题/描述 strip、标签排序）——同键同文件不同元数据 409 `IDEMPOTENCY_KEY_REUSED`，同元数据重放原批次。

**M3 KB-03A 已实现（后端，2026-11-07）**：

- 构建执行管线（arq `build_kb_version`，三段式短事务）：取件（BUILD_QUEUED→BUILDING，版本 APPROVED/FAILED→BUILDING）→ 事务外执行（PARSING→NORMALIZING→CHUNKING→EMBEDDING→INDEXING→VALIDATING，逐阶段短事务写 stage/progress/heartbeat）→ 收口（READY/FAILED + chunks 原子落库 + kb_index_versions + 审计 `school.kb.build.completed/failed`）；
- 解析（`app/kb/parser/`）：`DocumentParserPort` + MinerU V1 `/v1/file_parse` 适配器（tier 在请求体，2026-11-07 契约实测）+ `TieredParser` 降级链（tier 顺序 Settings 可配，失败原因链式落 `fallback_reason`）；TXT/MD 直接规范化；DOCX/PPTX 诚实失败 `PARSER_UNAVAILABLE`（TD-293）；
- 切块（`app/kb/chunker.py`，纯函数）：父子块、参数 Settings 可配（TD-294 临时默认 500/15%/1000）、表格/代码不拆、超长段句界优先硬切、重叠按码点、跨节不重叠；
- 嵌入/稀疏（`app/kb/embedding.py`）：TEI `/embed` dense（BGE-M3）+ jieba/BM25 sparse（按版本语料 IDF + 标准长度归一 avgdl + token sha256 uint32 哈希）；dense 输入拼「文档名>章节路径」前缀；
- Qdrant（`app/kb/indexer.py`）：collection `tenant_{sha256前12位}_kb_v{version_id}`、named vectors dense+sparse、payload 十字段（`tenant_id_hash` 不落明文租户 id，`is_published` 构建期 false）、删旧建新幂等 + upsert/VALIDATING 双对账；
- 快照（TD-288 兑现）：`GET /school/kb/build-jobs/{job_id}`（`school.kb.view` + 数据范围，全字段）；重试 `POST .../retry`（只有 FAILED，attempt+1/retry_count+1 不覆盖历史，审计 `school.kb.build.retry`）；
- 看门狗（cron 15 分钟错位）：BUILDING 心跳超时 → FAILED/STALE（可重试）；QUEUED 滞留重投（入队 best-effort 丢失的恢复路径）；
- arq `Function(build_kb_version, timeout_s=1800)`：显式覆盖 worker 默认 300s job_timeout（MinerU 冷启动 248s 实证、上限 600s，默认值会腰斩合法解析——审查 B2）；
- 收口 attempt 校验：STALE→重试后旧 worker 的迟到收口被 attempt 等值条件拒绝（不覆盖新 attempt 的 chunks/向量——审查 M1）；
- 异常漏斗：ParserError/EmbeddingError/IndexError_/ObjectStoreError/UnicodeDecodeError/SQLAlchemyError/httpx/OSError/ValueError 全部落 FAILED + error_code（GBK 文本 → `NORMALIZE_DECODE_FAILED`，Qdrant 抖动 → `INDEX_UNAVAILABLE`——审查 B1）。

### 5.6 构建进度 SSE

事件：

```text
event: build.stage
data: {"job_id":"...","stage":"PARSING","tier":"standard","started_at":"..."}

event: build.progress
data: {"job_id":"...","stage":"PARSING","progress":0.42,"processed_units":42,"total_units":100,"message":"..."}

event: build.log
data: {"job_id":"...","level":"INFO","message":"...","ts":"..."}

event: build.done
data: {"job_id":"...","status":"READY","chunks":128,"duration_ms":123456}

event: build.error
data: {"job_id":"...","stage":"EMBEDDING","error_code":"EMBED_TIMEOUT","message":"...","retryable":true}
```

- `kb_build_events.seq` 递增；`Last-Event-ID` 或 `?since=<seq>` 续传。
- 前端 fetch + Bearer；EventSource 无法带 Authorization header，不使用原生 EventSource。
- 轮询兜底：`GET /school/kb/build-jobs/{job_id}` 每 3s。
- 权限：`school.kb.view` + 数据范围；跨租 404。

### 5.7 分块预览 / 检索测试

`GET /school/kb/versions/{version_id}/chunks?cursor&limit&q&section&type` 返回：

```json
{
  "items": [{
    "chunk_id": "...",
    "parent_id": "...",
    "chunk_type": "child",
    "section_path": "第一章 > 1.2",
    "page_start": 3,
    "page_end": 4,
    "char_count": 420,
    "token_count": 256,
    "chunk_hash": "...",
    "text_markdown": "...",
    "embedding_status": "ready",
    "is_human_edited": false,
    "edited_by": null,
    "metadata": {"table": false, "code": false}
  }],
  "next_cursor": "...",
  "stats": {"parent_count": 12, "child_count": 128, "avg_chars": 410, "overlap_ratio": 0.12}
}
```

`POST /school/kb/versions/{version_id}/retrieval-test`：

```json
{
  "query": "奖学金申请条件",
  "top_k": 10,
  "as_user_id": null,
  "as_audiences": null,
  "ignore_acl": false
}
```

- 默认按**操作者自身**的人群集合过滤（与 C 端问答同一套过滤逻辑，保证"测试能复现线上"）。
- `as_user_id` / `as_audiences`：校方用"某用户/某人群视角"验证 ACL 配置是否命中预期。
- `ignore_acl=true`：仅校方超管（`school.kb.retrieval_test.unfiltered`）可用，用于排查"配置后为何查不到"，调用强制写审计。

返回 dense/sparse/RRF/rerank 分数、chunk_id、引用预览，以及每个候选块命中的 ACL token；用于解释召回原因。

**块编辑（K5）**：`POST /school/kb/chunks/{chunk_id}/edits` 提交 `{new_content, reason, base_hash}`；冲突返回 `409 CHUNK_CONFLICT`。`POST /school/kb/document-versions/{id}/derive` 在校方超管确认后生成派生版本，构建只重嵌改动块（未改块复用 `kb_embedding_cache`）；发布沿用 KB-05 的 alias 切流，旧会话仍固定源版本。

### 5.8 C 端 Chat SSE

**文档人群过滤（K4，M4 落地）**：每轮检索前由服务端把登录身份解析为人群 token 集合（B 端：部门/角色/任课·班主任班级/课程/自身；C 端学生：班级/年级/自身），作为 Qdrant 过滤条件（至少命中一个 token）；请求体中的任何人群/租户字段一律忽略。解析失败或结果为空按空集处理（fail-closed）→ 不召回、不引用、逐项兜底话术，不提示"存在但无权"。未经授权时回答、引用、错误信息均不得出现该文档的标题/章节。

`POST /campus/chat/turns`（`Accept: text/event-stream`）：

```text
event: meta
data: {"turn_id":"...","trace_id":"...","conversation_id":"..."}

event: step
data: {"stage":"retrieve","label":"正在检索学校知识库"}

event: tool_call
data: {"tool":"kb.search","status":"running"}

event: tool_result
data: {"tool":"kb.search","status":"ok","duration_ms":180,"hits":8}

event: reasoning_summary
data: {"summary":"已找到 3 条相关校规，正在核对引用"}

event: delta
data: {"text":"根据学校规定，"}

event: citation
data: {"index":1,"chunk_id":"...","document":"奖学金评定办法","page":3,"section":"第二章"}

event: done
data: {"tokens_in":1200,"tokens_out":300,"first_token_ms":820,"latency_ms":4200,"stop_reason":"FINAL_ANSWER"}

event: error
data: {"code":"LLM_TIMEOUT","message":"生成超时，请重试","retryable":true,"partial":true}
```

- 思考摘要只在高层次；不发送原始 CoT/系统提示词/工具参数。
- 用户停止：客户端 abort；服务端取消下游 LLM/工具任务。
- 断线重连：客户端可用 turn_id 查询最终状态；不支持续传无限 token 流，重连后展示已完成部分/重试。
- 兜底链（M4-AI01b 落地）：检索零命中/人群空集 → 兜底话术 delta + done(CAPABILITY_FALLBACK)，不提示「存在但无权」；LLM 未配置 → error(LLM_NOT_CONFIGURED) 无 done；LLM 失败按已发 delta 标 partial；客户端断开 → trace USER_CANCELLED。引用校验的解释口径：存在性/归属落在 RETRIEVE 的证据文本 PG 回查（检索结果本就租户+版本定域），VERIFY=引用序号一致性。

### 5.9 学校注册与审核（M1-B）

```text
POST /api/v1/platform/tenants/register        # 公开；multipart：school_name/address/leadership/email/qualification
POST /api/v1/platform/tenant-requests/{id}/review   # result=approved|rejected + reason
GET  /api/v1/platform/tenants                 # 平台侧列表
GET  /api/v1/platform/tenant-requests         # 平台侧申请列表（?status=pending/approved/rejected）
GET  /api/v1/platform/tenants/{id}/review-records
```

- 状态机：`pending → approved | rejected`（一次申请只允许一次审核，`SELECT ... FOR UPDATE` + 唯一约束保证幂等）；数据库触发器进一步禁止审结后的任何状态迁移。
- 去重：邮箱严格校验（单一 `@`、无空白/控制字符、local ≤64、总长 ≤254）且规范化后唯一（仅 `pending/approved` 占用，被拒可重报）；校名重复只置 `school_name_duplicate_hint`。
- 通过/拒绝统一调用 `review_registration_request(...)`（**SECURITY DEFINER**，`search_path=pg_catalog,public,pg_temp` 且函数内**所有表名显式限定 `public.`**，防止调用方用同名 TEMP 表遮蔽）：函数内 `SELECT ... FOR UPDATE` 锁定申请 → 校验 pending → 通过时原子创建租户（active + 学校编号）、3 个内置角色、超管账号与角色绑定 → 写审核记录（含凭证快照）→ 写审计 → 状态迁移；平台运行角色只能 EXECUTE 审核函数，不能直接改申请状态/插审核记录/写租户身份；内部分通函数 `provision_tenant` 对 PUBLIC 与平台一律撤销 EXECUTE（`e5c9d1f7a2b4` 对已应用 `d9` 的环境幂等重放）。
- 邮件仍在事务内发送：失败抛 502 并回滚整笔开通（申请保持 pending，可重试），避免"已激活但校方拿不到密码"；反向窗口（邮件已发出、事务提交失败）不会产生可登录账号，仅造成无效密码邮件，由重试生成新密码覆盖（TD-088）。
- 校方超管权限只取 `permissions.module IN ('school','campus')`；`platform.*` 属平台 realm，不授予租户。
- **平台最小权限**：`users/roles/role_permissions/user_roles` 零权限；`tenant_registration_requests` 仅 SELECT+INSERT（提交用，无 UPDATE/DELETE）；`school_review_records` 仅 SELECT（只追加触发器兜底）；`tenants` 仅 SELECT + 列级 `UPDATE(status,status_reason)`。开通/审核只能通过 `review_registration_request()`，无法向存量租户植入账号或伪造审核记录。
- 学校编号：`SCH-` + 8 位随机（字符集排除 0/O/1/I/L），唯一约束兜底；开通函数调用外层用 SAVEPOINT 对编号冲突换号重试。
- 对象存储 key：`onboarding/<request_id>/qualification.<ext>`、`reviews/<review_record_id>/qualification.<ext>`；开发/测试用本地目录，生产用 S3/MinIO。注册**先查重再写对象**，失败清理，避免重复提交产生孤儿文件；文件类型以魔数校验（PDF/PNG/JPEG），MIME 声明仅作快速拒绝。
- 请求体限制**按路由**生效（仅 `/platform/tenants/register` 11MB，避免给后续 KB 50MB/300MB 上传引入全局上限）：有 `Content-Length` 直接拒绝；chunked/无长度时先缓冲并累计字节数，超限返回 413；未登记路由原样透传（不做缓冲）。
- 鉴权：使用平台账号登录 + `platform.tenant.review`/`platform.tenant.view` 权限（开发令牌已删除，TD-078）；权限拒绝（含 realm 不符）写 `auth.permission.denied` 审计（带 reason）。
- 审计脱敏：邮箱只落掩码（`a***@domain`），不写明文联系方式；`request_id` 贯穿提交/审核，与响应头 `X-Request-ID` 一致。
- 启动自检（预发/生产）：**租户与平台两个连接**都不得为超级用户/BYPASSRLS；逐表要求 FORCE RLS、**每条策略都引用租户/平台上下文且只授予两个应用组角色**（防 `USING (true)` 宽松策略）；审核函数 owner 必须可绕过 FORCE RLS；无租户上下文时租户数据不可见。任一失败拒绝启动（TD-073）。

### 5.10 审批 / 请假 / 消息 / 删除

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/school/approvals/todo` | 待办 |
| GET | `/school/approvals/done` | 已办 |
| GET | `/school/approvals/{id}` | 详情（含附件） |
| POST | `/school/approvals/{id}/approve` | 通过 |
| POST | `/school/approvals/{id}/reject` | 驳回（理由必填） |
| POST | `/school/approvals/{id}/cancel` | 提交人撤销自己的 PENDING 单（KB-02；`school.kb.upload` 自助路径，引擎强校验 submitter） |

**M3 KB-02B 已实现（前端，2026-11-07）**：审批中心页 `/approvals`（挂 `school.approval.todo`；待办/已办两 tab 游标分页 + 详情抽屉：版本快照、步骤与动作留痕、驳回理由）；通过/驳回挂 `school.kb.approval`（通过一次确认、驳回理由前端先拦 strip 非空 + ≤500 码点）、撤销挂 `school.kb.upload` 自助（提交人判据）；自批**不做前端预判**（前端无版本归属数据，403 交后端裁决）。KB 文档抽屉入口：按状态+权限给「提交审批」（仅 `PENDING_APPROVAL`）/「创建构建」（仅 `APPROVED`），重复动作吃后端 409（服务端是唯一门禁）；REJECTED 文档给「到审批中心 → 已办查看」指引（文档维度直显理由挂 TD-291 → KB-03）；`kb-logic` 补 `APPROVED/REJECTED` 文案。mock 判据以函数名锚点手抄后端（TD-253 口径），全栈链路缺口登记 TD-292。
| POST | `/campus/leaves` | 学生提交请假 |
| GET | `/campus/leaves` | 我的请假 |
| POST | `/campus/leaves/{id}/withdraw` | 撤回 |
| GET | `/school/messages` | B 端消息 |
| POST | `/school/messages` | 发送消息 |
| POST | `/school/messages/{id}/read` | B 端已读 |
| GET | `/campus/messages` | C 端消息（C 端已读二期） |
| POST | `/campus/account-termination` | 用户自助删除 |
| POST | `/school/account-terminations` | 管理员撤权 |
| POST | `/school/account-terminations/{id}/appeal` | 申诉 |
| POST | `/school/account-terminations/{id}/freeze` | 冻结（暂停倒计时） |

### 5.11 看板 / 反馈

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/school/dashboard/overview` | 人员/文档/审批/消息/反馈统计 |
| GET | `/school/feedback` | 反馈列表（时间/评分筛选） |
| GET | `/school/feedback/{id}` | 反馈详情（问题/答案摘要/引用） |
| POST | `/school/feedback/{id}/handle` | 标记已处理 |
| POST | `/school/feedback/{id}/reply` | 站内信答复 |

### 5.12 角色权限配置与 MFA 重置（M2-UI2：TD-137/TD-143/TD-126）

前缀 `/school`，全部走租户 realm。角色配置要求 `school.role.manage`，**用户↔角色绑定要求 `school.permission_account.manage`**（M2-UI3：TD-187d，与 TD-187e 的启停共用），MFA 重置要求专用码 `school.personnel.mfa.reset`（D4），**删除角色要求独立的 `school.role.delete`**（M2-UI3：TD-187b）——删是不可逆操作，不能因为"能改权限"就顺带能删角色。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/school/roles` | 角色列表：`id/code/name/is_system/editable/lock_reason/version/defaults_applied/user_count/permission_codes/bindable/resettable`。`bindable` 由服务端下发（超管角色为 false），绑定页据此过滤，不按 code 自建第二份名单。`resettable` 由服务端派生（TD-200：**有出厂定义** 且 **角色可编辑**——重置要连过 `_assert_editable` → `ROLE_DEFAULTS_UNAVAILABLE` 两道门），**不是** `is_system` 的别名：前端按 `is_system` 点亮"恢复出厂"，会在"内置但撤了定义"或"内置锁定角色"上亮着一个恒 409 的按钮 |
| GET | `/school/bindable-roles` | 可经绑定接口授出的角色目录（M2-UI3：TD-187g 决策 2）：`[{id, code, name, is_system}]`，**不含 `permission_codes`**。要 `school.permission_account.manage` 而不是 `school.role.manage`：绑定页要的是"能选哪些角色"，完整权限矩阵是角色配置权的事，只持账号管理权的人不该顺手拿到。超管角色**不列**（列出来 = 能勾不能存） |
| GET | `/school/permissions` | 权限目录（`code/module/action/description/sensitive_self_grant`），前端按 `module` 分组渲染；`sensitive_self_grant` 由后端从 `SENSITIVE_SELF_GRANT_CODES` 派生，前端不复刻清单 |
| POST | `/school/roles` | 创建自定义角色：`{code, name, permission_codes[]?, copy_from_role_id?, reason?}` → `RoleResponse`（`version=0`、`is_system=false`、`defaults_applied=false`）。初始权限**二选一**：显式清单或复制同租户可编辑角色；都不给即空集 |
| PUT | `/school/roles/{role_id}/permissions` | 全量替换权限：`{permission_codes[], version, reason?, self_grant_ack?}` → 最新 `RoleResponse` |
| GET | `/school/roles/{role_id}/scopes` | 该角色当前范围行 `{scope_type, scope_id?, editable}[]`（只读、不审计）。**可编辑性由服务端下发**：`editable=false` 的行本端点既不写也不删（`class`/`course` 缺 `scope_id` 入参通道，见下条错误表），前端不许按类型名自己猜第二份（与 `sensitive_self_grant`、TD-200 同一口径） |
| POST | `/school/roles/{role_id}/scopes/preview` | 范围替换影响面：`{scope_types[]}` → `{version, added[], removed[], affected_users, empties_scope, retained_non_editable[], newly_reaching_self[], requires_confirmation}`。只读、不审计。类型族两档与 `PUT` **同码、相对顺序也同**（未知 → 不可手工授），否则运营会看到"预览说能过、保存被拒"。整体顺序**有意不同**且都是结构性的：`PUT` 的"角色存在/可编辑"夹在两档之间（要先 `FOR UPDATE` 拿到行），预览不锁行、也不做版本 CAS，所以"不存在的角色 + `class`"预览给 422 而 `PUT` 给 404 留痕（越权探测信号优先）；"不存在的角色 + 未知类型"两侧都是 422（类型族第一档都在取行之前） |
| PUT | `/school/roles/{role_id}/scopes` | 全量替换角色级数据范围（M2-UI3：TD-187c）：`{scope_types[], version, reason?, self_grant_ack?}` → 最新 `RoleResponse`。只写 `school`/`self`，与权限替换**共享同一列 `roles.version`**（TD-202） |
| POST | `/school/roles/{role_id}/reset/preview` | 重置出厂影响面（只读，不写库、不审计） |
| POST | `/school/roles/{role_id}/reset` | 执行重置：`{confirm_role_code, reason, version, self_grant_ack?}` → 影响面；权限与数据范围一并重种（D3），两个维度都过自扩权守卫（TD-194，见下）。**自定义角色恒 409 `ROLE_DEFAULTS_UNAVAILABLE`**：没有出厂定义可恢复 |
| POST | `/school/roles/{role_id}/delete/preview` | 删除影响面（`{role_code, name, version, affected_users, permission_codes[], fingerprint}`，只读、不审计） |
| POST | `/school/roles/{role_id}/delete` | 执行删除：`{confirm_role_code, reason, version, fingerprint}`（理由 ≥10 字）→ 与预览同形同值，`version` 是**被删行的最后版本号**（行已不在，不返回从未存在的 `+1`）。`version` 挡"预览后角色权限/范围被改"、`fingerprint` 挡"预览后持有人集合被改"（TD-199，见下）。子表由复合外键 `ON DELETE CASCADE` 清理；权限码集与受影响用户数**在删除前**快照进审计 |
| GET | `/school/users/lookup?login_id=` | 按登录名精确定位用户：`{id, display_name, user_type, status, mfa_enabled, roles[]}`，不回显登录名/邮箱/身份证/MFA 字段 |
| GET | `/school/permission-accounts` | 权限账号列表（M2-UI3：TD-187g 决策 1）：`{items[{id, display_name, user_type, status, role_codes[], roles_version, mfa_enabled}], next_cursor}`。要 `school.permission_account.manage`。**按账号类型过滤**（教师 + 校方管理员，与绑定写路径同一集合），学生与 `deleted` 不列，**不按人员数据范围裁剪**（权限账号管理是租户级操作，按 `school`/`self` 裁会让管理员看不见自己要管的账号），**不回显 `login_id`**（学号/工号属敏感标识符）。cursor 分页，游标解不开 → 400 而不是静默回到第一页 |
| GET | `/school/users/{user_id}/roles` | 该账号的绑定快照：`{user_id, display_name, user_type, status, roles_version, roles[{id, code, name, is_system, bindable}], permission_codes[]}`。只读、不审计成功查询；`roles_version` 是下一次 `PUT` 必带的绑定版本 |
| PUT | `/school/users/{user_id}/roles` | 全量替换绑定集（M2-UI3：TD-187d）：`{role_ids[], version, reason?}` → GET 同形 + `added/removed_role_codes`、`added/removed_permission_codes`。空清单合法（收回全部权限）；`users.roles_version` CAS，冲突 409 带最新快照 |
| POST | `/school/users/{user_id}/mfa/reset` | 管理员重置 MFA：`{reason}`（≥10 字）→ 204；清绑定+清锁定+清恢复码+撤刷新令牌家族+`token_version+1`+清 Redis 会话 |
| POST | `/school/users/{user_id}/deactivate` | 停用权限账号（M2-UI3：TD-187e）：`{reason}`（去空白后 ≥10 字）→ 204；`status='disabled'` + 撤**未撤销且未过期**的刷新家族（`revoked_at is null and expires_at > now()`，`admin_revoke`）+ `token_version+1` + 提交后清 Redis 会话与 MFA 待激活 secret。目标限教师/校方管理员（学生 422）、超管目标 403、停用自己 409、重复停用 409。跨租/不存在的 id 在取账号锁**之前**就被 404 挡下（advisory key 不含租户，见 §5.12） |
| POST | `/school/users/{user_id}/activate` | 重新启用（M2-UI3：TD-187e）：无请求体 → 204；`status='active'` 并清失败计数与锁定窗口；**不动 MFA、角色绑定与 `token_version`**。仅 `disabled → active` 合法，其余状态 409；已删除/跨租/不存在 404 |

**错误契约**：`detail = {code, message}`；`409` 版本冲突额外带 `detail.latest`（完整角色快照），前端据此刷新草稿而不是静默覆盖。二次确认字段 `confirm_role_code` 的宽度**有意比 `code` 的上界（63）宽**（收 64）：它只做逐字相等比较，超长必然是"用错了"，要让它走到服务层才有 `{code,message}` 形状的 `ROLE_CODE_MISMATCH`——收成 63 会先撞 Pydantic 的数组形 422，把错误契约打断（与重置侧同界）。

| code | HTTP | 触发 |
|---|---|---|
| `ROLE_NOT_FOUND` | 404 | 角色不存在或不属于本校（不存在与跨租同形，不区分「没有」与「不是你的」） |
| `USER_NOT_FOUND` | 404 | 登录名未命中或用户不在本校。权限账号侧（TD-187d 绑定 / TD-187e 启停 / MFA 重置）**四种来源同形**：id 不存在、在别租、已软删（`status='deleted'`）、无锁归属探针未命中（判据顺序见本文 §5.12）——"没有"与"不是你的"与"曾经有"一律同一个 404，免得响应时间或状态码把"这个 id 存在过/正被人写会话"泄露给猜测者 |
| `ROLE_IMMUTABLE` | 409 | 目标角色不可编辑：内置锁定角色（`school_super_admin`/`student`/`teacher`）给出 `lock_reason`；自定义角色自 M2-UI3（TD-187b）起可编辑，该码对它们不再出现 |
| `ROLE_VERSION_CONFLICT` | 409 | 提交的 `version` 与库内不一致，附 `detail.latest`（完整角色快照）。权限替换与范围替换**共用这一码这一形状**：两条写路径本就共享同一列版本，冲突对运营是同一件事——"这个角色被别人改过，刷新重试"；再造一个 scope 专用码只是让前端多认一个语义不变的分支 |
| `ROLE_DELETE_PREVIEW_STALE` | 409 | 执行删除时重算的影响面指纹与提交值不一致（预览后持有人集合被改，TD-199），附 `detail.latest_preview`（人数 + 指纹 + 版本）。与 `ROLE_VERSION_CONFLICT` 分流：前者管持有人、后者管角色自身配置；**不写拒绝审计**——并发改绑是正常配置，不是安全事件 |
| `PERMISSION_CATALOG_OUT_OF_SYNC` | 409 | 权限目录与库里对不上（代码已加新码、同步未跑），提示先执行 RBAC 同步：全量替换=提交的合法码在 `permissions` 查不全（写前探针 + 写后条数复核）；重置=按 `rbac_builtin_role_defaults` 重种的实际条数少于定义表应有的条数；创建=复制源/显式清单的合法码 insert…select 落库条数少于预期 |
| `ROLE_DEFAULTS_UNAVAILABLE` | 409 | 重置目标没有可用的出厂定义（预览与执行同判据），或出厂数据范围重种条数与定义表不一致。自定义角色走前一条：`is_system=false` 没有出厂定义，重置对它是**结构性不可能**（配置页的「恢复出厂」按钮因此对自建角色永久禁用） |
| `MFA_NOT_ENABLED` | 409 | 目标用户未绑定 MFA |
| `UNKNOWN_PERMISSION_CODE` | 422 | 目录外的码 |
| `PLATFORM_PERMISSION_FORBIDDEN` | 422 | 试图授予平台 realm 的码 |
| `UNKNOWN_SCOPE_TYPE` | 422 | 提交的数据范围类型不在模型的 `ScopeType`（`KNOWN_SCOPE_TYPES`）内。**判定排在可编辑性之前**：`{'bogus','class'}` 报本码而不是 `SCOPE_TYPE_NOT_EDITABLE`——可编辑性要先"认识"这个类型才谈得上判定。入参非法族，不留安全审计 |
| `SCOPE_TYPE_NOT_EDITABLE` | 422 | 试图手工把 `class`/`course` 写进角色范围。真原因是**入参维度不够**：这两类必须绑定具体班级/课程 id（库 CHECK `ck_data_scopes_scope_id_required_for_class_course`，缺 id 的行在解析层 `scope_context_from_rows` 直接抛 `ValueError` = 该角色每个持有人下一次请求 500），而本端点只有"类型"这一维、没有"选哪个班"的通道；`course` 另外还本期不贡献可见性（TD-151）。注意这条理由**不是**"会被 `scopes_sync` 差量删掉"——那句只对**用户级**行成立（同步的读写谓词都限定 `user_id is not null`），角色级同型行今天没有任何合法写入方（勘误见 `docs/review-log.md` TD-187c 轮）。这条拦的是"往库里写一条本端点补不全的行"的**意图**，与 `ROLE_CODE_NOT_DELEGABLE` 同族 → **留 `*.scopes.denied` 并在 `metadata.requested_scope_types` 记被探测的清单** |
| `SCOPE_CATALOG_OUT_OF_SYNC` | 409 | 库内 CHECK 与模型 `ScopeType` 漂移（模型加了类型、迁移没跟上）时 `INSERT` 撞 `ck_data_scopes_scope_type_valid`，或写后条数与提交数不符。约束名按**库里全名**比较：`app/db/base.py` 的命名约定给模型里声明的名字加 `ck_<table>_` 前缀，写成 `scope_type_valid` 会让这条 409 变成死代码、实际表现是纯文本 500（TD-187c 审查 Major 1；由 `tests/integration/test_rbac_custom_role_constraints.py` 拿 `pg_constraint` 钉住）。**只翻译这一条约束**，其余 `IntegrityError` 原样抛出（不把真故障伪装成业务冲突，与 TD-187b 的并发兜底同口径） |
| `ROLE_CODE_NOT_DELEGABLE` | 422 | 非超管主体试图把 `school.role.manage` 写进任一角色（见下） |
| `SELF_GRANT_CONFIRMATION_REQUIRED` | 422 | 本次写入的结果集**触达操作者自己所在的角色**且**净增**敏感码，但未带 `self_grant_ack=true`（TD-191 收口，见下） |
| `SELF_GRANT_REASON_REQUIRED` | 422 | 已勾选 `self_grant_ack` 但理由为空或全空白 |
| `ROLE_INITIAL_SOURCE_AMBIGUOUS` | 422 | 创建角色时同时给出 `permission_codes` 与 `copy_from_role_id`（初始权限二选一，服务端不猜） |
| `ROLE_COPY_SOURCE_NOT_EDITABLE` | 422 | 复制源是内置锁定角色（超管/C 端/学生）——把超管全量搬进自建角色再绑给自己，会绕开"只能从可编辑角色起步"的配置直觉 |
| `ROLE_CODE_TAKEN` | 409 | 创建的角色码撞内置保留码、撞本租户既有自建码，或并发建同名码被唯一约束挡下（`IntegrityError` → 人读 409，不是 500） |
| `ROLE_NOT_DELETABLE` | 409 | 删除目标是内置角色（`is_system=true`），只能重置或改配置 |
| `ROLE_DELETE_REASON_REQUIRED` | 422 | 删除理由 <10 个字符 |
| `ROLE_CODE_MISMATCH` | 422 | 二次确认的角色编码与目标不一致（重置与删除同一码） |
| `RESET_REASON_REQUIRED` | 422 | 重置理由 <10 字（角色重置与 MFA 重置同一码） |
| `BINDING_SELF_FORBIDDEN` | 403 | 试图改**自己**的角色绑定（决策 #5）。这是"建含敏感码的角色 → 绑给自己"整条链的承重墙，判据只比较 `Principal` 与路径参数，**先于对目标账号的任何读取与状态写入**；拒绝审计另起独立事务 |
| `BINDING_TARGET_NOT_ALLOWED` | 422 | 目标账号类型不是 `teacher`/`school_admin`（「权限账号」是 B 端概念，给学生绑后台角色等于绕过 B 端账号边界） |
| `BINDING_SUPER_ADMIN_FORBIDDEN` | 403 | 目标账号**当前持有**超管角色——增、删、清空一律拒绝（超管由平台开通流程管理，与 MFA 重置同口径） |
| `ROLE_NOT_BINDABLE` | 422 | 提交的角色含超管角色（否则持 `permission_account.manage` 的人可把任意账号提成超管，绕过平台开通） |
| `BINDING_VERSION_CONFLICT` | 409 | 提交的 `users.roles_version` 与库内不一致（同一账号被并发改绑），附 `detail.latest`（最新绑定快照）。不写拒绝审计 |
| `LOGIN_ID_REQUIRED` | 422 | 定位时登录名为空 |
| `MFA_SUPER_ADMIN_FORBIDDEN` | 403 | 试图重置校方超管的 MFA（防止把唯一能改角色的账号锁死；由平台 CLI 兜底） |
| `REALM_MISMATCH` | 403 | 非租户上下文调用（平台 realm 不能用这套接口） |
| `MFA_SELF_RESET` | 409 | 管理员重置自己的 MFA（应走恢复码/自助关闭，避免绕过双人确认） |
| `ACCOUNT_SUPER_ADMIN_FORBIDDEN` | 403 | 目标账号持有 `school_super_admin`，停用与启用一律拒绝（TD-187e）。超管离职的停用由平台侧兜底（TD-224，M6 前无 CLI） |
| `ACCOUNT_SELF_DEACTIVATE` | 409 | 操作者停用自己（比较的是 `Principal` 与路径参数，先于对目标账号的任何读取与状态写入；拒绝审计是另一条独立事务，不在此列） |
| `ACCOUNT_ALREADY_DISABLED` | 409 | 停用一个已停用的账号（并发停用也只有一路成功，另一路得到本码） |
| `ACCOUNT_ALREADY_ACTIVE` | 409 | 启用一个已启用的账号 |
| `ACCOUNT_NOT_DISABLED` | 409 | 启用 `locked` 账号：解锁走锁定窗口到期或密码/MFA 重置（独立解锁动作登记 TD-225） |
| `ACCOUNT_TARGET_NOT_ALLOWED` | 422 | 目标 `user_type` 不是 `teacher`/`school_admin`（学生是 C 端账号，不在这条权限的射程内；判据与用户↔角色绑定的 `BINDABLE_USER_TYPES` 同源）。类型判据先于状态机，两个方向同码 |
| `DEACTIVATE_REASON_REQUIRED` | 422 | 停用理由去空白后 <10 字。入参非法族，**不写安全审计**（与 `RESET_REASON_REQUIRED` 同口径） |

**可编辑范围**：内置角色里只有 `EDITABLE_ROLE_CODES = {school_admin, teacher_b_end}` 可改（超管角色的权限由代码目录全量驱动，界面只读并透出原因）；自定义角色（`is_system=false`）自 M2-UI3 起**可编辑、可删除，但不可重置出厂**。

- **自定义角色创建与删除的不变量（M2-UI3：TD-187b，简报 §4.1/§4.2）**：
  - **码的格式与保留**：`ROLE_CODE_PATTERN = ^[a-z][a-z0-9_]{2,62}$` **定义**在服务层（单一来源，Pydantic 从这里取），**强制**它的是两处：接口的 `Field(pattern=...)` 与库层 `ck_roles_code_format_valid`。服务函数自己不校验格式——直调 `create_role` 传非法码会撞库 CHECK 变成 500，这是有意的（`tests/integration/test_rbac_custom_role_create_race.py::test_other_integrity_errors_are_not_relabelled_as_code_taken` 正是靠这条不走 409 分支；直调面只由库层守，登记在 TD-198 的同界缺口里）；`tests/test_rbac_custom_role_crud_unit.py` 再拿迁移文件里的 `_CODE_FORMAT` 逐字符比对，防止两侧正则随重构漂移。内置码由 `RESERVED_TENANT_ROLE_CODES`（从 `builtin_roles("tenant")` 派生，不手抄）保留，`is_system=false` 不得占用——库层 `ck_roles_builtin_code_is_system` 兜住"绕过应用代码的手工 SQL"。
  - **`is_system=false` 永不进同步与出厂重种**：同步补种的谓词限定 `r.is_system`，`reset_role` 的出厂定义按 `role_code` 查 `rbac_builtin_role_defaults`。因此运营每接一版代码不会丢自建角色的权限集（BS-M2UI3-A 里跑一次 `sync_rbac_definitions` 后逐项断言码集/`version`/`defaults_applied` 原地不动），重置对它是结构性不可能。
  - **拒绝的顺序 = 版本必须不动**：`create_role` 的转授禁令/复制源检查都在 INSERT 之前；`delete_role` 把「内置角色不可删」排在版本 CAS 之前、「确认码不符」也排在 CAS 之前；`reset_role` 本轮把「出厂定义检查」提到 CAS 之前。理由一致：拒绝若发生在写路径之后，靠事务回滚抹掉版本增量虽然正确，但同事会看到一次莫名的 409，且"这单根本不该进写路径"的判断留在顺序里比留在回滚里好读。
  - **能力只在 PUT 与绑定时流动**：`create_role` **有意不接**自扩权守卫（`_assert_self_grant`）——新建的 `role_id` 此刻必然不在 `principal_role_ids` 里，判据"结果集经 `user_roles` 流到操作者自己所在角色"结构上不可触发。这偏离简报决策 #4 的字面表述（"三条写路径共用一个守卫函数"），代价与再评估口径见 `docs/tech-debt.md` TD-194。"建一个含敏感码的角色再绑给自己"这条链的承重墙是**自绑禁令**（403 `BINDING_SELF_FORBIDDEN`，TD-187d 已落地并有用例）；绑定**别人**的账号不改变操作者自己的权限并集，守卫在绑定侧同样无可达分支（2026-10-29 用户裁定，简报 §14.5）。#5 一旦放宽（自助换岗/代绑），绑定路径必须立刻按"绑定后的操作者权限并集"接守卫。
  - **删除的审计必须在 CASCADE 之前**：`role_permissions`/`data_scopes`/`user_roles` 由复合外键级联清理，删完就取不回，所以 `school.role.deleted` 的 `metadata` 带 `removed_permission_codes` 与 `affected_users` 两份删除前快照；`deleted.rowcount != 1` 必须断言——RLS 的 `USING` 子句若挡下这条 `DELETE`，语句会**静默影响 0 行**，紧接着的审计却写成"已删除"，是库里角色还在、审计说没了的最坏一致性组合。该分支**没有用例**且不是遗漏：读侧 `SELECT … FOR UPDATE` 与写侧 `DELETE` 走同一策略的同一个 `USING`，一致策略下不可能"看得见却删不动"，要命中只能 monkeypatch rowcount（测的是替身），说明写在 `tests/integration/test_rbac_custom_role_create_race.py` 模块头。
  - **删除的双判据：`version` + 影响面指纹（TD-199，2026-10-29 用户拍板，简报 §14.3）**：`version` 挡"预览后角色权限/范围被改"；`fingerprint = SHA-256(role_id + 排序后的持有人 id 集)` 挡"预览后持有人集合被改"——绑定写作不推进 `roles.version`，只靠版本挡不住换人。两套判据各管一件事，互不替代：权限/范围变化只触发 `ROLE_VERSION_CONFLICT`，持有人变化只触发 `ROLE_DELETE_PREVIEW_STALE`（409，回带 `latest_preview`：人数 + 指纹 + 版本）。指纹只回哈希、不回显持有人清单；无过期时间（绑定状态而非时间）；"绑了又解回原集合"指纹相同、删除成功，这是选指纹而非"绑定 bump 角色版本"换来的收益——改绑不会让管理员未提交的权限草稿失效。并发顺序已定：删除先锁角色行，再以 `FOR UPDATE` 锁住当前持有人行（`user_roles`），让"重算指纹 → CASCADE 删除"之间不会插入一次解绑——响应与审计的 `affected_users` 与真正被级联删掉的行集一致（解绑与角色行锁不冲突，不锁持有人行时它能在窗口里提交）；绑定写路径读提交角色时持 `FOR KEY SHARE`（与 `user_roles` 插入的外键锁同档），要么先提交（指纹不匹配 → 409），要么被角色行锁挡到删除提交后（角色已不存在 → 404 `ROLE_NOT_FOUND`）。锁序恒为 `roles → user_roles`，与绑定的 `users → roles → user_roles` 无环。三种层次各有用例：`tests/test_rbac_custom_role_crud_unit.py`（顺序无关/角色域/空集）、`test_rbac_custom_role_create_race.py::test_preview_then_rebind_makes_the_delete_stale` 与 `::test_rebinding_back_to_the_same_set_keeps_the_preview_valid`（真实并发窗口与"解回原集合"）、`test_bs_m2ui3_custom_role.py`（HTTP 端到端）。
  - **删除用独立权限码**：`school.role.delete`（TD-187a 新增，已随同步落库并补进超管全量），与 `school.role.manage` 分授；预览影响面只读、不审计，与重置预览同口径。

- **角色级数据范围编辑的不变量（M2-UI3：TD-187c，简报 §13）**：M2-UI2 只交付了「角色权限」这条写路径，「同一角色看全校还是只看本人」当时只能改库或走重置——本点补的就是这个缺口（口径 ⑨ 的运营化）。
  - **单一来源**：可编辑集 `EDITABLE_SCOPE_TYPES = {school, self}`；类型全集取 `app/rbac/scopes.py:KNOWN_SCOPE_TYPES`（= `get_args(ScopeType)`，模型派生），接口层**有意不用** `Literal` 收窄 `scope_types`——**未知字符串字面量**必须走到服务层才有人读的 `{code,message}` 422，Pydantic 先拦会换成 FastAPI 的数组形校验错误（与 `RoleDeletePayload.confirm_role_code` 同口径）；非字符串元素（`[1]`/`[null]`）由 Pydantic 挡，那是类型错误不是业务判据，不必留到服务层。清单不加 `max_length`（TD-204 已拒绝），重复项在服务层去重后写入。
  - **窄删除 + 结果集取并集**：`replace_role_scopes` 的 `DELETE` 带 `and scope_type = any(EDITABLE_SCOPE_TYPES)`，非可编辑行**不由这条写路径写入，因此也不删**；守卫与预览看到的"替换后结果集"= 提交清单 ∪ 现存非可编辑行。角色级 `class` 行今天没有合法写入方（同步只管用户级行），但删除语句一旦写成"删掉该角色全部范围行"，运营在没点任何按钮的情况下就动了一条带 `scope_id` 的行——那是解析层唯一读得懂 class 可见性的地方；按提交清单算结果集同样会低估守卫看到的范围、让预览的 `added/removed` 与审计差量说谎。用例：角色上手工预埋一条 `class` 行，替换后它仍在、`editable=false`、且出现在预览的 `retained_non_editable` 里。
  - **两个宇宙、一套判据**：`SENSITIVE_SCOPE_TYPES = {school}` + `evaluate_scope_self_grant(...)` 直接把 `evaluate_self_grant` 当内核复用（`kind="scope"` 只影响 422 文案与审计 `metadata.kind`，不改判定数学）。净增口径与权限侧一致：把 `school` 补给一个**已经**看得到全校的角色不是扩权。为什么不把 `school` 塞进 `SENSITIVE_SELF_GRANT_CODES`：两张清单的变更节奏不同（加权限码 vs 加覆盖型范围类型），混成一张表迟早出现"改了默认范围、没人问它算不算敏感项"；新增覆盖型范围类型时必须扩这张表（TD-203）。
  - **空清单合法**（简报 §13.2 决策④）：`scope_types=[]` 是一条真实配置（把角色收成"什么都看不到"，解析层 fail-closed 给空集），服务端不挡；预览用 `empties_scope=true` 配 `affected_users` 提示"N 名持有人将看不到任何数据"，把提示放在服务端判据里而不是前端文案里。
  - **守卫接线**：与权限替换同序——`_require_tenant`（realm 判据先于入参判据：平台主体带着非法类型来，应当 403 而不是 422）→ `_assert_known_scope_types`（不碰库）→ 行锁取角色（`None` → 404 留痕）→ 角色可编辑（`ROLE_IMMUTABLE` 留痕）→ 类型可手工授（留痕）→ 版本 CAS → `evaluate_scope_self_grant` → `_assert_self_grant(kind="scope")` → `version+1` → 窄删除 → 插入 → 审计。两条拒绝（未确认 / 未填理由）都**排在版本推进之前**，且同时留通用 `school.role.scopes.denied` 与专项 `school.role.self_grant`（`result="denied"`、`kind="scope"`、`newly_reaching_self`）；放行侧在 `*.scopes.updated` 之外多一条 `result="success"` 的专项审计。**打断必须真的挡住**：BS-M2UI3-C 断言被打断后范围行一字未动、`version` 没前进、keeper 的人员列表仍是空集。
  - **重置出厂是这条判据的第 4 个入口，TD-187d 已接（TD-194 收口）**：`POST /reset` 按定义表重种，不接收操作者提交的清单，但**结果集仍然会流向操作者本人**，权限与范围两个维度都过 `_guard_self_grant`。权限侧判据通常不触发（出厂默认 ∩ 敏感码 = ∅，由两侧用例守着）；**范围侧是可达路径**——`school_admin` 的出厂默认范围正是 `school`，"别人把它收窄成 `self` → 同角色的管理员点一次恢复出厂 → 拿回全校可见"就成立：未带 `self_grant_ack` 的重置现在 422 `SELF_GRANT_CONFIRMATION_REQUIRED`（排在版本 CAS 之前，`roles.version` 不前进），带 ack 则放行并写 `school.role.self_grant`。`test_factory_defaults_of_editable_roles_do_hold_the_sensitive_scope` 仍把这个**反向**事实钉成契约（默认集 ∩ 敏感范围必须**恰好**是 `school_admin: [school]`），谁改默认谁重新评估。**187g 之前前端没有确认条**：调用方需显式传 ack，这是本批已知的接口契约变化。用例：`tests/integration/test_rbac_reset_self_grant.py`（范围净增无 ack 422、库不动；带 ack 200 + 专项审计；净增为零不需要 ack）。

- **用户↔多角色绑定的不变量（M2-UI3：TD-187d，简报 §14）**：`user_roles` 的第一条写入路径，实现 `app/rbac/binding.py`。
  - **单一权限码 + 读路径同源**：`GET`/`PUT /school/users/{user_id}/roles` 都要求 `school.permission_account.manage`。绑定/解绑**下一个请求即生效**（`app/auth/principal.py` 每请求重读 `user_roles → role_permissions → permissions`），**不碰 `token_version`**、不作废在线会话——与 M2-UI2 权限编辑同口径。
  - **`users.roles_version` 是绑定集的乐观锁**（迁移 `e9b4d2f6a8c1`）：`GET` 下发、`PUT` 必带、每次全量替换 `+1`，不一致 409 并回带最新绑定快照。与 `token_version` 职责不同（后者是令牌作废代数）——借它做绑定 CAS 会把正常配置变成一次强制重登；只由绑定服务读写。
  - **判据顺序（越权先于并发错误，输入合法性先于 CAS）**：realm（403）→ **自绑（403，只比较 `Principal` 与路径参数，先于对目标账号的任何读取与状态写入；拒绝审计另起独立事务）**→ 目标存在且未删除（404）→ 目标**当前持有**超管角色（403，增删清空一律拒）→ 目标类型非 `teacher`/`school_admin`（422）→ 提交的角色存在（404，跨租与不存在同形）→ 提交的角色含超管（422）→ 绑定版本 CAS（409）。角色被删时就该报 404 让调用方刷新，用 409 盖住只会让人以为并发一下就能成功。
  - **自绑禁令是"建含敏感码的角色 → 绑给自己"这条链的唯一承重墙**：操作者改**别人**的绑定不改变自己的权限并集，`evaluate_self_grant` 在绑定侧无可达分支，因此绑定**不接**自扩权守卫（2026-10-29 用户裁定，简报 §14.5）。#5 一旦放宽（自助换岗/代绑），绑定路径必须立刻按"绑定后的操作者权限并集"接守卫。
  - **空集合法**：「收回全部权限」是离职/停岗的正常动作；提交清单去重保序，差异按码集算（`added/removed` 角色码与权限码），与审计写的是同一份数据。
  - **并发**：目标 `users` 行 `SELECT … FOR UPDATE` 串行化同一账号的绑定替换（以及走同一行锁的 MFA 重置）；**不用** `auth.sessions.acquire_user_lock`（advisory lock 的语义是登录/刷新/撤销的会话写操作，借它会把正常配置和登录挤成一条队列）。写路径读提交角色时另持 `FOR KEY SHARE`（与 `user_roles` 插入的外键锁同档）：并发删角色要么先完成（读到 0 行 → 404 + 拒绝审计），要么被挡到本事务提交之后（删除侧由影响面指纹兜住）——锁序恒为 `users → roles`，与删除（`roles → user_roles`）无环。GET 路径不加锁。
  - **审计**：成功 `school.user.roles.updated`（`role_codes_before/after`、`added/removed_role_codes`、`added/removed_permission_codes`、`reason`）；拒绝 `school.user.roles.denied`（独立事务，`reason` 为判据码）；**`BINDING_VERSION_CONFLICT` 不落拒绝审计**——并发改绑不是安全事件，只回快照。超管角色在 `GET /roles` 与绑定快照里都是 `bindable=false`，由服务端下发。

- **权限账号停用/启用的不变量（M2-UI3：TD-187e，简报 §15）**：实现 `app/personnel/account_admin.py`，与 MFA 重置同构（同一把账号锁、同样的提交后 Redis 清理口径）。
  - **停用是"状态 + 会话 + 版本"三件事一起变，各挡一个具体的洞**：`status='disabled'` 挡重新登录、每请求鉴权与刷新（`Principal` 与刷新侧都按状态拒绝）；把**未撤销且未过期**的 `refresh_tokens` 家族置 `admin_revoke`（谓词 `revoked_at is null and expires_at > now()`，与取家族清单的 `select distinct family_id` **逐字一致**）挡**重新启用后旧家族复活**（启用不动 `token_version`，不撤就是留下一颗随时可续期的种子），并在库内留 `revoked_reason='admin_revoke'` 字段（该字段不进任何响应/前端）；已过期家族不撤销也不计入 `sessions_revoked`——它本来就换不出令牌，把它算进去等于虚报"断开了几台设备"；`token_version+1` 让**已签发的 access** 下一请求即 401、同样防复活。因此不能说"少任何一件都有续命窗口"——三件各管一条路径。**Redis 会话标记删除失败不改变任何用户可见结果**（TD-102 降级：`revocation.delete_session` 吞 `RedisError` 只告警，停用仍 204）：设备列表取数是「库内 `revoked_at is null and expires_at > now()` ∩ Redis 标记存在」，家族已在同事务里被撤销，所以**孤儿标记不会让列表多出一条**；access 的 401 由 `status`/`token_version` 给出。实际残留只有一个由 TTL 自动回收的孤儿键。BS-M2UI3-H 的降级用例打桩真实客户端的 `delete`（摘掉那段吞异常代码用例即转红），并断言启用重登后设备列表里查不到那台设备。启用只恢复状态并清 `failed_login_attempts`/`failed_login_window_started_at`/`locked_until`，**不动 MFA、角色绑定与 token 版本**——启用不是重置。
  - **判据顺序**：realm（403）→ 停用理由（422，去空白后 <10 字；`locked`/`disabled` 同样先校验入参）→ 自停用（409，先于对目标账号的任何读取与状态写入）→ **无锁的本租归属探针（404，跑在自己的短事务里）** → 账号级 advisory lock + `users ... FOR UPDATE` → 不存在/跨租（404，同形）→ **已删除（404，先于角色判据——已删除但还挂着超管绑定的账号不得回 403 泄露"曾为超管"）** → 持有超管角色（403，两个方向）→ **目标类型非 `teacher`/`school_admin`（422，与绑定共用 `BINDABLE_USER_TYPES`）** → 状态机（停用 `active`/`locked` → `disabled`；启用仅 `disabled` → `active`；其余 409）。
  - **归属探针为什么必须排在取锁之前（TD-230）**：`acquire_user_lock` 的 key 是 `hashtextextended(f"{realm}:session:{user_id}")`，**不含 `tenant_id`**——同一个 id 无论在哪个租户都落到同一把锁上。若先取锁再判归属，本租管理员拿一个别租账号的 id 来停用，事务就会排进**那个账号**的登录/刷新锁队列：404 的耗时变成侧信道（慢 = 这个 id 存在且正被人写会话），反复提交还能定向拖慢对方的登录链路。探针只做 `select 1 from public.users where id = :uid and tenant_id = :tid`（不加锁、不改任何东西），不命中即 404 + 拒绝审计；命中后才进锁区间，锁后的 `FOR UPDATE` 仍是权威判定（探针与取锁之间账号被硬删照样 404）。**已拒绝**把 `tenant_id` 并进 advisory key：那会改全站登录/刷新/撤销的锁语义，为一个 404 分支动锁面不成比例（`app/personnel/mfa_admin.py` 同批加探针）。实测：摘掉探针后跨租用例卡在锁等待直到 pytest-timeout（`backend/pyproject.toml` 的 `timeout = 120`）打断。**探针跑在自己的短事务里**（第七轮 m3）：放在主事务内时，未命中的 404 要另开一个事务写拒绝审计，一次注定失败的请求会同时占住两条连接（`app/db/session.py:46` 每引擎 `pool_size=10 / max_overflow=10`），而"猜 id 的洪水请求"走的正是这条路径；挪出来之后该路径任一时刻只占一条连接，代价是命中路径多一次连接借还 + 一条走主键的 `select 1`，权威判定仍是锁后的 `FOR UPDATE`，没有新增竞态。
  - **登录/刷新与停用的竞态**：登录、刷新、撤销与启停共用 `acquire_user_lock`，停用侧同时取目标 `users` 行 `FOR UPDATE`（会话写入方 insert `refresh_tokens` 时对该行取 `FOR KEY SHARE`，两者天然互斥）。停用提交后，等待中的刷新重新读库会看到家族已撤销 + 状态非 active，换不出新 access；停用等待在途登录/刷新提交时，会把对方刚建的家族一并撤销。**两层锁各自独立承重**（2026-10-29 四次摘锁实测，跑完即恢复原实现）：在途用例只摘 advisory → 绿（行锁兜住）；只摘 `FOR UPDATE` → 绿（advisory 兜住）；两层同摘 → 在途与"纯锁 holder"两条用例同时转红，并发用例并报两路都成功（"恰好一路成功"被打破）。决定性的一条是 `test_deactivate_waits_for_a_lock_only_holder`——holder **只取账号锁、不写任何行**，`FOR UPDATE` 与 `FOR KEY SHARE` 都够不着它，摘掉 `acquire_user_lock` 后停用不再排队、直接提交，因此 advisory lock 不是行锁的副产品（原 TD-227 由此收口），`app/auth/sessions.py` 的「同一账号的登录/刷新/撤销/改密共享事务级 advisory lock，避免撤销与轮换竞态」表述按实测保留。`test_concurrent_deactivate_has_exactly_one_winner` 钉住"同一动作并发恰好一路成功、`token_version` 只前进一次"。
  - **审计**：成功 `school.user.deactivated`（`status_before`、`sessions_revoked`、`reason`）/ `school.user.activated`（`status_before`）；拒绝 `school.user.status_denied`（`operation`、`reason` 判据码）。目标超管、自停用、跨租/不存在、目标类型不符、重复状态迁移都留痕；**理由过短属入参非法族，不落安全审计**（与 `RESET_REASON_REQUIRED` 同口径）。
    - **`sessions_revoked` 的口径已统一（TD-231 收口，2026-10-29）**：这个键**全仓只写整数**，含义统一为「**本次操作真正断开的活跃会话家族数**」——活跃 = **未撤销且未过期**，与设备列表 `list_sessions` 的取数口径一致。四个写它的动作（停用 `school.user.deactivated` / 租户 MFA 重置 `school.user.mfa_reset` / 平台 MFA 重置 `platform.user.mfa_reset` / 密码重置 `auth.password.reset`）现在用同一条谓词取家族；**撤销动作本身仍是全量未撤销**（把已过期的死行一并标上撤销不花钱，计数准就行）。收口前 MFA 重置侧按"全部未撤销"计数（读数可能高于实际断开的设备数），改法与实测见 `docs/design-briefs/M2-UI3-custom-roles-accounts.md` §17。两侧的**安全语义始终相同**（"把未撤销的家族全断了"），差别只在计数，不影响谁能继续登录。
    - **布尔语义另立新键 `sessions_all_revoked`**：自助链路 4 个动作——`auth.mfa.activated` / `auth.mfa.disabled` / `auth.password.changed` / `auth.password.reset`——只表达"本次操作把旧会话全部撤销了"，不表达条数，改写新键 `sessions_all_revoked`（布尔 `true`）。`auth.password.reset` 原先另开的并行整数键 `sessions_revoked_count` 已并入同名整数键 `sessions_revoked`，不再存在。收口前布尔与整数混写同一个键，巡检 SQL `sum((metadata_json->>'sessions_revoked')::int)` 在布尔行上直接报错（PG 不能把 `true` cast 成 int），改用 `::text` 又把 `true` 与 `3` 当两类值。**键名对照表**（写报警/巡检脚本前照这张表读）：

      | 键 | 类型 | 含义 | 出现在 |
      |---|---|---|---|
      | `sessions_revoked` | 整数 | 本次真正断开的**活跃**会话家族数（未撤销且未过期） | `school.user.deactivated`、`school.user.mfa_reset`、`platform.user.mfa_reset`、`auth.password.reset` |
      | `sessions_all_revoked` | 布尔 | 本次操作把旧会话**全部**撤销了（不表示条数） | `auth.mfa.activated`、`auth.mfa.disabled`、`auth.password.changed`、`auth.password.reset` |

      读历史行要注意：**改名前**的自助链路写的是 `sessions_revoked: true`，本仓无巡检消费方（`deploy/`、`scripts/`、前端都没有读它的地方），短期无害；将来上巡检需按 `created_at` 切一个时间点，之前的行按旧键读。
  - **未接**：平台侧没有停用租户超管的可审计入口（TD-224，已采纳 M6）；没有独立解锁动作（TD-225，已延后 M6）。

- **前端页面的前置契约（M2-UI3：TD-187g-A，简报 §18）**：TD-187g-B/C 是前端两个页面（角色页、权限账号页），它们先要有后端契约，否则页面只能"自己猜"。本批交付三条契约，**不含任何前端代码**。
  - **权限账号列表**（`GET /school/permission-accounts`，决策 1）：**按账号类型过滤**（`BINDABLE_USER_TYPES = {teacher, school_admin}`——与绑定写路径**同一个集合**，读路径不能比写路径宽也不能窄），`deleted` 不列（它在绑定写路径里被当作不存在），**不按人员数据范围裁剪**——权限账号管理是租户级操作，按 `school`/`self` 裁会让管理员"看不见自己要管的账号"，那不是最小权限。**不回显 `login_id`**（学号/工号属敏感标识符）：响应形状里**没有**这个字段，不只是"前端别显示"。每行带 `role_codes` 与 `roles_version`——绑定页要拿 `roles_version` 做 CAS，拿 `role_codes` 显示"这个人已经有什么角色"——外加服务端派生的 `is_super_admin`：持有校方超管角色的账号，其角色绑定由平台开通流程管理，租户内改绑必然 403 `BINDING_SUPER_ADMIN_FORBIDDEN`，前端据此把绑定入口关掉并给出只读说明（TD-242）；这个位由服务端算，前端不按角色码自己认。cursor 分页（与 `GET /school/teachers` 同形），游标解不开 → **400** 而不是静默回到第一页（否则翻页看起来"没反应"且会重复拉第一页）。
  - **可绑定角色目录**（`GET /school/bindable-roles`，决策 2）：**不含 `permission_codes`**。这条端点要的是"能选哪些角色"，完整权限矩阵是 `school.role.manage` 的事；只持 `school.permission_account.manage` 的人不该顺手拿到全校角色的权限清单——这是它单独存在的理由，不是顺手简化。超管角色**不列**（列出来 = 能勾不能存）。**两份角色目录并存是有意的**（TD-238）：`GET /school/roles` 是角色配置页的完整视图（含权限码、要 `school.role.manage`），`GET /school/bindable-roles` 是绑定页的选择清单（不含权限码、要 `school.permission_account.manage`）；把两者合并会让只持账号管理权的人拿到矩阵，或让角色配置页被迫裁掉自己要显示的列。
  - **`resettable` 由服务端派生（TD-200）**：`GET /school/roles` 的 `RoleResponse` 多一个 `resettable`，判据 = 「该 `role_code` 在 `rbac_builtin_role_defaults` / `rbac_builtin_role_scope_defaults` 里有 realm='tenant' 的行」**且**「角色可编辑」（`lock_reason is None`）——重置要连过 `_assert_editable`（锁定角色 → 409 `ROLE_IMMUTABLE`）与 `ROLE_DEFAULTS_UNAVAILABLE`（没有出厂定义 → 409）**两道门**，少算一道就会给校方超管/学生/教师这三个内置锁定角色点亮一个恒 409 的按钮。它**不是** `is_system` 的别名：前端按 `is_system` 点亮"恢复出厂"，会在"内置但撤了定义"的角色上亮着一个恒 409 的按钮。派生放在 `_ROLE_SELECT_SQL` 的 EXISTS 子查询里（"有出厂定义"那半），与"可编辑"那半在 `_to_summary` 合成，避免两处口径漂移。**它只管这两道硬门**：真正执行还要理由 ≥10 字、逐字 `confirm_role_code`、当前 `version`，且 `school_admin` 的 `school` 范围净增会触发自扩权确认（422 `SELF_GRANT_CONFIRMATION_REQUIRED`）——前端按"确认流"处理，不要按"点了必成"处理。
  - **角色名非空白（TD-198）**：库层新增 `ck_roles_name_not_blank`（`char_length(btrim(name, E' \t\n\r\f\v')) > 0`），迁移 `a1c3e5f7b9d2` 在加约束前先查存量违例、违例即中止（不留半迁移状态）；因为 `roles` 已 FORCE ROW LEVEL SECURITY，预检要求迁移连接能绕过 RLS（超级用户或 BYPASSRLS，见 `docs/deployment.md` §6.6），否则存量计数会被遮蔽成 0、预检形同虚设。接口层用 Python `.strip()`——它去的是**全部** Unicode 空白，比库层的 ASCII 集合**更严**（U+3000 全角空格接口拒、库收），方向是"库层不得比接口层宽松"里的反例，但接口层严一档是对的：界面里不该出现看不见名字的角色。库层那份字符集在服务层留常量 `DB_ROLE_NAME_BLANK_CHARS`，迁移里是同一份字面量的**快照**（历史迁移不 import 代码目录），单元测试逐字符比对，防止两边随重构漂移。**残留窄缺口**：绕过应用代码的手工 SQL 仍能写进一个只有 U+3000 的名字（库层 ASCII 集合认它），已登记 TD-239。

- **预览/执行一致性与出厂不变量**：重置的重种语句按 `rbac_builtin_role_defaults`（和 `..._scope_defaults`）join `permissions` 写库，**当前**不可能静默少种，因为 `fk_rbac_builtin_role_defaults_permission_code_permissions` 与 `fk_role_permissions_permission_id_permissions` 都是 `ON DELETE CASCADE`——删一个 `permissions` 行会连带删掉引用它的出厂定义与所有租户映射，所以"库里缺码但定义表还引用它"这个状态在库层不存在。该结论依赖外键而不是代码，因此执行路径仍做一次**条数复核**（重种返回的行数 vs 定义表应有的行数，不符 → 409 `PERMISSION_CATALOG_OUT_OF_SYNC` 整体回滚）：将来谁把 FK 改成 RESTRICT/NO ACTION 或允许软删，这里会报错而不是"预览承诺 N 项、实种 N-k 项、还顺手把 `defaults_applied` 置真让同步永不回填"。全量替换侧同理，写前探针 + 写后条数复核两道。

- **`school.role.manage` 不可转授**：持有者能给自己所在角色补任意 `school.*`/`campus.*` 码，等于无审计的本校超管；因此只有 `school_super_admin` 主体能把该码写进权限集合（`_assert_delegable` → 422 `ROLE_CODE_NOT_DELEGABLE` + 拒绝审计）。这是接口层规则，不改权限矩阵（`school_admin` 那一格仍是「△=可由超管按需授予」）。
  - **本条只封住「这个码的扩散」**，封不住"放弃该码换别的敏感码"（提交一份不含 `role.manage` 的权限集即可绕过）——那条边界在 M2-UI2 当期是**有意识接受的残余风险**（TD-191），已由 M2-UI3 的自扩权守卫收口，见下条。两道墙的顺序也是契约：带 `role.manage` 的提交先撞转授禁令，到不了自扩权判定。

- **自扩权必须显式确认（M2-UI3：TD-191 收口，简报 §6 方案 A / 决策 #4）**：`SENSITIVE_SELF_GRANT_CODES = {school.role.manage, school.role.delete, school.personnel.sensitive.read, school.personnel.delete, school.personnel.mfa.reset, school.org.manage}`。一次写操作的**结果权限集**（不是提交差集）如果 ① 会经 `user_roles` 流到操作者自己所在的任一角色，且 ② 含操作者当前**没有**的敏感码，则：
  - 必须 `self_grant_ack=true` + 非空理由，否则 422 `SELF_GRANT_CONFIRMATION_REQUIRED` / `SELF_GRANT_REASON_REQUIRED`，并且**在版本 CAS 之前**拒绝（`roles.version` 不前进、映射不变）；
  - `school.role.self_grant` **放行与打断都写**（简报 §6 决策 #4 的「无论是否放行都写」）：放行时同事务（`result="success"`，字段 `role_code` / `newly_reaching_self` / `confirmed_by` / `reason`），被打断时在独立事务（`result="denied"`，与通用 `*.denied` 一起）。只记成功案例的报警等于只看得到得手的人，反复试探看不出来。两条审计的 `metadata` 都带 `newly_reaching_self`，打断侧额外带 `self_grant_ack`（区分"没勾框"与"勾了框没写理由"）；专项审计不替代通用 `*.updated` 流水，两者可交叉核对——完整前后码集只在 `*.updated`（`added`/`removed`，`before = (after ∪ removed) − added` 可还原），专项审计只记触达路径，同一份数据不存两遍。
  - **同一 `action` 两侧的 `reason` 含义不同（审计消费方必读）**：`result="success"` 侧的 `reason` 是操作者填写的自由文本理由；`result="denied"` 侧是判据码（`SELF_GRANT_CONFIRMATION_REQUIRED` / `SELF_GRANT_REASON_REQUIRED`，与本项目所有 `*.denied` 同口径）。按 `result` 分流即可，不要假设 `reason` 恒为自由文本。
  - 持有 `school_super_admin` 的主体**免打断**（不校验 ack 与理由）但**不免留痕**（`confirmed_by="super_admin"`）——否则每次正常配置都要重复勾选，运营会绕道自建角色。这条分支在**一致种子下不可达**（超管按矩阵 §4 持本校 school+campus 全量，敏感清单全在 school 域 → 净增恒为空），它兜的是"超管角色映射落后于目录"的漂移态（存量库/同步未跑/人工修剪）：那种状态下最高权限账号不能被判成死路。业务场景 BS-M2UI3-D 用显式删映射来还原漂移态并钉住这个分支。
  - 「操作者当前没有什么」在**写事务内重读**（`_principal_permission_codes`），不用认证依赖那次请求解析的快照：同一账号在两个标签页并发操作时，快照可能落后一条绑定，会把该挡的漏掉或把不该问的多问一遍；角色绑定判据（`_principal_role_ids`）本来就读库，两侧口径必须一致。这条接线由 `tests/integration/test_rbac_self_grant_wiring.py` 双向钉住（直接调服务层、喂一个故意与库不一致的 `Principal`）：快照**谎称已持有**仍必须打断（否则伪造快照就能绕过确认），快照**漏报**库里已经持有的码不得打断（否则挡正常流）。HTTP 用例跑不出这个分叉——两侧 SQL 同源、同一请求内必然相等，只有并发或直调服务层才能分开。
  - 判据为什么是「净增」而不是「角色里出现敏感码」：管理员本就持有某敏感码时，把它补进自己所在角色没有扩大他的能力面，属日常配置；只有能力变大才需要确认。这让守卫不挡正常流，同时保证每一次"变大"都留下可复盘的审计。
  - **前端预检只是提示，防线始终在服务端**：`selfExpansionCodes`（`frontend/apps/admin/src/rbac-logic.ts`）吃的是进入页面时的角色码集 + `/auth/me` 权限并集，两份快照都可能落后于库（典型：本次会话内别人把操作者绑进了被编辑的角色）。因此本地算不出净增而服务端仍拒时，`RoleSettingsView` 用 422 的 `SELF_GRANT_*` 判据码**强制展开确认条并保留草稿**（不重新 `load()`：那会把操作者已勾的权限和已写的理由一起清掉，是比死路更糟的"修复"），成功保存或切换角色后清零。反过来本地命中而服务端放行，只是多一个勾选框，不构成风险。
  - **这是显式化，不是硬封**：被确认过的自扩权仍会发生（全程可查、可事后撤）。完整硬封（"只能授自己已有的码"）与双人确认合并留给 M6 审批引擎（TD-191 处置）。


**审计**：成功与拒绝都留痕。`school.role.permissions.updated` / `school.role.scopes.updated` / `school.role.self_grant` / `school.role.reset` / `school.user.lookup` / `school.user.mfa_reset` / `school.user.roles.updated` / `school.user.deactivated` / `school.user.activated`；拒绝侧 `school.role.permissions.denied` / `school.role.scopes.denied` / `school.role.reset_denied` / `school.user.mfa_reset_denied` / `school.user.roles.denied` / `school.user.status_denied` 以及登录名未命中的 `school.user.lookup`。启停用**两个动作名**（`deactivated` / `activated`）而不是一个 + `metadata.operation`：报警规则里"停用了多少人"和"启用了多少人"是两个不同问题，靠 `result` 分不开（两侧都是 `success`）。`school.role.self_grant` 是**两侧都有**的那一条：成功时 `result="success"` 记在同一事务，被打断时 `result="denied"` 记在独立事务（见 §5.12 自扩权）；权限侧与范围侧共用这个动作名，靠 `metadata.kind`（`permission` / `scope`）分流，`kind` 是判据的入参而不是两个动作名——加第三种可扩权的宇宙时不要新增 action，报警规则按 `kind` 分维度即可。拒绝审计写在**独立事务**（服务层在写事务内 raise 会连同样事务的审计一起回滚），`result="denied"`、`metadata` 只放判据码，不放登录名等敏感明文；审计写入失败只降级为 warning 日志，不改变拒绝结果。

- **哪些拒绝算安全事件（M2-UI2 审查 m3 的处置；M2-UI3 TD-187b 扩到创建/删除）**：写侧（改权限、执行重置、MFA 重置、创建角色、执行删除、用户角色绑定）里**越权/撞码/目标不可用**三类拒绝都留痕，包括 404 `ROLE_NOT_FOUND`/`USER_NOT_FOUND`——"拿着别人家的 id 来改"本身就是越权探测信号，与 MFA 侧 `USER_NOT_FOUND` 同口径；`metadata.reason` 只记判据码，响应仍是与"不存在"同形的 404，不暴露对方租户。动作键分别为 `school.role.created`/`create_denied`、`school.role.deleted`/`delete_denied`（删除的"权限不足"由鉴权依赖挡在路由层，与既有 MFA 侧同口径，不进 `delete_denied`）。反之三类码**用错了**而非越权，不落安全审计（否则正常配置流会淹掉真实信号）：`ROLE_VERSION_CONFLICT`（版本对不上，权限与范围两条写路径同码）、`ROLE_DELETE_PREVIEW_STALE` 与 `BINDING_VERSION_CONFLICT`（并发改绑/改角色，TD-199 与 TD-187d 同口径：同事在正常配置，不是安全事件）、`ROLE_CODE_MISMATCH`（确认串打错）、`RESET_REASON_REQUIRED`/`ROLE_DELETE_REASON_REQUIRED`/`DEACTIVATE_REASON_REQUIRED`（理由过短）。绑定侧另有独立的拒绝族：`BINDING_SELF_FORBIDDEN`、`BINDING_TARGET_NOT_ALLOWED`、`BINDING_SUPER_ADMIN_FORBIDDEN`、`ROLE_NOT_BINDABLE` 与目标/角色不存在（`USER_NOT_FOUND`/`ROLE_NOT_FOUND`）都写 `school.user.roles.denied`——包括自绑这条（判据只比较 `Principal` 与路径参数，先于对目标账号的任何读取与状态写入；它的拒绝审计在写事务之外另起一个事务，所以不会跟着回滚）；`BINDING_VERSION_CONFLICT` 按上面的口径不写。启停侧同族写 `school.user.status_denied`（`metadata.operation` 记方向）：目标超管 `ACCOUNT_SUPER_ADMIN_FORBIDDEN`、自停用 `ACCOUNT_SELF_DEACTIVATE`、目标类型不符 `ACCOUNT_TARGET_NOT_ALLOWED`、不存在与**软删账号**（两者都收口成 `USER_NOT_FOUND`）都留痕，**重复迁移（`ACCOUNT_ALREADY_DISABLED`/`ACCOUNT_ALREADY_ACTIVE`/`ACCOUNT_NOT_DISABLED`）也留痕**——这条与"版本冲突不留痕"相反是有意的：版本冲突说明同事正在正常配置，而反复对同一目标撞"已停用"是对一个破坏性动作的持续试探，两种信号不能混在同一个计数里；不落痕的是 `DEACTIVATE_REASON_REQUIRED`（理由过短，入参非法）与 `REALM_MISMATCH`（调用方 realm 非法，在 HTTP 上到不了服务层——平台 principal 先被租户鉴权依赖挡下并写 `auth.permission.denied`，服务层那条是纵深防御，不重复留痕）。同族还有**入参非法**一族同样不留痕：`UNKNOWN_PERMISSION_CODE`、`UNKNOWN_SCOPE_TYPE`、`PLATFORM_PERMISSION_FORBIDDEN`、`ROLE_INITIAL_SOURCE_AMBIGUOUS`，以及 Pydantic 在进服务层之前就挡掉的格式/长度非法（BS-M2UI3-B 正向断言了这几条**不在** `create_denied` 里）。它们判的是"这份请求/这份目录不合法"，没有任何对方租户的对象被探测；`PLATFORM_PERMISSION_FORBIDDEN` 最接近越权，但平台码全局可见、被拒的原因是目录判据而非隔离判据，要不要为它单开一条报警登记在 TD-201。同族里 `ROLE_CODE_NOT_DELEGABLE` 与 `SCOPE_TYPE_NOT_EDITABLE` 是**故意反过来**留痕的：两条判据看的都是"操作者有没有资格走这条路"（前者看授码资格，后者看这条写入是否归别的写入方所有），而不是提交的字面量合不合法——试图手工写 `class` 行 = 试图绕开"班级可见性唯一事实来源"，反复试探与手滑要在同一份清单里分得开，所以 `metadata.requested_scope_types` 记下被提交的类型。范围侧的只读 `scopes/preview` 与 `GET /scopes` 同样不审计（不改状态、调用者已有 `school.role.manage`，与 `reset/preview`、`delete/preview` 同口径；`scopes/preview` 被拒不补留痕，反复试探由 `PUT` 那条 `*.denied` 计数）。第四类是**操作不适用**：判据只看目标对象的属性（`is_system`/有无出厂定义），不看操作者是谁，因此它既不是越权也不是用错。这一类里只留**目标是受保护对象**的那一半——试图删除内置角色（`ROLE_NOT_DELETABLE`）留痕，因为"想把 `student` 删掉"本身是破坏性能力的探测信号；反之把自己建的自定义角色恢复出厂（`ROLE_DEFAULTS_UNAVAILABLE`）不留痕：目标就是操作者自己的对象，没有任何被保护的东西，写进安全审计只会稀释信号（配置页对二者都已禁用按钮，能走到服务层的都是脚本调用）。只读的 `reset/preview`、`delete/preview` 不审计：它们不改任何状态，且调用者已具备相应权限码；`user/lookup` 虽也只读却审计，因为它的入参是**登录名**——枚举本校账号是 MFA 重置链路的前置探测面。

---

## 6. Loop + Harness

### 6.1 核心契约（Pydantic）

| Contract | 字段 |
|---|---|
| `AgentRequest` | tenant_id, user_id, conversation_id, turn_id, message, user_prefs, trace_id |
| `TenantContext` | tenant_id, tenant_status, user_id, roles, permissions, data_scope, kb_version_ids, feature_flags |
| `RouteResult` | intents[], primary_capability, confidence, needs_clarification, args |
| `AgentEvent` | type(meta/step/tool_call/tool_result/reasoning_summary/delta/citation/done/error), payload |
| `ToolSpec` | name, version, description, input_schema, output_schema, side_effect, permission, data_scope, timeout_ms, idempotent, audit_level, availability(ctx) |
| `ToolCall` | tool, args, call_id |
| `ToolResult` | ok, data, error_code, duration_ms, redacted_summary |
| `GuardResult` | allow/block/redact/fallback, rule, severity, reason |
| `StopReason` | FINAL_ANSWER/REFUSAL/CAPABILITY_FALLBACK/GUARD_BLOCK/TIMEOUT/USER_CANCELLED/MAX_STEPS/TOOL_BUDGET_EXCEEDED/REPEATED_CALL |
| `TraceSpan` | trace_id, span_type, name, start_ms, duration_ms, attributes, status |

### 6.2 状态与预算

```text
Fast Path（M4-AI01b 落地）：输入轻检 → ROUTE（规则直通单意图，零 LLM 调用）→
RETRIEVE（kb.search 工具 + 证据文本 PG 回查）→ GENERATE（LLM 流式）→
VERIFY（引用序号一致性；超范围告警不改写——delta 已发不可撤回，全量改写归 AI-09）→
输出轻检（占位，AI-09）→ DONE
ReAct Path: INPUT_GUARD → ROUTE → THINK → ACT → OBSERVE → THINK …
            → GENERATE → VERIFY → OUTPUT_GUARD → DONE
```

- `MAX_MODEL_DECISIONS=5`、`MAX_TOOL_CALLS=3`、`MAX_RETRIEVALS=2`、`TURN_TIMEOUT=30s`、`TOKEN_BUDGET=12000`。
- 重复工具调用（同工具+同参数）拒绝；预算超限兜底。
- 多意图：`intents[]`；编排器按预算为多个意图执行只读能力，合并回答；逐项兜底。

### 6.3 工具

| 工具 | 权限 | 说明 |
|---|---|---|
| `kb.search` | `campus.chat.use`（MVP，M4-AI01b） | 混合检索（人群过滤）；只读；工具面无证据文本 |
| `kb.read_chunk` | `campus.chat.use`（MVP） | 读取本轮证据集内分块（白名单守卫，防 ACL 旁路）；只读 |
| `campus.navigate` | `campus.nav` | 返回前端路由/入口；不写 |

> 权限注记（M4-AI01b D2）：`campus.kb.qa` 独立码延后 ReAct 点随权限目录扩展（新码=目录+落库迁移+角色授予）；MVP 三工具挂既有码（端点已强制 `campus.chat.use`，工具内同码纵深断言）。

- `ToolExecutor`：schema 校验 → 权限/租户/数据范围 → 超时 → 执行 → 审计/trace → 类型化结果。
- 二期写工具必须 `side_effect=true` + 确认卡片 + 幂等 + 审批 + 沙箱 + 审计；MVP 不注册。

### 6.4 上下文与记忆

- `ContextBuilder` 加载：租户/用户/角色/数据范围/能力可用性/已发布 KB 版本/会话近 10 轮/滚动摘要/用户开关。
- 能力可用性 = 已发布 KB 版本 + 已授权技能（二期）+ 用户开关 + 业务模块状态；禁止硬编码意图清单。
- 记忆：会话消息 + 滚动摘要；固定 `pinned_kb_version_id`；不做长期记忆。
- 清空当前会话：删除/标记当前会话消息，写审计，不影响其他会话。

### 6.5 护栏

- 输入：长度/编码/注入特征/PII/内容安全；输出按 **A7 七类红线**（2026-11-19 拍板，方案一）逐类判定：
  1. 跨租/他人隐私与数据泄露（含「同一自然人」跨租关联、其他用户数据）
  2. 系统提示词/内部指令/原始 CoT 泄露（不伪造思考内容同条）
  3. 工具参数/内部地址/密钥凭证泄露
  4. XSS 载荷与注入内容
  5. 伪造引用（虚构 chunk_id/文档名/页码——引用校验在 AI-04 点，此处为输出层兜底）
  6. 违法违规内容（犯罪指导/暴恐/色情等）
  7. 自伤/心理危机不当应答——命中→支持性话术+线下求助资源，不诊断不评判（A24）
- 落地：双层（系统提示词层红线约束 + 输出规则校验层）；命中→安全话术 + `guard_events` + 指标。
- 引用校验：chunk_id 存在、属于当前租户/版本、与答案相关；失败修复或降级。

### 6.6 观测 / Langfuse

- 本地 `chat_traces`/`chat_trace_spans`/`guard_events`/`llm_usage` 必写。
- `TraceExporter` 接口：`LocalTraceExporter` 必选；`LangfuseExporter` 可选，异步批量、脱敏、故障隔离。
- Langfuse 默认元数据+哈希；内容采集场景见 `docs/observability-langfuse.md`。
- Ragas 独立跑批；结果本地 `evals/reports/`，可选同步 Langfuse dataset/experiment。
- **应用指标（P0，2026-10-28）**：`GET /metrics`（Prometheus 文本格式，`METRICS_ENABLED` 默认 false，关闭时 404）只暴露 `campus_*` 计数器，使用私有 `CollectorRegistry`（不带 Python/进程默认采集器）。标签仅有界低敏维度：`method`（固定动词集，未知归一 `OTHER`）、`route`（路由模板，未匹配归一 `unmatched`）、`status`（含 `aborted`）、`realm`、`reason`、`action`；**禁止** tenant_id/user_id/角色名/登录名。
  - 中间件 `MetricsMiddleware` 必须注册在 `RequestContextMiddleware` 之后（`add_middleware` 后注册者最外层），否则异常路径拿不到 request_id 上下文；计数发生在 `http.response.start`（SSE 在出首字节时即计数），响应头发出后的断流/异常记 `campus_http_stream_errors_total`，不再重复记 5xx。
  - `/metrics` 自身不计入（含 `root_path` 前缀与尾斜杠变体），避免抓取流量污染 5xx 比例。
  - 端点无业务鉴权（Prometheus 不带业务令牌），上线必须由网络策略只放行抓取端，不经 Nginx 公开；开启时启动日志告警提示。
  - 多 worker 各进程独立计数，汇总需 `PROMETHEUS_MULTIPROC_DIR`，随 TD-214（M6，OBS-02）落地；告警规则文件 `deploy/observability/prometheus/alerts.yml` 本身不自动生效，抓取/通知渠道同随 TD-214。

### 6.7 恢复

- LLM（A8 映射，2026-11-19 落定）：单次调用连接 5s / 读（首 token）15s，回合级 30s 总预算由 Loop 层承担；首 token 前最多 2 次重试（超时/429/5xx/连接类，指数退避），401/403 与其他 4xx 不重试；流中失败不重试、发 error + partial；熔断 5 次/60s 开 30s（进程内 `LlmCircuitBreaker`，多 worker 边界见 TD-326）。
- 嵌入/重排：超时降级；重排 300ms 跳过。
- 工具：超时/权限/租户失败即返回结构化错误；不重试非幂等工具（MVP 全只读）。
- 用户取消：取消下游任务；记录 `USER_CANCELLED`。

---

## 7. RAG

### 7.1 解析

| 类型 | 路径 |
|---|---|
| PDF/图片 | MinerU 4.0.12 V1 API；Standard → Basic → Flash |
| DOCX/PPTX | LibreOffice headless 转 PDF → MinerU Standard；转换失败回退 Flash 原生 Office |
| TXT/MD | 直接 Markdown 规范化 |
| 统一输出 | Markdown + 页/章节/表格/代码元数据 |

- MinerU V1 API：`/v1/health`、`/v1/tiers`、`/v1/uploads`、`/v1/parse/jobs`、`/v1/files`；4.0.12 支持 `/v1/tasks`、`/v1/file_parse`。
- 解析器封装 `DocumentParserPort`；业务层不直接依赖 MinerU 细节。
- 失败：重试/死信；记录 tier/降级原因；ClamAV fail-closed。

### 7.2 切块（参数待 M3 实验）

- 设计支持父子块：`parent_id`、`chunk_type`、`overlap_prev/next`、`section_path`、`page_start/end`。
- 候选取值：子块 300/500/800 字、重叠 10/15/20%；父块 800–1200 字；表格/代码不拆。
- **M3 实验结论（2026-11-18，TD-294 检索直评估复审修正版；32 case × 12 格 + rerank/分词器对照；sparse/原文 + tie-break 复审 M1/m2 修正口径）**：recall@5 全格饱和 1.000（主题区分度高的校务语料上召回对切块参数不敏感，dense 主导）；唯一区分指标 citation precision@3——**300 组均值 0.733 ≈ 500 组 0.723（细块无优势，格内波动 ≤1.5 case 均低于 ≥2 case 判据）> 800 组 0.372（显著劣化 ≈11 case 当量）**；重叠比例（格内 spread ≤1.5 case）与父块尺寸（800/1000/1200 → 0.688–0.698）影响弱；tie-break（生产 `_stable_hits` 同款）后跨轮可复现。**参数维持默认 500/15%/1000**（300/500 持平取块数更少 89 vs 118、嵌入/存储成本更低的现默认；800 排除）；rerank 为 p@3 最大杠杆（16 case 子集双组最终实测：500/0.15 上 0.688→0.771、300/0.10 上 0.750→0.771，rerank 后两格打平——粒度差异被 rerank 抹平；CPU rerank 单 case 100-250s，生产启用沿用 KB-07 opt-in 口径）；faithfulness（生成侧）随 M4 ragas 批次补跑（TD-294a）。state-machines 不变量不变（表格/代码不拆、父块聚合规则原样）。
- 每个子块嵌入前加“文档名+章节路径”上下文。

### 7.3 嵌入 / 稀疏

- Dense：BGE-M3，1024 维，TEI CPU。
- Sparse：jieba 分词 + BM25；按 KB 版本维护 IDF（含 avgdl 长度归一——KB-03A 落地，审查 M4）；token hash 映射 uint32（sha256 前 4 字节）；Qdrant sparse vector。
- 批量嵌入：按批更新进度；失败可重试；嵌入状态写 `kb_chunks.embedding_status`。
- 若 TEI 不支持 BGE-M3/重排，改用 FlagEmbedding/sentence-transformers CPU 服务，接口不变（需确认）。

### 7.4 Qdrant

- 命名：`tenant_{tenant_hash}_kb_v{version_id}`；alias（KB-05 修订，简报 D1-A）**每文档一个**：`tenant_{tenant_hash}_kb_d{document_hash}_active`——原「租户单 alias 名」物理上不成立（Qdrant alias 指向单 collection，而 collection 是每版本一个）；`kb_publish_aliases` 行是事实来源（M4 检索解析读库行），同名 Qdrant named alias 是派生可观测视图（切换失败可经幂等重放发布重建）。
- Payload：`tenant_id_hash`、`kb_id`、`document_id`、`version_id`、`chunk_id`、`parent_id`、`doc_title`、`section_path`、`page_start/end`、`chunk_type`、`text_hash`、`is_published`。
- Named vectors：`dense`（BGE-M3）、`sparse`（BM25）。
- 发布：新 version collection 构建完成 → 测试通过 → alias 原子切换；旧 collection 保留至会话结束/7 天。
- 回滚：alias 切回旧 collection；缓存 Key 含 version。
- 隔离：查询强制 `tenant_id_hash` + `version_id`；每租户独立 collection；跨租测试。
- collection 数量增长：定期清理 retired collection；监控磁盘/内存。

### 7.5 检索 / 重排 / 引用

> **chat 快路径落地口径（M4-AI01b）**：无 rerank（CPU 推理延迟不可进 chat——TD-056/331）；
> 跨活跃版本 fan-out 后按 RRF 分数全局归并 `kb_chat_top_k`（默认 5）；无相邻块拼装与
> 6k 预算（归 ReAct 点/压测批）；引用校验=序号一致性（语义相关性归 AI-04/TD-332）。
> 以下为通用 QA 管线设计（ReAct/后续点对齐）。

1. Query 预处理（可选改写）。
2. Dense top50 + sparse top50。
3. RRF 融合（k=60）；按 parent_id 去重。
4. bge-reranker-v2-m3 重排 top50→top10；300ms 超时跳过。
5. 取 top10 子块+相邻块，≤6k token；按文档/章节组织。
6. 生成严格依据证据；输出 [n] 引用；校验 chunk_id 存在/相关；无效引用降级。
7. 无证据：拒答/兜底，不编造。

### 7.6 热更新

- 会话创建时固定 `pinned_kb_version_id`；新会话用当前 active 版本。
- 发布/回滚原子切 alias；旧版本 collection 保留。
- 删除/下线：新会话不可用；旧会话仍可引用旧版本；7 天后清理。
- 缓存 Key：`tenant_id:kb_id:version_id:*`；发布/回滚后旧 Key 自然失效。

---

## 8. 多租户隔离

### 8.1 强制层

1. JWT 只从 token 取 `tenant_id`；平台/租户分 realm/audience。
2. 中间件设置 `TenantContext`；请求级 contextvar。
3. 仓储层基类强制 `tenant_id` 过滤，并在此**统一注入数据范围谓词**（M1-E2：`app/db/repository.py:TenantScopedRepository.scoped()` — 范围判据随 `Principal` 一次解析出来，业务代码不再各自手写 `where`；需要绕过的模块必须显式传 `ScopeBypass(reason=…)`，裸 SQL 由守卫清单钉住，见 `docs/rbac-matrix.md` §2.1）；禁止裸 SQL 跨租。
4. PostgreSQL RLS：`SET LOCAL app.tenant_id`；策略 `tenant_id = current_setting('app.tenant_id')::uuid`；平台角色例外需显式 grant。
5. Qdrant：每租户独立 collection + payload 校验 + 文档人群 token 过滤（`min_should: 1`，无 token 的版本禁止发布，fail-closed）。
6. MinIO：`tenant/{tenant_id}/...` 前缀；签名 URL 短时有效。
7. 缓存：`tenant:{tenant_id}:...` 前缀。
8. 审计/日志：记录 tenant_id；跨租访问安全事件。

### 8.2 跨租访问

- 默认拒绝；平台跨租访问需二次授权（工单/审批）+ 审计；只读、最小必要。
- **M1-E2 已交付最小通道（2026-10-23）**：`app/personnel/cross_tenant.py` — 只有持有 `platform.cross_tenant.read` 的**平台 realm 主体**可调用；会话第一条语句即 `SET LOCAL TRANSACTION READ ONLY`，写入在数据库层被拒；一次只允许一个 `tenant_id`（批量跨租会把"看了多少学校"稀释成一条审计）；`reason` 必填（缺失/超长 → 422），每次成功读取写一条 `platform.cross_tenant.read` 审计（理由 + 行数）；结果按请求指定的单个租户过滤、敏感字段脱敏；**审计写入失败则请求失败**（不可审计的跨租访问宁可 5xx，不能静默发生）。**二次授权（工单/审批链）本期不做**，通道只做"码 + 只读 + 必填理由 + 强制审计"，工单化挂在 TD-153（已延后：M6 或首个真实跨租需求，以先到者为准；届时在同一 action 上追加工单号，不另开一条通道）。租户侧不存在跨租通道：`data_scopes` 是租户级表，越权一律 404（§8.1-3、矩阵 §2.1）。
- 不得关联“同一自然人”事实。
- 测试矩阵见 `docs/tenant-isolation.md`。

---

## 9. 认证与 RBAC

- JWT access 15m；refresh 7d 轮换；refresh token 存 hash；family 复用检测。
- 账号删除/撤权审批通过、密码修改、权限重大变更时递增 `users.token_version`，使现有 access/refresh 失效；账号删除保留期内允许重新登录，但只签发只读会话。
- 密码 bcrypt cost 12；初始密码随机生成（默认 12 位；平台账号按账号长度加长）；首登改密；历史 5；最小 10、至少 3 类、最大 128 字符且 ≤72 UTF-8 字节；禁止含学号/工号。
- 15 分钟内 5 次失败锁定 15 分钟，对外统一凭证错误文案并审计真实原因；密码重置清锁。MFA TOTP 可选（M1-D2）。
- 角色/权限/数据范围矩阵见 `docs/rbac-matrix.md`。
- **RBAC 落地口径（M1-E1，2026-10-23）**：
  - 权限项目录跟代码走 — `app/rbac/catalog.py`（61 码，M2-UI3 增 `school.role.delete`；`module`/`action` 由码前缀派生），迁移 `e5f7a9c1b3d5` 经 `sync_rbac_definitions()` upsert 到 `permissions`；库里出现目录外的码 = 界面能勾但后端没有校验点的假开关，因此**新增权限项必须发版**。
  - 角色↔权限映射与数据范围是**数据**：内置角色与其出厂默认放在两张代码持有的定义表 `rbac_builtin_roles`/`rbac_builtin_role_defaults`，新租户由 `provision_tenant()` 与平台审核 `review_registration_request()` 共同调用 `public.seed_builtin_roles(tenant_id)`（SECURITY DEFINER）写入；存量租户由同步补齐。运营在库里的增删改（界面 M2）不被同步覆盖。
  - 补写分两种口径：超管（`school_super_admin`/`platform_super_admin`）**只补不撤**（语义是跟着目录全量走）；其他内置角色仅在 `roles.defaults_applied = false`（出厂默认尚未初始化）时写一次默认，之后运营主动清空也不会被补回（TD-150，2026-10-26 起取代原“角色尚无任何映射”判据）。见 `app/rbac/seeds.py:FULL_SCOPE_ROLES`。
  - 每个接口都必须声明权限：`require_permission(码, realm=…)`（realm 必传，权限目录是全局的，码本身不构成跨 realm 边界）；免鉴权路由进显式白名单并写理由，由 `tests/test_route_permission_manifest_unit.py` 钉住（忘挂鉴权、幽灵码、白名单过期或越类都会让 CI 红）。
  - **热更新语义**：`Principal` 每请求从库读角色与权限，映射改动下一次请求即生效，**不作废在线会话**（不递增 `token_version`；会话轮换只用于改密、MFA 变更、踢设备这类账号级安全事件）。代价是每请求数条小 join，基线由 `tests/integration/test_rbac_sql_baseline.py` 钉住（TD-100 目标 M6）。
  - **热更新的边界**：本条只说"权限判据每请求从库读"。踢下线/设备撤销走 Redis 会话标记，Redis 故障时对 `is_active` fail-open（`app/auth/revocation.py:165`），被踢设备最长仍可用 15 分钟（TD-111 → M6）；需要立即断开某账号时按账号级安全事件做会话轮换，不要指望收回权限能断会话。
- **数据范围落地口径（M1-E2，2026-10-23）**：
  - 范围判据随 `Principal` 每请求从库读出（`data_scopes`：角色行 ∪ 用户行），与权限同一热更新口径；**解析不到范围 = 空集（fail-closed）**，绝不回退成"看全校"。本期只交付 `school`/`self`；`class`/`course` 依赖 M2 组织模型（TD-151）。
  - 过滤写在**仓储基类**（§8.1-3），不在各接口重复实现；例外必须显式 `ScopeBypass(reason=…)`，裸 SQL 由守卫清单钉住（`tests/test_bare_sql_scope_guard_unit.py`）。
  - 越权形状：列表静默过滤到范围内，**显式 id 越权 → 404**（与"不存在"同形，不暴露存在性）并写安全审计 `data_scope.denied`。
  - 出厂默认（超管/管理员 = `school`，其余租户角色 = `self`）声明在 `app/rbac/seeds.py:BuiltinRole.scopes`，落库到 `rbac_builtin_role_scope_defaults`；同步**只补"还没有任何范围行"的角色**，与权限映射同一"只补缺、不覆盖人工改动"口径；补完仍出现"整租户没有任何 `data_scopes` 行"时由 `sync_rbac_definitions()` 收尾的 `assert_every_tenant_has_scope()` fail-fast（TD-138）——宁可让迁移/同步失败，也不静默交付一个"接口全 200、列表全空"的租户。
  - 成本：带范围解析后一次带鉴权请求实测 6 条 SQL，上界仍钉 8 条（`tests/integration/test_rbac_sql_baseline.py`，TD-100 → M6 再评估缓存）。
  - 首个真实调用点：`GET /api/v1/school/personnel`(+`/{id}`，`school.personnel.view`)，响应形状按口径 ⑥ 固定为 `id/user_type/display_name/status/last_login_at/email_masked`——**不外发 `login_id`（学号/工号）**、明文邮箱、密码哈希与 MFA 字段；跨租通道复用同一形状。
- **角色配置与管理员重置落地口径（M2-UI2，2026-10-26）**：
  - 可编辑角色只有 `EDITABLE_ROLE_CODES = {school_admin, teacher_b_end}`；`school_super_admin`（权限随代码目录全量走）、`student`/`teacher`（内置数据范围由系统固定）与自定义角色（待 M2-UI3）一律 `editable=false` + `lock_reason`，接口侧 409 兜底，前端禁用只是 UX。
  - **`school.role.manage` 不可转授**：持有者能给自己所在角色补任意 `school.*`/`campus.*` 码，等于无审计的本校超管；因此只有 `school_super_admin` 主体能把该码写进权限集合（`_assert_delegable` → 422 `ROLE_CODE_NOT_DELEGABLE` + 拒绝审计）。这是接口层规则，不改权限矩阵。
  - **自扩权显式确认（TD-191 收口，M2-UI3，2026-10-26）**：§5.12 那条「禁令的边界」不再是有意识接受的残余风险——结果集触达操作者自己所在的角色且**净增**敏感码时，必须 `self_grant_ack=true` + 非空理由（超管免打断、不免留痕），拒绝在版本 CAS 之前（422 `SELF_GRANT_CONFIRMATION_REQUIRED`/`SELF_GRANT_REASON_REQUIRED`，`roles.version` 与映射都不动）。审计**放行与打断都写**：成功同事务、打断走独立事务，因此 `school.role.self_grant` 既是"能力面流向本人"的报警键，也是"谁在反复试探"的报警键。「操作者已持有什么」在写事务内重读，不吃认证依赖的请求快照。硬封（只能授自己已有的码）+ 双人确认仍在 TD-191 名下等 M6 审批引擎。判据与方案对比见 §5.12 与 `docs/design-briefs/M2-UI3-custom-roles-accounts.md` §6。**接线范围（2026-10-27/29 裁定，简报 §12/§14.5）**：角色权限全量替换 + 角色范围全量替换 + 重置出厂（权限 + 范围两个维度）共四个入口，均经 `_guard_self_grant`。**创建自定义角色与用户↔角色绑定不接**：前者新角色 id 必不在操作者所在角色集合里；后者改的是别人的绑定、自己的绑定被自绑禁令（决策 #5）挡住，判据都无可达分支。"建含敏感码的角色再绑给自己"由自绑禁令收口；#5 一旦放宽，绑定路径必须立刻接守卫。
  - 写操作统一「全量替换 + 版本 CAS」：`version` 不匹配 → 409 附 `latest`；重置也先 compare-and-set 占版本再删/种，两个动作共用同一把锁口径。目录漂移（代码有、库无）在 CAS 之前拦截为 409 `PERMISSION_CATALOG_OUT_OF_SYNC`，避免把"同步未跑"伪装成"权限码非法"；重置的重种另有条数复核（见 §5.12「预览/执行一致性与出厂不变量」）。
  - **TD-150 标记只管权限、不管数据范围**：同步判据是 `defaults_applied IS NOT TRUE`，而内置角色的范围在 `seed_builtin_roles()` 时已授过，所以一个「映射非空、范围被清空」的内置角色**不会被同步回填范围**——持有者从此看不到任何数据。本期不扩语义（扩成"两侧各自判缺失"会把运营刻意清掉的范围又补回来），口径是：清范围是有意操作，走 `POST /school/roles/{id}/reset` 恢复出厂（预览会列出 `scopes_added`）。
  - 拒绝审计走**独立事务** best-effort（`_audit_denied`）：服务层在写事务内 raise 会让同事务审计一起回滚，而"谁试过提权/谁试过重置锁定角色"本身就是安全事件；审计 `metadata` 只放判据码（统一 SCREAMING_SNAKE，便于巡检按 reason 分组），不放登录名等明文，写失败只降级为 warning 且不改变拒绝结果。裸 SQL 模块需进守卫清单豁免（`MAX_EXEMPT_MODULES`）。代价：外层事务此刻可能持着 `roles ... FOR UPDATE`，被拒请求因此占两条连接池连接——无锁环（审计写另一张表），但池满时会阻塞而非立刻返回，登记 TD-193（M3 前评审，收口要把审计挪出事务，须与 `personnel/service.py` 一起改）。
  - MFA 管理员重置只清凭证绑定与撤销会话（`revoked_reason='admin_revoke'` + `token_version+1` + Redis 清会话），**不动账号状态与角色**；校方超管与本人一律拒绝，超管兜底路径是平台 CLI。响应形状不含 `login_id`/邮箱/MFA 字段。
- **审批分权落地口径（M1-E2，2026-10-23）**：提交人不能自批 = **三道锁，不依赖数据库触发器**（用户口径：加触发器会把"改例外开关"变成"改数据库代码"，收益不超过 CHECK + 函数两道）。① 应用层 `app/onboarding/service.py:_guard_self_approval()` 在**状态机校验之后（409 已排除）、凭证快照复制之前**判定，正常路径拿到干净的 403/422，且不留下孤儿快照对象；拒绝审计 `approval.self_approval.denied` 在**本事务回滚之后**另开平台会话写入（"谁试过自批"不随回滚丢失）。② 数据库函数 `review_registration_request()` 内再判一次并返回 `SELF_APPROVAL_FORBIDDEN`/`SELF_APPROVAL_REASON_REQUIRED`，兜的是"接口层与函数读到的例外开关不是同一时刻"（运营并发改开关）。③ 同表 CHECK `no_self_approval_without_exception`（`提交人为空 OR 审核人≠提交人 OR self_exception_used IS TRUE`）兜绕过应用代码的手工 SQL，用反证测试钉住（无例外标记 → `IntegrityError`；带标记同一条写入必须成功，否则约束是空挡）。第三条款由例外功能逼出（2026-10-23 用户确认：两条款版会让例外不可用），代价是"直写同人 + 标记为真"数据库分不出真假，检测改走反查审计（有例外标记却没有 `approval.self_exception.used` → TD-161）。例外开关落 `system_settings`（`approval.self_exception_enabled`，默认关闭、每请求读库、改完不重启不踢会话），由 `PUT /api/v1/platform/settings/{key}` 修改（`platform.config.edit` + **必填理由** + `platform.setting.update` 审计）；打开后仍需理由，真正用掉例外写 `approval.self_exception.used` 审计。
- 审批分权：提交人不能自批；超管例外默认关闭；SaaS 审核员不得审核自己提交学校。

---

## 10. 状态机

详见 `docs/state-machines.md`。核心：

- 租户：pending → active / rejected / suspended → deleted。
- 用户：active → locked / retention_readonly → deleted/anonymized。
- KB 文档版本：UPLOADED → PENDING_APPROVAL → APPROVED/REJECTED → BUILDING → READY/FAILED → PUBLISHED → OFFLINE/DELETED；内容修改 → 新版本 PENDING_APPROVAL。
- 构建任务：QUEUED → PARSING → NORMALIZING → CHUNKING → EMBEDDING → INDEXING → VALIDATING → READY/FAILED。
- 请假：DRAFT → PENDING → APPROVED/REJECTED/WITHDRAWN。
- 账号删除：REQUESTED → APPROVED → RETENTION_READONLY → DELETED/ANONYMIZED；APPEALED/FROZEN 分支。
- 审批：PENDING → APPROVED/REJECTED/CANCELLED/EXPIRED。
- 消息 outbox：PENDING → SENDING → SENT / RETRY / DEAD。

---

## 11. Langfuse

版本：**Server 4.63.0 + Python SDK 5.0.2**（Server ≥4.59.0 才支持 Python SDK v5）。详见 `docs/observability-langfuse.md`。要点：

- 生产独立主机自托管；开发 optional。
- 默认 metadata+hash；内容采集场景：KB 测试、C 端反馈、定制化。
- 职责：观测+评估+Prompt 草稿/实验；Git 是生产 prompt 唯一事实来源。
- 平台/运维专属；tenant_hash；访问审计；trace 180 天。
- 异步导出；故障不影响聊天；非审计日志。

---

## 12. 配置中心

- 分层：global / tenant / user。
- 配置项：模型/嵌入/重排、切块/检索、限流/配额、功能开关、审批超时、内容安全、Langfuse 场景开关。
- 密钥：`model_configs.api_key_enc` 信封加密；界面/日志掩码；轮换/撤销。
  **MVP 阶段（M4）**：LLM 密钥走平台级 `.env` 的 `LLM_*`（`SecretStr`，不入库/不入日志/不回显）；
  `model_configs` 表 + 信封加密 + 租户级覆盖**顺延 M6**（2026-11-19 决策卡 3，M4 无模型配置 UI，建表无入口）。
- 变更：审批或双人确认；生产至少两个平台特权账号；单账号 break-glass（原因+审计+事后复核）。
- 版本化：每次变更生成版本；缓存失效；回滚到上一版本。
- DB 连接：只从环境变量/密钥管理读取，不进 UI。

---

## 13. 性能测量口径

- 核心 API：Nginx→FastAPI→DB/Redis；P95≤500ms（不含 AI）。
- SSE 首 token：Nginx 收到请求→客户端收到首个 `delta`（快路径）或首个 `step`（ReAct）；P95≤3s；ReAct 首答案≤5s。
- 快路径预算：向量化≤150ms、检索≤200ms、重排≤300ms、LLM TTFT≤2.3s。
- 并发：非 AI 200；AI 50 流；压测后回调。
- 工具：k6；报告含并发数、P50/P95/P99、错误率、CPU/内存/GPU/显存。
- 发布/回滚：200 并发零 5xx；旧会话固定版本。
- 结果存 `benchmarks/results/`。

---

## 14. ADR（关键决策）

| ADR | 决策 | 理由 | 代价 |
|---|---|---|---|
| ADR-001 | 模块化单体 + Worker | 单人开发、事务简单、部署轻 | 未来拆分需边界纪律 |
| ADR-002 | LangGraph + 自定义 Harness | 框架可替换；产品约束集中 | 自建契约/观测代码。M4-AI01b 注记：快路径为显式 async 状态机（线性管道无需图执行），LangGraph 延后 ReAct 点（循环/条件边）引入；依赖保留 |
| ADR-003 | Qdrant 替代 pgvector | dense+sparse、collection/alias 适合蓝绿 | 多一个服务 |
| ADR-004 | MinerU 4.0.12 + tier 降级 | 最新稳定、V1 API、质量档位 | 6GB 显存需降级验证 |
| ADR-005 | C 端 H5，不做小程序 | POST 流式简单、无审核/域名白名单 | 微信生态触达弱 |
| ADR-006 | 账号删除双入口 | 人员撤权 + 用户自助都合规 | 状态机复杂 |
| ADR-007 | KB 单文档版本审批 | 精细控制、拒绝不阻塞其他文档 | 审批条数增加 |
| ADR-008 | Git 为 prompt 唯一事实来源 | 避免双源、可审计 | Langfuse Prompt UI 仅实验 |
| ADR-009 | Langfuse 独立主机 | 数据不出域、资源隔离 | 需额外主机 |
| ADR-010 | 业务场景测试主门禁 | 保证业务输出而非仅代码正确 | 测试成本上升 |
| ADR-011 | 代码审查强制 | 降低安全/隔离/状态机缺陷 | 开发节奏变慢 |
| ADR-012 | ReAct 5 步/3 工具/2 检索 | 多跳能力与成本平衡 | 首字 5s 例外 |

---

## 15. 风险与待确认

| 风险 | 影响 | 对策 |
|---|---|---|
| 16GB/6GB 资源紧张 | OOM、构建慢 | 资源限额、GPU 单并发、Langfuse 独立主机、M0 基准 |
| MinerU Standard 在 6GB OOM | 解析失败 | Basic/Flash 降级；监控降级率；必要时默认 Basic |
| Arq 对 Python 3.13 兼容性 | 任务队列不可用 | M0 验证；不兼容则提出 Taskiq 等替代并确认 |
| Qdrant collection 增长 | 磁盘/内存 | 定期清理 retired；监控 |
| H5 微信内 SSE 缓冲 | 流式失效 | fetch 流式 + 轮询兜底；M0/E2E 实测 |
| 切块参数未定 | 召回质量 | M3 实验+评估集；不提前拍板 |
| Langfuse 独立主机未提供 | 生产观测缺失 | M6 交付部署方案；未提供则本地 trace+Ragas，最终阶段补 |
| 阶段冲突未定 | 范围/排期 | spec 标“待确认”；不纳入 MVP |
| 测试数据/密钥泄露 | 合规 | .gitignore、secret 扫描、测试环境隔离 |
