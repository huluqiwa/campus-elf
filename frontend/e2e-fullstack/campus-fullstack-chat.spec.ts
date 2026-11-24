/** M4-UI2 全栈链⑤：C 端聊天对真实后端（登录 → 提问 → 流式 → 引用抽屉 →
 * 反馈提交 → 清空 → 历史为空）。
 *
 * 前置（本地运行，CI 不跑）：
 *   1. `scripts/e2e-stack.sh up`（真后端栈 + 种子账号）；
 *   2. KB 发布链由本 spec beforeAll 以 API 驱动自建（上传→审批→ACL→构建→
 *      测试集 CSV〔无 expected_chunk_ids → 默认通过〕→ TEST_PASSED 轮询 → 发布）；
 *   3. 真流式弧需宿主导出 `E2E_LLM_API_KEY/E2E_LLM_BASE_URL/E2E_LLM_MODEL`
 *      （compose 旁通后端容器）；缺省 = LLM_NOT_CONFIGURED 兜底弧（反馈/清空
 *      仍全链有效——meta/trace 在 GENERATE 前已落）。
 *
 * 运行：`pnpm test:e2e:fullstack -- campus-fullstack-chat`（先 e2e-stack.sh up）。
 */

import { expect, test } from "@playwright/test";

import { field } from "../e2e/helpers/locators";
import { loadAccounts } from "./helpers/support";

const API = "/api/v1";

const accounts = loadAccounts();
const RUN_ID = Date.now();
const TITLE = `E2E 链⑤ ${RUN_ID}`;
const MARKDOWN = `# 财务报销手册 ${RUN_ID}\n\n## 第一章 差旅报销\n\n差旅报销需要发票和审批单。报销流程第一步提交申请。\n`;

