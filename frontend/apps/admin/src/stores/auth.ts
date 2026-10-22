/** 登录状态（内存令牌 + 当前用户），页面刷新后用 Cookie 静默恢复。 */

import type {
  AuthTokens,
  AuthUser,
  LoginOutcome,
  MfaEnrollment,
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
  /** 登录第二步：只放内存，过期或改换登录即作废（M1-D2）。 */
  const mfaToken = ref<string | null>(null);
  /** 强制绑定阶段的受限令牌：同样只在内存里。 */
  const enrollToken = ref<string | null>(null);
  /** 恢复码只在激活成功的那一次可见，离开页面即清空。 */
  const recoveryCodes = ref<string[]>([]);

  const isAuthenticated = computed(() => tokens.value !== null);
  // 门禁以 /auth/me 的库值为准（claim 是签发时快照，可能滞后）；未取到时退回 claim
  const mustChangePassword = computed(
    () => user.value?.mustChangePassword ?? tokens.value?.mustChangePassword ?? false,
  );
  const isPlatform = computed(() => tokens.value?.realm === "platform");
  /** 策略要求绑定但还没绑定：登录后必须走强制绑定页。 */
  const mustEnrollMfa = computed(
    () =>
      enrollToken.value !== null || (user.value?.mfaRequired === true && !user.value.mfaEnabled),
  );

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

  /** 登录结果三态：会话 / 动态码步骤 / 强制绑定。任何非会话态都不保留令牌。 */
  function applyLoginOutcome(outcome: LoginOutcome): void {
    restored.value = true;
    recoveryCodes.value = [];
    if (outcome.status === "authenticated") {
      tokens.value = outcome.tokens;
      mfaToken.value = null;
      enrollToken.value = null;
      return;
    }
    tokens.value = null;
    if (outcome.status === "mfaRequired") {
      mfaToken.value = outcome.mfaToken;
      enrollToken.value = null;
    } else {
      enrollToken.value = outcome.enrollToken;
      mfaToken.value = null;
    }
  }

  async function loginTenant(input: TenantLoginInput): Promise<LoginOutcome> {
    const outcome = await session.loginTenant(input);
    applyLoginOutcome(outcome);
    if (outcome.status === "authenticated") {
      await loadUser();
    }
    return outcome;
  }

  async function loginPlatform(input: PlatformLoginInput): Promise<LoginOutcome> {
    const outcome = await session.loginPlatform(input);
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

  /** 强制绑定：用登录给出的绑定令牌取回 secret（此时还没有会话）。 */
  async function enrollWithToken(): Promise<MfaEnrollment> {
    if (!enrollToken.value) {
      throw new Error("绑定链接已过期，请重新登录");
    }
    return await session.enrollMfaWithToken(enrollToken.value);
  }

  /** 强制绑定的激活：成功后拿到会话与一次性恢复码。 */
  async function activateWithToken(code: string): Promise<string[]> {
    if (!enrollToken.value) {
      throw new Error("绑定链接已过期，请重新登录");
    }
    const activation = await session.activateMfa(code, enrollToken.value);
    tokens.value = activation.tokens;
    enrollToken.value = null;
    recoveryCodes.value = activation.recoveryCodes;
    restored.value = true;
    await loadUser();
    return activation.recoveryCodes;
  }

  /** 安全设置页：已登录用户自助绑定（需重新输入密码）。 */
  async function enrollWithPassword(currentPassword: string): Promise<MfaEnrollment> {
    return await session.enrollMfa(currentPassword);
  }

  async function activateWithPassword(code: string): Promise<string[]> {
    const activation = await session.activateMfa(code);
    tokens.value = activation.tokens;
    recoveryCodes.value = activation.recoveryCodes;
    await loadUser();
    return activation.recoveryCodes;
  }

  async function disableMfa(currentPassword: string, code: string): Promise<void> {
    tokens.value = await session.disableMfa(currentPassword, code);
    recoveryCodes.value = [];
    await loadUser();
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
    mfaToken.value = null;
    enrollToken.value = null;
    recoveryCodes.value = [];
  }

  return {
    tokens,
    user,
    restored,
    mfaToken,
    enrollToken,
    recoveryCodes,
    isAuthenticated,
    mustChangePassword,
    mustEnrollMfa,
    isPlatform,
    loginTenant,
    loginPlatform,
    verifyMfa,
    enrollWithToken,
    activateWithToken,
    enrollWithPassword,
    activateWithPassword,
    disableMfa,
    restore,
    changePassword,
    logout,
    clear,
  };
});
