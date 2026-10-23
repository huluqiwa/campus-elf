import { expect, test } from "@playwright/test";

import { field } from "../e2e/helpers/locators";
import { loadAccounts } from "./helpers/support";

// M1 验收（TD-110）全栈 E2E：首登强制改密真实链路。
// 种子超管由真实「注册→审核→开通」流程产生（must_change_password=true），
// 初始密码是开通邮件里的真实随机密码（seed 从 MailHog 提取后写入账号文件）。

const NEW_PASSWORD = "E2E-Admin-New-Pass-1";

test("首登强制改密：初始口令登录→强制改密→旧口令失效→新口令可用", async ({ page }) => {
  const accounts = loadAccounts();
  const forced = accounts.school_admin_forced;

  // 1) 初始口令登录 → 强制跳改密页
  await page.goto("/login");
  await field(page, "school-code").fill(forced.school_code);
  await field(page, "login-id").fill(forced.login_id);
  await field(page, "password").fill(forced.initial_password);
  await page.getByTestId("login-submit").click();

  await expect(page).toHaveURL(/\/change-password$/);
  await expect(page.getByTestId("force-hint")).toBeVisible();

  // 2) 真实改密 → 进入首页
  await field(page, "current-password").fill(forced.initial_password);
  await field(page, "new-password").fill(NEW_PASSWORD);
  await field(page, "confirm-password").fill(NEW_PASSWORD);
  await page.getByTestId("change-submit").click();

  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId("identity")).toContainText("校方");

  // 3) 登出后旧口令必须失效（密码历史生效）
  await page.getByTestId("logout").click();
  await expect(page).toHaveURL(/\/login$/);

  await field(page, "school-code").fill(forced.school_code);
  await field(page, "login-id").fill(forced.login_id);
  await field(page, "password").fill(forced.initial_password);
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("login-error")).toContainText("不正确");
  await expect(page).toHaveURL(/\/login$/);

  // 4) 新口令可用
  await field(page, "school-code").fill(forced.school_code);
  await field(page, "login-id").fill(forced.login_id);
  await field(page, "password").fill(NEW_PASSWORD);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId("identity")).toContainText("校方");
});
