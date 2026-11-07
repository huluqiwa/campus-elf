import type { Page, Route } from "@playwright/test";

import { DEFAULT_ACTOR_USER_ID } from "./rbac-mock";
import type { KbDocumentFixture } from "./kb-mock";

/**
 * 审批中心接口 mock（M3 KB-02B）：有状态的 待办/已办/详情/通过/驳回/撤销 + KB 侧提交审批与建构建。
 *
 * **TD-253 口径**：判据手抄后端（每条注释以**函数名**为锚点），后端改了这里不会红，收口靠未来的全栈 E2E（TD-292）。
 * 对齐的契约（`app/api/school_approvals.py`、`app/approval/service.py`）：
 * - decide 判据顺序（`service.py::decide_request`）：404 → 非 PENDING 409 `APPROVAL_ALREADY_DECIDED`
 *   （detail 带 `status`/`result`）→ 自批 403 `SELF_APPROVAL_FORBIDDEN`（提交人**或**上传者，
 *   `forbidden_actor_ids=(submitter_id, version.created_by)`）→ 驳回空理由 422 `REJECT_REASON_REQUIRED`；
 * - cancel 判据顺序（`service.py::cancel_request`）：404 → 非 PENDING 409 → 非提交人 403
 *   `APPROVAL_CANCEL_FORBIDDEN`；
 * - 提交审批（`school_kb.py`）：按 `current_version.id` 找文档 404 `VERSION_NOT_FOUND` →
 *   非 `PENDING_APPROVAL` 409 `KB_NOT_SUBMITTABLE` → 已有 PENDING 单 409 `APPROVAL_ALREADY_PENDING`；
 * - 创建构建：非 `APPROVED` 409 `KB_NOT_APPROVED` → 已有活跃任务 409 `BUILD_ALREADY_ACTIVE`；
 * - done = 与本人有过**任何**动作往来的单（`service.py::list_done` 的子查询定义）。
 */

/** mock 世界里的当前登录人（与 auth-mock 的默认 userId 一致；rbac-mock 同源常量）。 */
export const SELF = DEFAULT_ACTOR_USER_ID;

export interface ApprovalActionFixture {
  id: string;
  actor_id: string;
  action: string;
  reason: string | null;
  created_at: string;
}

export interface ApprovalStepFixture {
  id: string;
  step_no: number;
  status: string;
  acted_by: string | null;
  acted_at: string | null;
  comment: string | null;
}

export interface ApprovalRequestFixture {
  id: string;
  type_code: string;
  title: string;
  status: string;
  result: string | null;
  reject_reason: string | null;
  submitter_id: string;
  document_version_id: string | null;
  payload: Record<string, unknown> | null;
  current_step: number;
  created_at: string;
  completed_at: string | null;
}

export interface ApprovalMockCaptures {
  listUrls: string[];
  detailIds: string[];
  decideCalls: { id: string; action: string; body: unknown }[];
  cancelIds: string[];
  submitCalls: string[];
  buildCalls: string[];
}

export interface ApprovalMockOptions {
  requests?: ApprovalRequestFixture[];
  /** 提交审批/创建构建按 `current_version.id` 在这里找文档与状态（与 kb-mock 夹具同源）。 */
  documents?: KbDocumentFixture[];
  /** 第 N 次 todo/done 请求返回 500（1 基），用于验证「数据可能不是最新」横幅。 */
  listFailOnRequest?: number;
  /** 这些 id 的**详情**请求返回 404（列表照常返回，模拟打开抽屉前单据被删/跨租）。 */
  detailMissingIds?: string[];
}

export interface ApprovalMock {
  captures: ApprovalMockCaptures;
  state: {
    requests: ApprovalRequestFixture[];
    actions: Map<string, ApprovalActionFixture[]>;
    jobs: { job_id: string; version_id: string; status: string; attempt: number }[];
  };
}

export interface ApprovalRequestOverrides {
  id?: string;
  title?: string;
  status?: string;
  result?: string | null;
  reject_reason?: string | null;
  submitter_id?: string;
  document_version_id?: string | null;
  payload?: Record<string, unknown> | null;
  created_at?: string;
  completed_at?: string | null;
}

