# 校园陪伴精灵 — 状态机（state-machines.md）

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-10-19 |
| 状态 | 待用户确认 |
| 规则 | 所有状态流转必须服务端校验、事务内更新、写审计；非法流转返回 409/403 |

---

## 1. 租户生命周期

```text
PENDING → ACTIVE → SUSPENDED → ACTIVE
   │         │
   └→ REJECTED
             └→ DELETED（二期，不在 MVP）
```

| 当前 | 事件 | 目标 | 守卫/副作用 |
|---|---|---|---|
| PENDING | 审核通过 | ACTIVE | 生成学校编号/超管账号/邮件；审计 |
| PENDING | 审核拒绝 | REJECTED | 理由必填；学校可查看；审计 |
| ACTIVE | 平台暂停 | SUSPENDED | 禁止登录/写入；保留数据；通知 |
| SUSPENDED | 恢复 | ACTIVE | 恢复登录；审计 |
| ACTIVE | 注销（二期） | DELETED | 合规清理；不在 MVP |

---

## 2. 用户状态

```text
ACTIVE → LOCKED → ACTIVE
ACTIVE → RETENTION_READONLY → DELETED / ANONYMIZED
ACTIVE → DISABLED
```

| 状态 | 能力 |
|---|---|
| ACTIVE | 正常登录/读写（按权限） |
| LOCKED | 登录失败锁定；到期自动解锁/管理员解锁 |
| RETENTION_READONLY | 可登录；只能申诉/只读；所有写接口 403 |
| DISABLED | 不可登录；token 撤销 |
| DELETED/ANONYMIZED | 不可登录；数据按留存矩阵处理 |

---

## 3. 知识库文档版本

```text
DRAFT → UPLOADED → SCAN_FAILED
                └→ PENDING_APPROVAL
PENDING_APPROVAL → REJECTED
                 └→ APPROVED
APPROVED → BUILDING → READY / FAILED
READY → TESTING → TEST_PASSED / TEST_FAILED
TEST_PASSED → PUBLISHED
PUBLISHED → OFFLINE
PUBLISHED → DELETED
PUBLISHED → NEW_VERSION → PENDING_APPROVAL
```

| 当前 | 事件 | 目标 | 守卫/副作用 |
|---|---|---|---|
| UPLOADED | ClamAV 通过 | PENDING_APPROVAL | 可提交审批 |
| UPLOADED | ClamAV 失败 | SCAN_FAILED | 隔离；不得提交审批 |
| PENDING_APPROVAL | 审批通过 | APPROVED | 审批记录；可构建 |
| PENDING_APPROVAL | 审批拒绝 | REJECTED | 理由必填；不可构建 |
| APPROVED | 创建构建 | BUILDING | 仅一个活跃任务；未审批 409 |
| BUILDING | 构建成功 | READY | 分块/向量就绪；可测试 |
| BUILDING | 构建失败 | FAILED | 错误码；可重试 |
| READY | 运行测试 | TESTING | 草稿配置+标记会话 |
| TESTING | 通过 | TEST_PASSED | 可发布 |
| TESTING | 失败 | TEST_FAILED | 不可发布；失败样例入回归 |
| TEST_PASSED | 发布 | PUBLISHED | alias 切换；新会话用新版本 |
| PUBLISHED | 下线 | OFFLINE | 新会话不可用；旧会话保留 |
| PUBLISHED | 删除 | DELETED | 软删；旧会话保留至结束/7 天 |
| PUBLISHED | 原文/结构修改（重传、重切块、增删块） | NEW_VERSION | 新版本 PENDING_APPROVAL；旧版本在线 |
| PUBLISHED | 块级文本校正（KB-10） | DERIVED | 派生版本（`derivation_type=chunk_edit`）→ 超管确认 → 派生构建；旧版本在线 |
| APPROVED/READY/TEST_PASSED | 块级文本校正 | DERIVED | 同上；派生版本沿用源版本已确认 ACL |
| READY/APPROVED | ACL 未确认（`acl_confirmed=false`） | 拒绝 | 构建/发布返回 409 `ACL_UNCONFIRMED`（K4，fail-closed） |
| 任意 | ACL 变更 | 同状态 | 即时生效（不受会话固定版本保护）；审计前后值 |
| PUBLISHED | 仅标签/描述修改 | PUBLISHED | 免审；审计前后值 |

