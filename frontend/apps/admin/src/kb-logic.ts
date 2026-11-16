/** 知识库页的纯逻辑（M3 KB-01B）：与后端同口径的本地预检、幂等键决策、状态文案与轮询判据。 */

import { ApiError, toUserMessage } from "@campus-elf/api-client";
import type {
  KbChunkSectionStat,
  KbChunkStatsView,
  KbDocumentView,
  KbFileMetadataInput,
  KbRetrievalTestRouteHit,
  KbRerankStatus,
  KbScanStateView,
} from "@campus-elf/api-client";

import { codePointLength } from "./logic";

/** 与后端 `app/kb/validation.py:17-19` 同界：15 个/批、50MB/文件、300MB/批。 */
export const KB_MAX_FILES = 15;
export const KB_MAX_FILE_BYTES = 50 * 1024 * 1024;
export const KB_MAX_BATCH_BYTES = 300 * 1024 * 1024;

/** 与后端 `CANONICAL_MIME` 的键同集（`validation.py:26-37`）；浏览器 `File.type` 只展示、不作为拒绝依据。 */
export const KB_ALLOWED_EXTENSIONS: readonly string[] = [
  ".pdf",
  ".md",
  ".txt",
  ".docx",
  ".pptx",
  ".ppt",
];

/** 列表默认每页条数（后端 `school_kb.py` 的 `DEFAULT_PAGE_SIZE`；上限 100 由后端校验，页面不暴露 limit 输入）。 */
export const KB_PAGE_SIZE = 20;

/** 上传结果页对 `scan.state=pending` 的轮询节奏与截止时间（简报 D4）。 */
export const SCAN_POLL_INTERVAL_MS = 5_000;
export const SCAN_POLL_WINDOW_MS = 5 * 60 * 1000;

/** 单文件预检只需要这三个文件元信息（便于单测构造，不依赖 DOM File）。 */
export interface KbFileLike {
  name: string;
  size: number;
  lastModified: number;
}

export interface KbFilePreflightInput {
  name: string;
  size: number;
}

/** 页面上的一行：文件 + 可编辑元数据（未编辑的行整项发 `{}`）。 */
export interface KbMetadataRow {
  title: string;
  tagsInput: string;
  description: string;
}

/**
 * 默认标题 = 文件名去扩展名，截到 200 字（镜像后端 `_default_title`，`service.py:634-636`）。
 * 清空标题表示让后端用它自己的默认值，所以这里只负责给输入框一个初始值。
 */
export function defaultTitle(filename: string): string {
  const base = filename.split(/[\\/]/).pop() ?? filename;
  const dot = base.lastIndexOf(".");
  const hasExtension = dot > 0 && dot < base.length - 1;
  const stem = (hasExtension ? base.slice(0, dot) : base).trim();
  return Array.from(stem || base)
    .slice(0, 200)
    .join("");
}

/** 标签输入：英文/中文逗号分隔，去空白、去空项、去重（保序）。 */
export function parseTags(input: string): string[] {
  const seen = new Set<string>();
  const tags: string[] = [];
  for (const part of input.split(/[,，]/)) {
    const tag = part.trim();
    if (!tag || seen.has(tag)) continue;
    seen.add(tag);
    tags.push(tag);
  }
  return tags;
}

function extensionOf(filename: string): string {
  const dot = filename.lastIndexOf(".");
  return dot < 0 ? "" : filename.slice(dot).toLowerCase();
}

/**
 * 单文件预检（镜像后端 `validate_file` 的检查顺序，`validation.py:218-232`）：
 * 扩展名 → 空文件 → 单文件大小。内容级校验（魔数/ZIP 炸弹/编码）由后端权威判定。
 */
export function filePreflightError(file: KbFilePreflightInput): string | null {
  const extension = extensionOf(file.name);
  if (!KB_ALLOWED_EXTENSIONS.includes(extension)) {
    return `「${file.name}」类型不支持，仅支持 ${KB_ALLOWED_EXTENSIONS.join("、")}`;
  }
  if (file.size === 0) return `「${file.name}」是空文件（0 字节），请重新选择`;
  if (file.size > KB_MAX_FILE_BYTES) return `「${file.name}」超过 50MB（单文件上限）`;
  return null;
}

/**
 * 整批预检：数量 → 逐文件 → 合计。返回第一条错误（带「第 N 个文件」定位），null 表示通过。
 * 后端仍会逐文件复核并整批 422，这里只是把必然失败的请求拦在发出去之前。
 */
