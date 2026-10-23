# M1-F — 审计哈希链（AUD-01）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-23 |
| 状态 | **已确认**（2026-10-23：D1 数据库触发器 / D2 全局单链 / D3 HMAC-SHA256 / D4 链+校验+查询接口） |
| 关联功能点 | M1-F / AUD-01：审计仅追加 + 哈希链 |
| 验收引用 | `docs/spec.md` §3.14 AUD-01；`docs/milestone.md` §4.4 退出标准 |
| 关闭 TD | TD-068（链校验脚本/只读接口）、TD-091（平台可伪造审计 → 链校验暴露 + 启动自检收口） |

## 1. 目标与不解决

**目标**：
- 每条 `audit_logs` 写入时自动计算哈希（`prev_hash` + `hash`），形成全局不可篡改链
- 提供链校验能力：全链连续性检查，篡改可检测
- 提供平台侧审计只读查询接口（`platform.audit.view` 权限）
- 启动自检覆盖审计写入路径

**不解决（延后 M6）**：WORM 每日导出、外部锚点（`anchor_ref` 仅供预留）、审计归档/365 天在线保留策略。

## 2. 现状

- `audit_logs` 表已有：`sequence`（PK 递增）、`prev_hash`（可空）、`hash`（可空）、`hash_version`（0=未入链）、`anchor_ref`（预留）
- 仅追加触发器已生效（禁 UPDATE/DELETE/TRUNCATE，对超级用户同样生效）
- 两个应用角色只有 `SELECT, INSERT`
- `app/audit/writer.py` 目前写入 `hash=None, hash_version=0`

## 3. 四项已确认决策

| 编号 | 决策 | 结论 | 理由 |
|---|---|---|---|
| D1 | 链计算位置 | **数据库触发器** | 应用无法绕过；直写 SQL / 外部工具插库也会入链；与"仅追加"同一防线 |
| D2 | 链粒度 | **全局单链**（按 `sequence` 递增） | 实现最简单、篡改检测最强（任何一行被删或改都断链）；避免平台事件（`tenant_id=NULL`）需单独链的复杂度 |
| D3 | 哈希算法 | **HMAC-SHA256** | 用独立密钥（`.env` 的 `AUDIT_HASH_KEY`，≥32 字节），防伪造：无密钥算不出合法哈希 |
| D4 | 本期范围 | **链计算 + 校验脚本 + 平台只读查询接口 + 启动自检** | WORM/锚点延后 M6；M1 只需"链可校验、篡改可检测" |

## 4. 实现方案

### 4.1 数据库层：哈希链触发器

新迁移（`<new_rev>_m1f_audit_hash_chain.py`）：

```sql
-- 密钥通过 GUC 传入（应用连接设置 app.audit_hash_key；迁移/校验工具也设置）
-- 不在 SQL 里存密钥

CREATE OR REPLACE FUNCTION audit_logs_compute_hash() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    v_prev_hash text;
    v_hash_input text;
BEGIN
    -- 全局链：取 sequence 最大且 hash 非空的那条（跳过未入链的历史行）
    SELECT hash INTO v_prev_hash
    FROM audit_logs
    WHERE hash IS NOT NULL
    ORDER BY sequence DESC LIMIT 1;

    NEW.prev_hash := v_prev_hash;  -- 链首为 NULL
    NEW.hash_version := 1;

    -- 拼接本行关键字段（排序稳定，字段列表与 writer.py 保持一致）
    v_hash_input := concat_ws('|',
        coalesce(NEW.event_id::text, ''),
        coalesce(NEW.tenant_id::text, ''),
        coalesce(NEW.actor_realm, ''),
        coalesce(NEW.actor_id::text, ''),
        coalesce(NEW.action, ''),
        coalesce(NEW.resource_type, ''),
        coalesce(NEW.resource_id, ''),
        coalesce(NEW.result, ''),
        coalesce(NEW.request_id, ''),
        coalesce(NEW.created_at::text, ''),
        coalesce(v_prev_hash, '')
    );

    NEW.hash := encode(hmac(
        convert_to(v_hash_input, 'UTF-8'),
        convert_to(current_setting('app.audit_hash_key', true), 'UTF-8'),
        'sha256'
    ), 'hex');

    RETURN NEW;
END;
$$;

CREATE TRIGGER audit_logs_hash_chain
BEFORE INSERT ON audit_logs
FOR EACH ROW EXECUTE FUNCTION audit_logs_compute_hash();
```

