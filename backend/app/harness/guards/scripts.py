"""护栏安全话术常量（M4-AI09；与 LLM_UNCONFIGURED_TEXT 同款集中管理——默认口径 2）。

自伤支持性话术口径（A24）：表达关心与陪伴 + 线下求助资源（学校心理辅导老师 /
24 小时热线），不做诊断、不评判。
"""

from collections.abc import Set

INJECTION_REFUSAL = (
    "抱歉，这个问题我无法回答。如果你有学校相关的问题（比如报销、请假、课表等），"
    "我很乐意帮忙。"
)
ILLEGAL_REFUSAL = (
    "抱歉，这个问题涉及违法内容，我无法提供任何相关信息。如果你遇到紧急情况，"
    "请立即联系学校保卫处或拨打 110。"
)
SELF_HARM_SUPPORTIVE = (
    "听到你现在的感受，我很关心你。你并不孤单，很多人在困难时刻都有类似的感受——"
    "这些感受是可以被帮助和改善的。请尽快告诉学校心理辅导老师，或拨打 24 小时心理援助热线"
    "（全国统一：12356），也可以找你信任的老师或朋友聊一聊。你的安全最重要，"
    "我们都在你身边。"
)
GENERIC_GUARD_FALLBACK = (
    "抱歉，这个回答内容暂时无法展示。你可以换个说法再试一次，"
    "或联系管理员确认相关资料是否已发布。"
)


def safety_script_for(rules: Set[str]) -> str:
    """block 级命中 → 安全话术（自伤 > 违法 > 注入/泄露 > 通用）。"""

    if "output.self_harm" in rules or "input.self_harm" in rules:
        return SELF_HARM_SUPPORTIVE
    if "output.illegal" in rules or "input.illegal" in rules:
        return ILLEGAL_REFUSAL
    if "output.prompt_leak" in rules or "input.injection" in rules:
        return INJECTION_REFUSAL
    return GENERIC_GUARD_FALLBACK
