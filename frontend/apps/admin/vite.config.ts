import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import AutoImport from "unplugin-auto-import/vite";
import Components from "unplugin-vue-components/vite";
import { ElementPlusResolver } from "unplugin-vue-components/resolvers";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [
    vue(),
    // Element Plus 按需引入：组件与 API（ElMessage 等）不再全量打包（TD-031）。
    AutoImport({ resolvers: [ElementPlusResolver()], dts: "src/auto-imports.d.ts" }),
    Components({ resolvers: [ElementPlusResolver()], dts: "src/components.d.ts" }),
  ],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  // 不配置 manualChunks：Element Plus 的按需解析从包入口再导出，手动分包会把这些
  // 再导出重新纳入产物。（本机 element-plus 2.14.7 单次实测：配 manualChunks 时该 chunk
  // 914KB 且含未使用组件；移除后 admin JS 合计 112KB。见 TD-031 处置。）
  // 不单独配置 preview.proxy：Vite 7 的 preview 默认继承 server.proxy，
  // e2e（vite preview）依赖该继承；如未来升级 Vite 需复核此行为。
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/health": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
