"""KB 文档版本状态机（M3 KB-01 扫描两跳 + KB-02 审批结论 + KB-03A 构建三态 + KB-05 发布三态）。

口径（`docs/state-machines.md` §3）：`UPLOADED` 收到 ClamAV 结论后，通过 → `PENDING_APPROVAL`，
感染/完整性失败 → `SCAN_FAILED`；瞬时扫描故障**不迁移**（保持 UPLOADED 等重试）。
KB-02 起：`PENDING_APPROVAL` 经审批迁移到 `APPROVED` / `REJECTED`；构建门禁
`assert_build_allowed` 只认可 `APPROVED`——未审批创建构建是 KB-02 的 409 依据。
KB-03A 起：`APPROVED → BUILDING → READY/FAILED`，`FAILED → BUILDING` 是重试；
KB-04A 起：`READY/TEST_* → TESTING → TEST_PASSED/TEST_FAILED`
（`assert_retriable` 只有 FAILED 可重试）；READY 的内容变更走 KB-05 再上传新版本，不出边。
KB-05 起：`TEST_PASSED → PUBLISHED`（发布，`assert_publishable`）；
`PUBLISHED → OFFLINE / DELETED`（下线/删除软删，`assert_offline_allowed`/`assert_deletable`，
回滚目标守卫 `assert_rollback_target` 同认 PUBLISHED）；OFFLINE/DELETED 是终态
（重发布走新版本，无出边）；发布/回滚只切 alias 不改分块（不变量见 state-machines §3）。
"""

KB_STATUS_UPLOADED = "UPLOADED"
KB_STATUS_PENDING_APPROVAL = "PENDING_APPROVAL"
KB_STATUS_SCAN_FAILED = "SCAN_FAILED"
KB_STATUS_APPROVED = "APPROVED"
KB_STATUS_REJECTED = "REJECTED"
KB_STATUS_BUILDING = "BUILDING"
KB_STATUS_READY = "READY"
KB_STATUS_FAILED = "FAILED"
KB_STATUS_TESTING = "TESTING"
KB_STATUS_TEST_PASSED = "TEST_PASSED"
KB_STATUS_TEST_FAILED = "TEST_FAILED"
KB_STATUS_PUBLISHED = "PUBLISHED"
KB_STATUS_OFFLINE = "OFFLINE"
KB_STATUS_DELETED = "DELETED"

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
    # KB-04A：READY → TESTING → TEST_PASSED/TEST_FAILED；TEST_* → TESTING（重跑）
    KB_STATUS_READY: frozenset({KB_STATUS_TESTING}),
    KB_STATUS_TESTING: frozenset({KB_STATUS_TEST_PASSED, KB_STATUS_TEST_FAILED}),
    # KB-05：TEST_PASSED → TESTING（重跑）或 PUBLISHED（发布）；发布后不可再测试
    # （修改内容走新版本）。
    KB_STATUS_TEST_PASSED: frozenset({KB_STATUS_TESTING, KB_STATUS_PUBLISHED}),
    KB_STATUS_TEST_FAILED: frozenset({KB_STATUS_TESTING}),
    # KB-05：PUBLISHED 只能下线（OFFLINE）或软删（DELETED）；两者均为终态。
    KB_STATUS_PUBLISHED: frozenset({KB_STATUS_OFFLINE, KB_STATUS_DELETED}),
    KB_STATUS_OFFLINE: _TERMINAL_IN_THIS_POINT,
    KB_STATUS_DELETED: _TERMINAL_IN_THIS_POINT,
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


class NotTestableError(Exception):
    """当前版本状态不可发起测试（KB-04A 的 409 依据）。"""

    code = "KB_NOT_TESTABLE"


class NotRetriableError(Exception):
    """当前构建任务不可重试（只有 FAILED 可重试；READY 不需要，QUEUED/BUILDING 在跑）。

    KB-03A 的 409 依据（`school_kb.py` 的 retry 端点）。
    """

    code = "KB_BUILD_NOT_RETRIABLE"


class NotPublishableError(Exception):
    """当前版本状态不可发布（只有 TEST_PASSED 可发布）。

    KB-05 的 409 依据（`KB_NOT_TEST_PASSED`）。
    """

    code = "KB_NOT_TEST_PASSED"


class NotPublishedError(Exception):
    """当前版本状态不满足下线/删除/回滚目标要求（必须 PUBLISHED）。

    KB-05 的 409 依据（`KB_NOT_PUBLISHED`）。
    """

    code = "KB_NOT_PUBLISHED"


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


def assert_test_allowed(status: str) -> None:
    """断言版本可发起测试（READY / TEST_FAILED / TEST_PASSED 可测试/重跑）。

    异常:
        NotTestableError: 其他状态（APPROVED 未构建、BUILDING 构建中、TESTING 已在测等）。
    """

    if status not in (KB_STATUS_READY, KB_STATUS_TEST_FAILED, KB_STATUS_TEST_PASSED):
        raise NotTestableError(f"版本状态 {status} 不可发起测试（须先构建到 READY）")


def assert_retriable(job_status: str) -> None:
    """断言构建任务可重试（只有 FAILED 可重试）。

    异常:
        NotRetriableError: BUILD_QUEUED/BUILDING 在跑或已就绪（READY）、未知状态。
    """

    if job_status != KB_STATUS_FAILED:
        raise NotRetriableError(
            f"构建任务状态 {job_status} 不可重试（只有失败的构建可以重试）"
        )


def assert_publishable(status: str) -> None:
    """断言版本可发布（只有 TEST_PASSED 可发布；已发布的幂等重放在服务层处理）。

    异常:
        NotPublishableError: 其他状态（未过测试、已发布/下线/删除、未知状态）。
    """

    if status != KB_STATUS_TEST_PASSED:
        raise NotPublishableError(f"版本状态 {status} 不可发布（必须先通过测试）")


def assert_offline_allowed(status: str) -> None:
    """断言版本可下线（只有 PUBLISHED 可下线）。

    异常:
        NotPublishedError: 其他状态。
    """

    if status != KB_STATUS_PUBLISHED:
        raise NotPublishedError(f"版本状态 {status} 不可下线（只有 PUBLISHED 可下线）")


def assert_deletable(status: str) -> None:
    """断言版本可删除（只有 PUBLISHED 可软删；状态机唯一删除出边）。

    异常:
        NotPublishedError: 其他状态。
    """

    if status != KB_STATUS_PUBLISHED:
        raise NotPublishedError(f"版本状态 {status} 不可删除（只有 PUBLISHED 可删除）")


def assert_rollback_target(status: str) -> None:
    """断言回滚目标版本状态合法（D2-A：切到任意 PUBLISHED 目标）。

    异常:
        NotPublishedError: 非 PUBLISHED。
    """

    if status != KB_STATUS_PUBLISHED:
        raise NotPublishedError(f"回滚目标状态 {status} 非法（目标必须是 PUBLISHED）")
