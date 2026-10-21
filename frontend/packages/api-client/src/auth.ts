/**
 * 认证会话模块（M1-C-UI）。
 *
 * 设计要点（见 docs/design-briefs/M1-C-UI-login-pages.md）：
 * - 访问令牌只存内存（刷新页面即丢），刷新令牌走 HttpOnly Cookie（JS 读不到），所有请求带 `credentials: "include"`；
 * - 401 → 尝试刷新一次（并发 401 只发一次刷新请求，即"单飞"）→ 成功重放原请求，失败回调 onUnauthorized；
 * - 403 分流：详情包含"修改密码"→ onPasswordChangeRequired（强制跳改密页）；其它 → onForbidden；
 * - 刷新接口本身 401 不再触发刷新（避免死循环）。
 */

export type Realm = "tenant" | "platform";

export interface AuthTokens {
  accessToken: string;
  expiresIn: number;
  mustChangePassword: boolean;
  tenantId: string | null;
  roles: string[];
  realm: Realm;
}

export interface AuthUser {
  userId: string;
  realm: Realm;
  tenantId: string | null;
  mustChangePassword: boolean;
  roles: string[];
  permissions: string[];
}

export interface TenantLoginInput {
  schoolCode: string;
  loginId: string;
  password: string;
}

export interface PlatformLoginInput {
  account: string;
  password: string;
}

export interface AuthSessionOptions {
  /** API 根地址；空串表示同源相对路径（Vite/Nginx 代理）。 */
  baseUrl: string;
  fetchImpl?: typeof fetch;
  /** 会话失效（刷新失败/被踢出/过期）：清空令牌并回登录页。 */
  onUnauthorized?: (reason: string) => void;
  /** 首登必须改密：跳转改密页。 */
  onPasswordChangeRequired?: () => void;
  /** 其它 403（权限不足）：仅提示，不跳转。 */
  onForbidden?: (message: string) => void;
  /** 令牌更新（登录/刷新/改密）：供 UI 层同步状态。 */
  onTokensRefreshed?: (tokens: AuthTokens) => void;
}

export interface AuthSession {
  getAccessToken(): string | null;
  getTokens(): AuthTokens | null;
  loginTenant(input: TenantLoginInput): Promise<AuthTokens>;
  loginPlatform(input: PlatformLoginInput): Promise<AuthTokens>;
  /** 页面加载时用 Cookie 静默恢复会话（失败返回 null，不触发 onUnauthorized）。 */
  restore(): Promise<AuthTokens | null>;
  refresh(): Promise<AuthTokens>;
  logout(): Promise<void>;
  changePassword(currentPassword: string, newPassword: string): Promise<AuthTokens>;
  me(): Promise<AuthUser>;
  /** 带鉴权的通用请求：401 自动刷新一次并重放。 */
  request<T>(path: string, init?: RequestInit): Promise<T>;
}

const REFRESH_PATH = "/api/v1/auth/refresh";
const PASSWORD_CHANGE_MARKER = "修改密码";
/** 首登门禁的机器可读标记（后端在 403 响应头给出，优先于文案匹配）。 */
const PASSWORD_CHANGE_HEADER = "X-Auth-Error";
const PASSWORD_CHANGE_CODE = "PASSWORD_CHANGE_REQUIRED";
/** 这些认证接口自身失败时不触发"刷新后重放"（避免递归与无意义请求）。 */
const NO_REFRESH_PATHS = new Set<string>([
  "/api/v1/auth/login",
  "/api/v1/auth/refresh",
  "/api/v1/auth/logout",
]);

/** 后端 /auth/me 的原始响应（snake_case），前端统一映射为 camelCase。 */
interface MePayload {
  user_id: string;
  realm: Realm;
  tenant_id: string | null;
  must_change_password: boolean;
  roles: string[];
  permissions: string[];
}

interface TokenPayload {
  access_token: string;
  expires_in: number;
  must_change_password: boolean;
  tenant_id: string | null;
  roles: string[];
  realm: Realm;
}

/** 面向用户的错误文案：技术性/网络错误统一兜底，避免把 "Failed to fetch" 一类直出界面。 */
export function toUserMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) {
    const technical = /failed to fetch|networkerror|load failed|aborted/i.test(error.message);
    if (!technical) {
      return error.message;
    }
  }
  return fallback;
}

/** 从错误响应中取出可读信息（FastAPI 的错误体是 {detail: "..."}）。 */
async function readErrorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail) {
      return body.detail;
    }
  } catch {
    /* 非 JSON 响应：忽略 */
  }
  return `请求失败（${response.status}）`;
}

