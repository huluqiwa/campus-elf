import type { Page, Route } from "@playwright/test";

/**
 * 知识库接口 mock（M3 KB-01B）：有状态的上传 → 文档、幂等键重放、详情扫描推进、游标分页。
 *
 * **TD-253 口径**：本文件的判据是手抄后端的（每条都注释了行号）。后端改了判据、这里不会变红，
 * 收口靠 KB-01C 的全栈 E2E；新增判据时必须去读一遍对应的服务层函数。
 *
 * 对齐的契约：
 * - 上传限制（15 个/批、50MB/文件、300MB/批，`validation.py:17-19`）由页面本地预检拦截，
 *   本 mock 不重复校验；真实后端的这条边界回归留给 KB-01C 全栈 E2E；
 * - 状态与扫描派生：`backend/app/kb/service.py:181-193`；
 * - 默认标题取文件名去扩展名并截 200（`service.py:634-636`）；
 * - 列表 `created_at DESC, id DESC` 游标分页（`service.py:462-479`），本 mock 按夹具顺序返回；
 * - 错误体 `{detail: {code, message, files?}}`（`service.py:316-319`）。
 */

export interface KbScanStateFixture {
  state: string;
  engine: string | null;
  signature: string | null;
  error_code: string | null;
  attempts: number;
  scanned_at: string | null;
}

export interface KbVersionFixture {
  id: string;
  version_no: number;
  filename: string;
  mime: string;
  size_bytes: number;
  status: string;
  scan: KbScanStateFixture;
  created_at: string;
}

/** 分块夹具（KB-06B；字段与后端 `KbChunkResponse` 一致——KB-10A 三列 + KB-10B `chunk_hash` 已含）。 */
export interface KbChunkFixture {
  id: string;
  chunk_type: "parent" | "child";
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
  /** KB-10A：人工编辑三列（spec :328）。 */
  is_human_edited: boolean;
  edited_by: string | null;
  edited_at: string | null;
  /** KB-10B（卡 E2）：编辑提交 base_hash 供给。 */
  chunk_hash: string;
}

export interface KbDocumentFixture {
  id: string;
  title: string;
  tags: string[];
  description: string | null;
  status: string;
  current_version: KbVersionFixture | null;
  /** 显式版本行（KB-05B）：提供时版本列表与发布动作直接读写这些行；缺省时按 current_version 合成旧行（只读）。 */
  versions?: KbVersionFixture[];
  created_at: string;
  updated_at: string;
}

export interface KbUploadCapture {
  idempotencyKey: string | null;
  fileNames: string[];
  metadata: string | null;
  contentType: string | null;
}

export interface KbVersionActionCapture {
  method: string;
  path: string;
  /** KB-10B：请求体原文（编辑提交用；其余动作空串）。 */
  body?: string;
}

export interface KbMockCaptures {
  uploads: KbUploadCapture[];
  listUrls: string[];
  detailIds: string[];
  versionUrls: string[];
  versionActions: KbVersionActionCapture[];
  /** KB-06B：分块详情请求路径（列表/统计走 versions 路由，进 versionActions）。 */
  chunkUrls: string[];
  /** KB-09C：人群配置请求路径（GET/PUT/confirm 走 document-versions 路由）。 */
  aclActions: KbVersionActionCapture[];
  /** KB-10B：编辑提交/派生动作捕获（method + path + body 原文）。 */
  chunkEditActions: KbVersionActionCapture[];
}

/** KB-09C：版本人群状态（有状态 mock；PUT 全量替换 + confirm 置确认）。 */
export interface KbAudienceStateFixture {
  audiences: { audience_type: string; audience_id: string | null }[];
  acl_confirmed: boolean;
}

/** KB-10B：版本块编辑状态（有状态 mock；POST edits 记录 + derive 翻转 confirmed）。 */
export interface KbChunkEditStateFixture {
  /** 提交记录（POST edits 追加；derive 把 submitted 翻 confirmed 并回填 derived_version_id）。 */
  edits: {
    chunk_id: string;
    source_version_id: string;
    status: string;
    before_hash: string;
    after_hash: string;
    new_content: string;
    reason: string;
    derived_version_id: string | null;
  }[];
  /** 派生产物（POST derive 追加；>0 = 该版本已派生 → 后续 derive 409 CHUNK_EDIT_STATE）。 */
  derived: { version_id: string; parent_version_id: string; edited_chunks: number }[];
}

export interface KbMockOptions {
  documents?: KbDocumentFixture[];
  /** 上传接口固定返回的错误（配合 `uploadErrorOnce` 只失败第一次）。 */
  uploadError?: { status: number; body: unknown };
  uploadErrorOnce?: boolean;
  /** 上传成功也返回 `replayed=true`（且不新建文档），用于验证重放提示。 */
  replayUploads?: boolean;
  /** 第 N 次列表请求返回 500（1 基），用于验证「数据可能不是最新」横幅。 */
  listFailOnRequest?: number;
  /** 版本列表的页大小（不传则按请求的 limit）。 */
  versionPageSize?: number;
  /** 列表响应延迟（毫秒），用于验证「加载中禁用翻页」。 */
  listDelayMs?: number;
  /** 详情/版本响应延迟（毫秒），用于验证「A 未返回就切 B」的竞态。 */
  detailDelayMs?: number;
  /** 详情首次读取时把 pending 推进为 passed（模拟扫描完成）；默认 true。 */
  autoPassOnDetail?: boolean;
  /** 这些文档 id 的详情/版本请求返回 404（模拟打开抽屉前被删）。 */
  missingDocumentIds?: string[];
  /** 详情固定返回的错误（模拟权限被撤/服务端拒绝），优先于 missingDocumentIds。 */
  detailError?: { status: number; body: unknown };
  /** KB-05B：初始活跃发布（docId → versionId），模拟库里的 alias 行；缺省 = 无活跃发布。 */
  activePublishByDocument?: Record<string, string>;
  /** KB-06B：分块夹具（versionId → 该版本全量块）；缺省 = 无分块（打开子视图为空集）。 */
  chunksByVersion?: Record<string, KbChunkFixture[]>;
  /** KB-07B：检索测试响应延迟（毫秒），用于验证「在途改筛选 → loading 收敛 + 迟响应丢弃」。 */
  retrievalDelayMs?: number;
  /** KB-09C：版本人群状态（versionId → 行集+确认态）；缺省 = 配 ("all", None) 已确认（卡 4 口径，
   * 既有检索断言零漂移——用户 token 恒含 all）。 */
  audiencesByVersion?: Record<string, KbAudienceStateFixture>;
  /** KB-09C：PUT audiences 固定返回错误（验证「服务端唯一门禁」422 兜底路径——审查 m3）。 */
  aclPutError?: { status: number; body: unknown };
  /** KB-10B：版本块编辑状态（versionId → edits/derived）；缺省 = 空态（无提交无派生）。 */
  chunkEditsByVersion?: Record<string, KbChunkEditStateFixture>;
}

