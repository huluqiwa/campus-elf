import { expect, test } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";
import { field } from "./helpers/locators";

// M1-D2 主路径（接口 mock）：两步验证登录、平台强制绑定、安全设置页开/关。

test("平台运营两步验证登录：密码 → 动态码 → 进入首页", async ({ page }) => {
  const handles = await mockAuth(page, {
    authenticated: false,
    mfaRequiredOnLogin: true,
    user: { realm: "platform", tenantId: null, roles: ["platform_super_admin"], mfaEnabled: true },
  });

  await page.goto("/login");
  await page.getByText("平台运营", { exact: true }).click();
  await field(page, "account").fill("ops");
  await field(page, "password").fill("platform-password-1");
  await page.getByTestId("login-submit").click();

  // 密码通过但只有挑战令牌：进入第二步，仍停在登录页
  await expect(page.getByTestId("mfa-code")).toBeVisible();
  await expect(page).toHaveURL(/\/login$/);

  await field(page, "mfa-code").fill("123456");
  await page.getByTestId("mfa-submit").click();

  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId("identity")).toContainText("平台运营");
  // 断言确实带着挑战令牌 + 动态码命中第二步接口，而非只看跳转
  expect(handles.verifyBodies).toEqual([{ mfa_token: "e2e-mfa-challenge-token", code: "123456" }]);
});

test("平台强制绑定：登录后进入绑定页，扫码激活后展示恢复码", async ({ page }) => {
  const handles = await mockAuth(page, {
    authenticated: false,
    mfaEnrollmentRequiredOnLogin: true,
    user: { realm: "platform", tenantId: null, roles: ["platform_super_admin"] },
  });

  await page.goto("/login");
  await page.getByText("平台运营", { exact: true }).click();
  await field(page, "account").fill("ops");
  await field(page, "password").fill("platform-password-1");
  await page.getByTestId("login-submit").click();

  // 强制绑定页：无会话，用一次性绑定令牌取密钥
  await expect(page).toHaveURL(/\/mfa\/enroll$/);
  await expect(page.getByTestId("mfa-qr")).toBeVisible();
  await expect(page.getByTestId("mfa-secret")).toHaveText("JBSWY3DPEHPK3PXP");

  await field(page, "mfa-activate-code").fill("123456");
  await page.getByTestId("mfa-activate-submit").click();

  // 激活成功后停留在本页展示一次性恢复码，确认后才跳转
  await expect(page.getByTestId("mfa-recovery-code").first()).toBeVisible();
  await page.getByTestId("mfa-enroll-finish").click();
  await expect(page).toHaveURL(/\/$/);

  // 绑定阶段只允许用绑定令牌（此时没有会话访问令牌）
  expect(handles.enrollAuthHeaders).toEqual(["Bearer e2e-enroll-token"]);
  expect(handles.activateBodies).toEqual([{ code: "123456" }]);
});

test("安全设置页：已登录自助开启两步验证并可关闭", async ({ page }) => {
  const handles = await mockAuth(page, {
    authenticated: true,
    user: { realm: "platform", tenantId: null, mfaEnabled: false },
  });

  await page.goto("/security");
  await expect(page.getByTestId("mfa-status")).toContainText("未启用");

  // 开启：先验密码 → 取二维码 → 输动态码激活 → 恢复码 → 完成
  await field(page, "enable-password").fill("platform-password-1");
  await page.getByTestId("enable-start").click();
  await expect(page.getByTestId("mfa-qr")).toBeVisible();

  await field(page, "mfa-activate-code").fill("654321");
  await page.getByTestId("mfa-activate-submit").click();
  await expect(page.getByTestId("mfa-recovery-code").first()).toBeVisible();
  await page.getByTestId("enable-finish").click();

  await expect(page.getByTestId("mfa-status")).toContainText("已启用");

  // 关闭：密码 + 动态码
  await field(page, "disable-password").fill("platform-password-1");
  await field(page, "disable-code").fill("111111");
  await page.getByTestId("disable-submit").click();
  await expect(page.getByTestId("mfa-status")).toContainText("未启用");

  // 自助绑定用会话访问令牌；关闭请求带密码与动态码
  expect(handles.enrollAuthHeaders).toEqual(["Bearer e2e-access-token"]);
  expect(handles.disableBodies).toEqual([
    { current_password: "platform-password-1", code: "111111" },
  ]);
});

test("平台强制策略下不允许关闭两步验证", async ({ page }) => {
  await mockAuth(page, {
    authenticated: true,
    user: { realm: "platform", tenantId: null, mfaEnabled: true, mfaRequired: true },
  });

  await page.goto("/security");
  await expect(page.getByTestId("mfa-status")).toContainText("已启用");
  await expect(page.getByTestId("disable-blocked")).toBeVisible();
  await expect(page.getByTestId("disable-submit")).toHaveCount(0);
});