**不变量**
- 未 APPROVED 不得创建构建任务；派生版本（K5）以"超管确认"替代平台审批，但同样不得原地修改源版本。
- TEST_PASSED 才能发布；派生版本与初始版本走同一发布/回滚路径。
- 同一文档版本最多一个活跃构建任务。
- 发布/回滚只切 alias，不修改分块内容。
- 原文/结构修改必须新版本；块级文本校正必须走派生版本（不可原地改已发布版本的块）。
- 块编辑不得增删块、改变父子结构与父块归属；`base_hash` 冲突即 409。
- 版本必须有已确认的 ACL 才能构建/发布（K4 fail-closed）。

### 3.1 块编辑与派生版本（K5）

```text
源版本（APPROVED / READY / TEST_PASSED / PUBLISHED）
  → 编辑子块（kb_chunk_edits: draft/submitted，base_hash 乐观锁）
  → 生成派生版本（derivation_type=chunk_edit, parent_version_id=源版本）
  → 超管确认（school.kb.chunk.confirm + 审计）
  → 派生构建（COPY → EMBED(仅改动块) → INDEX → VALIDATE → READY）
  → TESTING → TEST_PASSED → 发布（alias 切流）
```

| 当前 | 事件 | 目标 | 守卫/副作用 |
|---|---|---|---|
| 源版本在线 | 提交块编辑 | 编辑草稿 | 仅子块文本；理由必填；`base_hash` 匹配 |
| 编辑草稿 | 确认并派生 | 派生版本 | 校方超管权限；审计 `kb.version.derived`；复制全部块 |
| 派生版本 | 创建构建 | BUILDING | 跳过解析/切块；仅改动块重嵌（未改块复用向量缓存） |
| 派生版本 | 构建失败 | FAILED | 可重试；源版本与线上 alias 不受影响 |
| 派生版本发布 | alias 切流 | PUBLISHED | 新会话用派生版本；旧会话仍固定源版本 |

---

## 4. 构建任务

```text
QUEUED → PARSING → NORMALIZING → CHUNKING → EMBEDDING → INDEXING → VALIDATING → READY
   │         │           │           │           │           │           │
   └─────────┴───────────┴───────────┴───────────┴───────────┴───────────┴→ FAILED
```

| 阶段 | 进度单位 | 失败处理 |
|---|---|---|
| PARSING | 页/文档 | MinerU 重试；tier 降级；死信 |
| NORMALIZING | 段落/文档 | 记录错误；重试 |
| CHUNKING | 块/文档 | 重试；切块参数错误则失败 |
| EMBEDDING | 批/块 | 重试；超时降级 |
| INDEXING | 批/向量 | 重试；Qdrant 故障 |
| VALIDATING | 检查项 | 失败不可 READY |

**并发/恢复**
- Worker 心跳；超时任务标记 FAILED/STALE。
- 重试创建新 attempt，不覆盖历史。
- 进度事件持久化 `kb_build_events.seq`，支持续传。

---

## 5. 知识库发布/别名

```text
BUILD_READY → TEST_PASSED → PUBLISHED(alias → new collection)
PUBLISHED → ROLLBACK(alias → old collection)
PUBLISHED → OFFLINE
```

- alias：`tenant_{tenant_hash}_kb_active`。
- 旧 collection 保留至旧会话结束或 7 天。
- 回滚只切 alias；缓存 Key 含 version。
- 发布/回滚期间零 5xx；旧会话固定 `pinned_kb_version_id`。

---

## 6. 审批

```text
PENDING → APPROVED
        → REJECTED
        → CANCELLED
        → EXPIRED
```

| 事件 | 守卫 | 副作用 |
|---|---|---|
| 提交 | 类型/数据范围/附件 | 创建 steps；通知审批人 |
| 通过 | 非提交人（或例外开启+理由） | 执行业务动作；通知；审计 |
| 驳回 | 理由必填 | 通知提交人；审计 |
| 取消 | 提交人/管理员 | 通知；审计 |
| 超时 | 24h 提醒/48h 升级 | 通知；不自动通过 |

- 类型注册：KB_DOCUMENT、LEAVE、ACCOUNT_TERMINATION、MCP_SKILLS（二期）、ANNOUNCEMENT（二期）、SKIN（二期）、KB_CUSTOMIZATION（二期）、CREATION（三期）。
- 提交人不能自批；超管例外默认关闭；SaaS 审核员不得审核自己提交学校。

---

## 7. 请假

```text
DRAFT → PENDING → APPROVED
              → REJECTED
              → WITHDRAWN
```

