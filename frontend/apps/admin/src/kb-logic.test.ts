import { describe, expect, it } from "vitest";

import { ApiError } from "@campus-elf/api-client";
import type { KbDocumentView, KbVersionView } from "@campus-elf/api-client";

import {
  KB_MAX_BATCH_BYTES,
  KB_MAX_FILE_BYTES,
  SCAN_POLL_WINDOW_MS,
  batchPreflightError,
  buildMetadata,
  defaultTitle,
  documentStatusMeta,
  filePreflightError,
  formatBytes,
  kbErrorInfo,
  metadataError,
  parseTags,
  pendingScanDocumentIds,
  scanEngineLabel,
  scanStateMeta,
  scanTone,
  shouldPollScan,
  staleBannerText,
  uploadAttemptFor,
  uploadSignature,
} from "./kb-logic";

function file(name: string, size = 1024, lastModified = 1_700_000_000_000) {
  return { name, size, lastModified };
}

function row(overrides: Partial<{ title: string; tagsInput: string; description: string }> = {}) {
  return { title: "", tagsInput: "", description: "", ...overrides };
}

function version(overrides: Partial<KbVersionView> = {}): KbVersionView {
  return {
    id: "v-1",
    version_no: 1,
    filename: "a.pdf",
    mime: "application/pdf",
    size_bytes: 10,
    status: "UPLOADED",
    scan: {
      state: "pending",
      engine: null,
      signature: null,
      error_code: null,
      attempts: 0,
      scanned_at: null,
    },
    created_at: "2026-10-31T00:00:00Z",
    ...overrides,
  };
}

function document(id: string, scanState: string | null): KbDocumentView {
  return {
    id,
    title: `文档 ${id}`,
    tags: [],
    description: null,
    status: "UPLOADED",
    current_version:
      scanState === null
        ? null
        : version({
            id: `${id}-v`,
            scan: {
              state: scanState,
              engine: null,
              signature: null,
              error_code: null,
              attempts: 0,
              scanned_at: null,
            },
          }),
    created_at: "2026-10-31T00:00:00Z",
    updated_at: "2026-10-31T00:00:00Z",
  };
}

describe("defaultTitle / parseTags", () => {
  it("标题默认取文件名去扩展名，保留中间的点和以点开头的隐藏文件名", () => {
    expect(defaultTitle("工作报告.pdf")).toBe("工作报告");
    expect(defaultTitle("三年规划.v2.docx")).toBe("三年规划.v2");
    expect(defaultTitle(".env")).toBe(".env");
    expect(defaultTitle("C:\\资料\\通知.md")).toBe("通知");
    expect(defaultTitle("无扩展名")).toBe("无扩展名");
  });

  it("默认标题截到 200 个码点（与后端 _default_title 同界，service.py:634）", () => {
    const title = defaultTitle(`${"字".repeat(250)}.pdf`);
    expect(Array.from(title)).toHaveLength(200);
  });

  it("标签按英文/中文逗号切分，去空白、去空项、去重且保序", () => {
    expect(parseTags(" 安全, 教学，安全 , ,应急 ")).toEqual(["安全", "教学", "应急"]);
    expect(parseTags("   ")).toEqual([]);
  });
});

describe("本地预检（与后端 validation.py:17-19 同口径）", () => {
  it("接受白名单扩展名（大小写不敏感），不看浏览器声明的 MIME", () => {
    for (const name of ["a.pdf", "b.MD", "c.Txt", "d.docx", "e.pptx", "f.ppt"]) {
      expect(filePreflightError(file(name))).toBeNull();
    }
  });

  it("拒绝非白名单扩展名、空文件与超过 50MB 的单文件", () => {
    expect(filePreflightError(file("virus.exe"))).toContain("类型不支持");
    expect(filePreflightError(file("empty.pdf", 0))).toContain("空文件");
    expect(filePreflightError(file("big.pdf", KB_MAX_FILE_BYTES + 1))).toContain("50MB");
    expect(filePreflightError(file("edge.pdf", KB_MAX_FILE_BYTES))).toBeNull();
  });

  it("整批预检：数量、逐文件错误、合计 300MB", () => {
    expect(batchPreflightError([])).toBe("请至少上传一个文件");
    const tooMany = Array.from({ length: 16 }, (_, index) => file(`${index}.pdf`));
    expect(batchPreflightError(tooMany)).toContain("最多上传 15 个");
    expect(batchPreflightError([file("ok.pdf"), file("bad.exe")])).toContain("第 2 个文件");

    const fiftyMb = KB_MAX_FILE_BYTES;
    const tooLarge = Array.from({ length: 7 }, (_, index) => file(`${index}.pdf`, fiftyMb));
    expect(tooLarge.reduce((sum, item) => sum + item.size, 0)).toBeGreaterThan(KB_MAX_BATCH_BYTES);
    expect(batchPreflightError(tooLarge)).toContain("300MB");

    const justUnderBatch = Array.from({ length: 6 }, (_, index) =>
      file(`${index}.pdf`, KB_MAX_FILE_BYTES),
    );
    expect(batchPreflightError(justUnderBatch)).toBeNull();
  });
});

