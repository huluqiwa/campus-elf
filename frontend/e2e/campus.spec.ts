import { expect, test } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";

test("已登录访问 C 端首页展示身份（M4-UI1 起主页为聊天入口；health 探测移除）", async ({
  page,
}) => {
  await mockAuth(page, { user: { realm: "tenant", roles: ["student"] } });

  await page.goto("/");

  await expect(page.getByText("当前用户")).toBeVisible();
  // 无 campus.kb.qa（本测试未授权）→ 聊天入口不渲染，但历史会话入口常驻
  await expect(page.getByTestId("go-conversations")).toBeVisible();
});
