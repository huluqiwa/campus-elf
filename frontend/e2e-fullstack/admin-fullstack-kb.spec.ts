import { expect, test, type Page } from "@playwright/test";

import { field } from "../e2e/helpers/locators";
import { apiGet, apiLogin, loadAccounts, type E2EAccounts } from "./helpers/support";

// M3 KB-01C 全栈 E2E（真实后端 + 真实测试库 + 真实前端产物）。
// 扫描按简报 D1 用 dev-skip：上传直接落 PENDING_APPROVAL + scan.state=skipped（engine=dev-skip），
// 因此这里验证的是「待审批 + 已跳过扫描」主链路，不是 worker/ClamAV 的真实扫描（TD-284）。
// 四条用例会写同一租户的数据，必须串行；断言全部按唯一标题/文件名定位，不依赖全量条数。

test.describe.configure({ mode: "serial" });

const API = "/api/v1";

interface KbScanState {
  state: string;
  engine: string | null;
  signature: string | null;
  error_code: string | null;
  attempts: number;
  scanned_at: string | null;
}

interface KbVersion {
  id: string;
  version_no: number;
  filename: string;
  mime: string;
  size_bytes: number;
  status: string;
  scan: KbScanState;
  created_at: string;
}

interface KbDocument {
  id: string;
  title: string;
  tags: string[];
  description: string | null;
  status: string;
  current_version: KbVersion | null;
  created_at: string;
  updated_at: string;
}

interface KbUploadResponse {
  batch_id: string;
  replayed: boolean;
  documents: KbDocument[];
}

interface KbPage<T> {
  items: T[];
  next_cursor: string | null;
}

type TenantAccount = { school_code: string; login_id: string; password: string };

async function loginTenant(accounts: E2EAccounts, who: TenantAccount): Promise<string> {
  return apiLogin(accounts, {
    realm: "tenant",
    school_code: who.school_code,
    login_id: who.login_id,
    password: who.password,
  });
}

async function loginViaUi(page: Page, who: TenantAccount): Promise<void> {
  await page.goto("/login");
  await field(page, "school-code").fill(who.school_code);
  await field(page, "login-id").fill(who.login_id);
  await field(page, "password").fill(who.password);
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("identity")).toBeVisible();
}

/** 原生 multipart 上传（与 admin-fullstack-m2ui 的导入写法同源；不手设 Content-Type）。 */
async function uploadViaApi(
  accounts: E2EAccounts,
  token: string,
  files: Array<{ filename: string; content: string; type: string }>,
  options: { key: string; metadata?: unknown[] },
): Promise<{ status: number; json: unknown }> {
  const form = new FormData();
  for (const file of files) {
    form.append("files", new Blob([file.content], { type: file.type }), file.filename);
  }
  if (options.metadata !== undefined) form.set("metadata", JSON.stringify(options.metadata));
  const response = await fetch(`${accounts.e2e_base_url}${API}/school/kb/documents`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "Idempotency-Key": options.key },
    body: form,
  });
  return { status: response.status, json: await response.json().catch(() => null) };
}

async function pickFiles(
  page: Page,
  files: { name: string; mimeType: string; buffer: Buffer }[],
): Promise<void> {
  await page.locator('input[type="file"]').setInputFiles(files);
}

async function listDocuments(accounts: E2EAccounts, token: string): Promise<KbDocument[]> {
  const response = await apiGet(accounts, `${API}/school/kb/documents?limit=100`, token);
  expect(response.status).toBe(200);
  return (response.json as KbPage<KbDocument>).items;
}

