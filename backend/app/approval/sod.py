"""审批职责分离（RBAC-03）判定。

口径：`docs/spec.md` §3.4「提交人不能审批自己提交的事项；超管例外默认关闭，开启需理由+审计」；
本期真实落点是平台审核学校注册（简报 §0 ⑦），M3/M4 的请假与 KB 审批复用同一判定。

为什么还要一道数据库 `CHECK`：本模块只在应用层判定，例外开启时会主动放行；
`school_review_records.submitted_by_platform_user_id <> reviewer_id` 那道约束挡的是
**没走这条路径的直写**（手工 SQL、将来漏改的调用点）。两道锁的分工写进矩阵 §5-4，
避免被读成"数据库已经保证了，所以应用层可以省"。
"""

from dataclasses import dataclass
from uuid import UUID

REASON_MAX_LENGTH = 500


class ApprovalSeparationError(Exception):
    """分权校验失败（带机器可读码与建议 HTTP 状态码，供接口层映射）。"""

    def __init__(self, code: str, message: str, http_status: int) -> None:
        """初始化错误。

        参数:
            code: 机器可读错误码（进审计，不进响应体以外的地方）。
            message: 面向用户的中文说明（不回显任何用户 id）。
            http_status: 建议的 HTTP 状态码。
        """

        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class SelfApprovalOutcome:
    """分权判定结果。

    参数:
        allowed: 是否允许继续审批。
        used_exception: 是否用掉了「超管自批例外」——为 True 时调用方必须写审计并通知平台审计。
        code: 机器可读结论码（进审计）。
    """

    allowed: bool
    used_exception: bool
    code: str


def evaluate_self_approval(
    *,
    submitter_id: UUID | None,
    reviewer_id: UUID,
    exception_enabled: bool,
    reason: str | None,
) -> SelfApprovalOutcome:
    """判定「审核人 == 提交人」这一笔审批能不能放行。

    参数:
        submitter_id: 提交该事项的平台账号 id；None 表示没有平台侧提交人
            （校方自助提交，此时不存在自批问题）。
        reviewer_id: 当前审核人 id。
        exception_enabled: 系统设置里的自批例外开关。
        reason: 本次审批填写的理由（例外时必须非空）。

    返回值:
        `SelfApprovalOutcome`；无冲突时 `used_exception=False`。

    异常:
        ApprovalSeparationError：自批且例外未开启（403），
            或例外已开启但没给理由（422）。
    """

    if submitter_id is None:
        return SelfApprovalOutcome(allowed=True, used_exception=False, code="no_submitter")
    if submitter_id != reviewer_id:
        return SelfApprovalOutcome(allowed=True, used_exception=False, code="not_self_approval")
    if not exception_enabled:
        raise ApprovalSeparationError(
            "SELF_APPROVAL_FORBIDDEN",
            "提交人不能审批自己提交的事项；如需例外，请由平台运营开启自批例外并填写理由",
            403,
        )
    if reason is None or not reason.strip():
        raise ApprovalSeparationError(
            "SELF_APPROVAL_REASON_REQUIRED",
            "使用自批例外必须填写理由（会写入审计并通知平台审计）",
            422,
        )
    if len(reason.strip()) > REASON_MAX_LENGTH:
        raise ApprovalSeparationError(
            "SELF_APPROVAL_REASON_TOO_LONG",
            f"理由长度不能超过 {REASON_MAX_LENGTH} 字",
            422,
        )
    return SelfApprovalOutcome(allowed=True, used_exception=True, code="self_exception_used")


__all__ = [
    "ApprovalSeparationError",
    "SelfApprovalOutcome",
    "evaluate_self_approval",
]
