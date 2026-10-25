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
| `platform_roles` / `platform_user_roles` | id, code/name; user_id, role_id | 平台角色 |
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
| `roles` | id, tenant_id, code, name, is_system | 校方角色 |
| `permissions` | id, code, module, action | 权限点 |
| `role_permissions` | role_id, permission_id, tenant_id（复合外键保证与父行同租） | 角色-权限 |
| `user_roles` | user_id, role_id, tenant_id（复合外键保证与父行同租） | 多角色 |
| `data_scopes` | id, tenant_id, role_id/user_id, scope_type(school/class/course/self), scope_id | 数据范围 |
| `teachers` | id, tenant_id, user_id, employee_no, name, age, gender, position, **department_id**, id_card_enc, id_card_hmac, id_card_mask | 身份证加密/掩码（同租指纹部分唯一索引 `uq_teachers_idcard_hmac` 兜底查重）；部门归属供文档人群判定（A30） |
| `students` | id, tenant_id, user_id, student_no, name, class_id, gender, id_card_enc, id_card_hmac, id_card_mask | 档案（同租指纹部分唯一索引 `uq_students_idcard_hmac`）；班主任/辅导员权威在 `classes`（M2-ORG01-03 D2，读路径 join 聚合，不冗余落列） |
| `classes` | id, tenant_id, grade, name, head_teacher_id, counselor_id | 班级（grade 供"年级"人群使用） |
| `departments` | id, tenant_id, parent_id, name, code, path, status | 部门/科室树（A30）；复合外键 `(parent_id, tenant_id)`；唯一 `(tenant_id, parent_id, name)` |
| `courses` | id, tenant_id, code, name | 课程 |
| `teacher_class_courses` | teacher_id, class_id, course_id, term | 任教关系 |
| `import_jobs` | id, tenant_id, type, conflict(error\|skip), file_object_key, status, total_rows, success_rows, failed_rows, skipped_rows, error_report_key, row_filter_json(重试行号), retried_by, retry_count, created_by, started_at, finished_at, message | 导入（M2-ORG02 实际形态；错误行重试=派生 job 只导失败行） |
| `import_row_errors` | id, tenant_id, job_id, row_no, field, error_code, message, raw_json(原始行，重试免二次解析) | 错误行 |

### 4.3 知识库

| 表 | 关键字段 | 说明 |
|---|---|---|
| `kb_documents` | id, tenant_id, title, tags, description, status, current_version_id, created_by | 文档 |
| `kb_document_versions` | id, tenant_id, document_id, version_no, source_object_key, source_hash, mime, size, status, approval_request_id, **parent_version_id, derivation_type(initial/chunk_edit), acl_confirmed_by, acl_confirmed_at**, created_by | 版本；status 同下；派生版本（K5）由 parent_version_id 追溯 |
| `kb_build_jobs` | id, tenant_id, document_version_id, status, stage, progress, processed_units, total_units, tier, fallback_reason, retry_count, error_code, message, started_at, updated_at, finished_at | 构建任务；同版本仅一个活跃任务 |
| `kb_build_events` | id, job_id, seq, event_type, payload_json, created_at | SSE 事件持久化，支持 Last-Event-ID/since |
| `kb_chunks` | id, tenant_id, document_version_id, parent_id, chunk_type(parent/child), chunk_index, text, text_markdown, section_path, page_start, page_end, token_count, char_count, overlap_prev, overlap_next, chunk_hash, metadata_json, embedding_status, **is_human_edited, edited_by, edited_at** | 分块 |
| `kb_document_audiences` | id, tenant_id, version_id, audience_type(department/role/class/grade/course/user/all), audience_id, created_by, created_at | 版本级查阅人群（K4）；`all` 时 audience_id 为空；唯一 `(version_id, audience_type, audience_id)` |
| `kb_chunk_edits` | id, tenant_id, source_version_id, chunk_id, before_hash, after_hash, new_content, edited_by, reason, status(draft/submitted/confirmed/discarded), derived_version_id, created_at | 块编辑记录（K5） |
| `kb_embedding_cache` | tenant_id, model, chunk_hash, dense_object_key, sparse_object_key, created_at | 向量复用缓存（K5）；派生重建只对改动块重新推理 |
| `kb_index_versions` | id, tenant_id, kb_id/document_version_id, qdrant_collection, status, published_at, retired_at | 版本化索引 |
| `kb_publish_aliases` | tenant_id, kb_id, alias, collection, version_id, updated_at | alias 指针 |
| `kb_test_sets` | id, tenant_id, document_version_id, name, file_object_key, status, created_by | 测试集 |
| `kb_test_cases` | id, test_set_id, question, expected_answer, expected_points, expected_chunk_ids, category, is_adversarial, row_no | 测试用例 |
| `kb_test_runs` | id, tenant_id, test_set_id, document_version_id, status, metrics_json, report_object_key, started_at, finished_at | 测试报告 |
| `kb_retrieval_tests` | id, tenant_id, document_version_id, query, top_k, result_json, created_by | 检索测试 |

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
| `approval_requests` | id, tenant_id, type, submitter_id, status, current_step, title, payload_json, result, reject_reason, created_at, completed_at | 审批单 |
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