export function batchPreflightError(files: readonly KbFilePreflightInput[]): string | null {
  if (files.length === 0) return "请至少上传一个文件";
  if (files.length > KB_MAX_FILES) {
    return `一次最多上传 ${KB_MAX_FILES} 个文件（当前 ${files.length} 个）`;
  }
  for (const [index, file] of files.entries()) {
    const error = filePreflightError(file);
    if (error) return `第 ${index + 1} 个文件：${error}`;
  }
  const total = files.reduce((sum, file) => sum + file.size, 0);
  if (total > KB_MAX_BATCH_BYTES) {
    return `整批文件合计 ${formatBytes(total)}，超过 300MB 上限`;
  }
  return null;
}

export interface KbMetadataError {
  /** 0 基行下标（页面换算成「第 N 行」）。 */
  index: number;
  message: string;
}

/**
 * 元数据本地预检（镜像后端 `FileMetadata`，`service.py:75-82` 与 `_Tag` 约束）：
 * 标题 ≤200、标签 ≤10 个且每个 ≤32 字、描述 ≤1000 字；空标题/空标签/空描述表示省略。
 */
export function metadataError(rows: readonly KbMetadataRow[]): KbMetadataError | null {
  for (const [index, row] of rows.entries()) {
    const title = row.title.trim();
    if (title && codePointLength(title) > 200) {
      return { index, message: "标题不能超过 200 字" };
    }
    const tags = parseTags(row.tagsInput);
    if (tags.length > 10) {
      return { index, message: `标签最多 10 个（当前 ${tags.length} 个）` };
    }
    const longTag = tags.find((tag) => codePointLength(tag) > 32);
    if (longTag !== undefined) {
      return { index, message: `标签「${longTag}」超过 32 字` };
    }
    if (codePointLength(row.description.trim()) > 1000) {
      return { index, message: "描述不能超过 1000 字" };
    }
  }
  return null;
}

/**
 * 把表格行序列化成后端 `metadata` 表单字段（简报 §3.1）：
 * 所有行都为空时返回 `undefined`（整段省略，走后端默认标题）；
 * 只要有一行非空，就发与文件等长的数组，未编辑的行发 `{}`。
 */
export function buildMetadata(rows: readonly KbMetadataRow[]): KbFileMetadataInput[] | undefined {
  const entries = rows.map((row): KbFileMetadataInput => {
    const entry: KbFileMetadataInput = {};
    const title = row.title.trim();
    if (title) entry.title = title;
    const tags = parseTags(row.tagsInput);
    if (tags.length > 0) entry.tags = tags;
    const description = row.description.trim();
    if (description) entry.description = description;
    return entry;
  });
  const edited = entries.some((entry) => Object.keys(entry).length > 0);
  return edited ? entries : undefined;
}

/**
 * 本次提交的签名 = 有序文件的 `name|size|lastModified` + 元数据 JSON（简报 D3）。
 * 刻意不用内容哈希：300MB 全量哈希会卡住 UI；签名只决定「能否复用幂等键」。
 */
export function uploadSignature(
  files: readonly KbFileLike[],
  metadata?: readonly KbFileMetadataInput[],
): string {
  const filePart = files.map((file) => `${file.name}|${file.size}|${file.lastModified}`).join("\n");
  return metadata === undefined ? filePart : `${filePart}\n--\n${JSON.stringify(metadata)}`;
}

export interface UploadAttempt {
  key: string;
  signature: string;
}

/**
 * 幂等键决策：签名不变的重试复用同一个键，文件/元数据一改就换新键。
 * `forceNew` 用于 409 `IDEMPOTENCY_KEY_REUSED` 之后——该键已属于另一批文件，必须换掉。
 */
export function uploadAttemptFor(
  previous: UploadAttempt | null,
  signature: string,
  generate: () => string,
  forceNew = false,
): UploadAttempt {
  if (!forceNew && previous !== null && previous.signature === signature) return previous;
  return { key: generate(), signature };
}

export type KbTone = "success" | "warning" | "danger" | "info";

export interface KbStatusMeta {
  label: string;
  tone: KbTone;
}

/**
 * 文档/版本状态文案（权威全集 14 档，与后端 `app/kb/status.py:16-29` 同集；KB-05B 补齐
 * 构建生命周期四态、测试结论两态与发布三态）。未知档位显示原码，不默认成安全态。
 */
