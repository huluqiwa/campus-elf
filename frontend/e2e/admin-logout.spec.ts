import { expect, test } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";

test("退出登录：清空前端状态并回到登录页，之后受保护页不可访问", async ({ page }) => {
  // 使用有状态 mock：调用 /auth/logout 会把会话置为未登录
  await mockAuth(page, { authenticated: true });

  await page.goto("/");
  await expect(page.getByTestId("identity")).toBeVisible();

  await page.getByTestId("logout").click();

  await expect(page).toHaveURL(/\/login$/);

  // 登出后再访问受保护首页：被守卫拦回登录页（refresh 已 401）
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
});
