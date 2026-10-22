import { describe, expect, it, vi } from "vitest";

import type { AuthTokens, LoginOutcome } from "./auth";
import { createAuthSession, passwordPolicyError, toUserMessage } from "./auth";

/** 构造可编排的 fetch 替身：按 URL 后缀匹配处理器，并记录调用顺序。 */
function fetchStub(
  handlers: Array<{ match: string; respond: () => Response | Promise<Response> }>,
): {
  fetchImpl: typeof fetch;
  calls: string[];
} {
  const calls: string[] = [];
  const fetchImpl = (async (input: RequestInfo | URL) => {
    const url = String(input);
    calls.push(url);
    for (const handler of handlers) {
      if (url.includes(handler.match)) {
        return await handler.respond();
      }
    }
    throw new Error(`unexpected request: ${url}`);
  }) as typeof fetch;
  return { fetchImpl, calls };
}

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

function tokenBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    access_token: "access-1",
    refresh_token: "refresh-1",
    token_type: "bearer",
    expires_in: 900,
    realm: "tenant",
    must_change_password: false,
    tenant_id: "11111111-1111-1111-1111-111111111111",
    roles: ["student"],
    ...overrides,
  };
}

/** 登录三态里只取会话令牌（测试用：断言这些用例确实直接登录成功）。 */
function sessionTokens(outcome: LoginOutcome): AuthTokens {
  if (outcome.status !== "authenticated") {
    throw new Error(`期望登录成功，实际得到 ${outcome.status}`);
  }
  return outcome.tokens;
}

