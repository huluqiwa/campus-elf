"""回合预算计数器校验（M4-AI01c 步 1；design §6.2 数值）。"""

import pytest

from app.harness.budget import TurnBudget


def budget(**overrides: int) -> TurnBudget:
    """按 design 默认值构造（可覆盖）。"""

    values = {"max_decisions": 5, "max_tool_calls": 3, "max_retrievals": 2, "max_tokens": 12000}
    values.update(overrides)
    return TurnBudget(**values)


def test_initial_budget_allows_everything() -> None:
    """初始态：三类预算均可消费。"""

    b = budget()
    assert b.can_decide() and b.can_act() and b.can_retrieve()
    assert b.tokens_left() == 12000


def test_decision_budget_exhausts_at_max() -> None:
    """决策预算：第 max 次后不可再决策。"""

    b = budget(max_decisions=2)
    b.record_decision()
    assert b.can_decide()
    b.record_decision()
    assert not b.can_decide()


def test_tool_call_budget_exhausts_at_max() -> None:
    """工具预算：3 次后不可再调用。"""

    b = budget()
    for _ in range(3):
        b.record_tool_call()
    assert not b.can_act()


def test_retrieval_budget_exhausts_at_max() -> None:
    """检索预算：2 次后不可再检索。"""

    b = budget()
    b.record_retrieval()
    assert b.can_retrieve()
    b.record_retrieval()
    assert not b.can_retrieve()


def test_token_budget_counts_down() -> None:
    """token 预算：消耗后余量递减，耗尽即不可再消费。"""

    b = budget(max_tokens=100)
    b.record_tokens(60)
    assert b.tokens_left() == 40
    b.record_tokens(40)
    assert b.tokens_left() == 0
    assert not b.can_spend_tokens(1)


def test_counters_snapshot_for_trace() -> None:
    """快照（budget span attributes 用）：返回四项计数字典。"""

    b = budget()
    b.record_decision()
    b.record_tool_call()
    b.record_retrieval()
    b.record_tokens(500)
    snapshot = b.snapshot()
    assert snapshot == {
        "decisions": 1,
        "tool_calls": 1,
        "retrievals": 1,
        "tokens": 500,
    }


def test_budget_rejects_invalid_max() -> None:
    """上限必须为正（配置错误响亮失败）。"""

    with pytest.raises(ValueError):
        TurnBudget(max_decisions=0, max_tool_calls=3, max_retrievals=2, max_tokens=12000)