const DOCUMENT_STATUS_META: Record<string, KbStatusMeta> = {
  UPLOADED: { label: "待扫描/扫描中", tone: "info" },
  PENDING_APPROVAL: { label: "待审批", tone: "success" },
  SCAN_FAILED: { label: "扫描未通过", tone: "danger" },
  // KB-02：审批结论两态。APPROVED 用 info（可以建构建了但还没建），成功绿留给发布后的终态。
  APPROVED: { label: "已审批·待构建", tone: "info" },
  REJECTED: { label: "已驳回", tone: "danger" },
  // KB-03：构建生命周期。FAILED 是版本态（可重试重建），BUILDING 进行中。
  BUILDING: { label: "构建中", tone: "info" },
  READY: { label: "已构建·待测试", tone: "info" },
  FAILED: { label: "构建失败", tone: "danger" },
  // KB-04：测试沙箱（版本生命周期档位；测试运行的独立状态见 test-logic 的运行态 map）。
  TESTING: { label: "测试进行中", tone: "warning" },
  TEST_PASSED: { label: "测试通过·可发布", tone: "success" },
  TEST_FAILED: { label: "测试未通过", tone: "danger" },
  // KB-05：发布三态。PUBLISHED 拿成功绿；OFFLINE 是主动下线的终态用中性色；DELETED 危险。
  PUBLISHED: { label: "已发布", tone: "success" },
  OFFLINE: { label: "已下线", tone: "info" },
  DELETED: { label: "已删除", tone: "danger" },
};

export function documentStatusMeta(status: string): KbStatusMeta {
  return DOCUMENT_STATUS_META[status] ?? { label: `未知状态（${status}）`, tone: "warning" };
}

// ---- KB-05B：发布/回滚/下线/删除的前端门禁与文案 ----

/** 权限码（与后端 `school_kb.py:64-68` 的依赖声明同名；发布/回滚/下线同码，删除独立码）。 */
export const KB_PUBLISH_CODE = "school.kb.publish";
export const KB_DELETE_CODE = "school.kb.delete";

/** 版本状态常量（发布门禁与目标过滤用；全集见 `DOCUMENT_STATUS_META` 注释）。 */
export const KB_STATUS_TEST_PASSED = "TEST_PASSED";
export const KB_STATUS_PUBLISHED = "PUBLISHED";

/**
 * 发布按钮门禁（后端 `assert_publishable` 的 UX 镜像，`status.py:92-96`：只有 TEST_PASSED 可发布）。
 * 服务端仍是唯一门禁：这里为 false 就不给按钮，为 true 点了仍可能吃后端 409。
 */
export function publishGate(
  status: string,
  versionId: string | null | undefined,
  permissions: readonly string[],
): boolean {
  return (
    status === KB_STATUS_TEST_PASSED &&
    versionId !== null &&
    versionId !== undefined &&
    permissions.includes(KB_PUBLISH_CODE)
  );
}

/** 下线按钮门禁（后端只有 PUBLISHED 可下线，`status.py:101-105`）。 */
export function offlineGate(
  status: string,
  versionId: string | null | undefined,
  permissions: readonly string[],
): boolean {
  return (
    status === KB_STATUS_PUBLISHED &&
    versionId !== null &&
    versionId !== undefined &&
    permissions.includes(KB_PUBLISH_CODE)
  );
}

/** 删除按钮门禁（PUBLISHED + 独立码 `school.kb.delete`，与发布分授）。 */
export function deleteGate(
  status: string,
  versionId: string | null | undefined,
  permissions: readonly string[],
): boolean {
  return (
    status === KB_STATUS_PUBLISHED &&
    versionId !== null &&
    versionId !== undefined &&
    permissions.includes(KB_DELETE_CODE)
  );
}

export interface KbRollbackTarget {
  id: string;
  version_no: number;
  filename: string;
}

/**
 * 回滚目标候选：已加载版本里的 PUBLISHED 行（蓝绿共存，D2-A）。
 * `activeVersionId` 服真化（KB-08B/TD-305 前端半）：详情回包 `active_version_id` 初始化 +
 * 会话内 publish/rollback 成功响应就地更新；null = 无活跃发布（此时全列 PUBLISHED）。
 * 竞态窗口（详情加载后另一端切换过活跃）选到活跃版本仍吃后端 409
 * `KB_ROLLBACK_TARGET_IS_ACTIVE`（服务端是唯一门禁）。
 */
export function rollbackTargets(
  versions: readonly (KbRollbackTarget & { status: string })[],
  activeVersionId: string | null,
): KbRollbackTarget[] {
  return versions
    .filter((version) => version.status === KB_STATUS_PUBLISHED && version.id !== activeVersionId)
    .map(({ id, version_no, filename }) => ({ id, version_no, filename }));
}

/**
 * 版本行是否为当前活跃发布（版本表「当前活跃」tag 的判据，KB-08B）。
 * null = 无活跃发布/真值未加载——任何行都不打标（不误标）。
 */
export function isActiveVersion(versionId: string, activeVersionId: string | null): boolean {
  return activeVersionId !== null && versionId === activeVersionId;
}

