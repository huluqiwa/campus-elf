import { expect, test } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";

// 失败态与接口 mock（TD-033）：不依赖真实后端，确定性地覆盖「后端可用 / 后端异常」两种分支。
// 首页在 M1-C-UI 起受登录守卫保护：先 mock 认证（已登录），再验证健康检查分支。

test("后端可用时展示服务信息（health 接口 mock 200）", async ({ page }) => {
  await mockAuth(page);
  await page.route("**/health/live", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        service: "CampusCompanionElf",
        version: "0.1.0",
        env: "e2e",
      }),
    }),
  );

  await page.goto("/");

  await expect(page.getByText("CampusCompanionElf 0.1.0 (e2e)")).toBeVisible();
});

test("后端异常时展示失败态（health 接口 mock 503）", async ({ page }) => {
  await mockAuth(page);
  await page.route("**/health/live", (route) =>
    route.fulfill({ status: 503, body: "unavailable" }),
  );

  await page.goto("/");

  await expect(page.getByText("后端未连接（M0 脚手架）")).toBeVisible();
});

test("health 请求超时同样落到失败态", async ({ page }) => {
  await mockAuth(page);
  // 不 fulfill，模拟请求悬挂；客户端 5s 超时后应显示失败态。
  await page.route("**/health/live", () => {
    /* 故意不响应 */
  });

  await page.goto("/");
  await expect(page.getByText("后端未连接（M0 脚手架）")).toBeVisible({ timeout: 20_000 });
});