describe("createAuthSession", () => {
  it("登录成功后发送带 Cookie 的凭据请求并保存内存令牌", async () => {
    const { fetchImpl, calls } = fetchStub([
      {
        match: "/api/v1/auth/login",
        respond: () => jsonResponse(200, { status: "authenticated", ...tokenBody() }),
      },
    ]);
    const session = createAuthSession({ baseUrl: "http://api", fetchImpl });

    const tokens = sessionTokens(
      await session.loginTenant({ schoolCode: "SCH-1", loginId: "S-1", password: "pw" }),
    );

    expect(tokens.accessToken).toBe("access-1");
    expect(tokens.roles).toEqual(["student"]);
    expect(session.getAccessToken()).toBe("access-1");
    expect(calls).toEqual(["http://api/api/v1/auth/login"]);
  });

  it("401 时先刷新一次再重放原请求", async () => {
    let protectedCalls = 0;
    const { fetchImpl, calls } = fetchStub([
      {
        match: "/api/v1/platform/tenants",
        respond: () => {
          protectedCalls += 1;
          return protectedCalls === 1
            ? jsonResponse(401, { detail: "登录已过期" })
            : jsonResponse(200, { items: [] });
        },
      },
      { match: "/api/v1/auth/refresh", respond: () => jsonResponse(200, tokenBody()) },
    ]);
    const session = createAuthSession({ baseUrl: "http://api", fetchImpl });

    const result = await session.request<{ items: unknown[] }>("/api/v1/platform/tenants");

    expect(result).toEqual({ items: [] });
    expect(calls).toEqual([
      "http://api/api/v1/platform/tenants",
      "http://api/api/v1/auth/refresh",
      "http://api/api/v1/platform/tenants",
    ]);
  });

  it("并发 401 只触发一次刷新（单飞）", async () => {
    let refreshCalls = 0;
    let protectedCalls = 0;
    const { fetchImpl } = fetchStub([
      {
        match: "/api/v1/platform/tenants",
        respond: () => {
          protectedCalls += 1;
          return protectedCalls <= 2
            ? jsonResponse(401, { detail: "登录已过期" })
            : jsonResponse(200, { ok: true });
        },
      },
      {
        match: "/api/v1/auth/refresh",
        respond: () => {
          refreshCalls += 1;
          return jsonResponse(200, tokenBody({ access_token: `access-${refreshCalls + 1}` }));
        },
      },
    ]);
    const session = createAuthSession({ baseUrl: "http://api", fetchImpl });

    await Promise.all([
      session.request("/api/v1/platform/tenants"),
      session.request("/api/v1/platform/tenants"),
    ]);

    expect(refreshCalls).toBe(1);
  });

  it("刷新失败：清空令牌并回调 onUnauthorized，不再重放", async () => {
    const onUnauthorized = vi.fn();
    let protectedCalls = 0;
    const { fetchImpl } = fetchStub([
      {
        match: "/api/v1/platform/tenants",
        respond: () => {
          protectedCalls += 1;
          return jsonResponse(401, { detail: "登录已过期" });
        },
      },
      { match: "/api/v1/auth/refresh", respond: () => jsonResponse(401, { detail: "登录已失效" }) },
    ]);
    const session = createAuthSession({ baseUrl: "http://api", fetchImpl, onUnauthorized });

    await expect(session.request("/api/v1/platform/tenants")).rejects.toThrow("过期");
    expect(protectedCalls).toBe(1);
    expect(session.getAccessToken()).toBeNull();
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("403 含改密提示 → 回调 onPasswordChangeRequired；其它 403 → onForbidden", async () => {
    const onPasswordChangeRequired = vi.fn();
    const onForbidden = vi.fn();
    const { fetchImpl } = fetchStub([
      {
        match: "/api/v1/platform/tenants",
        respond: () => jsonResponse(403, { detail: "首次登录必须修改密码后才能使用其他功能" }),
      },
      {
        match: "/api/v1/school/teachers",
        respond: () => jsonResponse(403, { detail: "缺少权限：school.personnel.view" }),
      },
    ]);
    const session = createAuthSession({
      baseUrl: "http://api",
      fetchImpl,
      onPasswordChangeRequired,
      onForbidden,
    });

    await expect(session.request("/api/v1/platform/tenants")).rejects.toThrow("修改密码");
    await expect(session.request("/api/v1/school/teachers")).rejects.toThrow("缺少权限");

    expect(onPasswordChangeRequired).toHaveBeenCalledOnce();
    expect(onForbidden).toHaveBeenCalledOnce();
  });

  it("restore：Cookie 静默恢复；无 Cookie 时返回 null 且不回调未授权", async () => {
    const onUnauthorized = vi.fn();
    const okStub = fetchStub([
      { match: "/api/v1/auth/refresh", respond: () => jsonResponse(200, tokenBody()) },
    ]);
    const okSession = createAuthSession({
      baseUrl: "http://api",
      fetchImpl: okStub.fetchImpl,
      onUnauthorized,
    });
    await expect(okSession.restore()).resolves.toMatchObject({ accessToken: "access-1" });

    const failStub = fetchStub([
      { match: "/api/v1/auth/refresh", respond: () => jsonResponse(401, { detail: "无 Cookie" }) },
    ]);
    const failSession = createAuthSession({
      baseUrl: "http://api",
      fetchImpl: failStub.fetchImpl,
      onUnauthorized,
    });
    await expect(failSession.restore()).resolves.toBeNull();
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("登出调用接口并清空内存令牌（即使接口失败）", async () => {
    const { fetchImpl } = fetchStub([
      { match: "/api/v1/auth/login", respond: () => jsonResponse(200, tokenBody()) },
      { match: "/api/v1/auth/logout", respond: () => jsonResponse(500, { detail: "boom" }) },
    ]);
    const session = createAuthSession({ baseUrl: "http://api", fetchImpl });
    await session.loginTenant({ schoolCode: "SCH-1", loginId: "S-1", password: "pw" });

    await session.logout();

    expect(session.getAccessToken()).toBeNull();
  });

  it("改密成功后用新令牌替换内存令牌", async () => {
    const { fetchImpl } = fetchStub([
      {
        match: "/api/v1/auth/change-password",
        respond: () =>
          jsonResponse(200, tokenBody({ access_token: "access-2", must_change_password: false })),
      },
    ]);
    const session = createAuthSession({ baseUrl: "http://api", fetchImpl });

    const tokens = await session.changePassword("old-password", "New-Password-123");

    expect(tokens.accessToken).toBe("access-2");
    expect(session.getAccessToken()).toBe("access-2");
  });
});

describe("createAuthSession（审查修复回归）", () => {
  it("改密请求必须带 Authorization 头（B1）", async () => {
    const seen: Array<{ url: string; auth: string | undefined }> = [];
    const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const headers = new Headers(init?.headers ?? {});
      seen.push({ url: String(input), auth: headers.get("authorization") ?? undefined });
      if (String(input).includes("/auth/login")) {
        return jsonResponse(200, tokenBody());
      }
      return jsonResponse(200, tokenBody({ access_token: "access-2" }));
    }) as typeof fetch;

    const session = createAuthSession({ baseUrl: "http://api", fetchImpl });
    await session.loginTenant({ schoolCode: "SCH-1", loginId: "S-1", password: "pw" });
    await session.changePassword("old", "New-Password-123");

    const change = seen.find((item) => item.url.includes("/auth/change-password"));
    expect(change?.auth).toBe("Bearer access-1");
    const login = seen.find((item) => item.url.includes("/auth/login"));
    expect(login?.auth).toBeUndefined();
  });

  it("无本地令牌时登出仍调用后端（Cookie 才是凭证）", async () => {
    const calls: string[] = [];
    const fetchImpl = (async (input: RequestInfo | URL) => {
      calls.push(String(input));
      return jsonResponse(200, { status: "ok" });
    }) as typeof fetch;

    const session = createAuthSession({ baseUrl: "http://api", fetchImpl });
    await session.logout();

    expect(calls).toEqual(["http://api/api/v1/auth/logout"]);
  });

  it("/auth/me 在 access 过期时先刷新再重放（Minor 3）", async () => {
    let meCalls = 0;
    const { fetchImpl } = fetchStub([
      {
        match: "/api/v1/auth/me",
        respond: () => {
          meCalls += 1;
          return meCalls === 1
            ? jsonResponse(401, { detail: "登录已过期" })
            : jsonResponse(200, {
                user_id: "u-1",
                realm: "tenant",
                tenant_id: null,
                must_change_password: false,
                roles: [],
                permissions: [],
              });
        },
      },
      { match: "/api/v1/auth/refresh", respond: () => jsonResponse(200, tokenBody()) },
    ]);
    const session = createAuthSession({ baseUrl: "http://api", fetchImpl });

    const me = await session.me();

    expect(me.userId).toBe("u-1");
    expect(meCalls).toBe(2);
  });

  it("403 携带 X-Auth-Error 时优先按机器码分流（Minor 4）", async () => {
    const onPasswordChangeRequired = vi.fn();
    const fetchImpl = (async () =>
      ({
        ok: false,
        status: 403,
        headers: new Headers({ "X-Auth-Error": "PASSWORD_CHANGE_REQUIRED" }),
        json: async () => ({ detail: "some-translated-message" }),
      }) as Response) as typeof fetch;
    const session = createAuthSession({
      baseUrl: "http://api",
      fetchImpl,
      onPasswordChangeRequired,
    });

    await expect(session.request("/api/v1/school/teachers")).rejects.toThrow(
      "some-translated-message",
    );
    expect(onPasswordChangeRequired).toHaveBeenCalledOnce();
  });

  it("令牌更新触发 onTokensRefreshed（供 UI 同步）", async () => {
    const onTokensRefreshed = vi.fn();
    const { fetchImpl } = fetchStub([
      { match: "/api/v1/auth/login", respond: () => jsonResponse(200, tokenBody()) },
    ]);
    const session = createAuthSession({ baseUrl: "http://api", fetchImpl, onTokensRefreshed });

    await session.loginTenant({ schoolCode: "SCH-1", loginId: "S-1", password: "pw" });

    expect(onTokensRefreshed).toHaveBeenCalledOnce();
    expect(onTokensRefreshed.mock.calls[0][0].accessToken).toBe("access-1");
  });
});

describe("密码重置（M1-D1）", () => {
  it("申请重置：按 realm 映射字段，202 视为成功", async () => {
    const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
    const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        url: String(input),
        body: JSON.parse(String(init?.body)) as Record<string, unknown>,
      });
      return { ok: true, status: 202, json: async () => ({ status: "accepted" }) } as Response;
    }) as typeof fetch;
    const session = createAuthSession({ baseUrl: "http://api", fetchImpl });

    await session.requestPasswordReset({
      realm: "tenant",
      schoolCode: "SCH-1",
      loginId: "S-1",
    });
    await session.requestPasswordReset({ realm: "platform", account: "ops" });

    expect(calls).toEqual([
      {
        url: "http://api/api/v1/auth/password-reset/request",
        body: { realm: "tenant", school_code: "SCH-1", login_id: "S-1" },
      },
      {
        url: "http://api/api/v1/auth/password-reset/request",
        body: { realm: "platform", account: "ops" },
      },
    ]);
  });

  it("确认重置：204 空响应不解析 JSON", async () => {
    const seen: Array<Record<string, unknown>> = [];
    const fetchImpl = (async (_input: RequestInfo | URL, init?: RequestInit) => {
      seen.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      return { ok: true, status: 204 } as Response;
    }) as typeof fetch;
    const session = createAuthSession({ baseUrl: "http://api", fetchImpl });

    await expect(
      session.confirmPasswordReset("v1rt.tenant.secret", "New-Password-123"),
    ).resolves.toBeUndefined();
    expect(seen).toEqual([{ token: "v1rt.tenant.secret", new_password: "New-Password-123" }]);
  });

  it("确认重置失败：透出后端 detail（无效/过期令牌）", async () => {
    const fetchImpl = (async () =>
      jsonResponse(400, { detail: "重置链接无效或已过期" })) as typeof fetch;
    const session = createAuthSession({ baseUrl: "http://api", fetchImpl });

    await expect(session.confirmPasswordReset("bad-token", "New-Password-123")).rejects.toThrow(
      "重置链接无效或已过期",
    );
  });
});

