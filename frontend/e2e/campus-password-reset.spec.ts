import { expect, test } from "@playwright/test";

import { mockAuth, mockPasswordReset } from "./helpers/auth-mock";
import { field } from "./helpers/locators";

// M1-D1 C 端密码重置（接口 mock）：统一提示、成功回登录、无效令牌错误。

test("C 端忘记密码：提交学校编号与学号后展示统一提示", async ({ page }) => {
  await mockAuth(page, { authenticated: false });
  const reset = await mockPasswordReset(page);

  await page.goto("/forgot-password");
  await expect(page.getByTestId("student-reset-notice")).toContainText("学生账号暂不支持自助找回");
  await field(page, "school-code").fill("SCH-TEST0001");
  await field(page, "login-id").fill("S-9001");
  await page.getByTestId("reset-request-submit").click();

  await expect(page.getByTestId("reset-request-success")).toBeVisible();
  expect(reset.requests).toEqual([
    { realm: "tenant", school_code: "SCH-TEST0001", login_id: "S-9001" },
  ]);
});

test("C 端重置密码：客户端按 Unicode 口径拦截弱密码，不发确认请求", async ({ page }) => {
  await mockAuth(page, { authenticated: false });
  const reset = await mockPasswordReset(page);

  await page.goto("/reset-password#token=v1rt.test-secret");
  // 中文属于 alnum，不算符号：这里只有小写 + 数字两类
  await field(page, "reset-new-password").fill("密码密码密码abcd1");
  await field(page, "reset-confirm-password").fill("密码密码密码abcd1");
  await page.getByTestId("reset-confirm-submit").click();

  await expect(page.getByTestId("reset-confirm-error")).toContainText("3 类");
  expect(reset.confirms).toEqual([]);
});

test("C 端重置密码成功：回到登录页", async ({ page }) => {
  await mockAuth(page, { authenticated: false });
  const reset = await mockPasswordReset(page);

  await page.goto("/reset-password#token=v1rt.test-secret");
  await field(page, "reset-new-password").fill("New-Password-123");
  await field(page, "reset-confirm-password").fill("New-Password-123");
  await page.getByTestId("reset-confirm-submit").click();

  await expect(page).toHaveURL(/\/login$/);
  expect(reset.confirms).toEqual([{ token: "v1rt.test-secret", new_password: "New-Password-123" }]);
});

test("C 端重置密码失败：无效令牌展示后端文案且不跳转", async ({ page }) => {
  await mockAuth(page, { authenticated: false });
  await mockPasswordReset(page, {
    confirmStatus: 400,
    confirmErrorMessage: "重置链接无效或已过期",
  });

  await page.goto("/reset-password#token=expired-token");
  await field(page, "reset-new-password").fill("New-Password-123");
  await field(page, "reset-confirm-password").fill("New-Password-123");
  await page.getByTestId("reset-confirm-submit").click();

  await expect(page.getByTestId("reset-confirm-error")).toContainText("重置链接无效或已过期");
  await expect(page).toHaveURL(/\/reset-password/);
});
