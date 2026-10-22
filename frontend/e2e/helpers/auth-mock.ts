import type { Page } from "@playwright/test";

/** 登录后的用户信息（供 /auth/me mock 使用）。 */
export interface MockUser {
  userId?: string;
  realm?: "tenant" | "platform";
  tenantId?: string | null;
  mustChangePassword?: boolean;
  roles?: string[];
  permissions?: string[];
  mfaEnabled?: boolean;
  mfaRequired?: boolean;
}

export interface MockAuthOptions {
  /** 是否已登录（决定 /auth/refresh 是否成功）。 */
  authenticated?: boolean;
  /** 登录接口返回的 must_change_password。 */
  mustChangePasswordOnLogin?: boolean;
  /** 登录接口是否失败（返回统一文案）。 */
  loginFails?: boolean;
  /** 登录返回 mfa_required：已绑定用户先过密码、再过动态码（M1-D2）。 */
  mfaRequiredOnLogin?: boolean;
  /** 登录返回 mfa_enrollment_required：平台强制绑定，只给一次性绑定令牌。 */
  mfaEnrollmentRequiredOnLogin?: boolean;
  /** 用户信息。 */
  user?: MockUser;
  /** 登录失败文案。 */
  loginErrorMessage?: string;
}

/** 记录 MFA 相关请求，供断言"确实带了什么"，而非只看页面跳转。 */
export interface AuthMockHandles {
  verifyBodies: Record<string, unknown>[];
  enrollAuthHeaders: (string | null)[];
  activateBodies: Record<string, unknown>[];
  disableBodies: Record<string, unknown>[];
}

function tokenBody(options: MockAuthOptions, mustChangePassword: boolean): Record<string, unknown> {
  const realm = options.user?.realm ?? "tenant";
  return {
    access_token: "e2e-access-token",
    refresh_token: "e2e-refresh-token",
    token_type: "bearer",
    expires_in: 900,
    realm,
    must_change_password: mustChangePassword,
    // 平台令牌没有 tenant_id：与后端 exclude_none 行为一致（字段省略而非 null）
    ...(realm === "platform"
      ? {}
      : { tenant_id: options.user?.tenantId ?? "11111111-1111-1111-1111-111111111111" }),
    roles: options.user?.roles ?? ["school_super_admin"],
  };
}

function userBody(options: MockAuthOptions, mfaEnabled: boolean): Record<string, unknown> {
  return {
    user_id: options.user?.userId ?? "22222222-2222-2222-2222-222222222222",
    realm: options.user?.realm ?? "tenant",
    tenant_id: options.user?.tenantId ?? "11111111-1111-1111-1111-111111111111",
    must_change_password: options.user?.mustChangePassword ?? false,
    roles: options.user?.roles ?? ["school_super_admin"],
    permissions: options.user?.permissions ?? [],
    mfa_enabled: mfaEnabled,
    mfa_required: options.user?.mfaRequired ?? false,
  };
}

export interface PasswordResetRequestPayload {
  realm?: string;
  school_code?: string;
  login_id?: string;
  account?: string;
}

export interface PasswordResetConfirmPayload {
  token?: string;
  new_password?: string;
}

export interface MockPasswordResetOptions {
  /** 申请接口失败（默认 202）。 */
  requestFails?: boolean;
  requestErrorMessage?: string;
  /** 确认接口状态码（默认 204 成功）。 */
  confirmStatus?: number;
  confirmErrorMessage?: string;
}

export interface PasswordResetMock {
  requests: PasswordResetRequestPayload[];
  confirms: PasswordResetConfirmPayload[];
}

/** mock 密码重置接口，并记录请求体供断言（M1-D1）。 */
export async function mockPasswordReset(
  page: Page,
  options: MockPasswordResetOptions = {},
): Promise<PasswordResetMock> {
  const requests: PasswordResetRequestPayload[] = [];
  const confirms: PasswordResetConfirmPayload[] = [];

  await page.route("**/api/v1/auth/password-reset/request", (route) => {
    requests.push((route.request().postDataJSON() ?? {}) as PasswordResetRequestPayload);
    if (options.requestFails) {
      return route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: options.requestErrorMessage ?? "服务暂时不可用" }),
      });
    }
    return route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ status: "accepted" }),
    });
  });

  await page.route("**/api/v1/auth/password-reset/confirm", (route) => {
    confirms.push((route.request().postDataJSON() ?? {}) as PasswordResetConfirmPayload);
    const status = options.confirmStatus ?? 204;
    if (status !== 204) {
      return route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify({ detail: options.confirmErrorMessage ?? "重置链接无效或已过期" }),
      });
    }
    return route.fulfill({ status: 204 });
  });

  return { requests, confirms };
}

