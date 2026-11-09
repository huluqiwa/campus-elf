/** 知识库域 API（M3 KB-01A 接口的前端消费：批量上传 + 文档列表/详情/版本；KB-05B 增发布/回滚/下线/删除）。 */

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

/**
 * 发布/回滚结果（后端 `KbPublishResponse`，`school_kb.py:815-825`）。
 * 回滚时 path id = 目标版本（D2-A），响应里的 `version_id` 是切换后的活跃版本。
 */
export interface KbPublishResult {
  document_id: string;
  version_id: string;
  version_no: number;
  status: string;
  alias: string;
  collection: string;
  previous_version_id: string | null;
  published_at: string;
  /** 同一活跃版本重放发布（alias 恢复路径）时为 true。 */
  replayed: boolean;
  alias_synced: boolean;
}

/** 下线/删除结果（后端 `KbVersionStateResponse`，`school_kb.py:828-835`）。 */
export interface KbVersionStateResult {
  document_id: string;
  version_id: string;
  status: string;
  /** 该版本在操作时是否为活跃发布（活跃发布被撤除时 UI 据此提示影响）。 */
  was_active: boolean;
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
  /** 发布版本（门禁 TEST_PASSED 由后端执行；同一活跃版本重放 = 重建 alias，`replayed=true`）。 */
  publishVersion(versionId: string): Promise<KbPublishResult>;
  /** 回滚到任意 PUBLISHED 目标版本（path id = 目标；D2-A 蓝绿共存语义）。 */
  rollbackVersion(targetVersionId: string): Promise<KbPublishResult>;
  /** 下线版本（PUBLISHED → OFFLINE 终态；collection 保留）。 */
  offlineVersion(versionId: string): Promise<KbVersionStateResult>;
  /** 软删版本（PUBLISHED → DELETED 终态；审计；collection 保留至 M6 清理）。 */
  deleteVersion(versionId: string): Promise<KbVersionStateResult>;
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
    async publishVersion(versionId) {
      return request<KbPublishResult>(`/api/v1/school/kb/versions/${versionId}/publish`, {
        method: "POST",
      });
    },
    async rollbackVersion(targetVersionId) {
      return request<KbPublishResult>(`/api/v1/school/kb/versions/${targetVersionId}/rollback`, {
        method: "POST",
      });
    },
    async offlineVersion(versionId) {
      return request<KbVersionStateResult>(`/api/v1/school/kb/versions/${versionId}/offline`, {
        method: "POST",
      });
    },
    async deleteVersion(versionId) {
      return request<KbVersionStateResult>(`/api/v1/school/kb/versions/${versionId}`, {
        method: "DELETE",
      });
    },
  };
}