test("主链路：UI 上传两份文件（含元数据）→ 待审批/已跳过扫描 → 列表与抽屉可见", async ({
  page,
}) => {
  const accounts = loadAccounts();
  const admin = accounts.kb_admin;
  const runId = Date.now();
  const mdName = `kb-e2e-${runId}.md`;
  const pdfName = `kb-e2e-${runId}-附件.pdf`;
  const mdStem = `kb-e2e-${runId}`;
  const mdContent = `# E2E ${runId}\n`;
  const title = `E2E 知识库 ${runId}`;
  const description = `全栈 E2E 元数据 ${runId}`;

  await loginViaUi(page, admin);
  await page.getByRole("menuitem", { name: "上传文档" }).click();
  await expect(page).toHaveURL(/\/kb\/upload$/);

  await pickFiles(page, [
    { name: mdName, mimeType: "text/markdown", buffer: Buffer.from(mdContent) },
    { name: pdfName, mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4\n% e2e\n%%EOF\n") },
  ]);
  await expect(page.getByTestId("kb-upload-table")).toContainText(mdName);

  await page.getByTestId("kb-meta-toggle").click();
  await page.getByPlaceholder(`默认：${mdStem}`, { exact: true }).fill(title);
  await page.getByPlaceholder("逗号分隔，≤10 个，每个 ≤32 字").first().fill("e2e,知识库");
  await page.getByPlaceholder("可选").first().fill(description);
  await page.getByTestId("kb-submit").click();

  await expect(page.getByTestId("kb-upload-result")).toBeVisible();
  // 批次号必须是真实 UUID，不能是空值/占位符
  await expect(page.getByTestId("kb-batch-id")).toHaveText(
    /^批次：[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
  );
  await expect(page.getByTestId("kb-replayed")).toHaveCount(0);
  const resultTable = page.getByTestId("kb-result-table");
  await expect(resultTable.locator(".el-table__row")).toHaveCount(2);
  await expect(resultTable).toContainText(title);
  await expect(resultTable).toContainText(mdName);
  await expect(resultTable).toContainText(pdfName);
  await expect(resultTable).toContainText("待审批");
  await expect(resultTable).toContainText("已跳过扫描");
  await expect(resultTable).toContainText("开发环境跳过扫描");
  await expect(page.getByText("扫描已全部结束。")).toBeVisible();

  await page.getByRole("menuitem", { name: "知识库" }).click();
  await expect(page).toHaveURL(/\/kb\/documents$/);
  const documentTable = page.getByTestId("kb-document-table");
  const row = documentTable.locator(".el-table__row", { hasText: title });
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "查看" }).click();
  const drawer = page.getByTestId("kb-document-drawer");
  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText(title);
  await expect(drawer.getByText("e2e", { exact: true })).toBeVisible();
  await expect(drawer.getByText("知识库", { exact: true })).toBeVisible();
  await expect(drawer).toContainText(description);
  await expect(drawer).toContainText("v1");
  await expect(drawer).toContainText(mdName);
  await expect(drawer.getByTestId("kb-version-table").locator(".el-table__row")).toHaveCount(1);

  // 接口侧核对业务结果（不只是 UI 文案）：状态、派生扫描、元数据与版本
  const token = await loginTenant(accounts, admin);
  const documents = await listDocuments(accounts, token);
  const document = documents.find((item) => item.title === title);
  expect(document, `列表中应能按标题找到 ${title}`).toBeTruthy();
  expect(document?.status).toBe("PENDING_APPROVAL");
  expect(document?.tags).toEqual(["e2e", "知识库"]);
  expect(document?.description).toBe(description);
  expect(document?.current_version?.filename).toBe(mdName);
  expect(document?.current_version?.mime).toBe("text/markdown");
  expect(document?.current_version?.size_bytes).toBe(Buffer.byteLength(mdContent));
  expect(document?.current_version?.scan.state).toBe("skipped");
  expect(document?.current_version?.scan.engine).toBe("dev-skip");
  expect(document?.current_version?.scan.error_code).toBeNull();
  expect(document?.current_version?.scan.signature).toBeNull();

  const detail = await apiGet(accounts, `${API}/school/kb/documents/${document?.id}`, token);
  expect(detail.status).toBe(200);
  const versions = await apiGet(
    accounts,
    `${API}/school/kb/documents/${document?.id}/versions?limit=20`,
    token,
  );
  expect(versions.status).toBe(200);
  const versionPage = versions.json as KbPage<KbVersion>;
  expect(versionPage.items).toHaveLength(1);
  expect(versionPage.items[0]?.version_no).toBe(1);
  expect(versionPage.items[0]?.status).toBe("PENDING_APPROVAL");
});