describe("密码策略（M1-D1）", () => {
  it("与后端同口径：长度/字节/字符类/登录名包含", () => {
    expect(passwordPolicyError("Short-1A")).toContain("10~128");
    expect(passwordPolicyError("汉".repeat(25) + "Aa1!")).toContain("72 字节");
    expect(passwordPolicyError("alllowercase1")).toContain("3 类");
    expect(passwordPolicyError("Alice-Login-1A", "alice")).toContain("学号/工号");
    expect(passwordPolicyError("Valid-Pass-99")).toBeNull();
  });

  it("Unicode 口径：中文算 alnum 不算符号，长度按码点计", () => {
    // 后端把中文视为 alnum：这里只有小写 + 数字 2 类，必须拒绝
    expect(passwordPolicyError("密码密码密码abcd1")).toContain("3 类");
    // 5 个 emoji 在 UTF-16 下是 10 个 code unit，但只有 5 个码点
    expect(passwordPolicyError("😀😀😀😀😀")).toContain("10~128");
    // 中文 + 大小写 + 数字 + 符号：10 个码点，合法
    expect(passwordPolicyError("密码密码密码Ab1!")).toBeNull();
    // Python str.isdigit 接受上标数字（No），前端同口径
    expect(passwordPolicyError("密码密码密码Ab²!")).toBeNull();
  });
});