/** 从 ApiError detail 里取机器可读码（`approval-logic.approvalErrorCode` 同型）。 */
function kbPublishErrorCode(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  const detail = error.detail;
  if (!detail || typeof detail !== "object") return null;
  const { code } = detail as { code?: unknown };
  return typeof code === "string" ? code : null;
}

/** 发布/回滚/下线/删除 409 码 → 可执行指引（每条都给下一步；KB-05 后端审查 m-4 先例）。 */
const PUBLISH_ACTION_HINTS: Record<string, string> = {
  KB_NOT_TEST_PASSED: "该版本未通过测试沙箱验证，请先发起测试并通过后再发布",
  KB_PUBLISHED_NOT_ACTIVE: "该版本已发布但不是当前活跃版本，请使用「回滚」切换到它",
  KB_NOT_PUBLISHED: "该版本已不是已发布状态（可能已被下线或删除），请刷新后重试",
  KB_NO_ACTIVE_PUBLISH: "该文档当前没有活跃发布，无法回滚",
  KB_ROLLBACK_TARGET_IS_ACTIVE: "目标版本已是当前活跃发布，无需回滚",
  KB_INDEX_NOT_READY: "该版本的向量索引未就绪，请先创建构建并等构建完成",
  KB_INDEX_INCOMPLETE: "该版本的向量索引不完整，请重新创建构建",
};

/**
 * 发布/回滚/下线/删除的 409 人话文案：后端 `detail.message` 是主文案（已是可执行中文），
 * 缺失/泛化时按码给指引，再退调用方 fallback（`kbEntryConflictMessage` 同型，服务端优先）。
 */
export function kbPublishActionMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    const detail = error.detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      const serverMessage = (detail as Record<string, unknown>).message;
      if (typeof serverMessage === "string" && serverMessage) return serverMessage;
      // detail 是结构化对象但没有 message：error.message 是客户端哨兵（「请求失败（N）」），
      // 展示给用户没有信息量——直接按码给指引或退调用方 fallback。
      return PUBLISH_ACTION_HINTS[kbPublishErrorCode(error) ?? ""] ?? fallback;
    }
  }
  return toUserMessage(error, fallback);
}

/**
 * 派生扫描状态五档（后端 `derive_scan_state`，`service.py:181-193`）。
 * `skipped`（开发旁路）必须是警告色：它代表「没真扫」，不能显示成绿色通过。
 */
const SCAN_STATE_META: Record<string, KbStatusMeta> = {
  pending: { label: "扫描中/待扫描", tone: "info" },
  passed: { label: "已通过", tone: "success" },
  infected: { label: "发现威胁", tone: "danger" },
  error: { label: "扫描故障（后台将重试）", tone: "warning" },
  skipped: { label: "已跳过扫描", tone: "warning" },
};

export function scanStateMeta(state: string): KbStatusMeta {
  return SCAN_STATE_META[state] ?? { label: `未知（${state}）`, tone: "warning" };
}

/** 扫描引擎文案；开发旁路必须让人一眼看出「未真实扫描」。 */
export function scanEngineLabel(engine: string | null): string {
  if (!engine) return "—";
  if (engine === "clamav") return "ClamAV";
  if (engine === "dev-skip") return "开发环境跳过扫描";
  if (engine === "dev-degraded") return "扫描不可用，开发环境降级";
  return engine;
}

/** 开发旁路引擎一律警告色，即使 `state` 看起来是 passed（防御未来引擎与状态的组合漂移）。 */
export function scanTone(scan: Pick<KbScanStateView, "state" | "engine">): KbTone {
  if (scan.engine === "dev-skip" || scan.engine === "dev-degraded") return "warning";
  return scanStateMeta(scan.state).tone;
}

/** 上传结果页的轮询判据：有查看权 + 页面可见 + 本批仍有待扫描文档 + 未超过 5 分钟。 */
export function shouldPollScan(input: {
  hasPending: boolean;
  hasView: boolean;
  visible: boolean;
  elapsedMs: number;
}): boolean {
  return (
    input.hasView &&
    input.visible &&
    input.hasPending &&
    input.elapsedMs >= 0 &&
    input.elapsedMs < SCAN_POLL_WINDOW_MS
  );
}

/** 本批次里当前版本仍在扫描的文档 id（结果页只对它们发详情请求）。 */
export function pendingScanDocumentIds(documents: readonly KbDocumentView[]): string[] {
  return documents
    .filter((document) => document.current_version?.scan.state === "pending")
    .map((document) => document.id);
}

export interface KbFileErrorRow {
  index: number;
  filename: string;
  code: string;
  message: string;
}

export interface KbErrorInfo {
  code: string | null;
  message: string;
  files: KbFileErrorRow[];
}