export interface KbMock {
  captures: KbMockCaptures;
  state: {
    documents: KbDocumentFixture[];
    /** KB-06B：按 versionId 存取的分块夹具（断言走这里——mockKbApi 克隆了 options 传入值）。 */
    chunksByVersion: Record<string, KbChunkFixture[]>;
    /** KB-09C：按 versionId 存取的人群状态（PUT/confirm 就地改——断言走这里）。 */
    audiencesByVersion: Record<string, KbAudienceStateFixture>;
    /** KB-10B：按 versionId 存取的块编辑状态（edits/derive 就地改——断言走这里）。 */
    chunkEditsByVersion: Record<string, KbChunkEditStateFixture>;
  };
}

export function kbScanState(overrides: Partial<KbScanStateFixture> = {}): KbScanStateFixture {
  return {
    state: "passed",
    engine: "clamav",
    signature: null,
    error_code: null,
    attempts: 1,
    scanned_at: "2026-10-31T01:00:00Z",
    ...overrides,
  };
}

export function kbVersion(overrides: Partial<KbVersionFixture> = {}): KbVersionFixture {
  return {
    id: "61000000-0000-0000-0000-000000000001",
    version_no: 1,
    filename: "文档.pdf",
    mime: "application/pdf",
    size_bytes: 1024,
    status: "PENDING_APPROVAL",
    scan: kbScanState(),
    created_at: "2026-10-31T00:00:00Z",
    ...overrides,
  };
}

export function kbDocument(overrides: Partial<KbDocumentFixture> = {}): KbDocumentFixture {
  return {
    id: "60000000-0000-0000-0000-000000000001",
    title: "知识库文档",
    tags: [],
    description: null,
    status: "PENDING_APPROVAL",
    current_version: kbVersion(),
    created_at: "2026-10-31T00:00:00Z",
    updated_at: "2026-10-31T00:00:00Z",
    ...overrides,
  };
}

export function kbChunk(overrides: Partial<KbChunkFixture> = {}): KbChunkFixture {
  return {
    id: "62000000-0000-0000-0000-000000000001",
    chunk_type: "child",
    chunk_index: 0,
    parent_id: null,
    text: "默认块文本",
    text_markdown: "默认块文本",
    section_path: null,
    page_start: null,
    page_end: null,
    token_count: 10,
    char_count: 20,
    embedding_status: "ready",
    created_at: "2026-11-10T00:00:00Z",
    is_human_edited: false,
    edited_by: null,
    edited_at: null,
    chunk_hash: "a".repeat(64),
    ...overrides,
  };
}

/**
 * 默认六条夹具，覆盖三档文档状态、五种扫描状态（pending/passed/infected/error/skipped）、
 * 未知文档状态与未知扫描状态、以及 dev-skip 引擎（必须是警告色）。
 */
export const FIXTURE_KB_DOCUMENTS: KbDocumentFixture[] = [
  kbDocument({
    id: "60000000-0000-0000-0000-000000000001",
    title: "校园安全手册",
    tags: ["安全", "手册"],
    description: "校园安全制度汇编",
    status: "PENDING_APPROVAL",
    current_version: kbVersion({
      id: "61000000-0000-0000-0000-000000000001",
      version_no: 2,
      filename: "校园安全手册.pdf",
      size_bytes: 2048,
      scan: kbScanState(),
    }),
    created_at: "2026-10-31T03:00:00Z",
    updated_at: "2026-10-31T03:00:00Z",
  }),
  kbDocument({
    id: "60000000-0000-0000-0000-000000000002",
    title: "可疑附件",
    status: "SCAN_FAILED",
    current_version: kbVersion({
      id: "61000000-0000-0000-0000-000000000002",
      filename: "可疑附件.pdf",
      status: "SCAN_FAILED",
      scan: kbScanState({
        state: "infected",
        signature: "Eicar-Test-Signature",
        attempts: 1,
      }),
    }),
    created_at: "2026-10-31T02:00:00Z",
    updated_at: "2026-10-31T02:00:00Z",
  }),
  kbDocument({
    id: "60000000-0000-0000-0000-000000000003",
    title: "开发样例",
    status: "PENDING_APPROVAL",
    current_version: kbVersion({
      id: "61000000-0000-0000-0000-000000000003",
      filename: "开发样例.md",
      mime: "text/markdown",
      scan: kbScanState({
        state: "skipped",
        engine: "dev-skip",
        attempts: 0,
        scanned_at: null,
      }),
    }),
    created_at: "2026-10-31T01:30:00Z",
    updated_at: "2026-10-31T01:30:00Z",
  }),
  kbDocument({
    id: "60000000-0000-0000-0000-000000000004",
    title: "待扫描通知",
    status: "UPLOADED",
    current_version: kbVersion({
      id: "61000000-0000-0000-0000-000000000004",
      filename: "待扫描通知.docx",
      mime: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      status: "UPLOADED",
      scan: kbScanState({
        state: "pending",
        engine: null,
        attempts: 0,
        scanned_at: null,
      }),
    }),
    created_at: "2026-10-31T01:00:00Z",
    updated_at: "2026-10-31T01:00:00Z",
  }),
  kbDocument({
    id: "60000000-0000-0000-0000-000000000006",
    title: "扫描故障样例",
    status: "UPLOADED",
    current_version: kbVersion({
      id: "61000000-0000-0000-0000-000000000006",
      filename: "扫描故障样例.pdf",
      status: "UPLOADED",
      scan: kbScanState({
        state: "error",
        error_code: "SCAN_TIMEOUT",
        attempts: 3,
      }),
    }),
    created_at: "2026-10-31T00:45:00Z",
    updated_at: "2026-10-31T00:45:00Z",
  }),
  kbDocument({
    id: "60000000-0000-0000-0000-000000000005",
    title: "遗留档案",
    status: "ARCHIVED",
    current_version: kbVersion({
      id: "61000000-0000-0000-0000-000000000005",
      filename: "遗留档案.txt",
      mime: "text/plain",
      scan: kbScanState({
        state: "mystery",
        engine: "future-engine",
        attempts: 0,
        scanned_at: null,
      }),
    }),
    created_at: "2026-10-31T00:30:00Z",
    updated_at: "2026-10-31T00:30:00Z",
  }),
];

type MetadataFixture = { title?: string; tags?: string[]; description?: string };

function cloneVersion(version: KbVersionFixture): KbVersionFixture {
  return { ...version, scan: { ...version.scan } };
}

function cloneDocument(document: KbDocumentFixture): KbDocumentFixture {
  return {
    ...document,
    tags: [...document.tags],
    current_version: document.current_version ? cloneVersion(document.current_version) : null,
    versions: document.versions?.map(cloneVersion),
  };
}

