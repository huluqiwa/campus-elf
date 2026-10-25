import { expect, test } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";
import { field } from "./helpers/locators";
import { mockOrgApi } from "./helpers/org-mock";

// M2-UI1 部门树（接口 mock）：树形渲染、建根部门、删除 409 原因透出。

const VIEW_PERMISSIONS = ["school.personnel.view"];

test("部门树渲染父子层级并支持新建根部门", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: VIEW_PERMISSIONS } });
  await mockOrgApi(page);

  await page.goto("/departments");
  await expect(page.getByTestId("department-tree")).toBeVisible();
  await expect(page.getByText("校办（在用）")).toBeVisible();

  await page.getByTestId("dept-create").click();
  await field(page, "dept-name").fill("教务处");
  await page.getByTestId("dept-submit").click();

  await expect(page.getByText("教务处（在用）")).toBeVisible();
});

test("删除失败（409）把后端原因透出到提示", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: VIEW_PERMISSIONS } });
  await mockOrgApi(page);

  await page.goto("/departments");
  await page.getByRole("button", { name: "删除" }).first().click();
  // 删除有确认弹窗（M2-UI1 复审 Minor 3）：点确认后才发 DELETE
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
  await expect(page.getByText("有子部门或仍有教师归属时不可删", { exact: false })).toBeVisible();
});
