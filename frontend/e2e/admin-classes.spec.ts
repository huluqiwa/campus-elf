import { expect, test } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";
import { field } from "./helpers/locators";
import { FIXTURE_CLASS_ARCHIVED, mockOrgApi } from "./helpers/org-mock";

// M2-UI1 班级与课程（接口 mock）：列表/归档切换/建班/同届重名 409 提示/排课。

const MANAGE_PERMISSIONS = ["school.personnel.view", "school.org.manage"];

test("班级列表：默认只显示在用届，切换后归档届可见", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: MANAGE_PERMISSIONS } });
  await mockOrgApi(page, { classes: [FIXTURE_CLASS_ARCHIVED] });

  await page.goto("/classes");
  await expect(page.getByTestId("class-table")).toBeVisible();
  await expect(page.getByRole("row", { name: /高三/ })).toHaveCount(0);

  await page.getByText("显示归档届").click();
  await expect(page.getByRole("row", { name: /已归档/ })).toBeVisible();
});

test("建班：提交入学年份+当前年级+班名", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: MANAGE_PERMISSIONS } });
  const { captures } = await mockOrgApi(page);

  await page.goto("/classes");
  await page.getByTestId("class-create").click();
  await field(page, "class-grade").fill("高一");
  await field(page, "class-name").fill("2班");
  await page.getByTestId("class-submit").click();

  expect(captures.createClass[0]).toMatchObject({ entry_year: 2026, grade: "高一", name: "2班" });
});

test("同届重名 409：提示失败且弹窗不关闭", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: MANAGE_PERMISSIONS } });
  await mockOrgApi(page, {
    overrides: {
      "**/api/v1/school/classes": {
        status: 409,
        body: { detail: "同入学年份下班级名已存在（同届班名唯一）" },
      },
    },
  });

  await page.goto("/classes");
  await page.getByTestId("class-create").click();
  await field(page, "class-grade").fill("高一");
  await field(page, "class-name").fill("1班");
  await page.getByTestId("class-submit").click();

  await expect(page.getByText("同入学年份下班级名已存在", { exact: false })).toBeVisible();
  await expect(page.getByTestId("class-submit")).toBeVisible(); // 弹窗仍在
});

test("排课：教师/班级/课程/学期齐全后提交", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: MANAGE_PERMISSIONS } });
  await mockOrgApi(page);

  await page.goto("/classes");
  await page.getByRole("tab", { name: "任教关系" }).click();
  await page.getByRole("button", { name: "新排课" }).click();
  await expect(page.locator(".el-dialog")).toBeVisible();

  // 三个下拉逐个选第一项：键盘导航最稳（popper 复用导致 option 点击时序不稳）
  const selects = page.locator(".el-dialog .el-select");
  for (let i = 0; i < 3; i += 1) {
    await selects.nth(i).click();
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
  }
  await page.locator(".el-dialog input[placeholder='如：2026春']").fill("2026春");
  await page.locator(".el-dialog").getByRole("button", { name: "确认排课" }).click();

  await expect(page.getByRole("cell", { name: "2026春" })).toBeVisible();
});
