import { expect, test } from "@playwright/test";

import { field } from "../e2e/helpers/locators";
import { loadAccounts } from "./helpers/support";

// M1 验收（TD-110）全栈 E2E：C 端（campus，Vant H5）真实登录链路。
// 教师（正常状态）与学生（must_change_password=true 种子）各覆盖一条真实链路。

test("教师真实登录 C 端并登出", async ({ page }) => {
  const accounts = loadAccounts();

  await page.goto("/login");
  await field(page, "school-code").fill(accounts.teacher.school_code);
  await field(page, "login-id").fill(accounts.teacher.login_id);
  await field(page, "password").fill(accounts.teacher.password);
  await page.getByTestId("login-submit").click();

  await expect(page.getByText("C 端 H5 脚手架")).toBeVisible();
  // 真实后端健康检查经 preview 代理返回（不再显示「后端未连接」）
  await expect(page.getByTestId("logout")).toBeVisible();

  await page.getByTestId("logout").click();
  await expect(page).toHaveURL(/\/login$/);
});

test("学生首登强制改密：登录→强制改密→进入首页", async ({ page }) => {
  const accounts = loadAccounts();
  const NEW_PASSWORD = "E2E-Learner-New-Pass1";

  await page.goto("/login");
  await field(page, "school-code").fill(accounts.student.school_code);
  await field(page, "login-id").fill(accounts.student.login_id);
  await field(page, "password").fill(accounts.student.password);
  await page.getByTestId("login-submit").click();

  await expect(page).toHaveURL(/\/change-password$/);
  await expect(page.getByTestId("force-hint")).toBeVisible();

  await field(page, "current-password").fill(accounts.student.password);
  await field(page, "new-password").fill(NEW_PASSWORD);
  await field(page, "confirm-password").fill(NEW_PASSWORD);
  await page.getByTestId("change-submit").click();

  await expect(page.getByText("C 端 H5 脚手架")).toBeVisible();
});

test("学生错误口令提示且不跳转", async ({ page }) => {
  const accounts = loadAccounts();

  await page.goto("/login");
  await field(page, "school-code").fill(accounts.student.school_code);
  await field(page, "login-id").fill(accounts.student.login_id);
  await field(page, "password").fill("E2E-Wrong-Pass-9");
  await page.getByTestId("login-submit").click();

  await expect(page.getByTestId("login-error")).toContainText("不正确");
  await expect(page).toHaveURL(/\/login$/);
});
