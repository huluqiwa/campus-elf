import { expect, test, type Page } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";
import {
  kbChunk,
  kbDocument,
  kbVersion,
  mockKbApi,
  type KbChunkFixture,
  type KbDocumentFixture,
} from "./helpers/kb-mock";

// M3 KB-01B 知识库上传/列表（接口 mock）：权限入口、本地预检、整批原子错误、幂等键、
// 扫描状态轮询与文案、游标分页、详情版本加载、陈旧横幅。
// mock 的判据是手抄后端的（见 kb-mock.ts 的行号注释）——TD-253 的已知缺口，收口靠 KB-01C。

const UPLOAD_ONLY = ["school.kb.upload"];
const VIEW_ONLY = ["school.kb.view"];
const BOTH = ["school.kb.upload", "school.kb.view"];

const DOC_WITH_VERSIONS = "60000000-0000-0000-0000-000000000001";

async function pickFiles(
  page: Page,
  files: { name: string; mimeType: string; buffer: Buffer }[],
): Promise<void> {
  await page.locator("input[type=file]").setInputFiles(files);
}

test("教师（仅 upload）：只出现上传入口，直连知识库列表 → 403", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: UPLOAD_ONLY } });
  await mockKbApi(page);

  await page.goto("/");
  await expect(page.getByRole("menuitem", { name: "上传文档" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "知识库" })).toHaveCount(0);

  await page.goto("/kb/documents");
  await expect(page.getByText("你没有访问该页面的权限")).toBeVisible();
});

test("校方管理员（仅 view）：只出现知识库入口，直连上传页 → 403", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: VIEW_ONLY } });
  await mockKbApi(page);

  await page.goto("/");
  await expect(page.getByRole("menuitem", { name: "知识库" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "上传文档" })).toHaveCount(0);

  await page.goto("/kb/upload");
  await expect(page.getByText("你没有访问该页面的权限")).toBeVisible();
});

test("两种权限都有：两个入口都出现", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: BOTH } });
  await mockKbApi(page);

  await page.goto("/");
  await expect(page.getByRole("menuitem", { name: "知识库" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "上传文档" })).toBeVisible();
});

test("上传成功：结果页先显示待扫描，轮询后变为已通过", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: BOTH } });
  const mock = await mockKbApi(page);
  const { captures } = mock;

  await page.goto("/kb/upload");
  await pickFiles(page, [
    { name: "安全手册.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 e2e") },
  ]);
  await expect(page.getByTestId("kb-upload-table")).toContainText("安全手册.pdf");
  await page.getByTestId("kb-submit").click();

  await expect(page.getByTestId("kb-upload-result")).toBeVisible();
  await expect(page.getByTestId("kb-batch-id")).toContainText("批次：80000000");
  await expect(page.getByTestId("kb-result-table")).toContainText("待扫描/扫描中");

  expect(captures.uploads).toHaveLength(1);
  expect(captures.uploads[0]?.fileNames).toEqual(["安全手册.pdf"]);
  expect(captures.uploads[0]?.metadata).toBeNull();
  expect(captures.uploads[0]?.contentType).toContain("multipart/form-data");
  expect(captures.uploads[0]?.idempotencyKey).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
  );

  await expect(page.getByTestId("kb-result-table")).toContainText("已通过", { timeout: 10_000 });
  await expect(page.getByTestId("kb-result-table")).toContainText("ClamAV");
  await expect(page.getByTestId("kb-result-table")).toContainText("待审批");
  await expect(page.getByText("扫描已全部结束")).toBeVisible();
  expect(captures.detailIds).toEqual([mock.state.documents[0]?.id]);
  expect(mock.state.documents[0]?.status).toBe("PENDING_APPROVAL");
  expect(mock.state.documents[0]?.current_version?.scan.state).toBe("passed");
});

test("元数据：只发编辑过的行，未编辑的行发 {}", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: BOTH } });
  const { captures } = await mockKbApi(page);

  await page.goto("/kb/upload");
  await pickFiles(page, [
    { name: "安全手册.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 a") },
    { name: "通知.md", mimeType: "text/markdown", buffer: Buffer.from("# 通知") },
  ]);
  await page.getByTestId("kb-meta-toggle").click();
  await page.getByPlaceholder("默认：安全手册").fill("安全手册（2026）");
  await page.getByPlaceholder("逗号分隔，≤10 个，每个 ≤32 字").first().fill("安全，教学,安全");
  await page.getByPlaceholder("可选").first().fill("制度汇编");
  await page.getByTestId("kb-submit").click();

  await expect(page.getByTestId("kb-upload-result")).toBeVisible();
  expect(JSON.parse(captures.uploads[0]?.metadata ?? "null")).toEqual([
    { title: "安全手册（2026）", tags: ["安全", "教学"], description: "制度汇编" },
    {},
  ]);
  await expect(page.getByTestId("kb-result-table")).toContainText("安全手册（2026）");
  await expect(page.getByTestId("kb-result-table")).toContainText("通知");
});

test("元数据开关关闭：已填内容保留在表单，但本次不发送 metadata", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: BOTH } });
  const { captures } = await mockKbApi(page);

  await page.goto("/kb/upload");
  await pickFiles(page, [
    { name: "安全手册.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 a") },
  ]);
  await page.getByTestId("kb-meta-toggle").click();
  await page.getByPlaceholder("默认：安全手册").fill("安全手册（2026）");
  await page.getByTestId("kb-meta-toggle").click();
  await expect(page.getByPlaceholder("默认：安全手册")).toHaveCount(0);
  // 关闭只影响「是否发送」，已填内容不丢
  await page.getByTestId("kb-meta-toggle").click();
  await expect(page.getByPlaceholder("默认：安全手册")).toHaveValue("安全手册（2026）");
  await page.getByTestId("kb-meta-toggle").click();
  await page.getByTestId("kb-submit").click();

  await expect(page.getByTestId("kb-upload-result")).toBeVisible();
  expect(captures.uploads[0]?.metadata).toBeNull();
  // 未发送标题 → 后端用默认标题（文件名去扩展名）
  await expect(page.getByTestId("kb-result-table")).toContainText("安全手册");
});

test("本地预检：非白名单扩展名被拦下且不发请求", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: BOTH } });
  const { captures } = await mockKbApi(page);

  await page.goto("/kb/upload");
  await pickFiles(page, [
    { name: "virus.exe", mimeType: "application/octet-stream", buffer: Buffer.from("junk") },
  ]);
  await expect(page.getByTestId("kb-row-error")).toContainText("类型不支持");
  await page.getByTestId("kb-submit").click();

  await expect(page.getByTestId("kb-row-error")).toBeVisible();
  expect(captures.uploads).toHaveLength(0);
});

test("后端逐文件 422：整批未保存 + 逐文件错误表；原样重试复用同一个键", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: BOTH } });
  const { captures } = await mockKbApi(page, {
    uploadErrorOnce: true,
    uploadError: {
      status: 422,
      body: {
        detail: {
          code: "BATCH_VALIDATION_FAILED",
          message: "整批校验失败",
          files: [
            {
              index: 0,
              filename: "坏文件.pdf",
              code: "MAGIC_MISMATCH",
              message: "文件内容不是 PDF",
            },
          ],
        },
      },
    },
  });

  await page.goto("/kb/upload");
  await pickFiles(page, [
    { name: "坏文件.pdf", mimeType: "application/pdf", buffer: Buffer.from("not a pdf") },
  ]);
  await page.getByTestId("kb-submit").click();

  await expect(page.getByTestId("kb-upload-error")).toContainText("整批校验失败");
  await expect(page.getByTestId("kb-file-errors")).toContainText("MAGIC_MISMATCH");
  await expect(page.getByTestId("kb-file-errors")).toContainText("文件内容不是 PDF");
  await expect(page.getByTestId("kb-upload-table")).toContainText("坏文件.pdf");
  const firstErrorRow = page.getByTestId("kb-file-errors").locator(".el-table__row").first();
  await expect(firstErrorRow.locator("td").first()).toHaveText("1"); // 后端 index 是 0 基，界面显示第 1 行

  await page.getByTestId("kb-submit").click();
  await expect(page.getByTestId("kb-upload-result")).toBeVisible();
  expect(captures.uploads).toHaveLength(2);
  // 422 不改内容 → 保留幂等键原样重试（不换新键）
  expect(captures.uploads[1]?.idempotencyKey).toBe(captures.uploads[0]?.idempotencyKey);
});

test("413：显示后端字符串文案，保留选择并用同一个键重试", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: BOTH } });
  const { captures } = await mockKbApi(page, {
    uploadErrorOnce: true,
    uploadError: { status: 413, body: { detail: "请求体超过服务端上限" } },
  });

  await page.goto("/kb/upload");
  await pickFiles(page, [
    { name: "安全手册.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 a") },
  ]);
  await page.getByTestId("kb-submit").click();

  await expect(page.getByTestId("kb-upload-error")).toContainText("请求体超过服务端上限");
  await expect(page.getByTestId("kb-upload-table")).toContainText("安全手册.pdf");

  await page.getByTestId("kb-submit").click();
  await expect(page.getByTestId("kb-upload-result")).toBeVisible();
  expect(captures.uploads[1]?.idempotencyKey).toBe(captures.uploads[0]?.idempotencyKey);
});

test("409 幂等键冲突：自动换新键，换键后重试成功", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: BOTH } });
  const { captures } = await mockKbApi(page, {
    uploadErrorOnce: true,
    uploadError: {
      status: 409,
      body: {
        detail: {
          code: "IDEMPOTENCY_KEY_REUSED",
          message: "该提交编号已被另一批文件使用",
        },
      },
    },
  });

  await page.goto("/kb/upload");
  await pickFiles(page, [
    { name: "安全手册.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 a") },
  ]);
  await page.getByTestId("kb-submit").click();
  await expect(page.getByText(/已自动换新编号/)).toBeVisible();
  await expect.poll(() => captures.uploads.length).toBe(1);
  const firstKey = captures.uploads[0]?.idempotencyKey;

  await page.getByTestId("kb-submit").click();
  await expect(page.getByTestId("kb-upload-result")).toBeVisible();
  expect(captures.uploads).toHaveLength(2);
  expect(captures.uploads[1]?.idempotencyKey).not.toBe(firstKey);
});

test("replayed=true：明确提示未创建新文档", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: BOTH } });
  await mockKbApi(page, { replayUploads: true });

  await page.goto("/kb/upload");
  await pickFiles(page, [
    { name: "安全手册.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 a") },
  ]);
  await page.getByTestId("kb-submit").click();

  await expect(page.getByTestId("kb-replayed")).toContainText("未创建新文档");
});

test("无查看权限：上传结果只给静态说明，不轮询详情", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: UPLOAD_ONLY } });
  const { captures } = await mockKbApi(page);

  await page.goto("/kb/upload");
  await pickFiles(page, [
    { name: "安全手册.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 a") },
  ]);
  await page.getByTestId("kb-submit").click();

  await expect(page.getByTestId("kb-upload-result")).toContainText("你没有知识库查看权限");
  // 等过首个 5 秒轮询窗口：有查看权限时详情请求早已发出，零请求才算证明「无权限不轮询」
  await page.waitForTimeout(6000);
  expect(captures.detailIds).toHaveLength(0);
});

