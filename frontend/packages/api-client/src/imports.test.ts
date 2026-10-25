import { describe, expect, it, vi } from "vitest";

import { createImportsApi } from "./imports";

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
      throw new Error(`HTTP ${scripted.status}`);
    }
    return (scripted?.body ?? {}) as T;
  });
  return {
    session: { request: request as <T>(path: string, init?: RequestInit) => Promise<T> },
    calls,
  };
}

const JOB = {
  id: "j-1",
  type: "teachers",
  conflict: "error",
  status: "pending",
  total_rows: 0,
  success_rows: 0,
  failed_rows: 0,
  skipped_rows: 0,
  retried_by: null,
  created_at: "2026-10-26T00:00:00Z",
  started_at: null,
  finished_at: null,
  message: null,
};

describe("createImportsApi", () => {
  it("upload 用 FormData 提交且不手动设置 Content-Type（让浏览器带 boundary）", async () => {
    const { session, calls } = sessionStub(() => ({ body: JOB }));
    const file = new File(["工号,姓名\nE-1,张老师"], "teachers.csv", { type: "text/csv" });
    await createImportsApi(session).upload({ type: "teachers", conflict: "error", file });
    expect(calls[0].path).toBe("/api/v1/school/imports");
    expect(calls[0].init?.method).toBe("POST");
    const headers = calls[0].init?.headers as Record<string, string> | undefined;
    expect(headers?.["Content-Type"]).toBeUndefined();
    const form = calls[0].init?.body as FormData;
    expect(form.get("type")).toBe("teachers");
    expect(form.get("conflict")).toBe("error");
    expect(form.get("file")).toBe(file);
  });

  it("get 按 job id 查询进度", async () => {
    const { session, calls } = sessionStub(() => ({ body: { ...JOB, status: "running" } }));
    const job = await createImportsApi(session).get("j-1");
    expect(calls[0].path).toBe("/api/v1/school/imports/j-1");
    expect(job.status).toBe("running");
  });

  it("listErrors 的整数游标以 query 传递", async () => {
    const { session, calls } = sessionStub(() => ({ body: { items: [], next_cursor: null } }));
    await createImportsApi(session).listErrors("j-1", { limit: 100, cursor: 5 });
    expect(calls[0].path).toBe("/api/v1/school/imports/j-1/errors?limit=100&cursor=5");
  });

  it("retry 返回派生引用（source_job_id/job_id，非 job 本体——与后端 ImportRetryResponse 一致）", async () => {
    const { session, calls } = sessionStub(() => ({
      body: { source_job_id: "j-1", job_id: "j-2" },
    }));
    const ref = await createImportsApi(session).retry("j-1");
    expect(calls[0].path).toBe("/api/v1/school/imports/j-1/retry");
    expect(calls[0].init?.method).toBe("POST");
    expect(ref).toEqual({ source_job_id: "j-1", job_id: "j-2" });
  });
});
