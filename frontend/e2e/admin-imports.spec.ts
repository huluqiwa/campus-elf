import { expect, test } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";
import { mockOrgApi } from "./helpers/org-mock";

// M2-UI1 批量导入（接口 mock）：文件预检、上传轮询到完成、错误行展示。

const IMPORT_PERMISSIONS = ["school.personnel.import"];

test("文件预检：非 csv/xlsx 提示且不发起上传", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: IMPORT_PERMISSIONS } });
  const { captures } = await mockOrgApi(page);

  await page.goto("/imports");
  await page.locator("input[type=file]").setInputFiles({
    name: "teachers.exe",
    mimeType: "application/octet-stream",
    buffer: Buffer.from("junk"),
  });
  await page.getByTestId("import-submit").click();

  await expect(page.getByText("仅支持 .csv / .xlsx")).toBeVisible();
  expect(captures.createTeacher).toHaveLength(0); // 未发起任何上传（org mock 计数为 0 佐证）
});

test("上传成功：轮询到 completed 展示计数；失败行展示错误码", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: IMPORT_PERMISSIONS } });
  await mockOrgApi(page, {
    overrides: {
      "**/api/v1/school/imports/j-1": {
        status: 200,
        body: {
          id: "j-1",
          type: "teachers",
          conflict: "error",
          status: "completed_with_errors",
          total_rows: 3,
          success_rows: 2,
          failed_rows: 1,
          skipped_rows: 0,
          retried_by: null,
          created_at: "2026-10-26T00:00:00Z",
          started_at: null,
          finished_at: null,
          message: null,
        },
      },
      "**/api/v1/school/imports/j-1/errors*": {
        status: 200,
        body: {
          items: [{ row_no: 2, field: "employee_no", error_code: "ORG_LOGIN_TAKEN", message: "登录名已被占用" }],
          next_cursor: null,
        },
      },
    },
  });

  await page.goto("/imports");
  await page.locator("input[type=file]").setInputFiles({
    name: "teachers.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("工号,姓名\nE-1,张老师\nE-2,李老师\nE-1,王老师"),
  });
  await page.getByTestId("import-submit").click();

  await expect(page.getByText("completed_with_errors")).toBeVisible({ timeout: 5000 });
  await expect(page.getByText("ORG_LOGIN_TAKEN")).toBeVisible();
  await expect(page.getByRole("button", { name: "对失败行发起重试" })).toBeVisible();
});
