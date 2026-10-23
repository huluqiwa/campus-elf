"""审计哈希链校验脚本（M1-F，TD-068）。

用法：
    cd backend
    uv run python scripts/verify_audit_chain.py \
        --db-url "postgresql://user:pass@host:port/db" \
        --hash-key "$AUDIT_HASH_KEY"

验证内容：
    从第一条 hash IS NOT NULL 的行开始，逐行检查：
    1. prev_hash 等于上一行的 hash（首行为 NULL）；
    2. 用 HMAC-SHA256 重算哈希，与存储值比对。

退出码：
    0 = 全链通过；1 = 检测到篡改或链断裂；2 = 参数/连接错误。

字段列表与数据库触发器 `audit_logs_compute_hash()` 保持一致（改动需同步两处（此处 + 迁移内触发器））。
"""

import argparse
import hashlib
import hmac
import sys
from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True, slots=True)
class ChainRow:
    """链上一行的最小字段集。"""

    sequence: int
    event_id: str
    tenant_id: str | None
    actor_realm: str
    actor_id: str | None
    action: str
    resource_type: str
    resource_id: str | None
    result: str
    request_id: str | None
    created_at: str
    prev_hash: str | None
    hash: str


def compute_hash(row: ChainRow, key: str) -> str:
    """按与触发器一致的字段列表拼接并计算 HMAC-SHA256（hex）。"""

    parts = [
        row.event_id or "",
        row.tenant_id or "",
        row.actor_realm or "",
        row.actor_id or "",
        row.action or "",
        row.resource_type or "",
        row.resource_id or "",
        row.result or "",
        row.request_id or "",
        row.created_at or "",
        row.prev_hash or "",
    ]
    message = "|".join(parts).encode("utf-8")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


async def fetch_rows(conn: asyncpg.Connection) -> list[ChainRow]:
    """取所有已入链行（按 sequence 升序）。"""

    rows = await conn.fetch(
        """
        SELECT sequence, event_id::text, tenant_id::text, actor_realm,
               actor_id::text, action, resource_type, resource_id,
               result, request_id,
               to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') as created_at,
               prev_hash, hash
        FROM audit_logs
        WHERE hash IS NOT NULL
        ORDER BY sequence ASC
        """
    )
    return [
        ChainRow(
            sequence=r["sequence"],
            event_id=r["event_id"],
            tenant_id=r["tenant_id"],
            actor_realm=r["actor_realm"],
            actor_id=r["actor_id"],
            action=r["action"],
            resource_type=r["resource_type"],
            resource_id=r["resource_id"],
            result=r["result"],
            request_id=r["request_id"],
            created_at=r["created_at"],
            prev_hash=r["prev_hash"],
            hash=r["hash"],
        )
        for r in rows
    ]


async def fetch_unchained_count(conn: asyncpg.Connection) -> tuple[int, int]:
    """统计"链开始后仍为 hash_version=0"的未入链行（绕过密钥写入的信号）。

    返回 (未入链行数, 首条入链行的 sequence；链为空返回 0)。
    """

    first_chained = await conn.fetchval(
        "SELECT min(sequence) FROM audit_logs WHERE hash IS NOT NULL"
    )
    if first_chained is None:
        # 链从未开始：检查是否存在未入链行（staging 漏配密钥 / 全部绕过密钥的场景）
        count = await conn.fetchval(
            "SELECT count(*) FROM audit_logs WHERE hash_version = 0"
        )
        return count or 0, 0
    count = await conn.fetchval(
        "SELECT count(*) FROM audit_logs WHERE hash_version = 0 AND sequence >= $1",
        first_chained,
    )
    return count or 0, first_chained


def verify_chain(rows: list[ChainRow], key: str) -> list[str]:
    """逐行校验链的连续性与哈希正确性，返回问题列表（空=通过）。"""

    problems: list[str] = []
    prev_hash: str | None = None

    for i, row in enumerate(rows):
        label = f"sequence={row.sequence}（第 {i + 1} 条已入链行）"

        # 1. prev_hash 连续性
        if i == 0:
            if row.prev_hash is not None:
                problems.append(f"{label}：首条已入链行的 prev_hash 应为 NULL，实际={row.prev_hash}")
        elif row.prev_hash != prev_hash:
            problems.append(
                f"{label}：prev_hash 不等于上一行的 hash（预期={prev_hash}，实际={row.prev_hash}）"
            )

        # 2. 重算哈希
        expected = compute_hash(row, key)
        if expected != row.hash:
            problems.append(f"{label}：哈希不匹配（重算={expected[:16]}…，存储={row.hash[:16]}…）")

        prev_hash = row.hash

    return problems


async def main() -> int:
    """入口：解析参数、连接、校验、输出。"""

    parser = argparse.ArgumentParser(description="审计哈希链校验（M1-F）")
    parser.add_argument("--db-url", required=True, help="PostgreSQL 连接串（直连，非 SQLAlchemy）")
    parser.add_argument("--hash-key", required=True, help="AUDIT_HASH_KEY（≥32 字节）")
    args = parser.parse_args()

    if len(args.hash_key) < 32:
        print("ERROR: --hash-key 长度不足 32 字节", file=sys.stderr)
        return 2

    conn: asyncpg.Connection | None = None
    try:
        conn = await asyncpg.connect(args.db_url)
        rows = await fetch_rows(conn)
        unchained_count, _ = await fetch_unchained_count(conn)
    except Exception as exc:
        print(f"ERROR: 无法连接或读取审计表：{exc}", file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            await conn.close()

    if not rows:
        if unchained_count:
            print(f"⚠️  链为空但存在 {unchained_count} 条未入链行（hash_version=0）")
            return 1
        print("链为空：尚未有已入链行（hash IS NOT NULL），校验通过（trivially）。")
        return 0

    problems = verify_chain(rows, args.hash_key)
    if unchained_count:
        problems.append(
            f"检测到 {unchained_count} 条未入链行（hash_version=0，可能绕过密钥写入）"
        )

    print(f"已入链行数：{len(rows)}（sequence {rows[0].sequence} ~ {rows[-1].sequence}）")
    if problems:
        print(f"\n❌ 检测到 {len(problems)} 个问题：")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("✅ 全链通过：prev_hash 连续、哈希重算一致，无未入链行。")
    return 0


if __name__ == "__main__":
    import asyncio

    sys.exit(asyncio.run(main()))
