import { expect, test, type Page } from "@playwright/test";

import { field } from "../e2e/helpers/locators";
import { apiGet, apiLogin, apiPost, loadAccounts, type E2EAccounts } from "./helpers/support";

// M3 KB-05B + TD-292 全栈 E2E：审批 → 构建 → 测试 → 发布 → 下线 → 删除 真实链路（UI 驱动）。
//
// 口径注记（与简报 M3-KB-05B §5 一致）：
// - 上传恒建新文档且只有 v1（`service.py:890` `version_no=1`，产品面没有「给旧文档加版本」的
//   接口），因此回滚目标选择在真实产品里只能是「单一 PUBLISHED 版本 = 自身」——UI 走简报
//   D2-A 的兜底路径（重开抽屉活跃记忆复位 → 选到活跃版本 → 409 `KB_ROLLBACK_TARGET_IS_ACTIVE`）；
//   多版本间的真实 alias 切换由 KB-05 后端集成/业务场景测试覆盖（test_kb_publish.py 直插多版本）。
// - 测试集用无 expected_chunk_ids 的 CSV：`test_task.py:277-278` 无期望 → 默认通过，
//   TEST_PASSED 可达；带真实 chunk id 的 100% 命中强断言归 KB-04A 集成测试。
// - 发布重放（alias 恢复路径）与「指针已撤」两个后端恢复分支借 API 复发实测
//   （KB-05 后端审查 m-1 先例：恢复路径要有实测）。
// - MD 文件解析走 MarkdownNormalizer（`parser/dispatch.py:44`），不需要 MinerU；
//   embedding 与测试运行需要 TEI（e2e compose 已带 tei-embed，栈脚本等 healthy）。
//
// 串行写同一租户的数据；断言全部按唯一标题定位，不依赖全量条数。

test.describe.configure({ mode: "serial" });

const API = "/api/v1";

interface KbVersion {
  id: string;
  version_no: number;
  filename: string;
  status: string;
}

interface KbDocument {
  id: string;
  title: string;
  status: string;
  current_version: KbVersion | null;
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

/** 换账号登录前必须真登出：refresh Cookie 是 HttpOnly 的，不清掉 /login 会被守卫弹回首页。 */
async function logoutViaUi(page: Page): Promise<void> {
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page.getByTestId("login-submit")).toBeVisible();
}

async function pickFiles(
  page: Page,
  files: { name: string; mimeType: string; buffer: Buffer }[],
): Promise<void> {
  await page.locator("input[type=file]").setInputFiles(files);
}

async function listDocuments(accounts: E2EAccounts, token: string): Promise<KbDocument[]> {
  const response = await apiGet(accounts, `${API}/school/kb/documents?limit=100`, token);
  expect(response.status).toBe(200);
  return (response.json as KbPage<KbDocument>).items;
}

/** 轮询文档状态到目标值（文档行镜像当前版本状态，design §4.3）。 */
async function waitForDocumentStatus(
  accounts: E2EAccounts,
  token: string,
  documentId: string,
  target: string,
  timeoutMs = 240_000,
): Promise<KbDocument> {
  const deadline = Date.now() + timeoutMs;
  let last: KbDocument | null = null;
  while (Date.now() < deadline) {
    const response = await apiGet(accounts, `${API}/school/kb/documents/${documentId}`, token);
    expect(response.status).toBe(200);
    last = response.json as KbDocument;
    if (last.status === target) return last;
    await new Promise((resolve) => setTimeout(resolve, 3_000));
  }
  throw new Error(`等待文档状态 ${target} 超时（最后：${last?.status ?? "未知"}）`);
}

/**
 * MD 正文必须够大：切块参数默认 500 字符/块（TD-294），26 字节的标题文件会以
 * CHUNK_EMPTY 构建失败。约 3KB 正文可稳定切出父子块。
 */
