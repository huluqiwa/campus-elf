import { defineConfig, devices } from "@playwright/test";

import { assertDistFresh } from "./e2e/helpers/dist-freshness";
import { loadAccounts } from "./e2e-fullstack/helpers/support";

// M1 验收（TD-110）全栈 E2E 配置：真实后端容器 + 真实测试栈，不引入接口 mock；
// 与 playwright.config.ts（mock E2E）完全独立，互不影响。
// 后端地址不写死：账号文件里的 e2e_base_url 经 VITE_API_PROXY_TARGET 注入两个 preview 的代理。
// loadAccounts 会拒绝非本机地址与 8000——旧账号文件或跨项目路径都不可能把真实口令发出去。
const backendUrl = loadAccounts().e2e_base_url;
assertDistFresh("admin", __dirname);
assertDistFresh("campus", __dirname);
const inheritedEnv = Object.fromEntries(
  Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined),
);

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
      // 不复用已有 preview：旧进程可能带着错误的代理目标，复用会把请求打到别的后端。
      reuseExistingServer: false,
      timeout: 120_000,
      env: { ...inheritedEnv, VITE_API_PROXY_TARGET: backendUrl },
    },
    {
      command: "pnpm --filter campus preview",
      url: "http://127.0.0.1:4174",
      reuseExistingServer: false,
      timeout: 120_000,
      env: { ...inheritedEnv, VITE_API_PROXY_TARGET: backendUrl },
    },
  ],
});
