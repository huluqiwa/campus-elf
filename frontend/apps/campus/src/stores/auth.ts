/** C 端登录状态（内存令牌 + 当前用户），页面刷新后用 Cookie 静默恢复。 */

import type { AuthTokens, AuthUser, LoginOutcome, TenantLoginInput } from "@campus-elf/api-client";
import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { authEvents, session } from "../session";

export const useAuthStore = defineStore("auth", () => {
  const tokens = ref<AuthTokens | null>(null);
  const user = ref<AuthUser | null>(null);
  const restored = ref(false);
  /** 登录第二步：只放内存，过期或改换登录即作废（M1-D2）。 */
  const mfaToken = ref<string | null>(null);

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

  /** 登录结果三态：会话 / 动态码步骤 / 强制绑定。任何非会话态都不保留令牌。 */
  function applyLoginOutcome(outcome: LoginOutcome): void {
    restored.value = true;
    if (outcome.status === "authenticated") {
      tokens.value = outcome.tokens;
      mfaToken.value = null;
      return;
    }
    tokens.value = null;
    mfaToken.value = outcome.status === "mfaRequired" ? outcome.mfaToken : null;
  }

  async function login(input: TenantLoginInput): Promise<LoginOutcome> {
    const outcome = await session.loginTenant(input);
    applyLoginOutcome(outcome);
    if (outcome.status === "authenticated") {
      await loadUser();
    }
    return outcome;
  }

  /** 登录第二步：成功才建立会话。 */
  async function verifyMfa(code: string): Promise<AuthTokens> {
    if (!mfaToken.value) {
      throw new Error("登录状态已过期，请重新登录");
    }
    tokens.value = await session.verifyMfa(mfaToken.value, code);
    mfaToken.value = null;
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
    mfaToken.value = null;
  }

  return {
    tokens,
    user,
    restored,
    mfaToken,
    isAuthenticated,
    mustChangePassword,
    login,
    verifyMfa,
    restore,
    changePassword,
    logout,
    clear,
  };
});