**密钥传递**：应用的两个 `db_session()`（租户/平台）在事务开头执行：
```sql
SELECT set_config('app.audit_hash_key', '<from .env>', true);
```
校验脚本/迁移工具同理。

**历史行处理**：`hash_version=0` 的行（M1-A~M1-E2 产生）不回填（仅追加约束禁止 UPDATE）；链从第一条新写入的行开始，`prev_hash` 取第一条 `hash IS NOT NULL` 的行。

### 4.2 应用层：审计写入器适配

`app/audit/writer.py` 变更：
- 移除 `hash=None, hash_version=0` 的硬编码（触发器接管）
- `db_session()` 新增可选参数或在 `app/config.py` 加 `audit_hash_key: SecretStr`，会话工厂自动 `set_config`
- 生产校验：`AUDIT_HASH_KEY` ≥32 字节、非占位值

### 4.3 校验脚本

`backend/scripts/verify_audit_chain.py`（CLI，Alembic 风格）：
```bash
uv run python scripts/verify_audit_chain.py --db-url "$TEST_DATABASE_URL" --hash-key "$AUDIT_HASH_KEY"
```
- 从第一条 `hash IS NOT NULL` 的行开始，逐行验证：
  - `prev_hash` 等于上一行的 `hash`（首行为 NULL）
  - 重算 HMAC 与存储的 `hash` 一致
- 输出：总行数、验证通过/失败、失败行的 `sequence` 与原因
- 退出码：0=全链通过，1=检测到篡改

### 4.4 平台审计只读查询接口

`GET /api/v1/platform/audit-logs`（权限 `platform.audit.view`，realm=platform）：
- 查询参数：`action` / `actor_id` / `tenant_id` / `result` / `start_time` / `end_time` / `cursor` / `limit`（默认 50，上限 200）
- 响应字段：与表列一一对应（脱敏后的 `metadata_json` 透传，不含明文敏感值——写入时已脱敏）
- 跨租审计查询**必须**走平台 realm + `platform.audit.view` 权限；租户 realm 无此接口，跨租请求 403 并审计

### 4.5 启动自检收口（TD-091）

在 `app/db/selfcheck.py` 新增：
- 验证 `audit_logs_compute_hash()` 触发器存在且 owner 具备 `BYPASSRLS`/超级用户
- 验证 `AUDIT_HASH_KEY` 在 staging/production 非空且 ≥32 字节（配置校验层）
- 不做"写入是否只经函数"的运行时检测（哈希链本身即防线：绕过触发器写入的行 `hash=NULL`，链校验会报断链）

### 4.6 配置

`.env` / `.env.example` 新增：
```
AUDIT_HASH_KEY=<openssl rand -hex 32>
```
- `app/config.py`：`audit_hash_key: SecretStr`，staging/production 校验非空 + ≥32 字节 + 非占位值
- 与 `JWT_SECRET`、`ENCRYPTION_KEK` 相互独立

## 5. 实现顺序与 TDD 清单

1. **数据**：新迁移（触发器 + 密钥 GUC 约束）→ 集成测试验证插入自动算哈希
2. **应用**：`writer.py` 适配 + `db_session()` 注入密钥 + `config.py` 新增字段 → 单元测试
3. **校验**：CLI 脚本 → 单元测试（构造合法链 / 篡改检测）
4. **接口**：`GET /platform/audit-logs` + 权限/跨租拒绝 → 集成 + 业务场景
5. **自检**：`selfcheck.py` 新增项 → 集成测试

