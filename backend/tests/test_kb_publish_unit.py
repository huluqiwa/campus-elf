"""KB-05 发布/热更新单测（状态机发布/下线/删除转移 + 守卫 + alias 命名与切换操作）。

口径（`docs/design-briefs/M3-KB-05.md` 已确认 D1-A/D2-A/D4-A/D5+D6-A）：
- `TEST_PASSED → PUBLISHED`（发布）、`PUBLISHED → OFFLINE/DELETED`（下线/删除软删）；
- OFFLINE/DELETED 是终态（重发布走新版本，无出边）；
- alias 每文档一个：`tenant_{tenant_hash}_kb_d{document_hash}_active`（design §7.4 修订，
  名内不出现明文租户/文档 id，与 collection 命名同款隐私口径）；
- 切换操作：首次发布 = Create 单操作；切换 = [Delete, Create] 单次
  `update_collection_aliases` 原子完成。
"""

from uuid import uuid4

import pytest
from qdrant_client import models as qm

from app.kb.indexer import alias_name, alias_switch_ops
from app.kb.status import (
    KB_STATUS_APPROVED,
    KB_STATUS_BUILDING,
    KB_STATUS_DELETED,
    KB_STATUS_FAILED,
    KB_STATUS_OFFLINE,
    KB_STATUS_PUBLISHED,
    KB_STATUS_READY,
    KB_STATUS_TEST_FAILED,
    KB_STATUS_TEST_PASSED,
    KB_STATUS_TESTING,
    NotPublishableError,
    NotPublishedError,
    assert_deletable,
    assert_offline_allowed,
    assert_publishable,
    assert_rollback_target,
    can_transition,
)

# ---------------------------------------------------------------------------
# 状态机新转移
# ---------------------------------------------------------------------------


def test_test_passed_can_publish_and_retest() -> None:
    """TEST_PASSED 出边：发布（PUBLISHED）或重测（TESTING），别无其他。"""

    assert can_transition(KB_STATUS_TEST_PASSED, KB_STATUS_PUBLISHED) is True
    assert can_transition(KB_STATUS_TEST_PASSED, KB_STATUS_TESTING) is True
    for target in (KB_STATUS_OFFLINE, KB_STATUS_DELETED, KB_STATUS_APPROVED, "UNKNOWN"):
        assert can_transition(KB_STATUS_TEST_PASSED, target) is False


def test_published_goes_only_to_offline_or_deleted() -> None:
    """PUBLISHED 出边：下线（OFFLINE）/删除（DELETED）；不可回测试/自环/重发布。"""

    assert can_transition(KB_STATUS_PUBLISHED, KB_STATUS_OFFLINE) is True
    assert can_transition(KB_STATUS_PUBLISHED, KB_STATUS_DELETED) is True
    for target in (
        KB_STATUS_TESTING,
        KB_STATUS_TEST_PASSED,
        KB_STATUS_PUBLISHED,
        KB_STATUS_APPROVED,
        "UNKNOWN",
    ):
        assert can_transition(KB_STATUS_PUBLISHED, target) is False


def test_offline_and_deleted_are_terminal() -> None:
    """OFFLINE / DELETED 是终态：任何迁移都拒绝（重发布走新版本）。"""

    for current in (KB_STATUS_OFFLINE, KB_STATUS_DELETED):
        for target in (
            KB_STATUS_PUBLISHED,
            KB_STATUS_OFFLINE,
            KB_STATUS_DELETED,
            KB_STATUS_TESTING,
            "UNKNOWN",
        ):
            assert can_transition(current, target) is False


def test_ready_cannot_publish_directly() -> None:
    """READY 未过测试不得发布（TEST_PASSED 才能发布，state-machines §3 不变量）。"""

    assert can_transition(KB_STATUS_READY, KB_STATUS_PUBLISHED) is False
    assert can_transition(KB_STATUS_APPROVED, KB_STATUS_PUBLISHED) is False
    assert can_transition(KB_STATUS_TEST_FAILED, KB_STATUS_PUBLISHED) is False


# ---------------------------------------------------------------------------
# 守卫
# ---------------------------------------------------------------------------


