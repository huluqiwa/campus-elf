import { expect, test } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";
import { field } from "./helpers/locators";

// C 端登录主路径（接口 mock）：登录成功进入首页；首登强制改密。

test("C 端登录成功进入首页", async ({ page }) => {
  await mockAuth(page, {
    authenticated: false,
    user: { realm: "tenant", roles: ["student"] },
  });

  await page.goto("/login");
  await field(page, "school-code").fill("SCH-TEST0001");
  await field(page, "login-id").fill("S-9001");
  await field(page, "password").fill("student-password-1");
  await page.getByTestId("login-submit").click();

  await expect(page.getByText("C 端 H5 脚手架")).toBeVisible();
});

test("C 端首登先改密，再进入首页", async ({ page }) => {
  await mockAuth(page, {
    authenticated: false,
    mustChangePasswordOnLogin: true,
    user: { realm: "tenant", roles: ["student"], mustChangePassword: true },
  });

  await page.goto("/login");
  await field(page, "school-code").fill("SCH-TEST0001");
  await field(page, "login-id").fill("S-9001");
  await field(page, "password").fill("student-init-1");
  await page.getByTestId("login-submit").click();

  await expect(page.getByTestId("force-hint")).toBeVisible();
  await field(page, "current-password").fill("student-init-1");
  await field(page, "new-password").fill("Student-New-Pass-1");
  await field(page, "confirm-password").fill("Student-New-Pass-1");
  await page.getByTestId("change-submit").click();

  await expect(page.getByText("C 端 H5 脚手架")).toBeVisible();
});
