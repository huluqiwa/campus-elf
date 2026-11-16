import { describe, expect, it } from "vitest";

import { ApiError } from "@campus-elf/api-client";
import type { KbChunkStatsView, KbDocumentView, KbVersionView } from "@campus-elf/api-client";

import {
  aclBannerText,
  aclConfirmedLabel,
  aclManageGate,
  aclVisible,
  audienceEditError,
  audienceTypeLabel,
  chunkConfirmGate,
  chunkEditActionMessage,
  chunkEditError,
  chunkEditGate,
  chunkEditMaxChars,
  CHUNK_EDIT_MIN_CAP,
  chunkFilterError,
  chunkTypeLabel,
  chunksGate,
  embeddingStatusMeta,
  formatChunkStats,
  formatEditDiff,
  formatScore,
  KB_CHUNKS_CODE,
  KB_DELETE_CODE,
  KB_MAX_BATCH_BYTES,
  KB_MAX_FILE_BYTES,
  KB_PUBLISH_CODE,
  normalizeChunkQ,
  SCAN_POLL_WINDOW_MS,
  batchPreflightError,
  buildMetadata,
  defaultTitle,
  deleteGate,
  documentStatusMeta,
  filePreflightError,
  formatBytes,
  isActiveVersion,
  kbErrorInfo,
  kbPublishActionMessage,
  metadataError,
  offlineGate,
  parseTags,
  pendingScanDocumentIds,
  publishGate,
  retrievalQueryError,
  retrievalTopKError,
  rerankStatusLabel,
  rollbackTargets,
  routeScoreText,
  scanEngineLabel,
  scanStateMeta,
  scanTone,
  sectionOptions,
  shouldPollScan,
  staleBannerText,
  tokenLabel,
  unfilteredGate,
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

describe("发布态状态文案（KB-05B：权威全集 14 档，backend app/kb/status.py:16-29）", () => {
  it("构建生命周期四态", () => {
    expect(documentStatusMeta("BUILDING")).toEqual({ label: "构建中", tone: "info" });
    expect(documentStatusMeta("READY")).toEqual({ label: "已构建·待测试", tone: "info" });
    expect(documentStatusMeta("FAILED")).toEqual({ label: "构建失败", tone: "danger" });
    expect(documentStatusMeta("TESTING")).toEqual({ label: "测试进行中", tone: "warning" });
  });

  it("测试结论两态", () => {
    expect(documentStatusMeta("TEST_PASSED")).toEqual({
      label: "测试通过·可发布",
      tone: "success",
    });
    expect(documentStatusMeta("TEST_FAILED")).toEqual({ label: "测试未通过", tone: "danger" });
  });

  it("发布三态：成功绿留给 PUBLISHED，OFFLINE 中性，DELETED 危险", () => {
    expect(documentStatusMeta("PUBLISHED")).toEqual({ label: "已发布", tone: "success" });
    expect(documentStatusMeta("OFFLINE")).toEqual({ label: "已下线", tone: "info" });
    expect(documentStatusMeta("DELETED")).toEqual({ label: "已删除", tone: "danger" });
  });
});

describe("发布/回滚/下线/删除门禁（镜像后端 school_kb.py:64-68 与 status.py 守卫）", () => {
  const ALL = [KB_PUBLISH_CODE, KB_DELETE_CODE];

  it("publishGate：只有 TEST_PASSED + school.kb.publish 才亮", () => {
    expect(publishGate("TEST_PASSED", "v-1", ALL)).toBe(true);
    // 状态不对：门禁拒绝（后端 assert_publishable 的前端 UX 镜像）
    expect(publishGate("READY", "v-1", ALL)).toBe(false);
    expect(publishGate("PUBLISHED", "v-1", ALL)).toBe(false);
    expect(publishGate("TEST_FAILED", "v-1", ALL)).toBe(false);
    // 缺码
    expect(publishGate("TEST_PASSED", "v-1", ["school.kb.view"])).toBe(false);
    // 没有当前版本（详情未加载完）
    expect(publishGate("TEST_PASSED", null, ALL)).toBe(false);
    expect(publishGate("TEST_PASSED", undefined, ALL)).toBe(false);
  });

  it("offlineGate：PUBLISHED + school.kb.publish", () => {
    expect(offlineGate("PUBLISHED", "v-1", ALL)).toBe(true);
    expect(offlineGate("TEST_PASSED", "v-1", ALL)).toBe(false);
    expect(offlineGate("OFFLINE", "v-1", ALL)).toBe(false);
    expect(offlineGate("PUBLISHED", "v-1", ["school.kb.delete"])).toBe(false);
    expect(offlineGate("PUBLISHED", null, ALL)).toBe(false);
  });

  it("deleteGate：PUBLISHED + school.kb.delete（独立码，与发布分授）", () => {
    expect(deleteGate("PUBLISHED", "v-1", [KB_DELETE_CODE])).toBe(true);
    expect(deleteGate("PUBLISHED", "v-1", [KB_PUBLISH_CODE])).toBe(false);
    expect(deleteGate("OFFLINE", "v-1", [KB_DELETE_CODE])).toBe(false);
    expect(deleteGate("PUBLISHED", null, [KB_DELETE_CODE])).toBe(false);
  });

  it("rollbackTargets：只列 PUBLISHED，已知活跃版本被排除，未知活跃全列", () => {
    const versions = [
      { id: "v-1", version_no: 1, filename: "a.md", status: "PUBLISHED" },
      { id: "v-2", version_no: 2, filename: "b.md", status: "PUBLISHED" },
      { id: "v-3", version_no: 3, filename: "c.md", status: "TEST_PASSED" },
      { id: "v-4", version_no: 4, filename: "d.md", status: "OFFLINE" },
    ];
    expect(rollbackTargets(versions, null)).toHaveLength(2);
    expect(rollbackTargets(versions, null).map((t) => t.id)).toEqual(["v-1", "v-2"]);
    // 会话内已知活跃（publish/rollback 响应记忆，D2-A）：排除
    expect(rollbackTargets(versions, "v-2").map((t) => t.id)).toEqual(["v-1"]);
    // 全被排除时给空数组（下拉显示「无可回滚目标」而不是隐藏按钮——按钮可见性只看权限+状态）
    expect(rollbackTargets([versions[0]], "v-1")).toEqual([]);
  });
});

describe("KB-08B 活跃版本判据（isActiveVersion；服真化 TD-305 前端半）", () => {
  const versions = [
    { id: "v-1", version_no: 1, filename: "a.md", status: "PUBLISHED" },
    { id: "v-2", version_no: 2, filename: "b.md", status: "PUBLISHED" },
  ];

  it("active_version_id 命中的版本行打标", () => {
    expect(isActiveVersion("v-1", "v-1")).toBe(true);
    expect(isActiveVersion("v-2", "v-1")).toBe(false);
  });

  it("null（无活跃发布/真值未加载）时任何行都不打标——不误标", () => {
    for (const version of versions) {
      expect(isActiveVersion(version.id, null)).toBe(false);
    }
  });

  it("rollbackTargets 以服真 activeVersionId 排除活跃（真值 + 会话更新同接口）", () => {
    // 详情回包初始化（服务端真值）与会话内动作响应就地更新走同一参数，判据不变
    expect(rollbackTargets(versions, "v-1").map((t) => t.id)).toEqual(["v-2"]);
  });
});

describe("发布动作 409 文案（服务端 message 是主文案，码是兜底；KB-05 审查 m-4 先例）", () => {
  it("KB_PUBLISHED_NOT_ACTIVE 引导用回滚", () => {
    const error = new ApiError("请求失败（409）", 409, {
      code: "KB_PUBLISHED_NOT_ACTIVE",
      message: "版本已发布但不是活跃发布，请使用回滚端点切换",
    });
    expect(kbPublishActionMessage(error, "操作失败")).toBe(
      "版本已发布但不是活跃发布，请使用回滚端点切换",
    );
  });

  it("服务端 message 缺失时按码给可执行指引", () => {
    const bare = (code: string) => new ApiError("请求失败（409）", 409, { code });
    expect(kbPublishActionMessage(bare("KB_NOT_TEST_PASSED"), "操作失败")).toContain("测试");
    expect(kbPublishActionMessage(bare("KB_PUBLISHED_NOT_ACTIVE"), "操作失败")).toContain("回滚");
    expect(kbPublishActionMessage(bare("KB_NOT_PUBLISHED"), "操作失败")).toContain("已发布");
    expect(kbPublishActionMessage(bare("KB_NO_ACTIVE_PUBLISH"), "操作失败")).toContain("活跃发布");
    expect(kbPublishActionMessage(bare("KB_ROLLBACK_TARGET_IS_ACTIVE"), "操作失败")).toContain(
      "已是当前活跃",
    );
    expect(kbPublishActionMessage(bare("KB_INDEX_NOT_READY"), "操作失败")).toContain("构建");
    expect(kbPublishActionMessage(bare("KB_INDEX_INCOMPLETE"), "操作失败")).toContain("构建");
  });

  it("未知码回落到调用方 fallback；普通 Error 遵循 toUserMessage 惯例显示其 message", () => {
    const unknown = new ApiError("请求失败（409）", 409, { code: "SOMETHING_ELSE" });
    expect(kbPublishActionMessage(unknown, "操作失败")).toBe("操作失败");
    // 非 ApiError 交给 toUserMessage：有 message 显示 message，无 message 退 fallback
    expect(kbPublishActionMessage(new Error("网络错误"), "操作失败")).toBe("网络错误");
    expect(kbPublishActionMessage(new Error(""), "操作失败")).toBe("操作失败");
  });
});

// ---------------------------------------------------------------------------
// KB-06B：分块预览纯函数（后端契约 app/kb/chunk_read.py + school_kb.py:1064-1119）
// ---------------------------------------------------------------------------

describe("KB-06B chunksGate（backend app/kb/chunk_read.py 单码门禁）", () => {
  it("持有 school.kb.view_chunks 即给入口；缺码/空权限不给", () => {
    expect(chunksGate([KB_CHUNKS_CODE])).toBe(true);
    expect(chunksGate(["school.kb.view", KB_CHUNKS_CODE])).toBe(true);
    expect(chunksGate(["school.kb.view"])).toBe(false);
    expect(chunksGate([])).toBe(false);
  });

  it("KB_CHUNKS_CODE 与后端 ChunkViewerDep 同码", () => {
    expect(KB_CHUNKS_CODE).toBe("school.kb.view_chunks");
  });
});

describe("KB-06B 搜索词口径（chunk_read.py normalize_q + max_length=200）", () => {
  it("normalizeChunkQ：strip 后空 = 未提供（undefined 不进查询串）；词内空格保留", () => {
    expect(normalizeChunkQ("  配置项  ")).toBe("配置项");
    expect(normalizeChunkQ("表格 冻结")).toBe("表格 冻结");
    expect(normalizeChunkQ("   ")).toBeUndefined();
    expect(normalizeChunkQ("")).toBeUndefined();
  });

  it("chunkFilterError：raw 长度 >200 拦（含空白填充——后端按 raw 度量，审查 Minor 2 口径逐字一致）", () => {
    expect(chunkFilterError("字".repeat(201))).toBe("搜索词最长 200 字符");
    expect(chunkFilterError(`${"字".repeat(160)}${" ".repeat(41)}`)).toBe("搜索词最长 200 字符");
    expect(chunkFilterError("字".repeat(200))).toBeNull();
    expect(chunkFilterError("配置项")).toBeNull();
    expect(chunkFilterError(undefined)).toBeNull();
  });

  it("chunkFilterError 按码点度量（审查 m-1）：101 个 emoji（202 UTF-16 码元 / 101 码点）放行", () => {
    // JS .length 是 UTF-16 码元数：若按码元度量会把后端接受的 101 码点 emoji 拦下（假门禁）
    expect(chunkFilterError("🜁".repeat(101))).toBeNull();
    expect(chunkFilterError("🜁".repeat(201))).toBe("搜索词最长 200 字符");
  });
});

describe("KB-06B 文案与统计摘要", () => {
  it("chunkTypeLabel：父/子两档；未知档位显示原码", () => {
    expect(chunkTypeLabel("parent")).toBe("父块");
    expect(chunkTypeLabel("child")).toBe("子块");
    expect(chunkTypeLabel("weird")).toBe("未知类型（weird）");
  });

  it("embeddingStatusMeta：三态映射；未知档位显示原码", () => {
    expect(embeddingStatusMeta("pending")).toEqual({ label: "待嵌入", tone: "info" });
    expect(embeddingStatusMeta("ready")).toEqual({ label: "已嵌入", tone: "success" });
    expect(embeddingStatusMeta("failed")).toEqual({ label: "嵌入失败", tone: "danger" });
    expect(embeddingStatusMeta("weird").label).toBe("未知状态（weird）");
  });

  it("formatChunkStats：总数/父子/嵌入三态/字符齐全；缺失键按 0 兜底", () => {
    const stats: KbChunkStatsView = {
      total_chunks: 10,
      parent_count: 4,
      child_count: 6,
      embedding_status: { pending: 2, ready: 8, failed: 0 },
      total_tokens: 120,
      total_chars: 2400,
      section_count: 3,
      sections: [],
    };
    expect(formatChunkStats(stats)).toBe("共 10 块（父 4 / 子 6）· 嵌入 待2 成8 败0 · 2400 字符");
    const missing: KbChunkStatsView = { ...stats, embedding_status: {} };
    expect(formatChunkStats(missing)).toContain("待0 成0 败0");
  });

  it("sectionOptions：值=section_path（精确匹配语义），标签带块数", () => {
    expect(
      sectionOptions([
        { section_path: "操作手册 > 第一章 安装", chunk_count: 4 },
        { section_path: "操作手册 > 第二章 配置", chunk_count: 9 },
      ]),
    ).toEqual([
      { value: "操作手册 > 第一章 安装", label: "操作手册 > 第一章 安装（4）" },
      { value: "操作手册 > 第二章 配置", label: "操作手册 > 第二章 配置（9）" },
    ]);
    expect(sectionOptions([])).toEqual([]);
  });
});

// ---- KB-07B：检索测试纯函数（后端契约 retrieval_test.py / school_kb.py KbRetrievalTestResponse）----

describe("KB-07B rerankStatusLabel", () => {
  it("四档文案与 rerank_status 一一对应", () => {
    expect(rerankStatusLabel("applied")).toBe("已重排");
    expect(rerankStatusLabel("skipped")).toBe("重排超时跳过");
    expect(rerankStatusLabel("failed")).toBe("重排失败降级");
    expect(rerankStatusLabel("disabled")).toBe("未启用重排");
  });
});

describe("KB-07B 分数格式化", () => {
  it("formatScore：数值 4 位小数，null 显示「—」", () => {
    expect(formatScore(0.9786123)).toBe("0.9786");
    expect(formatScore(0)).toBe("0.0000");
    expect(formatScore(null)).toBe("—");
  });

  it("routeScoreText：分数 + 该路 1-based 名次；null = 该路未召回", () => {
    expect(routeScoreText({ score: 0.97861, rank: 1 })).toBe("0.9786 · #1");
    expect(routeScoreText({ score: 0.5, rank: 12 })).toBe("0.5000 · #12");
    expect(routeScoreText(null)).toBe("—");
  });
});

describe("KB-07B 检索词与 top_k 前端预检", () => {
  it("retrievalQueryError：strip 后空拦、raw 码点 >300 拦、合法通过", () => {
    expect(retrievalQueryError("")).toBe("请输入检索词");
    expect(retrievalQueryError("   ")).toBe("请输入检索词");
    expect(retrievalQueryError("长".repeat(301))).toBe("检索词不能超过 300 个字符");
    // m-1 教训：非 BMP 字符按码点度量——300 个 emoji（600 UTF-16 单元）是合法的
    expect(retrievalQueryError("👍".repeat(300))).toBeNull();
    expect(retrievalQueryError(" 奖学金申请条件 ")).toBeNull();
  });

  it("retrievalTopKError：1~20 之外的整数拦", () => {
    expect(retrievalTopKError(0)).toBe("top-k 需为 1~20 的整数");
    expect(retrievalTopKError(21)).toBe("top-k 需为 1~20 的整数");
    expect(retrievalTopKError(10)).toBeNull();
    expect(retrievalTopKError(1)).toBeNull();
    expect(retrievalTopKError(20)).toBeNull();
  });
});

describe("KB-09C 人群配置门禁（权限码纯函数）", () => {
  const VIEW = ["school.kb.view"];
  const MANAGE = ["school.kb.view", "school.kb.acl.manage"];
  const UNFILTERED = ["school.kb.view_chunks", "school.kb.retrieval_test.unfiltered"];

  it("aclVisible：school.kb.view 持有人可见入口（只读）", () => {
    expect(aclVisible(VIEW)).toBe(true);
    expect(aclVisible(MANAGE)).toBe(true);
    expect(aclVisible([])).toBe(false);
  });

  it("aclManageGate：编辑/确认按钮仅 acl.manage 持有人（GET view 与 PUT manage 分授镜像）", () => {
    expect(aclManageGate(MANAGE)).toBe(true);
    expect(aclManageGate(VIEW)).toBe(false);
    expect(aclManageGate([])).toBe(false);
  });

  it("unfilteredGate：ignore_acl 控件仅 unfiltered 码持有人渲染", () => {
    expect(unfilteredGate(UNFILTERED)).toBe(true);
    expect(unfilteredGate(["school.kb.view_chunks"])).toBe(false);
    expect(unfilteredGate([])).toBe(false);
  });
});

describe("KB-09C 人群条目校验（镜像后端 parse_audience_items 422 族）", () => {
  it("空集合法（PUT 空数组=清空→回未确认态）", () => {
    expect(audienceEditError([])).toBeNull();
  });

  it("七类逐类合法；all 不带 id 合法", () => {
    for (const t of ["department", "role", "class", "grade", "course", "user"]) {
      expect(audienceEditError([{ audience_type: t, audience_id: "x" }])).toBeNull();
    }
    expect(audienceEditError([{ audience_type: "all", audience_id: null }])).toBeNull();
  });

  it("白名单外类型 → AUDIENCE_TYPE_INVALID 文案", () => {
    expect(audienceEditError([{ audience_type: "group", audience_id: "x" }])).toContain("人群类型");
  });

  it("all 与其他类型混配 → AUDIENCE_ALL_MIXED 文案（fail-closed）", () => {
    const error = audienceEditError([
      { audience_type: "all", audience_id: null },
      { audience_type: "department", audience_id: "d-1" },
    ]);
    expect(error).toContain("不得与其他人群类型混配");
  });

  it("all 带 id → AUDIENCE_ID_FORBIDDEN 文案", () => {
    expect(audienceEditError([{ audience_type: "all", audience_id: "d-1" }])).toContain(
      "不接受对象 id",
    );
  });

  it("非 all 空 id（含空白）→ AUDIENCE_ID_REQUIRED 文案", () => {
    for (const blank of ["", "   ", null]) {
      expect(audienceEditError([{ audience_type: "department", audience_id: blank }])).toContain(
        "必须提供对象 id",
      );
    }
  });

  it("同 type+id 重复 → AUDIENCE_DUPLICATE 文案", () => {
    expect(
      audienceEditError([
        { audience_type: "department", audience_id: "d-1" },
        { audience_type: "department", audience_id: "d-1" },
      ]),
    ).toContain("重复");
  });

  it("id 尾随空白与洁净 id 视为同一条目（判重键先 trim，与后端 strip 后判重同构——复审 m4 固化）", () => {
    expect(
      audienceEditError([
        { audience_type: "department", audience_id: "d-1 " },
        { audience_type: "department", audience_id: "d-1" },
      ]),
    ).toContain("重复");
  });
});

describe("KB-09C 确认态文案与 token 展示", () => {
  it("aclConfirmedLabel：确认/未确认两态", () => {
    expect(aclConfirmedLabel(true)).toContain("已确认");
    expect(aclConfirmedLabel(false)).toContain("未确认");
  });

  it("aclBannerText：未确认时警示「确认后才能构建/发布」", () => {
    expect(aclBannerText(false)).toContain("未确认");
    expect(aclBannerText(false)).toContain("确认");
    expect(aclBannerText(true)).toBeNull();
  });

  it("audienceTypeLabel：七类中文标签；未知类型显示原码", () => {
    expect(audienceTypeLabel("department")).toBe("部门");
    expect(audienceTypeLabel("all")).toBe("全体");
    expect(audienceTypeLabel("group")).toBe("group");
  });

  it("tokenLabel：all→全体；type:<id> 原样展示（诊断面不美化 id）", () => {
    expect(tokenLabel("all")).toBe("全体");
    expect(tokenLabel("department:d-1")).toBe("department:d-1");
  });
});

describe("chunkEditGate / chunkConfirmGate（KB-10B 卡 E3）", () => {
  it("edit 门禁单码 chunk.edit", () => {
    expect(chunkEditGate(["school.kb.chunk.edit"])).toBe(true);
    expect(chunkEditGate(["school.kb.view"])).toBe(false);
    expect(chunkEditGate([])).toBe(false);
  });

  it("confirm 门禁 = chunk.confirm 码 + school_super_admin role 双判（derive 后端双重门禁镜像）", () => {
    expect(chunkConfirmGate(["school.kb.chunk.confirm"], ["school_super_admin"])).toBe(true);
    expect(chunkConfirmGate(["school.kb.chunk.confirm"], ["school_admin"])).toBe(false);
    expect(chunkConfirmGate(["school.kb.view"], ["school_super_admin"])).toBe(false);
    expect(chunkConfirmGate([], [])).toBe(false);
  });
});

describe("chunkEditMaxChars（KB-10B 卡 E5-A / TD-320）", () => {
  it("短块下限 500", () => {
    expect(chunkEditMaxChars(120)).toBe(CHUNK_EDIT_MIN_CAP);
    expect(chunkEditMaxChars(CHUNK_EDIT_MIN_CAP)).toBe(CHUNK_EDIT_MIN_CAP);
  });

  it("超长原子块 = 原块码点（同长度修正可过）", () => {
    expect(chunkEditMaxChars(946)).toBe(946);
    expect(chunkEditMaxChars(1200)).toBe(1200);
  });
});

describe("chunkEditError（镜像后端 validate_edit_content 422 族）", () => {
  it("空内容拒绝", () => {
    expect(chunkEditError("   ", 500)).toContain("不能为空");
    expect(chunkEditError("", 500)).toContain("CHUNK_CONTENT_INVALID");
  });

  it("超长拒绝且文案含动态上限", () => {
    const err = chunkEditError("字".repeat(601), 600);
    expect(err).toContain("601 > 600");
    expect(chunkEditError("字".repeat(600), 600)).toBeNull();
  });

  it("代码围栏奇数拒绝、偶数放行（```python 带语言标记同算）", () => {
    expect(chunkEditError("```python\nprint(1)\n", 500)).toContain("围栏未闭合");
    expect(chunkEditError("```python\nprint(1)\n```", 500)).toBeNull();
    expect(chunkEditError("```\na\n```\n```\nb\n```", 500)).toBeNull();
  });

  it("表格行组首尾管道齐备校验（单行组亦校验，审查 N7 口径）", () => {
    expect(chunkEditError("| 完整 | 行 |", 500)).toBeNull();
    expect(chunkEditError("| 表头 |\n| --- |\n| 断裂行", 500)).toContain("表格结构不完整");
    expect(chunkEditError("| 表头 |\n| --- |\n| 尾 |", 500)).toBeNull();
    expect(chunkEditError("正文段落，无表格。", 500)).toBeNull();
  });

  it("trim 规范化与上限联动（与后端一致：先 trim 再比长度）", () => {
    expect(chunkEditError("  ok  ", 500)).toBeNull();
  });
});

describe("chunkEditActionMessage（409/422/403 族码→指引；服务端 message 优先）", () => {
  const apiError = (code: string, message?: string) =>
    new ApiError("请求失败", 409, { code, ...(message ? { message } : {}) });

  it("服务端 message 优先", () => {
    expect(chunkEditActionMessage(apiError("CHUNK_CONFLICT", "服务端文案"), "兜底")).toBe(
      "服务端文案",
    );
  });

  it("按码给指引", () => {
    expect(chunkEditActionMessage(apiError("CHUNK_CONFLICT"), "兜底")).toContain("base_hash");
    expect(chunkEditActionMessage(apiError("CHUNK_EDIT_STATE"), "兜底")).toContain("派生版本");
    expect(chunkEditActionMessage(apiError("DERIVE_NO_EDITS"), "兜底")).toContain("编辑提交");
    expect(chunkEditActionMessage(apiError("CHUNK_CONFIRM_FORBIDDEN"), "兜底")).toContain("超管");
  });

  it("未知码退 fallback", () => {
    expect(chunkEditActionMessage(apiError("WEIRD_CODE"), "兜底")).toBe("兜底");
  });
});

describe("formatEditDiff（卡 E3 纯文本逐行摘要）", () => {
  it("无变更 = 空", () => {
    expect(formatEditDiff("同文", "同文")).toEqual({ added: [], removed: [] });
  });

  it("单行修改 = 一删一增", () => {
    const diff = formatEditDiff("第一行\n第二行", "第一行\n第二行改");
    expect(diff.removed).toEqual(["第二行"]);
    expect(diff.added).toEqual(["第二行改"]);
  });

  it("多行增删对齐（重复行按 multiset 计数）", () => {
    const diff = formatEditDiff("a\nb\nb", "a\nc\nb");
    expect(diff.removed).toEqual(["b"]);
    expect(diff.added).toEqual(["c"]);
  });
});