function mdBody(label: string, runId: number): string {
  return `# ${label} ${runId}\n\n${"校园陪伴精灵知识库全栈验收正文，用于切块与向量索引。".repeat(120)}\n`;
}

async function openDrawer(page: Page, title: string): Promise<ReturnType<Page["getByTestId"]>> {
  await page
    .getByTestId("kb-document-table")
    .locator(".el-table__row", { hasText: title })
    .getByRole("button", { name: "查看" })
    .click();
  const drawer = page.getByTestId("kb-document-drawer");
  await expect(drawer).toBeVisible();
  return drawer;
}

test("发布链①：提交审批 → 构建 409 门禁 → publisher 403 审批 → 审批人通过 → 创建构建 → READY", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const accounts = loadAccounts();
  const admin = accounts.kb_admin;
  const approver = accounts.kb_approver;
  const publisher = accounts.kb_publisher;
  const runId = Date.now();
  const mdName = `kb-pub-chain-${runId}.md`;
  const title = `E2E 发布链 ${runId}`;

  await loginViaUi(page, admin);
  await page.getByRole("menuitem", { name: "上传文档" }).click();
  await pickFiles(page, [
    { name: mdName, mimeType: "text/markdown", buffer: Buffer.from(mdBody("发布链", runId)) },
  ]);
  // 显式标题（默认标题 = 文件名去扩展名，不填则列表里定位不到 title）
  await page.getByTestId("kb-meta-toggle").click();
  await page.getByPlaceholder(`默认：kb-pub-chain-${runId}`, { exact: true }).fill(title);
  await page.getByTestId("kb-submit").click();
  await expect(page.getByTestId("kb-upload-result")).toBeVisible();
  await expect(page.getByTestId("kb-result-table")).toContainText(title);
  await expect(page.getByTestId("kb-result-table")).toContainText("待审批");

  // 抽屉提交审批（UI；提交在 school.kb.upload 码下）
  await page.getByRole("menuitem", { name: "知识库" }).click();
  const drawer = await openDrawer(page, title);
  await drawer.getByTestId("kb-submit-approval").click();
  // toast 全文断言：短文案会同时命中提交后出现的禁用按钮（strict mode 冲突）
  await expect(page.getByText("已提交审批，可在「审批中心 → 已办」查看进度或撤销")).toBeVisible();
  // 抽屉必须关掉：mask 挡着退出登录按钮（点 mask 只会关抽屉，不会点到按钮）
  await page.locator(".el-drawer__close-btn").click();
  await expect(page.getByTestId("kb-document-drawer")).toBeHidden();

  // 接口侧拿到版本与审批单（真实业务状态，不只看 UI）。查询类调用用 publisherToken
  //（school.kb.view）：kb_admin 在并行文件（KB-01C）里也被 UI+API 双登录，少一次
  // API 登录少一份并发干扰面。
  const publisherToken = await loginTenant(accounts, publisher);
  const approverToken = await loginTenant(accounts, approver);
  const documents = await listDocuments(accounts, publisherToken);
  const document = documents.find((item) => item.title === title);
  expect(document, `列表中应能按标题找到 ${title}`).toBeTruthy();
  expect(document?.status).toBe("PENDING_APPROVAL");
  const versionId = document?.current_version?.id;
  expect(versionId).toBeTruthy();

  // TD-292 构建门禁：未审批创建构建必须 409 KB_NOT_APPROVED
  const deniedBuild = await apiPost(
    accounts,
    `${API}/school/kb/document-versions/${versionId}/build`,
    publisherToken,
  );
  expect(deniedBuild.status).toBe(409);
  expect((deniedBuild.json as { detail: { code: string } }).detail.code).toBe("KB_NOT_APPROVED");

  // 职责分离：publisher 无审批码 → 审批接口 403
  const todo = await apiGet(accounts, `${API}/school/approvals/todo?limit=20`, approverToken);
  expect(todo.status).toBe(200);
  const todoItems = (todo.json as KbPage<{ id: string; title: string }>).items;
  // 审批单 title 是「知识库文档审批：{文档标题}」前缀格式（service.py:1214），用包含匹配
  const request = todoItems.find((item) => item.title.includes(title));
  expect(request, `审批待办里应有 ${title}`).toBeTruthy();
  const deniedApprove = await apiPost(
    accounts,
    `${API}/school/approvals/${request?.id}/approve`,
    publisherToken,
  );
  expect(deniedApprove.status).toBe(403);

  // 审批人 UI：真登出换账号（refresh Cookie 不清 /login 会被守卫弹回）→ 审批中心 → 通过
  await logoutViaUi(page);
  await loginViaUi(page, approver);
  await expect(page.getByRole("menuitem", { name: "审批中心" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "上传文档" })).toHaveCount(0);
  await page.getByRole("menuitem", { name: "审批中心" }).click();
  const row = page.getByTestId("approval-table").locator(".el-table__row", {
    hasText: title,
  });
  await expect(row).toBeVisible();
  await row.getByTestId("approval-open").click();
  const approvalDrawer = page.getByTestId("approval-drawer");
  await expect(approvalDrawer).toBeVisible();
  await approvalDrawer.getByTestId("approval-approve").click();
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
  // 审批抽屉同样挡住退出登录按钮，先关再登出
  await page.getByTestId("approval-drawer-close").click();
  await expect(page.getByTestId("approval-drawer")).toBeHidden();

  // 审批结论落库（服务端状态断言）
  const afterApprove = await apiGet(
    accounts,
    `${API}/school/kb/documents/${document?.id}`,
    publisherToken,
  );
  expect(afterApprove.status, `详情响应异常：${JSON.stringify(afterApprove.json)}`).toBe(200);
  expect((afterApprove.json as KbDocument).status).toBe("APPROVED");

  // publisher UI：登出换账号 → 创建构建 → 轮询到 READY（TEI embedding + Qdrant 索引真实执行）
  await logoutViaUi(page);
  await loginViaUi(page, publisher);
  // publisher 无审批中心入口、无上传入口（D3-A UI 边界）
  await expect(page.getByRole("menuitem", { name: "审批中心" })).toHaveCount(0);
  await expect(page.getByRole("menuitem", { name: "上传文档" })).toHaveCount(0);
  await page.getByRole("menuitem", { name: "知识库" }).click();
  const buildDrawer = await openDrawer(page, title);
  await buildDrawer.getByTestId("kb-create-build").click();
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
  await expect(page.getByText("构建任务已创建")).toBeVisible();
  await waitForDocumentStatus(accounts, publisherToken, document?.id ?? "", "READY");
  // 抽屉不会自己刷新（TD-296 无构建进度 UI）：重开后版本状态列显示「已构建·待测试」（14 档文案）
  await page.locator(".el-drawer__close-btn").click();
  await expect(page.getByTestId("kb-document-drawer")).toBeHidden();
  const rebuiltDrawer = await openDrawer(page, title);
  await expect(rebuiltDrawer.getByTestId("kb-version-table")).toContainText("已构建·待测试");
});

