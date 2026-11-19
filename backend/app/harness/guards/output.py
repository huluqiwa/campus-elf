"""输出护栏检测器（M4-AI09 步 2；卡 5-A 七类全量 + D2/D3）。

`scan_output(text, evidence_count)` → `OutputScan`：

- `results`：全部命中（GuardResult——rule/severity/action）
- `blocking`：是否存在 block 级命中（违法/自伤/提示词泄露——整段替换安全话术）
- `redacted_text`：应用全部 redact 类替换后的文本（命中本体以
  `[REDACTED:<rule>]` 占位——D3：span 落库脱敏，原始载荷不留存）

伪造引用（⑤）：`[n]` 序号 > evidence_count → 剔除该标记（替换为空串——
TD-332 正式收口，哨兵缓冲见 stream_guard.py）。

手机号不拦（KB 合法联系电话误伤率高——决策留痕 rules.py ①注释）。
"""

import re
from dataclasses import dataclass, field

from app.harness.contracts import GuardResult
from app.harness.guards.rules import (
    _ILLEGAL_PATTERNS,
    _SELF_HARM_PATTERNS,
    INTERNAL_ADDR_PATTERNS,
    PII_PATTERNS,
    PROMPT_LEAK_PATTERNS,
    REF_MARKER,
    SECRET_PATTERNS,
    XSS_PATTERNS,
)

_REDACT_TEMPLATE = "[REDACTED:{rule}]"

_RULE_PROMPT_LEAK = "output.prompt_leak"
_RULE_SECRET = "output.secret"
_RULE_INTERNAL_ADDR = "output.internal_addr"
_RULE_XSS = "output.xss"
_RULE_REF_RANGE = "output.ref_range"
_RULE_ILLEGAL = "output.illegal"
_RULE_SELF_HARM = "output.self_harm"
_RULE_PII_IDCARD = "output.pii_idcard"

# block 级规则（整段替换安全话术 + 终止生成）；其余命中为 redact 级
_BLOCKING_RULES = frozenset({_RULE_PROMPT_LEAK, _RULE_ILLEGAL, _RULE_SELF_HARM})

# redact 类：模式 → 规则名（redacted_text 替换时按规则占位）
_REDACT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    *[(p, _RULE_SECRET) for p in SECRET_PATTERNS],
    *[(p, _RULE_INTERNAL_ADDR) for p in INTERNAL_ADDR_PATTERNS],
    *[(p, _RULE_XSS) for p in XSS_PATTERNS],
    *[(p, _RULE_PII_IDCARD) for p in PII_PATTERNS],
)


@dataclass
class OutputScan:
    """输出扫描结果（loop/stream_guard 消费）。"""

    original_text: str
    results: list[GuardResult] = field(default_factory=list)
    blocking: bool = False
    redacted_text: str = ""


def _scan_patterns(
    text: str,
    *,
    patterns: tuple[re.Pattern[str], ...],
    rule: str,
    action: str,
    severity: str,
    reason: str,
) -> list[GuardResult]:
    """按模式组扫描；一条规则命中一次（去重——同组多模式不重复计）。"""

    for pattern in patterns:
        if pattern.search(text):
            return [
                GuardResult(
                    action=action,
                    rule=rule,
                    severity=severity,
                    reason=reason,
                )
            ]
    return []


def scan_output(text: str, *, evidence_count: int) -> OutputScan:
    """七类输出检测（卡 5-A）：纯函数，返回命中/blocking/redacted 文本。"""

    results: list[GuardResult] = []
    results.extend(
        _scan_patterns(
            text,
            patterns=PROMPT_LEAK_PATTERNS,
            rule=_RULE_PROMPT_LEAK,
            action="block",
            severity="high",
            reason="输出命中系统提示词/内部指令泄露特征",
        )
    )
    results.extend(
        _scan_patterns(
            text,
            patterns=_ILLEGAL_PATTERNS,
            rule=_RULE_ILLEGAL,
            action="block",
            severity="high",
            reason="输出命中违法内容特征",
        )
    )
    results.extend(
        _scan_patterns(
            text,
            patterns=_SELF_HARM_PATTERNS,
            rule=_RULE_SELF_HARM,
            action="block",
            severity="high",
            reason="输出命中自伤/心理危机特征（整段替换支持性话术）",
        )
    )
    results.extend(
        _scan_patterns(
            text,
            patterns=SECRET_PATTERNS,
            rule=_RULE_SECRET,
            action="redact",
            severity="high",
            reason="输出命中密钥/凭证形态",
        )
    )
    results.extend(
        _scan_patterns(
            text,
            patterns=INTERNAL_ADDR_PATTERNS,
            rule=_RULE_INTERNAL_ADDR,
            action="redact",
            severity="medium",
            reason="输出命中内网地址/组件端点形态",
        )
    )
    results.extend(
        _scan_patterns(
            text,
            patterns=XSS_PATTERNS,
            rule=_RULE_XSS,
            action="redact",
            severity="high",
            reason="输出命中 XSS/注入载荷特征",
        )
    )
    results.extend(
        _scan_patterns(
            text,
            patterns=PII_PATTERNS,
            rule=_RULE_PII_IDCARD,
            action="redact",
            severity="high",
            reason="输出命中身份证模式（他人 PII 泄露防线）",
        )
    )
    # ⑤伪造引用：[n] 序号 > evidence_count → 剔除（TD-332 改写收口）
    ref_out_of_range = False
    for match in REF_MARKER.finditer(text):
        if int(match.group(1)) > evidence_count:
            ref_out_of_range = True
            break
    if ref_out_of_range:
        results.append(
            GuardResult(
                action="redact",
                rule=_RULE_REF_RANGE,
                severity="medium",
                reason=f"引用序号超出证据条数（evidence={evidence_count}）——剔除标记",
            )
        )
    blocking = any(r.rule in _BLOCKING_RULES for r in results)
    return OutputScan(
        original_text=text,
        results=results,
        blocking=blocking,
        redacted_text=_redact(text, evidence_count=evidence_count),
    )


def _redact(text: str, *, evidence_count: int) -> str:
    """应用全部 redact 类替换：载荷占位 + 超范围引用剔除。"""

    for pattern, rule in _REDACT_RULES:
        text = pattern.sub(_REDACT_TEMPLATE.format(rule=rule), text)
    text = REF_MARKER.sub(
        lambda match: match.group(0) if int(match.group(1)) <= evidence_count else "",
        text,
    )
    return text