test("幂等：同键同文件重放同批次，同键换文件 409，新键同文件新建文档", async () => {
  const accounts = loadAccounts();
  const token = await loginTenant(accounts, accounts.kb_admin);
  const runId = Date.now();
  const filename = `kb-idem-${runId}.md`;
  const content = `# idempotency ${runId}\n`;
  const firstKey = `kb-e2e-${runId}-a`;

  const first = await uploadViaApi(
    accounts,
    token,
    [{ filename, content, type: "text/markdown" }],
    { key: firstKey },
  );
  expect(first.status).toBe(201);
  const firstBody = first.json as KbUploadResponse;
  expect(firstBody.replayed).toBe(false);
  expect(firstBody.batch_id).toBeTruthy();
  expect(firstBody.documents).toHaveLength(1);
  const firstId = firstBody.documents[0]?.id;
  expect(firstId).toBeTruthy();

  const replay = await uploadViaApi(
    accounts,
    token,
    [{ filename, content, type: "text/markdown" }],
    { key: firstKey },
  );
  expect(replay.status).toBe(201);
  const replayBody = replay.json as KbUploadResponse;
  expect(replayBody.replayed).toBe(true);
  expect(replayBody.batch_id).toBe(firstBody.batch_id);
  expect(replayBody.documents).toHaveLength(1);
  expect(replayBody.documents[0]?.id).toBe(firstId);
  // 重放不能落新行：文件名与默认标题各恰好 1 条
  const afterReplay = await listDocuments(accounts, token);
  expect(afterReplay.filter((item) => item.current_version?.filename === filename)).toHaveLength(1);
  expect(afterReplay.filter((item) => item.title === `kb-idem-${runId}`)).toHaveLength(1);

  const otherFilename = `kb-idem-other-${runId}.md`;
  const conflict = await uploadViaApi(
    accounts,
    token,
    [{ filename: otherFilename, content, type: "text/markdown" }],
    { key: firstKey },
  );
  expect(conflict.status).toBe(409);
  expect((conflict.json as { detail: { code: string } }).detail.code).toBe(
    "IDEMPOTENCY_KEY_REUSED",
  );
  // 409 必须整批拒绝：冲突请求里的文件不能偷偷落库
  const afterConflict = await listDocuments(accounts, token);
  expect(
    afterConflict.filter((item) => item.current_version?.filename === otherFilename),
  ).toHaveLength(0);
  expect(afterConflict.filter((item) => item.title === `kb-idem-other-${runId}`)).toHaveLength(0);

  const fresh = await uploadViaApi(
    accounts,
    token,
    [{ filename, content, type: "text/markdown" }],
    { key: `kb-e2e-${runId}-b` },
  );
  expect(fresh.status).toBe(201);
  const freshBody = fresh.json as KbUploadResponse;
  expect(freshBody.replayed).toBe(false);
  expect(freshBody.documents).toHaveLength(1);
  const freshId = freshBody.documents[0]?.id;
  expect(freshId).toBeTruthy();
  expect(freshId).not.toBe(firstId);
  // 新幂等键才会真的落第二行
  const afterFresh = await listDocuments(accounts, token);
  expect(afterFresh.filter((item) => item.current_version?.filename === filename)).toHaveLength(2);
  expect(afterFresh.filter((item) => item.title === `kb-idem-${runId}`)).toHaveLength(2);
});

