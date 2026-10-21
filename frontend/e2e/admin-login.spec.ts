import { expect, test } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";
import { field } from "./helpers/locators";

// M1-C-UI 主路径（接口 mock）：登录、首登强制改密、错误文案、未登录跳转。

test("未登录访问首页跳转到登录页", async ({ page }) => {
  await mockAuth(page, { authenticated: false });

  await page.goto("/");

  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByTestId("login-submit")).toBeVisible();
});

test("校方首登：登录后强制跳改密页，改密成功进入首页", async ({ page }) => {
  await mockAuth(page, {
    authenticated: false,
    mustChangePasswordOnLogin: true,
    user: { realm: "tenant", mustChangePassword: true },
  });

  await page.goto("/login");
  await field(page, "school-code").fill("SCH-TEST0001");
  await field(page, "login-id").fill("S-9001");
  await field(page, "password").fill("init-password-1");
  await page.getByTestId("login-submit").click();

  await expect(page).toHaveURL(/\/change-password$/);
  await expect(page.getByTestId("force-hint")).toBeVisible();

  await field(page, "current-password").fill("init-password-1");
  await field(page, "new-password").fill("New-Password-123");
  await field(page, "confirm-password").fill("New-Password-123");
  await page.getByTestId("change-submit").click();

  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId("identity")).toBeVisible();
});

test("平台运营登录：切换身份后直接进入首页", async ({ page }) => {
  await mockAuth(page, {
    authenticated: false,
    user: { realm: "platform", tenantId: null, roles: ["platform_super_admin"] },
  });

  await page.goto("/login");
  // Element Plus 的 radio input 视觉隐藏，点击可点的标签文本更稳
  await page.getByText("平台运营", { exact: true }).click();
  await field(page, "account").fill("ops");
  await field(page, "password").fill("platform-password-1");
  await page.getByTestId("login-submit").click();

  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId("identity")).toContainText("平台运营");
});

test("密码错误展示统一文案且不跳转", async ({ page }) => {
  await mockAuth(page, { authenticated: false, loginFails: true });

  await page.goto("/login");
  await field(page, "school-code").fill("SCH-TEST0001");
  await field(page, "login-id").fill("S-9001");
  await field(page, "password").fill("wrong-password");
  await page.getByTestId("login-submit").click();

  await expect(page.getByTestId("login-error")).toContainText("学校编号、账号或密码不正确");
  await expect(page).toHaveURL(/\/login$/);
});
