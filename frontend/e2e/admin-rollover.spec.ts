import { expect, test } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";
import { field } from "./helpers/locators";
import { mockOrgApi } from "./helpers/org-mock";

// M2-UI1 学年流转向导（接口 mock）：空载荷禁用、预览渲染、二次确认执行。

const MANAGE_PERMISSIONS = ["school.personnel.view", "school.org.manage"];

test("未配置映射时预览按钮禁用", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: MANAGE_PERMISSIONS } });
  await mockOrgApi(page);

  await page.goto("/rollover");
  await expect(page.getByTestId("rollover-preview")).toBeDisabled();
});

test("预览渲染逐班清单，二次确认后执行并展示影响计数", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: MANAGE_PERMISSIONS } });
  const { captures } = await mockOrgApi(page);

  await page.goto("/rollover");
  await field(page, "mapping-from-0").fill("高一");
  await field(page, "mapping-to-0").fill("高二");
  await page.getByText("预览影响面").click();

  await expect(page.getByTestId("rollover-preview-advanced")).toBeVisible();
  await expect(page.getByRole("cell", { name: "高二" }).first()).toBeVisible();

  // 未勾选确认时执行禁用（不可逆操作二次确认）
  await expect(page.getByTestId("rollover-execute")).toBeDisabled();
  await page.getByTestId("rollover-confirm").click();
  await page.getByTestId("rollover-execute").click();

  await expect(page.getByText("学年流转完成")).toBeVisible();
  expect(captures.rollover[1]).toMatchObject({ dry_run: false });
  expect(captures.rollover[0]).toMatchObject({ dry_run: true });
});

test("归档年级与升级起点冲突时预检报错", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: MANAGE_PERMISSIONS } });
  await mockOrgApi(page);

  await page.goto("/rollover");
  await field(page, "mapping-from-0").fill("高三");
  await field(page, "mapping-to-0").fill("毕业");
  await page.locator("input[placeholder='输入年级后回车，如：高三']").fill("高三");
  await page.keyboard.press("Enter");

  await expect(page.getByText("请确认语义", { exact: false })).toBeVisible();
  await expect(page.getByTestId("rollover-preview")).toBeDisabled();
});

test("执行失败重试：幂等键跨重试稳定；重放返回 409 提示已执行过", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: MANAGE_PERMISSIONS } });
  await mockOrgApi(page);

  // 有状态覆盖：第一次 dry_run=false 返回 500（模拟响应丢失），第二次成功，第三次 409
  let executeCalls = 0;
  const executedBodies: Array<Record<string, unknown>> = [];
  await page.route("**/api/v1/school/classes/rollover", (route) => {
    const body = route.request().postDataJSON() as { dry_run?: boolean };
    if (body.dry_run !== false) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          dry_run: true,
          advanced: [{ id: "x", entry_year: 2025, name: "1班", from_grade: "高一", to_grade: "高二" }],
          archived: [],
          advanced_count: 1,
          archived_count: 0,
          rollover_id: null,
        }),
      });
    }
    executeCalls += 1;
    executedBodies.push(body);
    if (executeCalls === 1) {
      return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "响应丢失" }) });
    }
    if (executeCalls === 2) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          dry_run: false,
          advanced: [],
          archived: [],
          advanced_count: 1,
          archived_count: 0,
          rollover_id: "r-9",
        }),
      });
    }
    return route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: "该幂等键已执行过学年流转，请勿重复提交" }),
    });
  });

  await page.goto("/rollover");
  await field(page, "mapping-from-0").fill("高一");
  await field(page, "mapping-to-0").fill("高二");
  await page.getByText("预览影响面").click();
  await expect(page.getByTestId("rollover-preview-advanced")).toBeVisible();

  await page.getByTestId("rollover-confirm").click();
  await page.getByTestId("rollover-execute").click(); // 第一次：500
  await page.getByTestId("rollover-execute").click(); // 重试：成功
  await expect(page.getByText("学年流转完成")).toBeVisible();

  expect(executedBodies.length).toBe(2);
  expect(executedBodies[0]["idempotency_key"]).toBeTruthy();
  expect(executedBodies[1]["idempotency_key"]).toBe(executedBodies[0]["idempotency_key"]); // 键跨重试稳定
});
