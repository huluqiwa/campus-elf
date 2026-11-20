"""输入护栏检测器（M4-AI09 步 1；卡 4-A/D2/D5）。

纯函数（D2）：`detect_input(message) -> list[GuardResult]`，无 IO 无状态；
处置决策归 loop 层。动作口径（卡 4-A）：

- 注入特征/违法内容 → `block`（loop 走 refusal 话术 + guard_event）
- 自伤输入 → 标记 `input.self_harm`（loop 走支持性话术路径——D5，宁枉勿纵）
- 用户自身 PII → **放行**（用户自然陈述自身证件号属正常咨询；输出侧防泄露）
"""

from app.harness.contracts import GuardResult
from app.harness.guards.rules import _ILLEGAL_PATTERNS, _INJECTION_PATTERNS, _SELF_HARM_PATTERNS

_INJECTION_RULE = "input.injection"
_ILLEGAL_RULE = "input.illegal"
_SELF_HARM_RULE = "input.self_harm"


def detect_input(message: str) -> list[GuardResult]:
    """输入护栏检测：返回命中列表（loop 聚合决策——D2）。"""

    results: list[GuardResult] = []
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(message):
            results.append(
                GuardResult(
                    action="block",
                    rule=_INJECTION_RULE,
                    severity="high",
                    reason="输入命中注入/提示词探取特征",
                )
            )
            break  # 注入类一条命中即定性（避免同义模式重复计数）
    for pattern in _ILLEGAL_PATTERNS:
        if pattern.search(message):
            results.append(
                GuardResult(
                    action="block",
                    rule=_ILLEGAL_RULE,
                    severity="high",
                    reason="输入命中违法内容特征",
                )
            )
            break
    for pattern in _SELF_HARM_PATTERNS:
        if pattern.search(message):
            results.append(
                GuardResult(
                    action="block",
                    rule=_SELF_HARM_RULE,
                    severity="high",
                    reason="输入命中自伤/心理危机特征（走支持性话术）",
                )
            )
            break
    return results


def has_self_harm(results: list[GuardResult]) -> bool:
    """命中列表是否含自伤类（loop 决定支持性话术路径）。"""

    return any(r.rule == _SELF_HARM_RULE for r in results)