function isKbFileErrorRow(value: unknown): value is KbFileErrorRow {
  if (!value || typeof value !== "object") return false;
  const row = value as Record<string, unknown>;
  return (
    typeof row.index === "number" &&
    typeof row.filename === "string" &&
    typeof row.code === "string" &&
    typeof row.message === "string"
  );
}

/**
 * 从错误里取「判据码 + 人话 + 逐文件明细」。
 * 后端错误体是 `{detail: {code, message, files?}}`（`service.py:316-319`）；
 * 413 由中间件返回 `{detail: "..."}`（字符串），形状不同要分开处理。
 */
export function kbErrorInfo(error: unknown, fallback = "操作失败，请稍后重试"): KbErrorInfo {
  if (!(error instanceof ApiError)) {
    return { code: null, message: toUserMessage(error, fallback), files: [] };
  }
  const detail = error.detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const payload = detail as Record<string, unknown>;
    const files = Array.isArray(payload.files) ? payload.files.filter(isKbFileErrorRow) : [];
    return {
      code: typeof payload.code === "string" ? payload.code : null,
      message:
        typeof payload.message === "string" && payload.message
          ? payload.message
          : toUserMessage(error, fallback),
      files,
    };
  }
  return { code: null, message: toUserMessage(error, fallback), files: [] };
}

/** 文件大小展示：1024 进制、最多一位小数、去掉多余的 `.0`。 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const rounded = Math.round(value * 10) / 10;
  return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)} ${units[unit]}`;
}

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

/** 列表加载失败但保留旧行时的横幅（TD-260 的教训：不做静默保留）。 */
export function staleBannerText(lastLoadedAt: Date | null): string {
  if (lastLoadedAt === null) return "数据可能不是最新（尚未成功加载过）";
  const clock = `${pad2(lastLoadedAt.getHours())}:${pad2(lastLoadedAt.getMinutes())}:${pad2(
    lastLoadedAt.getSeconds(),
  )}`;
  return `数据可能不是最新（上次成功 ${clock}）`;
}

// ---- KB-06B：分块预览的纯函数层（后端契约 app/kb/chunk_read.py + school_kb.py:1064-1119）----

/** 分块预览权限码（与后端 `ChunkViewerDep`（school_kb.py:81）同名；单码不拆分，简报卡 4A）。 */
export const KB_CHUNKS_CODE = "school.kb.view_chunks";

/** 分块入口门禁：持有 view_chunks 即给入口（服务端仍是唯一门禁；404/400 由后端裁决）。 */
export function chunksGate(permissions: readonly string[]): boolean {
  return permissions.includes(KB_CHUNKS_CODE);
}

/** q 的传输口径：strip 后空 = 未提供（与后端 `normalize_q`（chunk_read.py:41-45）同口径）。 */
export function normalizeChunkQ(q: string): string | undefined {
  const stripped = q.trim();
  return stripped === "" ? undefined : stripped;
}

/**
 * 搜索词前端先拦：后端 `Query(max_length=200)`（school_kb.py:1071）按 Unicode **码点**度量 422
 * （chunk_read.py 审查 Minor 2 口径）；JS `.length` 是 UTF-16 码元数，非 BMP 字符 1 码点 = 2 码元，
 * 必须用 `codePointLength` 对齐（审查 m-1；否则 emoji 等输入会「前端比后端严」造假门禁边界）。
 */
export function chunkFilterError(rawQ: string | undefined): string | null {
  if (rawQ !== undefined && codePointLength(rawQ) > 200) {
    return "搜索词最长 200 字符";
  }
  return null;
}

export interface KbChunkFilters {
  q: string;
  sectionPath: string;
  chunkType: "" | "parent" | "child";
}

/** 块类型文案（未知档位显示原码，不默认成安全态）。 */
export function chunkTypeLabel(chunkType: string): string {
  if (chunkType === "parent") return "父块";
  if (chunkType === "child") return "子块";
  return `未知类型（${chunkType}）`;
}

const EMBEDDING_STATUS_META: Record<string, KbStatusMeta> = {
  pending: { label: "待嵌入", tone: "info" },
  ready: { label: "已嵌入", tone: "success" },
  failed: { label: "嵌入失败", tone: "danger" },
};

/** 嵌入状态文案（后端 embedding_status 三态 `chunk_read.py` stats；未知档位显示原码）。 */
export function embeddingStatusMeta(status: string): KbStatusMeta {
  return EMBEDDING_STATUS_META[status] ?? { label: `未知状态（${status}）`, tone: "warning" };
}