test("列表：三档文档状态、扫描状态与开发跳过/未知兜底都不是绿色通过", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: VIEW_ONLY } });
  await mockKbApi(page);

  await page.goto("/kb/documents");
  const table = page.getByTestId("kb-document-table");

  await expect(table).toContainText("校园安全手册");
  await expect(table).toContainText("待审批");
  await expect(table).toContainText("可疑附件");
  await expect(table).toContainText("扫描未通过");
  await expect(table).toContainText("发现威胁");
  await expect(table).toContainText("开发样例");
  await expect(table).toContainText("已跳过扫描");
  await expect(table).toContainText("开发环境跳过扫描");
  await expect(table).toContainText("待扫描通知");
  await expect(table).toContainText("扫描中/待扫描");
  await expect(table).toContainText("扫描故障样例");
  await expect(table).toContainText("扫描故障（后台将重试）");
  await expect(table).toContainText("错误码：SCAN_TIMEOUT");
  // 列表的扫描列要能直接看到威胁签名，不必逐个打开抽屉（简报 D5）
  await expect(table).toContainText("签名：Eicar-Test-Signature");
  await expect(table).toContainText("遗留档案");
  await expect(table).toContainText("未知状态（ARCHIVED）");
  await expect(table).toContainText("未知（mystery）");
  await expect(table).toContainText("future-engine");

  await expect(table.locator(".el-tag--danger", { hasText: "发现威胁" })).toHaveCount(1);
  await expect(table.locator(".el-tag--warning", { hasText: "已跳过扫描" })).toHaveCount(1);
  await expect(table.locator(".el-tag--success", { hasText: "已跳过扫描" })).toHaveCount(0);
  await expect(
    table.locator(".el-tag--warning", { hasText: "扫描故障（后台将重试）" }),
  ).toHaveCount(1);
  await expect(table.locator(".el-tag--warning", { hasText: "未知状态（ARCHIVED）" })).toHaveCount(
    1,
  );
});

test("列表分页：游标上一页/下一页不伪造页码", async ({ page }) => {
  const documents: KbDocumentFixture[] = Array.from({ length: 22 }, (_, index) => {
    const number = index + 1;
    const padded = String(number).padStart(12, "0");
    return kbDocument({
      id: `60000000-0000-0000-0000-${padded}`,
      title: `批量文档 ${String(number).padStart(2, "0")}`,
      current_version: kbVersion({
        id: `61000000-0000-0000-0000-${padded}`,
        filename: `doc-${number}.pdf`,
      }),
    });
  });
  await mockAuth(page, { authenticated: true, user: { permissions: VIEW_ONLY } });
  const { captures } = await mockKbApi(page, { documents, listDelayMs: 400 });

  await page.goto("/kb/documents");
  const table = page.getByTestId("kb-document-table");
  const prev = page.getByRole("button", { name: "上一页" });
  const next = page.getByRole("button", { name: "下一页" });
  await expect(table).toContainText("批量文档 01");
  await expect(table).not.toContainText("批量文档 21");
  await expect(prev).toBeDisabled(); // 第一页不能假装还能往回翻
  expect(captures.listUrls[0]).toContain("limit=20");

  await next.click();
  // 慢网下加载中禁用翻页，避免两个响应互相覆盖游标栈
  await expect(next).toBeDisabled();
  await expect(prev).toBeDisabled();
  await expect(table).toContainText("批量文档 21");
  await expect(table).not.toContainText("批量文档 01");
  await expect(next).toBeDisabled(); // 22 条 = 20 + 2，第二页已是末页
  expect(captures.listUrls[1]).toContain("cursor=20");

  await prev.click();
  await expect(table).toContainText("批量文档 01");
  await expect(prev).toBeDisabled();
});

test("列表刷新失败：保留旧行并显示「数据可能不是最新」横幅", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: VIEW_ONLY } });
  await mockKbApi(page, { listFailOnRequest: 2 });

  await page.goto("/kb/documents");
  const table = page.getByTestId("kb-document-table");
  await expect(table).toContainText("校园安全手册");

  await page.getByTestId("kb-refresh").click();
  await expect(page.getByTestId("kb-stale-banner")).toContainText("数据可能不是最新（上次成功");
  await expect(table).toContainText("校园安全手册");

  await page.getByTestId("kb-stale-retry").click();
  await expect(page.getByTestId("kb-stale-banner")).toHaveCount(0);
});

test("详情抽屉：版本列表按游标加载更多", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: VIEW_ONLY } });
  const { captures } = await mockKbApi(page, { versionPageSize: 1 });

  await page.goto("/kb/documents");
  const row = page
    .getByTestId("kb-document-table")
    .locator(".el-table__row", { hasText: "校园安全手册" });
  await row.getByTestId("kb-detail-open").click();

  const drawer = page.getByTestId("kb-document-drawer");
  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText("当前版本");
  const versionRows = drawer.getByTestId("kb-version-table").locator(".el-table__row");
  await expect(versionRows).toHaveCount(1);
  expect(captures.versionUrls[0]).toContain("limit=20");

  await drawer.getByTestId("kb-versions-more").click();
  await expect(versionRows).toHaveCount(2);
  expect(captures.versionUrls[1]).toContain("cursor=1");
});

test("详情 404：提示文档已不可见、关闭抽屉并刷新列表", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: VIEW_ONLY } });
  const { captures } = await mockKbApi(page, { missingDocumentIds: [DOC_WITH_VERSIONS] });

  await page.goto("/kb/documents");
  const table = page.getByTestId("kb-document-table");
  await expect(table).toContainText("校园安全手册");
  await expect.poll(() => captures.listUrls.length).toBe(1);
  await table
    .locator(".el-table__row", { hasText: "校园安全手册" })
    .getByTestId("kb-detail-open")
    .click();

  await expect(page.getByText("文档已不可见，列表已刷新")).toBeVisible();
  await expect(page.getByTestId("kb-document-drawer")).toBeHidden();
  await expect.poll(() => captures.listUrls.length).toBe(2);
});

