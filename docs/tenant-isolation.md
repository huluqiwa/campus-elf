# 校园陪伴精灵 — 多租户隔离规范（tenant-isolation.md）

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-10-19 |
| 状态 | 待用户确认 |
| 原则 | 所有业务表/查询/缓存/对象存储/向量索引强制带 tenant_id；跨租默认拒绝 |

---

## 1. 隔离目标

1. 租户 A 不能读写/推断租户 B 的任何业务数据。
2. 平台跨租访问必须二次授权、只读最小必要、全程审计。
3. 测试/生产数据、配置、密钥严格隔离。
4. 不关联“同一自然人”跨租事实。
5. 任何新表/新查询/新缓存/新对象存储/新向量 collection 默认继承本规范。

---

## 2. 强制层（纵深防御）

```text
JWT (tenant_id, realm) 
  → TenantContext 中间件/contextvar
  → 服务层显式租户校验
  → 仓储层强制 tenant_id 过滤
  → PostgreSQL RLS
  → Qdrant collection/payload
  → MinIO 前缀/签名 URL
  → Redis Key 前缀
  → 审计/日志
```

### 2.1 身份层

- 平台用户与租户用户分 realm/audience；平台 token 不携带校方 tenant_id。
- 租户 token 的 `tenant_id` 只由服务端签发；不接受 body/query/header 覆盖。
- 角色/权限/数据范围每次请求从服务端加载或缓存（缓存需租户+用户维度，权限变更失效）。

### 2.2 应用层

- `TenantContext` 包含 tenant_id、user_id、roles、permissions、data_scope、feature_flags。
- 服务方法必须显式接收 `TenantContext`；禁止无上下文直接构造仓储查询。
- 仓储基类 `TenantScopedRepository` 自动注入 `tenant_id` 条件；复杂查询必须使用它。
- 原生 SQL 必须参数化并显式 `tenant_id = :tenant_id`；代码审查重点检查。
- 跨租平台查询只允许在 `platform` 专用仓储中，调用 `platform.cross_tenant.read` 权限 + 二次授权 + 审计。

### 2.3 PostgreSQL RLS

- 所有租户表启用 RLS：
  ```sql
  ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;
  CREATE POLICY tenant_isolation ON <table>
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
  ```
- 每个事务/连接设置 `SET LOCAL app.tenant_id = '<uuid>'`；连接池归还前重置。
- 平台角色使用单独 DB role；RLS 绕过仅限该角色，且应用层仍审计。
- 迁移脚本必须包含 RLS 策略；新增表未启用 RLS 视为 Blocker。

### 2.4 Qdrant

- 每租户+每版本独立 collection：`tenant_{tenant_hash}_kb_v{version_id}`；alias `tenant_{tenant_hash}_kb_active`。
- payload 必含 `tenant_id_hash`、`version_id`、`document_id`、`chunk_id`；查询强制过滤。
- 不使用跨租共享 collection；若未来合并，必须引入强制 payload 过滤 + 测试。
- collection 创建/删除/alias 切换全部审计；旧 collection 按会话结束/7 天清理。

### 2.5 MinIO

- 对象 Key：`tenant/{tenant_id}/{module}/{yyyy}/{mm}/{uuid}`。
- 签名 URL 短时有效（≤15 分钟）；下载前校验租户/权限。
- 禁止公开 bucket；服务端不返回永久 URL。
- 删除/下线对象按生命周期；审计。

### 2.6 Redis

- Key 前缀：`tenant:{tenant_id}:...`；平台 key 单独前缀。
- 缓存值不跨租复用；权限/配置缓存 Key 含租户/用户/版本。
- 限流按租户/用户维度；平台与租户分开。

### 2.7 日志/审计/trace

- 日志字段含 tenant_id/request_id；敏感字段脱敏。
- 跨租尝试记录安全事件；审计仅追加、哈希链。
- Langfuse 使用 `tenant_hash`，不发送明文 tenant_id/用户 ID；平台/运维专属。

---

## 3. 平台跨租访问

- 默认拒绝；仅在平台运营/审计需要时开放。
- 必须：二次授权（工单/审批）+ 明确数据范围 + 只读 + 最小必要 + 审计 + 时限。
- 禁止批量导出全校敏感数据；导出需审批+水印/审计。
- 不得关联跨租“同一自然人”。

---

## 4. 新功能检查清单

新增表：
- [ ] 含 `tenant_id UUID NOT NULL`（平台表除外）。
- [ ] 索引包含 tenant_id 前缀。
- [ ] 启用 RLS + policy。
- [ ] 外键/唯一约束含 tenant_id。
- [ ] 软删/审计字段。
- [ ] 迁移回滚脚本。

新增查询：
- [ ] 使用 TenantScopedRepository。
- [ ] 原生 SQL 显式 tenant_id + 参数化。
- [ ] 分页/聚合不泄露跨租计数。
- [ ] 缓存 Key 含 tenant_id。
- [ ] 测试跨租读写删拒绝。

新增对象/向量：
- [ ] MinIO 前缀含 tenant_id。
- [ ] Qdrant collection 按租户/版本。
- [ ] 签名 URL/查询强制租户过滤。
- [ ] 清理策略。

新增消息/通知：
- [ ] PLATFORM/TENANT 通道标识。
- [ ] 禁止租户间直发。
- [ ] 跨通道记录来源租户。

---

## 5. 自动化测试矩阵（A23）

| 测试 | 断言 |
|---|---|
| 跨租 SELECT | 404/403，无数据 |
| 跨租 INSERT | 拒绝，无副作用 |
| 跨租 UPDATE/DELETE | 拒绝，无副作用 |
| 跨租聚合/计数 | 不泄露他租数据 |
| 缓存跨租 | 不可命中 |
| MinIO 跨租 | 签名 URL/前缀拒绝 |
| Qdrant 跨租 | collection/payload 过滤拒绝 |
| 平台跨租 | 默认拒绝；授权+审计后只读 |
| 消息通道 | 平台/租户隔离，禁止租户间 |
| KB 检索 | 跨租文档/分块不可召回 |
| 测试/生产 | 密钥/数据库不可互访 |
| 生产放行开关 | `DEV_SKIP_*` 无法开启 |

实现：从 SQLAlchemy 模型自动生成 tenant-scoped 表清单；每个表至少 read/write/delete 三组；Qdrant/MinIO/缓存单独用例；CI 门禁。

---

## 6. 常见漏洞与防线

| 漏洞 | 防线 |
|---|---|
| 忘记 where tenant_id | 仓储基类 + RLS + 代码审查 |
| 前端传 tenant_id | 只从 token 取；忽略 body/query |
| 跨租缓存命中 | Key 含 tenant_id；缓存隔离 |
| 对象存储 Key 可猜 | UUID + 租户前缀 + 签名 URL |
| 向量 collection 共用 | 每租户 collection + payload 过滤 |
| 平台越权 | 二次授权 + 只读 + 审计 |
| 导出泄露 | 权限+审批+脱敏+审计 |
| 日志泄露 | 脱敏 + 访问控制 |

---

## 7. 事件响应

1. 发现跨租泄露：立即停用相关接口/凭证；保留证据；通知安全负责人。
2. 评估范围（租户/数据/时间）；按合规要求通知受影响方。
3. 修复后补测试；审查同类查询/缓存/对象/向量。
4. 审计记录不可删除；事件报告归档。
5. 误提交密钥：视为已泄露，立即轮换；不是仅从 Git 删除。
