import { expect, test } from "@playwright/test";

import { field } from "../e2e/helpers/locators";
import { extractResetToken, fetchLatestMailBody, loadAccounts } from "./helpers/support";

// M1 验收（TD-110）全栈 E2E：忘记密码 → MailHog 真实邮件 → 重置 → 新口令登录。
// 使用重置专用账号（resetter），避免影响其他用例的口令状态。

const NEW_PASSWORD = "E2E-Reset-New-Pass-1";

test("忘记密码：真实邮件送达→按邮件令牌重置→新口令登录", async ({ page }) => {
  const accounts = loadAccounts();
  const resetter = accounts.resetter;

  // 1) 登录页进入忘记密码，提交重置请求（真实 SMTP → MailHog）
  await page.goto("/login");
  await page.getByTestId("forgot-password-link").click();
  await expect(page).toHaveURL(/\/forgot-password$/);

  await field(page, "school-code").fill(resetter.school_code);
  await field(page, "login-id").fill(resetter.login_id);
  await page.getByTestId("reset-request-submit").click();
  await expect(page.getByTestId("reset-request-success")).toBeVisible();

  // 2) 从 MailHog 真实邮件提取重置令牌（链接 fragment #token=…）
  const body = await fetchLatestMailBody(accounts, resetter.email);
  const token = extractResetToken(body);

  // 3) 打开重置页（邮件里的地址指向 preview 的 4173，这里用 baseURL 保证环境一致）
  await page.goto(`/reset-password#token=${token}`);
  await field(page, "reset-new-password").fill(NEW_PASSWORD);
  await field(page, "reset-confirm-password").fill(NEW_PASSWORD);
  await page.getByTestId("reset-confirm-submit").click();

  // 4) 重置成功回登录页，新口令可用
  await expect(page).toHaveURL(/\/login$/);
  await field(page, "school-code").fill(resetter.school_code);
  await field(page, "login-id").fill(resetter.login_id);
  await field(page, "password").fill(NEW_PASSWORD);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId("identity")).toContainText("校方");
});
