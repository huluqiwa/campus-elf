/**
 * 认证会话单例（M1-C-UI）。
 *
 * 回调在 main.ts 里绑定（避免 store 与 router 互相 import 造成循环依赖）：
 * 通过持有同一个 options 对象并在启动后写入回调，会话内部访问时才读取。
 */

import {
  createAuthSession,
  type AuthSessionOptions,
  type AuthTokens,
} from "@campus-elf/api-client";

/** 令牌刷新事件（供 Pinia store 同步；避免 store 与会话模块互相 import）。 */
export const authEvents: { onTokensRefreshed?: (tokens: AuthTokens) => void } = {};

/** 会话配置对象；main.ts 会在路由就绪后写入回调（onUnauthorized 等）。 */
export const authOptions: AuthSessionOptions = {
  // 空串 = 同源相对路径，由 Vite/Nginx 代理 /api 到后端
  baseUrl: import.meta.env.VITE_API_BASE_URL ?? "",
  onTokensRefreshed: (tokens) => authEvents.onTokensRefreshed?.(tokens),
};

export const session = createAuthSession(authOptions);
