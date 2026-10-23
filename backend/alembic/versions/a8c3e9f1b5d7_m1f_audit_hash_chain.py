"""M1-F 审计哈希链：触发器 + 密钥 GUC（2026-10-23）。

设计（docs/design-briefs/M1-F-audit-hash-chain.md，用户确认 D1~D4）：
- D1 数据库触发器计算哈希（应用无法绕过，与仅追加同一防线）
- D2 全局单链（按 sequence 递增，跨租户/平台事件串成一条）
- D3 HMAC-SHA256，密钥从 GUC `app.audit_hash_key` 读取（应用/校验工具各自注入）
- D4 本期范围：链计算 + 校验脚本 + 平台只读查询接口 + 启动自检

历史行（`hash_version=0`，M1-A~M1-E2 产生）不回填：仅追加约束禁止 UPDATE。
链从第一条新写入的行开始：`prev_hash` 取 `hash IS NOT NULL` 的最大 sequence 行。

权限：触发器函数用 SECURITY DEFINER（需读 `audit_logs.hash` 取 prev_hash，
两个应用角色无 UPDATE 权限但触发器在 INSERT 前执行不受限）。
但**不**给应用角色额外授权——密钥通过 `set_config('app.audit_hash_key', ...)` 在事务内注入。

回滚：删除触发器与函数；不动数据（已入链的行保留哈希，但新行回到 hash_version=0）。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a8c3e9f1b5d7"
down_revision: str | None = "f7a9b1c3d5e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HASH_CHAIN_FUNCTION = r"""
CREATE OR REPLACE FUNCTION audit_logs_compute_hash() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_prev_hash text;
    v_hash_input text;
    v_key text;
BEGIN
    -- 密钥从 GUC 读取（应用/校验工具在事务内 set_config 注入）
    v_key := current_setting('app.audit_hash_key', true);
    IF v_key IS NULL OR v_key = '' THEN
        -- 密钥未注入（测试直写 / 绕过应用的场景）：不阻断审计写入，
        -- 标记为"未入链"（hash=NULL, hash_version=0），链校验可检测此类行为
        NEW.prev_hash := NULL;
        NEW.hash := NULL;
        NEW.hash_version := 0;
        RETURN NEW;
    END IF;

    -- 串行化：并发插入必须在取 prev_hash 前排队，否则两个事务读到同一 prev_hash 导致链分叉。
    -- 锁键用 hashtextextended 派生（与 sessions.py 的 advisory lock 同款方式，避免裸魔法数撞车）
    PERFORM pg_advisory_xact_lock(hashtextextended('audit_logs_hash_chain', 0));

    -- MA-1 修复：BIGSERIAL 在 BEFORE 触发器之前已分配 sequence，锁序与 sequence 序可能倒置
    --（B1 先 nextval 得小号但在锁前被抢占 → B2 先提交 → B1 恢复后 prev 指向 B2 → 双 prev 分叉）。
    -- 拿到锁后重新分配 sequence，保证锁序 = sequence 序。
    NEW.sequence := nextval(pg_get_serial_sequence('audit_logs', 'sequence'));

    -- 全局链：取 hash 非空的最大 sequence 行（跳过历史未入链行）
    SELECT hash INTO v_prev_hash
    FROM public.audit_logs
    WHERE hash IS NOT NULL
    ORDER BY sequence DESC
    LIMIT 1;

    NEW.prev_hash := v_prev_hash;
    NEW.hash_version := 1;

    -- 与校验脚本的字段列表保持一致（改动需同步两处：此处 + scripts/verify_audit_chain.py）。
    -- created_at 统一 UTC 确定性编码（避免会话 DateStyle/TimeZone 差异导致校验脆弱）
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
        coalesce(to_char(NEW.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US'), ''),
        coalesce(v_prev_hash, '')
    );

    NEW.hash := encode(
        hmac(
            convert_to(v_hash_input, 'UTF-8'),
            convert_to(v_key, 'UTF-8'),
            'sha256'
        ),
        'hex'
    );

    RETURN NEW;
END;
$$;
"""


def upgrade() -> None:
    """创建哈希链触发器（需 pgcrypto 提供 hmac()）。"""

    # hmac() 由 pgcrypto 扩展提供；幂等安装
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # 校验迁移角色具备 BYPASSRLS/超级用户（触发器需读 audit_logs 全表取 prev_hash）
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles
                WHERE rolname = current_user AND (rolsuper OR rolbypassrls)
            ) THEN
                RAISE EXCEPTION
                    'M1-F hash chain requires migration role superuser or BYPASSRLS';
            END IF;
        END $$;
        """
    )

    op.execute(_HASH_CHAIN_FUNCTION)
    op.execute(
        """
        CREATE TRIGGER audit_logs_hash_chain
        BEFORE INSERT ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION audit_logs_compute_hash()
        """
    )


def downgrade() -> None:
    """删除触发器与函数；不动数据（pgcrypto 保留——可能被其他功能使用）。"""

    op.execute("DROP TRIGGER IF EXISTS audit_logs_hash_chain ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS audit_logs_compute_hash()")