**TDD 红→绿清单**：
- 集成：插入审计 → `prev_hash`/`hash`/`hash_version=1` 自动填充；`prev_hash` 等于上一条 `hash`；首条 `prev_hash IS NULL`
- 集成：密钥未设置 → 插入失败（触发器报错或 `hash=NULL`，取决于 GUC 行为——需要实测）
- 集成：UPDATE/DELETE 审计 → 权限拒绝（既有测试保持通过）
- 单元：校验脚本对合法链返回 0；对篡改行（模拟 UPDATE 后重算不一致）返回 1 并指出行号
- 业务场景 BS-M1-F-01：登录/改密/审批各产生审计 → 链连续 → 校验脚本通过 → 平台查询接口可按条件查出并返回正确字段 → 租户 realm 访问平台审计接口 403 + 拒绝审计
- 业务场景 BS-M1-F-02：直写 SQL 插入审计行（绕过应用）→ 触发器仍算哈希 → 链校验通过（证明数据库层防线）
- 集成：启动自检在触发器缺失时报错

## 6. 测试策略

| 层 | 覆盖 |
|---|---|
| 单元 | 校验脚本的哈希计算逻辑（构造输入串、比对）；配置校验 |
| 集成 | 触发器自动算哈希 / 密钥注入 / 历史行跳过 / 仅追加约束保持 / 自检新增项 |
| 业务场景 | BS-M1-F-01（全链路 + 查询接口 + 跨租拒绝）、BS-M1-F-02（绕过应用直写仍入链） |
| E2E | 不涉及（无前端页面） |

## 7. 预判技术债

| 条目 | 处置 |
|---|---|
| 历史行（`hash_version=0`）不回填 | 接受：仅追加约束禁止 UPDATE；链从首条新行开始，旧行"未入链"状态本身可审计 |
| 全局单链在高并发下的写入热点 | `sequence` 是递增 PK，无锁竞争问题；触发器内取 `prev_hash` 的 `SELECT ... LIMIT 1` 走 PK 索引，代价可控 |
| WORM 导出 / 外部锚点 | 延后 M6（`anchor_ref` 字段已预留） |
| 密钥轮换 | 延后 M6（轮换需"新密钥从某条开始"的分段链设计，当前单密钥即可） |

## 8. 高风险确认

本功能属**加密/防篡改**类高风险。四项决策已由用户逐项确认（2026-10-23）。实现后按门禁执行：全量测试 → 独立子 agent 审查 → 修复复测 → 提交推送 → 远端 CI 绿。

## 9. 实现结果（2026-10-24）

- **迁移** `a8c3e9f1b5d7`：pgcrypto 扩展 + `audit_logs_compute_hash()` SECURITY DEFINER 函数 + BEFORE INSERT 触发器。触发器开头 `pg_advisory_xact_lock(4218301)` 串行化并发插入（防链分叉）；密钥从 GUC `app.audit_hash_key` 读取，缺失时 fail-open 标 hash_version=0（不阻断审计写入）；`created_at` 用 `to_char(... AT TIME ZONE 'UTC', ...)` 确定性编码。
- **应用层**：`db_session()` 在事务内 `set_config('app.audit_hash_key', ...)` 注入密钥；`writer.py` 去掉硬编码 hash=None/version=0（触发器接管）；`config.py` 新增 `audit_hash_key: SecretStr`（staging/production 校验 ≥32 字节非占位值）。
- **校验脚本** `scripts/verify_audit_chain.py`：asyncpg 直连，逐行验 prev_hash 连续 + HMAC-SHA256 重算 + 未入链行（hash_version=0 且 sequence ≥ 首条入链行）检测。
- **查询接口** `GET /api/v1/platform/audit-logs`：`platform.audit.view` + realm=platform，cursor 分页，支持 action/actor_id/tenant_id/result/start_time/end_time 过滤。
- **启动自检**：触发器存在且 owner 具备 BYPASSRLS/超级用户。
- **测试**：集成 9 条（触发器算哈希/HMAC 特征/仅追加/密钥缺失标 v0/校验脚本/并发不分叉/错误密钥/自检触发器缺失）；业务场景 1 条（HTTP 全链路：查询/过滤/分页/权限拒绝/直写仍入链）。后端全量 570+ passed。
- **第一轮审查修复（B1/M1/M2/M3/m1）**：advisory lock 串行化；校验脚本报告未入链行；created_at 统一 UTC 编码；补并发/错误密钥/自检触发器缺失测试；去掉 fixture 补建触发器的 hack；config 注释如实描述 fail-open。
