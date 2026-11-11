/** 知识库域 API（M3 KB-01A 上传/文档读 + KB-05B 发布四动作 + KB-06B 分块预览三读）。 */

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

/**
 * 分块列表行（后端 `KbChunkResponse`，`school_kb.py:973-989`；路由 :1064-1096）。
 * `text_markdown` 是保真渲染源（表格竖线/代码围栏原样），`text` 是后端搜索匹配列。
 */
export interface KbChunkView {
  id: string;
  chunk_type: string;
  chunk_index: number;
  parent_id: string | null;
  text: string;
  text_markdown: string;
  section_path: string | null;
  page_start: number | null;
  page_end: number | null;
  token_count: number;
  char_count: number;
  embedding_status: string;
  created_at: string;
}

/** 父/子块引用（详情家族导航）。 */
export interface KbChunkRef {
  id: string;
  chunk_index: number;
}

/** 分块详情（后端 `KbChunkDetailResponse` = 列表字段 + 版本归属 + 父/子引用）。 */
export interface KbChunkDetailView extends KbChunkView {
  document_version_id: string;
  parent: KbChunkRef | null;
  children: KbChunkRef[];
}

/** 章节统计行（chunk_count 降序、路径升序）。 */
export interface KbChunkSectionStat {
  section_path: string;
  chunk_count: number;
}

/** 版本分块统计（后端 `KbChunkStatsResponse`；per-version 固定口径，不随筛选变化）。 */
export interface KbChunkStatsView {
  total_chunks: number;
  parent_count: number;
  child_count: number;
  embedding_status: Record<string, number>;
  total_tokens: number;
  total_chars: number;
  section_count: number;
  sections: KbChunkSectionStat[];
}

/** 分块列表查询（四筛选 + 分页；后端语义：筛选在游标推进之前，`chunk_read.py:231-253`）。 */
export interface KbChunkListQuery {
  limit?: number;
  cursor?: string;
  q?: string;
  section_path?: string;
  chunk_type?: "parent" | "child";
  parent_id?: string;
}

// ---------------------------------------------------------------------------
// KB-07B：检索测试（后端契约 school_kb.py POST /versions/{id}/retrieval-test；
// 编排 retrieval_test.py——三查询旁路 + 可选 CPU rerank，rerank 失败降级不 5xx）
// ---------------------------------------------------------------------------

/** rerank 状态（applied=已重排；skipped=超时跳过；failed=其他失败；disabled=未启用）。 */
export type KbRerankStatus = "applied" | "skipped" | "failed" | "disabled";

/** 单路（dense/sparse）命中：分数 + 该路 1-based 排名（null = 该路 pool×2 内未召回）。 */
export interface KbRetrievalTestRouteHit {
  score: number;
  rank: number;
}

/** 检索测试结果行（「为什么召回」逐路可解释 + 最终引用标记）。 */
export interface KbRetrievalTestResultRow {
  chunk_id: string;
  chunk_index: number;
  chunk_type: string;
  section_path: string | null;
  page_start: number | null;
  page_end: number | null;
  /** 全文前 200 码点（完整内容经 getChunk 取）。 */
  text_preview: string;
  rrf_score: number;
  dense: KbRetrievalTestRouteHit | null;
  sparse: KbRetrievalTestRouteHit | null;
  rerank_score: number | null;
  final_rank: number;
  is_final_reference: boolean;
}

/** 检索测试响应（pool_size = len(results)；results 按 final_rank 升序）。 */
export interface KbRetrievalTestResult {
  version_id: string;
  query: string;
  top_k: number;
  with_rerank: boolean;
  rerank_status: KbRerankStatus;
  latency_ms: number;
  pool_size: number;
  results: KbRetrievalTestResultRow[];
}

/** 检索测试输入（query raw 码点 ≤300 strip 后非空；top_k 1-20；with_rerank 默认 false；
 * ACL 三参数为 KB-09 契约预留——后端设值即 422 `RETRIEVAL_TEST_ACL_UNAVAILABLE`，前端不提供入口）。 */
export interface KbRetrievalTestInput {
  query: string;
  top_k?: number;
  with_rerank?: boolean;
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
  /** 按版本列出分块（游标分页 + q/章节/类型/父块筛选；404 `VERSION_NOT_FOUND`）。 */
  listChunks(versionId: string, query?: KbChunkListQuery): Promise<Page<KbChunkView>>;
  /** 分块详情（父/子引用；不存在/跨租/范围外一律 404 `CHUNK_NOT_FOUND`，不泄露存在性）。 */
  getChunk(chunkId: string): Promise<KbChunkDetailView>;
  /** 版本分块统计（per-version 固定口径；404 `VERSION_NOT_FOUND`）。 */
  getChunkStats(versionId: string): Promise<KbChunkStatsView>;
  /** 单 query 检索测试（dense/sparse/RRF/rerank 分数与最终引用；跨租/范围外 404 `VERSION_NOT_FOUND`，
   * rerank 失败不报错走 `rerank_status` 降级）。 */
  retrievalTest(versionId: string, input: KbRetrievalTestInput): Promise<KbRetrievalTestResult>;
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
    async listChunks(versionId, query = {}) {
      return request<Page<KbChunkView>>(
        `/api/v1/school/kb/versions/${versionId}/chunks${buildQuery({
          limit: query.limit,
          cursor: query.cursor,
          q: query.q,
          section_path: query.section_path,
          chunk_type: query.chunk_type,
          parent_id: query.parent_id,
        })}`,
      );
    },
    async getChunk(chunkId) {
      return request<KbChunkDetailView>(`/api/v1/school/kb/chunks/${chunkId}`);
    },
    async getChunkStats(versionId) {
      return request<KbChunkStatsView>(`/api/v1/school/kb/versions/${versionId}/chunks/stats`);
    },
    async retrievalTest(versionId, input) {
      return request<KbRetrievalTestResult>(
        `/api/v1/school/kb/versions/${versionId}/retrieval-test`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            query: input.query,
            ...(input.top_k === undefined ? {} : { top_k: input.top_k }),
            ...(input.with_rerank === undefined ? {} : { with_rerank: input.with_rerank }),
          }),
        },
      );
    },
  };
}
