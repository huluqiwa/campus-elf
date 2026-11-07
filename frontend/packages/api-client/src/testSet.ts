/** 测试沙箱域 API（M3 KB-04B）。与后端 `app/api/school_kb.py` 的 test 端点对齐。 */

import { buildQuery, type AuthSessionRequest } from "./org";

export interface KbTestSetCreateResult {
  test_set_id: string;
  name: string;
  case_count: number;
}

export interface KbTestRunStartResult {
  run_id: string;
  version_id: string;
  test_set_id: string;
  status: string;
}

export interface KbTestRunDetail {
  run_id: string;
  version_id: string;
  test_set_id: string;
  status: string;
  metrics_json: Record<string, unknown> | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface KbVersionApprovalView {
  request_id: string;
  title: string;
  status: string;
  result: string | null;
  reject_reason: string | null;
  submitter_id: string;
  created_at: string;
  completed_at: string | null;
}

export interface KbTestApi {
  uploadTestSet(
    versionId: string,
    input: { name: string; file: File },
  ): Promise<KbTestSetCreateResult>;
  startTestRun(versionId: string, testSetId: string): Promise<KbTestRunStartResult>;
  getTestRun(runId: string): Promise<KbTestRunDetail>;
  listVersionApprovals(versionId: string): Promise<KbVersionApprovalView[]>;
}

export function createTestApi(session: { request: AuthSessionRequest }): KbTestApi {
  const request = session.request;

  return {
    async uploadTestSet(versionId, input) {
      const form = new FormData();
      form.set("name", input.name);
      form.append("file", input.file);
      return request<KbTestSetCreateResult>(`/api/v1/school/kb/versions/${versionId}/test-sets`, {
        method: "POST",
        body: form,
      });
    },
    async startTestRun(versionId, testSetId) {
      const form = new FormData();
      form.set("test_set_id", testSetId);
      return request<KbTestRunStartResult>(`/api/v1/school/kb/versions/${versionId}/tests`, {
        method: "POST",
        body: form,
      });
    },
    async getTestRun(runId) {
      return request<KbTestRunDetail>(`/api/v1/school/kb/test-runs/${runId}`);
    },
    async listVersionApprovals(versionId) {
      const qs = buildQuery({ limit: 20 });
      const page = await request<{ items: KbVersionApprovalView[] }>(
        `/api/v1/school/kb/document-versions/${versionId}/approvals${qs}`,
      );
      return page.items;
    },
  };
}
