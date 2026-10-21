import type { Page } from "@playwright/test";

/** 登录后的用户信息（供 /auth/me mock 使用）。 */
export interface MockUser {
  userId?: string;
  realm?: "tenant" | "platform";
  tenantId?: string | null;
  mustChangePassword?: boolean;
  roles?: string[];
  permissions?: string[];
}

export interface MockAuthOptions {
  /** 是否已登录（决定 /auth/refresh 是否成功）。 */
  authenticated?: boolean;
  /** 登录接口返回的 must_change_password。 */
  mustChangePasswordOnLogin?: boolean;
  /** 登录接口是否失败（返回统一文案）。 */
  loginFails?: boolean;
  /** 用户信息。 */
  user?: MockUser;
  /** 登录失败文案。 */
  loginErrorMessage?: string;
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

function userBody(options: MockAuthOptions): Record<string, unknown> {
  return {
    user_id: options.user?.userId ?? "22222222-2222-2222-2222-222222222222",
    realm: options.user?.realm ?? "tenant",
    tenant_id: options.user?.tenantId ?? "11111111-1111-1111-1111-111111111111",
    must_change_password: options.user?.mustChangePassword ?? false,
    roles: options.user?.roles ?? ["school_super_admin"],
    permissions: options.user?.permissions ?? [],
  };
}

/**
 * 统一 mock 认证接口（**有状态**）：refresh/me/login/change-password/logout 与 health。
 *
 * 状态语义与真实后端一致：
 * - `authenticated` 表示"初始是否已登录"（决定 refresh/me 是否成功）；
 * - 登录成功后 /auth/me 才开始返回用户，否则返回 401（避免"未登录却取到用户"的假绿）；
 * - 改密成功后 must_change_password 变为 false，/auth/me 随之更新；
 * - 登出后 refresh/me 回到 401。
 */
export async function mockAuth(page: Page, options: MockAuthOptions = {}): Promise<void> {
  let loggedIn = options.authenticated ?? true;
  let mustChange = options.user?.mustChangePassword ?? options.mustChangePasswordOnLogin ?? false;

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
            userBody({ ...options, user: { ...options.user, mustChangePassword: mustChange } }),
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
    loggedIn = true;
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
}
