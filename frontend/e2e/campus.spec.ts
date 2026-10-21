import { expect, test } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";

test("已登录访问 C 端首页展示脚手架与身份", async ({ page }) => {
  await mockAuth(page, { user: { realm: "tenant", roles: ["student"] } });

  await page.goto("/");

  await expect(page.getByText("C 端 H5 脚手架")).toBeVisible();
  await expect(page.getByText("CampusCompanionElf 0.1.0 (e2e)")).toBeVisible();
});
