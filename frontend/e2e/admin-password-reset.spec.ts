import { expect, test } from "@playwright/test";

import { mockAuth, mockPasswordReset } from "./helpers/auth-mock";
import { field } from "./helpers/locators";

// M1-D1 管理端密码重置（接口 mock）：统一提示、成功回登录、无效令牌错误。

test("忘记密码：校方账号提交后只展示统一提示", async ({ page }) => {
  await mockAuth(page, { authenticated: false });
  const reset = await mockPasswordReset(page);

  await page.goto("/forgot-password");
  await field(page, "school-code").fill("SCH-TEST0001");
  await field(page, "login-id").fill("S-9001");
  await page.getByTestId("reset-request-submit").click();

  await expect(page.getByTestId("reset-request-success")).toBeVisible();
  expect(reset.requests).toEqual([
    { realm: "tenant", school_code: "SCH-TEST0001", login_id: "S-9001" },
  ]);
});

test("忘记密码：平台运营切换身份后提交账号", async ({ page }) => {
  await mockAuth(page, { authenticated: false });
  const reset = await mockPasswordReset(page);

  await page.goto("/forgot-password");
  await page.getByText("平台运营", { exact: true }).click();
  await field(page, "account").fill("ops");
  await page.getByTestId("reset-request-submit").click();

  await expect(page.getByTestId("reset-request-success")).toBeVisible();
  expect(reset.requests).toEqual([{ realm: "platform", account: "ops" }]);
});

test("重置密码成功：提交令牌与新密码后回到登录页", async ({ page }) => {
  await mockAuth(page, { authenticated: false });
  const reset = await mockPasswordReset(page);

  await page.goto("/reset-password#token=v1rt.test-secret");
  await field(page, "reset-new-password").fill("New-Password-123");
  await field(page, "reset-confirm-password").fill("New-Password-123");
  await page.getByTestId("reset-confirm-submit").click();

  await expect(page).toHaveURL(/\/login$/);
  expect(reset.confirms).toEqual([{ token: "v1rt.test-secret", new_password: "New-Password-123" }]);
});

test("重置密码失败：无效令牌展示后端文案且不跳转", async ({ page }) => {
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

test("重置密码：链接缺少令牌时禁用提交并提示", async ({ page }) => {
  await mockAuth(page, { authenticated: false });
  await mockPasswordReset(page);

  await page.goto("/reset-password");

  await expect(page.getByTestId("reset-confirm-error")).toContainText("缺少令牌");
  await expect(page.getByTestId("reset-confirm-submit")).toBeDisabled();
});

test("首登未改密：仍可打开邮件里的重置链接", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { mustChangePassword: true } });
  await mockPasswordReset(page);

  await page.goto("/reset-password#token=v1rt.test-secret");

  await expect(page.getByTestId("reset-new-password")).toBeVisible();
  await expect(page).toHaveURL(/\/reset-password/);
});
