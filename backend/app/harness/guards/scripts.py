"""护栏固定话术（M4-AI09；TD-363 分类化安全模板）。

模型只返回安全类别，不在正文中生成用户可见安全话术。模板集中放在 Git
版本化代码中，保证同类输入在不同模型/供应商下获得稳定、可审计的处置。
"""

from collections.abc import Set

from app.harness.router import VALID_SAFETY_PLANS, SafetyAction, SafetyCategory, SafetyPlan

INJECTION_REFUSAL = (
    "抱歉，这个问题我无法回答。如果你有学校相关的问题（比如报销、请假、课表等），我很乐意帮忙。"
)
ILLEGAL_REFUSAL = (
    "抱歉，这个问题涉及违法内容，我无法提供任何相关信息。如果你遇到紧急情况，"
    "请立即联系学校保卫处或拨打 110。"
)
SELF_HARM_SUPPORTIVE = (
    "听到你现在的感受，我很关心你。你不需要独自承担，也不代表你有问题。"
    "请尽快告诉一位可信成年人、学校心理中心或辅导员；如果你可能马上伤害自己，"
    "请立刻联系 110 或 120，也可以拨打全国 24 小时心理援助热线 12356。"
    "我拒绝提供任何可执行的自伤或轻生步骤。你的安全最重要；你可以只说明足以保障"
    "安全的信息，我会尊重必要的隐私，但不能替你隐瞒正在持续的危险。如果你同时遭受"
    "欺凌、网暴或骚扰，请保存截图、聊天记录、时间和地点等证据，并向辅导员、学生工作"
    "部门或正式举报渠道报告；也可以寻求学校心理中心或专业心理支持，不需要独自承受。"
)
GENERIC_GUARD_FALLBACK = (
    "抱歉，这个回答内容暂时无法展示。你可以换个说法再试一次，或联系管理员确认相关资料是否已发布。"
)
# 敏感回合写入历史时的固定占位：自伤/违法/注入等原文不得以明文进入
# conversation_messages，避免心理危机等敏感内容被长期留存（TD-363 审查 Blocker）。
SENSITIVE_HISTORY_PLACEHOLDER = "【敏感消息已脱敏】"

SAFETY_UNAVAILABLE_TEXT = (
    "这个问题目前无法安全处理。请换一种说法再试；如果你或身边的人正面临紧急危险，"
    "请立即联系可信任的人、学校保卫处或拨打 110/120，并优先前往安全地点。"
)

