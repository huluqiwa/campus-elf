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
    re.compile(r"忽略(之前|上面|以上|前面|先前|所有|全部)?(的)?(所有|全部)?(的)?(指令|提示|规则|设定)", re.IGNORECASE),
    re.compile(r"(ignore|disregard|forget)\s+(all\s+)?(previous|prior|above)\s+(instructions|rules|prompts)", re.IGNORECASE),
    re.compile(r"(system\s*prompt|系统提示词?|你的?(内置)?(指令|设定|提示语))\s*(是|什么|给我|看看|展示|输出|泄露)?", re.IGNORECASE),
    re.compile(r"(告诉我|透露|展示|输出|打印)(你的)?(系统提示词?|内部指令|初始指令|真实身份设定)", re.IGNORECASE),
    re.compile(r"(reveal|show|print|repeat)\s+(your\s+)?(system\s+prompt|instructions)", re.IGNORECASE),
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
    re.compile(r"(制作|制造|合成|组装)(炸弹|爆炸物|炸药|毒品|病毒|武器|枪支)", re.IGNORECASE),
    re.compile(r"(how\s+to\s+)?(make|build|synthesize)\s+(a\s+)?(bomb|explosive|drug|virus|weapon)", re.IGNORECASE),
    re.compile(r"(杀人|谋杀|下毒|投毒)(方法|教程|指南)"),
    re.compile(r"(毒品|冰毒|海洛因)(制作|提炼|合成)"),
)

# ---------------------------------------------------------------------------
# 自伤/心理危机（A24：支持性话术，宁枉勿纵；severity=high）
# ---------------------------------------------------------------------------

_SELF_HARM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(自杀|自残|自伤|轻生|寻死|想死|不想活|活不下去|结束生命|了结自己)"),
    re.compile(r"(kill\s+myself|suicide|self[-\s]?harm|end\s+my\s+life)", re.IGNORECASE),
)

# ---------------------------------------------------------------------------
# 输出侧：系统提示词/CoT 泄露（输出含模板原文片段）
# ---------------------------------------------------------------------------

PROMPT_LEAK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"你是「?校园陪伴精灵」?[，,]?(根据|作为|提供)"),
    re.compile(r"##\s*(回答规则|红线|意图清单|参考资料|用户问题)"),
    re.compile(r"(我的|我的全部)?(思考过程|内部指令|系统提示|prompt)\s*(是|如下|为)：", re.IGNORECASE),
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
    re.compile(r"http(s)?://(127\.0\.0\.1|localhost|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)"),
    re.compile(r"\b(qdrant|redis|minio|postgres(ql)?|arq|tei|clamav|mineru)\s*[=:：@]", re.IGNORECASE),
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