test("抽屉竞态：A 的迟到 404 不会关掉已打开的 B", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: VIEW_ONLY } });
  const { captures } = await mockKbApi(page, {
    missingDocumentIds: [DOC_WITH_VERSIONS],
    detailDelayMs: 700,
  });

  await page.goto("/kb/documents");
  const table = page.getByTestId("kb-document-table");
  await expect(table).toContainText("校园安全手册");
  const lateNotFound = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/api/v1/school/kb/documents/${DOC_WITH_VERSIONS}`) &&
      response.status() === 404,
  );
  await table
    .locator(".el-table__row", { hasText: "校园安全手册" })
    .getByTestId("kb-detail-open")
    .click();
  await expect.poll(() => captures.detailIds.length).toBeGreaterThan(0);

  // A 还在路上时关掉抽屉，改开 B
  await page.locator(".el-drawer__close-btn").click();
  await expect(page.getByTestId("kb-document-drawer")).toBeHidden();
  await table
    .locator(".el-table__row", { hasText: "可疑附件" })
    .getByTestId("kb-detail-open")
    .click();

  const drawer = page.getByTestId("kb-document-drawer");
  // 先等 A 的 404 真正回到页面：它属于上一条文档，不能误关 B，也不能弹「已不可见」
  await lateNotFound;
  await expect(drawer).toContainText("可疑附件");
  await expect(drawer).toBeVisible();
  await expect(page.getByText("文档已不可见，列表已刷新")).toHaveCount(0);
});

// ---- TD-282：上传结果页轮询的三条停止分支（页面级） ----

test("轮询停止①：详情 403 后停止刷新并提示无查看权限", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: BOTH } });
  const { captures } = await mockKbApi(page, {
    autoPassOnDetail: false,
    detailError: { status: 403, body: { detail: { code: "FORBIDDEN", message: "无权限" } } },
  });

  await page.goto("/kb/upload");
  await pickFiles(page, [
    { name: "权限撤回.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 e2e") },
  ]);
  await page.getByTestId("kb-submit").click();
  await expect(page.getByTestId("kb-upload-result")).toBeVisible();
  await expect(page.getByTestId("kb-result-table")).toContainText("扫描中/待扫描");

  await expect.poll(() => captures.detailIds.length, { timeout: 10_000 }).toBe(1);
  await expect(page.getByText("自动刷新已停止（无查看权限或被服务端拒绝）")).toBeVisible();
  // 停止后不再发详情请求（等过下一轮 5 秒窗口）
  await page.waitForTimeout(6000);
  expect(captures.detailIds).toHaveLength(1);
});

test("轮询暂停②：页面隐藏不发详情请求，恢复可见后继续并结束", async ({ page }) => {
  await page.addInitScript(() => {
    let hidden = false;
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => (hidden ? "hidden" : "visible"),
    });
    (window as unknown as { __setKbHidden: (value: boolean) => void }).__setKbHidden = (value) => {
      hidden = value;
    };
  });
  await mockAuth(page, { authenticated: true, user: { permissions: BOTH } });
  const { captures } = await mockKbApi(page);

  await page.goto("/kb/upload");
  await pickFiles(page, [
    { name: "隐藏暂停.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 e2e") },
  ]);
  await page.getByTestId("kb-submit").click();
  await expect(page.getByTestId("kb-upload-result")).toBeVisible();
  await page.evaluate(() => {
    (window as unknown as { __setKbHidden: (value: boolean) => void }).__setKbHidden(true);
  });

  await page.waitForTimeout(6000);
  expect(captures.detailIds).toHaveLength(0);
  await expect(page.getByTestId("kb-result-table")).toContainText("扫描中/待扫描");
  await expect(page.getByText("自动刷新已停止")).toHaveCount(0);

  await page.evaluate(() => {
    (window as unknown as { __setKbHidden: (value: boolean) => void }).__setKbHidden(false);
  });
  await expect.poll(() => captures.detailIds.length, { timeout: 10_000 }).toBeGreaterThan(0);
  await expect(page.getByText("扫描已全部结束")).toBeVisible();
});

test("轮询截止③：超过 5 分钟停止刷新并提示手动查看", async ({ page }) => {
  await page.addInitScript(() => {
    const realNow = Date.now.bind(Date);
    let offset = 0;
    Object.defineProperty(Date, "now", {
      configurable: true,
      value: () => realNow() + offset,
    });
    (window as unknown as { __advanceKbClock: (ms: number) => void }).__advanceKbClock = (ms) => {
      offset += ms;
    };
  });
  await mockAuth(page, { authenticated: true, user: { permissions: BOTH } });
  const { captures } = await mockKbApi(page, { autoPassOnDetail: false });

  await page.goto("/kb/upload");
  await pickFiles(page, [
    { name: "超时截止.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 e2e") },
  ]);
  await page.getByTestId("kb-submit").click();
  await expect(page.getByTestId("kb-upload-result")).toBeVisible();
  await page.evaluate(() => {
    (window as unknown as { __advanceKbClock: (ms: number) => void }).__advanceKbClock(
      5 * 60 * 1000 + 1000,
    );
  });

  // 下一轮 tick 先判 5 分钟窗口，不发请求直接停
  await expect(page.getByText("自动刷新已停止（超过 5 分钟）")).toBeVisible({ timeout: 10_000 });
  await page.waitForTimeout(6000);
  expect(captures.detailIds).toHaveLength(0);
});

// ---------------------------------------------------------------------------
// M3 KB-02B：文档抽屉的审批入口（提交审批 / 创建构建 / REJECTED 指引）
// ---------------------------------------------------------------------------

import { mockApprovalsApi } from "./helpers/approval-mock";
import { FIXTURE_KB_DOCUMENTS } from "./helpers/kb-mock";

test("抽屉审批入口：PENDING_APPROVAL + upload 权限可提交，成功后本抽屉会话内禁用", async ({
  page,
}) => {
  // 独占版本（不与 approval 夹具共享），提交走 201
  const doc = kbDocument({
    id: "60000000-0000-0000-0000-0000000000a1",
    title: "可提交样例",
    status: "PENDING_APPROVAL",
    current_version: kbVersion({
      id: "61000000-0000-0000-0000-0000000000a1",
      filename: "可提交样例.pdf",
    }),
  });
  await mockAuth(page, { authenticated: true, user: { permissions: BOTH } });
  await mockKbApi(page, { documents: [doc] });
  await mockApprovalsApi(page, { documents: [doc] });

  await page.goto("/kb/documents");
  await page
    .locator(".el-table__row", { hasText: "可提交样例" })
    .getByTestId("kb-detail-open")
    .click();
  const drawer = page.getByTestId("kb-document-drawer");
  await expect(drawer).toBeVisible();

  await drawer.getByTestId("kb-submit-approval").click();
  await expect(page.getByText("已提交审批，可在「审批中心 → 已办」查看进度或撤销")).toBeVisible();
  await expect(drawer.getByTestId("kb-submit-approval-done")).toBeDisabled();
  await expect(drawer.getByTestId("kb-submit-approval")).toHaveCount(0);
});

test("抽屉审批入口：同版本已有待审批单 → 后端 409 提示，不发整页刷新", async ({ page }) => {
  // 夹具第一条待审批单挂在 校园安全手册 v...0001 上 → 提交同一版本命中 APPROVAL_ALREADY_PENDING
  await mockAuth(page, { authenticated: true, user: { permissions: BOTH } });
  await mockKbApi(page, { documents: FIXTURE_KB_DOCUMENTS });
  await mockApprovalsApi(page, { documents: FIXTURE_KB_DOCUMENTS });

  await page.goto("/kb/documents");
  await page
    .locator(".el-table__row", { hasText: "校园安全手册" })
    .getByTestId("kb-detail-open")
    .click();
  const drawer = page.getByTestId("kb-document-drawer");
  await expect(drawer).toBeVisible();

  await drawer.getByTestId("kb-submit-approval").click();
  await expect(page.getByText("该版本已有待审批的申请")).toBeVisible();
});

test("抽屉构建入口：APPROVED + build 权限可创建，重复创建吃 409", async ({ page }) => {
  const doc = kbDocument({
    id: "60000000-0000-0000-0000-0000000000b1",
    title: "已审批样例",
    status: "APPROVED",
    current_version: kbVersion({
      id: "61000000-0000-0000-0000-0000000000b1",
      filename: "已审批样例.pdf",
      status: "APPROVED",
    }),
  });
  await mockAuth(page, {
    authenticated: true,
    user: { permissions: [...BOTH, "school.kb.build"] },
  });
  await mockKbApi(page, { documents: [doc] });
  const approvals = await mockApprovalsApi(page, { documents: [doc] });

  await page.goto("/kb/documents");
  await page
    .locator(".el-table__row", { hasText: "已审批样例" })
    .getByTestId("kb-detail-open")
    .click();
  const drawer = page.getByTestId("kb-document-drawer");
  await expect(drawer).toBeVisible();

  await drawer.getByTestId("kb-create-build").click();
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
  await expect(page.getByText("构建任务已创建（BUILD_QUEUED）")).toBeVisible();
  expect(approvals.state.jobs).toHaveLength(1);

  // 重开抽屉：会话内记忆复位，再点 → 后端 409 BUILD_ALREADY_ACTIVE
  await page.locator(".el-drawer__close-btn").click();
  await page
    .locator(".el-table__row", { hasText: "已审批样例" })
    .getByTestId("kb-detail-open")
    .click();
  await expect(drawer).toBeVisible();
  await drawer.getByTestId("kb-create-build").click();
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
  await expect(page.getByText("该版本已有进行中的构建任务")).toBeVisible();
  expect(approvals.state.jobs).toHaveLength(1);
});

test("REJECTED 文档：显示已驳回文案与审批中心指引，无入口按钮", async ({ page }) => {
  const doc = kbDocument({
    id: "60000000-0000-0000-0000-0000000000c1",
    title: "被驳回样例",
    status: "REJECTED",
    current_version: kbVersion({
      id: "61000000-0000-0000-0000-0000000000c1",
      filename: "被驳回样例.pdf",
      status: "REJECTED",
    }),
  });
  await mockAuth(page, { authenticated: true, user: { permissions: BOTH } });
  await mockKbApi(page, { documents: [doc] });
  await mockApprovalsApi(page, { documents: [doc] });

  await page.goto("/kb/documents");
  const row = page.locator(".el-table__row", { hasText: "被驳回样例" });
  await expect(row).toContainText("已驳回");
  await row.getByTestId("kb-detail-open").click();
  const drawer = page.getByTestId("kb-document-drawer");
  await expect(drawer).toBeVisible();

  await expect(drawer.getByTestId("kb-reject-guidance")).toContainText("审批中心 → 已办");
  await expect(drawer.getByTestId("kb-submit-approval")).toHaveCount(0);
  await expect(drawer.getByTestId("kb-create-build")).toHaveCount(0);
});

// ---------------------------------------------------------------------------
// M3 KB-04B：测试沙箱前端（上传→运行→轮询→结果 + TD-291 直显驳回理由）
// ---------------------------------------------------------------------------

import { mockTestApi } from "./helpers/test-mock";

test("测试沙箱：READY 文档上传测试集→运行→刷新→TEST_PASSED", async ({ page }) => {
  await mockAuth(page, {
    authenticated: true,
    user: { permissions: [...BOTH, "school.kb.test"] },
  });
  const doc = kbDocument({
    id: "60000000-0000-0000-0000-0000000000d1",
    title: "测试沙箱文档",
    status: "READY",
    current_version: kbVersion({
      id: "61000000-0000-0000-0000-0000000000d1",
      filename: "测试沙箱.pdf",
      status: "READY",
    }),
  });
  await mockKbApi(page, { documents: [doc] });
  await mockTestApi(page);

  await page.goto("/kb/documents");
  await page
    .locator(".el-table__row", { hasText: "测试沙箱文档" })
    .getByTestId("kb-detail-open")
    .click();
  const drawer = page.getByTestId("kb-document-drawer");
  await expect(drawer).toBeVisible();
  await expect(drawer.getByTestId("kb-test-section")).toBeVisible();

  // 上传并运行
  await drawer.getByTestId("kb-test-set-name").fill("冒烟测试集");
  await drawer.locator('input[data-testid="kb-test-set-file"]').setInputFiles({
    name: "test.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("question\n什么是开学时间？"),
  });
  await drawer.getByTestId("kb-test-start").click();
  await expect(page.getByText("测试集已上传")).toBeVisible();
  await expect(drawer.getByTestId("kb-test-run-status")).toContainText("测试进行中");

  // 刷新 → TEST_PASSED
  await drawer.getByTestId("kb-test-poll").click();
  await expect(drawer.getByTestId("kb-test-run-status")).toContainText("测试通过");
  await expect(drawer.getByTestId("kb-test-run-status")).toContainText("通过率 100%");
});

test("测试沙箱：无 school.kb.test 码 → 测试区块不出现", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: VIEW_ONLY } });
  await mockKbApi(page);

  await page.goto("/kb/documents");
  await page
    .locator(".el-table__row", { hasText: "校园安全手册" })
    .getByTestId("kb-detail-open")
    .click();
  const drawer = page.getByTestId("kb-document-drawer");
  await expect(drawer).toBeVisible();
  await expect(drawer.getByTestId("kb-test-section")).toHaveCount(0);
});

// ---- M3 KB-05B：发布/回滚/下线/删除（接口 mock；判据手抄 service.py，锚点见 kb-mock.ts）----

const PUBLISH_PERMS = [...BOTH, "school.kb.publish", "school.kb.delete"];

/** 蓝绿夹具：v1 已发布（可回滚目标）、v2 测试通过（待发布）。 */
function blueGreenDocument(): KbDocumentFixture {
  return kbDocument({
    id: "60000000-0000-0000-0000-0000000000d1",
    title: "发布样例",
    status: "TEST_PASSED",
    current_version: kbVersion({
      id: "61000000-0000-0000-0000-0000000000d2",
      version_no: 2,
      filename: "发布样例-v2.pdf",
      status: "TEST_PASSED",
    }),
    versions: [
      kbVersion({
        id: "61000000-0000-0000-0000-0000000000d1",
        version_no: 1,
        filename: "发布样例-v1.pdf",
        status: "PUBLISHED",
      }),
      kbVersion({
        id: "61000000-0000-0000-0000-0000000000d2",
        version_no: 2,
        filename: "发布样例-v2.pdf",
        status: "TEST_PASSED",
      }),
    ],
  });
}

function publishedDocument(): KbDocumentFixture {
  return kbDocument({
    id: "60000000-0000-0000-0000-0000000000e1",
    title: "已发布样例",
    status: "PUBLISHED",
    current_version: kbVersion({
      id: "61000000-0000-0000-0000-0000000000e1",
      filename: "已发布样例.pdf",
      status: "PUBLISHED",
    }),
    versions: [
      kbVersion({
        id: "61000000-0000-0000-0000-0000000000e1",
        version_no: 1,
        filename: "已发布样例.pdf",
        status: "PUBLISHED",
      }),
    ],
  });
}

async function openDrawer(page: Page, title: string): Promise<ReturnType<Page["getByTestId"]>> {
  await page.locator(".el-table__row", { hasText: title }).getByTestId("kb-detail-open").click();
  const drawer = page.getByTestId("kb-document-drawer");
  await expect(drawer).toBeVisible();
  return drawer;
}

test("KB-05B 发布：TEST_PASSED 可发布，成功后状态列/详情变已发布，服务端状态双写", async ({
  page,
}) => {
  const doc = blueGreenDocument();
  await mockAuth(page, { authenticated: true, user: { permissions: PUBLISH_PERMS } });
  const mock = await mockKbApi(page, { documents: [doc] });

  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "发布样例");

  // 版本表状态列：v1 已发布、v2 测试通过·可发布（14 档文案）
  const versionTable = drawer.getByTestId("kb-version-table");
  await expect(versionTable).toContainText("已发布");
  await expect(versionTable).toContainText("测试通过·可发布");

  await drawer.getByTestId("kb-publish").click();
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
  await expect(page.getByText("v2 已发布")).toBeVisible();

  // 服务端状态断言（不只看 toast）：版本行、current_version、文档行三方同步
  // （mockKbApi 会克隆夹具，断言必须走 mock.state.documents，不是 spec 持有的 doc 引用）
  const state = mock.state.documents[0];
  const v2 = state?.versions?.[1];
  expect(v2?.status).toBe("PUBLISHED");
  expect(state?.status).toBe("PUBLISHED");
  expect(state?.current_version?.status).toBe("PUBLISHED");
  expect(mock.captures.versionActions).toEqual([
    { method: "POST", path: expect.stringContaining("/publish") },
  ]);
});

test("KB-05B 发布门禁：READY 或缺 publish 码都不给发布按钮（前端门禁只是 UX）", async ({
  page,
}) => {
  const readyDoc = kbDocument({
    id: "60000000-0000-0000-0000-0000000000f1",
    title: "未测试样例",
    status: "READY",
    current_version: kbVersion({
      id: "61000000-0000-0000-0000-0000000000f1",
      filename: "未测试样例.pdf",
      status: "READY",
    }),
  });
  await mockAuth(page, { authenticated: true, user: { permissions: PUBLISH_PERMS } });
  await mockKbApi(page, { documents: [readyDoc] });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "未测试样例");
  await expect(drawer.getByTestId("kb-publish")).toHaveCount(0);
  await expect(drawer.getByTestId("kb-publish-row")).toHaveCount(0);
});

test("KB-05B 回滚 + KB-08B 服真化：发布→tag 迁移→回滚→重开抽屉 tag 仍在、下拉仍排除活跃", async ({
  page,
}) => {
  const doc = blueGreenDocument();
  await mockAuth(page, { authenticated: true, user: { permissions: PUBLISH_PERMS } });
  const mock = await mockKbApi(page, {
    documents: [doc],
    // v1 是当前活跃发布（真实蓝绿状态：文档显示 v2 的 TEST_PASSED，活跃发布仍是 v1）
    activePublishByDocument: { [doc.id]: "61000000-0000-0000-0000-0000000000d1" },
  });

  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "发布样例");
  const versionTable = drawer.getByTestId("kb-version-table");

  // KB-08B 服真化初始化：详情回包 active_version_id=v1 → v1 行挂「当前活跃」tag
  const v1Row = versionTable.locator("tbody tr", { hasText: "发布样例-v1.pdf" });
  const v2Row = versionTable.locator("tbody tr", { hasText: "发布样例-v2.pdf" });
  await expect(v1Row.getByTestId("kb-active-tag")).toBeVisible();
  await expect(v2Row.getByTestId("kb-active-tag")).toHaveCount(0);

  await drawer.getByTestId("kb-publish").click();
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
  await expect(page.getByText("v2 已发布")).toBeVisible();

  // 发布成功：tag 迁移到 v2（就地更新在 KB-05B 旧口径下也有——真正钉死服真化的是下方
  // 「重开抽屉 tag 仍在」半段，审查观察项的归属精确化）
  await expect(v2Row.getByTestId("kb-active-tag")).toBeVisible();
  await expect(v1Row.getByTestId("kb-active-tag")).toHaveCount(0);

  // 发布成功后刷新：回滚下拉排除活跃 v2，只列 v1（D2-A；KB-08B 起排除基准=服务端真值）
  await drawer.getByTestId("kb-rollback-trigger").click();
  const menu = page.getByTestId("kb-rollback-menu");
  await expect(menu).toBeVisible();
  await expect(menu).toContainText("v1 · 发布样例-v1.pdf");
  await expect(menu).not.toContainText("v2 · 发布样例-v2.pdf");
  await menu.getByTestId("kb-rollback-target").click();
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
  await expect(page.getByText("已回滚：v1 成为活跃发布")).toBeVisible();
  expect(mock.captures.versionActions).toHaveLength(2);

  // KB-08B：回滚后 tag 迁回 v1
  await expect(v1Row.getByTestId("kb-active-tag")).toBeVisible();
  await expect(v2Row.getByTestId("kb-active-tag")).toHaveCount(0);

  // 重开抽屉：旧口径会话记忆复位 → tag 消失并全列 PUBLISHED 吃 409 兜底；KB-08B 服真化后
  // 详情回包 active_version_id=v1 → tag 仍在、下拉仍排除活跃 v1。「选到活跃版本 → 409
  // KB_ROLLBACK_TARGET_IS_ACTIVE」降为竞态窗口可达（mock/全栈不复刻竞态，映射由 kb-logic
  // 单测承担——简报 §8）
  await page.locator(".el-drawer__close-btn").click();
  const reopened = await openDrawer(page, "发布样例");
  await expect(
    reopened
      .getByTestId("kb-version-table")
      .locator("tbody tr", { hasText: "发布样例-v1.pdf" })
      .getByTestId("kb-active-tag"),
  ).toBeVisible();
  await reopened.getByTestId("kb-rollback-trigger").click();
  const reopenedMenu = page.getByTestId("kb-rollback-menu");
  await expect(reopenedMenu).toContainText("v2 · 发布样例-v2.pdf");
  await expect(reopenedMenu).not.toContainText("v1 ·");
});

test("KB-08B 活跃标识初始化与重开持久：详情回包真值打标，重开抽屉不再丢失", async ({ page }) => {
  const doc = publishedDocument();
  await mockAuth(page, { authenticated: true, user: { permissions: PUBLISH_PERMS } });
  await mockKbApi(page, {
    documents: [doc],
    activePublishByDocument: { [doc.id]: "61000000-0000-0000-0000-0000000000e1" },
  });

  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "已发布样例");
  await expect(
    drawer
      .getByTestId("kb-version-table")
      .locator("tbody tr", { hasText: "已发布样例.pdf" })
      .getByTestId("kb-active-tag"),
  ).toBeVisible();

  // 重开抽屉：旧口径会话内记忆复位 → tag 消失；服真化后 tag 仍在（TD-305 前端半核心验收）
  await page.locator(".el-drawer__close-btn").click();
  const reopened = await openDrawer(page, "已发布样例");
  await expect(
    reopened
      .getByTestId("kb-version-table")
      .locator("tbody tr", { hasText: "已发布样例.pdf" })
      .getByTestId("kb-active-tag"),
  ).toBeVisible();
  // 唯一 PUBLISHED 即活跃自身 → 回滚下拉无可回滚目标（排除基准=服务端真值）
  await reopened.getByTestId("kb-rollback-trigger").click();
  await expect(page.getByTestId("kb-rollback-menu")).toContainText("无可回滚目标");
});

test("KB-08B 契约边界：列表回包不带 active_version_id（详情专属字段）", async ({ page }) => {
  const doc = publishedDocument();
  await mockAuth(page, { authenticated: true, user: { permissions: PUBLISH_PERMS } });
  await mockKbApi(page, {
    documents: [doc],
    activePublishByDocument: { [doc.id]: "61000000-0000-0000-0000-0000000000e1" },
  });

  const listResponse = page.waitForResponse(/\/school\/kb\/documents\?/);
  await page.goto("/kb/documents");
  const body = (await (await listResponse).json()) as { items: Record<string, unknown>[] };
  expect(body.items.length).toBeGreaterThan(0);
  expect(body.items.every((item) => !("active_version_id" in item))).toBe(true);
});

test("KB-05B 回滚门禁：文档无活跃发布（指针已撤）→ 409 KB_NO_ACTIVE_PUBLISH 可执行提示", async ({
  page,
}) => {
  const doc = publishedDocument();
  await mockAuth(page, { authenticated: true, user: { permissions: PUBLISH_PERMS } });
  // 不给 activePublishByDocument：PUBLISHED 版本但无 alias 行（真实世界的指针撤除边缘态）
  await mockKbApi(page, { documents: [doc] });

  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "已发布样例");
  await drawer.getByTestId("kb-rollback-trigger").click();
  await page.getByTestId("kb-rollback-menu").getByTestId("kb-rollback-target").click();
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
  await expect(page.getByText("该文档当前没有活跃发布，无法回滚")).toBeVisible();
  // 409 后状态不变（回滚失败不产生副作用；断言走 mock.state，见发布用例注记）
  await expect(drawer.getByTestId("kb-version-table")).toContainText("已发布");
});

test("KB-05B 下线：PUBLISHED 可下线，活跃发布撤除，终态后动作区消失", async ({ page }) => {
  const doc = publishedDocument();
  await mockAuth(page, { authenticated: true, user: { permissions: PUBLISH_PERMS } });
  const mock = await mockKbApi(page, {
    documents: [doc],
    activePublishByDocument: { [doc.id]: "61000000-0000-0000-0000-0000000000e1" },
  });

  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "已发布样例");
  // KB-08B：活跃发布在位 → v1 行挂「当前活跃」tag（下线前对照）
  await expect(
    drawer
      .getByTestId("kb-version-table")
      .locator("tbody tr", { hasText: "已发布样例.pdf" })
      .getByTestId("kb-active-tag"),
  ).toBeVisible();
  await drawer.getByTestId("kb-offline").click();
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
  await expect(page.getByText("v1 已下线，活跃发布已撤除")).toBeVisible();

  const state = mock.state.documents[0];
  expect(state?.status).toBe("OFFLINE");
  expect(state?.versions?.[0]?.status).toBe("OFFLINE");
  expect(mock.captures.versionActions).toEqual([
    { method: "POST", path: expect.stringContaining("/offline") },
  ]);
  // OFFLINE 是终态：动作区（发布/回滚/下线/删除）整体消失
  await expect(drawer.getByTestId("kb-publish-row")).toHaveCount(0);
  // 版本表状态列变为已下线
  await expect(drawer.getByTestId("kb-version-table")).toContainText("已下线");
  // KB-08B：活跃发布撤除（was_active 就地置 null + 重读详情校准）→ tag 消失
  await expect(
    drawer
      .getByTestId("kb-version-table")
      .locator("tbody tr", { hasText: "已发布样例.pdf" })
      .getByTestId("kb-active-tag"),
  ).toHaveCount(0);
});

test("KB-05B 删除：PUBLISHED 可软删，确认按钮为「删除」，状态列变已删除", async ({ page }) => {
  const doc = publishedDocument();
  await mockAuth(page, { authenticated: true, user: { permissions: PUBLISH_PERMS } });
  const mock = await mockKbApi(page, {
    documents: [doc],
    activePublishByDocument: { [doc.id]: "61000000-0000-0000-0000-0000000000e1" },
  });

  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "已发布样例");
  await drawer.getByTestId("kb-delete").click();
  // 删除确认框的执行按钮是「删除」（D5-A 一次确认），不是默认 OK
  await page.locator(".el-message-box").getByRole("button", { name: "删除" }).click();
  await expect(page.getByText("v1 已删除，活跃发布已撤除")).toBeVisible();

  const state = mock.state.documents[0];
  expect(state?.status).toBe("DELETED");
  expect(state?.versions?.[0]?.status).toBe("DELETED");
  expect(mock.captures.versionActions).toEqual([
    { method: "DELETE", path: expect.not.stringContaining("/publish") },
  ]);
  await expect(drawer.getByTestId("kb-publish-row")).toHaveCount(0);
  await expect(drawer.getByTestId("kb-version-table")).toContainText("已删除");
});

test("KB-05B 权限分授：有 publish 无 delete 只见下线/回滚；有 delete 无 publish 只见删除", async ({
  page,
}) => {
  const docA = publishedDocument();
  await mockAuth(page, {
    authenticated: true,
    user: { permissions: [...BOTH, "school.kb.publish"] },
  });
  await mockKbApi(page, {
    documents: [docA],
    activePublishByDocument: { [docA.id]: "61000000-0000-0000-0000-0000000000e1" },
  });
  await page.goto("/kb/documents");
  const drawerA = await openDrawer(page, "已发布样例");
  await expect(drawerA.getByTestId("kb-offline")).toBeVisible();
  await expect(drawerA.getByTestId("kb-rollback")).toBeVisible();
  await expect(drawerA.getByTestId("kb-delete")).toHaveCount(0);
  await page.locator(".el-drawer__close-btn").click();

  // 第二种权限组合用独立页面（auth mock 按 page 注册，互不污染）
  const page2 = await page.context().newPage();
  const docB = publishedDocument();
  await mockAuth(page2, {
    authenticated: true,
    user: { permissions: [...BOTH, "school.kb.delete"] },
  });
  await mockKbApi(page2, {
    documents: [docB],
    activePublishByDocument: { [docB.id]: "61000000-0000-0000-0000-0000000000e1" },
  });
  await page2.goto("/kb/documents");
  const drawerB = await openDrawer(page2, "已发布样例");
  await expect(drawerB.getByTestId("kb-delete")).toBeVisible();
  await expect(drawerB.getByTestId("kb-offline")).toHaveCount(0);
  await expect(drawerB.getByTestId("kb-rollback")).toHaveCount(0);
  await page2.close();
});

// ---- M3 KB-06B：分块预览（接口 mock；判据手抄 chunk_read.py，锚点见 kb-mock.ts）----

const CHUNK_PERMS = [...BOTH, "school.kb.view_chunks"];

const CHUNK_VERSION_ID = "61000000-0000-0000-0000-0000000000c1";
const CHUNK_PARENT_ID = "62000000-0000-0000-0000-0000000000c1";

/** 分块样例文档（READY）：一父两子（第一章）+ 一个表格父块（第二章，未被拆坏样例）。 */
function chunkedDocument(): KbDocumentFixture {
  return kbDocument({
    id: "60000000-0000-0000-0000-0000000000c1",
    title: "分块样例",
    status: "READY",
    current_version: kbVersion({
      id: CHUNK_VERSION_ID,
      version_no: 1,
      filename: "分块样例.pdf",
      status: "READY",
    }),
    versions: [
      kbVersion({
        id: CHUNK_VERSION_ID,
        version_no: 1,
        filename: "分块样例.pdf",
        status: "READY",
      }),
    ],
  });
}

function chunkFixtures(): KbChunkFixture[] {
  return [
    kbChunk({
      id: CHUNK_PARENT_ID,
      chunk_type: "parent",
      chunk_index: 0,
      parent_id: null,
      text: "第一章 全文拼接 含配置项",
      text_markdown: "第一章 全文拼接 含配置项",
      section_path: "手册 > 第一章 安装",
      token_count: 30,
      char_count: 60,
      embedding_status: "pending",
    }),
    kbChunk({
      id: "62000000-0000-0000-0000-0000000000c2",
      chunk_index: 1,
      parent_id: CHUNK_PARENT_ID,
      text: "安装步骤1完成。",
      text_markdown: "安装步骤1完成。",
      section_path: "手册 > 第一章 安装",
      char_count: 10,
    }),
    kbChunk({
      id: "62000000-0000-0000-0000-0000000000c3",
      chunk_index: 2,
      parent_id: CHUNK_PARENT_ID,
      text: "安装步骤2完成。",
      text_markdown: "安装步骤2完成。",
      section_path: "手册 > 第一章 安装",
      char_count: 10,
    }),
    kbChunk({
      id: "62000000-0000-0000-0000-0000000000c4",
      chunk_index: 3,
      parent_id: null,
      chunk_type: "parent",
      text: "| 参数 | 默认值 |\n| --- | --- |\n| 超时 | 30 |",
      text_markdown: "| 参数 | 默认值 |\n| --- | --- |\n| 超时 | 30 |",
      section_path: "手册 > 第二章 配置",
      embedding_status: "failed",
    }),
  ];
}

async function openChunksView(page: Page, drawer: ReturnType<Page["getByTestId"]>): Promise<void> {
  await drawer.getByTestId("kb-chunks-open").first().click();
  await expect(drawer.getByTestId("kb-chunks-view")).toBeVisible();
}

function chunkRows(drawer: ReturnType<Page["getByTestId"]>): ReturnType<Page["locator"]> {
  return drawer.getByTestId("kb-chunks-table").locator("tbody tr");
}

test("KB-06B 分块：版本行入口进子视图，列表/统计/类型标签渲染", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: CHUNK_PERMS } });
  const mock = await mockKbApi(page, {
    documents: [chunkedDocument()],
    chunksByVersion: { [CHUNK_VERSION_ID]: chunkFixtures() },
  });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "分块样例");
  await openChunksView(page, drawer);

  await expect(drawer.getByTestId("kb-chunks-back")).toContainText("返回文档");
  await expect(drawer.getByTestId("kb-chunks-view")).toContainText("v1 分块预览");
  // 统计摘要（per-version 固定口径；mock 聚合锚定 chunk_read.py chunk_stats）
  await expect(drawer.getByTestId("kb-chunks-stats")).toContainText("共 4 块（父 2 / 子 2）");
  await expect(drawer.getByTestId("kb-chunks-stats")).toContainText("嵌入 待1 成2 败1");
  await expect(chunkRows(drawer)).toHaveCount(4);
  // 请求级断言：列表 + stats 两次 GET（走 versions 路由捕获）
  const chunkGets = mock.captures.versionActions.filter(
    (action) => action.method === "GET" && action.path.includes("/chunks"),
  );
  expect(chunkGets).toHaveLength(2);
});

test("KB-06B 搜索：只返回匹配块；纯空白 q 等价未提供（normalize_q 口径）", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: CHUNK_PERMS } });
  await mockKbApi(page, {
    documents: [chunkedDocument()],
    chunksByVersion: { [CHUNK_VERSION_ID]: chunkFixtures() },
  });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "分块样例");
  await openChunksView(page, drawer);

  await drawer.getByTestId("kb-chunks-q").fill("配置项");
  await drawer.getByTestId("kb-chunks-search").click();
  // 表格不渲染块文本列：命中块（#0 父块）以「章节列 + 行数」断言（文本保真在详情用例验）
  await expect(chunkRows(drawer)).toHaveCount(1);
  await expect(chunkRows(drawer)).toContainText("手册 > 第一章 安装");

  await drawer.getByTestId("kb-chunks-q").fill("   ");
  await drawer.getByTestId("kb-chunks-search").click();
  await expect(chunkRows(drawer)).toHaveCount(4);
});

test("KB-06B 筛选：章节精确匹配 + 类型筛选；筛选变化回到第一页", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: CHUNK_PERMS } });
  await mockKbApi(page, {
    documents: [chunkedDocument()],
    chunksByVersion: { [CHUNK_VERSION_ID]: chunkFixtures() },
  });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "分块样例");
  await openChunksView(page, drawer);

  await drawer.getByTestId("kb-chunks-section").click();
  await page.locator(".el-select-dropdown__item", { hasText: "第二章" }).click();
  await expect(chunkRows(drawer)).toHaveCount(1);
  await expect(chunkRows(drawer)).toContainText("手册 > 第二章 配置");

  await drawer.getByTestId("kb-chunks-type").click();
  await page.locator(".el-select-dropdown__item", { hasText: "父块" }).click();
  // 章节 + 类型组合：第二章的父块（该节唯一的块就是表格父块）
  await expect(chunkRows(drawer)).toHaveCount(1);
  await expect(chunkRows(drawer)).toContainText("父块");

  await drawer.getByTestId("kb-chunks-section").click();
  await page.locator(".el-select-dropdown__item", { hasText: "第一章" }).first().click();
  // 类型仍为父块：第一章 ∩ 父块 = 1 行（组合筛选语义，回第一页）
  await expect(chunkRows(drawer)).toHaveCount(1);
  await expect(chunkRows(drawer)).toContainText("父块");
});

test("KB-06B 详情：text_markdown 纯文本保真（表格竖线原样）+ 父/子导航 + 只看子块", async ({
  page,
}) => {
  await mockAuth(page, { authenticated: true, user: { permissions: CHUNK_PERMS } });
  await mockKbApi(page, {
    documents: [chunkedDocument()],
    chunksByVersion: { [CHUNK_VERSION_ID]: chunkFixtures() },
  });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "分块样例");
  await openChunksView(page, drawer);

  // 表格父块（第二章唯一行）详情：markdown 源码原样呈现（未被拆坏 + 无二次加工）
  await chunkRows(drawer)
    .filter({ hasText: "手册 > 第二章 配置" })
    .getByTestId("kb-chunk-detail-open")
    .click();
  const detail = drawer.getByTestId("kb-chunk-detail");
  await expect(detail).toBeVisible();
  const chunkText = drawer.getByTestId("kb-chunk-text");
  await expect(chunkText).toContainText("| 参数 | 默认值 |");
  await expect(chunkText).toContainText("| 超时 | 30 |");
  // 父块无 parent 链接、无子块（独立组）
  await expect(detail.getByTestId("kb-chunk-parent-link")).toHaveCount(0);
  await expect(detail.getByTestId("kb-chunk-child-link")).toHaveCount(0);

  // 回列表 → 父块 #0（首行，按 chunk_index 升序）详情 → 子块导航 + 只看该块的子块
  await drawer.getByTestId("kb-chunk-detail-close").click();
  await chunkRows(drawer).first().getByTestId("kb-chunk-detail-open").click();
  await expect(detail.getByTestId("kb-chunk-child-link")).toHaveCount(2);
  await detail.getByTestId("kb-chunk-child-link").first().click();
  await expect(detail).toContainText("安装步骤1完成。");
  await expect(detail.getByTestId("kb-chunk-parent-link")).toContainText("#0");

  // 经父块链接回父块（子块 children 为空，「只看该块的子块」按钮不渲染）
  await detail.getByTestId("kb-chunk-parent-link").click();
  await expect(detail.getByTestId("kb-chunk-child-link")).toHaveCount(2);
  await detail.getByTestId("kb-chunk-children-filter").click();
  await expect(drawer.getByTestId("kb-chunks-parent-filter")).toContainText("只看 #0 的子块");
  await expect(chunkRows(drawer)).toHaveCount(2);
});

test("KB-06B 分页：第一页 20 行 + 加载更多，到底后按钮消失", async ({ page }) => {
  const manyChunks: KbChunkFixture[] = [
    kbChunk({ id: CHUNK_PARENT_ID, chunk_index: 0, chunk_type: "parent" }),
  ];
  for (let index = 1; index < 25; index += 1) {
    manyChunks.push(
      kbChunk({
        id: `62000000-0000-0000-0000-${String(index).padStart(12, "0")}`,
        chunk_index: index,
        parent_id: CHUNK_PARENT_ID,
        text: `第${index}块`,
      }),
    );
  }
  await mockAuth(page, { authenticated: true, user: { permissions: CHUNK_PERMS } });
  await mockKbApi(page, {
    documents: [chunkedDocument()],
    chunksByVersion: { [CHUNK_VERSION_ID]: manyChunks },
  });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "分块样例");
  await openChunksView(page, drawer);

  await expect(chunkRows(drawer)).toHaveCount(20);
  await drawer.getByTestId("kb-chunks-more").click();
  await expect(chunkRows(drawer)).toHaveCount(25);
  await expect(drawer.getByTestId("kb-chunks-more")).toHaveCount(0);
  await expect(drawer.getByTestId("kb-chunks-stats")).toContainText("共 25 块");
});

test("KB-06B 权限：无 view_chunks 码不给分块入口列，子视图不可达", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: BOTH } });
  await mockKbApi(page, {
    documents: [chunkedDocument()],
    chunksByVersion: { [CHUNK_VERSION_ID]: chunkFixtures() },
  });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "分块样例");
  await expect(drawer.getByTestId("kb-chunks-open")).toHaveCount(0);
  await expect(drawer.getByTestId("kb-chunks-view")).toHaveCount(0);
});

test("KB-06B 空集：筛选无命中显示空态；切文档/重开抽屉子视图复位", async ({ page }) => {
  const docA = chunkedDocument();
  const docB = publishedDocument();
  await mockAuth(page, { authenticated: true, user: { permissions: CHUNK_PERMS } });
  await mockKbApi(page, {
    documents: [docA, docB],
    chunksByVersion: { [CHUNK_VERSION_ID]: chunkFixtures() },
  });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "分块样例");
  await openChunksView(page, drawer);

  await drawer.getByTestId("kb-chunks-q").fill("不存在的词");
  await drawer.getByTestId("kb-chunks-search").click();
  await expect(drawer.getByTestId("kb-chunks-empty")).toBeVisible();

  // 回文档 → 打开另一文档 → 子视图不残留
  await drawer.getByTestId("kb-chunks-back").click();
  await expect(drawer.getByTestId("kb-chunks-view")).toHaveCount(0);
  await drawer.getByTestId("kb-chunks-open").first().click();
  await expect(drawer.getByTestId("kb-chunks-view")).toBeVisible();
  await drawer.getByTestId("kb-chunks-back").click();
  await page.keyboard.press("Escape");
  await page
    .locator(".el-table__row", { hasText: "已发布样例" })
    .getByTestId("kb-detail-open")
    .click();
  await expect(page.getByTestId("kb-document-drawer")).toBeVisible();
  await expect(page.getByTestId("kb-chunks-view")).toHaveCount(0);
});

// ---- M3 KB-07B：检索测试（接口 mock；判据手抄 retrieval_test.py，锚点见 kb-mock.ts）----

function retrievalRows(drawer: ReturnType<Page["getByTestId"]>): ReturnType<Page["locator"]> {
  return drawer.getByTestId("kb-retrieval-table").locator("tbody tr");
}

/** 打开抽屉 + 分块子视图，等待检索测试区就绪（空态提示可见）。 */
async function openRetrievalReady(page: Page): Promise<{
  drawer: ReturnType<Page["getByTestId"]>;
  mock: Awaited<ReturnType<typeof mockKbApi>>;
}> {
  await mockAuth(page, { authenticated: true, user: { permissions: CHUNK_PERMS } });
  const mock = await mockKbApi(page, {
    documents: [chunkedDocument()],
    chunksByVersion: { [CHUNK_VERSION_ID]: chunkFixtures() },
  });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "分块样例");
  await openChunksView(page, drawer);
  await expect(drawer.getByTestId("kb-retrieval-empty")).toBeVisible();
  return { drawer, mock };
}

test("KB-07B 检索测试：默认快查渲染结果表（双路分数+名次、RRF、最终引用、状态行）", async ({
  page,
}) => {
  const { drawer } = await openRetrievalReady(page);
  await drawer.getByTestId("kb-retrieval-q").fill("安装步骤1");
  await drawer.getByTestId("kb-retrieval-run").click();

  await expect(drawer.getByTestId("kb-retrieval-table")).toBeVisible();
  await expect(drawer.getByTestId("kb-retrieval-meta")).toContainText("未启用重排 · 4 行 · 1.2ms");
  await expect(retrievalRows(drawer)).toHaveCount(4);
  // 命中序：query bigram 与夹具包含计数确定——「安装步骤1完成。」全命中第 1
  const firstRow = retrievalRows(drawer).first();
  await expect(firstRow).toContainText("安装步骤1完成。");
  await expect(firstRow).toContainText("#1");
  await expect(firstRow).toContainText("1.0000 · #1");
  // 最终引用：top_k=10 ≥ 行数 → 每行都有标记（4 个 tag；表头列名不算，scope 到 tbody）
  await expect(
    drawer.getByTestId("kb-retrieval-table").locator("tbody").getByText("最终引用"),
  ).toHaveCount(4);
  // 未启用重排：rerank 列全「—」
  await expect(drawer.getByTestId("kb-retrieval-table")).toContainText("—");
});

test("KB-07B 查看分块：结果行打开详情子面板（最终引用定位到分块闭环）", async ({ page }) => {
  const { drawer } = await openRetrievalReady(page);
  await drawer.getByTestId("kb-retrieval-q").fill("安装步骤1");
  await drawer.getByTestId("kb-retrieval-run").click();
  await expect(retrievalRows(drawer).first()).toContainText("安装步骤1完成。");

  await retrievalRows(drawer).first().getByTestId("kb-retrieval-open-chunk").click();
  await expect(drawer.getByTestId("kb-chunk-detail")).toBeVisible();
  await expect(drawer.getByTestId("kb-chunk-detail")).toContainText("#1");
  await expect(drawer.getByTestId("kb-chunk-detail")).toContainText("安装步骤1完成。");
});

test("KB-07B rerank 开关：状态行「已重排」+ 分数列有值 + 顺序翻转（路由名次保持融合序）", async ({
  page,
}) => {
  const { drawer } = await openRetrievalReady(page);
  await drawer.getByTestId("kb-retrieval-q").fill("安装步骤");
  // 后端语义：rerank 池 = max(config 20, top_k) 与索引内块数取小 → 4 行（不是 top_k 裁剪）；
  // fake 重排按融合位置给升序分数 → 顺序整体翻转，路由名次保持融合序
  await drawer.getByTestId("kb-retrieval-rerank").click();
  await drawer.getByTestId("kb-retrieval-run").click();

  await expect(drawer.getByTestId("kb-retrieval-meta")).toContainText("已重排 · 4 行 · 1.2ms");
  await expect(retrievalRows(drawer)).toHaveCount(4);
  // 翻转：融合末位（0 命中、index 3 的表格块）重排到 final_rank 1，其路由名次仍是 #4
  const firstRow = retrievalRows(drawer).first();
  await expect(firstRow).toContainText("| 超时 | 30 |");
  await expect(firstRow).toContainText("0.4000 · #4");
  await expect(firstRow).toContainText("1.0000");
  const secondRow = retrievalRows(drawer).nth(1);
  await expect(secondRow).toContainText("第一章 全文拼接 含配置项");
  await expect(secondRow).toContainText("0.6000 · #3");
  await expect(secondRow).toContainText("0.7500");
  // 最终引用 = final_rank ≤ top_k(10) → 4 行全标（scope tbody，表头列名不算）
  await expect(
    drawer.getByTestId("kb-retrieval-table").locator("tbody").getByText("最终引用"),
  ).toHaveCount(4);
});

test("KB-07B top_k 输入：裁剪结果行数（pool_size 语义）", async ({ page }) => {
  const { drawer } = await openRetrievalReady(page);
  await drawer.getByTestId("kb-retrieval-q").fill("安装步骤");
  const topkInput = drawer.getByTestId("kb-retrieval-topk").locator("input");
  await topkInput.fill("2");
  await drawer.getByTestId("kb-retrieval-run").click();

  await expect(drawer.getByTestId("kb-retrieval-meta")).toContainText("未启用重排 · 2 行 · 1.2ms");
  await expect(retrievalRows(drawer)).toHaveCount(2);
  // 最终引用 = final_rank ≤ top_k → 2 行全标（scope tbody，表头列名不算）
  await expect(
    drawer.getByTestId("kb-retrieval-table").locator("tbody").getByText("最终引用"),
  ).toHaveCount(2);
});

test("KB-07B 空 query 前端拦：内联错误且不发请求", async ({ page }) => {
  const { drawer, mock } = await openRetrievalReady(page);
  await drawer.getByTestId("kb-retrieval-run").click();

  await expect(drawer.getByTestId("kb-retrieval-error")).toContainText("请输入检索词");
  await expect(drawer.getByTestId("kb-retrieval-table")).toHaveCount(0);
  const retrievalPosts = mock.captures.versionActions.filter(
    (action) => action.method === "POST" && action.path.includes("/retrieval-test"),
  );
  expect(retrievalPosts).toHaveLength(0);
});

test("KB-07B 超长 query 前端拦（码点度量，m-1 教训）：内联错误且不发请求", async ({ page }) => {
  const { drawer, mock } = await openRetrievalReady(page);
  // 301 个非 BMP 字符 = 301 码点（602 UTF-16 单元）——后端码点口径下超限
  await drawer.getByTestId("kb-retrieval-q").fill("👍".repeat(301));
  await drawer.getByTestId("kb-retrieval-run").click();

  await expect(drawer.getByTestId("kb-retrieval-error")).toContainText("检索词不能超过 300 个字符");
  const retrievalPosts = mock.captures.versionActions.filter(
    (action) => action.method === "POST" && action.path.includes("/retrieval-test"),
  );
  expect(retrievalPosts).toHaveLength(0);
});

test("KB-07B 在途检索时改筛选：loading 收敛（运行序号）、迟响应被丢弃（审查 M1 回归）", async ({
  page,
}) => {
  await mockAuth(page, { authenticated: true, user: { permissions: CHUNK_PERMS } });
  await mockKbApi(page, {
    documents: [chunkedDocument()],
    chunksByVersion: { [CHUNK_VERSION_ID]: chunkFixtures() },
    retrievalDelayMs: 400,
  });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "分块样例");
  await openChunksView(page, drawer);
  await expect(drawer.getByTestId("kb-retrieval-empty")).toBeVisible();

  await drawer.getByTestId("kb-retrieval-q").fill("安装步骤1");
  await drawer.getByTestId("kb-retrieval-run").click();
  // 在途期间改分块筛选（applyChunkFilters bump chunksGeneration）——迟响应必须被丢弃
  await drawer.getByTestId("kb-chunks-q").fill("配置项");
  await drawer.getByTestId("kb-chunks-search").click();
  await expect(chunkRows(drawer)).toHaveCount(1);

  // 迟到响应到达后：loading 已收敛（按钮可再点，审查 M1 的卡死场景），结果被丢弃（不渲染表）
  await expect(drawer.getByTestId("kb-retrieval-table")).toHaveCount(0);
  const runButton = drawer.getByTestId("kb-retrieval-run");
  await expect(runButton).toBeEnabled();
  await runButton.click();
  await expect(drawer.getByTestId("kb-retrieval-table")).toBeVisible();
  await expect(retrievalRows(drawer)).toHaveCount(4);
});

// ---------------------------------------------------------------------------
// KB-09C：人群配置子视图（卡 C1-A/C2-A）与检索测试视角（卡 C3-A）
// ---------------------------------------------------------------------------

const ACL_PERMS = [...BOTH, "school.kb.acl.manage"];
const ACL_VERSION_ID = "61000000-0000-0000-0000-0000000000e1";

function aclDocument(): KbDocumentFixture {
  return kbDocument({
    id: "60000000-0000-0000-0000-0000000000e0",
    title: "人群样例",
    status: "TEST_PASSED",
    current_version: kbVersion({
      id: ACL_VERSION_ID,
      version_no: 1,
      filename: "人群样例.pdf",
      status: "TEST_PASSED",
    }),
    versions: [
      kbVersion({
        id: ACL_VERSION_ID,
        version_no: 1,
        filename: "人群样例.pdf",
        status: "TEST_PASSED",
      }),
    ],
  });
}

async function openAclView(page: Page, drawer: ReturnType<Page["getByTestId"]>): Promise<void> {
  await drawer.getByTestId("kb-acl-open").first().click();
  await expect(drawer.getByTestId("kb-acl-view")).toBeVisible();
}

test("KB-09C view 持有人：人群列出现，子视图默认 all 已确认，无编辑/确认按钮", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: VIEW_ONLY } });
  await mockKbApi(page, { documents: [aclDocument()] });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "人群样例");
  await openAclView(page, drawer);

  await expect(drawer.getByTestId("kb-acl-confirmed-tag")).toContainText("已确认");
  await expect(drawer.getByTestId("kb-acl-table").locator("tbody tr")).toHaveCount(1);
  await expect(drawer.getByTestId("kb-acl-table")).toContainText("全体");
  await expect(drawer.getByTestId("kb-acl-banner")).toHaveCount(0);
  await expect(drawer.getByTestId("kb-acl-edit")).toHaveCount(0);
  await expect(drawer.getByTestId("kb-acl-confirm")).toHaveCount(0);
});

test("KB-09C 未确认：警示横幅出现，确认成功后 tag 变已确认、横幅消失", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: ACL_PERMS } });
  const mock = await mockKbApi(page, {
    documents: [aclDocument()],
    audiencesByVersion: {
      [ACL_VERSION_ID]: {
        audiences: [{ audience_type: "department", audience_id: "d-1" }],
        acl_confirmed: false,
      },
    },
  });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "人群样例");
  await openAclView(page, drawer);

  await expect(drawer.getByTestId("kb-acl-confirmed-tag")).toContainText("未确认");
  await expect(drawer.getByTestId("kb-acl-banner")).toContainText("未确认");
  await drawer.getByTestId("kb-acl-confirm").click();

  await expect(drawer.getByTestId("kb-acl-confirmed-tag")).toContainText("已确认");
  await expect(drawer.getByTestId("kb-acl-banner")).toHaveCount(0);
  expect(mock.state.audiencesByVersion[ACL_VERSION_ID]?.acl_confirmed).toBe(true);
});

test("KB-09C 编辑保存：PUT 全量替换 + 警告「置为未确认」+ state 落库", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: ACL_PERMS } });
  const mock = await mockKbApi(page, { documents: [aclDocument()] });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "人群样例");
  await openAclView(page, drawer);

  await drawer.getByTestId("kb-acl-edit").click();
  // el-dialog teleport 到 body——对话框内控件用 page 级定位（drawer 子树不含它）。
  // 初始夹具是 [all]：直接把第一行类型改为部门并填 id（复用行，不新增）。
  await expect(page.getByTestId("kb-acl-dialog")).toBeVisible();
  await page
    .getByTestId("kb-acl-edit-table")
    .locator("tbody tr")
    .first()
    .getByTestId("kb-acl-row-type")
    .getByText("部门", { exact: true })
    .click();
  await page
    .getByTestId("kb-acl-edit-table")
    .locator("tbody tr")
    .first()
    .getByTestId("kb-acl-row-id")
    .fill("d-1");
  await page.getByTestId("kb-acl-save").click();

  await expect(drawer.getByTestId("kb-acl-confirmed-tag")).toContainText("未确认");
  await expect(drawer.getByTestId("kb-acl-banner")).toContainText("未确认");
  const state = mock.state.audiencesByVersion[ACL_VERSION_ID];
  expect(state?.audiences).toEqual([{ audience_type: "department", audience_id: "d-1" }]);
  expect(state?.acl_confirmed).toBe(false);
  expect(mock.captures.aclActions.some((item) => item.method === "PUT")).toBe(true);
});

test("KB-09C 编辑预检：all 与部门混配 → 前端拦下错误文案，保存禁用", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: ACL_PERMS } });
  const mock = await mockKbApi(page, { documents: [aclDocument()] });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "人群样例");
  await openAclView(page, drawer);

  await drawer.getByTestId("kb-acl-edit").click();
  await expect(page.getByTestId("kb-acl-dialog")).toBeVisible();
  // 初始夹具已是 [all]：直接新增一行 department 并填 id → [all, department] 混配
  await page.getByTestId("kb-acl-row-add").click();
  await page
    .getByTestId("kb-acl-edit-table")
    .locator("tbody tr")
    .nth(1)
    .getByTestId("kb-acl-row-id")
    .fill("d-1");

  await expect(page.getByTestId("kb-acl-edit-error")).toContainText("不得与其他人群类型混配");
  await expect(page.getByTestId("kb-acl-save")).toBeDisabled();
  expect(mock.captures.aclActions.filter((item) => item.method === "PUT")).toHaveLength(0);
});

test("KB-09C 清空保存：空表提示 + confirm 吃 409 ACL_REQUIRED", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: ACL_PERMS } });
  const mock = await mockKbApi(page, { documents: [aclDocument()] });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "人群样例");
  await openAclView(page, drawer);

  await drawer.getByTestId("kb-acl-edit").click();
  await expect(page.getByTestId("kb-acl-dialog")).toBeVisible();
  await page
    .getByTestId("kb-acl-edit-table")
    .locator("tbody tr")
    .first()
    .getByTestId("kb-acl-row-remove")
    .click();
  await page.getByTestId("kb-acl-save").click();
  await expect(drawer.getByTestId("kb-acl-empty")).toContainText("未配置人群");

  await drawer.getByTestId("kb-acl-confirm").click();
  await expect(page.getByText("人群 ACL 未配置")).toBeVisible();
  const state = mock.state.audiencesByVersion[ACL_VERSION_ID];
  expect(state?.audiences).toHaveLength(0);
  expect(state?.acl_confirmed).toBe(false);
});

test("KB-09C 服务端唯一门禁：PUT 422 → 错误文案回填对话框且不关闭（审查 m3）", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: ACL_PERMS } });
  await mockKbApi(page, {
    documents: [aclDocument()],
    aclPutError: {
      status: 422,
      body: {
        detail: { code: "AUDIENCE_ALL_MIXED", message: "all（显式全体）不得与其他人群类型混配" },
      },
    },
  });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "人群样例");
  await openAclView(page, drawer);

  await drawer.getByTestId("kb-acl-edit").click();
  await expect(page.getByTestId("kb-acl-dialog")).toBeVisible();
  await page.getByTestId("kb-acl-save").click();

  await expect(page.getByTestId("kb-acl-edit-error")).toContainText("不得与其他人群类型混配");
  await expect(page.getByTestId("kb-acl-dialog")).toBeVisible();
});

test("KB-09C 检索测试：模拟人群 token 不匹配 → 零行 + 视角 token 回显", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: CHUNK_PERMS } });
  await mockKbApi(page, {
    documents: [chunkedDocument()],
    chunksByVersion: { [CHUNK_VERSION_ID]: chunkFixtures() },
    audiencesByVersion: {
      [CHUNK_VERSION_ID]: {
        audiences: [{ audience_type: "department", audience_id: "d-1" }],
        acl_confirmed: true,
      },
    },
  });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "分块样例");
  await openChunksView(page, drawer);
  await drawer.getByTestId("kb-retrieval-as-audiences").fill("department:other-1");
  await drawer.getByTestId("kb-retrieval-q").fill("安装步骤1");
  await drawer.getByTestId("kb-retrieval-run").click();

  await expect(drawer.getByTestId("kb-retrieval-meta")).toContainText("视角 token");
  await expect(drawer.getByTestId("kb-retrieval-resolved-tokens")).toContainText(
    "department:other-1",
  );
  // 不匹配 → 零召回（有结果容器 0 行 + No Data，不渲染 empty——empty 仅「未运行」时出现）
  await expect(drawer.getByTestId("kb-retrieval-meta")).toContainText("0 行");
  await expect(retrievalRows(drawer)).toHaveCount(0);
  await expect(drawer.getByTestId("kb-retrieval-table")).toContainText("No Data");
});

test("KB-09C ignore_acl：无码开关不渲染；有码开启后未过滤视图恢复行数且命中 token 为空", async ({
  page,
}) => {
  await mockAuth(page, { authenticated: true, user: { permissions: CHUNK_PERMS } });
  await mockKbApi(page, {
    documents: [chunkedDocument()],
    chunksByVersion: { [CHUNK_VERSION_ID]: chunkFixtures() },
    audiencesByVersion: {
      [CHUNK_VERSION_ID]: {
        audiences: [{ audience_type: "department", audience_id: "d-1" }],
        acl_confirmed: true,
      },
    },
  });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "分块样例");
  await openChunksView(page, drawer);
  // 无 unfiltered 码：开关与提示均不渲染
  await expect(drawer.getByTestId("kb-retrieval-ignore-acl")).toHaveCount(0);
  await drawer.getByTestId("kb-retrieval-q").fill("安装步骤1");
  await drawer.getByTestId("kb-retrieval-run").click();
  // 部门文档 × 操作者 token（all）→ 零召回（结果容器 0 行 + No Data）
  await expect(drawer.getByTestId("kb-retrieval-meta")).toContainText("0 行");
  await expect(drawer.getByTestId("kb-retrieval-table")).toContainText("No Data");

  await mockAuth(page, {
    authenticated: true,
    user: { permissions: [...CHUNK_PERMS, "school.kb.retrieval_test.unfiltered"] },
  });
  await page.reload();
  const drawer2 = await openDrawer(page, "分块样例");
  await openChunksView(page, drawer2);
  await expect(drawer2.getByTestId("kb-retrieval-ignore-acl")).toBeVisible();
  await drawer2.getByTestId("kb-retrieval-ignore-acl").click();
  await drawer2.getByTestId("kb-retrieval-q").fill("安装步骤1");
  await drawer2.getByTestId("kb-retrieval-run").click();

  await expect(drawer2.getByTestId("kb-retrieval-unfiltered")).toContainText("未过滤视图");
  await expect(retrievalRows(drawer2)).toHaveCount(4);
  // 未过滤视图：行仍带 payload token 交集——操作者 token 只含 all，部门文档无交集 → 全「—」
  await expect(drawer2.getByTestId("kb-retrieval-table")).toContainText("—");
});

// ---------------------------------------------------------------------------
// KB-10B：切块编辑与派生重建（卡 E1-E6；chunk.edit/chunk.confirm 码 + 超管 role 双判；
// 服务端唯一门禁——mock 镜像乐观锁/部分唯一/已派生 409 语义，422 族由前端预检拦）。
// ---------------------------------------------------------------------------

const KB10B_VERSION_ID = "61000000-0000-0000-0000-0000000000f1";
const KB10B_EDIT_PERMS = [...BOTH, "school.kb.view_chunks", "school.kb.chunk.edit"];
const KB10B_CONFIRM_PERMS = [...KB10B_EDIT_PERMS, "school.kb.chunk.confirm"];
const KB10B_CHILD_ID = "62000000-0000-0000-0000-0000000000f1";

const chunkDocChild = kbChunk({
  id: KB10B_CHILD_ID,
  chunk_type: "child",
  chunk_index: 1,
  parent_id: "62000000-0000-0000-0000-0000000000f0",
  text: "安装步骤说明，需要修正错别字。",
  text_markdown: "安装步骤说明，需要修正错别字。",
  char_count: 15,
  is_human_edited: false,
  edited_by: null,
  edited_at: null,
  chunk_hash: "b".repeat(64),
});
const chunkDocParent = kbChunk({
  id: "62000000-0000-0000-0000-0000000000f0",
  chunk_type: "parent",
  chunk_index: 0,
  parent_id: null,
  text: "安装步骤说明，需要修正错别字。",
  text_markdown: "安装步骤说明，需要修正错别字。",
  char_count: 15,
  is_human_edited: false,
  edited_by: null,
  edited_at: null,
  chunk_hash: "a".repeat(64),
});

function chunkEditDocument(): KbDocumentFixture {
  return kbDocument({
    id: "60000000-0000-0000-0000-0000000000f0",
    title: "编辑样例",
    status: "TEST_PASSED",
    current_version: kbVersion({
      id: KB10B_VERSION_ID,
      version_no: 1,
      filename: "编辑样例.pdf",
      status: "TEST_PASSED",
    }),
    versions: [
      kbVersion({
        id: KB10B_VERSION_ID,
        version_no: 1,
        filename: "编辑样例.pdf",
        status: "TEST_PASSED",
      }),
    ],
  });
}

async function openChunkView(
  page: Page,
  perms: string[],
  roles?: string[],
): Promise<{
  drawer: ReturnType<Page["getByTestId"]>;
  mock: Awaited<ReturnType<typeof mockKbApi>>;
}> {
  await mockAuth(page, {
    authenticated: true,
    user: { permissions: perms, ...(roles === undefined ? {} : { roles }) },
  });
  const mock = await mockKbApi(page, {
    documents: [chunkEditDocument()],
    chunksByVersion: { [KB10B_VERSION_ID]: [chunkDocParent, chunkDocChild] },
  });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "编辑样例");
  await drawer.getByTestId("kb-chunks-open").first().click();
  await expect(drawer.getByTestId("kb-chunks-view")).toBeVisible();
  return { drawer, mock };
}

async function submitEdit(page: Page, content: string, reason: string): Promise<void> {
  await page.getByTestId("kb-chunk-edit-content").fill(content);
  await page.getByTestId("kb-chunk-edit-reason").fill(reason);
  await page.getByTestId("kb-chunk-edit-submit").click();
}

test("KB-10B edit 码渲染编辑列（仅 child 行）；无码隐藏；人工修订 tag 预置渲染", async ({
  page,
}) => {
  // 有码：child 行有编辑按钮、parent 行没有；无人工修订 tag
  const first = await openChunkView(page, KB10B_EDIT_PERMS);
  await expect(first.drawer.getByTestId("kb-chunk-edit-open")).toHaveCount(1);
  await expect(first.drawer.getByTestId("kb-chunk-human-tag")).toHaveCount(0);
  await first.drawer.getByTestId("kb-chunk-detail-open").nth(1).click(); // child 行（第 2 行）
  await expect(first.drawer.getByTestId("kb-chunk-detail")).toBeVisible();
  await expect(first.drawer.getByTestId("kb-chunk-detail-edit")).toBeVisible();

  // 预置 is_human_edited → tag 渲染（表列 + 详情头部）
  await mockAuth(page, { authenticated: true, user: { permissions: KB10B_EDIT_PERMS } });
  await mockKbApi(page, {
    documents: [chunkEditDocument()],
    chunksByVersion: {
      [KB10B_VERSION_ID]: [
        chunkDocParent,
        kbChunk({
          ...chunkDocChild,
          is_human_edited: true,
          edited_by: "70000000-0000-0000-0000-000000000001",
          edited_at: "2026-11-17T00:00:00Z",
        }),
      ],
    },
  });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "编辑样例");
  await drawer.getByTestId("kb-chunks-open").first().click();
  await expect(drawer.getByTestId("kb-chunk-human-tag")).toBeVisible();

  // 无 chunk.edit 码：编辑列整体不出现（view_chunks 仍可看子视图）
  const none = await openChunkView(page, ["school.kb.view", "school.kb.view_chunks"]);
  await expect(none.drawer.getByTestId("kb-chunk-edit-open")).toHaveCount(0);
});

test("KB-10B 编辑提交 201：提交记录区出现待派生 + state 落记录", async ({ page }) => {
  const { drawer, mock } = await openChunkView(page, KB10B_EDIT_PERMS);
  await drawer.getByTestId("kb-chunk-edit-open").click();
  const dialog = page.getByTestId("kb-chunk-edit-dialog");
  await expect(dialog).toBeVisible();
  // 预填原文（对话框初始值 = chunk text）
  await expect(page.getByTestId("kb-chunk-edit-content")).toHaveValue(
    "安装步骤说明，需要修正错别字。",
  );
  // el-dialog teleport 到 body：page 级定位（KB-09C 先例）
  await submitEdit(page, "安装步骤说明，已修正错别字。", "修正错别字");
  await expect(dialog).toBeHidden();
  // 提交记录区出现（待派生 tag + 理由 + 块号）
  await expect(drawer.getByTestId("kb-chunk-edits")).toBeVisible();
  await expect(drawer.getByTestId("kb-chunk-edits")).toContainText("待派生");
  await expect(drawer.getByTestId("kb-chunk-edits")).toContainText("修正错别字");
  await expect(drawer.getByTestId("kb-chunk-edits")).toContainText("块 #1");
  // state 断言（有状态 mock）
  const state = mock.state.chunkEditsByVersion[KB10B_VERSION_ID];
  expect(state.edits).toHaveLength(1);
  expect(state.edits[0]?.status).toBe("submitted");
  expect(state.edits[0]?.chunk_id).toBe(KB10B_CHILD_ID);
  expect(state.edits[0]?.before_hash).toBe("b".repeat(64));
  expect(mock.captures.chunkEditActions).toHaveLength(1);
});

test("KB-10B 预检镜像：围栏不闭合/超上限禁提交；理由清空禁用", async ({ page }) => {
  const { drawer } = await openChunkView(page, KB10B_EDIT_PERMS);
  await drawer.getByTestId("kb-chunk-edit-open").click();
  await page.getByTestId("kb-chunk-edit-reason").fill("理由");
  const content = page.getByTestId("kb-chunk-edit-content");
  await content.fill("```python\nprint(1)\n");
  await expect(page.getByTestId("kb-chunk-edit-precheck")).toContainText("围栏未闭合");
  await expect(page.getByTestId("kb-chunk-edit-submit")).toBeDisabled();
  await content.fill("安装步骤说明，需要修正错别字。");
  await expect(page.getByTestId("kb-chunk-edit-precheck")).toHaveCount(0);
  await page.getByTestId("kb-chunk-edit-reason").fill("");
  await expect(page.getByTestId("kb-chunk-edit-submit")).toBeDisabled();
});

