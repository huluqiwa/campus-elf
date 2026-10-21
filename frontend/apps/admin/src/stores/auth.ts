/** 登录状态（内存令牌 + 当前用户），页面刷新后用 Cookie 静默恢复。 */

import type {
  AuthTokens,
  AuthUser,
  PlatformLoginInput,
  TenantLoginInput,
} from "@campus-elf/api-client";
import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { authEvents, session } from "../session";

export const useAuthStore = defineStore("auth", () => {
  const tokens = ref<AuthTokens | null>(null);
  const user = ref<AuthUser | null>(null);
  /** 是否已尝试过 Cookie 静默恢复（每个页面生命周期只试一次）。 */
  const restored = ref(false);

  const isAuthenticated = computed(() => tokens.value !== null);
  // 门禁以 /auth/me 的库值为准（claim 是签发时快照，可能滞后）；未取到时退回 claim
  const mustChangePassword = computed(
    () => user.value?.mustChangePassword ?? tokens.value?.mustChangePassword ?? false,
  );
  const isPlatform = computed(() => tokens.value?.realm === "platform");

  // 业务请求触发静默刷新时同步令牌（否则 store 持旧 expires/claim）
  authEvents.onTokensRefreshed = (next) => {
    tokens.value = next;
  };

  async function loadUser(): Promise<void> {
    try {
      user.value = await session.me();
    } catch {
      user.value = null;
    }
  }

  async function loginTenant(input: TenantLoginInput): Promise<AuthTokens> {
    tokens.value = await session.loginTenant(input);
    restored.value = true;
    await loadUser();
    return tokens.value;
  }

  async function loginPlatform(input: PlatformLoginInput): Promise<AuthTokens> {
    tokens.value = await session.loginPlatform(input);
    restored.value = true;
    await loadUser();
    return tokens.value;
  }

  /** 页面刷新：仅凭 HttpOnly Cookie 换一次新令牌；失败即未登录（不弹错）。 */
  async function restore(): Promise<boolean> {
    restored.value = true;
    const restoredTokens = await session.restore();
    tokens.value = restoredTokens;
    if (restoredTokens) {
      await loadUser();
    }
    return restoredTokens !== null;
  }

  async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
    tokens.value = await session.changePassword(currentPassword, newPassword);
    await loadUser();
  }

  async function logout(): Promise<void> {
    await session.logout();
    clear();
  }

  function clear(): void {
    tokens.value = null;
    user.value = null;
  }

  return {
    tokens,
    user,
    restored,
    isAuthenticated,
    mustChangePassword,
    isPlatform,
    loginTenant,
    loginPlatform,
    restore,
    changePassword,
    logout,
    clear,
  };
});
