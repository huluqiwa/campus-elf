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

export interface KbDocumentFixture {
  id: string;
  title: string;
  tags: string[];
  description: string | null;
  status: string;
  current_version: KbVersionFixture | null;
  created_at: string;
  updated_at: string;
}

export interface KbUploadCapture {
  idempotencyKey: string | null;
  fileNames: string[];
  metadata: string | null;
  contentType: string | null;
}

export interface KbMockCaptures {
  uploads: KbUploadCapture[];
  listUrls: string[];
  detailIds: string[];
  versionUrls: string[];
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
}

export interface KbMock {
  captures: KbMockCaptures;
  state: { documents: KbDocumentFixture[] };
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
  const captures: KbMockCaptures = {
    uploads: [],
    listUrls: [],
    detailIds: [],
    versionUrls: [],
  };
  const replayByKey = new Map<string, { batch_id: string; documents: KbDocumentFixture[] }>();

  let listRequests = 0;
  let uploadSeq = 0;
  let batchSeq = 0;
  let uploadErrorConsumed = false;

  function versionsFor(document: KbDocumentFixture): KbVersionFixture[] {
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
    return json(route, 200, cloneDocument(document));
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

  return { captures, state: { documents } };
}
