"""审计 HMAC 哈希链、序号与脱敏测试。"""

from uuid import uuid4

import pytest

from app.harness.audit import AuditEvent, InMemoryAuditSink, redact_mapping

SECRET = "test-secret-key-123456"


def event(metadata: dict[str, object] | None = None) -> AuditEvent:
    """构造测试审计事件。"""

    return AuditEvent(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        action="test.action",
        resource_type="test",
        resource_id="1",
        result="success",
        metadata=metadata or {},
    )


def test_audit_hash_chain_and_verify() -> None:
    """追加事件形成带序号/HMAC 的链且可验证。"""

    sink = InMemoryAuditSink(SECRET)
    first = sink.append(event({"password": "secret", "nested": {"token": "abc"}}))
    second = sink.append(event({"ok": True}))

    assert first.sequence == 1
    assert second.sequence == 2
    assert first.prev_hash == ""
    assert first.hash
    assert second.prev_hash == first.hash
    assert sink.count == 2
    assert sink.head_hash == second.hash
    assert sink.verify() is True
    assert first.metadata["password"] == "***"
    assert first.metadata["nested"]["token"] == "***"


def test_audit_tamper_detection() -> None:
    """篡改存储中的事件后 verify 失败。"""

    sink = InMemoryAuditSink(SECRET)
    sink.append(event({"ok": True}))
    tampered = sink.events[0].model_copy(deep=True)
    tampered.metadata["ok"] = False
    sink._events[0] = tampered  # 模拟存储被篡改
    assert sink.verify() is False


def test_audit_external_anchor_detects_truncation() -> None:
    """外部锚点可检测尾部截断。"""

    sink = InMemoryAuditSink(SECRET)
    sink.append(event({"ok": 1}))
    sink.append(event({"ok": 2}))
    head = sink.head_hash
    assert sink.verify(expected_head=head, expected_count=2) is True
    sink._events.pop()
    assert sink.verify(expected_head=head, expected_count=2) is False


def test_audit_expected_head_mismatch() -> None:
    """链头与外部锚点不一致时 verify 失败。"""

    sink = InMemoryAuditSink(SECRET)
    sink.append(event({"ok": True}))
    assert sink.verify(expected_head="wrong-head") is False


def test_audit_rejects_duplicate_event_id() -> None:
    """重放相同 event_id 被拒绝。"""

    sink = InMemoryAuditSink(SECRET)
    original = event({"ok": True})
    sink.append(original)
    with pytest.raises(ValueError):
        sink.append(original)


def test_redact_mapping_is_recursive_and_value_aware() -> None:
    """敏感 key 递归脱敏，值中的邮箱/手机/证件号也被脱敏。"""

    result = redact_mapping(
        {
            "api_key": "x",
            "nested": {"id_card": "110101199001011234", "safe": "ok"},
            "list": [{"authorization": "Bearer x"}],
            "contact": "a@example.com",
            "note": "13800138000",
            "spaced": "138 0013 8000",
            "ref": "110101199001011234",
            "old_id": "110101900101123",
        }
    )
    assert result["api_key"] == "***"
    assert result["nested"]["id_card"] == "***"
    assert result["nested"]["safe"] == "ok"
    assert result["list"][0]["authorization"] == "***"
    assert result["contact"] == "***"
    assert result["note"] == "***"
    assert result["spaced"] == "***"
    assert result["ref"] == "***"
    assert result["old_id"] == "***"
