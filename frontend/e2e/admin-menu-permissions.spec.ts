import { expect, test } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";
import { mockOrgApi } from "./helpers/org-mock";

// TD-141 收口（M2-UI1）：菜单按 /auth/me 的 permissions 渲染；直连无权限路由 → 403 页。

test("无任何 school 权限：业务菜单全部隐藏（平台运营视角）", async ({ page }) => {
  await mockAuth(page, {
    authenticated: true,
    user: { realm: "platform", tenantId: null, permissions: [] },
  });
  await mockOrgApi(page);

  await page.goto("/");
  await expect(page.getByRole("menuitem", { name: "人员管理" })).toHaveCount(0);
  await expect(page.getByRole("menuitem", { name: "学年流转" })).toHaveCount(0);
});

test("部分权限：只渲染对应菜单项（缺 import/manage 的入口不可见）", async ({ page }) => {
  await mockAuth(page, {
    authenticated: true,
    user: { permissions: ["school.personnel.view"] },
  });
  await mockOrgApi(page);

  await page.goto("/");
  await expect(page.getByRole("menuitem", { name: "人员管理" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "班级与课程" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "部门树" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "批量导入" })).toHaveCount(0);
  await expect(page.getByRole("menuitem", { name: "学年流转" })).toHaveCount(0);
});

test("直连无权限路由 → 403 页（后端 403 之外的前端体验层）", async ({ page }) => {
  await mockAuth(page, {
    authenticated: true,
    user: { permissions: ["school.personnel.view"] },
  });
  await mockOrgApi(page);

  await page.goto("/rollover");
  await expect(page.getByText("你没有访问该页面的权限")).toBeVisible();
});