- Base path：业务 API 使用 `/api/v1`；健康检查例外，固定在根路径 `/health/live`、`/health/ready`，供 Compose/Nginx/探针使用。
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
- MFA 失败与锁定：错误 TOTP/恢复码、绑定验证码错误、关闭校验失败均复用 AUTH-05 计数（5 次/15 分钟锁 15 分钟），对外统一凭证错误文案；审计 `auth.mfa.enrollment_failed` / `activation_failed` / `disable_failed` 只记 reason/failed_attempts/lock_applied 与 IP/UA 哈希，`auth.mfa.activated` 记恢复码数量与 `sessions_revoked`，`auth.mfa.recovery_used` 记 `phase=login|disable`；任何审计/日志/响应不得出现 secret、otpauth URI、验证码、恢复码明文（恢复码明文只在该激活响应出现一次）。
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
| GET | `/school/personnel/{id}/id-card` | 身份证完整查看（sensitive.read + 请求体理由 ≥10 字符 → 明文 + 审计 + no-store；D3-A） |
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
Fast Path:  INPUT_GUARD → ROUTE → RETRIEVE → GENERATE → VERIFY → OUTPUT_GUARD → DONE
ReAct Path: INPUT_GUARD → ROUTE → THINK → ACT → OBSERVE → THINK …
            → GENERATE → VERIFY → OUTPUT_GUARD → DONE
