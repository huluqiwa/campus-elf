"""系统设置（超管自批例外开关）的键白名单与值域校验单元测试。

口径：`docs/design-briefs/M1-E2-data-scope.md` §0 ⑧ —— 运营可调的策略进库、每请求读库热生效；
键必须在代码目录里声明（不做动态注册），库里出现未知键或脏值都要失败而不是猜一个默认。
"""

import pytest

from app.settings.store import (
    SETTING_DEFS,
    InvalidSettingValue,
    UnknownSettingKey,
    parse_setting_value,
    serialize_setting_value,
)

SELF_EXCEPTION_KEY = "approval.self_exception_enabled"


def test_self_exception_key_is_declared_and_defaults_to_off() -> None:
    """自批例外键存在、类型为布尔、出厂默认关闭（spec §3.4「默认关闭」）。"""

    definition = SETTING_DEFS[SELF_EXCEPTION_KEY]

    assert definition.value_type == "bool"
    assert definition.default is False


@pytest.mark.parametrize("raw", ["true", "TRUE", "  true  "])
def test_parse_accepts_true_forms(raw: str) -> None:
    """读值容忍大小写与首尾空白（写路径由 `serialize_setting_value` 控制）。

    注意：只接受 true/false 本身。`1/yes/on` 一律拒绝——出现这些值说明有人手工改库，
    静默当成真会悄悄打开一个安全例外。
    """

    assert parse_setting_value(SELF_EXCEPTION_KEY, raw) is True


@pytest.mark.parametrize("raw", ["false", "False"])
def test_parse_accepts_false_forms(raw: str) -> None:
    assert parse_setting_value(SELF_EXCEPTION_KEY, raw) is False


@pytest.mark.parametrize("raw", ["yes", "on", "", "2", "null", "1", "0"])
def test_parse_rejects_values_outside_the_declared_domain(raw: str) -> None:
    """值域外的字符串必须报错：把 `yes` 当成真、把空串当成假都是静默改策略。"""

    with pytest.raises(InvalidSettingValue):
        parse_setting_value(SELF_EXCEPTION_KEY, raw)


def test_unknown_key_is_rejected() -> None:
    """未声明的键不允许读写：防止运营或代码随手造一个没人审的开关。"""

    with pytest.raises(UnknownSettingKey):
        parse_setting_value("approval.some_new_switch", "true")


def test_serialize_roundtrips_parse() -> None:
    """写出去的值一定能被自己读回来（否则例外开关会出现「改了读不到」）。"""

    for value in (True, False):
        raw = serialize_setting_value(SELF_EXCEPTION_KEY, value)
        assert parse_setting_value(SELF_EXCEPTION_KEY, raw) is value


def test_every_declared_key_has_a_type_and_description() -> None:
    """每个键都要有类型、默认值与说明：说明会进矩阵与运营文档，不能留空。"""

    for definition in SETTING_DEFS.values():
        assert definition.value_type == "bool"
        assert isinstance(definition.default, bool)
        assert definition.description.strip()
