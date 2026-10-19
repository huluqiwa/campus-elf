# 前端（M0-D 脚手架）

## 结构

```text
frontend/
├─ apps/admin/     Vue3 + TS + Vite + Element Plus；/platform 与 /school 路由后续接入
├─ apps/campus/    Vue3 + TS + Vite + Vant 4；移动端 H5
├─ packages/api-client/  API 客户端骨架（M1 起换 OpenAPI 生成）
├─ packages/ui/          共享 UI 占位
├─ packages/config/      共享 tsconfig
├─ e2e/                  Playwright 冒烟
└─ playwright.config.ts  使用系统 Chrome channel
```

## 命令

```bash
cd frontend
pnpm install
pnpm build          # 递归构建
pnpm typecheck      # vue-tsc / tsc
pnpm test:e2e       # 先 build，再用 vite preview 跑 Playwright 冒烟
pnpm dev:admin      # http://127.0.0.1:5173
pnpm dev:campus     # http://127.0.0.1:5174
```

## 说明

- Playwright 使用 `channel: "chrome"`，依赖本机已安装 Chrome，不下载 Chromium。
- 前端变量放在 `frontend/.env`（见 `frontend/.env.example`）；根目录 `.env` 不会被 Vite 读取。
- 默认 `VITE_API_BASE_URL` 留空，`/health` 与 `/api` 由 Vite/Nginx 同源代理到后端，避免浏览器 CORS。
- 后端未启动时页面显示“后端未连接（M0 脚手架）”。
- H5 兼容微信内置浏览器/移动 Safari；SSE 在后续里程碑接入。
- 质量门禁（M0-F 已落地，CI 同步执行）：
  - `pnpm lint`（ESLint 扁平配置，覆盖 .ts/.vue/e2e）、`pnpm format:check`（Prettier）；
  - `pnpm typecheck`（含根 `tsconfig.json` 对 `playwright.config.ts`/`e2e` 的检查）；
  - `pnpm test:unit`（Vitest，当前覆盖 `packages/api-client`）；
  - `pnpm test:e2e`（Playwright：渲染 + health mock 200/503/超时失败态）；
  - `pnpm audit`（本地若使用不支持 audit endpoint 的镜像源，会失败；`scripts/ci.sh` 会自动切官方源重试）。
- Element Plus 由 `unplugin-vue-components` 按需引入（不再全量注册）；**不要**给 Vite 配 `manualChunks` 点名 element-plus——会把包入口的再导出重新纳入产物（TD-031）。
- `vite preview` 继承 `server.proxy`（Vite 7 行为），e2e 依赖该继承。
