import { expect, test, type Page } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";
import { kbDocument, kbVersion, mockKbApi, type KbDocumentFixture } from "./helpers/kb-mock";

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
  await row.getByRole("button", { name: "查看" }).click();

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
    .getByRole("button", { name: "查看" })
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
    .getByRole("button", { name: "查看" })
    .click();
  await expect.poll(() => captures.detailIds.length).toBeGreaterThan(0);

  // A 还在路上时关掉抽屉，改开 B
  await page.locator(".el-drawer__close-btn").click();
  await expect(page.getByTestId("kb-document-drawer")).toBeHidden();
  await table
    .locator(".el-table__row", { hasText: "可疑附件" })
    .getByRole("button", { name: "查看" })
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
    .getByRole("button", { name: "查看" })
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
    .getByRole("button", { name: "查看" })
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
    .getByRole("button", { name: "查看" })
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
    .getByRole("button", { name: "查看" })
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
  await row.getByRole("button", { name: "查看" }).click();
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
    .getByRole("button", { name: "查看" })
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
    .getByRole("button", { name: "查看" })
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
  await page
    .locator(".el-table__row", { hasText: title })
    .getByRole("button", { name: "查看" })
    .click();
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

test("KB-05B 回滚：发布后可回滚到旧 PUBLISHED 版本；重开抽屉后选活跃版本吃 409 兜底", async ({
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
  await drawer.getByTestId("kb-publish").click();
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
  await expect(page.getByText("v2 已发布")).toBeVisible();

  // 发布成功后刷新：回滚下拉排除会话内已知活跃 v2，只列 v1
  await drawer.getByTestId("kb-rollback-trigger").click();
  const menu = page.getByTestId("kb-rollback-menu");
  await expect(menu).toBeVisible();
  await expect(menu).toContainText("v1 · 发布样例-v1.pdf");
  await expect(menu).not.toContainText("v2");
  await menu.getByTestId("kb-rollback-target").click();
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
  await expect(page.getByText("已回滚：v1 成为活跃发布")).toBeVisible();
  expect(mock.captures.versionActions).toHaveLength(2);

  // 重开抽屉：活跃记忆复位，v1/v2 都是 PUBLISHED 全列；选到活跃 v1 → 后端 409 兜底（D2-A）
  await page.locator(".el-drawer__close-btn").click();
  const reopened = await openDrawer(page, "发布样例");
  await reopened.getByTestId("kb-rollback-trigger").click();
  const reopenedMenu = page.getByTestId("kb-rollback-menu");
  await expect(reopenedMenu).toContainText("v2 · 发布样例-v2.pdf");
  await reopenedMenu.getByTestId("kb-rollback-target").first().click();
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
  await expect(page.getByText("目标版本已是当前活跃发布")).toBeVisible();
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
