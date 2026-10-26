"""权限项目录（M1-E1）。

口径（`docs/rbac-matrix.md` v1.2 §3 + `docs/design-briefs/M1-E1-rbac-core.md` §3.1-A）：

- **本文件是"系统里存在哪些权限项"的唯一事实来源**：矩阵 §3 与它必须逐码相等，
  由 `backend/tests/test_rbac_matrix_unit.py` 钉住；
- 权限项目录跟代码走，**不是运营可随意增删的配置**：凭空加一个没有对应服务端校验点的码
  = 界面能勾、后端不生效的假开关。角色↔权限映射与数据范围才是库里的配置（见 `seeds.py`）；
- `module`/`action` 由码本身派生（不另写第二份标注），与 `permissions` 表列一致：
  `module` 决定 realm 守卫（`platform.*` 不得进租户角色，DB 触发器 `role_permissions_realm_guard`），
  也决定超管"本校全量"的范围（`provision_tenant()` 按 `module IN ('school','campus')` 授权）。

注意：`app/db/models/rbac.py:PERMISSION_CATALOG` 是 M1-A 迁移 `779f731db342` 的**历史种子输入**
（已入库的迁移不改），其中 9 个与矩阵重复的旧码由 M1-E1 迁移删除；现行目录以本文件为准。
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PermissionDef:
    """一个权限项：码、模块（realm 守卫与范围过滤依据）、动作、用途说明。"""

    code: str
    module: str
    action: str
    description: str


def _entry(code: str, description: str) -> PermissionDef:
    """由码派生 module/action，避免码与标注两份真相漂移。"""

    return PermissionDef(
        code=code,
        module=code.split(".", 1)[0],
        action=code.rsplit(".", 1)[1],
        description=description,
    )


# (码, 说明) —— 与 docs/rbac-matrix.md §3 逐条对应
_DEFINITIONS: tuple[tuple[str, str], ...] = (
    # §3.1 平台
    ("platform.tenant.view", "查看租户与开通审核记录"),
    ("platform.tenant.review", "审核通过/拒绝学校开通申请"),
    ("platform.tenant.activate", "开通/暂停租户"),
    ("platform.tenant.export", "导出开通审核记录"),
    ("platform.config.view", "查看平台配置（密钥只回掩码）"),
    ("platform.config.edit", "编辑平台配置（含模型密钥）"),
    ("platform.config.approve", "审批平台配置变更"),
    ("platform.audit.view", "查看平台侧审计"),
    ("platform.audit.export", "导出平台侧审计"),
    ("platform.message.view", "查看平台消息"),
    ("platform.message.send", "发送平台消息"),
    ("platform.cross_tenant.read", "跨租只读（只读事务+必填理由+强制审计；工单化二次授权延后 TD-153）"),
    # §3.2 校方
    ("school.dashboard.view", "查看校方基础看板"),
    ("school.feedback.view", "查看反馈列表与详情"),
    ("school.feedback.update", "标记反馈处理状态/答复反馈"),
    ("school.org.manage", "维护组织结构（部门树/班级/课程/任教关系）"),
    ("school.personnel.view", "查看人员档案（按数据范围）"),
    ("school.personnel.create", "新建人员档案"),
    ("school.personnel.edit", "修改人员档案"),
    ("school.personnel.delete", "删除人员档案"),
    ("school.personnel.import", "批量导入人员档案"),
    ("school.personnel.sensitive.read", "完整查看身份证等敏感字段（需理由并审计）"),
    ("school.personnel.mfa.reset", "重置用户 MFA 绑定（需理由并审计）"),
    ("school.permission_account.manage", "维护权限账号（开/停 B 端权限账号）"),
    ("school.role.manage", "维护角色与权限分配"),
    ("school.role.delete", "删除自定义角色（与配置权限分离，M2-UI3）"),
    ("school.kb.view", "查看知识库文档与构建进度"),
    ("school.kb.upload", "上传知识库文档（上传者不得自批）"),
    ("school.kb.approval", "审批知识库文档版本"),
    ("school.kb.build", "触发解析/切块/索引构建与重试"),
    ("school.kb.test", "使用测试沙箱与测试集"),
    ("school.kb.publish", "发布/回滚/下线知识库版本"),
    ("school.kb.delete", "删除知识库文档"),
    ("school.kb.view_chunks", "预览分块与执行检索测试"),
    ("school.kb.acl.manage", "配置并确认文档查阅人群（SC-01 K4）"),
    ("school.kb.chunk.edit", "提交分块编辑（SC-01 K5）"),
    ("school.kb.chunk.confirm", "确认分块编辑并派生新版本（SC-01 K5，仅超管）"),
    ("school.kb.retrieval_test.unfiltered", "沙箱忽略 ACL 的对比检索（SC-01 K4，仅超管，强制审计）"),
    ("school.approval.todo", "查看待办与审批列表"),
    ("school.approval.leave", "审批请假申请"),
    ("school.approval.account_termination", "审批账号删除/撤权申请"),
    ("school.message.view", "查看校方消息"),
    ("school.message.send", "发送校方消息"),
    ("school.message.read", "标记校方消息已读"),
    ("school.audit.view", "查看本校审计事件"),
    ("school.account_termination.request", "替本校用户发起撤权/删除申请"),
    ("school.account_termination.appeal", "处理撤权申诉"),
    ("school.account_termination.freeze", "冻结/解冻待处理账号"),
    # §3.3 C 端
    ("campus.chat.use", "使用 AI 对话"),
    ("campus.feedback.create", "提交反馈"),
    ("campus.conversation.clear", "清空当前会话"),
    ("campus.message.view", "查看消息（系统/审批/师生 1:1）"),
    ("campus.message.send", "发送消息"),
    ("campus.leave.create", "提交请假申请"),
    ("campus.leave.withdraw", "撤回本人请假申请"),
    ("campus.account_termination.request", "申请删除本人账号"),
    ("campus.nav", "使用业务入口导航"),
    # §3.4 审计/观测（审计查看/导出用 realm 专用码，不设无 realm 的通用码）
    ("observability.trace.view", "查看本地 trace"),
    ("observability.langfuse.access", "访问 Langfuse"),
    ("eval.run", "运行 AI 评估"),
    ("eval.report.view", "查看评估报告"),
)

PERMISSION_CATALOG: tuple[PermissionDef, ...] = tuple(
    _entry(code, description) for code, description in _DEFINITIONS
)

_CATALOG_BY_CODE: dict[str, PermissionDef] = {entry.code: entry for entry in PERMISSION_CATALOG}
_CODES_BY_MODULE: dict[str, frozenset[str]] = {
    module: frozenset(
        entry.code for entry in PERMISSION_CATALOG if entry.module == module
    )
    for module in {entry.module for entry in PERMISSION_CATALOG}
}


def catalog_codes() -> frozenset[str]:
    """目录内全部权限码。"""

    return frozenset(_CATALOG_BY_CODE)


def permission_def(code: str) -> PermissionDef | None:
    """按码取权限项定义（未登记返回 None）。"""

    return _CATALOG_BY_CODE.get(code)


def codes_with_module(module: str) -> frozenset[str]:
    """某模块（码前缀）下的全部权限码。"""

    return _CODES_BY_MODULE.get(module, frozenset())


# realm 的唯一口径（矩阵 §3.4/§5-11 + §4 定稿脚注 2026-10-23）：`school`/`campus` 属于租户可授予
# 范围，其余模块（platform/observability/eval）都是平台运维域——`observability.*`、`eval.*`
# 虽不带 `platform.` 前缀，也**不下放租户角色**（用户确认：校方要看评估结论由平台侧出报告；
# 将来真要自评就新增 `school.eval.*` 专用码，不改这两个码的归属）。
# 只靠 DB 触发器挡 `module='platform'` 不足以覆盖这两类，所以 realm 判定与清单测试都以这里为准。
TENANT_MODULES: frozenset[str] = frozenset({"school", "campus"})


def code_realm(code: str) -> str:
    """权限码的归属 realm：`tenant` | `platform`。

    异常:
        KeyError：码不在目录（清单测试据此拒绝幽灵码）。
    """

    entry = _CATALOG_BY_CODE[code]
    return "tenant" if entry.module in TENANT_MODULES else "platform"


def legacy_codes_removed_by_m1e1() -> tuple[str, ...]:
    """M1-A 目录里与矩阵重复、M1-E1 迁移要从库里删除的旧码（口径 ⑧，共 9 条）。"""

    return (
        "school.profile.view",
        "school.profile.edit",
        "school.user.view",
        "school.user.create",
        "school.user.edit",
        "school.user.delete",
        "school.role.assign",
        "school.leave.approve",
        "campus.leave.submit",
    )
