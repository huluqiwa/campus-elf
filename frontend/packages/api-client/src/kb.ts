/** 知识库域 API（M3 KB-01A 接口的前端消费：批量上传 + 文档列表/详情/版本）。 */

import { buildQuery, type AuthSessionRequest, type Page } from "./org";

export interface KbScanStateView {
  state: string;
  engine: string | null;
  signature: string | null;
  error_code: string | null;
  attempts: number;
  scanned_at: string | null;
}

export interface KbVersionView {
  id: string;
  version_no: number;
  filename: string;
  mime: string;
  size_bytes: number;
  status: string;
  scan: KbScanStateView;
  created_at: string;
}

export interface KbDocumentView {
  id: string;
  title: string;
  tags: string[];
  description: string | null;
  status: string;
  current_version: KbVersionView | null;
  created_at: string;
  updated_at: string;
}

export interface KbUploadResult {
  batch_id: string;
  replayed: boolean;
  documents: KbDocumentView[];
}

/** 单文件可选元数据；未提供时后端按文件名派生标题。 */
export interface KbFileMetadataInput {
  title?: string;
  tags?: string[];
  description?: string;
}

export interface KbUploadInput {
  files: File[];
  /** 8–128 位 `[A-Za-z0-9._:-]`；调用方按提交签名决定复用或换新。 */
  idempotencyKey: string;
  /** 与 `files` 等长；`undefined` 表示整段省略（后端走默认）。 */
  metadata?: KbFileMetadataInput[];
}

export interface KbApi {
  /** multipart 上传；不得手动设置 Content-Type（浏览器需自动带 boundary）。 */
  upload(input: KbUploadInput): Promise<KbUploadResult>;
  listDocuments(query?: { limit?: number; cursor?: string }): Promise<Page<KbDocumentView>>;
  getDocument(documentId: string): Promise<KbDocumentView>;
  listVersions(
    documentId: string,
    query?: { limit?: number; cursor?: string },
  ): Promise<Page<KbVersionView>>;
}

export function createKbApi(session: { request: AuthSessionRequest }): KbApi {
  const request = session.request;

  return {
    async upload(input) {
      const form = new FormData();
      for (const file of input.files) form.append("files", file);
      if (input.metadata !== undefined) form.set("metadata", JSON.stringify(input.metadata));
      return request<KbUploadResult>("/api/v1/school/kb/documents", {
        method: "POST",
        headers: { "Idempotency-Key": input.idempotencyKey },
        body: form,
      });
    },
    async listDocuments(query = {}) {
      return request<Page<KbDocumentView>>(
        `/api/v1/school/kb/documents${buildQuery({
          limit: query.limit,
          cursor: query.cursor,
        })}`,
      );
    },
    async getDocument(documentId) {
      return request<KbDocumentView>(`/api/v1/school/kb/documents/${documentId}`);
    },
    async listVersions(documentId, query = {}) {
      return request<Page<KbVersionView>>(
        `/api/v1/school/kb/documents/${documentId}/versions${buildQuery({
          limit: query.limit,
          cursor: query.cursor,
        })}`,
      );
    },
  };
}
