/** 批量导入域 API（M2-ORG02 接口的前端消费；multipart 上传 + 进度/错误行/重试）。 */

import type { AuthSessionRequest } from "./org";

export interface ImportJobView {
  id: string;
  type: "teachers" | "students";
  conflict: "error" | "skip";
  status: "pending" | "running" | "completed" | "completed_with_errors" | "failed";
  total_rows: number;
  success_rows: number;
  failed_rows: number;
  skipped_rows: number;
  retried_by: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  message: string | null;
}

export interface ImportRowErrorView {
  row_no: number;
  field: string;
  error_code: string;
  message: string;
}

export interface ImportErrorsPage {
  items: ImportRowErrorView[];
  next_cursor: number | null;
}

export interface ImportUploadInput {
  type: "teachers" | "students";
  conflict: "error" | "skip";
  file: File;
}

function jsonInit(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

/** 重试返回派生引用（后端 ImportRetryResponse），不是 job 本体——调用方需按新 id 重新轮询。 */
export interface ImportRetryRef {
  source_job_id: string;
  job_id: string;
}

export interface ImportsApi {
  /** multipart 上传；不得手动设置 Content-Type（浏览器需自动带 multipart boundary）。 */
  upload(input: ImportUploadInput): Promise<ImportJobView>;
  get(jobId: string): Promise<ImportJobView>;
  listErrors(jobId: string, query?: { limit?: number; cursor?: number }): Promise<ImportErrorsPage>;
  retry(jobId: string): Promise<ImportRetryRef>;
}

export function createImportsApi(session: { request: AuthSessionRequest }): ImportsApi {
  const request = session.request;

  return {
    async upload(input) {
      const form = new FormData();
      form.set("type", input.type);
      form.set("conflict", input.conflict);
      form.set("file", input.file);
      return request<ImportJobView>("/api/v1/school/imports", {
        method: "POST",
        body: form,
      });
    },
    async get(jobId) {
      return request<ImportJobView>(`/api/v1/school/imports/${jobId}`);
    },
    async listErrors(jobId, query = {}) {
      const search = new URLSearchParams();
      if (query.limit !== undefined) search.set("limit", String(query.limit));
      if (query.cursor !== undefined) search.set("cursor", String(query.cursor));
      const qs = search.toString();
      return request<ImportErrorsPage>(
        `/api/v1/school/imports/${jobId}/errors${qs ? `?${qs}` : ""}`,
      );
    },
    async retry(jobId) {
      return request<ImportRetryRef>(`/api/v1/school/imports/${jobId}/retry`, jsonInit("POST", {}));
    },
  };
}
