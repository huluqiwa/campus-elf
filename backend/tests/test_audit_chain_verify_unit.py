"""审计哈希链校验脚本 verify_chain() 的负路径单测（M1-F 复审 Major-1）。

纯函数测试，不依赖数据库：篡改行（重算不匹配）与断链（prev_hash 不连续）
必须被检出，否则"篡改检测静默失效"时 CI 仍绿。
"""

from scripts.verify_audit_chain import ChainRow, compute_hash, verify_chain

KEY = "unit-test-audit-hash-key-0123456789abcdef"


def _make_row(
    sequence: int,
    *,
    action: str = "test.action",
    prev_hash: str | None = None,
    event_id: str | None = None,
) -> ChainRow:
    """构造一行并按触发器同款字段列表计算 hash（frozen slots，重建实例替换 hash）。"""

    row = ChainRow(
        sequence=sequence,
        event_id=event_id or f"event-{sequence}",
        tenant_id=None,
        actor_realm="system",
        actor_id=None,
        action=action,
        resource_type="test",
        resource_id=None,
        result="success",
        request_id=None,
        created_at="2026-10-24 00:00:00.000000",
        prev_hash=prev_hash,
        hash="",
    )
    return _rebuild_with_hash(row, compute_hash(row, KEY))


def _rebuild_with_hash(row: ChainRow, hash_hex: str) -> ChainRow:
    """ChainRow 是 frozen slots dataclass，重建实例替换 hash。"""

    return ChainRow(
        sequence=row.sequence,
        event_id=row.event_id,
        tenant_id=row.tenant_id,
        actor_realm=row.actor_realm,
        actor_id=row.actor_id,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        result=row.result,
        request_id=row.request_id,
        created_at=row.created_at,
        prev_hash=row.prev_hash,
        hash=hash_hex,
    )


def _make_chain(length: int) -> list[ChainRow]:
    """构造连续合法链：prev_hash 逐行链接、hash 按同密钥计算。"""

    rows: list[ChainRow] = []
    prev: str | None = None
    for i in range(1, length + 1):
        row = _make_row(i, prev_hash=prev)
        rows.append(row)
        prev = row.hash
    return rows


def test_verify_chain_passes_on_valid_chain() -> None:
    """合法链（含 prev_hash 链接）零问题。"""

    rows = _make_chain(3)
    assert verify_chain(rows, KEY) == []


def test_verify_chain_detects_tampered_field() -> None:
    """篡改行字段（action 改动）→ 重算不匹配被检出，含 sequence 与原因。"""

    rows = _make_chain(2)
    tampered = ChainRow(
        sequence=rows[1].sequence,
        event_id=rows[1].event_id,
        tenant_id=rows[1].tenant_id,
        actor_realm=rows[1].actor_realm,
        actor_id=rows[1].actor_id,
        action="tampered.action",
        resource_type=rows[1].resource_type,
        resource_id=rows[1].resource_id,
        result=rows[1].result,
        request_id=rows[1].request_id,
        created_at=rows[1].created_at,
        prev_hash=rows[1].prev_hash,
        hash=rows[1].hash,
    )
    problems = verify_chain([rows[0], tampered], KEY)

    assert len(problems) == 1, problems
    assert f"sequence={tampered.sequence}" in problems[0]
    assert "哈希不匹配" in problems[0]


def test_verify_chain_detects_broken_prev_hash_link() -> None:
    """中间行 prev_hash 被改 → 断链被检出，预期值=上一行存储 hash。"""

    rows = _make_chain(3)
    broken = ChainRow(
        sequence=rows[1].sequence,
        event_id=rows[1].event_id,
        tenant_id=rows[1].tenant_id,
        actor_realm=rows[1].actor_realm,
        actor_id=rows[1].actor_id,
        action=rows[1].action,
        resource_type=rows[1].resource_type,
        resource_id=rows[1].resource_id,
        result=rows[1].result,
        request_id=rows[1].request_id,
        created_at=rows[1].created_at,
        prev_hash="0" * 64,
        hash=rows[1].hash,
    )
    problems = verify_chain([rows[0], broken, rows[2]], KEY)

    # prev_hash 同时是哈希输入：改 prev_hash 既断链、也使重算不匹配 → 该行报两条问题；
    # 第三行 prev_hash 仍等于第二行存储 hash 且重算一致 → 不误报。
    assert any(f"sequence={broken.sequence}" in p and "prev_hash 不等于上一行的 hash" in p for p in problems), problems
    assert any(f"sequence={broken.sequence}" in p and "哈希不匹配" in p for p in problems), problems
    assert len(problems) == 2, problems


def test_verify_chain_detects_non_null_prev_hash_on_first_row() -> None:
    """首条已入链行 prev_hash 非 NULL → 报告（链头不完整，可能被删行）。"""

    rows = _make_chain(2)
    head = ChainRow(
        sequence=rows[0].sequence,
        event_id=rows[0].event_id,
        tenant_id=rows[0].tenant_id,
        actor_realm=rows[0].actor_realm,
        actor_id=rows[0].actor_id,
        action=rows[0].action,
        resource_type=rows[0].resource_type,
        resource_id=rows[0].resource_id,
        result=rows[0].result,
        request_id=rows[0].request_id,
        created_at=rows[0].created_at,
        prev_hash="f" * 64,
        hash=rows[0].hash,
    )
    problems = verify_chain([head, rows[1]], KEY)

    assert any("首条已入链行的 prev_hash 应为 NULL" in p for p in problems), problems


def test_verify_chain_detects_wrong_key() -> None:
    """用错误密钥校验 → 每行重算都不匹配（密钥错误可被感知）。"""

    rows = _make_chain(2)
    problems = verify_chain(rows, "wrong-key-0123456789abcdef-0123456789abcdef")

    assert len(problems) == 2
    assert all("哈希不匹配" in p for p in problems)