test("发布链②：测试沙箱 → TEST_PASSED → UI 发布 → 回滚 409 兜底 → 重放恢复 → UI 下线 → 指针已撤 409", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const accounts = loadAccounts();
  const admin = accounts.kb_admin;
  const approver = accounts.kb_approver;
  const publisher = accounts.kb_publisher;
  const adminToken = await loginTenant(accounts, admin);
  const approverToken = await loginTenant(accounts, approver);
  const publisherToken = await loginTenant(accounts, publisher);
  const runId = Date.now();
  const mdName = `kb-pub-pub-${runId}.md`;
  const title = `E2E 发布动作 ${runId}`;
  const question = `发布动作验证问题 ${runId} 的内容是什么？`;

  // 前置：上传 → 提交 → 通过（接口速通；审批/构建/发布的 UI 断言在链①与链③）
  const form = new FormData();
  form.append("files", new Blob([mdBody("发布动作", runId)], { type: "text/markdown" }), mdName);
  // 显式标题，否则列表/待办按 title 定位不到（默认标题 = 文件名去扩展名）
  form.append("metadata", JSON.stringify([{ title }]));
  const upload = await fetch(`${accounts.e2e_base_url}${API}/school/kb/documents`, {
    method: "POST",
    headers: { authorization: `Bearer ${adminToken}`, "Idempotency-Key": `kb-pub-${runId}` },
    body: form,
  });
  expect(upload.status).toBe(201);
  const uploaded = (await upload.json()) as { documents: KbDocument[] };
  const documentId = uploaded.documents[0]?.id;
  const versionId = uploaded.documents[0]?.current_version?.id;
  expect(documentId).toBeTruthy();
  const submit = await apiPost(
    accounts,
    `${API}/school/kb/document-versions/${versionId}/submit-approval`,
    adminToken,
  );
  expect(submit.status).toBe(201); // 创建审批单（school_kb.py:346 status_code=201）
  const todo = await apiGet(accounts, `${API}/school/approvals/todo?limit=20`, approverToken);
  const request = (todo.json as KbPage<{ id: string; title: string }>).items.find((item) =>
    item.title.includes(title),
  );
  expect(request, `审批待办里应有 ${title}`).toBeTruthy();
  const approve = await apiPost(
    accounts,
    `${API}/school/approvals/${request?.id}/approve`,
    approverToken,
  );
  expect(approve.status).toBe(200);
  const build = await apiPost(
    accounts,
    `${API}/school/kb/document-versions/${versionId}/build`,
    publisherToken,
  );
  expect(build.status).toBe(201); // 创建构建任务（school_kb.py:372 status_code=201）
  await waitForDocumentStatus(accounts, publisherToken, documentId ?? "", "READY");

  // publisher UI：测试沙箱上传 CSV（无 expected_chunk_ids → 默认通过）→ 发起运行
  await loginViaUi(page, publisher);
  await page.getByRole("menuitem", { name: "知识库" }).click();
  const drawer = await openDrawer(page, title);
  await expect(drawer.getByTestId("kb-test-section")).toBeVisible();
  const csvName = `kb-pub-cases-${runId}.csv`;
  await drawer.getByTestId("kb-test-set-file").setInputFiles({
    name: csvName,
    mimeType: "text/csv",
    buffer: Buffer.from(`question\n${question}\n`),
  });
  await drawer.getByTestId("kb-test-start").click();
  await expect(page.getByText("测试运行已发起")).toBeVisible();

  // 轮询文档状态到 TEST_PASSED，UI 刷新测试结果显示
  await waitForDocumentStatus(accounts, publisherToken, documentId ?? "", "TEST_PASSED");
  await drawer.getByTestId("kb-test-poll").click();
  await expect(drawer.getByTestId("kb-test-run-status")).toContainText("测试通过");

  // 动作门禁按打开时的状态渲染（抽屉不自动刷新）：重开抽屉拿 TEST_PASSED 的新状态，发布按钮才出现
  await page.locator(".el-drawer__close-btn").click();
  await expect(page.getByTestId("kb-document-drawer")).toBeHidden();
  const publishDrawer = await openDrawer(page, title);

  // UI 发布：确认 → toast → 状态列变已发布
  await publishDrawer.getByTestId("kb-publish").click();
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
  await expect(page.getByText("v1 已发布")).toBeVisible();

  // 有活跃记忆（本次发布响应）：回滚下拉无可回滚目标（活跃版本被排除，D2-A）
  await publishDrawer.getByTestId("kb-rollback-trigger").click();
  await expect(page.getByTestId("kb-rollback-menu")).toContainText("无可回滚目标");
  await page.keyboard.press("Escape");

  // 重开抽屉（活跃记忆复位）：单一 PUBLISHED 版本 = 自身 → 409 KB_ROLLBACK_TARGET_IS_ACTIVE
  await page.locator(".el-drawer__close-btn").click();
  const reopened = await openDrawer(page, title);
  await reopened.getByTestId("kb-rollback-trigger").click();
  await page.getByTestId("kb-rollback-menu").getByTestId("kb-rollback-target").click();
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
  await expect(page.getByText("目标版本已是当前活跃发布")).toBeVisible();

  // 恢复路径实测（KB-05 审查 m-1 先例）：已发布 + 活跃 → 重放 = 重建 alias（replayed=true）
  const replay = await apiPost(
    accounts,
    `${API}/school/kb/versions/${versionId}/publish`,
    publisherToken,
  );
  expect(replay.status).toBe(200);
  const replayBody = replay.json as { replayed: boolean; version_id: string; alias: string };
  expect(replayBody.replayed).toBe(true);
  expect(replayBody.version_id).toBe(versionId);
  expect(replayBody.alias).toContain("_active");

  // UI 下线：活跃发布撤除 → 终态后动作区消失
  await reopened.getByTestId("kb-offline").click();
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
  await expect(page.getByText("v1 已下线，活跃发布已撤除")).toBeVisible();
  await expect(reopened.getByTestId("kb-publish-row")).toHaveCount(0);
  await expect(reopened.getByTestId("kb-version-table")).toContainText("已下线");

  // 下线后重发 → 409 KB_NOT_TEST_PASSED（OFFLINE 不满足发布断言；「PUBLISHED 但指针丢失」
  // 的 KB_PUBLISHED_NOT_ACTIVE 只在 alias 行丢失的恢复场景出现，正常流不可达——
  // 该分支由 KB-05 集成测试直删 alias 行实测）
  const afterOffline = await apiPost(
    accounts,
    `${API}/school/kb/versions/${versionId}/publish`,
    publisherToken,
  );
  expect(afterOffline.status).toBe(409);
  expect((afterOffline.json as { detail: { code: string } }).detail.code).toBe(
    "KB_NOT_TEST_PASSED",
  );

  // 服务端状态断言：文档行与版本行都是 OFFLINE
  const document = await apiGet(
    accounts,
    `${API}/school/kb/documents/${documentId}`,
    publisherToken,
  );
  expect((document.json as KbDocument).status).toBe("OFFLINE");
});