_SELF_HARM_REFUSE = (
    "我无法提供，也必须拒绝任何自伤或轻生方法。请不要独自承受这些想法，尽快告诉可信成年人、"
    "学校心理中心或辅导员；如果你可能马上伤害自己，请立刻联系 110 或 120，"
    "也可拨打全国 24 小时心理援助热线 12356。"
)
_MENTAL_HEALTH_SUPPORT = (
    "我不能诊断你是否患有某种心理疾病，持续失眠和情绪困扰需要由专业人员做专业评估。"
    "建议联系学校心理中心或医生说明情况；如果出现立即伤害自己或他人的紧急想法，"
    "请马上联系 110/120、辅导员或危机求助渠道。具体预约方式以学校官方公开信息为准。"
)
_VIOLENCE_SUPPORT = (
    "听到你正受到伤害或冲突困扰，我很理解你的难受和愤怒，你的感受值得被认真对待。"
    "先确保人身安全。请离开可能继续发生伤害的环境，联系可信亲友、辅导员或学校保卫处；"
    "紧急危险时立即报警或拨打 110/120，受伤时及时就医。尽量保存记录、照片、聊天记录、"
    "时间和地点等证据。我拒绝提供实施暴力或报复的方法，请不要自行报复，也不要用新的暴力"
    "回应冲突。"
)
_VIOLENCE_REFUSE = (
    "我必须拒绝提供伤害他人的方法或规避法律责任的做法。暴力行为可能带来法律责任和校纪处分。"
    "如果你与人发生冲突，请先离开危险环境，联系可信亲友、辅导员或学校相关部门沟通；"
    "如果已经出现现实人身危险，请及时求助，联系保卫处或拨打 110，并优先前往安全地点。"
)
_ILLEGAL_SUPPORT = (
    "先确保自己的人身和财产安全，避免继续与对方接触或再次转账。请保留聊天记录、转账凭证、"
    "文件、时间和地点等证据，及时告诉可信成年人、辅导员或学校保卫处；涉及诈骗、盗窃或其他"
    "紧急危险时及时报警或拨打 110。不要自行报复，也不要公开传播未经核实的个人信息。"
)
_ILLEGAL_REFUSE = (
    "我拒绝提供任何危险或违法行为的操作步骤，尤其不会提供可使人昏迷或失去意识的药物"
    "具体品种、数量或取得方式，也不会协助获取违禁品或实施考试作弊、盗窃、伪造学生证等行为。"
    "这些行为可能违反法律法规或考试纪律，并受到相应纪律处理与法律后果。"
    "请通过合法途径解决问题；遇到紧急危险请联系学校保卫处或拨打 110。"
)
_PRIVACY_SUPPORT = (
    "请先停止继续转发或公开相关内容，必要时修改可能已暴露账号的密码并检查登录记录。"
    "保存截图、时间和来源等证据，尽快告诉可信成年人、辅导员或学校信息保护、学生工作部门；"
    "如涉及威胁、跟踪或紧急人身危险，请及时联系保卫处或拨打 110。具体处理以学校正式流程为准。"
)
_PRIVACY_REFUSE = (
    "我拒绝帮助获取、查看或确认未经授权的他人身份证、心理健康、行踪或其他个人信息，"
    "也不能确认这些信息是否存在。如果只是怀疑室友有遗失、冲突或违规行为，请不要私自搜查；"
    "应联系宿管、辅导员或保卫处，通过有权限的正式业务渠道处理。隐私纠纷也应保留必要记录"
    "并交由相应部门核实，不要自行翻看他人的物品、账号或私密信息。"
)
_HARASSMENT_SUPPORT = (
    "我相信你的感受，你的安全和尊严最重要。请优先离开或减少与伤害者的接触，并告诉可信亲友、"
    "辅导员或学生工作部门；需要通过正式举报渠道提出举报，也可联系纪检部门、保卫处或报警。"
    "请保存截图、录音、聊天记录、时间和地点等证据。我拒绝提供跟踪、威胁、骚扰或报复对方的"
    "方法；请让正式渠道调查处理。你也可以寻求心理中心或专业心理支持，不需要独自承受。"
)
_HARASSMENT_REFUSE = (
    "我拒绝提供实施骚扰、跟踪、威胁或网暴他人的方法。这些行为可能带来法律责任和校纪处分。"
    "如果你与他人有冲突，请先保持距离，通过辅导员、正规投诉渠道或调解方式处理；"
    "如果你正面临人身危险，请及时联系保卫处或拨打 110。"
)
_ABUSIVE_REFUSE = (
    "我拒绝生成辱骂或羞辱他人的内容。你可以先冷静表达自己的感受和诉求，通过理性沟通、"
    "正规渠道反馈或寻找调解支持来处理冲突；如果你正遭受威胁或伤害，请优先确保安全并求助。"
)
_MAJOR_DECISION_SUPPORT = (
    "退学、离家或其他重大选择可能属于重大决定，请不急于在情绪或压力下马上行动。建议先与家长、辅导员、"
    "学业导师和可信任的人沟通，核对可能的选择与后果。我拒绝提供绕过正式流程、保证最快办理或"
    "躲避安全支持的办法。一般流程须以学校正式文件和经办部门的最新说明为准；如果你面临人身安全"
    "或心理危机，请先寻求紧急支持。"
)


def safety_response_script(action: SafetyAction, category: SafetyCategory) -> str:
    """按已校验的安全动作与类别选择服务端固定话术。"""

    plan = SafetyPlan(action=action, category=category)
    if action is SafetyAction.ALLOW:
        raise ValueError("allow/none 安全计划不产生安全终止话术")
    if plan not in VALID_SAFETY_PLANS:
        raise ValueError(f"不支持的安全动作/类别组合：{action.value}/{category.value}")

    if category is SafetyCategory.SELF_HARM:
        return _SELF_HARM_REFUSE if action is SafetyAction.REFUSE else SELF_HARM_SUPPORTIVE
    if category is SafetyCategory.MENTAL_HEALTH:
        return _MENTAL_HEALTH_SUPPORT
    if category is SafetyCategory.VIOLENCE:
        return _VIOLENCE_REFUSE if action is SafetyAction.REFUSE else _VIOLENCE_SUPPORT
    if category is SafetyCategory.ILLEGAL:
        return _ILLEGAL_REFUSE if action is SafetyAction.REFUSE else _ILLEGAL_SUPPORT
    if category is SafetyCategory.PRIVACY:
        return _PRIVACY_REFUSE if action is SafetyAction.REFUSE else _PRIVACY_SUPPORT
    if category is SafetyCategory.HARASSMENT:
        return _HARASSMENT_REFUSE if action is SafetyAction.REFUSE else _HARASSMENT_SUPPORT
    if category is SafetyCategory.ABUSIVE_CONTENT:
        return _ABUSIVE_REFUSE
    if category is SafetyCategory.MAJOR_DECISION:
        return _MAJOR_DECISION_SUPPORT
    raise ValueError(f"不支持的敏感内容类别：{category.value}")


def safety_script_for(rules: Set[str]) -> str:
    """确定性输入/输出护栏的兼容话术（自伤 > 违法 > 注入 > 通用）。"""

    if "output.self_harm" in rules or "input.self_harm" in rules:
        return SELF_HARM_SUPPORTIVE
    if "output.illegal" in rules or "input.illegal" in rules:
        return ILLEGAL_REFUSAL
    if "output.prompt_leak" in rules or "input.injection" in rules:
        return INJECTION_REFUSAL
    return GENERIC_GUARD_FALLBACK