const SUBMITTER_OTHER = "95000000-0000-0000-0000-000000000009";

/** 快照的最小形态（对齐后端 `_version_snapshot` 的键）。 */
export function approvalPayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    document_id: "60000000-0000-0000-0000-000000000001",
    document_title: "校园安全手册",
    tags: ["安全", "手册"],
    description: "校园安全制度汇编",
    version_id: "61000000-0000-0000-0000-000000000001",
    version_no: 2,
    source_filename: "校园安全手册.pdf",
    source_hash: "a".repeat(64),
    mime: "application/pdf",
    size_bytes: 2048,
    scan_engine: "clamav",
    scanned_at: "2026-11-06T09:00:00Z",
    created_by: SUBMITTER_OTHER,
    ...overrides,
  };
}

export function approvalRequest(overrides: ApprovalRequestOverrides = {}): ApprovalRequestFixture {
  return {
    id: "90000000-0000-0000-0000-000000000001",
    type_code: "KB_DOCUMENT",
    title: "知识库文档审批：校园安全手册",
    status: "PENDING",
    result: null,
    reject_reason: null,
    submitter_id: SUBMITTER_OTHER,
    document_version_id: "61000000-0000-0000-0000-000000000001",
    payload: approvalPayload(),
    current_step: 1,
    created_at: "2026-11-06T10:00:00Z",
    completed_at: null,
    ...overrides,
  };
}

/** 默认五条：待办两条（他人提交/自己提交）；已办四条——通过/驳回/撤销各一，
 * 加上 0002（自己提交的待办单）：done 按「任何动作往来」过滤，submit 也算（审查 m2）。 */
export const FIXTURE_APPROVAL_REQUESTS: ApprovalRequestFixture[] = [
  approvalRequest(),
  approvalRequest({
    id: "90000000-0000-0000-0000-000000000002",
    title: "知识库文档审批：开发样例",
    submitter_id: SELF,
    document_version_id: "61000000-0000-0000-0000-000000000003",
    payload: approvalPayload({
      document_id: "60000000-0000-0000-0000-000000000003",
      document_title: "开发样例",
      version_id: "61000000-0000-0000-0000-000000000003",
      version_no: 1,
      source_filename: "开发样例.md",
      mime: "text/markdown",
      size_bytes: 128,
      scan_engine: "dev-skip",
      created_by: SELF,
    }),
    created_at: "2026-11-06T11:00:00Z",
  }),
  approvalRequest({
    id: "90000000-0000-0000-0000-000000000003",
    title: "知识库文档审批：待扫描通知",
    status: "APPROVED",
    result: "approve",
    submitter_id: SUBMITTER_OTHER,
    document_version_id: "61000000-0000-0000-0000-000000000004",
    payload: approvalPayload({
      document_id: "60000000-0000-0000-0000-000000000004",
      document_title: "待扫描通知",
      version_id: "61000000-0000-0000-0000-000000000004",
      version_no: 1,
      source_filename: "待扫描通知.docx",
      created_by: SUBMITTER_OTHER,
    }),
    completed_at: "2026-11-06T12:00:00Z",
    created_at: "2026-11-06T11:30:00Z",
  }),
  approvalRequest({
    id: "90000000-0000-0000-0000-000000000004",
    title: "知识库文档审批：可疑附件",
    status: "REJECTED",
    result: "reject",
    reject_reason: "内容与学校口径不符，退回重写",
    submitter_id: SUBMITTER_OTHER,
    document_version_id: "61000000-0000-0000-0000-000000000002",
    payload: approvalPayload({
      document_id: "60000000-0000-0000-0000-000000000002",
      document_title: "可疑附件",
      version_id: "61000000-0000-0000-0000-000000000002",
      source_filename: "可疑附件.pdf",
    }),
    completed_at: "2026-11-06T13:00:00Z",
    created_at: "2026-11-06T12:30:00Z",
  }),
  approvalRequest({
    id: "90000000-0000-0000-0000-000000000005",
    title: "知识库文档审批：已撤销样例",
    status: "CANCELLED",
    result: "cancel",
    submitter_id: SELF,
    document_version_id: "61000000-0000-0000-0000-000000000005",
    payload: approvalPayload({
      document_title: "已撤销样例",
      version_id: "61000000-0000-0000-0000-000000000005",
      created_by: SELF,
    }),
    completed_at: "2026-11-06T14:00:00Z",
    created_at: "2026-11-06T13:30:00Z",
  }),
];