/** 统计摘要行文案（per-version 固定口径；sections 只作章节下拉数据源，不进摘要）。 */
export function formatChunkStats(stats: KbChunkStatsView): string {
  const embedding = stats.embedding_status;
  return (
    `共 ${stats.total_chunks} 块（父 ${stats.parent_count} / 子 ${stats.child_count}）· ` +
    `嵌入 待${embedding.pending ?? 0} 成${embedding.ready ?? 0} 败${embedding.failed ?? 0} · ` +
    `${stats.total_chars} 字符`
  );
}

/** 章节下拉选项（stats.sections → 值/标签；标签带块数，精确匹配语义与后端一致）。 */
export function sectionOptions(
  sections: readonly KbChunkSectionStat[],
): Array<{ value: string; label: string }> {
  return sections.map((section) => ({
    value: section.section_path,
    label: `${section.section_path}（${section.chunk_count}）`,
  }));
}

// ---- KB-07B：检索测试（后端契约 retrieval_test.py / school_kb.py KbRetrievalTestResponse）----

/** rerank 状态文案（KbRerankStatus 闭合 Literal，switch 穷尽；rerank_status 语义见简报 M3-KB-07 §3）。 */
export function rerankStatusLabel(status: KbRerankStatus): string {
  switch (status) {
    case "applied":
      return "已重排";
    case "skipped":
      return "重排超时跳过";
    case "failed":
      return "重排失败降级";
    case "disabled":
      return "未启用重排";
  }
}

/** 分数格式化：4 位小数；null（未启用/未召回）显示「—」。 */
export function formatScore(score: number | null): string {
  return score === null ? "—" : score.toFixed(4);
}

/** 单路分数 + 该路 1-based 名次（「为什么召回」逐路可解释；null = 该路 pool×2 内未召回）。 */
export function routeScoreText(hit: KbRetrievalTestRouteHit | null): string {
  return hit === null ? "—" : `${hit.score.toFixed(4)} · #${hit.rank}`;
}

/** 检索词前端预检（后端：raw 码点 ≤300 + strip 后非空，school_kb.py KbRetrievalTestRequest）。
 * m-1 教训：非 BMP 字符按码点度量（codePointLength），不能用 UTF-16 .length。 */
export function retrievalQueryError(rawQuery: string): string | null {
  const stripped = rawQuery.trim();
  if (stripped === "") return "请输入检索词";
  if (codePointLength(rawQuery) > 300) return "检索词不能超过 300 个字符";
  return null;
}

/** top_k 前端预检（后端 Field ge=1 le=20）。 */
export function retrievalTopKError(topK: number): string | null {
  if (!Number.isInteger(topK) || topK < 1 || topK > 20) return "top-k 需为 1~20 的整数";
  return null;
}

// ---------------------------------------------------------------------------
// KB-09C：文档查阅人群 ACL（简报 M3-KB-09 §3.3 决策卡 C1-A/C2-A/C3-A）
// ---------------------------------------------------------------------------

/** 权限码：配置读侧入口（GET audiences 挂 view）；编辑/确认按钮分授（PUT/confirm 挂 manage）。 */
export const KB_ACL_MANAGE_CODE = "school.kb.acl.manage";
/** 权限码：ignore_acl 控件渲染判据（后端 403 `RETRIEVAL_TEST_UNFILTERED_FORBIDDEN` 的 UX 镜像）。 */
export const KB_RETRIEVAL_UNFILTERED_CODE = "school.kb.retrieval_test.unfiltered";

export const KB_VIEW_CODE = "school.kb.view";

/** 人群配置入口可见性（`school.kb.view` 持有人可读；后端 GET audiences 同码）。 */
export function aclVisible(permissions: readonly string[]): boolean {
  return permissions.includes(KB_VIEW_CODE);
}

/** 人群编辑/确认按钮门禁（`school.kb.acl.manage`；服务端仍是唯一门禁）。 */
export function aclManageGate(permissions: readonly string[]): boolean {
  return permissions.includes(KB_ACL_MANAGE_CODE);
}

/** ignore_acl 控件渲染门禁（`school.kb.retrieval_test.unfiltered`；无码 403 的 UX 镜像）。 */
export function unfilteredGate(permissions: readonly string[]): boolean {
  return permissions.includes(KB_RETRIEVAL_UNFILTERED_CODE);
}

/** 人群类型白名单（后端 `KB_AUDIENCE_TYPES`，models/kb.py；顺序即下拉框序）。 */
export const KB_AUDIENCE_TYPES: readonly string[] = [
  "department",
  "role",
  "class",
  "grade",
  "course",
  "user",
  "all",
];

export interface KbAudienceEditRow {
  audience_type: string;
  audience_id: string | null;
}