```

- `MAX_MODEL_DECISIONS=5`、`MAX_TOOL_CALLS=3`、`MAX_RETRIEVALS=2`、`TURN_TIMEOUT=30s`、`TOKEN_BUDGET=12000`。
- 重复工具调用（同工具+同参数）拒绝；预算超限兜底。
- 多意图：`intents[]`；编排器按预算为多个意图执行只读能力，合并回答；逐项兜底。

### 6.3 工具

| 工具 | 权限 | 说明 |
|---|---|---|
| `kb.search` | `campus.kb.qa` | 混合检索；只读 |
| `kb.read_chunk` | `campus.kb.qa` | 读取证据；只读 |
| `campus.navigate` | `campus.nav` | 返回前端路由/入口；不写 |

- `ToolExecutor`：schema 校验 → 权限/租户/数据范围 → 超时 → 执行 → 审计/trace → 类型化结果。
- 二期写工具必须 `side_effect=true` + 确认卡片 + 幂等 + 审批 + 沙箱 + 审计；MVP 不注册。

### 6.4 上下文与记忆

- `ContextBuilder` 加载：租户/用户/角色/数据范围/能力可用性/已发布 KB 版本/会话近 10 轮/滚动摘要/用户开关。
- 能力可用性 = 已发布 KB 版本 + 已授权技能（二期）+ 用户开关 + 业务模块状态；禁止硬编码意图清单。
- 记忆：会话消息 + 滚动摘要；固定 `pinned_kb_version_id`；不做长期记忆。
- 清空当前会话：删除/标记当前会话消息，写审计，不影响其他会话。

### 6.5 护栏

- 输入：长度/编码/注入特征/PII/内容安全；输出：跨租 PII、系统提示词、工具/密钥/内网地址、XSS、伪造引用、违法/自伤不当内容。
- 命中：安全话术 + `guard_events` + 指标；自伤支持性话术+线下求助，不诊断。
- 引用校验：chunk_id 存在、属于当前租户/版本、与答案相关；失败修复或降级。

### 6.6 观测 / Langfuse

- 本地 `chat_traces`/`chat_trace_spans`/`guard_events`/`llm_usage` 必写。
- `TraceExporter` 接口：`LocalTraceExporter` 必选；`LangfuseExporter` 可选，异步批量、脱敏、故障隔离。
- Langfuse 默认元数据+哈希；内容采集场景见 `docs/observability-langfuse.md`。
- Ragas 独立跑批；结果本地 `evals/reports/`，可选同步 Langfuse dataset/experiment。

### 6.7 恢复

- LLM：首 token 前最多 2 次重试；流中失败发 error + partial；熔断 5 次/60s 开 30s。
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
- M3 必须用评估集比较 recall/faithfulness/citation precision 后定参数，回写设计。
- 每个子块嵌入前加“文档名+章节路径”上下文。

### 7.3 嵌入 / 稀疏

- Dense：BGE-M3，1024 维，TEI CPU。
- Sparse：jieba 分词 + BM25；按 KB 版本维护 IDF；token hash 映射 uint32；Qdrant sparse vector。
- 批量嵌入：按批更新进度；失败可重试；嵌入状态写 `kb_chunks.embedding_status`。
- 若 TEI 不支持 BGE-M3/重排，改用 FlagEmbedding/sentence-transformers CPU 服务，接口不变（需确认）。

### 7.4 Qdrant

- 命名：`tenant_{tenant_hash}_kb_v{version_id}`；alias `tenant_{tenant_hash}_kb_active`。
- Payload：`tenant_id_hash`、`kb_id`、`document_id`、`version_id`、`chunk_id`、`parent_id`、`doc_title`、`section_path`、`page_start/end`、`chunk_type`、`text_hash`、`is_published`。
- Named vectors：`dense`（BGE-M3）、`sparse`（BM25）。
- 发布：新 version collection 构建完成 → 测试通过 → alias 原子切换；旧 collection 保留至会话结束/7 天。
- 回滚：alias 切回旧 collection；缓存 Key 含 version。
- 隔离：查询强制 `tenant_id_hash` + `version_id`；每租户独立 collection；跨租测试。
- collection 数量增长：定期清理 retired collection；监控磁盘/内存。

### 7.5 检索 / 重排 / 引用

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
  - 权限项目录跟代码走 — `app/rbac/catalog.py`（59 码，`module`/`action` 由码前缀派生），迁移 `e5f7a9c1b3d5` 经 `sync_rbac_definitions()` upsert 到 `permissions`；库里出现目录外的码 = 界面能勾但后端没有校验点的假开关，因此**新增权限项必须发版**。
  - 角色↔权限映射与数据范围是**数据**：内置角色与其出厂默认放在两张代码持有的定义表 `rbac_builtin_roles`/`rbac_builtin_role_defaults`，新租户由 `provision_tenant()` 与平台审核 `review_registration_request()` 共同调用 `public.seed_builtin_roles(tenant_id)`（SECURITY DEFINER）写入；存量租户由同步补齐。运营在库里的增删改（界面 M2）不被同步覆盖。
  - 补写分两种口径：超管（`school_super_admin`/`platform_super_admin`）**只补不撤**（语义是跟着目录全量走）；其他内置角色仅在**该角色尚无任何映射**时写默认（否则会把运营撤销的码塞回去）。见 `app/rbac/seeds.py:FULL_SCOPE_ROLES`。
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
| ADR-002 | LangGraph + 自定义 Harness | 框架可替换；产品约束集中 | 自建契约/观测代码 |
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