describe("元数据校验与序列化", () => {
  it("标题 ≤200、标签 ≤10 个且每个 ≤32 字、描述 ≤1000 字", () => {
    expect(metadataError([row({ title: "字".repeat(200) })])).toBeNull();
    expect(metadataError([row({ title: "字".repeat(201) })])).toEqual({
      index: 0,
      message: "标题不能超过 200 字",
    });

    const tenTags = Array.from({ length: 10 }, (_, index) => `t${index}`).join(",");
    expect(metadataError([row({ tagsInput: tenTags })])).toBeNull();
    expect(metadataError([row({ tagsInput: `${tenTags},t10` })])).toEqual({
      index: 0,
      message: "标签最多 10 个（当前 11 个）",
    });
    expect(metadataError([row({ tagsInput: "字".repeat(33) })])).toEqual({
      index: 0,
      message: `标签「${"字".repeat(33)}」超过 32 字`,
    });

    expect(metadataError([row({ description: "字".repeat(1001) })])).toEqual({
      index: 0,
      message: "描述不能超过 1000 字",
    });
  });

  it("返回 0 基行下标（页面换算成「第 N 行」），便于表格定位", () => {
    expect(metadataError([row(), row({ title: "字".repeat(201) })])?.index).toBe(1);
  });

  it("全部行未编辑时不发送 metadata；有一行编辑就发与文件等长的数组，空行发 {}", () => {
    expect(buildMetadata([row(), row({ title: "  " })])).toBeUndefined();

    const built = buildMetadata([
      row({ title: " 学年报告 ", tagsInput: "安全, 安全，教学 ", description: " 说明 " }),
      row(),
    ]);
    expect(built).toEqual([{ title: "学年报告", tags: ["安全", "教学"], description: "说明" }, {}]);
    expect(built).toHaveLength(2);
  });

  it("清空标题表示用后端默认标题：该行只带其余字段", () => {
    expect(buildMetadata([row({ tagsInput: "标签" })])).toEqual([{ tags: ["标签"] }]);
  });
});

describe("上传签名与幂等键", () => {
  const files = [file("a.pdf", 10, 111), file("b.md", 20, 222)];
  const metadata = [{ title: "甲" }, {}];

  it("文件与元数据不变 → 签名不变；任一变化 → 签名变化", () => {
    const base = uploadSignature(files, metadata);
    expect(uploadSignature([file("a.pdf", 10, 111), file("b.md", 20, 222)], metadata)).toBe(base);
    expect(uploadSignature([...files].reverse(), metadata)).not.toBe(base);
    expect(uploadSignature([file("a.pdf", 11, 111), files[1]], metadata)).not.toBe(base);
    expect(uploadSignature([file("a.pdf", 10, 999), files[1]], metadata)).not.toBe(base);
    expect(uploadSignature(files, [{ title: "乙" }, {}])).not.toBe(base);
    expect(uploadSignature(files)).not.toBe(base);
  });

  it("签名不变时复用同一个键，签名变化时换新键", () => {
    let counter = 0;
    const generate = () => `key-${++counter}-abcdefgh`;
    const first = uploadAttemptFor(null, "sig-1", generate);
    expect(first).toEqual({ key: "key-1-abcdefgh", signature: "sig-1" });
    expect(uploadAttemptFor(first, "sig-1", generate)).toBe(first);
    const changed = uploadAttemptFor(first, "sig-2", generate);
    expect(changed.key).toBe("key-2-abcdefgh");
    expect(changed.signature).toBe("sig-2");
  });

  it("409 后强制换新键（同一签名也不再复用）", () => {
    let counter = 0;
    const generate = () => `key-${++counter}-abcdefgh`;
    const first = uploadAttemptFor(null, "sig-1", generate);
    const afterConflict = uploadAttemptFor(first, "sig-1", generate, true);
    expect(afterConflict.key).not.toBe(first.key);
    expect(afterConflict.signature).toBe("sig-1");
  });
});

