import { defineConfig, devices } from "@playwright/test";

// M1 验收（TD-110）全栈 E2E 配置：真实后端容器（127.0.0.1:8000）+ 真实测试栈，
// 不引入接口 mock；与 playwright.config.ts（mock E2E）完全独立，互不影响。
// 前置：scripts/e2e-stack.sh up（并按其输出导出 E2E_ACCOUNTS_FILE）。
export default defineConfig({
  testDir: "./e2e-fullstack",
  timeout: 60_000,
  fullyParallel: true,
  workers: 2,
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