test("发布链③：发布响应契约 + UI 删除（软删终态）", async ({ page }) => {
  test.setTimeout(300_000);
  const accounts = loadAccounts();
  const admin = accounts.kb_admin;
  const approver = accounts.kb_approver;
  const publisher = accounts.kb_publisher;
  const adminToken = await loginTenant(accounts, admin);
  const approverToken = await loginTenant(accounts, approver);
  const publisherToken = await loginTenant(accounts, publisher);
  const runId = Date.now();
  const mdName = `kb-pub-del-${runId}.md`;
  const title = `E2E 发布删除 ${runId}`;

  // 接口速通到 TEST_PASSED（链②已验证 UI 侧；这里补发布响应契约 + UI 删除）
  const form = new FormData();
  form.append("files", new Blob([mdBody("发布删除", runId)], { type: "text/markdown" }), mdName);
  form.append("metadata", JSON.stringify([{ title }]));
  const upload = await fetch(`${accounts.e2e_base_url}${API}/school/kb/documents`, {
    method: "POST",
    headers: { authorization: `Bearer ${adminToken}`, "Idempotency-Key": `kb-del-${runId}` },
    body: form,
  });
  expect(upload.status).toBe(201);
  const uploaded = (await upload.json()) as { documents: KbDocument[] };
  const documentId = uploaded.documents[0]?.id;
  const versionId = uploaded.documents[0]?.current_version?.id;
  await apiPost(
    accounts,
    `${API}/school/kb/document-versions/${versionId}/submit-approval`,
    adminToken,
  );
  const todo = await apiGet(accounts, `${API}/school/approvals/todo?limit=20`, approverToken);
  const request = (todo.json as KbPage<{ id: string; title: string }>).items.find((item) =>
    item.title.includes(title),
  );
  expect(request, `审批待办里应有 ${title}`).toBeTruthy();
  await apiPost(accounts, `${API}/school/approvals/${request?.id}/approve`, approverToken);
  const build = await apiPost(
    accounts,
    `${API}/school/kb/document-versions/${versionId}/build`,
    publisherToken,
  );
  expect(build.status).toBe(201); // 创建构建任务（school_kb.py:372 status_code=201）
  await waitForDocumentStatus(accounts, publisherToken, documentId ?? "", "READY");

  // 测试集（multipart）→ 运行 → TEST_PASSED（接口侧；无 expected_chunk_ids 默认通过）
  const testForm = new FormData();
  testForm.append(
    "file",
    new Blob([`question\n删除链验证问题 ${runId}？\n`], { type: "text/csv" }),
    `kb-del-cases-${runId}.csv`,
  );
  testForm.append("name", `删除链测试集 ${runId}`);
  const testSet = await fetch(
    `${accounts.e2e_base_url}${API}/school/kb/versions/${versionId}/test-sets`,
    { method: "POST", headers: { authorization: `Bearer ${publisherToken}` }, body: testForm },
  );
  expect(testSet.status).toBe(201);
  const testSetBody = (await testSet.json()) as { test_set_id: string };
  // 运行发起端点收 form（与 api-client testSet.startTestRun 同形），不是 JSON
  const runForm = new FormData();
  runForm.set("test_set_id", testSetBody.test_set_id);
  const runStart = await fetch(
    `${accounts.e2e_base_url}${API}/school/kb/versions/${versionId}/tests`,
    { method: "POST", headers: { authorization: `Bearer ${publisherToken}` }, body: runForm },
  );
  expect(runStart.status).toBe(201);
  await waitForDocumentStatus(accounts, publisherToken, documentId ?? "", "TEST_PASSED");

  // 发布（API）：响应契约逐字段断言（KB-05 KbPublishResponse，school_kb.py:815-825）
  const publish = await apiPost(
    accounts,
    `${API}/school/kb/versions/${versionId}/publish`,
    publisherToken,
  );
  expect(publish.status).toBe(200);
  const publishBody = publish.json as {
    document_id: string;
    version_id: string;
    version_no: number;
    status: string;
    alias: string;
    collection: string;
    previous_version_id: string | null;
    published_at: string;
    replayed: boolean;
    alias_synced: boolean;
  };
  expect(publishBody.document_id).toBe(documentId);
  expect(publishBody.version_id).toBe(versionId);
  expect(publishBody.version_no).toBe(1);
  expect(publishBody.status).toBe("PUBLISHED");
  expect(publishBody.alias).toContain("_active");
  expect(publishBody.collection).toContain("_kb_v");
  expect(publishBody.previous_version_id).toBeNull();
  expect(publishBody.replayed).toBe(false);
  expect(publishBody.alias_synced).toBe(true);
  expect(publishBody.published_at).toBeTruthy();

  // UI 删除（publisher）：确认按钮是「删除」→ 状态列已删除 → 服务端 DELETED
  await loginViaUi(page, publisher);
  await page.getByRole("menuitem", { name: "知识库" }).click();
  const drawer = await openDrawer(page, title);
  await drawer.getByTestId("kb-delete").click();
  await page.locator(".el-message-box").getByRole("button", { name: "删除" }).click();
  await expect(page.getByText("v1 已删除，活跃发布已撤除")).toBeVisible();
  await expect(drawer.getByTestId("kb-publish-row")).toHaveCount(0);
  await expect(drawer.getByTestId("kb-version-table")).toContainText("已删除");

  const document = await apiGet(
    accounts,
    `${API}/school/kb/documents/${documentId}`,
    publisherToken,
  );
  expect((document.json as KbDocument).status).toBe("DELETED");
});
