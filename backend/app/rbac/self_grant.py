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

**两个宇宙，一套判据**（M2-UI3 TD-187c，简报 §13.3）：同一个判据也管数据范围——把操作者所在角色
从 `self` 抬成 `school` 与补一个敏感码是同一件事（他能查到的行集变大）。范围侧走
`evaluate_scope_self_grant`，敏感项清单是 `SENSITIVE_SCOPE_TYPES`，与权限码清单分开维护。

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


# 敏感范围清单（M2-UI3 TD-187c，简报 §13.3）：范围放大与补一个敏感码在效果上是同一件事——
# 持有人能看到的数据集变大。`school` 是唯一的敏感项，因为它是 `scopes.py:sees_whole_school`
# 里那个"覆盖其余一切"的类型。`self` 不入清单：∅ → `self` 确实也是扩大，但扩大出来的那一份
# 只有持有人自己的记录，够不到任何别人的数据，问一次 ack 只会淹没真信号。`class`/`course`
# 压根不可经角色配置写入（本端点没有 `scope_id` 通道，见 `admin.py:_SCOPE_TYPE_OWNERSHIP`），
# 所以清单不需要为它们决定什么。清单变化由 `tests/test_self_grant_guard_unit.py` 两条断言盯着：
# 与 `KNOWN_SCOPE_TYPES` 的包含关系，以及"今天仍是单值"（新增覆盖型范围类型时必须回来重新决定，
# TD-203）。
SENSITIVE_SCOPE_TYPES: frozenset[str] = frozenset({"school"})


def evaluate_scope_self_grant(
    *,
    role_id: str,
    resulting_scopes: set[str] | frozenset[str],
    principal_role_ids: set[str] | frozenset[str],
    principal_scopes: set[str] | frozenset[str],
) -> SelfGrantExposure:
    """范围版的自扩权判据——**同一个内核，换了宇宙**。

    `evaluate_self_grant` 的三条判据（触达本人、结果集、净增）与"集合里装的是权限码还是范围类型"
    无关，所以这里不复制一份判据，只把参数映射过去：`resulting_codes` ← 结果范围类型集、
    `principal_permissions` ← 操作者当前生效的范围类型并集、`sensitive_codes` ← 敏感范围清单。

    为什么不干脆让调用方直接传 `evaluate_self_grant`：那样读代码的人会以为范围替换在用权限码
    清单做判定，而这两张清单的变更节奏完全不同（加一个敏感码 vs 加一种范围类型）。
    返回结构的字段名 `newly_reaching_self` 保持通用（元组里是范围类型时读作"新触达本人的范围"）。
    """

    return evaluate_self_grant(
        role_id=role_id,
        resulting_codes=resulting_scopes,
        principal_role_ids=principal_role_ids,
        principal_permissions=principal_scopes,
        sensitive_codes=SENSITIVE_SCOPE_TYPES,
    )
