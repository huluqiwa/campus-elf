/** 知识库页的纯逻辑（M3 KB-01B）：与后端同口径的本地预检、幂等键决策、状态文案与轮询判据。 */

import { ApiError, toUserMessage } from "@campus-elf/api-client";
import type { KbDocumentView, KbFileMetadataInput, KbScanStateView } from "@campus-elf/api-client";

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

/** 文档状态五档（`school_kb.py` 的 `status`；KB-02 起 +APPROVED/REJECTED）；未知档位显示原码，不默认成安全态。 */
const DOCUMENT_STATUS_META: Record<string, KbStatusMeta> = {
  UPLOADED: { label: "待扫描/扫描中", tone: "info" },
  PENDING_APPROVAL: { label: "待审批", tone: "success" },
  SCAN_FAILED: { label: "扫描未通过", tone: "danger" },
  // KB-02：审批结论两态。APPROVED 用 info（可以建构建了但还没建），成功绿留给发布后的终态。
  APPROVED: { label: "已审批·待构建", tone: "info" },
  REJECTED: { label: "已驳回", tone: "danger" },
};

export function documentStatusMeta(status: string): KbStatusMeta {
  return DOCUMENT_STATUS_META[status] ?? { label: `未知状态（${status}）`, tone: "warning" };
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
