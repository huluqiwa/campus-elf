"""自扩权判据单元测试（M2-UI3，TD-191）。

这里只测纯判定（`app/rbac/self_grant.evaluate_self_grant` 与 `app/rbac/admin._assert_self_grant`）：
接口/事务接线由业务场景用例覆盖（`tests/business/test_bs_m2ui3_self_grant.py`）。
用例的设计目的是把"什么时候打断运营"钉死——判据写宽了会把正常配置全挡掉，写窄了等于没有守卫。
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.auth.principal import Principal
from app.rbac.admin import EDITABLE_ROLE_CODES, RoleAdminError, _assert_self_grant
from app.rbac.catalog import catalog_codes
from app.rbac.scopes import KNOWN_SCOPE_TYPES
from app.rbac.seeds import TENANT_BUILTIN_ROLES
from app.rbac.self_grant import (
    SENSITIVE_SCOPE_TYPES,
    SENSITIVE_SELF_GRANT_CODES,
    SelfGrantExposure,
    evaluate_scope_self_grant,
    evaluate_self_grant,
)

_SELF_ROLE = "11111111-1111-1111-1111-111111111111"
_OTHER_ROLE = "22222222-2222-2222-2222-222222222222"


def test_sensitive_code_list_exists_in_the_catalog() -> None:
    """清单里拼错一个码 = 该敏感能力永远不进判定，而且是静默的。"""

    known = set(catalog_codes())
    unknown = sorted(SENSITIVE_SELF_GRANT_CODES - known)
    assert unknown == [], f"敏感码清单含目录外的码：{unknown}"


def test_role_manage_and_its_delete_sibling_are_both_sensitive() -> None:
    """`role.manage` 能配权限、`role.delete` 能清场，两者都属"给自己加就等于加全校能力"的一档。"""

    assert {"school.role.manage", "school.role.delete"} <= SENSITIVE_SELF_GRANT_CODES


def test_change_to_another_role_never_requires_confirmation() -> None:
    """改的是自己不所在的角色：能力面不流向操作者，属正常配置。"""

    exposure = evaluate_self_grant(
        role_id=_OTHER_ROLE,
        resulting_codes={"school.personnel.sensitive.read"},
        principal_role_ids={_SELF_ROLE},
        principal_permissions=set(),
    )
    assert exposure.requires_confirmation is False
    assert exposure.newly_reaching_self == ()


def test_non_sensitive_codes_are_not_flagged() -> None:
    """敏感清单之外（如看板/消息）的自扩不拦：那是矩阵里普通的一格，挡它只会逼人绕路。"""

    exposure = evaluate_self_grant(
        role_id=_SELF_ROLE,
        resulting_codes={"school.dashboard.view", "school.message.send"},
        principal_role_ids={_SELF_ROLE},
        principal_permissions=set(),
    )
    assert exposure.requires_confirmation is False


def test_newly_granted_sensitive_code_reaching_self_is_flagged_in_order() -> None:
    """净增两个敏感码 → 两个都列出（字典序，便于审计与用例断言稳定）。"""

    exposure = evaluate_self_grant(
        role_id=_SELF_ROLE,
        resulting_codes={"school.org.manage", "school.personnel.sensitive.read"},
        principal_role_ids={_SELF_ROLE},
        principal_permissions=set(),
    )
    assert exposure.newly_reaching_self == (
        "school.org.manage",
        "school.personnel.sensitive.read",
    )
    assert exposure.requires_confirmation is True


def test_code_already_held_is_not_an_expansion() -> None:
    """判据是"净增"：自己已经有这个敏感码时，把它补进自己所在角色没有扩大能力面。"""

    exposure = evaluate_self_grant(
        role_id=_SELF_ROLE,
        resulting_codes={"school.personnel.mfa.reset"},
        principal_role_ids={_SELF_ROLE},
        principal_permissions={"school.personnel.mfa.reset"},
    )
    assert exposure.requires_confirmation is False


def test_retained_codes_in_the_resulting_set_are_never_recounted() -> None:
    """判据只看「最终集 − 已持有」：角色里本来就有、操作者也就本来就有的码不再报一遍。

    这条钉的是入参语义，不是行为差异：`evaluate_self_grant` 没有"本次新增差集"参数，调用方
    只能传写入后的最终集。而当操作者确实绑在这个角色上时，他的持有并集必然已经覆盖该角色的
    既有映射（并集就是从同一批 `role_permissions` 行算出来的），所以"传差集"和"传最终集"在
    一致数据下算出同一个结果——真正的风险是持有集来自**过期快照**（另一条角色绑定刚变过），
    那由写事务内的 `_principal_permission_codes` 重读兜住，不在纯函数这一层。
    """

    exposure = evaluate_self_grant(
        role_id=_SELF_ROLE,
        resulting_codes={"school.personnel.view", "school.org.manage"},
        principal_role_ids={_SELF_ROLE},
        principal_permissions={"school.personnel.view", "school.org.manage"},
    )
    assert exposure.newly_reaching_self == ()
    assert exposure.requires_confirmation is False


def test_multi_role_binding_reaches_self_through_any_held_role() -> None:
    """多角色下"触达自己"= 目标角色出现在操作者的任意一条绑定里（并集语义）。"""

    exposure = evaluate_self_grant(
        role_id=_OTHER_ROLE,
        resulting_codes={"school.role.delete"},
        principal_role_ids={_SELF_ROLE, _OTHER_ROLE},
        principal_permissions=set(),
    )
    assert exposure.newly_reaching_self == ("school.role.delete",)


@pytest.mark.parametrize("role_id", [_SELF_ROLE, _OTHER_ROLE])
def test_empty_resulting_set_is_never_a_self_expansion(role_id: str) -> None:
    """清空权限集（撤权）永远不需要确认——守卫只管"变大"，不能反过来拖慢收权。"""

    exposure = evaluate_self_grant(
        role_id=role_id,
        resulting_codes=set(),
        principal_role_ids={_SELF_ROLE},
        principal_permissions={"school.role.manage"},
    )
    assert exposure.requires_confirmation is False


# --- 打断规则（`app/rbac/admin._assert_self_grant`）：判据说"要不要确认"，这里说"不给就怎样" ---


def _principal(*, is_super: bool = False, permissions: set[str] | None = None) -> Principal:
    now = datetime.now(UTC)
    return Principal(
        user_id=uuid4(),
        realm="tenant",
        tenant_id=uuid4(),
        jti=uuid4(),
        session_id=uuid4(),
        expires_at=now + timedelta(minutes=15),
        must_change_password=False,
        roles=frozenset({"school_super_admin" if is_super else "school_admin"}),
        permissions=frozenset(permissions or set()),
    )


_EXPOSURE = SelfGrantExposure(("school.personnel.sensitive.read",))


def test_self_grant_without_ack_is_rejected_with_the_codes_that_triggered_it() -> None:
    """普通主体未勾选确认 → 422，且消息里点名是哪几个码（前端要能直接展示，不能只说"不合法"）。"""

    with pytest.raises(RoleAdminError) as raised:
        _assert_self_grant(_principal(), _EXPOSURE, self_grant_ack=False, reason="岗位调整")
    assert raised.value.code == "SELF_GRANT_CONFIRMATION_REQUIRED"
    assert raised.value.http_status == 422
    assert "school.personnel.sensitive.read" in raised.value.message


def test_ack_without_reason_is_rejected() -> None:
    """勾了确认但没写理由 → 另一条判据码：两者在前端是两步（勾选 + 填理由），不能混成一个错误。"""

    for reason in (None, "", "   "):
        with pytest.raises(RoleAdminError) as raised:
            _assert_self_grant(_principal(), _EXPOSURE, self_grant_ack=True, reason=reason)
        assert raised.value.code == "SELF_GRANT_REASON_REQUIRED"


def test_ack_with_reason_passes() -> None:
    """确认 + 理由齐备即放行：本点不做审批（M6 双人确认替换的正是这一步）。"""

    _assert_self_grant(_principal(), _EXPOSURE, self_grant_ack=True, reason="代管人事敏感信息核对")


def test_super_admin_skips_the_interrupt_but_the_caller_still_audits() -> None:
    """超管免打断：ack/理由都不校验（否则每次正常配置都要重复勾选，运营会绕道）。

    免的是打断不是留痕——审计由调用方按 `exposure.requires_confirmation` 决定，
    本用例把这个分工钉住：这里不抛，审计仍会在写事务里出现。
    """

    _assert_self_grant(
        _principal(is_super=True), _EXPOSURE, self_grant_ack=False, reason=None
    )


def test_no_exposure_never_interrupts() -> None:
    """判据没命中时不得要求 ack：正常配置流（改别人、非敏感码、净增为零）必须一路通行。"""

    _assert_self_grant(
        _principal(),
        SelfGrantExposure(()),
        self_grant_ack=False,
        reason=None,
    )


def test_factory_defaults_of_editable_roles_hold_no_sensitive_code() -> None:
    """重置出厂是守卫**之外**的第四条写路径：它按出厂默认重种权限，不经过 ack。

    今天它不构成自扩权通道，只因为可编辑内置角色的出厂默认里一个敏感码都没有（矩阵 §4 把
    敏感字段/删除/MFA/组织管理全打成 △，不进默认）。持有 `school.role.manage` 的人把自己所在
    角色的"重置出厂"执行一次就能重写该角色的权限集，所以只要有人往 `school_admin`/`teacher_b_end`
    的默认里加一个敏感码，`reset_role` 立刻变成绕过 TD-191 的野路子——而且静默、无审计。
    这条断言把"只因为"钉成契约：默认集与敏感清单的交集必须为空，否则先给 reset 接守卫再改默认。
    """

    offenders = {
        role.code: sorted(role.permissions & SENSITIVE_SELF_GRANT_CODES)
        for role in TENANT_BUILTIN_ROLES
        if role.code in EDITABLE_ROLE_CODES and role.permissions & SENSITIVE_SELF_GRANT_CODES
    }
    assert offenders == {}, f"可编辑角色的出厂默认含敏感码，reset 会是无守卫的自扩权路径：{offenders}"


# --- 范围版判据（M2-UI3 TD-187c，简报 §13.3）---


def test_sensitive_scope_list_is_a_subset_of_known_scope_types() -> None:
    """范围敏感项清单必须是判据层已知范围类型的子集（拼错即失败，同权限侧那条对称的断言）。"""

    assert SENSITIVE_SCOPE_TYPES <= KNOWN_SCOPE_TYPES


def test_sensitive_scope_list_is_currently_school_only() -> None:
    """TD-203 的防御：敏感范围清单今天就是 `{school}` 单值。

    新增 `ScopeType`（M3 真按部门/课程过滤，或 `course` 开始贡献可见性）时这条会红，逼改的人
    回答"这个新范围是不是又一个覆盖型范围"——不是的话加进 `KNOWN_SCOPE_TYPES` 即可，是的话
    必须同时决定是否进敏感清单，否则"把角色抬成跨部门可见"会走成静默配置。
    """

    assert set(SENSITIVE_SCOPE_TYPES) == {"school"}


def test_scope_upgrade_to_school_reaching_self_requires_confirmation() -> None:
    """把操作者所在角色的范围从 self 抬到 school = 一次自扩权，判据与权限码那条同形。"""

    exposure = evaluate_scope_self_grant(
        role_id=_SELF_ROLE,
        resulting_scopes={"school"},
        principal_role_ids={_SELF_ROLE},
        principal_scopes={"self"},
    )
    assert exposure.newly_reaching_self == ("school",)
    assert exposure.requires_confirmation is True


def test_scope_already_visible_to_the_operator_is_not_an_expansion() -> None:
    """操作者通过别的角色已经看得到全校时，把他所在角色也抬成 school 不放大他的能力面。

    与权限侧同一理由：净增为零是日常配置，不是扩权。
    """

    exposure = evaluate_scope_self_grant(
        role_id=_SELF_ROLE,
        resulting_scopes={"school"},
        principal_role_ids={_SELF_ROLE},
        principal_scopes={"school"},
    )
    assert exposure.requires_confirmation is False


def test_scope_narrowing_never_requires_confirmation() -> None:
    """school → self、以及清空范围（fail-closed 成"什么都看不到"）都不该打断：守卫只管变大。"""

    for resulting in ({"self"}, set(), {"class"}):
        exposure = evaluate_scope_self_grant(
            role_id=_SELF_ROLE,
            resulting_scopes=resulting,
            principal_role_ids={_SELF_ROLE},
            principal_scopes={"school"},
        )
        assert exposure.requires_confirmation is False, resulting


def test_scope_change_on_another_role_does_not_reach_self() -> None:
    """改的是别人所在的角色 → 范围放大不流向操作者本人，与权限侧同一判据 ①。"""

    exposure = evaluate_scope_self_grant(
        role_id=_OTHER_ROLE,
        resulting_scopes={"school"},
        principal_role_ids={_SELF_ROLE},
        principal_scopes=set(),
    )
    assert exposure.requires_confirmation is False


def test_factory_defaults_of_editable_roles_do_hold_the_sensitive_scope() -> None:
    """范围侧的不变量与权限侧**相反**，而且这个相反是有意的：必须钉住它。

    权限侧断言"可编辑角色的出厂默认 ∩ 敏感码 = ∅"，所以 `reset_role` 今天不构成自扩权通道。
    范围侧同一条断言不成立——`school_admin` 的出厂范围本来就是 `school`（矩阵 §2）。于是
    「有人把 school_admin 降级成 self → 另一个持有者执行重置出厂 → 他自己的可见范围从本人抬回
    全校」是一条**不经过 ack** 的抬升路径。它今天靠 reset 自己的逐字确认 + 理由 ≥10 +
    `school.role.reset` 审计兜住，但按 TD-194 已记录的收口范围，抽 helper 时必须把 reset
    接进**范围版**判据，不能只接权限码那条腿。这条用例是那个结论的证据，不是待修的缺陷。
    """

    offenders = {
        role.code: sorted(role.scopes & SENSITIVE_SCOPE_TYPES)
        for role in TENANT_BUILTIN_ROLES
        if role.code in EDITABLE_ROLE_CODES and role.scopes & SENSITIVE_SCOPE_TYPES
    }
    assert offenders == {"school_admin": ["school"]}, (
        f"可编辑角色的出厂默认范围与敏感清单的交集变了：{offenders}；"
        "变成 ∅ 说明 school_admin 的出厂范围被改窄（要回写矩阵 §2 与 design.md §9），"
        "出现新成员说明又多了一条无 ack 的 reset 抬升路径（TD-194 的 reset 接线范围要扩）"
    )