test("KB-10B 同块重复提交 409：对话框回填错误不关闭（部分唯一镜像）", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: KB10B_EDIT_PERMS } });
  await mockKbApi(page, {
    documents: [chunkEditDocument()],
    chunksByVersion: { [KB10B_VERSION_ID]: [chunkDocParent, chunkDocChild] },
    chunkEditsByVersion: {
      [KB10B_VERSION_ID]: {
        edits: [
          {
            chunk_id: KB10B_CHILD_ID,
            source_version_id: KB10B_VERSION_ID,
            status: "submitted",
            before_hash: "b".repeat(64),
            after_hash: "f".repeat(64),
            new_content: "第一次提交。",
            reason: "第一次",
            derived_version_id: null,
          },
        ],
        derived: [],
      },
    },
  });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "编辑样例");
  await drawer.getByTestId("kb-chunks-open").first().click();
  // 提交记录区已显示预置记录
  await expect(drawer.getByTestId("kb-chunk-edits")).toContainText("待派生");
  await drawer.getByTestId("kb-chunk-edit-open").click();
  await submitEdit(page, "第二次提交。", "第二次");
  await expect(page.getByTestId("kb-chunk-edit-error")).toContainText("待派生的编辑提交");
  await expect(page.getByTestId("kb-chunk-edit-dialog")).toBeVisible();
});