async function apiLogin(who: {
  school_code: string;
  login_id: string;
  password: string;
}): Promise<string> {
  const res = await fetch(`${accounts.e2e_base_url}${API}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      realm: "tenant",
      school_code: who.school_code,
      login_id: who.login_id,
      password: who.password,
    }),
  });
  expect(res.status).toBe(200);
  return ((await res.json()) as { access_token: string }).access_token;
}

async function apiCall<T>(
  token: string,
  path: string,
  init?: { method?: string; body?: string; contentType?: string },
): Promise<{ status: number; json: T | null }> {
  const res = await fetch(`${accounts.e2e_base_url}${path}`, {
    method: init?.method ?? "GET",
    headers: {
      ...(init?.contentType ? { "Content-Type": init.contentType } : {}),
      ...(init?.body ? { "Content-Length": String(init.body.length) } : {}),
      Authorization: `Bearer ${token}`,
    },
    ...(init?.body ? { body: init.body } : {}),
  });
  const text = await res.text();
  return { status: res.status, json: text ? (JSON.parse(text) as T) : null };
}

test.describe.configure({ mode: "serial" });

test("链⑤-前置：API 驱动发布 KB（上传→审批→ACL→构建→测试集→TEST_PASSED→发布）", async () => {
  // 各步用种子里的专职账号（school_ops 无 KB 权限——第 2 轮实测 403 教训）；
  // ACL/构建/测试/发布用 kb_publisher（acl.manage 等码），审批用 kb_approver
  const uploaderToken = await apiLogin(accounts.kb_uploader);
  const approverToken = await apiLogin(accounts.kb_approver);
  const publisherToken = await apiLogin(accounts.kb_publisher);

  // 上传（multipart）
  const form = new FormData();
  form.append("files", new Blob([MARKDOWN], { type: "text/markdown" }), `chain5-${RUN_ID}.md`);
  form.append("metadata", JSON.stringify([{ title: TITLE, tags: [] }]));
  const upload = await fetch(`${accounts.e2e_base_url}${API}/school/kb/documents`, {
    method: "POST",
    headers: { Authorization: `Bearer ${uploaderToken}`, "Idempotency-Key": crypto.randomUUID() },
    body: form,
  });
  if (upload.status !== 201) {
    console.error("[UPLOAD]", upload.status, await upload.clone().text());
  }
  expect(upload.status).toBe(201);
  const doc = (
    (await upload.json()) as {
      documents: { id: string; title: string; current_version: { id: string } }[];
    }
  ).documents[0];
  const documentId = doc.id;
  const versionId = doc.current_version.id;

  // 提交审批 → 审批人通过
  const submit = await apiCall(
    uploaderToken,
    `${API}/school/kb/document-versions/${versionId}/submit-approval`,
    {
      method: "POST",
      body: "{}",
      contentType: "application/json",
    },
  );
  expect(submit.status).toBe(201);
  const requestId = (submit.json as { request_id: string }).request_id;
  const approve = await apiCall(approverToken, `${API}/school/approvals/${requestId}/approve`, {
    method: "POST",
    body: "{}",
    contentType: "application/json",
  });
  expect(approve.status).toBe(200);

  // ACL（all）确认
  const put = await apiCall(
    publisherToken,
    `${API}/school/kb/document-versions/${versionId}/audiences`,
    {
      method: "PUT",
      body: JSON.stringify({ audiences: [{ audience_type: "all", audience_id: null }] }),
      contentType: "application/json",
    },
  );
  expect(put.status).toBe(200);
  const confirm = await apiCall(
    publisherToken,
    `${API}/school/kb/document-versions/${versionId}/audiences/confirm`,
    { method: "POST", body: "{}", contentType: "application/json" },
  );
  expect(confirm.status).toBe(200);

  // 构建到 READY
  const build = await apiCall(
    publisherToken,
    `${API}/school/kb/document-versions/${versionId}/build`,
    {
      method: "POST",
      body: "{}",
      contentType: "application/json",
    },
  );
  expect(build.status).toBe(201);
  for (let i = 0; i < 60; i++) {
    const detail = await apiCall<{ status: string }>(
      publisherToken,
      `${API}/school/kb/documents/${documentId}`,
    );
    if (detail.json?.status === "READY") break;
    await new Promise((r) => setTimeout(r, 1000));
  }

  // 测试集 CSV（无 expected_chunk_ids → 默认通过）→ 发起运行 → TEST_PASSED
  const csv = "question\n差旅报销需要什么材料？\n";
  const csvForm = new FormData();
  csvForm.append("file", new Blob([csv], { type: "text/csv" }), `cases-${RUN_ID}.csv`);
  csvForm.append("name", `链⑤测试集 ${RUN_ID}`);
  const testSet = await fetch(
    `${accounts.e2e_base_url}${API}/school/kb/versions/${versionId}/test-sets`,
    { method: "POST", headers: { Authorization: `Bearer ${publisherToken}` }, body: csvForm },
  );
  expect(testSet.status).toBe(201);
  const testSetId = ((await testSet.json()) as { test_set_id: string }).test_set_id;
  const runForm = new FormData();
  runForm.append("test_set_id", testSetId);
  const run = await fetch(`${accounts.e2e_base_url}${API}/school/kb/versions/${versionId}/tests`, {
    method: "POST",
    headers: { Authorization: `Bearer ${publisherToken}` },
    body: runForm,
  });
  expect([201, 202]).toContain(run.status);
  let passed = false;
  for (let i = 0; i < 90; i++) {
    const detail = await apiCall<{ status: string }>(
      publisherToken,
      `${API}/school/kb/documents/${documentId}`,
    );
    if (detail.json?.status === "TEST_PASSED") {
      passed = true;
      break;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  expect(passed, "KB 未到 TEST_PASSED").toBe(true);

  // 发布
  const publish = await apiCall(publisherToken, `${API}/school/kb/versions/${versionId}/publish`, {
    method: "POST",
    body: "{}",
    contentType: "application/json",
  });
  expect(publish.status).toBe(200);
});

test("链⑤：学生登录 → 提问 → 流式/兜底 → 反馈提交 → 清空 → 该会话消息为空", async ({ page }) => {
  // 专用聊天学生（种子 `campus_chat`）：不强制改密 → 本链可重复运行；
  // 首登强改密弧由 campus-fullstack.spec 用 `student` 覆盖。
  const student = accounts.campus_chat;
  await page.goto("/login");
  await field(page, "school-code").fill(student.school_code);
  await field(page, "login-id").fill(student.login_id);
  await field(page, "password").fill(student.password);
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("go-chat")).toBeVisible({ timeout: 15_000 });

  await page.getByTestId("go-chat").click();
  await page.getByTestId("chat-input").locator("textarea").fill("差旅报销怎么办理？");
  await page.getByTestId("send-button").click();

  // 终态三分支：真流式（FINAL_ANSWER+引用）/兜底（CAPABILITY_FALLBACK）/未配 LLM（error）。
  // 必须先等 data-status 离开 streaming——正文首个 delta 就含关键词，只断言文字会在
  // 流式途中通过，后续反馈入口/引用的可见性判断全部落空。
  const messages = page.getByTestId("messages");
  const terminalBubble = messages.locator(
    '[data-role="assistant"][data-status="final"], [data-role="assistant"][data-status="fallback"], [data-role="assistant"][data-status="error"]',
  );
  await expect(terminalBubble).toBeVisible({ timeout: 60_000 });
  await expect(messages).toContainText(/差旅报销|抱歉|回答失败/);

  // 引用抽屉（仅真流式弧有 citation）——可选断言：有引用按钮才打开
  const citationButton = page.getByTestId("citation-button");
  if (await citationButton.isVisible().catch(() => false)) {
    await citationButton.click();
    const drawer = page.getByTestId("citation-drawer");
    await expect(drawer).toContainText("财务报销手册");
    // 关闭抽屉并等遮罩退场：van-popup 默认无 Esc 关闭，点遮罩才是真实关闭路径；
    // 关闭有淡出动画，遮罩仍在会拦截后续点击。
    await page.locator(".van-overlay").click({ position: { x: 10, y: 10 } });
    await expect(drawer).toBeHidden();
  }

  // 反馈提交（真库：断言 POST=201 首次落库；UI 侧断言「已反馈」标记）
  const fbRow = page.getByTestId("feedback-row");
  const hasFeedback = await fbRow.isVisible().catch(() => false);
  test.skip(!hasFeedback, "该终态无反馈入口（error 非末条/无 turn_id）");
  await page.getByTestId("fb-unhelpful").click();
  await page.getByTestId("feedback-form").getByText("内容不准确").click();
  const feedbackPost = page.waitForResponse(
    (r) => r.url().includes("/api/v1/campus/feedback") && r.request().method() === "POST",
  );
  await page.getByTestId("feedback-submit").click();
  expect((await feedbackPost).status()).toBe(201); // 201=首次写入（200 才是幂等重放）
  await expect(page.getByTestId("feedback-done")).toContainText("无用");

  // 清空 → 该会话消息为空（AI-07：软删消息，会话行保留——spec §AI-06/07）。
  // 断言真实 DELETE 响应里 cleared>0：证明本轮消息确实被软删，而不是「列表还有行」。
  await page.getByTestId("conversations-link").click();
  const clearDelete = page.waitForResponse(
    (r) => r.request().method() === "DELETE" && r.url().includes("/messages"),
  );
  await page.getByTestId("clear-button").first().click();
  await page.getByRole("button", { name: "确认" }).click();
  const cleared = await clearDelete;
  expect(cleared.status()).toBe(200);
  expect(((await cleared.json()) as { cleared: number }).cleared).toBeGreaterThan(0);

  // 打开刚清空的会话（列表首条 = 本轮最新）：消息区为空（只剩待机精灵）
  await page.locator('[data-testid^="conversation-"]').first().click();
  await expect(page.getByTestId("idle-mascot")).toBeVisible({ timeout: 15_000 });
});
