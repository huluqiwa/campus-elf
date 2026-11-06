"""KB 文档版本状态机（M3 KB-01 扫描两跳 + KB-02 审批结论与构建门禁）。

口径（`docs/state-machines.md` §3）：`UPLOADED` 收到 ClamAV 结论后，通过 → `PENDING_APPROVAL`，
感染/完整性失败 → `SCAN_FAILED`；瞬时扫描故障**不迁移**（保持 UPLOADED 等重试）。
KB-02 起：`PENDING_APPROVAL` 经审批迁移到 `APPROVED` / `REJECTED`（本点两者都是终态，
「已发布修改正文 → 新版本」等后续出边随对应功能点扩展）；构建门禁
`assert_build_allowed` 只认可 `APPROVED`——未审批创建构建是 KB-02 的 409 依据
（AGENTS.md §4 知识库门禁）。
"""

KB_STATUS_UPLOADED = "UPLOADED"
KB_STATUS_PENDING_APPROVAL = "PENDING_APPROVAL"
KB_STATUS_SCAN_FAILED = "SCAN_FAILED"
KB_STATUS_APPROVED = "APPROVED"
KB_STATUS_REJECTED = "REJECTED"

_TERMINAL_IN_THIS_POINT: frozenset[str] = frozenset()
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    KB_STATUS_UPLOADED: frozenset({KB_STATUS_PENDING_APPROVAL, KB_STATUS_SCAN_FAILED}),
    KB_STATUS_PENDING_APPROVAL: frozenset({KB_STATUS_APPROVED, KB_STATUS_REJECTED}),
    KB_STATUS_SCAN_FAILED: _TERMINAL_IN_THIS_POINT,
    KB_STATUS_APPROVED: _TERMINAL_IN_THIS_POINT,
    KB_STATUS_REJECTED: _TERMINAL_IN_THIS_POINT,
}


class InvalidStatusTransition(Exception):
    """状态机非法迁移（调用方应视为编程错误或并发冲突）。"""

    code = "INVALID_STATUS_TRANSITION"


class NotSubmittableError(Exception):
    """当前版本状态不可提交审批（KB-02 的 409 依据）。"""

    code = "KB_NOT_SUBMITTABLE"


class NotBuildableError(Exception):
    """当前版本状态不可创建构建任务（未审批 409 的依据，AGENTS.md §4）。"""

    code = "KB_NOT_APPROVED"


def can_transition(current: str, target: str) -> bool:
    """判断迁移是否合法（未知状态一律拒绝）。"""

    return target in _ALLOWED_TRANSITIONS.get(current, _TERMINAL_IN_THIS_POINT)


def assert_transition(current: str, target: str) -> None:
    """断言迁移合法。

    异常:
        InvalidStatusTransition: 非法迁移或未知状态。
    """

    if not can_transition(current, target):
        raise InvalidStatusTransition(f"知识库状态不允许从 {current} 迁移到 {target}")


def assert_submittable(status: str) -> None:
    """断言版本可提交审批（扫描通过且等待审批）。

    异常:
        NotSubmittableError: 未扫描通过（UPLOADED/SCAN_FAILED/未知状态）。
    """

    if status != KB_STATUS_PENDING_APPROVAL:
        raise NotSubmittableError(f"版本状态 {status} 不可提交审批（必须先通过扫描）")


def assert_build_allowed(status: str) -> None:
    """断言版本可创建构建任务（只有审批通过可构建）。

    异常:
        NotBuildableError: 未审批（PENDING_APPROVAL/REJECTED/UPLOADED/SCAN_FAILED/未知状态）。
    """

    if status != KB_STATUS_APPROVED:
        raise NotBuildableError(f"版本状态 {status} 不可创建构建任务（必须先审批通过）")