describe("改密 401 刷新重放（第二轮 Minor 1）", () => {
  it("改密遇 401 时先刷新再重放，且重放带新令牌", async () => {
    const calls: string[] = [];
    const authHeaders: Array<string | null> = [];
    let changeCalls = 0;
    const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push(url);
      const headers = new Headers(init?.headers ?? {});
      if (url.includes("/auth/login")) {
        return jsonResponse(200, tokenBody({ access_token: "stale-access" }));
      }
      if (url.includes("/auth/refresh")) {
        return jsonResponse(200, tokenBody({ access_token: "fresh-access" }));
      }
      if (url.includes("/auth/change-password")) {
        changeCalls += 1;
        authHeaders.push(headers.get("authorization"));
        return changeCalls === 1
          ? jsonResponse(401, { detail: "登录已过期" })
          : jsonResponse(200, tokenBody({ access_token: "after-change" }));
      }
      throw new Error(`unexpected ${url}`);
    }) as typeof fetch;

    const session = createAuthSession({ baseUrl: "http://api", fetchImpl });
    await session.loginTenant({ schoolCode: "SCH-1", loginId: "S-1", password: "pw" });
    const tokens = await session.changePassword("old", "New-Password-123");

    expect(tokens.accessToken).toBe("after-change");
    expect(calls.filter((url) => url.includes("/auth/refresh"))).toHaveLength(1);
    expect(changeCalls).toBe(2);
    expect(authHeaders).toEqual(["Bearer stale-access", "Bearer fresh-access"]);
  });

  it("toUserMessage：网络/技术文案走兜底，业务文案原样返回", () => {
    expect(toUserMessage(new Error("Failed to fetch"), "登录失败，请检查网络后重试")).toBe(
      "登录失败，请检查网络后重试",
    );
    expect(toUserMessage(new Error("学校编号、账号或密码不正确"), "兜底")).toBe(
      "学校编号、账号或密码不正确",
    );
  });
});