/** 取 multipart 字段的原始文本（metadata 是 JSON 数组，不含二进制）。 */
function multipartField(raw: string, name: string): string | null {
  const index = raw.indexOf(`name="${name}"`);
  if (index < 0) return null;
  const start = raw.indexOf("\r\n\r\n", index);
  if (start < 0) return null;
  const end = raw.indexOf("\r\n--", start + 4);
  return raw.slice(start + 4, end < 0 ? undefined : end);
}

/** 镜像后端 `_default_title`（`service.py:634-636`）：文件名去扩展名，截 200。 */
function defaultTitle(filename: string): string {
  const base = filename.split(/[\\/]/).pop() ?? filename;
  const dot = base.lastIndexOf(".");
  const stem = dot > 0 && dot < base.length - 1 ? base.slice(0, dot) : base;
  return Array.from(stem.trim() || base)
    .slice(0, 200)
    .join("");
}

function json(route: Route, status: number, body: unknown): Promise<void> {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function delay(ms: number | undefined): Promise<void> {
  return ms === undefined ? Promise.resolve() : new Promise((resolve) => setTimeout(resolve, ms));
}

function notFound(route: Route): Promise<void> {
  return json(route, 404, { detail: { code: "DOCUMENT_NOT_FOUND", message: "文档不存在" } });
}

export async function mockKbApi(page: Page, options: KbMockOptions = {}): Promise<KbMock> {
  const documents = (options.documents ?? FIXTURE_KB_DOCUMENTS).map(cloneDocument);
  // KB-06B：分块夹具按 versionId 克隆存取（断言走 state.chunksByVersion——克隆语义与 documents 相同）
  const chunksByVersion: Record<string, KbChunkFixture[]> = {};
  for (const [versionId, chunks] of Object.entries(options.chunksByVersion ?? {})) {
    chunksByVersion[versionId] = chunks.map((chunk) => ({ ...chunk }));
  }
  const captures: KbMockCaptures = {
    uploads: [],
    listUrls: [],
    detailIds: [],
    versionUrls: [],
    versionActions: [],
    chunkUrls: [],
    aclActions: [],
    chunkEditActions: [],
  };
  /** KB-09C：按 versionId 存取的人群状态（PUT/confirm 直接改这里——断言走 state）。 */
  const audiencesByVersion: Record<string, KbAudienceStateFixture> = {};
  for (const [versionId, state] of Object.entries(options.audiencesByVersion ?? {})) {
    audiencesByVersion[versionId] = {
      audiences: state.audiences.map((item) => ({ ...item })),
      acl_confirmed: state.acl_confirmed,
    };
  }
  /** KB-10B：按 versionId 存取的块编辑状态（POST edits/derive 直接改这里——断言走 state）。 */
  const chunkEditsByVersion: Record<string, KbChunkEditStateFixture> = {};
  for (const [versionId, state] of Object.entries(options.chunkEditsByVersion ?? {})) {
    chunkEditsByVersion[versionId] = {
      edits: state.edits.map((item) => ({ ...item })),
      derived: state.derived.map((item) => ({ ...item })),
    };
  }
  /** KB-10B：块编辑状态读取（缺省空态）。 */
  function chunkEditStateFor(versionId: string): KbChunkEditStateFixture {
    if (chunkEditsByVersion[versionId] === undefined) {
      chunkEditsByVersion[versionId] = { edits: [], derived: [] };
    }
    return chunkEditsByVersion[versionId] as KbChunkEditStateFixture;
  }
  const replayByKey = new Map<string, { batch_id: string; documents: KbDocumentFixture[] }>();
  /** KB-05B：每文档活跃发布（mock 的 alias 行，含首次发布时间——重放保留原值，service.py:2577）；夹具不预置——「已发布但无指针」的重放拒绝路径因此可测。 */
  const activePublish = new Map<string, { versionId: string; publishedAt: string }>();
  for (const [docId, versionId] of Object.entries(options.activePublishByDocument ?? {})) {
    activePublish.set(docId, { versionId, publishedAt: new Date().toISOString() });
  }

  let listRequests = 0;
  let uploadSeq = 0;
  let batchSeq = 0;
  let uploadErrorConsumed = false;

  function versionsFor(document: KbDocumentFixture): KbVersionFixture[] {
    if (document.versions !== undefined) return document.versions;
    const latest = document.current_version;
    if (latest === null) return [];
    const versions: KbVersionFixture[] = [];
    for (let versionNo = latest.version_no; versionNo >= 1; versionNo -= 1) {
      versions.push(
        versionNo === latest.version_no
          ? cloneVersion(latest)
          : {
              ...cloneVersion(latest),
              id: `${latest.id}-v${versionNo}`,
              version_no: versionNo,
              created_at: `2026-10-${String(20 + versionNo).padStart(2, "0")}T00:00:00Z`,
            },
      );
    }
    return versions;
  }

  // ---- KB-06B：分块路由判据（逐字锚定 backend/app/kb/chunk_read.py） ----

  /** 稳定次序 chunk_index asc, id asc（chunk_read.py:241 列表排序 + :298 详情子块同键）。 */
  function byChunkOrder(a: KbChunkFixture, b: KbChunkFixture): number {
    return a.chunk_index - b.chunk_index || a.id.localeCompare(b.id);
  }

  function encodeChunkCursor(chunk: KbChunkFixture): string {
    return Buffer.from(JSON.stringify({ i: chunk.chunk_index, d: chunk.id })).toString("base64url");
  }

  function decodeChunkCursor(cursor: string): { index: number; id: string } {
    const payload = JSON.parse(Buffer.from(cursor, "base64url").toString("utf8")) as {
      i?: unknown;
      d?: unknown;
    };
    if (typeof payload.i !== "number" || typeof payload.d !== "string" || payload.i < 0) {
      throw new Error("bad cursor");
    }
    return { index: payload.i, id: payload.d };
  }

  /**
   * 列表页（chunk_read.py list_chunks）：筛选在游标推进**之前**（:231-241 先筛选、:242-253 游标）；
   * q = 大小写不敏感 contains（后端 ILIKE :234，通配符转义由后端集成测试背书、mock 夹具不含通配符）；
   * section_path 精确（:236）；取满 limit 才给游标（:254-257）。
   */
  function chunkPageFor(
    versionId: string,
    query: URLSearchParams,
  ): { items: KbChunkFixture[]; next_cursor: string | null } | "BAD_CURSOR" {
    const all = chunksByVersion[versionId] ?? [];
    let rows = all;
    const q = query.get("q");
    if (q !== null && q.trim() !== "") {
      const needle = q.trim().toLowerCase();
      rows = rows.filter((chunk) => chunk.text.toLowerCase().includes(needle));
    }
    const sectionPath = query.get("section_path");
    if (sectionPath !== null) rows = rows.filter((chunk) => chunk.section_path === sectionPath);
    const chunkType = query.get("chunk_type");
    if (chunkType !== null) rows = rows.filter((chunk) => chunk.chunk_type === chunkType);
    const parentId = query.get("parent_id");
    if (parentId !== null) rows = rows.filter((chunk) => chunk.parent_id === parentId);
    rows = [...rows].sort(byChunkOrder);
    const cursor = query.get("cursor");
    if (cursor !== null) {
      let marker: { index: number; id: string };
      try {
        marker = decodeChunkCursor(cursor);
      } catch {
        // chunk_read.py:249 raise → 路由层 INVALID_CURSOR 400（绝不静默回第一页，TD-243 口径）
        return "BAD_CURSOR";
      }
      rows = rows.filter(
        (chunk) =>
          chunk.chunk_index > marker.index ||
          (chunk.chunk_index === marker.index && chunk.id > marker.id),
      );
    }
    const limit = Math.max(1, Number(query.get("limit") ?? "20"));
    const page = rows.slice(0, limit);
    return {
      items: page,
      next_cursor: page.length === limit ? encodeChunkCursor(page[page.length - 1]!) : null,
    };
  }

  /**
   * 详情（chunk_read.py get_chunk）：不存在与「不在该版本」（跨版本同语义）同码同文 404（:283/:288）；
   * 父引用须同版本（:296-301 FK 只约束同租）；children 按 chunk_index 升序。
   */
  function chunkDetailFor(versionId: string, chunkId: string): KbChunkFixture | null {
    const all = chunksByVersion[versionId] ?? [];
    return all.find((chunk) => chunk.id === chunkId) ?? null;
  }

  function chunkChildrenFor(versionId: string, parentId: string): KbChunkFixture[] {
    return (chunksByVersion[versionId] ?? []).filter((chunk) => chunk.parent_id === parentId);
  }

  /** 统计（chunk_read.py chunk_stats）：分组聚合（:368-370）；sections 块数降序、路径升序（:389）；embedding 三态恒有键。 */
  function chunkStatsFor(versionId: string) {
    const all = chunksByVersion[versionId] ?? [];
    const embedding = { pending: 0, ready: 0, failed: 0 };
    const sections = new Map<string, number>();
    let parentCount = 0;
    let totalTokens = 0;
    let totalChars = 0;
    for (const chunk of all) {
      if (chunk.chunk_type === "parent") parentCount += 1;
      totalTokens += chunk.token_count;
      totalChars += chunk.char_count;
      if (chunk.embedding_status in embedding) {
        embedding[chunk.embedding_status as keyof typeof embedding] += 1;
      }
      if (chunk.section_path !== null) {
        sections.set(chunk.section_path, (sections.get(chunk.section_path) ?? 0) + 1);
      }
    }
    const sectionRows = [...sections.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([section_path, chunk_count]) => ({ section_path, chunk_count }));
    return {
      total_chunks: all.length,
      parent_count: parentCount,
      child_count: all.length - parentCount,
      embedding_status: embedding,
      total_tokens: totalTokens,
      total_chars: totalChars,
      section_count: sectionRows.length,
      sections: sectionRows,
    };
  }

  /**
   * KB-07B 检索测试（retrieval_test.py run_retrieval_test；确定性伪分数，简报 M3-KB-07B 卡 4A）：
   * query 字符 bigram 与夹具文本的包含计数决定命中序（同分按 chunk_index 稳定），dense/sparse
   * 同序（分数按位置递减），RRF 与后端/Qdrant 同公式 k=60；fake 重排按融合位置给升序分数
   * （与后端集成 FakeReranker 同形）→ 稳定翻转融合序。不模拟 409/502（简报卡 4）。
   */
  /** KB-09C：人群状态读取（缺省配 ("all", None) 已确认——卡 4 口径）。 */
  function audienceStateFor(versionId: string): KbAudienceStateFixture {
    return (
      audiencesByVersion[versionId] ?? {
        audiences: [{ audience_type: "all", audience_id: null }],
        acl_confirmed: true,
      }
    );
  }

  /** KB-09C：该版本 payload acl_tokens（构建期写入的版本级 token 集，测试用）。 */
  function audienceTokensFor(versionId: string): string[] {
    return audienceStateFor(versionId).audiences.map((item) =>
      item.audience_id === null ? item.audience_type : `${item.audience_type}:${item.audience_id}`,
    );
  }

  function retrievalRowsFor(
    versionId: string,
    query: string,
    topK: number,
    withRerank: boolean,
    viewTokens: string[],
    ignoreAcl: boolean,
  ): {
    rerank_status: "applied" | "disabled";
    pool_size: number;
    latency_ms: number;
    resolved_audience_tokens: string[];
    acl_filter_applied: boolean;
    results: unknown[];
  } {
    const all = chunksByVersion[versionId] ?? [];
    const payloadTokens = audienceTokensFor(versionId);
    const grams = new Set<string>();
    const chars = [...query];
    for (let i = 0; i + 1 < chars.length; i += 1) {
      grams.add((chars[i] ?? "") + (chars[i + 1] ?? ""));
    }
    const hitCount = (text: string): number => {
      let count = 0;
      for (let i = 0; i + 1 < text.length; i += 1) {
        if (grams.has(text.slice(i, i + 2))) count += 1;
      }
      return count;
    };
    const pool = Math.min(withRerank ? Math.max(20, topK) : topK, all.length);
    // KB-09B ACL 语义镜像（retrieval_test.py）：视角 token 空 → 零召回不发查询；
    // ignore_acl → 不过滤（诊断视图，命中 token 列恒空）。
    // 版本级 ACL：每块 payload token 同集。ignore_acl → 不过滤（诊断视图）；
    // 视角 token 空 → 零候选（fail-closed 不发查询）；有交集 → 全部候选；无交集 → 零候选。
    const hasOverlap = viewTokens.some((token) => payloadTokens.includes(token));
    const candidates = ignoreAcl || hasOverlap ? all : [];
    const scored = candidates
      .map((chunk) => ({ chunk, hits: hitCount(chunk.text) }))
      .sort((a, b) => b.hits - a.hits || a.chunk.chunk_index - b.chunk.chunk_index)
      .slice(0, pool);
    const poolSize = scored.length;
    const rowFor = (
      entry: { chunk: KbChunkFixture },
      position: number,
      rerankScore: number | null,
      finalRank: number,
    ) => {
      const rank = position + 1;
      // 位置递减的路由分数（0.9×10^4 精度）；dense/sparse 同序同分是 mock 的简化
      const routeScore = Math.round((1 - position / (poolSize + 1)) * 10000) / 10000;
      return {
        chunk_id: entry.chunk.id,
        chunk_index: entry.chunk.chunk_index,
        chunk_type: entry.chunk.chunk_type,
        section_path: entry.chunk.section_path,
        page_start: entry.chunk.page_start,
        page_end: entry.chunk.page_end,
        text_preview: entry.chunk.text.slice(0, 200),
        rrf_score: Number((1 / (60 + rank) + 1 / (60 + rank)).toFixed(6)),
        dense: { score: routeScore, rank },
        sparse: { score: routeScore, rank },
        rerank_score: rerankScore,
        final_rank: finalRank,
        is_final_reference: finalRank <= topK,
        // KB-09B：该块 payload token ∩ 视角 token（版本级 ACL → 每块同集）
        acl_matched_tokens: payloadTokens.filter((token) => viewTokens.includes(token)),
      };
    };
    if (!withRerank) {
      return {
        rerank_status: "disabled",
        pool_size: poolSize,
        latency_ms: 1.2,
        resolved_audience_tokens: viewTokens,
        acl_filter_applied: !ignoreAcl,
        results: scored.map((entry, position) => rowFor(entry, position, null, position + 1)),
      };
    }
    const results = scored
      .map((entry, position) => ({
        entry,
        position,
        rerankScore: Math.round(((position + 1) / poolSize) * 10000) / 10000,
      }))
      .sort((a, b) => b.rerankScore - a.rerankScore)
      .map((item, finalPosition) =>
        rowFor(item.entry, item.position, item.rerankScore, finalPosition + 1),
      );
    return {
      rerank_status: "applied",
      pool_size: poolSize,
      latency_ms: 1.2,
      resolved_audience_tokens: viewTokens,
      acl_filter_applied: !ignoreAcl,
      results,
    };
  }

  /** 上传成功后新建的文档：状态 UPLOADED、扫描 pending（`service.py:181-193` 的初始映射）。 */
  function makeUploadedDocument(
    filename: string,
    metadata: MetadataFixture | undefined,
    now: string,
  ): KbDocumentFixture {
    uploadSeq += 1;
    const sequence = String(uploadSeq).padStart(12, "0");
    return {
      id: `70000000-0000-0000-0000-${sequence}`,
      title: metadata?.title ?? defaultTitle(filename),
      tags: metadata?.tags ?? [],
      description: metadata?.description ?? null,
      status: "UPLOADED",
      current_version: {
        id: `71000000-0000-0000-0000-${sequence}`,
        version_no: 1,
        filename,
        mime: "application/pdf",
        size_bytes: 1024,
        status: "UPLOADED",
        scan: {
          state: "pending",
          engine: null,
          signature: null,
          error_code: null,
          attempts: 0,
          scanned_at: null,
        },
        created_at: now,
      },
      created_at: now,
      updated_at: now,
    };
  }

  async function handleUpload(route: Route): Promise<void> {
    const request = route.request();
    const raw = request.postDataBuffer()?.toString("utf8") ?? "";
    const fileNames = [...raw.matchAll(/name="files"; filename="([^"]*)"/g)].map(
      (match) => match[1] ?? "",
    );
    const metadataRaw = multipartField(raw, "metadata");
    const idempotencyKey = request.headers()["idempotency-key"] ?? null;
    captures.uploads.push({
      idempotencyKey,
      fileNames,
      metadata: metadataRaw,
      contentType: request.headers()["content-type"] ?? null,
    });

    if (options.uploadError && (!options.uploadErrorOnce || !uploadErrorConsumed)) {
      uploadErrorConsumed = true;
      return json(route, options.uploadError.status, options.uploadError.body);
    }

    if (idempotencyKey !== null) {
      const replay = replayByKey.get(idempotencyKey);
      if (replay) return json(route, 201, { ...replay, replayed: true });
    }

    let metadata: MetadataFixture[] | null = null;
    if (metadataRaw !== null) {
      metadata = JSON.parse(metadataRaw) as MetadataFixture[];
    }
    const now = new Date().toISOString();
    const uploaded = fileNames.map((filename, index) =>
      makeUploadedDocument(filename, metadata?.[index], now),
    );
    batchSeq += 1;
    const batch = {
      batch_id: `80000000-0000-0000-0000-${String(batchSeq).padStart(12, "0")}`,
      documents: uploaded,
    };
    if (!options.replayUploads) {
      documents.unshift(...uploaded.map(cloneDocument));
      if (idempotencyKey !== null) replayByKey.set(idempotencyKey, batch);
    }
    return json(route, 201, { ...batch, replayed: options.replayUploads === true });
  }

  async function handleList(route: Route, url: URL): Promise<void> {
    captures.listUrls.push(route.request().url());
    await delay(options.listDelayMs);
    listRequests += 1;
    if (options.listFailOnRequest === listRequests) {
      return json(route, 500, { detail: "服务暂时不可用" });
    }
    const limit = Number(url.searchParams.get("limit") ?? "20");
    const offset = Number(url.searchParams.get("cursor") ?? "0") || 0;
    const items = documents.slice(offset, offset + limit).map(cloneDocument);
    const nextCursor = offset + limit < documents.length ? String(offset + limit) : null;
    return json(route, 200, { items, next_cursor: nextCursor });
  }

  async function handleDetail(route: Route, id: string): Promise<void> {
    captures.detailIds.push(id);
    await delay(options.detailDelayMs);
    if (options.detailError)
      return json(route, options.detailError.status, options.detailError.body);
    if (options.missingDocumentIds?.includes(id)) return notFound(route);
    const document = documents.find((item) => item.id === id);
    if (!document) return notFound(route);

    // 模拟扫描 worker 完成：pending → passed（状态映射见 `service.py:181-193`）
    if (options.autoPassOnDetail !== false && document.current_version?.scan.state === "pending") {
      const now = new Date().toISOString();
      document.status = "PENDING_APPROVAL";
      document.current_version = {
        ...document.current_version,
        status: "PENDING_APPROVAL",
        scan: {
          ...document.current_version.scan,
          state: "passed",
          engine: "clamav",
          attempts: 1,
          scanned_at: now,
        },
      };
      document.updated_at = now;
    }
    // KB-08B：详情回包带活跃发布指针（mock 的 alias 行 = activePublish；列表/上传回包不带
    // 该字段——KB-08A 契约边界在 mock 层同样成立）
    return json(route, 200, {
      ...cloneDocument(document),
      active_version_id: activePublish.get(id)?.versionId ?? null,
    });
  }

  async function handleVersions(route: Route, url: URL, id: string): Promise<void> {
    captures.versionUrls.push(route.request().url());
    await delay(options.detailDelayMs);
    if (options.missingDocumentIds?.includes(id)) return notFound(route);
    const document = documents.find((item) => item.id === id);
    if (!document) return notFound(route);

    const all = versionsFor(document);
    const limit = options.versionPageSize ?? Number(url.searchParams.get("limit") ?? "20");
    const offset = Number(url.searchParams.get("cursor") ?? "0") || 0;
    const items = all.slice(offset, offset + limit);
    const nextCursor = offset + limit < all.length ? String(offset + limit) : null;
    return json(route, 200, { items, next_cursor: nextCursor });
  }

  // ---- KB-05B：发布/回滚/下线/删除（判据手抄自 service.py 的 publish/rollback/offline/delete） ----

  /** 跨文档按版本 id 找行（只查显式 versions；合成行不可变、不参与动作）。 */
  function findVersion(
    versionId: string,
  ): { document: KbDocumentFixture; version: KbVersionFixture } | null {
    for (const document of documents) {
      const version = document.versions?.find((item) => item.id === versionId);
      if (version) return { document, version };
    }
    return null;
  }

  /**
   * 状态写入：版本行 + current_version（同 id 时镜像状态）+ 文档行镜像（design §4.3）。
   * `syncDocument`：后端 offline/delete 只在 was_active 时同步文档行（service.py:2826-2845/2903-2922，
   * 非活跃 PUBLISHED 版本下线后文档行保持 PUBLISHED）；publish/rollback 无条件同步。
   */
  function setVersionStatus(
    document: KbDocumentFixture,
    version: KbVersionFixture,
    status: string,
    now: string,
    syncDocument = true,
  ): void {
    version.status = status;
    if (document.current_version?.id === version.id) {
      document.current_version.status = status;
    }
    if (syncDocument) {
      document.status = status;
    }
    document.updated_at = now;
  }

  /** publish/rollback 把文档的当前版本指针切到目标版本行（service.py:2592-2593/2760-2761）。 */
  function migrateCurrentVersion(document: KbDocumentFixture, version: KbVersionFixture): void {
    document.current_version = version;
    document.updated_at = new Date().toISOString();
  }

  function publishPayload(
    document: KbDocumentFixture,
    version: KbVersionFixture,
    previousVersionId: string | null,
    replayed: boolean,
    publishedAt: string,
  ): Record<string, unknown> {
    return {
      document_id: document.id,
      version_id: version.id,
      version_no: version.version_no,
      status: "PUBLISHED",
      alias: `tenant_mock_kb_${document.id.slice(0, 8)}_active`,
      collection: `tenant_mock_kb_v${version.version_no}`,
      previous_version_id: previousVersionId,
      published_at: publishedAt,
      replayed,
      alias_synced: true,
    };
  }

  await page.route("**/api/v1/school/kb/versions/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    captures.versionActions.push({ method, path });
    const match = path.match(
      /\/school\/kb\/versions\/([^/]+)(\/(publish|rollback|offline|chunks|retrieval-test)(\/stats)?)?$/,
    );
    if (!match) return json(route, 404, { detail: "未 mock 的版本路径" });
    const versionId = match[1] ?? "";
    const action = match[3];

    // 404 探测不区分跨租/范围外（后端同口径，不泄露存在性；service.py:2460 等）。
    const found = findVersion(versionId);
    if (!found) {
      return json(route, 404, { detail: { code: "VERSION_NOT_FOUND", message: "文档版本不存在" } });
    }
    const { document, version } = found;
    const now = new Date().toISOString();
    const active = activePublish.get(document.id) ?? null;
    const activeVersionId = active?.versionId ?? null;

    // KB-06B 分块列表/统计（chunk_read.py list_chunks/chunk_stats；版本 404 探测已由上方 findVersion 同口径处理）
    if (method === "GET" && action === "chunks") {
      if (match[4] !== undefined) {
        return json(route, 200, chunkStatsFor(versionId));
      }
      const page = chunkPageFor(versionId, url.searchParams);
      if (page === "BAD_CURSOR") {
        return json(route, 400, { detail: { code: "INVALID_CURSOR", message: "分页游标无效" } });
      }
      return json(route, 200, page);
    }

    // KB-07B 检索测试（retrieval_test.py 编排；school_kb.py KbRetrievalTestRequest 校验）。
    // 只镜像路由层可达的 422：纯空 query 被后端 pydantic 先拦（数组 shape，mock 不复刻），
    // 全空白 strip 后空走路由 KbError（school_kb.py:1251-1253，{code,message} shape）——
    // 前端预检先拦，正常 UI 到不了这里（防御镜像，审查 m1 口径）。top_k 校验是
    // pydantic 层（数组 shape），mock 不复刻。
    if (method === "POST" && action === "retrieval-test") {
      const body = (request.postDataJSON() ?? {}) as {
        query?: string;
        top_k?: number;
        with_rerank?: boolean;
        as_user_id?: string;
        as_audiences?: string[];
        ignore_acl?: boolean;
      };
      const delayMs = options.retrievalDelayMs ?? 0;
      if (delayMs > 0) await new Promise((resolve) => setTimeout(resolve, delayMs));
      if (body.query !== undefined && body.query.trim() === "") {
        return json(route, 422, {
          detail: { code: "RETRIEVAL_TEST_QUERY_REQUIRED", message: "检索词不能为空" },
        });
      }
      // KB-09B 视角（retrieval_test.py）：默认操作者；as_audiences 优先于 as_user_id
      // （后端两者同设 422 PERSPECTIVE_CONFLICT）；ignore_acl 不过滤但回显 token。
      // 操作者/目标用户 token mock 简化为 ["all"]+self（role 集不模拟——all 恒注入是
      // 零漂移口径；真实解析器行为由集成测试背书）。
      // as_audiences 不做结构校验（后端 parse_audience_tokens 422 族，audience.py:170-207）——
      // 前端无 as_audiences 预检入口、非法 token 走后端唯一门禁；mock 伪 token 零召回与
      // 后端「无交集零召回」同形（KB-09C 审查 m2 登记）。
      const ignoreAcl = body.ignore_acl === true;
      const viewTokens =
        body.as_audiences !== undefined && body.as_audiences !== null
          ? body.as_audiences
          : body.as_user_id !== undefined && body.as_user_id !== null
            ? [`user:${body.as_user_id}`, "all"]
            : ["all"];
      const topK = body.top_k ?? 10;
      return json(
        route,
        200,
        retrievalRowsFor(
          versionId,
          body.query ?? "",
          topK,
          body.with_rerank === true,
          viewTokens,
          ignoreAcl,
        ),
      );
    }

    if (method === "POST" && action === "publish") {
      // 幂等重放判定（service.py:2473）：已 PUBLISHED 走恢复路径，不做 TEST_PASSED 断言。
      const replay = version.status === "PUBLISHED";
      if (!replay && version.status !== "TEST_PASSED") {
        // assert_publishable（status.py:182）
        return json(route, 409, {
          detail: {
            code: "KB_NOT_TEST_PASSED",
            message: `版本状态 ${version.status} 不可发布（必须先通过测试）`,
          },
        });
      }
      if (replay) {
        if (active === null) {
          // service.py:2506-2512：已发布但指针已撤除（被下线/删除），重放发布不可达
          return json(route, 409, {
            detail: {
              code: "KB_PUBLISHED_NOT_ACTIVE",
              message: "版本已发布但活跃发布指针已撤除（活跃版本被下线或删除），不可重放发布",
            },
          });
        }
        if (active.versionId !== version.id) {
          // service.py:2513-2519：已发布但不是活跃发布，走回滚
          return json(route, 409, {
            detail: {
              code: "KB_PUBLISHED_NOT_ACTIVE",
              message: "版本已发布但不是活跃发布，请使用回滚端点切换",
            },
          });
        }
        // 重放 = 重建 alias/payload，状态与首次发布时间不变（service.py:2521/2577 previous=None）
        return json(route, 200, publishPayload(document, version, null, true, active.publishedAt));
      }
      // 索引就绪复核在真实后端做（KB_INDEX_NOT_READY，service.py:2479）；mock 版本恒视为已索引。
      const previous = activeVersionId;
      activePublish.set(document.id, { versionId: version.id, publishedAt: now });
      migrateCurrentVersion(document, version);
      setVersionStatus(document, version, "PUBLISHED", now);
      return json(route, 200, publishPayload(document, version, previous, false, now));
    }

    if (method === "POST" && action === "rollback") {
      // assert_rollback_target（status.py:215）先于活跃判定（service.py:2676→2683→2686）
      if (version.status !== "PUBLISHED") {
        return json(route, 409, {
          detail: {
            code: "KB_NOT_PUBLISHED",
            message: `回滚目标状态 ${version.status} 非法（目标必须是 PUBLISHED）`,
          },
        });
      }
      if (activeVersionId === null) {
        // service.py:2683
        return json(route, 409, {
          detail: {
            code: "KB_NO_ACTIVE_PUBLISH",
            message: "该文档当前没有活跃发布，无法回滚",
          },
        });
      }
      if (activeVersionId === version.id) {
        // service.py:2686
        return json(route, 409, {
          detail: {
            code: "KB_ROLLBACK_TARGET_IS_ACTIVE",
            message: "目标版本已是当前活跃发布",
          },
        });
      }
      // 蓝绿共存：原活跃版本保持 PUBLISHED，只切指针（state-machines §3）
      const previous = activeVersionId;
      activePublish.set(document.id, { versionId: version.id, publishedAt: now });
      migrateCurrentVersion(document, version);
      setVersionStatus(document, version, "PUBLISHED", now);
      return json(route, 200, publishPayload(document, version, previous, false, now));
    }

    if (method === "POST" && action === "offline") {
      // assert_offline_allowed（status.py:193）
      if (version.status !== "PUBLISHED") {
        return json(route, 409, {
          detail: {
            code: "KB_NOT_PUBLISHED",
            message: `版本状态 ${version.status} 不可下线（只有 PUBLISHED 可下线）`,
          },
        });
      }
      const wasActive = activeVersionId === version.id;
      if (wasActive) activePublish.delete(document.id);
      // 后端只在 was_active 时同步文档行（service.py:2826-2845）
      setVersionStatus(document, version, "OFFLINE", now, wasActive);
      return json(route, 200, {
        document_id: document.id,
        version_id: version.id,
        status: "OFFLINE",
        was_active: wasActive,
      });
    }

    if (method === "DELETE" && action === undefined) {
      // assert_deletable（status.py:204）；软删终态，collection 保留（清理归 M6，TD-303）
      if (version.status !== "PUBLISHED") {
        return json(route, 409, {
          detail: {
            code: "KB_NOT_PUBLISHED",
            message: `版本状态 ${version.status} 不可删除（只有 PUBLISHED 可删除）`,
          },
        });
      }
      const wasActive = activeVersionId === version.id;
      if (wasActive) activePublish.delete(document.id);
      // 后端只在 was_active 时同步文档行（service.py:2903-2922）
      setVersionStatus(document, version, "DELETED", now, wasActive);
      return json(route, 200, {
        document_id: document.id,
        version_id: version.id,
        status: "DELETED",
        was_active: wasActive,
      });
    }

    return json(route, 404, { detail: "未 mock 的版本路径" });
  });

  // KB-06B 分块详情（chunk_read.py get_chunk）：按块自身版本定域；不存在/跨租/跨版本同码同文 404。
  await page.route("**/api/v1/school/kb/chunks/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    captures.chunkUrls.push(path);
    // KB-10B（卡 E1）：POST /chunks/{id}/edits——乐观锁 409（base_hash 对不上）/ 201 记录；
    // 结构/长度 422 族由前端预检拦，mock 不复刻（服务端唯一门禁口径，09C 先例）。
    const editMatch = path.match(/\/school\/kb\/chunks\/([^/]+)\/edits$/);
    if (editMatch !== null && request.method() === "POST") {
      const chunkId = editMatch[1] ?? "";
      const owner = Object.keys(chunksByVersion).find((versionId) =>
        (chunksByVersion[versionId] ?? []).some((chunk) => chunk.id === chunkId),
      );
      if (owner === undefined) {
        return json(route, 404, { detail: { code: "CHUNK_NOT_FOUND", message: "分块不存在" } });
      }
      const chunkRow = (chunksByVersion[owner] ?? []).find((chunk) => chunk.id === chunkId);
      if (chunkRow === undefined || chunkRow.chunk_type !== "child") {
        return json(route, 422, {
          detail: { code: "CHUNK_EDIT_TARGET_INVALID", message: "仅子块文本可编辑" },
        });
      }
      captures.chunkEditActions.push({
        method: "POST",
        path,
        body: typeof request.postData() === "string" ? (request.postData() ?? "") : "",
      });
      const body = (request.postDataJSON() ?? {}) as {
        base_hash?: string;
        new_content?: string;
        reason?: string;
      };
      if (body.base_hash !== chunkRow.chunk_hash) {
        return json(route, 409, {
          detail: { code: "CHUNK_CONFLICT", message: "分块内容已被他人编辑（base_hash 过期）" },
        });
      }
      const state = chunkEditStateFor(owner);
      // 镜像后端部分唯一语义（uq_kb_chunk_edits_source_chunk_submitted）：同块已有
      // submitted → 409 CHUNK_CONFLICT（KB-10A N1 口径）。
      if (state.edits.some((edit) => edit.chunk_id === chunkId && edit.status === "submitted")) {
        return json(route, 409, {
          detail: { code: "CHUNK_CONFLICT", message: "该分块已有待派生的编辑提交" },
        });
      }
      state.edits.push({
        chunk_id: chunkId,
        source_version_id: owner,
        status: "submitted",
        before_hash: chunkRow.chunk_hash,
        after_hash: "f".repeat(64),
        new_content: body.new_content ?? "",
        reason: body.reason ?? "",
        derived_version_id: null,
      });
      return json(route, 201, state.edits[state.edits.length - 1]);
    }
    const chunkId = path.match(/\/school\/kb\/chunks\/([^/]+)$/)?.[1] ?? "";
    const owner = Object.keys(chunksByVersion).find((versionId) =>
      (chunksByVersion[versionId] ?? []).some((chunk) => chunk.id === chunkId),
    );
    const chunk = owner === undefined ? null : chunkDetailFor(owner, chunkId);
    if (owner === undefined || chunk === null) {
      return json(route, 404, { detail: { code: "CHUNK_NOT_FOUND", message: "分块不存在" } });
    }
    const children = chunkChildrenFor(owner, chunkId)
      .sort(byChunkOrder)
      .map((child) => ({ id: child.id, chunk_index: child.chunk_index }));
    // 父引用须同版本（chunk_read.py:308-311：FK 只约束同租，跨版本按无父呈现，不伪造引用）
    const parentRow = chunk.parent_id === null ? null : chunkDetailFor(owner, chunk.parent_id);
    const parent =
      parentRow === null ? null : { id: parentRow.id, chunk_index: parentRow.chunk_index };
    return json(route, 200, {
      ...chunk,
      document_version_id: owner,
      parent,
      children,
    });
  });

  await page.route("**/api/v1/school/kb/documents**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (method === "POST" && path.endsWith("/school/kb/documents")) {
      return handleUpload(route);
    }
    if (method === "GET" && path.endsWith("/school/kb/documents")) {
      return handleList(route, url);
    }
    const match = path.match(/\/school\/kb\/documents\/([^/]+)(\/versions)?$/);
    if (method === "GET" && match) {
      const id = match[1] ?? "";
      return match[2] ? handleVersions(route, url, id) : handleDetail(route, id);
    }
    return json(route, 404, { detail: "未 mock 的知识库路径" });
  });

  // KB-09C 人群配置路由（后注册优先于 documents**——glob 会吞 document-versions 前缀）。
  // 判据锚点：GET 挂 view（school_kb.py GET audiences）、PUT 全量替换+置未确认、
  // confirm 空集 409 ACL_REQUIRED / 已确认 409 ACL_ALREADY_CONFIRMED（service.py:2356-2474）；
  // PUT 结构校验族（audience.py parse_audience_items）由前端预检拦，mock 不复刻。
  await page.route("**/api/v1/school/kb/document-versions/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    captures.aclActions.push({ method, path });
    const editListMatch = path.match(/\/school\/kb\/document-versions\/([^/]+)\/chunk-edits$/);
    if (editListMatch !== null && request.method() === "GET") {
      const versionId = editListMatch[1] ?? "";
      if (findVersion(versionId) === null) {
        return json(route, 404, {
          detail: { code: "VERSION_NOT_FOUND", message: "文档版本不存在" },
        });
      }
      return json(route, 200, chunkEditStateFor(versionId).edits);
    }
    const deriveMatch = path.match(/\/school\/kb\/document-versions\/([^/]+)\/derive$/);
    if (deriveMatch !== null && request.method() === "POST") {
      const versionId = deriveMatch[1] ?? "";
      if (findVersion(versionId) === null) {
        return json(route, 404, {
          detail: { code: "VERSION_NOT_FOUND", message: "文档版本不存在" },
        });
      }
      captures.chunkEditActions.push({ method: "POST", path, body: "" });
      const state = chunkEditStateFor(versionId);
      const submitted = state.edits.filter((edit) => edit.status === "submitted");
      if (state.derived.length > 0) {
        return json(route, 409, {
          detail: { code: "CHUNK_EDIT_STATE", message: "该版本已派生——请到派生版本继续" },
        });
      }
      if (submitted.length === 0) {
        return json(route, 409, {
          detail: { code: "DERIVE_NO_EDITS", message: "该版本没有待派生的编辑提交" },
        });
      }
      const derivedVersionId = `7${versionId.slice(1)}`;
      for (const edit of submitted) {
        edit.status = "confirmed";
        edit.derived_version_id = derivedVersionId;
      }
      state.derived.push({
        version_id: derivedVersionId,
        parent_version_id: versionId,
        edited_chunks: submitted.length,
      });
      return json(route, 201, {
        version_id: derivedVersionId,
        parent_version_id: versionId,
        derivation_type: "chunk_edit",
        edited_chunks: submitted.length,
      });
    }
    const match = path.match(/\/school\/kb\/document-versions\/([^/]+)(\/audiences)(\/confirm)?$/);
    if (!match) return json(route, 404, { detail: "未 mock 的 document-versions 路径" });
    const versionId = match[1] ?? "";
    const action = match[3];
    const found = findVersion(versionId);
    if (!found) {
      return json(route, 404, { detail: { code: "VERSION_NOT_FOUND", message: "文档版本不存在" } });
    }
    // 显式写回 audiencesByVersion（惰性默认对象不落 dict——断言走 state 必须可读）
    if (audiencesByVersion[versionId] === undefined) {
      audiencesByVersion[versionId] = audienceStateFor(versionId);
    }
    const state = audiencesByVersion[versionId] as KbAudienceStateFixture;
    const viewPayload = () => ({
      version_id: versionId,
      audiences: state.audiences,
      acl_confirmed: state.acl_confirmed,
      acl_confirmed_by: state.acl_confirmed ? "60000000-0000-0000-0000-000000000009" : null,
      acl_confirmed_at: state.acl_confirmed ? "2026-11-14T00:00:00Z" : null,
    });
    if (method === "GET") {
      return json(route, 200, viewPayload());
    }
    if (method === "PUT") {
      if (options.aclPutError !== undefined) {
        return json(route, options.aclPutError.status, options.aclPutError.body);
      }
      const body = (request.postDataJSON() ?? {}) as {
        audiences?: { audience_type: string; audience_id: string | null }[];
      };
      state.audiences = (body.audiences ?? []).map((item) => ({ ...item }));
      state.acl_confirmed = false; // 全量替换置未确认（spec :309）
      return json(route, 200, viewPayload());
    }
    if (method === "POST" && action === "/confirm") {
      if (state.audiences.length === 0) {
        return json(route, 409, {
          detail: { code: "ACL_REQUIRED", message: "人群 ACL 未配置（先设置查阅人群）" },
        });
      }
      if (state.acl_confirmed) {
        return json(route, 409, {
          detail: { code: "ACL_ALREADY_CONFIRMED", message: "人群 ACL 已确认" },
        });
      }
      state.acl_confirmed = true;
      return json(route, 200, viewPayload());
    }
    return json(route, 404, { detail: "未 mock 的人群动作" });
  });

  return {
    captures,
    state: { documents, chunksByVersion, audiencesByVersion, chunkEditsByVersion },
  };
}
