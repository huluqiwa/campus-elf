import { expect, test } from "@playwright/test";

test("管理端脚手架可渲染", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /管理端脚手架/ })).toBeVisible();
  await expect(page.getByText("后端健康检查")).toBeVisible();
});