def test_publishable_only_from_test_passed() -> None:
    """发布守卫：只有 TEST_PASSED 可发布（KB-05 的 409 依据 KB_NOT_TEST_PASSED）。"""

    assert_publishable(KB_STATUS_TEST_PASSED)

    for status in (
        KB_STATUS_READY,
        KB_STATUS_TEST_FAILED,
        KB_STATUS_TESTING,
        KB_STATUS_PUBLISHED,
        KB_STATUS_OFFLINE,
        KB_STATUS_DELETED,
        KB_STATUS_APPROVED,
        "UNKNOWN",
    ):
        with pytest.raises(NotPublishableError, match="不可发布"):
            assert_publishable(status)


def test_offline_and_delete_only_from_published() -> None:
    """下线/删除守卫：只有 PUBLISHED 可下线/删除（409 依据 KB_NOT_PUBLISHED）。"""

    assert_offline_allowed(KB_STATUS_PUBLISHED)
    assert_deletable(KB_STATUS_PUBLISHED)

    for status in (
        KB_STATUS_READY,
        KB_STATUS_TEST_PASSED,
        KB_STATUS_TESTING,
        KB_STATUS_OFFLINE,
        KB_STATUS_DELETED,
        KB_STATUS_BUILDING,
        KB_STATUS_FAILED,
        "UNKNOWN",
    ):
        with pytest.raises(NotPublishedError, match="PUBLISHED"):
            assert_offline_allowed(status)
        with pytest.raises(NotPublishedError, match="PUBLISHED"):
            assert_deletable(status)


def test_rollback_target_only_from_published() -> None:
    """回滚目标守卫：目标版本必须是 PUBLISHED（D2-A：切到任意 PUBLISHED 目标）。"""

    assert_rollback_target(KB_STATUS_PUBLISHED)

    for status in (
        KB_STATUS_READY,
        KB_STATUS_TEST_PASSED,
        KB_STATUS_OFFLINE,
        KB_STATUS_DELETED,
        "UNKNOWN",
    ):
        with pytest.raises(NotPublishedError, match="PUBLISHED"):
            assert_rollback_target(status)


# ---------------------------------------------------------------------------
# alias 命名与切换操作（indexer）
# ---------------------------------------------------------------------------


def test_alias_name_is_document_scoped_and_pseudonymous() -> None:
    """alias 每文档一个；名内不含明文租户/文档 id；确定且可区分。"""

    tenant_id = uuid4()
    doc_a = uuid4()
    doc_b = uuid4()

    alias_a = alias_name(tenant_id, doc_a)
    assert alias_a == alias_name(tenant_id, doc_a)
    assert alias_a != alias_name(tenant_id, doc_b)
    assert str(tenant_id) not in alias_a
    assert str(doc_a) not in alias_a
    assert alias_a.startswith("tenant_")
    assert "_kb_d" in alias_a
    assert alias_a.endswith("_active")


def test_alias_switch_ops_first_publish_creates_only() -> None:
    """首次发布（无旧 collection）：单个 Create 操作。"""

    ops = alias_switch_ops("tenant_x_kb_d_y_active", None, "collection_new")
    assert len(ops) == 1
    assert isinstance(ops[0], qm.CreateAliasOperation)
    assert ops[0].create_alias.collection_name == "collection_new"
    assert ops[0].create_alias.alias_name == "tenant_x_kb_d_y_active"


def test_alias_switch_ops_replaces_atomically() -> None:
    """切换（有旧 collection）：[Delete, Create] 单次调用原子完成（D1-A 蓝绿）。"""

    ops = alias_switch_ops("tenant_x_kb_d_y_active", "collection_old", "collection_new")
    assert len(ops) == 2
    assert isinstance(ops[0], qm.DeleteAliasOperation)
    assert ops[0].delete_alias.alias_name == "tenant_x_kb_d_y_active"
    assert isinstance(ops[1], qm.CreateAliasOperation)
    assert ops[1].create_alias.collection_name == "collection_new"
    assert ops[1].create_alias.alias_name == "tenant_x_kb_d_y_active"
