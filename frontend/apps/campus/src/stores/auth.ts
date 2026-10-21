/** C 端登录状态（内存令牌 + 当前用户），页面刷新后用 Cookie 静默恢复。 */

import type { AuthTokens, AuthUser, TenantLoginInput } from "@campus-elf/api-client";
import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { authEvents, session } from "../session";

export const useAuthStore = defineStore("auth", () => {
  const tokens = ref<AuthTokens | null>(null);
  const user = ref<AuthUser | null>(null);
  const restored = ref(false);

  const isAuthenticated = computed(() => tokens.value !== null);
  // 门禁以 /auth/me 的库值为准；未取到时退回令牌 claim
  const mustChangePassword = computed(
    () => user.value?.mustChangePassword ?? tokens.value?.mustChangePassword ?? false,
  );

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

  async function login(input: TenantLoginInput): Promise<AuthTokens> {
    tokens.value = await session.loginTenant(input);
    restored.value = true;
    await loadUser();
    return tokens.value;
  }

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
    login,
    restore,
    changePassword,
    logout,
    clear,
  };
});
