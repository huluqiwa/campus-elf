"""内置角色与出厂默认权限映射（M1-E1）。

口径（`docs/rbac-matrix.md` v1.2 §1/§4 + `docs/design-briefs/M1-E1-rbac-core.md` §3.1/§5.1）：

- 这里是**出厂默认**（新租户开通时由 `seed_builtin_roles()` 种一次；存量租户见
  `FULL_SCOPE_ROLES` 的两种补授口径），不是运行时的最终真相：
  角色↔权限映射入库后属于运营配置，可以增删改并热更新（下一次请求生效）；
  同步逻辑**绝不覆盖、也绝不恢复人工改动**（`app/rbac/__init__.py:sync_rbac_definitions`）。
- 每个码都能在这里找到矩阵 §4 的出处；矩阵打 △ 的项**不进默认**（可授予，但需显式操作）。
- 租户角色不得引用 `platform.*`（口径与 DB 触发器、启动自检一致，`app/rbac/__init__.py` 会校验）。
"""

from dataclasses import dataclass
from typing import Literal

from app.rbac.catalog import catalog_codes, codes_with_module

Realm = Literal["platform", "tenant"]


@dataclass(frozen=True, slots=True)
class BuiltinRole:
    """内置（系统）角色：码、显示名、所属 realm、出厂默认权限集。"""

    code: str
    name: str
    realm: Realm
    permissions: frozenset[str]


# 矩阵 §4 列名顺序：平台超管 | 平台运营 | 平台审计
# 平台超管持有目录全量（rbac-matrix.md §5-11：平台是超集运营角色，平台路由只接受平台主体）
PLATFORM_BUILTIN_ROLES: tuple[BuiltinRole, ...] = (
    BuiltinRole(
        code="platform_super_admin",
        name="SaaS 超管",
        realm="platform",
        permissions=catalog_codes(),
    ),
    BuiltinRole(
        code="platform_operator",
        name="SaaS 运营",
        realm="platform",
        # §4「平台租户审核」√ +「平台配置」△（只给 view，不给 edit/approve：口径 4）
        # +「平台消息」√；「平台审计」—（口径 5：审计导出不给运营）
        permissions=frozenset(
            {
                "platform.tenant.view",
                "platform.tenant.review",
                "platform.tenant.activate",
                "platform.tenant.export",
                "platform.config.view",
                "platform.message.view",
                "platform.message.send",
            }
        ),
    ),
    BuiltinRole(
        code="platform_auditor",
        name="SaaS 审计",
        realm="platform",
        # §4「平台审计」√、「平台租户审核」—（只读租户状态，不给 review/activate）
        permissions=frozenset(
            {"platform.tenant.view", "platform.audit.view", "platform.audit.export"}
        ),
    ),
)


# 矩阵 §1.2「校方超管：本校全部权限」+ §4 定稿脚注（2026-10-23），实现上与 provision_tenant()
# 的 `module IN ('school','campus')` 同一语义；observability/eval 属平台运维域，不给租户。
TENANT_SUPER_ADMIN_PERMISSIONS: frozenset[str] = codes_with_module("school") | codes_with_module(
    "campus"
)

# 矩阵 §4 列名顺序：校方超管 | 校方管理员 | 教师 B 端 | 学生 C 端 | 教师 C 端
TENANT_BUILTIN_ROLES: tuple[BuiltinRole, ...] = (
    BuiltinRole(
        code="school_super_admin",
        name="校方超管",
        realm="tenant",
        permissions=TENANT_SUPER_ADMIN_PERMISSIONS,
    ),
    BuiltinRole(
        code="school_admin",
        name="校方授权管理员",
        realm="tenant",
        # §4 打 √ 的行：校方看板、人员查看、KB 查看/构建进度、校方消息。
        # 打 △ 的行（反馈答复、人员增删改/导入、部门树、敏感字段、权限账号/角色、
        # KB 上传/审批/构建/测试/发布/分块/ACL/块编辑、审批待办、校方审计）一律不进默认。
        permissions=frozenset(
            {
                "school.dashboard.view",
                "school.personnel.view",
                "school.kb.view",
                "school.message.view",
                "school.message.send",
                "school.message.read",
            }
        ),
    ),
    BuiltinRole(
        code="teacher_b_end",
        name="教师（B 端权限）",
        realm="tenant",
        # 口径 ③（改后）：持后台账号即可上传知识库（§4 KB 上传行教师 B 端 √）。
        # 请假审批（§6-2）与 KB 查看/构建/测试/分块（△）需超管显式授予。
        permissions=frozenset({"school.kb.upload"}),
    ),
    BuiltinRole(
        code="student",
        name="学生（C 端）",
        realm="tenant",
        # §3.3 全部 campus 码：对话、反馈、清会话、消息、请假、自助删除、导航
        permissions=codes_with_module("campus"),
    ),
    BuiltinRole(
        code="teacher",
        name="教师（C 端）",
        realm="tenant",
        # §4「C 端请假」教师 C 端列为 —：不默认给请假提交/撤回；其余同学生
        permissions=codes_with_module("campus")
        - frozenset({"campus.leave.create", "campus.leave.withdraw"}),
    ),
)

_ROLES_BY_REALM: dict[str, tuple[BuiltinRole, ...]] = {
    "platform": PLATFORM_BUILTIN_ROLES,
    "tenant": TENANT_BUILTIN_ROLES,
}

# 「全量型」角色：矩阵把它们**定义**为「某范围内的全部权限」（§1.2 + §4 脚注的校方超管、§5-11 的平台超管），
# 所以同步对它们补齐缺失项（只补不撤）——否则新增一个权限码后，超管连给自己授权的码都看不到。
# 其余内置角色的默认只在「角色还没被配置过」时授一次（见 `app/rbac/__init__.py`），
# 因为那类默认是运营可增删改的配置，人工撤销的权限不能被同步悄悄加回来。
FULL_SCOPE_ROLES: frozenset[tuple[str, str]] = frozenset(
    {("tenant", "school_super_admin"), ("platform", "platform_super_admin")}
)


def builtin_roles(realm: Realm) -> tuple[BuiltinRole, ...]:
    """某 realm 的内置角色（平台 3 个 / 租户 5 个）。"""

    return _ROLES_BY_REALM[realm]


def builtin_role(realm: Realm, code: str) -> BuiltinRole:
    """按 realm + 码取内置角色。

    异常:
        KeyError：码不在内置角色清单（自定义角色不属于本模块）。
    """

    for role in _ROLES_BY_REALM[realm]:
        if role.code == code:
            return role
    raise KeyError(f"非内置角色：{realm}/{code}")