/**
 * 人群条目编辑预检（镜像后端 `parse_audience_items` 422 族，`audience.py`；
 * 服务端仍是唯一门禁——前端拦的是明显错误，crafty 输入吃后端 422）。
 * 返回 null = 合法；否则为人读错误文案（与后端 message 对齐）。
 */
export function audienceEditError(items: readonly KbAudienceEditRow[]): string | null {
  const seen = new Set<string>();
  for (const item of items) {
    const audienceType = item.audience_type;
    if (!KB_AUDIENCE_TYPES.includes(audienceType)) {
      return `未知人群类型：${audienceType}（AUDIENCE_TYPE_INVALID）`;
    }
    if (audienceType === "all") {
      if (item.audience_id !== null && item.audience_id !== undefined) {
        return "all（显式全体）不接受对象 id（AUDIENCE_ID_FORBIDDEN）";
      }
    } else if (item.audience_id === null || item.audience_id.trim() === "") {
      return `${audienceType} 人群必须提供对象 id（AUDIENCE_ID_REQUIRED）`;
    }
    // 判重键先 trim（与后端 parse_audience_items「strip 后判重」同构，审查 m4）
    const key = `${audienceType}:${(item.audience_id ?? "").trim()}`;
    if (seen.has(key)) {
      return "人群条目重复（AUDIENCE_DUPLICATE）";
    }
    seen.add(key);
  }
  const hasAll = items.some((item) => item.audience_type === "all");
  if (hasAll && items.length > 1) {
    return "all（显式全体）不得与其他人群类型混配（AUDIENCE_ALL_MIXED）";
  }
  return null;
}

/** 确认态 tag 文案。 */
export function aclConfirmedLabel(confirmed: boolean): string {
  return confirmed ? "已确认" : "未确认";
}

/**
 * 人群子视图顶部横幅文案（确认态=可构建/发布不变量的 UX 提示，state-machines :86/:98）。
 * 已确认返回 null（不渲染横幅）。
 */
export function aclBannerText(confirmed: boolean): string | null {
  if (confirmed) return null;
  return "人群 ACL 未确认：确认后才能构建/发布；保存（全量替换）会把确认态重置为未确认。";
}

/** 人群类型中文标签（未知类型显示原码，不猜）。 */
export function audienceTypeLabel(audienceType: string): string {
  const labels: Record<string, string> = {
    department: "部门",
    role: "角色",
    class: "班级",
    grade: "年级",
    course: "课程",
    user: "指定人",
    all: "全体",
  };
  return labels[audienceType] ?? audienceType;
}

/**
 * 检索测试命中 token 展示（诊断面不美化 id——原 token 即事实源；
 * 仅 `all` 翻译为「全体」，与人群类型标签对齐）。
 */
export function tokenLabel(token: string): string {
  return token === "all" ? "全体" : token;
}

// ---------------------------------------------------------------------------
// KB-10B：切块编辑与派生重建（K5 前端半；后端契约 school_kb.py:1458-1597、
// chunk_edit.py 纯函数镜像；简报 docs/design-briefs/M3-KB-10B.md 卡 E1-E6）
// ---------------------------------------------------------------------------

/** 权限码：子块编辑提交（POST /chunks/{id}/edits 单码）。 */
export const KB_CHUNK_EDIT_CODE = "school.kb.chunk.edit";
/** 权限码：派生确认（POST derive 码半；叠加校方超管 role 双重门禁）。 */
export const KB_CHUNK_CONFIRM_CODE = "school.kb.chunk.confirm";
/** 校方超管 role（后端 derive 二重门禁 403 `CHUNK_CONFIRM_FORBIDDEN`；AuthUser.roles 消费起点）。 */
export const SCHOOL_SUPER_ADMIN_ROLE = "school_super_admin";
/** 编辑长度下限（TD-320 卡 E5-A：上限 = max(500, 原块码点)；与后端 CHUNK_CONTENT_MAX_CHARS 同源）。 */
export const CHUNK_EDIT_MIN_CAP = 500;

/** 编辑理由长度上限（镜像后端 KbChunkEditRequest.reason max_length=500，school_kb.py:1462）。 */
export const CHUNK_EDIT_REASON_MAX_CHARS = 500;

/** 编辑按钮门禁（`school.kb.chunk.edit`；服务端仍是唯一门禁）。 */
export function chunkEditGate(permissions: readonly string[]): boolean {
  return permissions.includes(KB_CHUNK_EDIT_CODE);
}

/**
 * 派生按钮门禁（卡 E3）：`chunk.confirm` 码 + `school_super_admin` role 双判——
 * 后端 `ChunkConfirmerDep` + role 校验的双重门禁镜像（前端首个 role 判，
 * AuthUser.roles 自 api-client 已存在、此前无人消费）。
 */
