import type { Page, Route } from "@playwright/test";

/** 测试沙箱接口 mock（M3 KB-04B；TD-253 口径：判据手抄后端）。 */

export interface TestMockState {
  runs: {
    run_id: string;
    version_id: string;
    test_set_id: string;
    status: string;
    metrics_json: Record<string, unknown> | null;
  }[];
}

export async function mockTestApi(page: Page): Promise<TestMockState> {
  let runSeq = 0;
  const runs: TestMockState["runs"] = [];

  async function json(route: Route, status: number, body: unknown): Promise<void> {
    await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  }

  await page.route("**/api/v1/school/kb/versions/*/test-sets", async (route) => {
    runSeq += 1;
    const runId = `r-${String(runSeq).padStart(12, "0")}`;
    const versionId =
      route
        .request()
        .url()
        .match(/versions\/([^/]+)\/test-sets/)?.[1] ?? "";
    runs.push({
      run_id: runId,
      version_id: versionId,
      test_set_id: `ts-${String(runSeq).padStart(12, "0")}`,
      status: "TESTING",
      metrics_json: null,
    });
    return json(route, 201, {
      test_set_id: `ts-${String(runSeq).padStart(12, "0")}`,
      name: "mock",
      case_count: 3,
    });
  });

  await page.route("**/api/v1/school/kb/versions/*/tests", async (route) => {
    return json(route, 201, {
      run_id: runs[runs.length - 1]?.run_id ?? "r-unknown",
      version_id:
        route
          .request()
          .url()
          .match(/versions\/([^/]+)\/tests/)?.[1] ?? "",
      test_set_id: runs[runs.length - 1]?.test_set_id ?? "ts-unknown",
      status: "TESTING",
    });
  });

  await page.route("**/api/v1/school/kb/test-runs/**", async (route) => {
    const runId =
      route
        .request()
        .url()
        .match(/test-runs\/([^/]+)/)?.[1] ?? "";
    const run = runs.find((r) => r.run_id === runId);
    if (!run)
      return json(route, 404, { detail: { code: "TEST_RUN_NOT_FOUND", message: "不存在" } });
    // 模拟异步完成：TESTING → 返回 TEST_PASSED（带 metrics）
    return json(route, 200, {
      ...run,
      status: "TEST_PASSED",
      metrics_json: {
        total_cases: 3,
        passed_cases: 3,
        pass_rate: 1.0,
        latency_p50_ms: 120,
        latency_p95_ms: 200,
        cases: [
          { row_no: 1, question: "q1", pass: true, latency_ms: 100 },
          { row_no: 2, question: "q2", pass: true, latency_ms: 120 },
          { row_no: 3, question: "q3", pass: true, latency_ms: 140 },
        ],
      },
      started_at: new Date().toISOString(),
      finished_at: new Date().toISOString(),
    });
  });

  return { runs };
}