/**
 * 统一 mock 认证接口（**有状态**）：refresh/me/login/change-password/logout/mfa 与 health。
 *
 * 状态语义与真实后端一致：
 * - `authenticated` 表示"初始是否已登录"（决定 refresh/me 是否成功）；
 * - 登录成功后 /auth/me 才开始返回用户，否则返回 401（避免"未登录却取到用户"的假绿）；
 * - 改密成功后 must_change_password 变为 false，/auth/me 随之更新；
 * - 登出后 refresh/me 回到 401；
 * - MFA：登录返回 mfa_required/mfa_enrollment_required 时不建立会话（refresh/me 仍 401），
 *   直到 verify/activate 成功才 loggedIn=true；activate 后 mfa_enabled 变 true、disable 变 false。
 */
export async function mockAuth(
  page: Page,
  options: MockAuthOptions = {},
): Promise<AuthMockHandles> {
  let loggedIn = options.authenticated ?? true;
  let mustChange = options.user?.mustChangePassword ?? options.mustChangePasswordOnLogin ?? false;
  let mfaEnabled = options.user?.mfaEnabled ?? options.mfaRequiredOnLogin === true;

  const handles: AuthMockHandles = {
    verifyBodies: [],
    enrollAuthHeaders: [],
    activateBodies: [],
    disableBodies: [],
  };

  await page.route("**/api/v1/auth/refresh", (route) =>
    loggedIn
      ? route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(tokenBody(options, mustChange)),
        })
      : route.fulfill({
          status: 401,
          contentType: "application/json",
          body: JSON.stringify({ detail: "刷新令牌无效，请重新登录" }),
        }),
  );

  // 未登录时 /auth/me 必须 401（与真实后端一致，避免"未登录却取到用户"的假绿）
  await page.route("**/api/v1/auth/me", (route) =>
    loggedIn
      ? route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(
            userBody(
              { ...options, user: { ...options.user, mustChangePassword: mustChange } },
              mfaEnabled,
            ),
          ),
        })
      : route.fulfill({
          status: 401,
          contentType: "application/json",
          body: JSON.stringify({ detail: "缺少访问令牌" }),
        }),
  );

  await page.route("**/api/v1/auth/login", (route) => {
    if (options.loginFails) {
      return route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({
          detail: options.loginErrorMessage ?? "学校编号、账号或密码不正确",
        }),
      });
    }
    if (options.mfaRequiredOnLogin) {
      // 密码通过但未建会话：只返回挑战令牌
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "mfa_required",
          mfa_token: "e2e-mfa-challenge-token",
          expires_in: 300,
          methods: ["totp"],
        }),
      });
    }
    if (options.mfaEnrollmentRequiredOnLogin) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "mfa_enrollment_required",
          enroll_token: "e2e-enroll-token",
          expires_in: 600,
        }),
      });
    }
    loggedIn = true;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(tokenBody(options, mustChange)),
    });
  });

  // 登录第二步：动态码/恢复码校验成功后建立会话
  await page.route("**/api/v1/auth/mfa/verify", (route) => {
    handles.verifyBodies.push((route.request().postDataJSON() ?? {}) as Record<string, unknown>);
    loggedIn = true;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(tokenBody(options, mustChange)),
    });
  });

  // 取回绑定密钥：强制绑定用 enroll_token Bearer，自助绑定用会话 Bearer
  await page.route("**/api/v1/auth/mfa/enroll", (route) => {
    handles.enrollAuthHeaders.push(route.request().headers().authorization ?? null);
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "Cache-Control": "no-store" },
      body: JSON.stringify({
        secret: "JBSWY3DPEHPK3PXP",
        otpauth_uri:
          "otpauth://totp/%E6%A0%A1%E5%9B%AD%E9%99%AA%E4%BC%B4%E7%B2%BE%E7%81%B5%3Ae2e?secret=JBSWY3DPEHPK3PXP&issuer=CampusElf",
        expires_in: 600,
      }),
    });
  });

  // 激活：建立会话 + 一次性恢复码，并把 mfa_enabled 翻转为 true
  await page.route("**/api/v1/auth/mfa/activate", (route) => {
    handles.activateBodies.push((route.request().postDataJSON() ?? {}) as Record<string, unknown>);
    loggedIn = true;
    mfaEnabled = true;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...tokenBody(options, mustChange),
        recovery_codes: ["AAAA-BBBB-CCCC", "DDDD-EEEE-FFFF"],
      }),
    });
  });

  // 关闭：mfa_enabled 翻转回 false，返回新的会话令牌（会话轮换）
  await page.route("**/api/v1/auth/mfa/disable", (route) => {
    handles.disableBodies.push((route.request().postDataJSON() ?? {}) as Record<string, unknown>);
    mfaEnabled = false;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(tokenBody(options, mustChange)),
    });
  });

  await page.route("**/api/v1/auth/change-password", (route) => {
    mustChange = false;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(tokenBody(options, false)),
    });
  });

  await page.route("**/api/v1/auth/logout", (route) => {
    loggedIn = false;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "ok" }),
    });
  });

  await page.route("**/health/live", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        service: "CampusCompanionElf",
        version: "0.1.0",
        env: "e2e",
      }),
    }),
  );

  return handles;
}
