import { expect, test } from "@playwright/test";

import { field } from "../e2e/helpers/locators";
import { loadAccounts } from "./helpers/support";

// M1 验收（TD-110）全栈 E2E：三端登录真实链路（浏览器 → vite preview 代理 → 真实后端容器）。
// 无接口 mock；账号来自 backend/scripts/e2e_seed.py 产出的种子数据。

test("平台运营真实登录并登出", async ({ page }) => {
  const accounts = loadAccounts();

  await page.goto("/login");
  await page.getByText("平台运营", { exact: true }).click();
  await field(page, "account").fill(accounts.platform.account);
  await field(page, "password").fill(accounts.platform.password);
  await page.getByTestId("login-submit").click();

  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId("identity")).toContainText("平台运营");

  await page.getByTestId("logout").click();
  await expect(page).toHaveURL(/\/login$/);
});

test("校方运营管理员真实登录", async ({ page }) => {
  const accounts = loadAccounts();

  await page.goto("/login");
  await field(page, "school-code").fill(accounts.school_ops.school_code);
  await field(page, "login-id").fill(accounts.school_ops.login_id);
  await field(page, "password").fill(accounts.school_ops.password);
  await page.getByTestId("login-submit").click();

  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId("identity")).toContainText("校方");
});

test("错误口令提示统一文案且不跳转", async ({ page }) => {
  const accounts = loadAccounts();

  await page.goto("/login");
  await field(page, "school-code").fill(accounts.school_ops.school_code);
  await field(page, "login-id").fill(accounts.school_ops.login_id);
  await field(page, "password").fill("E2E-Wrong-Pass-9");
  await page.getByTestId("login-submit").click();

  await expect(page.getByTestId("login-error")).toContainText("不正确");
  await expect(page).toHaveURL(/\/login$/);
});