describe("状态文案与色调", () => {
  it("五档文档状态与五种扫描状态有中文标签和颜色", () => {
    expect(documentStatusMeta("UPLOADED")).toEqual({ label: "待扫描/扫描中", tone: "info" });
    expect(documentStatusMeta("PENDING_APPROVAL")).toEqual({ label: "待审批", tone: "success" });
    expect(documentStatusMeta("SCAN_FAILED")).toEqual({ label: "扫描未通过", tone: "danger" });
    // KB-02 审批结论两态（审查 M1）：不再显示原码
    expect(documentStatusMeta("APPROVED")).toEqual({ label: "已审批·待构建", tone: "info" });
    expect(documentStatusMeta("REJECTED")).toEqual({ label: "已驳回", tone: "danger" });

    expect(scanStateMeta("pending")).toEqual({ label: "扫描中/待扫描", tone: "info" });
    expect(scanStateMeta("passed")).toEqual({ label: "已通过", tone: "success" });
    expect(scanStateMeta("infected")).toEqual({ label: "发现威胁", tone: "danger" });
    expect(scanStateMeta("error")).toEqual({ label: "扫描故障（后台将重试）", tone: "warning" });
    expect(scanStateMeta("skipped")).toEqual({ label: "已跳过扫描", tone: "warning" });
  });

  it("未知档位显示原码且是警告色，绝不默认成通过", () => {
    expect(documentStatusMeta("ARCHIVED")).toEqual({
      label: "未知状态（ARCHIVED）",
      tone: "warning",
    });
    expect(scanStateMeta("mystery")).toEqual({ label: "未知（mystery）", tone: "warning" });
    expect(scanStateMeta("mystery").tone).not.toBe("success");
  });

  it("引擎文案：开发旁路明确写「未真实扫描」，且强制警告色", () => {
    expect(scanEngineLabel("clamav")).toBe("ClamAV");
    expect(scanEngineLabel("dev-skip")).toBe("开发环境跳过扫描");
    expect(scanEngineLabel("dev-degraded")).toBe("扫描不可用，开发环境降级");
    expect(scanEngineLabel(null)).toBe("—");
    expect(scanEngineLabel("future-engine")).toBe("future-engine");

    expect(scanTone({ state: "passed", engine: "dev-skip" })).toBe("warning");
    expect(scanTone({ state: "passed", engine: "dev-degraded" })).toBe("warning");
    expect(scanTone({ state: "passed", engine: "clamav" })).toBe("success");
    expect(scanTone({ state: "infected", engine: "clamav" })).toBe("danger");
  });
});

describe("轮询判据", () => {
  const base = { hasPending: true, hasView: true, visible: true, elapsedMs: 0 };

  it("只有仍有待扫描 + 有查看权 + 页面可见 + 未超过 5 分钟才轮询", () => {
    expect(shouldPollScan(base)).toBe(true);
    expect(shouldPollScan({ ...base, elapsedMs: SCAN_POLL_WINDOW_MS - 1 })).toBe(true);
    expect(shouldPollScan({ ...base, elapsedMs: SCAN_POLL_WINDOW_MS })).toBe(false);
    expect(shouldPollScan({ ...base, elapsedMs: -1 })).toBe(false);
    expect(shouldPollScan({ ...base, hasPending: false })).toBe(false);
    expect(shouldPollScan({ ...base, hasView: false })).toBe(false);
    expect(shouldPollScan({ ...base, visible: false })).toBe(false);
  });

  it("只挑出当前版本仍在扫描的文档", () => {
    expect(
      pendingScanDocumentIds([
        document("a", "pending"),
        document("b", "passed"),
        document("c", null),
        document("d", "pending"),
      ]),
    ).toEqual(["a", "d"]);
  });
});

describe("错误 detail 提取", () => {
  it("422 整批失败：取 code/message 与逐文件错误", () => {
    const files = [
      { index: 0, filename: "a.pdf", code: "MAGIC_MISMATCH", message: "文件内容不是 PDF" },
      { index: 1, filename: "b.exe", code: "EXTENSION_NOT_ALLOWED", message: "不支持的类型" },
    ];
    const error = new ApiError("整批校验失败", 422, {
      code: "BATCH_VALIDATION_FAILED",
      message: "整批校验失败",
      files,
    });
    expect(kbErrorInfo(error)).toEqual({
      code: "BATCH_VALIDATION_FAILED",
      message: "整批校验失败",
      files,
    });
  });

  it("413 字符串 detail 原样展示；网络错误走兜底", () => {
    expect(kbErrorInfo(new ApiError("请求体过大", 413, "请求体过大"), "兜底")).toEqual({
      code: null,
      message: "请求体过大",
      files: [],
    });
    expect(kbErrorInfo(new Error("Failed to fetch"), "网络异常，可原样重试")).toEqual({
      code: null,
      message: "网络异常，可原样重试",
      files: [],
    });
  });

  it("detail 形状不对或 files 元素残缺时不吞掉主文案", () => {
    const info = kbErrorInfo(
      new ApiError("冲突", 409, {
        code: "IDEMPOTENCY_KEY_REUSED",
        message: "该提交编号已被使用",
        files: [{ index: 0 }, null, { index: 1, filename: "b", code: "X", message: "y" }],
      }),
    );
    expect(info.code).toBe("IDEMPOTENCY_KEY_REUSED");
    expect(info.message).toBe("该提交编号已被使用");
    expect(info.files).toEqual([{ index: 1, filename: "b", code: "X", message: "y" }]);
  });
});

describe("展示辅助", () => {
  it("文件大小格式化", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1024)).toBe("1 KB");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(KB_MAX_FILE_BYTES)).toBe("50 MB");
    expect(formatBytes(KB_MAX_BATCH_BYTES)).toBe("300 MB");
    expect(formatBytes(1024 * 1024 * 1024)).toBe("1 GB");
  });

  it("列表加载失败的陈旧横幅带上次成功时间", () => {
    expect(staleBannerText(null)).toBe("数据可能不是最新（尚未成功加载过）");
    expect(staleBannerText(new Date(2026, 9, 31, 9, 5, 7))).toBe(
      "数据可能不是最新（上次成功 09:05:07）",
    );
  });
});
