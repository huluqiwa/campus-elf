"""回合预算计数器（M4-AI01c 步 1；design §6.2 数值 Settings 化）。

预算只做**降级**不做报错（决策卡 5-A）：调用方在 `can_*` 为假时带已有证据进入
GENERATE；`snapshot()` 供 budget span attributes 落 trace。
"""

from dataclasses import dataclass, field


@dataclass
class TurnBudget:
    """单回合预算（可变计数器；非线程安全——单回合单事件循环内使用）。"""

    max_decisions: int
    max_tool_calls: int
    max_retrievals: int
    max_tokens: int
    decisions: int = field(default=0)
    tool_calls: int = field(default=0)
    retrievals: int = field(default=0)
    tokens: int = field(default=0)

    def __post_init__(self) -> None:
        """上限必须为正（配置错误响亮失败，不静默放行）。"""

        for name in ("max_decisions", "max_tool_calls", "max_retrievals", "max_tokens"):
            if getattr(self, name) <= 0:
                raise ValueError(f"budget {name} must be positive")

    def can_decide(self) -> bool:
        """还能做模型决策。"""

        return self.decisions < self.max_decisions

    def can_act(self) -> bool:
        """还能执行工具调用。"""

        return self.tool_calls < self.max_tool_calls

    def can_retrieve(self) -> bool:
        """还能检索。"""

        return self.retrievals < self.max_retrievals

    def tokens_left(self) -> int:
        """token 余量。"""

        return max(self.max_tokens - self.tokens, 0)

    def can_spend_tokens(self, amount: int) -> bool:
        """还能消费给定 token 量。"""

        return self.tokens_left() >= amount

    def record_decision(self) -> None:
        """记一次模型决策。"""

        self.decisions += 1

    def record_tool_call(self) -> None:
        """记一次工具调用。"""

        self.tool_calls += 1

    def record_retrieval(self) -> None:
        """记一次检索。"""

        self.retrievals += 1

    def record_tokens(self, amount: int) -> None:
        """记 token 消耗（provider 报数；未报不计）。"""

        if amount > 0:
            self.tokens += amount

    def snapshot(self) -> dict[str, int]:
        """计数快照（budget span attributes）。"""

        return {
            "decisions": self.decisions,
            "tool_calls": self.tool_calls,
            "retrievals": self.retrievals,
            "tokens": self.tokens,
        }
