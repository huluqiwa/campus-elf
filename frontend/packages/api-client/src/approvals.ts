/** 审批中心域 API（M3 KB-02B）。
 *
 * 与后端 `app/api/school_approvals.py` 响应模型一一对齐；KB 侧两个入口
 * （`submit-approval` / `build`）挂在本文档域而不是 kb.ts——它们消费的是审批契约的
 * 错误码（`APPROVAL_ALREADY_PENDING` 等），与 kb.ts 的读契约分开放，调用方一眼分清。
 * 错误（403 自批/越权、404 跨租、409 重复、422 理由）由调用方从异常 detail 里取
 * `{code, message}` 展示；后端是唯一门禁，本模块只透传。
 */

import { buildQuery, type AuthSessionRequest, type Page } from "./org";

/** 审批单（对齐 `ApprovalRequestResponse`；`payload` 是提交时的版本快照，结构由提交方决定）。 */
export interface ApprovalRequestView {
  id: string;
  type_code: string;
  title: string;
  /** `PENDING` / `APPROVED` / `REJECTED` / `CANCELLED`（`approval/service.py` 的 `_REQUEST_STATUS_SQL`）。 */
  status: string;
  /** 最终动作：`approve` / `reject` / `cancel`；PENDING 时为 null。 */
  result: string | null;
  reject_reason: string | null;
  submitter_id: string;
  /** 仅 KB_DOCUMENT 类型非空（引擎层可空列是为非 KB 类型预留）。 */
  document_version_id: string | null;
  payload: Record<string, unknown> | null;
  current_step: number;
  created_at: string;
  completed_at: string | null;
}

/** 审批步骤（本点恒 1 步；对齐 `ApprovalStepResponse`）。 */
export interface ApprovalStepView {
  id: string;
  step_no: number;
  status: string;
  acted_by: string | null;
  acted_at: string | null;
  comment: string | null;
}

/** 审批动作留痕（对齐 `ApprovalActionResponse`；`action ∈ submit/approve/reject/cancel`）。 */
export interface ApprovalActionView {
  id: string;
  actor_id: string;
  action: string;
  reason: string | null;
  created_at: string;
}

/** 审批单详情（对齐 `ApprovalDetailResponse`）。 */
export interface ApprovalDetailView {
  request: ApprovalRequestView;
  steps: ApprovalStepView[];
  actions: ApprovalActionView[];
}

/** 通过/驳回请求体：驳回理由必填由后端裁决（422），通过可不填。 */
export interface ApprovalDecideInput {
  reason?: string | null;
}

/** KB 版本提交审批结果（对齐 `KbApprovalSubmitResponse`）。 */
export interface KbApprovalSubmitResult {
  request_id: string;
  document_version_id: string | null;
  title: string;
  status: string;
  submitter_id: string;
  created_at: string;
}

/** KB 构建任务创建结果（对齐 `KbBuildJobResponse`；进度字段随 KB-03）。 */
export interface KbBuildJobResult {
  job_id: string;
  version_id: string;
  status: string;
  attempt: number;
  created_at: string;
}

export interface ApprovalsApi {
  /** 待办：全部 PENDING 审批单（`school.approval.todo`）。 */
  listTodo(query?: { limit?: number; cursor?: string }): Promise<Page<ApprovalRequestView>>;
  /** 已办：与本人有过动作往来（含自己提交的）的单——撤销入口在这里。 */
  listDone(query?: { limit?: number; cursor?: string }): Promise<Page<ApprovalRequestView>>;
  getDetail(requestId: string): Promise<ApprovalDetailView>;
  /** 通过（终态，不可撤销）。 */
  approve(requestId: string, input?: ApprovalDecideInput): Promise<ApprovalRequestView>;
  /** 驳回（终态；理由必填，后端 422 `REJECT_REASON_REQUIRED`）。 */
  reject(requestId: string, input: ApprovalDecideInput): Promise<ApprovalRequestView>;
  /** 提交人撤销自己的 PENDING 单（`school.kb.upload` 自助；非提交人 403）。 */
  cancel(requestId: string): Promise<ApprovalRequestView>;
  /** 提交 KB 版本审批（`school.kb.upload`；仅 `PENDING_APPROVAL`，409 `KB_NOT_SUBMITTABLE`/`APPROVAL_ALREADY_PENDING`）。 */
  submitKbVersion(versionId: string): Promise<KbApprovalSubmitResult>;
  /** 创建构建任务（`school.kb.build`；仅 `APPROVED`，409 `KB_NOT_APPROVED`/`BUILD_ALREADY_ACTIVE`）。 */
  createKbBuild(versionId: string): Promise<KbBuildJobResult>;
}

function jsonInit(method: string, body?: unknown): RequestInit {
  if (body === undefined) return { method };
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export function createApprovalsApi(session: { request: AuthSessionRequest }): ApprovalsApi {
  const request = session.request;

  return {
    async listTodo(query = {}) {
      const qs = buildQuery({ limit: query.limit, cursor: query.cursor });
      return request<Page<ApprovalRequestView>>(`/api/v1/school/approvals/todo${qs}`);
    },
    async listDone(query = {}) {
      const qs = buildQuery({ limit: query.limit, cursor: query.cursor });
      return request<Page<ApprovalRequestView>>(`/api/v1/school/approvals/done${qs}`);
    },
    async getDetail(requestId) {
      return request<ApprovalDetailView>(`/api/v1/school/approvals/${requestId}`);
    },
    async approve(requestId, input) {
      return request<ApprovalRequestView>(
        `/api/v1/school/approvals/${requestId}/approve`,
        jsonInit("POST", input ?? {}),
      );
    },
    async reject(requestId, input) {
      return request<ApprovalRequestView>(
        `/api/v1/school/approvals/${requestId}/reject`,
        jsonInit("POST", input),
      );
    },
    async cancel(requestId) {
      return request<ApprovalRequestView>(`/api/v1/school/approvals/${requestId}/cancel`, {
        method: "POST",
      });
    },
    async submitKbVersion(versionId) {
      return request<KbApprovalSubmitResult>(
        `/api/v1/school/kb/document-versions/${versionId}/submit-approval`,
        { method: "POST" },
      );
    },
    async createKbBuild(versionId) {
      return request<KbBuildJobResult>(`/api/v1/school/kb/document-versions/${versionId}/build`, {
        method: "POST",
      });
    },
  };
}
