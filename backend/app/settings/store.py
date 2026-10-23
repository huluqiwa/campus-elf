"""运营可调的系统策略开关（M1-E2 口径 ⑧）。

为什么入库而不是 env：例外开关属于运营策略（谁都能在合规要求变化时改），
env 改动要发版/重启，且改完没人知道是谁改的。入库后：
- **每请求从库读**（与权限判据同口径）：改完下一次请求即生效，不踢在线会话；
- 写入必须带理由并进审计（`platform.config.edit`），因此"谁开的、为什么开"可追；
- 键在代码里白名单声明（`SETTING_DEFS`）：`GET /settings` 只列声明过的键（手工插进库的未知键
  接口读不到），写未知键 `put_setting()` 抛错并由接口映射成 404 —— 因此"随手造一个没人审的开关"
  在应用层走不通；把未知键在**启动自检**里点名尚未实现（TD-160，随 M2 运营后台一并做）。
  加键要发版，是有意的门槛（见 TD-155）。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

ValueType = Literal["bool"]

# 只接受 `serialize_setting_value` 写得出来的两种值（大小写/首尾空白容忍）。
# 故意不收 `1/yes/on`：写路径由本模块控制，出现别的值说明有人手工改库，
# 静默当成真/假都可能把一个安全例外打开，所以直接失败。
_TRUE_VALUES: frozenset[str] = frozenset({"true"})
_FALSE_VALUES: frozenset[str] = frozenset({"false"})

# 本期唯一的键：超管「允许自批自己提交的审批事项」例外开关（spec §3.4 默认关闭）
SELF_EXCEPTION_KEY = "approval.self_exception_enabled"


@dataclass(frozen=True, slots=True)
class SettingDef:
    """一个系统设置键的声明：类型、出厂默认与说明。"""

    key: str
    value_type: ValueType
    default: bool
    description: str


SETTING_DEFS: Mapping[str, SettingDef] = {
    SELF_EXCEPTION_KEY: SettingDef(
        key=SELF_EXCEPTION_KEY,
        value_type="bool",
        default=False,
        description="允许审批人审批自己提交的审批事项（默认关闭；开启需理由+审计并通知平台审计）",
    )
}


class UnknownSettingKey(ValueError):
    """键不在代码声明的白名单里（拒绝读写，不猜默认值）。"""


class InvalidSettingValue(ValueError):
    """值不在该键声明的值域里（拒绝读写，不把 `yes` 当真、也不把空串当假）。"""


@dataclass(frozen=True, slots=True)
class SettingSnapshot:
    """一次读取的结果：值 + 是否来自库里的人工设置。"""

    key: str
    value: bool
    is_library_value: bool


def parse_setting_value(key: str, raw: str) -> bool:
    """把库里的字符串解析成声明类型。

    异常:
        UnknownSettingKey：键未声明。
        InvalidSettingValue：值不在值域内。
    """

    definition = _require_definition(key)
    normalized = raw.strip().lower()
    if definition.value_type == "bool":
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    raise InvalidSettingValue(f"{key} 的值 {raw!r} 不是合法的布尔值")


def serialize_setting_value(key: str, value: bool) -> str:
    """把布尔值序列化成入库字符串（与 `parse_setting_value` 互逆）。"""

    _require_definition(key)
    return "true" if value else "false"


def _require_definition(key: str) -> SettingDef:
    definition = SETTING_DEFS.get(key)
    if definition is None:
        raise UnknownSettingKey(f"未声明的系统设置键：{key}（允许值 {sorted(SETTING_DEFS)}）")
    return definition


def is_known_key(key: str) -> bool:
    """键是否已声明（接口层用来把未知键映射成 404/422）。"""

    return key in SETTING_DEFS


async def get_setting(session: AsyncSession, key: str) -> SettingSnapshot:
    """读取一个开关：库里没有行时回落出厂默认（默认关闭是安全侧）。

    异常:
        UnknownSettingKey：键未声明。
        InvalidSettingValue：库里的脏值（手工写入）——不猜，直接失败让自检/请求暴露它。
    """

    definition = _require_definition(key)
    raw = await session.scalar(
        text("select value from public.system_settings where key = :key"), {"key": key}
    )
    if raw is None:
        return SettingSnapshot(key=key, value=definition.default, is_library_value=False)
    return SettingSnapshot(
        key=key, value=parse_setting_value(key, str(raw)), is_library_value=True
    )


async def put_setting(
    session: AsyncSession,
    *,
    key: str,
    value: bool,
    updated_by: UUID,
    reason: str,
) -> SettingSnapshot:
    """写入一个开关（理由必填：审计要能回答「谁、为什么改」）。

    异常:
        UnknownSettingKey：键未声明。
        ValueError：理由为空。
    """

    _require_definition(key)
    if not reason or not reason.strip():
        raise ValueError("修改系统设置必须填写理由")
    await session.execute(
        text(
            "insert into public.system_settings (key, value, updated_by, reason, updated_at) "
            "values (:key, :value, :updated_by, :reason, now()) "
            "on conflict (key) do update set value = excluded.value, "
            "updated_by = excluded.updated_by, reason = excluded.reason, updated_at = now()"
        ),
        {
            "key": key,
            "value": serialize_setting_value(key, value),
            "updated_by": str(updated_by),
            "reason": reason.strip(),
        },
    )
    return SettingSnapshot(key=key, value=value, is_library_value=True)


async def list_settings(session: AsyncSession) -> list[SettingSnapshot]:
    """列出全部已声明键的当前生效值（未落库的键回出厂默认）。"""

    return [await get_setting(session, key) for key in sorted(SETTING_DEFS)]


async def self_exception_enabled(session: AsyncSession) -> bool:
    """超管自批例外是否开启（本期唯一的读取方是审批分权判定）。"""

    snapshot = await get_setting(session, SELF_EXCEPTION_KEY)
    return snapshot.value


__all__ = [
    "SELF_EXCEPTION_KEY",
    "SETTING_DEFS",
    "InvalidSettingValue",
    "SettingDef",
    "SettingSnapshot",
    "UnknownSettingKey",
    "get_setting",
    "is_known_key",
    "list_settings",
    "parse_setting_value",
    "put_setting",
    "self_exception_enabled",
    "serialize_setting_value",
]