describe("MFA（M1-D2）", () => {
  it("已绑定账号登录只得到挑战令牌：不写会话、不回调令牌更新", async () => {
    const onTokensRefreshed = vi.fn();
    const { fetchImpl } = fetchStub([
      {
        match: "/api/v1/auth/login",
        respond: () =>
          jsonResponse(200, {
            status: "mfa_required",
            mfa_token: "mfa-challenge-token",
            expires_in: 300,
            methods: ["totp"],
          }),
      },
    ]);
    const session = createAuthSession({ baseUrl: "http://api", fetchImpl, onTokensRefreshed });

    const outcome = await session.loginTenant({
      schoolCode: "SCH-1",
      loginId: "S-1",
      password: "pw",
    });

    expect(outcome).toEqual({
      status: "mfaRequired",
      mfaToken: "mfa-challenge-token",
      expiresIn: 300,
      methods: ["totp"],
    });
    expect(session.getAccessToken()).toBeNull();
    expect(onTokensRefreshed).not.toHaveBeenCalled();
  });

  it("第二步校验成功后才保存会话令牌", async () => {
    const seen: Array<Record<string, unknown>> = [];
    const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/auth/login")) {
        return jsonResponse(200, {
          status: "mfa_required",
          mfa_token: "mfa-challenge-token",
          expires_in: 300,
          methods: ["totp"],
        });
      }
      if (url.includes("/auth/mfa/verify")) {
        seen.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        return jsonResponse(200, { status: "authenticated", ...tokenBody() });
      }
      throw new Error(`unexpected ${url}`);
    }) as typeof fetch;
    const session = createAuthSession({ baseUrl: "http://api", fetchImpl });

    const outcome = await session.loginTenant({
      schoolCode: "SCH-1",
      loginId: "S-1",
      password: "pw",
    });
    expect(session.getAccessToken()).toBeNull();
    if (outcome.status !== "mfaRequired") throw new Error("应为动态码步骤");

    const tokens = await session.verifyMfa(outcome.mfaToken, "123456");

    expect(tokens.accessToken).toBe("access-1");
    expect(seen).toEqual([{ mfa_token: "mfa-challenge-token", code: "123456" }]);
  });

  it("第二步失败不触发静默刷新（没有会话可刷新）", async () => {
    const calls: string[] = [];
    const fetchImpl = (async (input: RequestInfo | URL) => {
      calls.push(String(input));
      return jsonResponse(401, { detail: "学校编号、账号或密码不正确" });
    }) as typeof fetch;
    const session = createAuthSession({ baseUrl: "http://api", fetchImpl });

    await expect(session.verifyMfa("mfa-challenge-token", "000000")).rejects.toThrow("不正确");
    expect(calls).toEqual(["http://api/api/v1/auth/mfa/verify"]);
  });

  it("强制绑定：绑定令牌作为 Bearer 发起 enroll/activate，且拿到恢复码", async () => {
    const requests: Array<{ url: string; auth: string | null; body: Record<string, unknown> }> = [];
    const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const headers = new Headers(init?.headers ?? {});
      requests.push({
        url,
        auth: headers.get("authorization"),
        body: JSON.parse(String(init?.body)) as Record<string, unknown>,
      });
      if (url.includes("/auth/login")) {
        return jsonResponse(200, {
          status: "mfa_enrollment_required",
          enroll_token: "enroll-token",
          expires_in: 600,
        });
      }
      if (url.includes("/auth/mfa/enroll")) {
        return jsonResponse(200, {
          secret: "JBSWY3DPEHPK3PXP",
          otpauth_uri: "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP",
          expires_in: 600,
        });
      }
      if (url.includes("/auth/mfa/activate")) {
        return jsonResponse(200, {
          ...tokenBody({ realm: "platform" }),
          recovery_codes: ["AAAAA-BBBBB", "CCCCC-DDDDD"],
        });
      }
      throw new Error(`unexpected ${url}`);
    }) as typeof fetch;
    const session = createAuthSession({ baseUrl: "http://api", fetchImpl });

    const outcome = await session.loginPlatform({ account: "ops", password: "pw" });
    expect(outcome).toEqual({
      status: "mfaEnrollmentRequired",
      enrollToken: "enroll-token",
      expiresIn: 600,
    });
    // 绑定阶段没有任何会话
    expect(session.getAccessToken()).toBeNull();

    const enrollment = await session.enrollMfaWithToken("enroll-token");
    expect(enrollment).toEqual({
      secret: "JBSWY3DPEHPK3PXP",
      otpauthUri: "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP",
      expiresIn: 600,
    });

    const activation = await session.activateMfa("123456", "enroll-token");
    expect(activation.recoveryCodes).toEqual(["AAAAA-BBBBB", "CCCCC-DDDDD"]);
    expect(activation.tokens.realm).toBe("platform");
    expect(session.getAccessToken()).toBe("access-1");

    expect(requests.filter((item) => item.url.includes("/auth/mfa/"))).toEqual([
      {
        url: "http://api/api/v1/auth/mfa/enroll",
        auth: "Bearer enroll-token",
        body: {},
      },
      {
        url: "http://api/api/v1/auth/mfa/activate",
        auth: "Bearer enroll-token",
        body: { code: "123456" },
      },
    ]);
  });

  it("已登录用户自助绑定：enroll 带访问令牌与当前密码", async () => {
    const requests: Array<{ url: string; auth: string | null; body: Record<string, unknown> }> = [];
    const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const headers = new Headers(init?.headers ?? {});
      requests.push({
        url,
        auth: headers.get("authorization"),
        body: JSON.parse(String(init?.body)) as Record<string, unknown>,
      });
      if (url.includes("/auth/login")) {
        return jsonResponse(200, { status: "authenticated", ...tokenBody() });
      }
      if (url.includes("/auth/mfa/enroll")) {
        return jsonResponse(200, {
          secret: "SEC",
          otpauth_uri: "otpauth://totp/x",
          expires_in: 600,
        });
      }
      if (url.includes("/auth/mfa/activate")) {
        return jsonResponse(200, { ...tokenBody(), recovery_codes: ["AAAAA-BBBBB"] });
      }
      if (url.includes("/auth/mfa/disable")) {
        return jsonResponse(200, tokenBody({ access_token: "access-after-disable" }));
      }
      throw new Error(`unexpected ${url}`);
    }) as typeof fetch;
    const session = createAuthSession({ baseUrl: "http://api", fetchImpl });
    await session.loginTenant({ schoolCode: "SCH-1", loginId: "S-1", password: "pw" });

    await session.enrollMfa("Old-Password-1");
    const activation = await session.activateMfa("123456");
    expect(activation.recoveryCodes).toEqual(["AAAAA-BBBBB"]);

    const disabled = await session.disableMfa("Old-Password-1", "123456");
    expect(disabled.accessToken).toBe("access-after-disable");

    expect(
      requests.map((item) => [item.url.replace("http://api/api/v1/auth", ""), item.auth]),
    ).toEqual([
      ["/login", null],
      ["/mfa/enroll", "Bearer access-1"],
      ["/mfa/activate", "Bearer access-1"],
      ["/mfa/disable", "Bearer access-1"],
    ]);
  });

  it("/auth/me 暴露 MFA 状态（供安全设置页与强制绑定门禁使用）", async () => {
    const { fetchImpl } = fetchStub([
      {
        match: "/api/v1/auth/me",
        respond: () =>
          jsonResponse(200, {
            user_id: "u-1",
            realm: "platform",
            tenant_id: null,
            must_change_password: false,
            roles: [],
            permissions: [],
            mfa_enabled: false,
            mfa_required: true,
          }),
      },
    ]);
    const session = createAuthSession({ baseUrl: "http://api", fetchImpl });

    const me = await session.me();

    expect(me.mfaEnabled).toBe(false);
    expect(me.mfaRequired).toBe(true);
  });

  it("旧版后端未返回 MFA 字段时按未启用/不强制处理", async () => {
    const { fetchImpl } = fetchStub([
      {
        match: "/api/v1/auth/me",
        respond: () =>
          jsonResponse(200, {
            user_id: "u-1",
            realm: "tenant",
            tenant_id: null,
            must_change_password: false,
            roles: [],
            permissions: [],
          }),
      },
    ]);
    const session = createAuthSession({ baseUrl: "http://api", fetchImpl });

    const me = await session.me();

    expect(me.mfaEnabled).toBe(false);
    expect(me.mfaRequired).toBe(false);
  });
});
