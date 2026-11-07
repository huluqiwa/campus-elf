import { describe, expect, it, vi } from "vitest";

import { createTestApi } from "./testSet";
import { ApiError } from "./auth";

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

describe("createTestApi", () => {
  it("uploadTestSet 用 multipart POST（不手动设 Content-Type）", async () => {
    const { session, calls } = sessionStub(() => ({
      body: { test_set_id: "ts-1", name: "测试集A", case_count: 5 },
    }));
    const file = new File(["q,a\n1,2"], "test.csv", { type: "text/csv" });
    const result = await createTestApi(session).uploadTestSet("v-1", {
      name: "测试集A",
      file,
    });

    expect(calls[0].path).toBe("/api/v1/school/kb/versions/v-1/test-sets");
    expect(calls[0].init?.method).toBe("POST");
    const headers = calls[0].init?.headers as Record<string, string> | undefined;
    expect(headers?.["Content-Type"]).toBeUndefined();
    const form = calls[0].init?.body as FormData;
    expect(form.get("name")).toBe("测试集A");
    expect(form.get("file")).toBeInstanceOf(File);
    expect(result.case_count).toBe(5);
  });

  it("startTestRun 用 multipart POST 带 test_set_id", async () => {
    const { session, calls } = sessionStub(() => ({
      body: { run_id: "r-1", version_id: "v-1", test_set_id: "ts-1", status: "TESTING" },
    }));
    const result = await createTestApi(session).startTestRun("v-1", "ts-1");

    expect(calls[0].path).toBe("/api/v1/school/kb/versions/v-1/tests");
    expect(calls[0].init?.method).toBe("POST");
    const form = calls[0].init?.body as FormData;
    expect(form.get("test_set_id")).toBe("ts-1");
    expect(result.status).toBe("TESTING");
  });

  it("getTestRun 按 run id GET", async () => {
    const { session, calls } = sessionStub(() => ({
      body: {
        run_id: "r-1",
        version_id: "v-1",
        test_set_id: "ts-1",
        status: "TEST_PASSED",
        metrics_json: { pass_rate: 1 },
        started_at: "2026-11-08T00:00:00Z",
        finished_at: "2026-11-08T00:01:00Z",
      },
    }));
    const result = await createTestApi(session).getTestRun("r-1");
    expect(calls[0].path).toBe("/api/v1/school/kb/test-runs/r-1");
    expect(result.status).toBe("TEST_PASSED");
  });

  it("listVersionApprovals 返回 items", async () => {
    const { session, calls } = sessionStub(() => ({
      body: {
        items: [
          {
            request_id: "ar-1",
            title: "t",
            status: "REJECTED",
            result: "reject",
            reject_reason: "不符",
            submitter_id: "u",
            created_at: "",
            completed_at: "",
          },
        ],
      },
    }));
    const items = await createTestApi(session).listVersionApprovals("v-1");
    expect(calls[0].path).toBe("/api/v1/school/kb/document-versions/v-1/approvals?limit=20");
    expect(items).toHaveLength(1);
    expect(items[0]?.reject_reason).toBe("不符");
  });

  it("409/422 错误原样透传 detail", async () => {
    const { session } = sessionStub(() => ({
      status: 409,
      body: { code: "TEST_ALREADY_RUNNING", message: "该版本已有进行中的测试运行" },
    }));
    const error = await createTestApi(session)
      .startTestRun("v-1", "ts-1")
      .then(
        () => null,
        (e: unknown) => e,
      );
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(409);
    expect((error as ApiError).detail).toEqual({
      code: "TEST_ALREADY_RUNNING",
      message: "该版本已有进行中的测试运行",
    });
  });
});
