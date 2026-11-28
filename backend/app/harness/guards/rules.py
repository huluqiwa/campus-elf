"""护栏规则表（M4-AI09 步 1；D4：模式/关键词常量内置于代码，不引外部文件）。

来源标注：各模式以注释标注触发场景与调校来源（AI 评估批回归集回灌——TD-P1）。
检测器（input.py/output.py）消费本模块；处置决策归 loop 层（D2）。

红线依据：A7 七类（requirements 附录 A，M4-AI01a 决策卡 4 方案一）+ A24
（自伤支持性话术，宁枉勿纵）。
"""

import re

# ---------------------------------------------------------------------------
# 输入侧：注入特征（含提示词探取/角色扮演诱导）
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 指令覆盖类
    re.compile(
        r"忽略(之前|上面|以上|前面|先前|所有|全部)?(的)?(所有|全部)?(的)?(指令|提示|规则|设定)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(ignore|disregard|forget)\s+(all\s+)?(previous|prior|above)\s+(instructions|rules|prompts)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(system\s*prompt|系统提示词?|你的?(内置)?(指令|设定|提示语))\s*(是|什么|给我|看看|展示|输出|泄露)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(告诉我|透露|展示|输出|打印)(你的)?(系统提示词?|内部指令|初始指令|真实身份设定)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(reveal|show|print|repeat)\s+(your\s+)?(system\s+prompt|instructions)", re.IGNORECASE
    ),
    # 角色扮演诱导（DAN/越狱变体：解除限制/无限制模式）
    re.compile(r"(解除|摆脱|无视|突破)(所有)?(限制|约束|审查|过滤)", re.IGNORECASE),
    re.compile(r"(jailbreak|DAN\s*mode|developer\s*mode|无限制模式)", re.IGNORECASE),
    re.compile(r"(你现在|假装|扮演)(是)?(一个)?(没有|不受|(无)?道德| unrestricted)", re.IGNORECASE),
    # 分隔符/结构注入
    re.compile(r"<\|(im_start|im_end|system|endoftext)\|>", re.IGNORECASE),
)

# ---------------------------------------------------------------------------
# 内容安全：违法/犯罪指导关键词（输入与输出共用；命中即 block）
# ---------------------------------------------------------------------------

_ILLEGAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 只匹配“索取实施方式”，避免把“制作炸弹会承担什么责任”等政策咨询当成
    # 实施请求。确定性护栏是语义 route_v4 之后的最后防线，本层不能退化成
    # 单个危险名词即拦截。TD-363 / SC-A3。
    re.compile(
        r"(怎么|怎样|如何|教我)"
        # 排除“如何处理制作炸弹的违纪行为”等政策/处置咨询：查询词后若先出现
        # 处理/处分/责任等词，说明问的是后果而非实施方式。
        r"(?![^，。！？]{0,10}(处理|处分|责任|处罚|违纪|规定|后果|应对|看待|管理|危害|影响))"
        r".{0,16}"
        r"(制作|制造|合成|组装).{0,8}(炸弹|爆炸物|炸药|毒品|病毒|武器|枪支)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(告诉我|给我|帮我|想要|打算).{0,16}"
        r"(制作|制造|合成|组装).{0,8}(炸弹|爆炸物|炸药|毒品|病毒|武器|枪支)"
        r"(?!.{0,16}(责任|处分|处罚|后果|规定|制度|政策|判刑))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(炸弹|爆炸物|炸药|毒品|病毒|武器|枪支).{0,8}"
        r"(制作|制造|合成|组装).{0,8}(方法|教程|步骤|指南|流程|配方|材料)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(制作|制造|合成|组装).{0,8}(炸弹|爆炸物|炸药|毒品|病毒|武器|枪支)"
        r".{0,8}(方法|教程|步骤|指南|流程|配方|材料)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(how\s+(do|can|could|would|should)\s+i|how\s+to|teach\s+me\s+to|"
        r"tell\s+me\s+how\s+to)\s+(make|build|synthesize|produce)\s+"
        r"(a\s+)?(bomb|explosive|drug|virus|weapon)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(give\s+me|provide)\s+(instructions|steps|directions)\s+"
        r"(for|to)\s+(make|build|synthesize|produce)\s+"
        r"(a\s+)?(bomb|explosive|drug|virus|weapon)",
        re.IGNORECASE,
    ),
    # 购买渠道索取（违禁药品/毒品/武器）：属实施类请求，不能只拦“制作”。
    # 覆盖“怎么买到/我想买/我要购买/哪里能买”等中文直接购买意图，用负向前瞻
    # 排除“购买渠道的管理规定/法律责任”等政策咨询（TD-363 审查 Major）。
    re.compile(
        r"(想|要|打算|准备|需要|帮我|给我|如何|怎么|怎样|哪里|在哪|能不能|可以)"
        r"(?![^，。！？]{0,8}"
        r"(了解|知道|法律|法规|规定|制度|政策|处罚|处分|后果|管理|监管|危害|案例|判刑|新闻))"
        r"[^，。！？]{0,8}"
        r"(买|买到|购买|购得|获取|搞到|弄到|入手|下单|采购)[^，。！？]{0,6}"
        r"(违禁药品|违禁品|毒品|冰毒|海洛因|大麻|枪支|枪械|弹药|炸弹|炸药|爆炸物|违禁药物)"
    ),
    re.compile(
        r"(how\s+(?:to|can\s+i|could\s+i|do\s+i|would\s+i|should\s+i)"
        r"|where\s+(?:(?:can|could|do|would|should)\s+i\s+)?(?:to\s+)?)"
        r"\s*(buy|get|obtain|purchase|order)\s+(?:a\s+)?"
        r"(drugs?|cocaine|heroin|meth|marijuana|cannabis|guns?|firearms?|weapons?|bombs?|explosives?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bi\s+(want|need|plan|intend|would\s+like)\s+to\s+"
        r"(buy|get|obtain|purchase|order)\s+(a\s+)?"
        r"(drugs?|cocaine|heroin|meth|marijuana|cannabis|guns?|firearms?|weapons?|bombs?|explosives?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(杀人|谋杀|下毒|投毒)(方法|教程|指南|步骤)"),
    re.compile(r"(毒品|冰毒|海洛因)(制作|提炼|合成)(方法|教程|步骤|流程)"),
)

# ---------------------------------------------------------------------------
# 自伤/心理危机（A24：支持性话术，宁枉勿纵；severity=high）
# ---------------------------------------------------------------------------

# inquiry guard：在意图动词之后若紧跟“了解/预防/培训/制度…”等词，视为政策
# 咨询而非当前风险，避免把“我想了解自杀预防的知识”误判为自伤。
_INQUIRY_GUARD = (
    r"(?![^，。！？]{0,6}(了解|知道|学习|预防|帮助|关心|统计|制度|政策|"
    r"培训|干预|报名|讲座|宣传|意义))"
)

_SELF_HARM_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 确定性层只负责直接、当前的自伤风险；制度、培训、统计、预案等政策咨询
    # 由 route_v4 语义判断后交给本校 KB，不能用裸名词一刀切。TD-363 / SC-A3。
    # 1) 无歧义的痛苦表达：本身即风险信号，无需人称标记。
    re.compile(r"(想死|不想活|活不下去|活着没意思|活着没意义|了结自己|了结我自己|结束一切)"),
    # 2) 第一人称：意图动词 + 自伤对象；排除“想了解/想预防”等政策咨询句式。
    re.compile(
        r"(我|本人|自己)[^，。！？]{0,10}"
        r"(?:想|要|打算|准备|正在|已经|忍不住|控制不住|计划)"
        + _INQUIRY_GUARD
        + r"[^，。！？]{0,10}(自杀|自残|自伤|轻生|寻死|结束生命|结束一切)"
    ),
    # 3) 第三人称近关系：同样要求意图动词；排除政策咨询。
    re.compile(
        r"(室友|同学|朋友|家人|孩子|父母|爸爸|妈妈|男朋友|女朋友|他|她)[^，。！？]{0,10}"
        r"(?:想|要|打算|准备|正在|已经|忍不住|控制不住|计划)"
        + _INQUIRY_GUARD
        + r"[^，。！？]{0,10}(自杀|自残|自伤|轻生|寻死|结束生命|结束一切|想死|不想活)"
    ),
    # 4) 带意图前缀的直接“结束生命/结束自己生命”。
    re.compile(r"(想|要|打算|准备|正在)[^，。！？]{0,6}结束(我的|自己的|我自己的)?生命"),
    re.compile(
        r"\b(i\s+(want|plan|intend|am\s+going|'m\s+going)\s+to\s+"
        r"(kill\s+myself|end\s+my\s+life|hurt\s+myself)|i\s+(feel|am)\s+suicidal)\b",
        re.IGNORECASE,
    ),
)

