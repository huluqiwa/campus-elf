import { defineConfig, devices } from "@playwright/test";

import { assertDistFresh } from "./e2e/helpers/dist-freshness";

assertDistFresh("admin", __dirname);
assertDistFresh("campus", __dirname);

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: true,
  // Windows 高并发下 Chrome 偶发导航 ERR_ABORTED；固定 2 与 CI runner 对齐
  workers: 2,
  // 2 worker 并发时页面首屏（655KB 包 + 会话恢复）偶发超过默认 5s；
  // 断言内容不变，只放宽等待，避免把渲染慢误判成失败。
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    trace: "off",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "admin",
      testMatch: "**/admin*.spec.ts",
      use: {
        ...devices["Desktop Chrome"],
        channel: "chrome",
        baseURL: "http://127.0.0.1:4173",
      },
    },
    {
      name: "campus",
      testMatch: "**/campus*.spec.ts",
      use: {
        ...devices["Pixel 5"],
        channel: "chrome",
        baseURL: "http://127.0.0.1:4174",
      },
    },
  ],
  webServer: [
    {
      command: "pnpm --filter admin preview",
      url: "http://127.0.0.1:4173",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: "pnpm --filter campus preview",
      url: "http://127.0.0.1:4174",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
});
