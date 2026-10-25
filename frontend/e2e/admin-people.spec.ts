import { expect, test } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";
import { field } from "./helpers/locators";
import { FIXTURE_CLASS_ACTIVE, FIXTURE_STUDENT, mockOrgApi } from "./helpers/org-mock";

// M2-UI1 人员管理主链路（接口 mock）：列表/建档/初始密码/转班/受控查看（TD-141 权限渲染）。

const VIEW_PERMISSIONS = [
  "school.personnel.view",
  "school.personnel.create",
  "school.personnel.edit",
  "school.org.manage",
  "school.personnel.sensitive.read",
];

test("教师建档：提交后展示一次性初始密码并刷新列表", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: VIEW_PERMISSIONS } });
  const { captures } = await mockOrgApi(page);

  await page.goto("/people");
  await expect(page.getByTestId("teacher-table")).toBeVisible();
  await page.getByTestId("teacher-create").click();
  await field(page, "teacher-employee-no").fill("E-9002");
  await field(page, "teacher-name").fill("李老师");
  await page.getByTestId("teacher-submit").click();

  await expect(page.getByTestId("initial-password")).toBeVisible();
  await expect(page.getByTestId("initial-password")).toContainText("Init-Pass-123");
  expect(captures.createTeacher[0]).toMatchObject({ employee_no: "E-9002", name: "李老师" });
});

test("学生转班：选择目标班级提交后出现成功提示", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: VIEW_PERMISSIONS } });
  const { captures } = await mockOrgApi(page, {
    students: [{ ...FIXTURE_STUDENT, class_id: FIXTURE_CLASS_ACTIVE.id }],
  });

  await page.goto("/people");
  await page.getByRole("tab", { name: "学生" }).click();
  await expect(page.getByTestId("student-table")).toBeVisible();

  await page.getByRole("button", { name: "转班" }).first().click();
  await expect(page.getByTestId("transfer-submit")).toBeVisible();
  // 目标班级下拉：选当前唯一活跃班
  await page.locator(".el-dialog .el-select").first().click();
  await page.getByRole("option", { name: /1班/ }).first().click();
  await page.getByTestId("transfer-submit").click();

  expect(captures.transfer[0]).toMatchObject({ class_id: FIXTURE_CLASS_ACTIVE.id });
});

test("无 sensitive.read 权限：查看身份证入口不渲染（TD-141）", async ({ page }) => {
  await mockAuth(page, {
    authenticated: true,
    user: { permissions: ["school.personnel.view"] },
  });
  await mockOrgApi(page);

  await page.goto("/people");
  await expect(page.getByTestId("teacher-table")).toBeVisible();
  await expect(page.getByRole("button", { name: "查看身份证" })).toHaveCount(0);
});

test("有权限时受控查看：理由不足 10 字禁用提交，理由充分返回明文", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: VIEW_PERMISSIONS } });
  await mockOrgApi(page, {
    overrides: {
      "**/api/v1/school/personnel/*/id-card": {
        status: 200,
        body: { number: "110101199003077758", mask: "110***********7758" },
      },
    },
  });

  await page.goto("/people");
  await page.getByRole("button", { name: "查看身份证" }).first().click();

  const reasonBox = field(page, "id-card-reason");
  await reasonBox.fill("查一下");
  await expect(page.getByTestId("id-card-submit")).toBeDisabled();

  await reasonBox.fill("医保办理需要查看完整号码");
  await page.getByTestId("id-card-submit").click();
  await expect(page.getByTestId("id-card-number")).toHaveText("110101199003077758");
});