# ---------------------------------------------------------------------------
# 输出侧：系统提示词/CoT 泄露（输出含模板原文片段）
# ---------------------------------------------------------------------------

PROMPT_LEAK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"你是「?校园陪伴精灵」?[，,]?(根据|作为|提供)"),
    re.compile(r"##\s*(回答规则|红线|意图清单|参考资料|用户问题)"),
    re.compile(
        r"(我的|我的全部)?(思考过程|内部指令|系统提示|prompt)\s*(是|如下|为)：", re.IGNORECASE
    ),
    re.compile(r"不得使用超出参考资料条目数的编号"),
)

# ---------------------------------------------------------------------------
# 输出侧：密钥/凭证形态（③工具参数/内网地址/密钥）
# ---------------------------------------------------------------------------

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(api[_-]?key|secret|password|token|bearer)\s*[=:：]\s*\S{8,}", re.IGNORECASE),
    re.compile(r"(sk-[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9\-_.]{16,})"),
    re.compile(r"(ENCRYPTION_KEK|JWT_SECRET|LLM_API_KEY)\s*[=:：]", re.IGNORECASE),
)

# ---------------------------------------------------------------------------
# 输出侧：内网地址/工具调用形态（③）
# ---------------------------------------------------------------------------

INTERNAL_ADDR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"http(s)?://(127\.0\.0\.1|localhost|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)"
    ),
    re.compile(
        r"\b(qdrant|redis|minio|postgres(ql)?|arq|tei|clamav|mineru)\s*[=:：@]", re.IGNORECASE
    ),
    re.compile(r"postgresql?://\S+|redis://\S+"),
)

# ---------------------------------------------------------------------------
# 输出侧：XSS/注入载荷（④）
# ---------------------------------------------------------------------------

XSS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"<\s*script", re.IGNORECASE),
    re.compile(r"javascript\s*:", re.IGNORECASE),
    re.compile(r"\bon(error|load|click|mouseover)\s*=", re.IGNORECASE),
    re.compile(r"<\s*iframe", re.IGNORECASE),
    re.compile(r"<\s*img[^>]+\bon\w+\s*=", re.IGNORECASE),
    re.compile(r"(union\s+select|drop\s+table|--\s*$)", re.IGNORECASE),
)

# ---------------------------------------------------------------------------
# 输出侧：跨租/他人 PII（①）——身份证模式（高精度）；手机号不放行判断：
# KB 合法资料含联系电话（招生办/值班电话），全局拦截误伤率高，决策=不拦手机号
# （TD-338 同款「口径回写」逻辑：宽严取舍在 rules.py 注释留痕）。
# ---------------------------------------------------------------------------

PII_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{6}(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b"),
)

# 伪造引用标记（⑤）：[n] 形态，n 与证据数比对由调用方完成
REF_MARKER = re.compile(r"\[(\d+)\]")

# ---------------------------------------------------------------------------
# 哨兵缓冲切片参数（D1）
# ---------------------------------------------------------------------------

SENTENCE_ENDINGS = "。！？；\n"
MAX_SLICE_CHARS = 120
