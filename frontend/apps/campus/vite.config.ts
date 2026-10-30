import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

// 全栈 E2E 的 preview 由 Playwright 注入 VITE_API_PROXY_TARGET（跟随账号文件里的 e2e_base_url）；
// 不注入时保持开发默认的 8000。Vite 7 的 preview 继承 server.proxy，改这里即同时作用于两者。
const apiProxyTarget = process.env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  // 不单独配置 preview.proxy：Vite 7 的 preview 默认继承 server.proxy，
  // e2e（vite preview）依赖该继承；如未来升级 Vite 需复核此行为。
  server: {
    port: 5174,
    proxy: {
      "/api": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
      "/health": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
});
