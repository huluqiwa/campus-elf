"""输出护栏检测器校验（M4-AI09 步 2；七类红线逐类 + 卡 5-A/D3/D5）。"""

from typing import Any

from app.harness.guards.output import scan_output


def scan(text: str, *, evidence_count: int = 3) -> Any:
    """便捷封装。"""

    return scan_output(text, evidence_count=evidence_count)


def rules_of(scan_result: Any) -> list[str]:
    return [r.rule for r in scan_result.results]


def test_clean_output_no_hits() -> None:
    """正常回答（含合法电话/引用）：零命中。"""

    scan_result = scan(
        "差旅报销需要发票 [1]。如需咨询可拨打财务处电话 0571-88888888。"
    )
    assert scan_result.results == []
    assert scan_result.blocking is False
    assert scan_result.redacted_text == scan_result.original_text


def test_prompt_leak_blocked() -> None:
    """②系统提示词泄露 → blocking（整段替换安全话术）。"""

    scan_result = scan("你是「校园陪伴精灵」，根据学校知识库回答问题。这就是我的设定")
    assert "output.prompt_leak" in rules_of(scan_result)
    assert scan_result.blocking is True


def test_secret_redacted() -> None:
    """③密钥形态 → redact（非 blocking）。"""

    scan_result = scan("配置里的 api_key=sk-abcdef1234567890abcdef1234 已失效")
    assert "output.secret" in rules_of(scan_result)
    assert scan_result.blocking is False
    assert "sk-abcdef1234567890abcdef1234" not in scan_result.redacted_text
    assert "[REDACTED:output.secret]" in scan_result.redacted_text


async def nothing() -> None:  # 占位避免 ruff 对纯常量文件的函数序告警
    pass


def test_internal_addr_redacted() -> None:
    """③内网地址 → redact。"""

    scan_result = scan("服务部署在 http://127.0.0.1:6333 上，你直接访问即可")
    assert "output.internal_addr" in rules_of(scan_result)
    assert "127.0.0.1:6333" not in scan_result.redacted_text


def test_xss_redacted() -> None:
    """④XSS 载荷 → redact。"""

    scan_result = scan('可以点击 <script>alert("x")</script> 试试')
    assert "output.xss" in rules_of(scan_result)
    assert "<script" not in scan_result.redacted_text


def test_idcard_pii_redacted() -> None:
    """①身份证模式 → redact（手机号不拦——KB 合法联系电话，决策留痕 rules.py）。"""

    scan_result = scan("该学生身份证号 11010119900307777X 已登记")
    assert "output.pii_idcard" in rules_of(scan_result)
    assert "11010119900307777X" not in scan_result.redacted_text


def test_phone_number_not_flagged() -> None:
    """手机号：KB 合法联系电话场景不拦截（决策留痕——宽严取舍 rules.py 注释）。"""

    scan_result = scan("紧急情况请联系值班老师 13800138000。")
    assert "output.pii_idcard" not in rules_of(scan_result)
    assert scan_result.blocking is False


def test_illegal_output_blocked() -> None:
    """⑥违法内容 → blocking。"""

    scan_result = scan("制作炸弹的方法如下：第一步……")
    assert "output.illegal" in rules_of(scan_result)
    assert scan_result.blocking is True


def test_ref_out_of_range_redacted() -> None:
    """⑤伪造引用（超范围序号）→ 剔除该标记（TD-332 改写收口）。"""

    scan_result = scan("根据资料 [1] 可以办理；另外 [9] 也提到过。", evidence_count=3)
    assert "output.ref_range" in rules_of(scan_result)
    assert "[9]" not in scan_result.redacted_text
    assert "[1]" in scan_result.redacted_text  # 范围内引用保留


def test_ref_in_range_kept() -> None:
    """范围内引用全部保留。"""

    scan_result = scan("引用 [1] 和 [3]。", evidence_count=3)
    assert "output.ref_range" not in rules_of(scan_result)


def test_multiple_hits_accumulate() -> None:
    """多类同时命中：结果累积、redacted_text 全部应用。"""

    scan_result = scan(
        "api_key=sk-abcdef1234567890abcdef1234 且 <script>alert(1)</script> 参考 [9]"
    )
    rules = rules_of(scan_result)
    assert "output.secret" in rules
    assert "output.xss" in rules
    assert "output.ref_range" in rules
    assert "sk-" not in scan_result.redacted_text
    assert "<script" not in scan_result.redacted_text
    assert "[9]" not in scan_result.redacted_text