test("整批原子：坏文件 422 逐文件错误，同批好文件不落库", async ({ page }) => {
  const accounts = loadAccounts();
  const admin = accounts.kb_admin;
  const token = await loginTenant(accounts, admin);
  const runId = Date.now();
  const good = `kb-atomic-good-${runId}.md`;
  const bad = `kb-atomic-bad-${runId}.md`;

  await loginViaUi(page, admin);
  await page.getByRole("menuitem", { name: "上传文档" }).click();
  await pickFiles(page, [
    { name: good, mimeType: "text/markdown", buffer: Buffer.from(`# ok ${runId}\n`) },
    // 合法 UTF-8 + NUL：命中后端 TEXT_BINARY_CONTENT（不是编码错误）
    { name: bad, mimeType: "text/markdown", buffer: Buffer.from("a\u0000b") },
  ]);
  await page.getByTestId("kb-submit").click();

  await expect(page.getByTestId("kb-upload-error")).toContainText("整批未保存");
  const fileErrors = page.getByTestId("kb-file-errors");
  await expect(fileErrors).toContainText(bad);
  await expect(fileErrors).toContainText("TEXT_BINARY_CONTENT");
  await expect(page.getByTestId("kb-upload-result")).toHaveCount(0);

  const documents = await listDocuments(accounts, token);
  const filenames = documents.map((item) => item.current_version?.filename);
  expect(filenames).not.toContain(good);
  expect(filenames).not.toContain(bad);
  // 默认标题取自文件名去扩展名；即便详情缺失 current_version，也不应有残留文档
  const titles = documents.map((item) => item.title);
  expect(titles).not.toContain(`kb-atomic-good-${runId}`);
  expect(titles).not.toContain(`kb-atomic-bad-${runId}`);
});

test("上传-only 账号：接口 403、菜单/守卫生效、结果页零详情请求，校管可见其文档", async ({
  page,
}) => {
  const accounts = loadAccounts();
  const uploader = accounts.kb_uploader;
  const admin = accounts.kb_admin;
  const adminToken = await loginTenant(accounts, admin);
  const uploaderToken = await loginTenant(accounts, uploader);
  const runId = Date.now();
  const filename = `kb-upload-only-${runId}.md`;
  const filenameStem = `kb-upload-only-${runId}`;
  const title = `上传-only ${runId}`;

  const deniedList = await apiGet(accounts, `${API}/school/kb/documents?limit=5`, uploaderToken);
  expect(deniedList.status).toBe(403);

  const detailRequests: string[] = [];
  page.on("request", (request) => {
    if (request.method() !== "GET") return;
    const path = new URL(request.url()).pathname;
    if (/^\/api\/v1\/school\/kb\/documents\/[^/]+$/.test(path)) detailRequests.push(path);
  });

  await loginViaUi(page, uploader);
  await expect(page.getByRole("menuitem")).toHaveCount(1);
  await expect(page.getByRole("menuitem", { name: "上传文档" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "知识库" })).toHaveCount(0);

  await page.goto("/kb/documents");
  await expect(page).toHaveURL(/\/forbidden$/);
  await expect(page.getByText("你没有访问该页面的权限")).toBeVisible();

  await page.goto("/kb/upload");
  await pickFiles(page, [
    { name: filename, mimeType: "text/markdown", buffer: Buffer.from(`# uploader ${runId}\n`) },
  ]);
  await page.getByTestId("kb-meta-toggle").click();
  await page.getByPlaceholder(`默认：${filenameStem}`, { exact: true }).fill(title);
  await page.getByTestId("kb-submit").click();

  await expect(page.getByTestId("kb-upload-result")).toBeVisible();
  await expect(page.getByTestId("kb-result-table")).toContainText("待审批");
  await expect(page.getByTestId("kb-result-table")).toContainText("已跳过扫描");
  await expect(page.getByText("扫描在后台进行；你没有知识库查看权限")).toBeVisible();

  // 无 view 不会开始轮询：等过一轮 5 秒窗口，详情请求必须仍为 0
  await page.waitForTimeout(6000);
  expect(detailRequests).toHaveLength(0);

  const documents = await listDocuments(accounts, adminToken);
  const document = documents.find((item) => item.title === title);
  expect(document, "校管（全校范围）应能看到上传-only 账号的文档").toBeTruthy();
  expect(document?.status).toBe("PENDING_APPROVAL");
  expect(document?.current_version?.filename).toBe(filename);
  expect(document?.current_version?.scan.state).toBe("skipped");

  const deniedDetail = await apiGet(
    accounts,
    `${API}/school/kb/documents/${document?.id}`,
    uploaderToken,
  );
  expect(deniedDetail.status).toBe(403);
});
