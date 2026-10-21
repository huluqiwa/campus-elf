/** 认证会话单例（M1-C-UI，C 端 H5）。回调在 main.ts 绑定，避免 store ↔ router 循环依赖。 */

import {
  createAuthSession,
  type AuthSessionOptions,
  type AuthTokens,
} from "@campus-elf/api-client";

/** 令牌刷新事件（供 Pinia store 同步；避免 store 与会话模块互相 import）。 */
export const authEvents: { onTokensRefreshed?: (tokens: AuthTokens) => void } = {};

export const authOptions: AuthSessionOptions = {
  baseUrl: import.meta.env.VITE_API_BASE_URL ?? "",
};

export const session = createAuthSession(authOptions);