| 当前 | 事件 | 目标 | 守卫/副作用 |
|---|---|---|---|
| DRAFT | 提交 | PENDING | 校验字段；通知审批人 |
| PENDING | 撤回 | WITHDRAWN | 仅提交人；通知 |
| PENDING | 通过 | APPROVED | 审批权限；通知；审计 |
| PENDING | 拒绝 | REJECTED | 理由必填；通知 |
| APPROVED | 编辑 | — | 拒绝；只读预览 |
| APPROVED | 删除 | — | 拒绝（保留记录） |

- 审批人 = 学生班主任/辅导员（由组织关系确定）；需 B 端审批权限。
- 通过后只读；审批记录含审批人/时间/理由。

---

## 8. 账号删除/撤销登录权限

```text
REQUESTED → PENDING_APPROVAL → APPROVED → RETENTION_READONLY
                                         │  ├→ APPEALED → APPROVED/REJECTED
                                         │  ├→ FROZEN → RETENTION_READONLY
                                         │  └→ DELETED / ANONYMIZED
                                         └→ REJECTED（恢复 ACTIVE）
```

| 当前 | 事件 | 目标 | 守卫/副作用 |
|---|---|---|---|
| REQUESTED | 提交审批 | PENDING_APPROVAL | 自助/管理员；通知 |
| PENDING_APPROVAL | 通过 | RETENTION_READONLY | 用户状态切换为只读；递增 `token_version` 使现有 access/refresh 失效；允许重新登录但只签发只读会话；所有写接口 403；通知+审计 |
| PENDING_APPROVAL | 拒绝 | REJECTED | 恢复原状态；通知 |
| RETENTION_READONLY | 写操作 | — | 403；审计 |
| RETENTION_READONLY | 申诉 | APPEALED | 通知管理员；倒计时暂停 |
| RETENTION_READONLY | 冻结 | FROZEN | 暂停倒计时；人工复核 |
| FROZEN | 复核恢复 | RETENTION_READONLY | 恢复倒计时/延长 |
| RETENTION_READONLY | 5 天到期 | DELETED/ANONYMIZED | 删除证明；审计 |
| APPEALED | 维持 | RETENTION_READONLY | 继续倒计时 |
| APPEALED | 撤销 | REJECTED | 恢复 ACTIVE |

**关键语义**
- 审批通过后仍可登录，但只能申诉/只读；所有写操作拒绝。
- 冻结 = 暂停删除倒计时 + 人工复核；不是禁止登录。
- 唯一超管申请：本校不能自批，转 SaaS 复核。
- 保留期内不可提前删除；拒绝恢复原状态。
- 业务数据按 A5 留存矩阵保留/匿名化；删除证明含范围哈希、时间戳、备份到期。

---

## 9. 消息/Outbox

```text
Outbox: PENDING → SENDING → SENT
                     ├→ RETRY → SENDING
                     └→ DEAD → 手动重发
```

| 状态 | 说明 |
|---|---|
| PENDING | 待发送 |
| SENDING | 正在发送 |
| SENT | 成功 |
| RETRY | 失败，等待退避重试（最多 3 次） |
| DEAD | 死信，后台可重发 |

- 站内信与邮件独立状态；站内信成功不因邮件失败回滚。
- 平台/租户通道隔离；跨通道记录来源租户。
- B 端已读 MVP；C 端已读二期。

---

## 10. 导入任务

```text
QUEUED → PARSING → VALIDATING → IMPORTING → COMPLETED
                                      └→ PARTIAL / FAILED
```

- 行级错误报告；重复策略（skip/update/reject）配置化。
- 失败可重试；不产生半成品账号（事务/分批提交+回滚策略）。

---

## 11. 幂等与并发不变量

- 所有写操作带 `Idempotency-Key`；唯一约束兜底。
- 审批/请假/删除/发布使用乐观锁或行锁，防止并发非法流转。
- 同一文档版本仅一个活跃构建；同一审批单仅一个最终动作。
- 发布/回滚 alias 切换原子；缓存 Key 含 version。
- 重复工具调用/重复消息不产生重复业务效果。

---

## 12. 审计事件

每个状态流转必须写：
- `actor_id`、`tenant_id`、`action`、`resource_type/id`、`from_status/to_status`、`reason`、`request_id`、`created_at`。
- 敏感值脱敏；审计仅追加、哈希链。
- 非法流转尝试也记录安全事件。
