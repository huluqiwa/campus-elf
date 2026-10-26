"""自扩权显式确认（M2-UI3，TD-191 收口）。

要解决的问题（`docs/design-briefs/M2-UI3-custom-roles-accounts.md` §6）：持有 `school.role.manage`
的校方管理员可以把任意敏感码写进**自己所在**的角色，或直接新建一个含敏感码的角色绑到自己身上。
M2-UI2 只封住了 `role.manage` 这一个码的转授，没有封住这条。

本期口径（用户 2026-10-26 决策：不硬封，改为**显式化**）：

- 「触达自己」且「新增的是自己当前没有的敏感码」→ 必须带 `self_grant_ack=true` + 理由，否则 422；
- 无论是否超管主体，只要结果触达该判据就写一条 `school.role.self_grant` 审计——
  超管免的是打断，不是免留痕；
- 完整硬封（只能授自己已有的码）与双人确认一起留给 M6，见 TD-191。

判据为什么是「自己当前没有」而不是「角色里出现了敏感码」：管理员本就持有敏感码时，把同一个码
补给自己所在角色并没有扩大他的能力面，那是日常配置；只有**净增**才是扩权。这条差集让守卫不挡
正常流，同时保证任何一次"能力变大"都留下可复盘的审计。

本模块只做纯判定，SQL 与事务由调用方负责（同 `app/rbac/scopes.py` 的分层）。
"""

from dataclasses import dataclass

# 敏感码清单：一旦进入"净增即扩权"的判定集合，就意味着这些码不再能被静默授予自己所在角色。
# 与 `catalog.py` 的字面码一一对应；新增敏感能力时**必须**同时决定是否进这张表，
# 由 `tests/test_self_grant_guard_unit.py` 断言这里的每个码都存在于权限目录（拼错即失败）。
SENSITIVE_SELF_GRANT_CODES: frozenset[str] = frozenset(
    {
        "school.role.manage",
        "school.role.delete",
        "school.personnel.sensitive.read",
        "school.personnel.delete",
        "school.personnel.mfa.reset",
        "school.org.manage",
    }
)


@dataclass(frozen=True, slots=True)
class SelfGrantExposure:
    """一次写操作的自扩权判定结果。"""

    #: 会经由目标角色流到操作者本人、且他当前**没有**的敏感码（按字典序，便于断言与审计）。
    newly_reaching_self: tuple[str, ...]

    @property
    def requires_confirmation(self) -> bool:
        """只有"净增且触达自己"才需要显式确认。"""

        return bool(self.newly_reaching_self)


def evaluate_self_grant(
    *,
    role_id: str,
    resulting_codes: set[str] | frozenset[str],
    principal_role_ids: set[str] | frozenset[str],
    principal_permissions: set[str] | frozenset[str],
    sensitive_codes: set[str] | frozenset[str] = SENSITIVE_SELF_GRANT_CODES,
) -> SelfGrantExposure:
    """判定「这次提交会不会让操作者本人凭空多出敏感能力」。

    参数:
        role_id: 被写入权限集的角色 id。
        resulting_codes: 写入后该角色的**最终**权限码集合（全量替换语义，不是差集）。
        principal_role_ids: 操作者当前绑定的角色 id 集合（判定"是否触达自己"）。
        principal_permissions: 操作者当前生效的权限码并集（判定"净增"）。
        sensitive_codes: 敏感码清单；测试里可注入窄集合，生产调用一律用默认值。

    返回值:
        SelfGrantExposure: 净增且触达自己的敏感码；空元组表示不需要确认。
    """

    if role_id not in {str(item) for item in principal_role_ids}:
        # 改的是别人所在的角色：能力面没有流向操作者本人，不属于自扩权。
        return SelfGrantExposure(())
    granted = {str(code) for code in resulting_codes}
    held = {str(code) for code in principal_permissions}
    newly = sorted((granted & set(sensitive_codes)) - held)
    return SelfGrantExposure(tuple(newly))
