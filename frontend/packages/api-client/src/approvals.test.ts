import { describe, expect, it, vi } from "vitest";

import { createApprovalsApi } from "./approvals";
import { ApiError } from "./auth";
import type { ApprovalDetailView, ApprovalRequestView } from "./approvals";

function sessionStub(
  handler: (path: string, init?: RequestInit) => { status?: number; body?: unknown } | undefined,
): {
  session: { request: <T>(path: string, init?: RequestInit) => Promise<T> };
  calls: { path: string; init?: RequestInit }[];
} {
  const calls: { path: string; init?: RequestInit }[] = [];
  const request = vi.fn(async <T>(path: string, init?: RequestInit): Promise<T> => {
    calls.push({ path, init });
    const scripted = handler(path, init);
    if (scripted?.status !== undefined && scripted.status >= 400) {
      throw new ApiError("失败", scripted.status, scripted.body);
    }
    return (scripted?.body ?? {}) as T;
  });
  return {
    session: { request: request as <T>(path: string, init?: RequestInit) => Promise<T> },
    calls,
  };
}

const REQUEST: ApprovalRequestView = {
  id: "r-1",
  type_code: "KB_DOCUMENT",
  title: "知识库文档审批：课程大纲",
  status: "PENDING",
  result: null,
  reject_reason: null,
  submitter_id: "u-1",
  document_version_id: "v-1",
  payload: { version_id: "v-1", document_title: "课程大纲" },
  current_step: 1,
  created_at: "2026-11-07T00:00:00Z",
  completed_at: null,
};

const DETAIL: ApprovalDetailView = {
  request: REQUEST,
  steps: [
    { id: "s-1", step_no: 1, status: "PENDING", acted_by: null, acted_at: null, comment: null },
  ],
  actions: [
    {
      id: "a-1",
      actor_id: "u-1",
      action: "submit",
      reason: null,
      created_at: "2026-11-07T00:00:00Z",
    },
  ],
};

describe("createApprovalsApi", () => {
  it("listTodo/listDone 带 cursor 与 limit 查询串", async () => {
    const { session, calls } = sessionStub(() => ({
      body: { items: [REQUEST], next_cursor: null },
    }));
    const api = createApprovalsApi(session);

    await api.listTodo({ limit: 20, cursor: "cur-1" });
    await api.listDone();

    expect(calls[0].path).toBe("/api/v1/school/approvals/todo?limit=20&cursor=cur-1");
    expect(calls[1].path).toBe("/api/v1/school/approvals/done");
  });

  it("getDetail 按请求 id 取详情", async () => {
    const { session, calls } = sessionStub(() => ({ body: DETAIL }));
    const detail = await createApprovalsApi(session).getDetail("r-1");

    expect(calls[0].path).toBe("/api/v1/school/approvals/r-1");
    expect(detail.request.id).toBe("r-1");
    expect(detail.actions[0]?.action).toBe("submit");
  });

  it("approve 默认发空对象请求体；reject 原样透传理由", async () => {
    const { session, calls } = sessionStub(() => ({ body: { ...REQUEST, status: "APPROVED" } }));
    const api = createApprovalsApi(session);

    await api.approve("r-1");
    await api.reject("r-1", { reason: " 内容不符 " });

    expect(calls[0].path).toBe("/api/v1/school/approvals/r-1/approve");
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({});
    expect(calls[1].path).toBe("/api/v1/school/approvals/r-1/reject");
    expect(JSON.parse(String(calls[1].init?.body))).toEqual({ reason: " 内容不符 " });
  });

  it("cancel 不带请求体", async () => {
    const { session, calls } = sessionStub(() => ({
      body: { ...REQUEST, status: "CANCELLED", result: "cancel" },
    }));
    const result = await createApprovalsApi(session).cancel("r-1");

    expect(calls[0].path).toBe("/api/v1/school/approvals/r-1/cancel");
    expect(calls[0].init?.method).toBe("POST");
    expect(calls[0].init?.body).toBeUndefined();
    expect(result.status).toBe("CANCELLED");
  });

  it("submitKbVersion/createKbBuild 打到 KB 侧两个门禁端点", async () => {
    const { session, calls } = sessionStub(() => ({
      body: {
        request_id: "r-2",
        document_version_id: "v-2",
        title: "t",
        status: "PENDING",
        submitter_id: "u-1",
        created_at: "2026-11-07T00:00:00Z",
      },
    }));
    const api = createApprovalsApi(session);

    await api.submitKbVersion("v-2");
    await api.createKbBuild("v-2");

    expect(calls[0].path).toBe("/api/v1/school/kb/document-versions/v-2/submit-approval");
    expect(calls[0].init?.method).toBe("POST");
    expect(calls[1].path).toBe("/api/v1/school/kb/document-versions/v-2/build");
  });

  it("422/403/409 错误原样透传 detail（供调用方取 code/message）", async () => {
    const { session } = sessionStub((path) => {
      if (path.endsWith("/reject")) {
        return {
          status: 422,
          body: { code: "REJECT_REASON_REQUIRED", message: "驳回必须填写理由" },
        };
      }
      return {
        status: 403,
        body: {
          code: "SELF_APPROVAL_FORBIDDEN",
          message: "提交人或上传者不能审批该文档版本（无例外通道）",
        },
      };
    });
    const api = createApprovalsApi(session);

    const rejectError = await api.reject("r-1", { reason: "" }).then(
      () => null,
      (error: unknown) => error,
    );
    expect(rejectError).toBeInstanceOf(ApiError);
    expect((rejectError as ApiError).status).toBe(422);
    expect((rejectError as ApiError).detail).toEqual({
      code: "REJECT_REASON_REQUIRED",
      message: "驳回必须填写理由",
    });

    const approveError = await api.approve("r-1").then(
      () => null,
      (error: unknown) => error,
    );
    expect((approveError as ApiError).status).toBe(403);
    expect((approveError as ApiError).detail).toEqual({
      code: "SELF_APPROVAL_FORBIDDEN",
      message: "提交人或上传者不能审批该文档版本（无例外通道）",
    });
  });
});