function clone(request: ApprovalRequestFixture): ApprovalRequestFixture {
  return { ...request, payload: request.payload ? { ...request.payload } : null };
}

function json(route: Route, status: number, body: unknown): Promise<void> {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

/** 与 kb-mock 同款 offset 游标（前端把 cursor 当不透明串）。 */
function paginate<T>(items: T[], url: URL): { items: T[]; next_cursor: string | null } {
  const limit = Number(url.searchParams.get("limit") ?? "20");
  const offset = Number(url.searchParams.get("cursor") ?? "0") || 0;
  const page = items.slice(offset, offset + limit);
  const nextCursor = offset + limit < items.length ? String(offset + limit) : null;
  return { items: page, next_cursor: nextCursor };
}

export async function mockApprovalsApi(
  page: Page,
  options: ApprovalMockOptions = {},
): Promise<ApprovalMock> {
  const requests = (options.requests ?? FIXTURE_APPROVAL_REQUESTS).map(clone);
  const documents = options.documents;
  const actions = new Map<string, ApprovalActionFixture[]>();
  const steps = new Map<string, ApprovalStepFixture[]>();
  let actionSeq = 0;

  function seedHistory(request: ApprovalRequestFixture): void {
    const history: ApprovalActionFixture[] = [
      {
        id: `a0000000-0000-0000-0000-${String(++actionSeq).padStart(12, "0")}`,
        actor_id: request.submitter_id,
        action: "submit",
        reason: null,
        created_at: request.created_at,
      },
    ];
    if (request.result === "approve") {
      history.push({
        id: `a0000000-0000-0000-0000-${String(++actionSeq).padStart(12, "0")}`,
        actor_id: SELF,
        action: "approve",
        reason: null,
        created_at: request.completed_at ?? request.created_at,
      });
    }
    if (request.result === "reject") {
      history.push({
        id: `a0000000-0000-0000-0000-${String(++actionSeq).padStart(12, "0")}`,
        actor_id: SELF,
        action: "reject",
        reason: request.reject_reason,
        created_at: request.completed_at ?? request.created_at,
      });
    }
    if (request.result === "cancel") {
      history.push({
        id: `a0000000-0000-0000-0000-${String(++actionSeq).padStart(12, "0")}`,
        actor_id: request.submitter_id,
        action: "cancel",
        reason: null,
        created_at: request.completed_at ?? request.created_at,
      });
    }
    actions.set(request.id, history);
    steps.set(request.id, [
      {
        id: `s0000000-0000-0000-0000-${request.id.slice(-12)}`,
        step_no: 1,
        status: request.status,
        acted_by: request.result ? (history[history.length - 1]?.actor_id ?? null) : null,
        acted_at: request.completed_at,
        comment: request.reject_reason,
      },
    ]);
  }
  for (const request of requests) seedHistory(request);

  const captures: ApprovalMockCaptures = {
    listUrls: [],
    detailIds: [],
    decideCalls: [],
    cancelIds: [],
    submitCalls: [],
    buildCalls: [],
  };
  const jobs: ApprovalMock["state"]["jobs"] = [];
  let jobSeq = 0;
  let listRequests = 0;

  function byId(id: string): ApprovalRequestFixture | undefined {
    return requests.find((item) => item.id === id);
  }

  function appendAction(
    requestId: string,
    actorId: string,
    action: string,
    reason: string | null,
    now: string,
  ): void {
    const history = actions.get(requestId) ?? [];
    history.push({
      id: `a0000000-0000-0000-0000-${String(++actionSeq).padStart(12, "0")}`,
      actor_id: actorId,
      action,
      reason,
      created_at: now,
    });
    actions.set(requestId, history);
  }

  async function handleList(route: Route, kind: "todo" | "done"): Promise<void> {
    captures.listUrls.push(route.request().url());
    listRequests += 1;
    if (options.listFailOnRequest === listRequests) {
      return json(route, 500, { detail: "服务暂时不可用" });
    }
    const url = new URL(route.request().url());
    const source =
      kind === "todo"
        ? requests.filter((item) => item.status === "PENDING")
        : requests.filter((item) =>
            (actions.get(item.id) ?? []).some((action) => action.actor_id === SELF),
          );
    return json(route, 200, paginate(source.map(clone), url));
  }

  async function handleDetail(route: Route, id: string): Promise<void> {
    captures.detailIds.push(id);
    if (options.detailMissingIds?.includes(id)) {
      return json(route, 404, { detail: { code: "APPROVAL_NOT_FOUND", message: "审批单不存在" } });
    }
    const request = byId(id);
    if (!request) {
      return json(route, 404, { detail: { code: "APPROVAL_NOT_FOUND", message: "审批单不存在" } });
    }
    return json(route, 200, {
      request: clone(request),
      steps: [...(steps.get(id) ?? [])],
      actions: [...(actions.get(id) ?? [])],
    });
  }

  async function handleDecide(
    route: Route,
    id: string,
    action: "approve" | "reject",
  ): Promise<void> {
    const body = (route.request().postDataJSON() as { reason?: string } | null) ?? {};
    captures.decideCalls.push({ id, action, body });
    const request = byId(id);
    // `service.py::decide_request`：404 → 状态机 → 自批 → 理由
    if (!request) {
      return json(route, 404, { detail: { code: "APPROVAL_NOT_FOUND", message: "审批单不存在" } });
    }
    if (request.status !== "PENDING") {
      return json(route, 409, {
        detail: {
          code: "APPROVAL_ALREADY_DECIDED",
          message: "该审批单已有最终结论",
          status: request.status,
          result: request.result,
        },
      });
    }
    const createdBy = request.payload?.["created_by"];
    if (request.submitter_id === SELF || createdBy === SELF) {
      return json(route, 403, {
        detail: {
          code: "SELF_APPROVAL_FORBIDDEN",
          message: "提交人或上传者不能审批该文档版本（无例外通道）",
        },
      });
    }
    const now = new Date().toISOString();
    if (action === "reject") {
      const reason = (body.reason ?? "").trim();
      if (!reason) {
        return json(route, 422, {
          detail: { code: "REJECT_REASON_REQUIRED", message: "驳回必须填写理由" },
        });
      }
      request.status = "REJECTED";
      request.result = "reject";
      request.reject_reason = reason;
      request.completed_at = now;
      appendAction(id, SELF, "reject", reason, now);
    } else {
      request.status = "APPROVED";
      request.result = "approve";
      request.completed_at = now;
      appendAction(id, SELF, "approve", null, now);
    }
    const step = (steps.get(id) ?? [])[0];
    if (step) {
      step.status = request.status;
      step.acted_by = SELF;
      step.acted_at = now;
      step.comment = request.reject_reason;
    }
    return json(route, 200, clone(request));
  }

  async function handleCancel(route: Route, id: string): Promise<void> {
    captures.cancelIds.push(id);
    const request = byId(id);
    // `service.py::cancel_request`：404 → 状态机 → 提交人
    if (!request) {
      return json(route, 404, { detail: { code: "APPROVAL_NOT_FOUND", message: "审批单不存在" } });
    }
    if (request.status !== "PENDING") {
      return json(route, 409, {
        detail: {
          code: "APPROVAL_ALREADY_DECIDED",
          message: "该审批单已有最终结论，不能撤销",
          status: request.status,
          result: request.result,
        },
      });
    }
    if (request.submitter_id !== SELF) {
      return json(route, 403, {
        detail: { code: "APPROVAL_CANCEL_FORBIDDEN", message: "只有提交人可以撤销该审批单" },
      });
    }
    const now = new Date().toISOString();
    request.status = "CANCELLED";
    request.result = "cancel";
    request.completed_at = now;
    appendAction(id, SELF, "cancel", null, now);
    return json(route, 200, clone(request));
  }

  function findDocumentByVersion(versionId: string): KbDocumentFixture | undefined {
    return documents?.find((item) => item.current_version?.id === versionId);
  }

  async function handleSubmitKb(route: Route, versionId: string): Promise<void> {
    captures.submitCalls.push(versionId);
    const document = findDocumentByVersion(versionId);
    // `school_kb.py::submit_kb_document_version_approval`：404 → 可提交状态 → 唯一活跃单
    if (!document) {
      return json(route, 404, {
        detail: { code: "VERSION_NOT_FOUND", message: "文档版本不存在" },
      });
    }
    if (document.status !== "PENDING_APPROVAL") {
      return json(route, 409, {
        detail: { code: "KB_NOT_SUBMITTABLE", message: "该版本未通过扫描，不能提交审批" },
      });
    }
    if (
      requests.some((item) => item.document_version_id === versionId && item.status === "PENDING")
    ) {
      return json(route, 409, {
        detail: { code: "APPROVAL_ALREADY_PENDING", message: "该版本已有待审批的申请" },
      });
    }
    const now = new Date().toISOString();
    const version = document.current_version;
    const request = approvalRequest({
      id: `90000000-0000-0000-0000-${String(requests.length + 100).padStart(12, "0")}`,
      title: `知识库文档审批：${document.title}`,
      submitter_id: SELF,
      document_version_id: versionId,
      payload: approvalPayload({
        document_id: document.id,
        document_title: document.title,
        version_id: versionId,
        version_no: version?.version_no ?? 1,
        source_filename: version?.filename ?? "",
        created_by: SELF,
      }),
      created_at: now,
    });
    requests.unshift(request);
    seedHistory(request);
    return json(route, 201, {
      request_id: request.id,
      document_version_id: versionId,
      title: request.title,
      status: request.status,
      submitter_id: SELF,
      created_at: now,
    });
  }

  async function handleBuild(route: Route, versionId: string): Promise<void> {
    captures.buildCalls.push(versionId);
    const document = findDocumentByVersion(versionId);
    if (!document) {
      return json(route, 404, {
        detail: { code: "VERSION_NOT_FOUND", message: "文档版本不存在" },
      });
    }
    if (document.status !== "APPROVED") {
      return json(route, 409, {
        detail: { code: "KB_NOT_APPROVED", message: "该版本还未审批通过，不能创建构建" },
      });
    }
    if (jobs.some((job) => job.version_id === versionId)) {
      return json(route, 409, {
        detail: { code: "BUILD_ALREADY_ACTIVE", message: "该版本已有进行中的构建任务" },
      });
    }
    jobSeq += 1;
    const job = {
      job_id: `b0000000-0000-0000-0000-${String(jobSeq).padStart(12, "0")}`,
      version_id: versionId,
      status: "BUILD_QUEUED",
      attempt: 1,
    };
    jobs.push(job);
    return json(route, 201, {
      job_id: job.job_id,
      version_id: versionId,
      status: job.status,
      attempt: job.attempt,
      created_at: new Date().toISOString(),
    });
  }

  await page.route("**/api/v1/school/approvals/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();

    if (method === "GET" && path.endsWith("/school/approvals/todo")) {
      return handleList(route, "todo");
    }
    if (method === "GET" && path.endsWith("/school/approvals/done")) {
      return handleList(route, "done");
    }
    const match = path.match(/\/school\/approvals\/([^/]+)(\/approve|\/reject|\/cancel)?$/);
    if (match) {
      const id = match[1] ?? "";
      if (method === "GET") return handleDetail(route, id);
      if (method === "POST" && match[2] === "/approve") return handleDecide(route, id, "approve");
      if (method === "POST" && match[2] === "/reject") return handleDecide(route, id, "reject");
      if (method === "POST" && match[2] === "/cancel") return handleCancel(route, id);
    }
    return json(route, 404, { detail: "未 mock 的审批路径" });
  });

  await page.route("**/api/v1/school/kb/document-versions/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (method === "POST") {
      const submit = path.match(/\/school\/kb\/document-versions\/([^/]+)\/submit-approval$/);
      if (submit) return handleSubmitKb(route, submit[1] ?? "");
      const build = path.match(/\/school\/kb\/document-versions\/([^/]+)\/build$/);
      if (build) return handleBuild(route, build[1] ?? "");
    }
    return json(route, 404, { detail: "未 mock 的知识库版本路径" });
  });

  return { captures, state: { requests, actions, jobs } };
}