test("KB-10B diff 摘要展开：一删一增纯文本", async ({ page }) => {
  const { drawer } = await openChunkView(page, KB10B_EDIT_PERMS);
  await drawer.getByTestId("kb-chunk-edit-open").click();
  await submitEdit(page, "安装步骤说明，已修正错别字。", "修正");
  await drawer.getByTestId("kb-chunk-edit-diff").click();
  const diffView = drawer.getByTestId("kb-chunk-edit-diff-view");
  await expect(diffView).toBeVisible();
  await expect(diffView).toContainText("- 安装步骤说明，需要修正错别字。");
  await expect(diffView).toContainText("+ 安装步骤说明，已修正错别字。");
});

test("KB-10B confirm 码+role 双判：派生 201 翻转 confirmed；无 role 无派生列", async ({ page }) => {
  // 码+role 双全：编辑（子视图）→ 返回主体 → 派生 201 → edits 翻 confirmed + derived 记录
  const ok = await openChunkView(page, KB10B_CONFIRM_PERMS);
  await ok.drawer.getByTestId("kb-chunk-edit-open").click();
  await submitEdit(page, "安装步骤说明，已修正错别字。", "修正");
  await ok.drawer.getByTestId("kb-chunks-back").click(); // 派生按钮在文档主体版本表
  await expect(ok.drawer.getByTestId("kb-derive-open")).toBeVisible();
  await ok.drawer.getByTestId("kb-derive-open").click();
  const state = ok.mock.state.chunkEditsByVersion[KB10B_VERSION_ID];
  await expect(state.derived).toHaveLength(1);
  // 卡 E6：派生后再开编辑对话框 → 横幅引导（防静默搁浅）
  await ok.drawer.getByTestId("kb-chunks-open").first().click();
  await ok.drawer.getByTestId("kb-chunk-edit-open").first().click();
  await expect(page.getByTestId("kb-chunk-edit-derived-warn")).toBeVisible();
  await page.getByTestId("kb-chunk-edit-cancel").click();
  expect(state.derived[0]?.parent_version_id).toBe(KB10B_VERSION_ID);
  expect(state.derived[0]?.edited_chunks).toBe(1);
  await expect(state.edits[0]?.status).toBe("confirmed");

  // 有码无 role（school_admin）：派生列不出现（回主体看版本表）
  const noRole = await openChunkView(page, KB10B_CONFIRM_PERMS, ["school_admin"]);
  await noRole.drawer.getByTestId("kb-chunks-back").click();
  await expect(noRole.drawer.getByTestId("kb-derive-open")).toHaveCount(0);

  // 无码有 role：派生列同样不出现
  const noCode = await openChunkView(page, KB10B_EDIT_PERMS, ["school_super_admin"]);
  await noCode.drawer.getByTestId("kb-chunks-back").click();
  await expect(noCode.drawer.getByTestId("kb-derive-open")).toHaveCount(0);
});