export function chunkConfirmGate(
  permissions: readonly string[],
  roles: readonly string[],
): boolean {
  return permissions.includes(KB_CHUNK_CONFIRM_CODE) && roles.includes(SCHOOL_SUPER_ADMIN_ROLE);
}

/** 编辑内容长度上限（卡 E5-A）：`max(500, 原块码点)`——与后端 `edit_max_chars` 同构。 */
export function chunkEditMaxChars(originalCharCount: number): number {
  return Math.max(CHUNK_EDIT_MIN_CAP, originalCharCount);
}

/**
 * 编辑内容预检（镜像后端 `validate_edit_content` 422 族，`chunk_edit.py:52-96`；
 * 服务端仍是唯一门禁——前端拦明显错误，crafty 输入吃后端 422）。
 * 返回 null = 合法；否则人读文案（内嵌错误码，`audienceEditError` 同款）。
 */
export function chunkEditError(newContent: string, maxChars: number): string | null {
  const trimmed = newContent.trim();
  if (!trimmed) return "编辑内容不能为空（CHUNK_CONTENT_INVALID）";
  if (codePointLength(trimmed) > maxChars) {
    return `编辑内容超出长度上限（${codePointLength(trimmed)} > ${maxChars} 码点）（CHUNK_CONTENT_INVALID）`;
  }
  if ((trimmed.match(/```/g) ?? []).length % 2 !== 0) {
    return "代码围栏未闭合（``` 开合不配对）（CHUNK_CONTENT_INVALID）";
  }
  // 表格组 = 以 `|` 开头的连续行块（组内每行首尾管道齐备；单行组亦校验——审查 N7 口径；
  // 围栏内管道行不排除——轻量校验边界 TD-322）
  const runs: string[][] = [];
  let current: string[] = [];
  for (const line of trimmed.split("\n")) {
    if (line.trimStart().startsWith("|")) {
      current.push(line);
    } else if (current.length > 0) {
      runs.push(current);
      current = [];
    }
  }
  if (current.length > 0) runs.push(current);
  for (const run of runs) {
    if (!run.every((row) => row.trimStart().startsWith("|") && row.trimEnd().endsWith("|"))) {
      return "表格结构不完整（管道行未以 | 起止）（CHUNK_CONTENT_INVALID）";
    }
  }
  return null;
}

/** 编辑/派生 409/422/403 族码 → 可执行指引（每条都给下一步）。 */
const CHUNK_EDIT_ACTION_HINTS: Record<string, string> = {
  CHUNK_CONFLICT: "分块内容已被他人编辑（base_hash 过期），请关闭对话框并刷新分块后重试",
  CHUNK_CONTENT_INVALID: "编辑内容未通过结构校验（非空/长度上限/表格与代码闭合）",
  CHUNK_EDIT_TARGET_INVALID: "仅子块文本可编辑（父块/结构性改动走重传+重审）",
  CHUNK_EDIT_STATE: "该版本已有待派生的编辑或已派生——请到派生版本继续编辑",
  DERIVE_NO_EDITS: "该版本还没有待派生的编辑提交，请先编辑子块",
  CHUNK_CONFIRM_FORBIDDEN: "派生确认仅限校方超管",
};

/**
 * 编辑/派生动作错误的人话文案：后端 `detail.message` 是主文案（已是可执行中文），
 * 缺失/泛化时按码给指引，再退调用方 fallback（`kbPublishActionMessage` 同型）。
 */
export function chunkEditActionMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    const detail = error.detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      const serverMessage = (detail as Record<string, unknown>).message;
      if (typeof serverMessage === "string" && serverMessage) return serverMessage;
      return CHUNK_EDIT_ACTION_HINTS[kbPublishErrorCode(error) ?? ""] ?? fallback;
    }
  }
  return toUserMessage(error, fallback);
}

/** 编辑 diff 摘要（卡 E3）：逐行 multiset 对比——纯文本不引第三方 diff 库（TD-1）。 */
export interface ChunkEditDiffSummary {
  added: string[];
  removed: string[];
}

export function formatEditDiff(before: string, after: string): ChunkEditDiffSummary {
  const beforeLines = before.split("\n");
  const afterLines = after.split("\n");
  const remain = new Map<string, number>();
  for (const line of beforeLines) remain.set(line, (remain.get(line) ?? 0) + 1);
  const added: string[] = [];
  for (const line of afterLines) {
    const count = remain.get(line) ?? 0;
    if (count > 0) {
      remain.set(line, count - 1);
    } else {
      added.push(line);
    }
  }
  const removed: string[] = [];
  for (const [line, count] of remain) {
    for (let i = 0; i < count; i += 1) removed.push(line);
  }
  return { added, removed };
}
