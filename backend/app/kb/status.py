"""KB 文档版本状态机（M3 KB-01 扫描两跳 + KB-02 审批结论 + KB-03A 构建三态）。

口径（`docs/state-machines.md` §3）：`UPLOADED` 收到 ClamAV 结论后，通过 → `PENDING_APPROVAL`，
感染/完整性失败 → `SCAN_FAILED`；瞬时扫描故障**不迁移**（保持 UPLOADED 等重试）。
KB-02 起：`PENDING_APPROVAL` 经审批迁移到 `APPROVED` / `REJECTED`；构建门禁
`assert_build_allowed` 只认可 `APPROVED`——未审批创建构建是 KB-02 的 409 依据。
KB-03A 起：`APPROVED → BUILDING → READY/FAILED`，`FAILED → BUILDING` 是重试
（`assert_retriable` 只有 FAILED 可重试）；READY 的内容变更走 KB-05 再上传新版本，不出边。
"""

KB_STATUS_UPLOADED = "UPLOADED"
KB_STATUS_PENDING_APPROVAL = "PENDING_APPROVAL"
KB_STATUS_SCAN_FAILED = "SCAN_FAILED"
KB_STATUS_APPROVED = "APPROVED"
KB_STATUS_REJECTED = "REJECTED"
KB_STATUS_BUILDING = "BUILDING"
KB_STATUS_READY = "READY"
KB_STATUS_FAILED = "FAILED"

_TERMINAL_IN_THIS_POINT: frozenset[str] = frozenset()
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    KB_STATUS_UPLOADED: frozenset({KB_STATUS_PENDING_APPROVAL, KB_STATUS_SCAN_FAILED}),
    KB_STATUS_PENDING_APPROVAL: frozenset({KB_STATUS_APPROVED, KB_STATUS_REJECTED}),
    KB_STATUS_SCAN_FAILED: _TERMINAL_IN_THIS_POINT,
    KB_STATUS_APPROVED: frozenset({KB_STATUS_BUILDING}),
    KB_STATUS_REJECTED: _TERMINAL_IN_THIS_POINT,
    # FAILED → BUILDING 是重试（retry 端点 attempt+1）；READY 是本轮构建的终态
    # （「修改内容生成新版本」走 KB-05 的再上传路径，不出边）。
    KB_STATUS_BUILDING: frozenset({KB_STATUS_READY, KB_STATUS_FAILED}),
    KB_STATUS_FAILED: frozenset({KB_STATUS_BUILDING}),
    KB_STATUS_READY: _TERMINAL_IN_THIS_POINT,
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


class NotRetriableError(Exception):
    """当前构建任务不可重试（只有 FAILED 可重试；READY 不需要，QUEUED/BUILDING 在跑）。

    KB-03A 的 409 依据（`school_kb.py` 的 retry 端点）。
    """

    code = "KB_BUILD_NOT_RETRIABLE"


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
    """断言版本可创建构建任务（只有审批通过可**首次**构建）。

    READY/FAILED 的重建走 retry 端点（`assert_retriable`）；BUILDING 有活跃任务；
    其余状态未审批。

    异常:
        NotBuildableError: 非 APPROVED（PENDING_APPROVAL/REJECTED/UPLOADED/SCAN_FAILED/
            BUILDING/READY/FAILED/未知状态）。
    """

    if status != KB_STATUS_APPROVED:
        raise NotBuildableError(f"版本状态 {status} 不可创建构建任务（必须先审批通过）")


def assert_retriable(job_status: str) -> None:
    """断言构建任务可重试（只有 FAILED 可重试）。

    异常:
        NotRetriableError: BUILD_QUEUED/BUILDING 在跑或已就绪（READY）、未知状态。
    """

    if job_status != KB_STATUS_FAILED:
        raise NotRetriableError(
            f"构建任务状态 {job_status} 不可重试（只有失败的构建可以重试）"
        )
