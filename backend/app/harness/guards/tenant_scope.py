"""确定性租户范围计划（TD-361）。

本模块只决定哪些文本可以进入路由、提示词和和只读工具；它不替代
Principal/RLS/仓储/Qdrant 的硬边界，也不保存命中原文。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from app.harness.contracts import GuardResult

# 零宽/不可见格式字符（Unicode 类别 Cf 的常见成员）。从网页或富文本复制来的文本
# 常带这些字符（“北大的学生名单”），它们不在 \s 的范围内：去空白口径看不见它们，
# 于是“北\u200b大的学生名单”“他\u200b们的成绩”这类纯外校/他人隐私索取能绕过早拒
# （十轮审查 Major 1）。判定前统一剥离，剥离必须早于校名遮蔽，否则认证租户名
# “北京大学”与正文“北京\u200b大学”对不上，本校自指会被误判成外校。
_INVISIBLE_FORMAT_RE = re.compile(
    r"[\u00ad\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff]"
)

TENANT_SCOPE_BOUNDARY = (
    "当前账号只能使用本校授权范围内的知识库和功能，我无法读取或确认本校范围之外的资料。"
    "你可以继续询问本校规则，或通过目标学校的官方渠道查询。"
)
QUOTED_CONTENT_REDACTION = "【已移除的敏感引用】"
TENANT_SCOPE_SENSITIVE_BOUNDARY = (
    "我不能通过聊天绕过授权、用途说明和审计流程查看学生或其他人员的敏感信息。"
    "请使用受控管理入口并联系有权限的管理员。"
)
# 只有身份声称、没有实质问题：给澄清话术，不按跨租拒绝，也不记跨租高危命中。
TENANT_SCOPE_IDENTITY_CLAIM = (
    "身份声明不会改变账号的权限范围，我只能按你当前账号所属学校回答。"
    "请直接说明你想查询本校的什么内容。"
)
TENANT_SCOPE_RESTRICTION_ACK = (
    "好的，我不会读取、索取或提供本校范围之外的学生数据和其他租户资料。"
    "如需本校制度或功能帮助，可以继续告诉我。"
)
# 助手自己的固定话术：回灌历史时逐字相等即原样保留（十轮审查 m2）
_SYSTEM_SCRIPTS = (
    TENANT_SCOPE_SENSITIVE_BOUNDARY,
    TENANT_SCOPE_BOUNDARY,
    TENANT_SCOPE_IDENTITY_CLAIM,
)

_EXTERNAL_SCOPE_RE = re.compile(
    r"(?:其他|别的|另一(?:所|个|间)?)(?:学校|租户|院校|校区|单位)"
    r"|别校|各校|各院校|另外一所"
    r"|一所学校|一个学校|一间学校|隔壁|外校|他校|跨学校|跨校"
    r"|他们学校|她们学校|它们学校"
    r"|(?:学校|租户)\s*B\b|B\s*学校|B\s*校|某校|某某学校|某(?:大学|学院|中学|小学|职校)"
    r"|(?:所有|全部|全平台|平台内所有|平台全部).{0,8}(?:学校|租户|tenant)"
    r"|(?:所有|全部).{0,8}collection"
    r"|\btenant(?:[_-]?id|[_-]?other|[_-]?[a-z0-9]{1,16})\b"
    r"|(?:租户|tenant)\s*[=:：]?\s*[A-Za-z0-9]{1,16}"
    r"|(?:tenant|租户)[_-][A-Za-z0-9]{1,16}",
    re.IGNORECASE,
)
# 显式字母校码属于可指称的外校实体；认证租户恰好使用同一单字母校码时，
# 在判定前只遮蔽“该字母 + 学校/校”的明确自指形态。
_LETTER_SCHOOL_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]\s*(?:学校|校)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_EXTERNAL_REQUEST_TARGET_RE = re.compile(
    rf"(?:{_EXTERNAL_SCOPE_RE.pattern})|(?:{_LETTER_SCHOOL_RE.pattern})",
    re.IGNORECASE,
)
# 防御性陈述的否定锚点。旧写法只认“不要/别/不能”等裸词，且窗口高达 24 字，
# “能不能给我其他学校的学生名单”“为什么不能给我其他学校的成绩”这类能力询问
# 会被当成防御说明放行（十二轮审查 M2）。
# 十三轮审查 M1：裸“别”没有词边界，“分别/区别/别人/别的”里的“别”也会命中，
# 于是“分别给我其他学校的学生名单”这类纯外校索取被整条豁免（且不落任何审计）。
# 裸“别”现在必须**独立成词**（句首或礼貌前缀后）且**直接支配一个访问动作动词**，
# 才算“不要/别碰”的禁止语气；其余位置一律不豁免。
_DEFENSIVE_BIE_VERB = (
    r"给|发|拿|查|看|要|提|供|导|搜|列|告|确|传|调|复|整|显|泄|透|转|将|把|用|"
    r"上|下|访|读|获|取"
)
_DEFENSIVE_NEGATION_PATTERN = (
    r"(?:不要|请勿|无需|不必|不需要|禁止|严禁|不得|不应|不应该|不能|不可|"
    r"不可以|不允许|并非|不可能|不会|没法|无法|不给|"
    r"(?:(?<![\u4e00-\u9fff])别|(?:请|千万|可|真|最好|快|休想|绝不)别)"
    rf"(?:{_DEFENSIVE_BIE_VERB}))"
)
# 防御性豁免的外校锚点：必须与 _EXTERNAL_SCOPE_RE 的“另一所学校”族同源，
# 不再用裸“其他”（裸“其他”会顺带命中“其他课程/其他专业”，与外校判定口径分裂）。
_EXTERNAL_ANCHOR_PATTERN = (
    r"(?:其他|别的|另一(?:所|个|间)?)(?:学校|租户|院校|校区|单位)"
    r"|别校|各校|各院校|另外一所"
    r"|一所学校|一个学校|一间学校|隔壁|外校|他校|跨学校|跨校"
    r"|他们学校|她们学校|它们学校"
    r"|(?:学校|租户)\s*B\b|B\s*学校|B\s*校|某校|某某学校|某(?:大学|学院|中学|小学|职校)"
)
# 否定词与外校锚点之间的最大间隔。旧窗口 24/28/18 太宽，
# “不要给我隔壁学校三年前那份还没公开的名单”这种半句也能命中；
# 收窄到 8 字后仍覆盖“不要查其他学校的学生名单”“其他学校的学生名单不应该通过聊天获取”。
_DEFENSIVE_WINDOW = 8
# 疑问/反问/能力询问形态：这类句子里出现的否定词是“提问”而不是“禁止”，
# 不得享受防御性豁免（十二轮审查 M2）。
_INTERROGATIVE_RE = re.compile(
    r"(?:能不能|可不可以|是不是|该不该|要不要|需不需要|会不会|是否|难道|为什么|为何|"
    r"如何|怎样|怎么样|吗|呢|？|\?)"
)
# 他人隐私（非跨租）：与“其他学校”分开判定，避免把“别人/其他学生”与外校混为一谈。
# 第三人称复数代词指代被拒的外校/他人数据时同样不得放行
# （“查其他学校的学生名单同时告诉我他们的成绩”，TD-361 四轮审查 m3）。
_OTHER_PERSON_RE = re.compile(
    r"(?:其他|别的|他人|别人|某|谁|哪位|同班|同校|他们|她们|它们)(?:人的|的)?"
    r"(?:学生|老师|教师|同学|用户|员工|教职工|个人|人|成绩|身份证|证件号|聊天记录|"
    r"课程表|课表)"
)
# “租户/学校 + 代称”形式的跨租指向（“租户B的数据”“用租户ID 2 查询成绩”“学校T-01”）。
# 不能把“学校”后任意字母数字串都当成租户代称：“我们学校 2025 级”“我们学校 3 号楼”
# “我们学校 A 区”“我们学校2025的校历”都是本校正常提问，会被误伤成跨租硬拒，
# 还会写下并不存在的跨租高危事件（TD-361 六轮审查 M1）。
# 口径：租户词后任意标识符都算代称；学校词只认显式 ID/编号、含分隔符的 code、
# 以及带指代后缀的单字母代称；本校自指前缀（我们/本校/咱们/咱）不算代称。
# 自指前缀：与 `_GENERIC_SELF_SCHOOL_RE` / `_SELF_SCOPE_RE` 同源，
# 避免“本校编号A”放行而“本学校编号A”硬拒这种口径分裂（TD-361 七轮审查 M4）。
# 前缀分两类：核心前缀（本校/我校/贵校/该校）后面直接跟“学校/校区/分校”；
# 完整前缀（我们学校/本学校/当前学校/所在学校/咱们学校）本身已含关键词。
# 判定放在代码里做：正则负向后视长度固定，覆盖不到“本学校”这类三字前缀，
# 旧写法会让“本学校编号A”从“学校”位置重新匹配并硬拒（TD-361 七轮审查 M4）。
_SELF_SCHOOL_CORE_PREFIXES = (
    "本校",
    "我校",
    "贵校",
    "该校",
)
_SELF_SCHOOL_FULL_PREFIXES = (
    "本学校",
    "当前学校",
    "我们学校",
    "咱们学校",
    "所在学校",
)
_TENANT_ID_RE = re.compile(
    r"(?:租户|tenant)\s*[=:：]?\s*[A-Za-z0-9]{1,16}(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_SCHOOL_LABEL_RE = re.compile(
    r"(?P<keyword>学校|校区|分校)\s*(?:"
    r"(?:ID|id|编号|代码|Code|code|No|no)\s*[=:：]?\s*[A-Za-z0-9]{1,16}"
    r"|[A-Za-z0-9]*[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+"
    r"|[A-Za-z](?![A-Za-z0-9])(?=(?:的|是|有|属于|叫|名|号|资料|数据|信息|名单|成绩|记录|学生|老师)"
    r"|[，。！？；、,!?;:]|$)"
    r")",
    re.IGNORECASE,
)
_BYPASS_RE = re.compile(
    r"(忽略|绕过|突破|无视|解除|关闭|去掉|跳过).{0,24}(租户|学校范围|隔离|限制|权限)"
    r"|(所有租户|全部租户|全平台|跨租|tenant_id.{0,12}(所有|全部)"
    r"|(?:SQL|sql).{0,18}(所有|全部).{0,18}(tenant|租户|数据)"
    r"|(?:qdrant|collection).{0,24}(?:tenant|租户|other)"
    r"|(?:tenant|租户).{0,24}(?:qdrant|collection)"
    r"|(?:绕过|突破|跳过).{0,8}(?:方法|办法|技巧|途径|方案))",
    re.IGNORECASE,
)
# 直接针对租户/隔离/权限的绕过：不依赖外部实体即可判定。
_BYPASS_SCOPE_RE = re.compile(
    r"(忽略|绕过|突破|无视|解除|关闭|去掉|跳过).{0,24}(租户|学校范围|隔离|校区)",
    re.IGNORECASE,
)
# 身份冒充声称：命中即不得进入模型上下文。
# 词边界不跨标点且尽量短，便于只摘掉“声称本体”而不误伤后面的正常问题。
# 自称（我是/现在是/本人是/扮演）命中角色词即算声称；
# “用/以”只有同时带“身份/权限”才算声称，否则“以便查询老师评语”
# “用老师的方法查本校成绩”这类正常提问会被误摘（TD-361 四轮审查 m4）。
_ROLE_WORD_PATTERN = (
    r"(?:SaaS|平台|超级管理员|超管|校方|校领导|校管理员|管理员|员工|老师)"
)
_PLATFORM_CLAIM_RE = re.compile(
    r"(?:"
    # 强自称：覆盖“假装是平台管理员”“我是校方超管”（九轮审查 Major 1：
    # 这一族高频冒充句式原话会进模型上下文）。
    r"(?:我是|现在是|本人是|我系|扮演|假装)"
    r"[^，。！？；、,!?;：:\s]{0,12}?"
    + _ROLE_WORD_PATTERN
    + r"(?:身份|权限)?"
    r"|"
    # 把（自己/本人）当成某角色：“请把我当成管理员”“视我为校方超管”“我被当作超管”。
    r"(?:(?:把(?:我|本人|我自己)|我)(?:被)?(?:当成|当作|视为|视作|看成|看做)|"
    r"视我为|当我为)"
    r"[^，。！？；、,!?;：:\s]{0,12}?"
    + _ROLE_WORD_PATTERN
    + r"(?:身份|权限)?"
    r"|"
    r"(?:用|以(?!及))"
    r"[^，。！？；、,!?;：:\s]{0,12}?"
    + _ROLE_WORD_PATTERN
    + r"(?:身份|权限|账号|帐号|账户|帐户)"
    r"|"
    # 任职/主管类声明（“本人在校方任职”“我系校方超管”）同样是身份冒充，
    # 不得原样进模型上下文（TD-361 六轮审查 m3）。
    r"(?:本人|我)(?:在|于)(?:校方|本校|贵校|当前学校|本学校|我们学校|咱们学校|"
    r"所在学校|学校|校区|学院)(?:任职|工作|负责|主管|管理)"
    r")",
    re.IGNORECASE,
)
# 弱声称：“其实是/名为/算是 + 角色词”。这类句式只有带第一人称主语
# （“我其实是管理员”“本人算是校方超管”）或整句就是这句声称（“名为校方超管”）
# 才是自我声称；否则“这算是校方的安排吗”“名为校方超管的那个人是谁”是普通问句，
# 误判会把正常问题摘成“的安排吗”这种残句并写一条假身份事件（十轮审查 m1）。
_WEAK_CLAIM_RE = re.compile(
    r"(?:其实是|名为|算是)"
    r"[^，。！？；、,!?;：:\s]{0,12}?"
    + _ROLE_WORD_PATTERN
    + r"(?:身份|权限)?"
)
_FIRST_PERSON_MARKERS = ("我", "本人", "咱", "俺", "自己")


def _has_first_person_before(text: str, start: int) -> bool:
    """声称前缀之前是否有人称主语（“我其实是…”“本人算是…”）。"""

    prefix = text[:start]
    return any(prefix.endswith(marker) for marker in _FIRST_PERSON_MARKERS)


def _claim_covers_clause(text: str, end: int) -> bool:
    """声称之后是否只剩标点（整句就是这句声称）。"""

    return not text[end:].strip(" \t\r\n。！？；，,.!?;:")


def _claim_end(text: str, match: re.Match[str]) -> int:
    """声称本体的结束位置：紧跟其后的角色词（“超管”）也算声称本体。"""

    role = _PLATFORM_CLAIM_ROLE_RE.match(text, match.end())
    return role.end() if role is not None else match.end()


def _has_identity_claim(text: str) -> bool:
    """文本里是否有身份声称。

    强自称（我是/现在是/本人是/我系/扮演/假装/把我当成/用身份/任职）直接算；
    弱声称（其实是/名为/算是）必须带第一人称主语或整句就是这句声称，
    否则“这算是校方的安排吗”这类普通问句会被误判（十轮审查 m1）。
    """

    if _PLATFORM_CLAIM_RE.search(text) is not None:
        return True
    for match in _WEAK_CLAIM_RE.finditer(text):
        if _has_first_person_before(text, match.start()) or _claim_covers_clause(
            text, _claim_end(text, match)
        ):
            return True
    return False


# 身份声称后面紧跟的角色词（“扮演平台|管理员”“本人是平台|超管”）也要一并摘除，
# 否则上下文里会剩下“管理员/超管”这种残片。
_PLATFORM_CLAIM_ROLE_RE = re.compile(
    _ROLE_WORD_PATTERN + r"(?:身份|权限)?",
    re.IGNORECASE,
)
_DATA_OBJECT_RE = re.compile(
    r"(知识库|文档|学生|成绩|审批|反馈|对话|聊天|消息|审计|身份证|证件号|名单|记录|"
    r"测试集|数据|资料|内容|政策|制度|结果|collection|tenant|QDrant|SQL|"
    r"校历|图书馆|社团|录取|分数线|招生|课表|课程表|宿舍|食堂|开放时间|联系方式|官网|排名|"
    r"老师|教师|教职工|手机号|手机号码|电话号码|联系电话|邮箱|电子邮箱|住址|家庭地址|"
    r"出生日期|生日|姓名|学号|工号|联系人)",
    re.IGNORECASE,
)
_ACCESS_ACTION_RE = re.compile(
    r"(查|搜索|检索|查看|看|读取|获取|发|给|导出|复制|列出|告诉|确认|调用|合并|"
    r"查询|多少|名单|绕过|方法|SQL|collection|QDrant|怎么走|怎么去|位置|路线)",
    re.IGNORECASE,
)
_SENSITIVE_RE = re.compile(
    r"(身份证|证件号|学生|成绩|审批|反馈|对话|聊天|消息|审计|名单|记录|个人资料|敏感|"
    r"课程表|课表|手机号|手机号码|电话号码|联系电话|联系方式|邮箱|电子邮箱|住址|"
    r"家庭地址|出生日期|生日|姓名|学号|工号|联系人)",
    re.IGNORECASE,
)
_QUOTED_RE = re.compile(
    r'"[^"]*"|“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』',
    re.DOTALL,
)
# 校名类型词：中专/中职/普高/幼儿园/分校/校区同样是学校实体形态，
# 漏掉会让“海州中专的学生名单发我”这类请求放行（TD-361 四轮审查 m2）。
_SCHOOL_TYPE_SUFFIX = (
    r"职业技术学院|职业学校|大学|学院|学校|中学|小学|高中|职校"
    r"|附中|附小|初中|职中|职高|技校|职教|中专|中职|普高|幼儿园|分校|校区"
    # 序号型后缀（一中/二中/一小/二小）也是最常见的校名简称形态：
    # 漏掉会让“海州一中的学生名单发我”这类纯外校请求进检索（TD-361 六轮审查 M2）。
    r"|[一二三四五六七八九十]{1,3}(?:中|小)|[0-9]{1,3}(?:中|小)"
)
_SCHOOL_MENTION_RE = re.compile(
    r"(?:[\u4e00-\u9fff]{2,32}(?:" + _SCHOOL_TYPE_SUFFIX + r")"
    r"|(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]{0,7}\s*校(?![区部]))",
    re.IGNORECASE,
)
_SCHOOL_SUFFIX_RE = re.compile(r"(?:" + _SCHOOL_TYPE_SUFFIX + r")")
# 具名外校的类型词位置（学校/大学/一中/附小…）：防御性豁免的距离一律量到
# 类型词边界。旧写法直接用 _SCHOOL_MENTION_RE 的贪婪中文前缀，
# 否定词落在整段中文的任意位置都算“在校名之前”，窗口形同虚设
# （十二轮审查 Major 2）。
_SCHOOL_TYPE_WORD_RE = re.compile(
    r"(?:" + _SCHOOL_TYPE_SUFFIX + r")"
    r"|(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]{0,7}\s*校(?![\u533a\u90e8])"
)
# 高校简称（北大/清华/复旦/…）与序号型后缀同属最常见的校名简称形态：
# 漏掉会让“北大的学生名单”这类纯外校请求进检索与模型（TD-361 六轮审查 M2；TD-376 简称批次）。
# 只认带学校语境后缀的简称，避免“人大会议”“重大活动”这类普通词被当成校名。
# 简称 → 唯一可确认的全名候选：只有一个候选时才可能指代本校（认证租户校名包含该全名
# 才算本校自指），否则按外校处理。空元组表示含义不唯一（“南大”可能是南京大学或
# 南昌大学，“中大”可能是中山大学或中科大），无法确认指向本校 → 一律按外校
# （TD-361 七轮审查 M1：子序列自指是漏拒口子）。
_SCHOOL_ABBREVIATION_ALIASES: dict[str, tuple[str, ...]] = {
    "北大": ("北京大学",),
    "北航": ("北京航空航天大学",),
    "北科": ("北京科技大学",),
    "北科大": ("北京科技大学",),
    "北理": ("北京理工大学",),
    "北理工": ("北京理工大学",),
    "北师": ("北京师范大学",),
    "北师大": ("北京师范大学",),
    "北邮": ("北京邮电大学",),
    "北外": ("北京外国语大学",),
    "清华": ("清华大学",),
    "复旦": ("复旦大学",),
    "上交": ("上海交通大学",),
    "同济": ("同济大学",),
    "西电": ("西安电子科技大学",),
    "西交": ("西安交通大学",),
    "浙大": ("浙江大学",),
    "天大": ("天津大学",),
    "武大": ("武汉大学",),
    "川大": ("四川大学",),
    "吉大": ("吉林大学",),
    "厦大": ("厦门大学",),
    "人大": ("中国人民大学",),
    "中科大": ("中国科学技术大学",),
    "电子科大": ("电子科技大学",),
    "华东师大": ("华东师范大学",),
    "华东理工": ("华东理工大学",),
    "华南师大": ("华南师范大学",),
    "华南理工": ("华南理工大学",),
    "南开": ("南开大学",),
    "交大": (),
    "南大": (),
    "中大": (),
}
# 这些简称本身是常用词（“上交”是动词），后面跟“的”时不能直接当成校名指向，
# 必须命中学校对象语境（“上交的作业”是本校作业，不是上交大学）。
_SCHOOL_ABBREV_COMMON_WORD = frozenset({"上交"})
# 形态尾词：与别名表互补，覆盖表里没有的简称形态（“北理工”“华东师大”“电子科大”）。
# 同样必须带学校语境后缀才算外校实体。
_SCHOOL_ABBREV_TAILS = (
    "理工",
    "师范",
    "师大",
    "科大",
    "财经",
    "政法",
    "海洋",
    "地质",
    "矿业",
    "石油",
    "化工",
    "电力",
    "航空",
    "航天",
    "外语",
    "医科",
    "农林",
)
# 说明：民族/科技/工业/建筑/商务/金融/农业 是通用词，尾词兜底会把
# “少数民族学生的名单”“生物科技专业学生的名单”“华为科技的名单”误拒成本校外校，
# 并写下并不存在的跨租高危事件（八轮审查 M2）；这类全称（如“中央民族大学”）
# 由 `_has_named_school` 的类型词规则覆盖，不靠尾词兜底。
_SCHOOL_ABBREV_TAIL_RE = re.compile(
    r"([\u4e00-\u9fff]{1,2})(?:" + "|".join(_SCHOOL_ABBREV_TAILS) + r")"
)
# 学校语境对象词表：简称后面必须出现其中之一，才算“在问这所学校的什么”。
# 别名档与形态尾词档共用同一张表，避免“北科大的资料”早拒而“南科大的资料”放行
# 这种同族形态口径分裂（八轮审查 m3）。
_SCHOOL_ABBREV_OBJECT_WORDS = (
    "学生",
    "考生",
    "教职工",
    "老师",
    "员工",
    "名单",
    "成绩",
    "档案",
    "校历",
    "图书馆",
    "分数线",
    "录取",
    "招生",
    "食堂",
    "宿舍",
    "社团",
    "公告",
    "官网",
    "联系方式",
    "地址",
    "电话",
    "概况",
    "简介",
    "办公室",
    "排名",
    "资料",
    "数据",
    "记录",
    "内容",
    "文档",
    "知识库",
    "历史",
)
# 学校语境闸门：简称后面必须接学校对象，否则“人大会议”“重大活动”“上交作业”
# 这类普通词会被当成校名（TD-361 六轮审查 M2、七轮审查 M2）。
# 宽松档：“的”后接什么都算指向该校（简称识别层；是否拒绝还要看有没有本校信息
# 对象或访问动作——“北大的校歌”本身不含信息对象，不构成跨租索取），
# 与第六轮口径一致——纯外校请求宁可早拒；“的”与对象之间允许夹最多 4 个汉字
# （“北大的在校学生人数”）；对象词可直接紧跟简称（“南科大名单”），
# 否则同族形态会因少一个“的”而被放行（八轮审查 m3 的口径统一）。
_SCHOOL_ABBREV_CONTEXT_RE = re.compile(
    r"^(?:的"
    r"|(?:[\u4e00-\u9fff]{0,4})?(?:" + "|".join(_SCHOOL_ABBREV_OBJECT_WORDS) + r")"
    r")"
)
# 严格档：必须命中学校对象且“的”必须出现在对象词之前。只用于简称本身是常用词的
# 情形（“上交”是动词：“上交作业/上交名单”是本校动作，“上交的学生名单”才是外校），
# 形态尾词档不用严格档——尾词是校名形状，缺“的”仍是外校实体。
_SCHOOL_ABBREV_OBJECT_CONTEXT_RE = re.compile(
    r"^的(?:[\u4e00-\u9fff]{0,4})?(?:" + "|".join(_SCHOOL_ABBREV_OBJECT_WORDS) + r")"
)
# 别名后接通用机构名或行政区划时不是校名实体（“同济医院的地址”“南开区的图书馆”）。
_SCHOOL_ABBREV_NON_SCHOOL_SUFFIX_RE = re.compile(
    r"^(?:医院|书店|超市|商场|园区|区|店|馆|中心|车站|机场|地铁|路|街|村|镇|县|市|省|"
    r"部|局|厅|社|台|网|吧|城|港)"
)
# 形态尾词的前缀不得含功能词，否则“我们的科技招生”里的“们的科技”会被当成校名简称。
_SCHOOL_ABBREV_TAIL_PREFIX_BLOCKLIST = frozenset(
    "的了是在就把给从和与及并且我你他她咱这那每某所个们各少多好新老"
)
# 本校自指标记：泛称“本校/我们学校”在判定外部实体时替换成该标记，
# 便于识别“我们学校的XX”这种自指形态（TD-361 七轮审查 M2）。
_SELF_MARK = "\ue000"
# 分校/校区前面是泛称时是“校区”泛指，不是具体外校名（“学校有分校吗”）。
_BRANCH_CAMPUS_SUFFIXES = frozenset({"分校", "校区"})
_GENERIC_SCHOOL_NOUN_RE = re.compile(
    r"(?:学校|学院|大学|中学|小学|高中|职校|职中|职高|技校|附中|附小|初中|"
    r"幼儿园|中专|中职|普高)"
)
# 拉丁校码必须至少含一个字母：纯数字是年份/楼号（“我们学校2025校历”“2025校历是什么”
# 都是本校正常提问，被当拉丁校码会整题硬拒并写下假高危事件（TD-361 七轮审查 M3）。
# “A校区”是校区泛称，不是校码。
_LATIN_SCHOOL_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]{0,7}\s*校(?![区部])", re.IGNORECASE
)
_HAN_TAIL_RE = re.compile(r"[\u4e00-\u9fff]+$")
# 类型词前若以功能词/语气词/指示词收尾，则“学校/大学”是泛称而非专名
# （“这个学校”“被告知学校”“告诉我学校”都不是具体外校名）。
# 只收多字功能短语与纯语气助词：单个汉字既是常用功能字，也可能出现在真实校名里
# （“海中中学”“城外中学”“实事求是中学”），用单字判定会把真实外校名当泛称放行。
_NON_NAME_TAIL_WORDS = (
    # 指示词与限定词（多字）
    "这个", "那个", "这所", "那所", "哪所", "哪家", "哪个", "这些", "那些",
    "这间", "那间", "每所", "各所", "某所", "每个", "各个", "某个", "某些",
    "所有", "全部", "任何", "什么",
    # “目标学校”是泛指（助手边界话术“或通过目标学校的官方渠道查询”），
    # 不能当成专名校名，否则助手自己的话术在历史回灌时会被自己判成外校而截断
    # （九轮审查 m8）。
    "目标",
    # 判断与连接词（多字）
    "不是", "就是", "也是", "而是", "于是", "但是", "可是", "然后", "所以",
    "因为", "因此", "如果", "虽然", "然而", "而且", "并且", "或者", "以及",
    "不过", "只是", "只有", "由于", "关于", "对于", "通过", "按照", "根据",
    "基于",
    # 动作与请求词（多字）
    "被", "被告知", "被告诉", "被要求", "被问到", "被通知",
    "告诉", "告诉我", "告知", "听说", "提到", "说到", "谈到", "问到",
    "介绍", "讲解", "咨询", "询问", "回答", "作答", "回复", "解答", "说明",
    "答复", "引用", "参考", "学习", "访问", "了解", "知道", "需要", "打算",
    "查到", "找到", "看到", "听到", "收到", "属于", "关注", "负责", "面向",
    "针对", "请问", "麻烦", "我们", "咱们", "你们", "他们", "她们",
    # 纯语气助词
    "吗", "呢", "吧", "啊", "呀", "哦", "嗯", "的", "了", "着", "过", "们",
)
# 量词短语（“一所中学”“一名中学生”“多所中学”）：类型词前是量词而不是专名。
# 与 _NON_NAME_TAIL_WORDS 分开维护：量词里的数字是开放的，不能穷举。
_QUANTIFIED_NOUN_RE = re.compile(
    r"(?:[一二两三四五六七八九十百千万零多几半数若干好无]+)"
    r"(?:所|个|名|位|间|家|份|张|片|栋|座|台|套|条|部|本|群|批)"
    r"(?:[一二两三四五六七八九十])?$"
)
# 动作/方位短语（“我在学校医院看病”“想去学校医院”）：类型词前是动作短语而不是专名。
_FUNCTION_TAIL_RE = re.compile(
    r"(?:"
    r"(?:我|你|他|她|它|咱|俺|大家|人们|同学|同事|朋友|家人|家长|老师|医生|护士|患者)"
    r"|(?:想|要|准备|打算|正在|已经|曾|刚刚|刚|正|就|也|还|都|又)"
    r")(?:"
    r"(?:想|要|准备|打算|正在|已经|曾|刚刚|刚|正|就|也|还|都|又)?"
    r"(?:去|到|在|进|回|住|来自|来到|前往|返回|读书|上学)?"
    r")$"
)
# 泛称本校（允许“本校附属中学”“本校第一分校/第一校区”这类校区限定）；
# 遮蔽后才判断是否命中具体外校（TD-361 四轮审查 m2）。
_GENERIC_SELF_SCHOOL_RE = re.compile(
    r"(?:本校|我校|贵校|当前学校|本学校|我们学校|咱们学校|所在学校)"
    r"(?:的)?"
    r"(?:第[一二三四五六七八九十\d]{1,3}(?:分校|校区|附中)?|附属|附设|附中|分校|校区)?"
)
# 自指本校的标记：混合请求的可回答片段必须自包含（TD-361 四轮审查 m3）。
_SELF_SCOPE_RE = re.compile(
    r"(?:本校|我校|贵校|当前学校|本学校|我们学校|咱们学校|所在学校|该校)"
)
# 悬空代词：指代被拒的外校/他人数据，不得单独成为可回答片段。
_DANGLING_PRONOUN_RE = re.compile(r"(?:他们|她们|它们)")
# 外部人员回指即使和“本校”出现在同一片段，也不能证明其数据对象属于本校。
_EXTERNAL_PERSON_REFERENCE_RE = re.compile(
    r"(?:其他|那些|这些|前述|上述|该|相关|那位|这位|此)"
    r"(?:的)?(?:老师|教师|学生|同学|人员)"
    r"|对方|其(?:老师|教师|学生|同学|人员)"
    r"|那个人|那些人|他人|别人|某人|有人|旁人"
    r"|(?<![其])[他她](?:的)?(?:老师|教师|学生|同学|人员)?"
)
# MIXED 片段里只要人员角色和个人属性共现，就可能是省略主语继承了前句的外校
# 对象。例如“本校校历 + 老师手机号”无法仅凭一个前缀证明数据对象属于本校；
# 这类片段整组 fail-closed，普通单租户问题不受此闸门影响。
_MIXED_PERSONAL_ATTRIBUTE_RE = re.compile(
    r"(?:老师|教师|教授|导师|辅导员|导员|班主任|学生|同学|教职工|员工|"
    r"人员|用户|家长|室友|宿管|保安|校长|院长|领导|负责人|联系人)"
    r".{0,12}(?:手机号|手机号码|电话号码|办公电话|电话|联系方式|邮箱|电子邮箱|"
    r"微信|QQ|二维码|照片|影像|社交账号|账号|住址|家庭地址|地址|身份证|"
    r"证件号|学号|工号|成绩|名单|档案|聊天记录|定位)"
    r"|(?:手机号|手机号码|电话号码|办公电话|电话|联系方式|邮箱|电子邮箱|"
    r"微信|QQ|二维码|照片|影像|社交账号|账号|住址|家庭地址|地址|身份证|"
    r"证件号|学号|工号|成绩|名单|档案|聊天记录|定位)"
    r".{0,12}(?:老师|教师|教授|导师|辅导员|导员|班主任|学生|同学|教职工|"
    r"员工|人员|用户|家长|室友|宿管|保安|校长|院长|领导|负责人|联系人)",
    re.IGNORECASE,
)
# 校名前面的自指修饰（“本校第一中学”“我们学校的第一中学”）仍算本校。
_SELF_NAME_TAIL_RE = re.compile(
    r"(?:本校|我校|贵校|当前学校|本学校|我们学校|咱们学校|所在学校)的?$"
)
# 行政区划修饰（“海州市第一中学”）仍算本校。
_ADMIN_NAME_TAIL = ("市", "区", "县", "州", "盟")
# 请求礼貌语前缀：剥离后才能区分“帮我查学校”（泛称）与“帮我查北京大学”（外校）。
_REQUEST_PREFIX_RE = re.compile(
    r"^(?:请问|请|麻烦|帮我|帮忙|替我|给我|我想知道|想知道|我想要|我想|我要|我需要|需要|"
    r"知道|查询|查一下|查一查|查查|查|搜索|检索|看一下|看看|看下|介绍|讲解|说明|"
    r"告诉我|告诉|说说|把|将|从|向|跟|和|与|该)+"
)
# 全大写拉丁校名缩写（MIT/UCLA…）；只有再接“的学生名单/成绩单”才算具体外校实体。
_LATIN_ACADEMIC_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,8})(?![A-Za-z0-9])")
_LATIN_NON_SCHOOL_TOKENS = frozenset(
    {
        "AI", "API", "CSV", "DNS", "FAQ", "HR", "HTML", "HTTP", "HTTPS", "ID", "IP",
        "IT", "JSON", "JWT", "MFA", "OA", "PDF", "SQL", "SSO", "TCP", "TLS", "UI",
        "URL", "UX", "XML",
    }
)
_LATIN_SCHOOL_OBJECT_RE = re.compile(r"^的?(?:学生|成绩|名单|档案|考生|教职工|老师|员工)")
_CLAUSE_SPLIT_RE = re.compile(
    r"(?<=[。！？；!?;，,\n])"
    r"|(?=(?:另外|同时|然后|以及|并且|顺便|还要|再有)(?:把|请|查|给|发|列|导|告|确|调|合))"
    r"|(?=并(?=把|查|给|发|列|导|告|确|调|合))"
    r"|(?=再(?=把|请|查|给|发|列|导|告|确|调|合))"
    r"|(?=(?:(?:再|并|然后|顺便|同时|以及|而且|且|最后|接着|随后|另外|还要|再有)"
    r"(?:来|用来|用于)?|(?:来|用来|用于))(?:回答|作答|回复|解答|答复|说明))"
)
_TRIM_RE = re.compile(
    r"^[\s\u3002\uff01\uff1f\uff1b\uff0c\uff1a;:!?,\u2026]+|[\s\u3002\uff01\uff1f\uff1b\uff0c\uff1a;:!?,\u2026]+$"
)
_LEADING_CONNECTOR_RE = re.compile(r"^(?:另外|同时|然后|以及|并且|顺便|还要|再有|并|另外请|同时请)")
# 回答类动词前的结构切点：单句混合（“…再回答本校的…”）要在动词前切开，
# 不能靠枚举具体句子；同时覆盖 再/并/然后/顺便/同时/以及 + 回答 的连接形式。
_ANSWER_VERB_RE = re.compile(
    r"(?:(?:再|并|然后|顺便|同时|以及|而且|且|最后|接着|随后|另外|还要|再有)\s*)?"
    r"(?:来|用来|用于)?"
    r"(?:回答|作答|回复|解答|答复|说明)"
)
# 回答类动词前的连接词（“再来回答”“用来回答”）要整段剥掉，
# 否则片段会残留“来回答”这种没有实质内容的残片（TD-361 四轮审查 m6）。
_LEADING_ANSWER_CONNECTOR_RE = re.compile(
    r"^(?:再来|再用来|再用于|再|用来|用于|来)(?=(?:回答|作答|回复|解答|答复|说明))"
)
# 安全片段必须含有实质内容：只剩连接词/回答动词的片段（如“来回答”）不是可回答内容，
# 更不能当作检索 query 发给模型。
_FRAGMENT_NOISE_RE = re.compile(
    r"(?:再|并|然后|顺便|同时|以及|而且|且|最后|接着|随后|另外|还要|再有|来|用来|用于|"
    r"回答|作答|回复|解答|答复|说明|一下|请|请问|麻烦|帮我|帮忙|替我|给我|我|"
    r"的|了|着|过|们|吗|呢|吧|啊|呀|哦|嗯)+"
)
_UNSAFE_FRAGMENT_RE = re.compile(r"(?:行不行|分别给(?:我)?|分别发(?:给)?我)")
# 确定性公开比较只接受完整的机构级公开信息句式。这里用整句语法白名单，
# 不枚举手机号、账号、二维码、照片等敏感字段；任何额外分句都无法借用前一句
# 的“其他学校”主语，必须由路由器返回经原文校验的本校 MIXED 安全片段。
_PUBLIC_COMPARISON_PREFIX = r"(?:(?:请|请问|麻烦)?(?:帮我|帮忙|替我)?)?"
_PUBLIC_COMPARISON_SELF = _SELF_SCOPE_RE.pattern
_PUBLIC_COMPARISON_EXTERNAL = (
    rf"(?:{_EXTERNAL_SCOPE_RE.pattern}|{_SCHOOL_MENTION_RE.pattern})"
)
_PUBLIC_COMPARISON_ATTRIBUTE = (
    r"(?:公开(?:办学特色|排名|录取|分数线)?|排名|办学特色|录取|分数线)"
)
_PUBLIC_COMPARISON_TAIL = (
    r"(?:(?:有(?:什么|何))?(?:比较|对比|不同|区别|差别|差异)|(?:如何|怎么样|怎样))?"
)
_PUBLIC_COMPARISON_PATTERNS = (
    re.compile(
        rf"^{_PUBLIC_COMPARISON_PREFIX}(?:比较|对比)"
        rf"{_PUBLIC_COMPARISON_SELF}(?:和|与|跟|同)"
        rf"{_PUBLIC_COMPARISON_EXTERNAL}的?{_PUBLIC_COMPARISON_ATTRIBUTE}"
        rf"{_PUBLIC_COMPARISON_TAIL}$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^{_PUBLIC_COMPARISON_PREFIX}{_PUBLIC_COMPARISON_SELF}"
        rf"(?:和|与|跟|同){_PUBLIC_COMPARISON_EXTERNAL}的?"
        rf"{_PUBLIC_COMPARISON_ATTRIBUTE}{_PUBLIC_COMPARISON_TAIL}$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^{_PUBLIC_COMPARISON_PREFIX}{_PUBLIC_COMPARISON_SELF}"
        rf"(?:和|与|跟|同){_PUBLIC_COMPARISON_EXTERNAL}的?"
        rf"(?:比较|对比)的?{_PUBLIC_COMPARISON_ATTRIBUTE}$",
        re.IGNORECASE,
    ),
)



class TenantScopeMode(StrEnum):
    """护栏内部状态；v3 的语义结论来自 scope-aware router。"""

    ALLOW = "allow"
    MIXED = "mixed"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class TenantScopePlan:
    """只含规则码与安全片段的内部计划；不保存被拒原文。"""

    mode: TenantScopeMode
    safe_text: str | None
    rule_codes: tuple[str, ...]
    blocked_count: int


@dataclass(frozen=True, slots=True)
class TenantScopeEvidence:
    """确定性文本证据；只描述“像不像范围请求”，不裁决自然语言意图。"""

    external_scope: bool
    data_object: bool
    access_action: bool
    sensitive_object: bool
    other_person: bool
    bypass: bool
    direct_external_request: bool
    direct_other_person_request: bool

    @property
    def external_access_candidate(self) -> bool:
        """是否需要进一步做语义范围判定。"""

        return self.external_scope and (self.data_object or self.access_action)


_REQUEST_BEFORE_CUE_RE = re.compile(
    r"(?:给(?:我|我一份|我一下)?|发给(?:我)?|发送给(?:我)?|提供给(?:我)?|"
    r"告诉我|发我|帮我(?:查|查询|找|看看|看一下|获取|导出|列出|整理)|"
    r"替我(?:查|查询|找|看看|看一下|获取|导出|列出|整理)|"
    r"列出|导出|查询|搜索|检索|读取|获取|调取|查看|分析|整理|"
    r"查(?:一下|一查)?|复制|合并|汇总|确认)"
)
_REQUEST_AFTER_CUE_RE = re.compile(
    r"(?:发(?:给)?我|发我|给我|提供(?:给)?我|发送给我|告诉我|"
    r"发一下|查一下|找一下|列出来|整理给我|整理一下|导出来|导出一份|"
    r"查询|搜索|检索|读取|获取|调取|查看|导出|复制|合并|汇总|确认|查(?:一下|一查)?|呢)"
)
_NEGATED_REQUEST_TAIL_RE = re.compile(
    r"(?:不要|请勿|勿|无需|不必|不需要|禁止|严禁|不得|不应|"
    r"不应该|不能|不可|不可以|不允许|不会|没法|无法|不给|"
    r"(?:(?<![\u4e00-\u9fff])别|(?:请|千万|可|真|最好|快|休想|绝不|都|咱|咱们|大家|谁都)别))"
    r".{0,6}$"
)
_A_NOT_A_BEFORE_RE = re.compile(r"[\u4e00-\u9fff]不[\u4e00-\u9fff]")
_DIRECT_SCOPE_LOOKBEHIND = 20
_DIRECT_SCOPE_LOOKAHEAD = 64


def inspect_tenant_scope_evidence(
    message: str,
    *,
    school_names: Iterable[str] = (),
) -> TenantScopeEvidence:
    """提取范围请求证据，不作 allow/block 语义裁决。

    这里只识别高置信显式索取所需的词元关系。反问、禁止、嵌套否定、公开比较
    和混合句由 scope-aware router 判断；任何方向不明确的表达不能在这里结案。
    """

    normalized_school_names = _school_name_set(school_names)
    canonical = _project(_mask_school_names(message, normalized_school_names))
    canonical = _mask_authenticated_school_code_labels(canonical, normalized_school_names)
    stripped = re.sub(r"\s+", "", canonical)
    external = (
        bool(_EXTERNAL_SCOPE_RE.search(canonical))
        or _has_tenant_label(canonical)
        or _LETTER_SCHOOL_RE.search(canonical) is not None
        or _has_external_school_entity(canonical, normalized_school_names)
    )
    data_object = bool(_DATA_OBJECT_RE.search(canonical)) or bool(
        _DATA_OBJECT_RE.search(stripped)
    )
    access_action = bool(_ACCESS_ACTION_RE.search(canonical)) or bool(
        _ACCESS_ACTION_RE.search(stripped)
    )
    sensitive_object = bool(_SENSITIVE_RE.search(canonical)) or bool(
        _SENSITIVE_RE.search(stripped)
    )
    other_person = bool(_OTHER_PERSON_RE.search(canonical)) or bool(
        _OTHER_PERSON_RE.search(stripped)
    )
    bypass = bool(_BYPASS_RE.search(canonical)) or bool(_BYPASS_SCOPE_RE.search(canonical))
    return TenantScopeEvidence(
        external_scope=external,
        data_object=data_object,
        access_action=access_action,
        sensitive_object=sensitive_object,
        other_person=other_person,
        bypass=bypass,
        direct_external_request=external
        and (data_object or access_action)
        and not _has_independent_self_scope_clause(canonical, normalized_school_names)
        and _has_high_confidence_request(canonical, _EXTERNAL_REQUEST_TARGET_RE),
        direct_other_person_request=other_person
        and sensitive_object
        and _has_high_confidence_request(canonical, _OTHER_PERSON_RE),
    )


def _has_independent_self_scope_clause(
    text: str, school_names: frozenset[str]
) -> bool:
    """是否存在与外校内容分开、可独立回答的本校片段。

    有这种片段时，确定性证据只标记风险，不抢在校本片段前把整条消息判成
    纯外校拒绝；scope-aware router 负责返回 mixed 和经原文校验的安全片段。
    """

    for clause in _CLAUSE_SPLIT_RE.split(text):
        candidate = _clean_clause(clause)
        if not candidate or not _has_substantive_content(candidate):
            continue
        has_self_marker = _SELF_SCOPE_RE.search(candidate) is not None
        has_local_data_without_access = (
            _DATA_OBJECT_RE.search(candidate) is not None
            and _ACCESS_ACTION_RE.search(candidate) is None
            and _SENSITIVE_RE.search(candidate) is None
        )
        if not has_self_marker and not has_local_data_without_access:
            continue
        external = (
            _EXTERNAL_SCOPE_RE.search(candidate) is not None
            or _has_tenant_label(candidate)
            or _LETTER_SCHOOL_RE.search(candidate) is not None
            or _has_external_school_entity(candidate, school_names)
        )
        if not external:
            return True
    return False

def _has_high_confidence_request(text: str, target: re.Pattern[str]) -> bool:
    """目标实体附近是否存在无歧义的当前索取提示。

    判定只看目标实体前后的局部动词和 A-not-A 问句。它不尝试理解否定辖域：
    被否定、嵌套或语气不明的形式一律留给语义路由器，因此这里的漏判不会把
    外校数据送进检索，只会多一次语义判定。
    """

    for target_match in target.finditer(text):
        before_start = max(0, target_match.start() - _DIRECT_SCOPE_LOOKBEHIND)
        before = text[before_start : target_match.start()]
        a_not_a_before = _A_NOT_A_BEFORE_RE.search(before)
        for cue in _REQUEST_BEFORE_CUE_RE.finditer(before):
            prefix = before[: cue.start()]
            if a_not_a_before is not None:
                continue
            if _NEGATED_REQUEST_TAIL_RE.search(prefix) is None:
                return True

        after = text[target_match.end() : target_match.end() + _DIRECT_SCOPE_LOOKAHEAD]
        prohibition_before = _NEGATED_REQUEST_TAIL_RE.search(before) is not None
        for cue in _REQUEST_AFTER_CUE_RE.finditer(after):
            prefix = after[: cue.start()]
            if (
                prohibition_before
                or a_not_a_before is not None
                or _A_NOT_A_BEFORE_RE.search(prefix) is not None
            ):
                continue
            if _NEGATED_REQUEST_TAIL_RE.search(prefix) is None:
                return True
    return False


def classify_tenant_scope(
    message: str,
    *,
    school_names: Iterable[str] = (),
) -> TenantScopePlan:
    """执行零模型早拒与引述/身份净化；普通范围意图交给 scope-aware router。"""

    source = message.strip()
    if not source:
        return TenantScopePlan(TenantScopeMode.ALLOW, "", (), 0)

    normalized_school_names = _school_name_set(school_names)
    sanitized, quoted_rules, quoted_count = _sanitize_dangerous_quotes(
        source, normalized_school_names
    )
    evidence = inspect_tenant_scope_evidence(
        sanitized, school_names=normalized_school_names
    )
    rules: set[str] = set()
    if evidence.direct_external_request:
        rules.add("tenant.external_scope")
        if evidence.sensitive_object:
            rules.add("tenant.sensitive_access")
    if evidence.direct_other_person_request:
        rules.add("tenant.other_individual_access")
    if evidence.bypass and (evidence.external_scope or evidence.access_action or evidence.data_object):
        rules.add("tenant.platform_bypass")
    if _BYPASS_SCOPE_RE.search(_project(sanitized)):
        rules.add("tenant.platform_bypass")
    if rules:
        return TenantScopePlan(
            TenantScopeMode.BLOCK,
            None,
            tuple(sorted(rules)),
            max(1, len(rules)),
        )

    canonical = _project(sanitized)
    identity_claim = _has_identity_claim(canonical) or _BYPASS_RE.search(canonical) is not None
    safe_text = sanitized
    if identity_claim:
        safe_text = _TRIM_RE.sub("", _strip_identity_claim(sanitized))
        if not _has_substantive_content(safe_text):
            return TenantScopePlan(
                TenantScopeMode.BLOCK,
                None,
                ("tenant.identity_claim_only",),
                1,
            )

    if quoted_rules:
        # 纯引述在移除危险内容后没有可独立回答的剩余问题时必须整条拒绝；
        # 只有外围仍有真实本校问题时才允许 redact 留痕并继续。
        if not _has_substantive_content(safe_text):
            return TenantScopePlan(
                TenantScopeMode.BLOCK,
                None,
                tuple(sorted(quoted_rules)),
                max(1, quoted_count),
            )
        return TenantScopePlan(
            TenantScopeMode.ALLOW,
            safe_text,
            tuple(sorted(quoted_rules)),
            quoted_count,
        )
    return TenantScopePlan(TenantScopeMode.ALLOW, safe_text, (), 0)


def _is_self_contained_fragment_clause(
    clause: str, school_names: frozenset[str]
) -> bool:
    """单个安全片段分句能否独立证明答复范围。

    显式本校标记可直接证明归属；没有本校标记时，只接受足够具体、且不含
    访问动作或个人敏感对象的机构级事实，例如“校门开放时间”。裸对象
    “名单/成绩”或需要省略主语推断的人员信息一律拒绝。
    """

    candidate = _clean_clause(clause)
    if not candidate or not _has_substantive_content(candidate):
        return False
    external = (
        _EXTERNAL_SCOPE_RE.search(candidate) is not None
        or _has_tenant_label(candidate)
        or _LETTER_SCHOOL_RE.search(candidate) is not None
        or _has_external_school_entity(candidate, school_names)
    )
    if external:
        return False
    if _SELF_SCOPE_RE.search(candidate) is not None:
        return True
    content_length = len(re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", candidate))
    return (
        content_length >= 4
        and _DATA_OBJECT_RE.search(candidate) is not None
        and _ACCESS_ACTION_RE.search(candidate) is None
        and _SENSITIVE_RE.search(candidate) is None
    )


def validate_scope_fragments(
    source: str,
    fragments: Iterable[str],
    *,
    school_names: Iterable[str] = (),
) -> tuple[str, ...] | None:
    """校验语义路由器返回的本校片段；任何不可追溯片段使整组失效。"""

    normalized_school_names = _school_name_set(school_names)
    validated: list[str] = []
    for fragment in fragments:
        if not isinstance(fragment, str):
            return None
        candidate = _clean_clause(fragment)
        if not candidate or candidate not in source:
            return None
        canonical = _project(candidate)
        if not _has_substantive_content(canonical):
            return None
        # 原文字符串存在只能证明“没有拼接”，不能证明片段属于本校。每个实质
        # 分句都必须能独立证明归属：显式本校范围，或无访问动作、无个人敏感
        # 对象的完整机构级事实。省略主语的“老师微信/手机号”不能借前句放行。
        for raw_clause in _CLAUSE_SPLIT_RE.split(canonical):
            subclause = _clean_clause(raw_clause)
            if subclause and not _is_self_contained_fragment_clause(
                subclause, normalized_school_names
            ):
                return None
        if (
            _DANGLING_PRONOUN_RE.search(canonical) is not None
            or _EXTERNAL_PERSON_REFERENCE_RE.search(canonical) is not None
            or _MIXED_PERSONAL_ATTRIBUTE_RE.search(canonical) is not None
        ):
            return None
        evidence = inspect_tenant_scope_evidence(
            canonical, school_names=normalized_school_names
        )
        if evidence.external_scope or evidence.bypass:
            return None
        if _has_identity_claim(canonical) or _UNSAFE_FRAGMENT_RE.search(canonical) is not None:
            return None
        validated.append(candidate)
    return tuple(validated)


def semantic_scope_plan(
    decision: str,
    *,
    source: str,
    safe_fragments: Iterable[str] = (),
    blocked_count: int = 1,
    school_names: Iterable[str] = (),
) -> TenantScopePlan | None:
    """把 router 的结构化语义结果转换为内部计划，并执行 fail-closed 校验。"""

    if decision in {"block", "restriction", "identity_claim"}:
        rule = (
            "tenant.identity_claim_only"
            if decision == "identity_claim"
            else "tenant.external_scope"
        )
        return TenantScopePlan(
            TenantScopeMode.BLOCK,
            None,
            (rule,),
            max(1, blocked_count),
        )
    fragments = tuple(safe_fragments)
    validated = validate_scope_fragments(
        source, fragments, school_names=school_names
    )
    if validated is None:
        return None
    if decision == "mixed":
        if not validated:
            return None
        return TenantScopePlan(
            TenantScopeMode.MIXED,
            "；".join(validated),
            ("tenant.external_scope",),
            max(1, blocked_count),
        )
    if decision == "allow":
        if validated:
            return TenantScopePlan(
                TenantScopeMode.ALLOW,
                "；".join(validated),
                (),
                0,
            )
        return TenantScopePlan(TenantScopeMode.ALLOW, source, (), 0)
    return None


def allows_public_comparison(text: str) -> bool:
    """公开比较豁免的只读入口，供语义路由后的 query 校验复用。"""

    return _allows_public_comparison(text)

def _has_substantive_content(text: str) -> bool:
    """片段是否含实质内容（不是纯连接词/回答动词/占位符残片）。"""

    content = text.replace(QUOTED_CONTENT_REDACTION, "")
    if len(re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", content)) < 3:
        return False
    return len(_FRAGMENT_NOISE_RE.sub("", content)) >= 2


def sanitize_history_text(
    text: str,
    *,
    school_names: Iterable[str] = (),
) -> str | None:
    """历史消息进模型前应用保守净化；可疑外校取数片段整条丢弃。"""

    if _is_system_script(text):
        return text
    segments = _split_by_system_scripts(text)
    normalized_school_names = _school_name_set(school_names)
    non_script = "".join(segment for is_script, segment in segments if not is_script)
    if non_script.strip():
        evidence = inspect_tenant_scope_evidence(
            non_script, school_names=normalized_school_names
        )
        if evidence.external_access_candidate or evidence.bypass:
            return None
    parts: list[str] = []
    for is_script, segment in segments:
        if is_script:
            parts.append(segment)
            continue
        if not segment.strip():
            continue
        sanitized, _quote_rules, _quote_count = _sanitize_dangerous_quotes(
            segment, normalized_school_names
        )
        plan = classify_tenant_scope(
            sanitized, school_names=normalized_school_names
        )
        if plan.mode is TenantScopeMode.BLOCK:
            return None
        evidence = inspect_tenant_scope_evidence(
            sanitized, school_names=normalized_school_names
        )
        if evidence.external_access_candidate or evidence.bypass:
            return None
        if plan.safe_text:
            parts.append(plan.safe_text)
    return "".join(parts)


def _sanitize_dangerous_quotes(
    text: str,
    school_names: frozenset[str],
) -> tuple[str, set[str], int]:
    """从引述中移除含外校取数证据的内容，同时保留外围问题。"""

    removed_rules: set[str] = set()
    redaction_count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal redaction_count
        whole = match.group(0)
        body = whole[1:-1]
        evidence = inspect_tenant_scope_evidence(body, school_names=school_names)
        if not (evidence.external_access_candidate or evidence.bypass):
            return whole
        removed_rules.add(
            "tenant.platform_bypass"
            if evidence.bypass
            else "tenant.external_scope"
        )
        redaction_count += 1
        return QUOTED_CONTENT_REDACTION

    return _QUOTED_RE.sub(replace, text), removed_rules, redaction_count
def tenant_scope_boundary_script(rule_codes: Iterable[str]) -> str:
    """按规则码选出用户可见话术：他人隐私走隐私话术，其余走跨租范围话术。

    拒绝事件、混合请求的边界说明必须与 guard result 用同一套话术，
    否则用户会收到与实际拦截口径不一致的解释（TD-361 四轮审查 M1）。
    """

    codes = frozenset(rule_codes)
    if "tenant.identity_claim_only" in codes:
        return TENANT_SCOPE_IDENTITY_CLAIM
    if "tenant.other_individual_access" in codes:
        return TENANT_SCOPE_SENSITIVE_BOUNDARY
    return TENANT_SCOPE_BOUNDARY


def tenant_scope_guard_result(plan: TenantScopePlan) -> GuardResult:
    """把计划转为稳定 GuardResult；绝不拼接用户原文。"""

    if plan.mode is TenantScopeMode.ALLOW:
        raise ValueError("allow 计划不应生成 block guard result")
    reason = "规则码：" + ",".join(plan.rule_codes)
    replacement = tenant_scope_boundary_script(plan.rule_codes)
    # 只有身份声称时不是跨租攻击：规则与严重度都要与实际拦截口径一致，
    # 否则审计里会出现并不存在的跨租安全事件（TD-361 五轮审查 M2）。
    identity_only = "tenant.identity_claim_only" in plan.rule_codes
    other_individual = "tenant.other_individual_access" in plan.rule_codes
    if identity_only:
        rule, severity = "input.identity_claim", "medium"
    elif "tenant.scope_router_cancelled" in plan.rule_codes:
        rule, severity = "input.scope_router_unavailable", "medium"
    elif other_individual:
        # 他人隐私与跨租是两条规则：审计列必须能直接区分，否则 guard_events.rule
        # 全是 input.cross_tenant，与用户看到的话术、指标标签对不上
        # （TD-361 六轮审查 m1）。
        rule, severity = "input.other_individual", "high"
    else:
        rule, severity = "input.cross_tenant", "high"
    return GuardResult(
        action="block",
        rule=rule,
        severity=severity,
        reason=reason,
        replacement=replacement,
    )


def tenant_scope_quote_guard_result(rule_codes: tuple[str, ...]) -> GuardResult:
    """引述内跨租指令被净化、整体仍放行时的审计结果（action=redact）。"""

    return GuardResult(
        action="redact",
        rule="input.cross_tenant",
        severity="medium",
        reason="规则码：" + ",".join(rule_codes),
        replacement=QUOTED_CONTENT_REDACTION,
    )



def _is_system_script(text: str) -> bool:
    """文本是否就是助手自己的固定话术（逐字相等）。

    助手自己说过的拒答理由回灌进历史时不得被自己判成外校/身份声称并截断，
    否则下一轮上下文里只剩半句话，模型可能重新回答同一个被拒的问题
    （十轮审查 m2）。只认逐字相等的固定话术：用户消息里夹带话术片段时
    仍走正常判定，不享受豁免。
    """

    candidate = text.strip().rstrip("。！？.!?")
    return any(candidate == script.rstrip("。！？.!?") for script in _SYSTEM_SCRIPTS)


def _split_by_system_scripts(text: str) -> list[tuple[bool, str]]:
    """按固定话术片段切开文本：(是否话术, 片段)。

    助手的话术带前缀（“根据规定：…”）时也不能被自己截断：话术片段原样
    保留，其余部分照常判定（十一轮审查 m1）。
    """

    segments: list[tuple[bool, str]] = []
    position = 0
    while position < len(text):
        best_index = -1
        best_script = ""
        for script in _SYSTEM_SCRIPTS:
            index = text.find(script, position)
            if index >= 0 and (best_index < 0 or index < best_index):
                best_index = index
                best_script = script
        if best_index < 0:
            segments.append((False, text[position:]))
            break
        if best_index > position:
            segments.append((False, text[position:best_index]))
        segments.append((True, best_script))
        position = best_index + len(best_script)
    return segments


def _strip_invisible(text: str) -> str:
    """剥离零宽/不可见格式字符（十轮审查 Major 1）。"""

    return _INVISIBLE_FORMAT_RE.sub("", text)


def _project(text: str) -> str:
    """判定用的**统一**投影：NFKC + 剥离不可见格式字符。

    判定、实体识别、混合切分、可回答片段闸门必须看同一份投影，否则同一条
    规则会在不同路径上得出不同结论——第十轮 Major 1 与第十一轮 Major 1 都是
    同一类问题（判定与改写必须用同一投影），新增投影入口时一律走这里。
    """

    return _strip_invisible(unicodedata.normalize("NFKC", text))


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", _strip_invisible(unicodedata.normalize("NFKC", value))).lower()


def _school_name_set(names: Iterable[str]) -> frozenset[str]:
    """保留原始名称（遮蔽时大小写/空白不敏感，压缩形式会漏掉全大写校码）。"""

    return frozenset(value.strip() for name in names if (value := str(name).strip()))


def _allows_public_comparison(text: str) -> bool:
    """仅放行整句都能证明为机构级公开比较的确定性豁免。

    每个非空分句都必须完整匹配公开比较语法白名单。若出现任何额外分句，
    无论其是否重复写“其他学校”，都不从本函数获得豁免；调用方只能接受
    路由器返回、经原文校验的本校 MIXED 安全片段。
    """

    projected = _project(text)
    comparison_seen = False
    for raw_clause in _CLAUSE_SPLIT_RE.split(projected):
        clause = _clean_clause(raw_clause)
        if not clause:
            continue
        if not any(pattern.fullmatch(clause) for pattern in _PUBLIC_COMPARISON_PATTERNS):
            return False
        comparison_seen = True
    return comparison_seen



def _has_external_school_entity(clause: str, school_names: frozenset[str]) -> bool:
    """命中小学实体；认证租户名称先遮蔽，泛称“本校/学校”不算具体外校。"""

    # 先剥离不可见字符再遮蔽：顺序反了会让“北京\u200b大学”对不上认证租户名
    # “北京大学”，把本校自指误判成外校（十轮审查 Major 1）。
    masked = _project(clause)
    if not masked.strip():
        return False
    masked = _mask_school_names(masked, school_names)
    # 泛称自指替换成标记而不是空格：简称形态要能识别“我们学校的XX”是自指
    # （TD-361 七轮审查 M2）。
    masked = _GENERIC_SELF_SCHOOL_RE.sub(_SELF_MARK, masked)
    compact = _strip_request_prefixes(_compact(masked))
    if _has_named_school(compact, school_names):
        return True
    if _has_school_abbreviation(compact, school_names):
        return True
    return _has_latin_school_entity(masked)


def _mask_authenticated_school_code_labels(
    text: str, school_names: frozenset[str]
) -> str:
    """单字母认证校码只在明确“校码+学校/校”形态下按本校遮蔽。"""

    for name in school_names:
        compact = name.strip()
        if len(compact) != 1 or not compact.isalnum():
            continue
        pattern = rf"(?<![A-Za-z0-9]){re.escape(compact)}\s*(?=学校|校)"
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    return text

def _mask_school_names(text: str, school_names: frozenset[str]) -> str:
    """遮蔽认证租户名称；大小写与空白不敏感（全大写校码也必须命中）。

    租户名若是更长专名的尾部（租户“第一中学”遇到“北京第一中学”），遮蔽会把外校名
    整段抹掉，外校请求就不被早拒；这种情况不遮蔽，交给具名外校判定。
    """

    for name in sorted(school_names, key=len, reverse=True):
        # 单字符校名/校码不进遮蔽集合：它会命中正文里的任意同字母
        # （租户校码“A”会把“Ask about leave policy”抹成空格），
        # 判定文本被破坏后本校正常提问也会判错（九轮审查 m2）。
        if not name or len(name.strip()) < 2:
            continue
        pattern = r"\s+".join(re.escape(part) for part in name.split())
        text = _mask_tenant_name(text, pattern)
        # 内部带空格的变体（“海州 第一中学” == “海州第一中学”）同样必须命中。
        chars = [char for char in name if not char.isspace()]
        if len(chars) > 1:
            loose = r"\s*".join(re.escape(char) for char in chars)
            text = _mask_tenant_name(text, loose)
    return text


def _mask_tenant_name(text: str, pattern: str) -> str:
    """按“能否当作本校名”决定是否遮蔽命中片段。"""

    def replace(match: re.Match[str]) -> str:
        if not _may_mask_tenant_name(text, match.start()):
            return match.group(0)
        return " "

    return re.sub(pattern, replace, text, flags=re.IGNORECASE)


def _may_mask_tenant_name(text: str, start: int) -> bool:
    """校名能否当作本校名遮蔽。

    只有校名前面的文字是礼貌前缀、单字功能词、本校自指或行政区划修饰时，
    才当作本校名（“查第一中学”“本校第一中学”“海州市第一中学”）；
    前面是另一段专名形状的文字时不遮蔽（“北京第一中学”是另一所学校）。
    """

    prefix = text[:start]
    if not prefix:
        return True
    if _HAN_TAIL_RE.search(prefix) is None:
        # 前面是标点/空格/开头：不是专名的一部分。
        return True
    remainder = _strip_request_prefixes(_compact(prefix))
    if len(remainder) <= 1:
        return True
    if _SELF_NAME_TAIL_RE.search(remainder) is not None:
        return True
    if remainder.endswith(_ADMIN_NAME_TAIL):
        return True
    return any(remainder.endswith(word) for word in _NON_NAME_TAIL_WORDS)


def _alias_points_to_tenant(tail: str, school_names: Iterable[str]) -> bool:
    """校区/分校前缀里的简称是否指向认证租户。

    “浙大紫金港校区”在租户=浙江大学时是本校校区，不是外校实体；
    租户=北京大学时“浙大紫金港校区”仍指外校，必须拒绝。
    """

    tenants = {_squash(name) for name in school_names}
    if not tenants:
        return False
    for alias, candidates in _SCHOOL_ABBREVIATION_ALIASES.items():
        if len(candidates) != 1 or not tail.startswith(alias):
            continue
        if candidates[0] in tenants:
            return True
    return False


def _has_named_school(text: str, school_names: Iterable[str] = ()) -> bool:
    """具体校名判定：类型词前必须是专名形状，不是泛称或功能词残片。"""

    for match in _SCHOOL_SUFFIX_RE.finditer(text):
        candidate = _HAN_TAIL_RE.search(text[: match.start()])
        if candidate is None or len(candidate.group(0)) < 2:
            continue
        tail = candidate.group(0)
        if any(tail.endswith(word) for word in _NON_NAME_TAIL_WORDS):
            continue
        if _QUANTIFIED_NOUN_RE.search(tail) or _FUNCTION_TAIL_RE.search(tail):
            # 量词短语（“一所中学”“一名中学生”）与动作短语（“我在学校医院看病”）
            # 都不是校名，硬拒会误伤本校正常提问（TD-361 七轮审查 M5）。
            continue
        if (
            match.group(0) in _BRANCH_CAMPUS_SUFFIXES
            and _GENERIC_SCHOOL_NOUN_RE.search(tail)
        ):
            # “学校有分校吗”里的“分校”是校区泛称，不是具体外校名。
            continue
        if (
            match.group(0) in _BRANCH_CAMPUS_SUFFIXES
            and _alias_points_to_tenant(tail, school_names)
        ):
            # “浙大紫金港校区”：简称指向本校时这是本校校区，不是外校实体
            # （九轮审查 m3）。
            continue
        return True
    return _LATIN_SCHOOL_CODE_RE.search(text) is not None


def _is_self_school_label(text: str, start: int, keyword: str) -> bool:
    """“本校/我们学校”后面的校码是本校自指，不是跨租指向。"""

    prefix = text[:start]
    if prefix.endswith(_SELF_SCHOOL_CORE_PREFIXES) or prefix.endswith(
        _SELF_SCHOOL_FULL_PREFIXES
    ):
        return True
    return (prefix + keyword).endswith(_SELF_SCHOOL_FULL_PREFIXES)


def _has_tenant_label(text: str) -> bool:
    """租户/学校代称判定；本校自指前缀不算代称。"""

    if _TENANT_ID_RE.search(text) is not None:
        return True
    for match in _SCHOOL_LABEL_RE.finditer(text):
        if _is_self_school_label(text, match.start(), match.group("keyword")):
            continue
        return True
    return False


def _strip_request_prefixes(text: str) -> str:
    """剥离开头的请求礼貌语：“帮我查学校”是泛称，“帮我查北京大学”是外校。"""

    previous: str | None = None
    current = text
    while previous != current:
        previous = current
        current = _REQUEST_PREFIX_RE.sub("", current, count=1)
    return current


def _squash(value: str) -> str:
    """去掉空白并做 NFKC 归一，用于校名别名与租户校名比较。"""

    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def _is_self_school_reference(text: str, start: int) -> bool:
    """简称前面若是本校自指标记（我们学校/本校…），不算具体外校实体。"""

    marker = text.rfind(_SELF_MARK, 0, start)
    if marker < 0:
        return False
    between = text[marker + 1 : start]
    return not any("\u4e00" <= char <= "\u9fff" for char in between)


def _has_school_abbreviation(text: str, school_names: frozenset[str]) -> bool:
    """高校简称形态的外校实体（“北大的学生名单”“清华的图书馆开放时间”）。

    简称只有在唯一指向本校时才按本校自指放行：含义不唯一（“南大”可能是南京大学
    或南昌大学）无法确认指向，一律按外校处理（TD-361 七轮审查 M1/M2）。
    """

    tenants = [_squash(name) for name in school_names]
    alias_spans: list[tuple[int, int]] = []
    for alias, candidates in _SCHOOL_ABBREVIATION_ALIASES.items():
        context = (
            _SCHOOL_ABBREV_OBJECT_CONTEXT_RE
            if alias in _SCHOOL_ABBREV_COMMON_WORD
            else _SCHOOL_ABBREV_CONTEXT_RE
        )
        for match in re.finditer(re.escape(alias), text):
            alias_spans.append(match.span())
            if not context.match(text[match.end() :]):
                continue
            if _SCHOOL_ABBREV_NON_SCHOOL_SUFFIX_RE.match(text[match.end() :]):
                # “同济医院的地址”“南开区的图书馆”不是外校学校实体。
                continue
            # 别名分支不做“本校自指标记紧邻简称”的豁免：简称本身就是外校实体形态，
            # “我们学校的北大的学生名单”“本校北大的学生名单”指向的是外校北大，
            # 不能因为前面是“我们的学校/本校”就放行（八轮审查 m2）。
            # 真正指本校的形态由下面的“候选全名完全等于认证租户校名”覆盖：
            # 租户=北京大学 时“我们学校的北大的学生名单”仍然放行。
            if len(candidates) == 1 and (
                # 指向本校只认“完全相等”：租户名是候选全名的子串时（“中国人民大学附属中学”
                # 含“中国人民大学”），两者是不同学校，子串自指是漏拒口子（八轮审查 M1）。
                any(candidate == tenant for candidate in candidates for tenant in tenants)
                # 认证租户校名本身就是这个简称（如租户名“人大”）：显然指本校。
                or any(tenant == alias for tenant in tenants)
            ):
                # 认证租户就是该简称唯一指向的学校：算本校自指。
                continue
            return True
    for match in _SCHOOL_ABBREV_TAIL_RE.finditer(text):
        if any(start <= match.start() < end for start, end in alias_spans):
            # 已被别名表判定的片段（含本校自指），不再走形态尾词规则。
            continue
        if any(char in _SCHOOL_ABBREV_TAIL_PREFIX_BLOCKLIST for char in match.group(1)):
            # 前缀含功能词（“们的科技”“在北科大”），不是简称形态。
            continue
        if not _SCHOOL_ABBREV_CONTEXT_RE.match(text[match.end() :]):
            # 形态尾词与别名同口径：对象词可直接紧跟简称（“南科大名单”），
            # 少一个“的”不得放行纯外校索取（八轮审查 m3 的口径统一）。
            continue
        if _is_self_school_reference(text, match.start()):
            continue
        return True
    return False


def _has_latin_school_entity(text: str) -> bool:
    """全大写拉丁缩写 + “的学生名单/成绩单”才算具体外校实体。"""

    for match in _LATIN_ACADEMIC_TOKEN_RE.finditer(text):
        token = match.group(1)
        if token in _LATIN_NON_SCHOOL_TOKENS:
            continue
        tail = text[match.end() :].lstrip()
        if _LATIN_SCHOOL_OBJECT_RE.match(tail):
            return True
    return False


def _clean_clause(clause: str) -> str:
    cleaned = _TRIM_RE.sub("", _LEADING_CONNECTOR_RE.sub("", clause.strip())).strip()
    return _LEADING_ANSWER_CONNECTOR_RE.sub("", cleaned)


def _nfkc_project(text: str) -> tuple[str, list[int]]:
    """逐字符 NFKC 投影，并保留投影字符到原文下标的映射（无法一一对应记 -1）。"""

    folded: list[str] = []
    mapping: list[int] = []
    for index, char in enumerate(text):
        if _INVISIBLE_FORMAT_RE.match(char):
            # 零宽字符不可见：投影里直接跳过，映射下标自然跳过对应原文位置，
            # 摘除声称时整段（含零宽字符）一并去掉（十轮审查 Major 1）。
            continue
        piece = unicodedata.normalize("NFKC", char)
        folded.append(piece)
        if len(piece) == 1:
            mapping.append(index)
        else:
            mapping.extend([-1] * len(piece))
    return "".join(folded), mapping


def _project_span(mapping: list[int], start: int, end: int) -> tuple[int, int] | None:
    """把投影文本的区间映射回原文区间；无法一一对应时返回 None。"""

    if start < 0 or end > len(mapping) or start >= end:
        return None
    original_start = mapping[start]
    original_end = mapping[end - 1]
    if original_start < 0 or original_end < 0:
        return None
    return original_start, original_end + 1


def _strip_claims(
    source: str,
    canonical: str,
    mapping: list[int] | None,
) -> str | None:
    """摘掉投影里的身份声称；mapping 为 None 时直接在投影文本上删除。"""

    parts: list[str] = []
    position = 0
    canonical_pos = 0
    matches = list(_PLATFORM_CLAIM_RE.finditer(canonical))
    for match in _WEAK_CLAIM_RE.finditer(canonical):
        # 弱声称只在带第一人称主语或整句即声称时摘除（十轮审查 m1）。
        if _has_first_person_before(canonical, match.start()) or _claim_covers_clause(
            canonical, _claim_end(canonical, match)
        ):
            matches.append(match)
    matches.sort(key=lambda item: item.start())
    for match in matches:
        start = match.start()
        if start < canonical_pos:
            continue
        end = match.end()
        role = _PLATFORM_CLAIM_ROLE_RE.match(canonical, end)
        if role is not None:
            end = role.end()
        # 声称后面紧跟的“的/地/得”一并摘掉，否则会留下“的权限有哪些”这种
        # 既不自包含、也不能当检索 query 的残句（十轮审查 m1）。
        if end < len(canonical) and canonical[end] in "的地得":
            end += 1
        # 声称前面紧跟的人称代词（“我现在是管理员”）一并摘掉，避免上下文里留下孤立的“我/你”。
        drop_pronoun = start > 0 and canonical[start - 1] in "我你咱"
        if mapping is None:
            src_start = start - 1 if drop_pronoun else start
            src_end = end
        else:
            span = _project_span(mapping, start, end)
            if span is None:
                return None
            src_start, src_end = span
            if drop_pronoun:
                previous = mapping[start - 1]
                if previous < 0:
                    return None
                src_start = previous
        parts.append(source[position:src_start])
        position = src_end
        canonical_pos = end
    parts.append(source[position:])
    return "".join(parts)


def _strip_identity_claim(text: str) -> str:
    """摘掉身份声称本体（含紧跟其后的角色词），其余原话保留。

    识别用 NFKC 投影（全角写法不得绕过），删除落回原文，
    保证返回片段仍是原文子串（可溯源）。
    """

    canonical, mapping = _nfkc_project(text)
    stripped = _strip_claims(text, canonical, mapping)
    if stripped is None:
        # 投影与原文无法一一对应：退回整串投影结果（仍保证声称本体被摘除）。
        projected = _strip_invisible(unicodedata.normalize("NFKC", text))
        fallback = _strip_claims(projected, projected, None)
        return fallback if fallback is not None else projected
    if stripped == text:
        # 逐字符投影没命中时，用整串投影再试一次（组合字符等边界形态）。
        projected = _strip_invisible(unicodedata.normalize("NFKC", text))
        fallback = _strip_claims(projected, projected, None)
        if fallback is not None and fallback != projected:
            return fallback
    return stripped
