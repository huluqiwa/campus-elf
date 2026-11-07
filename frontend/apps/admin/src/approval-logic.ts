/** 审批中心的纯逻辑（M3 KB-02B）：状态/动作文案、写控件门禁与理由预检，便于单元测试。
 *
 * 与后端同口径：全部判据（权限、状态机、自批）由已合并的
 * `app/api/school_approvals.py` / `app/approval/service.py` 裁决，这里只做
 * 「不给必然失败的按钮」的体验层预检（`account-logic.ts` 同一原则）。
 */

import { ApiError } from "@campus-elf/api-client";
import type { ApprovalRequestView } from "@campus-elf/api-client";

import { codePointLength } from "./logic";

/** 审批中心页面的入口码（`AdminLayout` 菜单与路由 `meta.permission` 同源）。 */
export const APPROVAL_TODO_CODE = "school.approval.todo";
/** 通过/驳回的权限码（后端 `KbApproverDep`）。 */
export const KB_APPROVAL_CODE = "school.kb.approval";
/** 提交审批与撤销自助路径的权限码（后端 `UploaderDep`）。 */
export const KB_UPLOAD_CODE = "school.kb.upload";
/** 创建构建的权限码（后端 `BuilderDep`）。 */
export const KB_BUILD_CODE = "school.kb.build";

/** 驳回理由长度上限，与后端 `KbApprovalDecidePayload` 的 `max_length=500` 同界。 */
export const APPROVAL_REASON_MAX_LENGTH = 500;

/** 本页能决定的审批类型（后端 `APPROVAL_TYPE_NOT_SUPPORTED` 的前端同款判据）。 */
export const SUPPORTED_TYPE_CODES: readonly string[] = ["KB_DOCUMENT"];

export interface ApprovalStatusMeta {
  label: string;
  tone: "success" | "warning" | "danger" | "info";
}

/** 审批单状态四档；未知档位显示原码，不默认成安全态（`account-logic.statusLabel` 同口径）。 */
const REQUEST_STATUS_META: Record<string, ApprovalStatusMeta> = {
  PENDING: { label: "待审批", tone: "warning" },
  APPROVED: { label: "已通过", tone: "success" },
  REJECTED: { label: "已驳回", tone: "danger" },
  CANCELLED: { label: "已撤销", tone: "info" },
};

export function approvalStatusMeta(status: string): ApprovalStatusMeta {
  return REQUEST_STATUS_META[status] ?? { label: `未知状态（${status}）`, tone: "warning" };
}

/** 动作留痕的文案（`actions[].action`；未知档位显示原码）。 */
const ACTION_LABELS: Record<string, string> = {
  submit: "提交",
  approve: "通过",
  reject: "驳回",
  cancel: "撤销",
};

export function actionLabel(action: string): string {
  return ACTION_LABELS[action] ?? `未知动作（${action}）`;
}

/** 审批类型文案；未知档位显示原码。 */
const TYPE_LABELS: Record<string, string> = {
  KB_DOCUMENT: "知识库文档",
};

export function approvalTypeLabel(typeCode: string): string {
  return TYPE_LABELS[typeCode] ?? `未知类型（${typeCode}）`;
}

/**
 * 驳回理由预检：后端判据是「strip 后非空」（422 `REJECT_REASON_REQUIRED`）+
 * ≤500 字（`REASON_TOO_LONG`）；按码点计数与后端 `len()` 同口径（TD-259 教训）。
 */
export function approvalReasonError(reason: string): string | null {
  const value = reason.trim();
  if (!value) return "驳回必须填写理由";
  const length = codePointLength(value);
  if (length > APPROVAL_REASON_MAX_LENGTH) {
    return `驳回理由不能超过 ${APPROVAL_REASON_MAX_LENGTH} 个字`;
  }
  return null;
}

export interface ApprovalGate {
  allowed: boolean;
  /** `allowed=false` 时给人看的一句话；`allowed=true` 时为 null。 */
  hint: string | null;
}

function allow(): ApprovalGate {
  return { allowed: true, hint: null };
}

function deny(hint: string): ApprovalGate {
  return { allowed: false, hint };
}

function missingPermissionHint(code: string): string {
  const labels: Record<string, string> = {
    [KB_APPROVAL_CODE]: "知识库审批",
    [KB_UPLOAD_CODE]: "知识库上传",
  };
  const label = labels[code];
  return label ? `缺少「${label}」权限，这一项用不了` : "缺少相应权限，这一项用不了";
}

/**
 * 通过/驳回按钮的门禁：权限码 + 仍是 PENDING + 本页支持的类型。
 * **不做自批预判**——「审批人是否提交人/上传者」里上传者要查版本归属，前端没有该数据，
 * 也不该猜：让按钮在，403 `SELF_APPROVAL_FORBIDDEN` 由后端裁决并 toast（服务端是唯一门禁）。
 */
export function decideGate(
  request: ApprovalRequestView,
  permissions: readonly string[],
): ApprovalGate {
  if (!permissions.includes(KB_APPROVAL_CODE)) return deny(missingPermissionHint(KB_APPROVAL_CODE));
  if (request.status !== "PENDING") {
    return deny(`该审批单已有最终结论（${approvalStatusMeta(request.status).label}）`);
  }
  if (!SUPPORTED_TYPE_CODES.includes(request.type_code)) {
    return deny(`「${approvalTypeLabel(request.type_code)}」类型暂不支持在本页审批`);
  }
  return allow();
}

/**
 * 撤销按钮的门禁：`school.kb.upload` + 仍是 PENDING + 是自己提交的。
 * 后端同序判据（`cancel_request`：状态 → submitter），非提交人 403 `APPROVAL_CANCEL_FORBIDDEN`。
 */
export function cancelGate(
  request: ApprovalRequestView,
  selfUserId: string | null,
  permissions: readonly string[],
): ApprovalGate {
  if (!permissions.includes(KB_UPLOAD_CODE)) return deny(missingPermissionHint(KB_UPLOAD_CODE));
  if (request.status !== "PENDING") {
    return deny(`该审批单已有最终结论（${approvalStatusMeta(request.status).label}），不能撤销`);
  }
  if (selfUserId === null || request.submitter_id !== selfUserId) {
    return deny("只有提交人可以撤销该审批单");
  }
  return allow();
}

/** 从 ApiError detail 里取机器可读码（`rbac-logic.rbacErrorCode` 同型）。 */
export function approvalErrorCode(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  const detail = error.detail;
  if (!detail || typeof detail !== "object") return null;
  const { code } = detail as { code?: unknown };
  return typeof code === "string" ? code : null;
}

/** 提交审批 / 创建构建的 409 人话文案（服务端 message 是主文案，码是兜底）。 */
export function kbEntryConflictMessage(error: unknown, fallback: string): string {
  const code = approvalErrorCode(error);
  if (code === "APPROVAL_ALREADY_PENDING") return "该版本已有待审批的申请，无需重复提交";
  if (code === "BUILD_ALREADY_ACTIVE") return "该版本已有进行中的构建任务";
  if (code === "KB_NOT_SUBMITTABLE") return "该版本未通过扫描，不能提交审批";
  if (code === "KB_NOT_APPROVED") return "该版本还未审批通过，不能创建构建";
  return fallback;
}

/** REJECTED 文档抽屉的静态指引（TD-291：文档维度直显驳回理由挂 KB-03 的只读端点）。 */
export const REJECT_REASON_GUIDANCE = "该版本已驳回：理由与审批记录请到「审批中心 → 已办」查看";
