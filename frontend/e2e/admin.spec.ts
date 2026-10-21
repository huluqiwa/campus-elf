import { expect, test } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";

test("已登录访问管理后台首页展示身份与健康状态", async ({ page }) => {
  await mockAuth(page);

  await page.goto("/");

  await expect(page.getByRole("heading", { name: /管理后台/ })).toBeVisible();
  await expect(page.getByTestId("identity")).toContainText("校方");
  await expect(page.getByText("CampusCompanionElf 0.1.0 (e2e)")).toBeVisible();
});
