import { expect, test } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";
import { field } from "./helpers/locators";

// M1-D2 C 端（接口 mock）：已绑定账号登录走"密码 → 动态码"两步。
// C 端不做绑定入口（学生自助绑定延后），这里只覆盖登录校验步骤。

test("C 端两步验证登录：密码 → 动态码 → 进入对话页", async ({ page }) => {
  const handles = await mockAuth(page, {
    authenticated: false,
    mfaRequiredOnLogin: true,
    user: { realm: "tenant", mfaEnabled: true },
  });

  await page.goto("/login");
  await field(page, "school-code").fill("SCH-TEST0001");
  await field(page, "login-id").fill("S-9001");
  await field(page, "password").fill("student-password-1");
  await page.getByTestId("login-submit").click();

  await expect(page.getByTestId("mfa-code")).toBeVisible();
  await expect(page).toHaveURL(/\/login$/);

  await field(page, "mfa-code").fill("246810");
  await page.getByTestId("mfa-submit").click();

  await expect(page.getByText("C 端 H5 脚手架")).toBeVisible();
  expect(handles.verifyBodies).toEqual([{ mfa_token: "e2e-mfa-challenge-token", code: "246810" }]);
});

test("C 端动态码返回后可退回重填密码", async ({ page }) => {
  await mockAuth(page, {
    authenticated: false,
    mfaRequiredOnLogin: true,
    user: { realm: "tenant", mfaEnabled: true },
  });

  await page.goto("/login");
  await field(page, "school-code").fill("SCH-TEST0001");
  await field(page, "login-id").fill("S-9001");
  await field(page, "password").fill("student-password-1");
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("mfa-code")).toBeVisible();

  await page.getByTestId("mfa-back").click();
  await expect(page.getByTestId("password")).toBeVisible();
});
