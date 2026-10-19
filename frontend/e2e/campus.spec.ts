import { expect, test } from "@playwright/test";

test("C 端 H5 脚手架可渲染", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /C 端 H5 脚手架/ })).toBeVisible();
  await expect(page.getByText("后端健康")).toBeVisible();
});