/** 创建认证会话（无框架依赖，便于单元测试；Vue 侧用 Pinia 包一层）。 */
export function createAuthSession(options: AuthSessionOptions): AuthSession {
  const fetchImpl = options.fetchImpl ?? fetch;
  const baseUrl = options.baseUrl.replace(/\/+$/, "");
  let tokens: AuthTokens | null = null;
  let refreshInFlight: Promise<AuthTokens> | null = null;

  function setTokens(next: AuthTokens): AuthTokens {
    tokens = next;
    options.onTokensRefreshed?.(next);
    return next;
  }

  function toTokens(payload: TokenPayload): AuthTokens {
    return {
      accessToken: payload.access_token,
      expiresIn: payload.expires_in,
      mustChangePassword: payload.must_change_password,
      tenantId: payload.tenant_id,
      roles: payload.roles ?? [],
      realm: payload.realm,
    };
  }

  async function post<T>(path: string, body: unknown, opts?: { auth?: boolean }): Promise<T> {
    const response = await fetchImpl(`${baseUrl}${path}`, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(opts?.auth && tokens ? { Authorization: `Bearer ${tokens.accessToken}` } : {}),
      },
      body: JSON.stringify(body ?? {}),
    });
    if (!response.ok) {
      throw new Error(await readErrorDetail(response));
    }
    return (await response.json()) as T;
  }

  async function refresh(): Promise<AuthTokens> {
    // 单飞：并发触发时复用同一个刷新请求
    if (!refreshInFlight) {
      refreshInFlight = (async () => {
        const response = await fetchImpl(`${baseUrl}${REFRESH_PATH}`, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        if (!response.ok) {
          throw new Error(await readErrorDetail(response));
        }
        return setTokens(toTokens((await response.json()) as TokenPayload));
      })().finally(() => {
        refreshInFlight = null;
      });
    }
    return refreshInFlight;
  }

  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    const url = path.startsWith("http") ? path : `${baseUrl}${path}`;
    const send = async (): Promise<Response> =>
      fetchImpl(url, {
        ...init,
        credentials: "include",
        headers: {
          ...(init?.headers ?? {}),
          ...(tokens ? { Authorization: `Bearer ${tokens.accessToken}` } : {}),
        },
      });

    let response = await send();
    if (response.status === 401 && !NO_REFRESH_PATHS.has(path)) {
      try {
        await refresh();
      } catch {
        tokens = null;
        options.onUnauthorized?.("登录已过期，请重新登录");
        throw new Error("登录已过期，请重新登录");
      }
      response = await send();
      if (response.status === 401) {
        tokens = null;
        options.onUnauthorized?.("登录已失效，请重新登录");
        throw new Error("登录已失效，请重新登录");
      }
    } else if (response.status === 401) {
      // 明确禁止刷新重试的认证接口：交给调用方展示（登录失败/刷新失败/登出等）
      throw new Error(await readErrorDetail(response));
    }

    if (response.status === 403) {
      const detail = await readErrorDetail(response);
      const code = response.headers?.get?.(PASSWORD_CHANGE_HEADER) ?? null;
      if (code === PASSWORD_CHANGE_CODE || detail.includes(PASSWORD_CHANGE_MARKER)) {
        options.onPasswordChangeRequired?.();
      } else {
        options.onForbidden?.(detail);
      }
      throw new Error(detail);
    }

    if (!response.ok) {
      throw new Error(await readErrorDetail(response));
    }
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }

  return {
    getAccessToken: () => tokens?.accessToken ?? null,
    getTokens: () => tokens,

    async loginTenant(input: TenantLoginInput): Promise<AuthTokens> {
      const payload = await post<TokenPayload>("/api/v1/auth/login", {
        realm: "tenant",
        school_code: input.schoolCode,
        login_id: input.loginId,
        password: input.password,
      });
      return setTokens(toTokens(payload));
    },

    async loginPlatform(input: PlatformLoginInput): Promise<AuthTokens> {
      const payload = await post<TokenPayload>("/api/v1/auth/login", {
        realm: "platform",
        account: input.account,
        password: input.password,
      });
      return setTokens(toTokens(payload));
    },

    async restore(): Promise<AuthTokens | null> {
      try {
        return await refresh();
      } catch {
        tokens = null;
        return null;
      }
    },

    refresh,

    async logout(): Promise<void> {
      try {
        // 始终调用后端：刷新令牌在 HttpOnly Cookie 里，前端"有没有 access"不代表已登出；
        // access 若已过期，后端仍会凭 Cookie 撤销家族并清除 Cookie（M1-C-UI 审查 MA2）。
        await fetchImpl(`${baseUrl}/api/v1/auth/logout`, {
          method: "POST",
          credentials: "include",
          headers: {
            "Content-Type": "application/json",
            ...(tokens ? { Authorization: `Bearer ${tokens.accessToken}` } : {}),
          },
          body: "{}",
        });
      } finally {
        tokens = null;
      }
    },

    async changePassword(currentPassword: string, newPassword: string): Promise<AuthTokens> {
      // 改密接口要求 Bearer 主体（审查 B1）；走 request() 以便 access 恰好过期时
      // 先刷新一次再重放（审查第二轮 Minor 1）
      const payload = await request<TokenPayload>("/api/v1/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
      });
      return setTokens(toTokens(payload));
    },

    async me(): Promise<AuthUser> {
      // 后端字段是 snake_case（MeResponse），在此统一映射，避免各页面各自记字段名
      const payload = await request<MePayload>("/api/v1/auth/me");
      return {
        userId: payload.user_id,
        realm: payload.realm,
        tenantId: payload.tenant_id,
        mustChangePassword: payload.must_change_password,
        roles: payload.roles ?? [],
        permissions: payload.permissions ?? [],
      };
    },

    request,
  };
}