test("KB-10B 已派生版本再点派生：409 CHUNK_EDIT_STATE 警示引导（卡 E6）", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: KB10B_CONFIRM_PERMS } });
  await mockKbApi(page, {
    documents: [chunkEditDocument()],
    chunksByVersion: { [KB10B_VERSION_ID]: [chunkDocParent, chunkDocChild] },
    chunkEditsByVersion: {
      [KB10B_VERSION_ID]: {
        edits: [
          {
            chunk_id: KB10B_CHILD_ID,
            source_version_id: KB10B_VERSION_ID,
            status: "confirmed",
            before_hash: "b".repeat(64),
            after_hash: "f".repeat(64),
            new_content: "已派生内容。",
            reason: "历史提交",
            derived_version_id: "71000000-0000-0000-0000-0000000000f1",
          },
        ],
        derived: [
          {
            version_id: "71000000-0000-0000-0000-0000000000f1",
            parent_version_id: KB10B_VERSION_ID,
            edited_chunks: 1,
          },
        ],
      },
    },
  });
  await page.goto("/kb/documents");
  const drawer = await openDrawer(page, "编辑样例");
  await drawer.getByTestId("kb-derive-open").click(); // 派生按钮在主体版本表（无需进子视图）
  // 409 → warning toast（服务端 message 优先：mock 文案含「派生」）
  await expect(page.locator(".el-message")).toContainText("派生");
});
