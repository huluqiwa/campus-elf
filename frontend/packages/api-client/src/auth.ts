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
  mfaEnabled: boolean;
  mfaRequired: boolean;
}

/** 登录需要动态码（已绑定）：只拿到短时挑战令牌，此时还没有会话。 */
export interface MfaChallengeOutcome {
  status: "mfaRequired";
  mfaToken: string;
  expiresIn: number;
  methods: string[];
}

/** 平台要求必须绑定但还没绑定：只拿到受限绑定令牌。 */
export interface MfaEnrollmentOutcome {
  status: "mfaEnrollmentRequired";
  enrollToken: string;
  expiresIn: number;
}

/** 登录三态：直接进会话 / 输动态码 / 先绑定验证器。 */
export type LoginOutcome =
  { status: "authenticated"; tokens: AuthTokens } | MfaChallengeOutcome | MfaEnrollmentOutcome;

/** 绑定信息：secret 只在内存中使用，禁止写 localStorage/日志。 */
export interface MfaEnrollment {
  secret: string;
  otpauthUri: string;
  expiresIn: number;
}

/** 激活结果：新会话 + 一次性恢复码（此后再也看不到）。 */
export interface MfaActivation {
  tokens: AuthTokens;
  recoveryCodes: string[];
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

/** 重置申请：沿用登录的 realm 语义；tenant 需要学校编号 + 登录名，platform 需要账号。 */
export interface PasswordResetRequestInput {
  realm: Realm;
  schoolCode?: string;
  loginId?: string;
  account?: string;
}

const MIN_PASSWORD_LENGTH = 10;
const MAX_PASSWORD_LENGTH = 128;
const MAX_PASSWORD_BYTES = 72;
const MIN_LOGIN_ID_FOR_CONTAINMENT = 3;
// 用 Unicode 二元属性对齐 Python 的 isupper/islower（含 Other_Uppercase/Lowercase）
const HAS_UPPER = /\p{Uppercase}/u;
const HAS_LOWER = /\p{Lowercase}/u;
// Python str.isdigit 还接受上/下标数字（No）；服务端仍为最终权威
const HAS_DIGIT = /[\p{Nd}\u00B2\u00B3\u00B9\u2070-\u2079\u2080-\u2089]/u;
// Python 的 str.isalnum 覆盖 Unicode 字母/数字；中文属于 alnum，不算符号
const HAS_SYMBOL = /[^\p{L}\p{N}\s]/u;

/**
 * 与后端 AUTH-03 同口径的本地预校验：返回错误文案，`null` 表示通过。
 * 登录名可选（申请/重置页拿不到）；服务端仍会做最终校验。
 */
export function passwordPolicyError(password: string, loginId?: string): string | null {
  // 按 Unicode 码点计数，与后端 len() 一致（emoji 不会被算成两个字符）
  const length = Array.from(password).length;
  if (length < MIN_PASSWORD_LENGTH || length > MAX_PASSWORD_LENGTH) {
    return `密码长度需在 ${MIN_PASSWORD_LENGTH}~${MAX_PASSWORD_LENGTH} 个字符之间`;
  }
  if (new TextEncoder().encode(password).length > MAX_PASSWORD_BYTES) {
    return `密码不能超过 ${MAX_PASSWORD_BYTES} 字节（一个汉字约 3 字节）`;
  }
  const classes = [HAS_UPPER, HAS_LOWER, HAS_DIGIT, HAS_SYMBOL].filter((pattern) =>
    pattern.test(password),
  ).length;
  if (classes < 3) {
    return "密码需至少包含大写字母、小写字母、数字、符号中的 3 类";
  }
  const login = (loginId ?? "").trim().toLowerCase();
  if (login.length >= MIN_LOGIN_ID_FOR_CONTAINMENT && password.toLowerCase().includes(login)) {
    return "密码不能包含学号/工号或账号";
  }
  return null;
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
  loginTenant(input: TenantLoginInput): Promise<LoginOutcome>;
  loginPlatform(input: PlatformLoginInput): Promise<LoginOutcome>;
  /** 登录第二步：TOTP 6 位码或一次性恢复码；成功才签发会话。 */
  verifyMfa(mfaToken: string, code: string): Promise<AuthTokens>;
  /** 已登录用户发起绑定：必须重新验证当前密码。 */
  enrollMfa(currentPassword: string): Promise<MfaEnrollment>;
  /** 强制绑定阶段（尚无会话）：用登录返回的绑定令牌发起。 */
  enrollMfaWithToken(enrollToken: string): Promise<MfaEnrollment>;
  /** 激活绑定：成功签发新会话并返回一次性恢复码；`enrollToken` 用于强制绑定阶段。 */
  activateMfa(code: string, enrollToken?: string): Promise<MfaActivation>;
  /** 关闭绑定：密码 + 动态码/恢复码，成功签发新会话。 */
  disableMfa(currentPassword: string, code: string): Promise<AuthTokens>;
  /** 页面加载时用 Cookie 静默恢复会话（失败返回 null，不触发 onUnauthorized）。 */
  restore(): Promise<AuthTokens | null>;
  refresh(): Promise<AuthTokens>;
  logout(): Promise<void>;
  changePassword(currentPassword: string, newPassword: string): Promise<AuthTokens>;
  /** 申请重置：后端固定 202（不暴露账号是否存在），调用方只需展示统一提示。 */
  requestPasswordReset(input: PasswordResetRequestInput): Promise<void>;
  /** 确认重置：成功返回 204，旧会话全部失效，需要重新登录。 */
  confirmPasswordReset(token: string, newPassword: string): Promise<void>;
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
  "/api/v1/auth/mfa/verify",
]);

/** 后端 /auth/me 的原始响应（snake_case），前端统一映射为 camelCase。 */
interface MePayload {
  user_id: string;
  realm: Realm;
  tenant_id: string | null;
  must_change_password: boolean;
  roles: string[];
  permissions: string[];
  mfa_enabled?: boolean;
  mfa_required?: boolean;
}

interface TokenPayload {
  access_token: string;
  expires_in: number;
  must_change_password: boolean;
  tenant_id: string | null;
  roles: string[];
  realm: Realm;
}

/** /auth/login 的三态响应（按 `status` 判别）。 */
type LoginPayload =
  | (TokenPayload & { status: "authenticated" })
  | { status: "mfa_required"; mfa_token: string; expires_in: number; methods: string[] }
  | { status: "mfa_enrollment_required"; enroll_token: string; expires_in: number };

interface MfaEnrollPayload {
  secret: string;
  otpauth_uri: string;
  expires_in: number;
}

interface MfaActivatePayload extends TokenPayload {
  recovery_codes: string[];
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

/** HTTP 错误：保留状态码与原始 detail（业务错误形如 {code, message, latest?}）。 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(message: string, status: number, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/** 从错误响应中取出可读信息（FastAPI 的错误体是 {detail: "..."} 或校验错误数组）。 */
async function readErrorDetail(response: Response): Promise<{ message: string; detail: unknown }> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail) {
      return { message: body.detail, detail: body.detail };
    }
    if (body.detail && typeof body.detail === "object" && !Array.isArray(body.detail)) {
      const message = (body.detail as { message?: unknown }).message;
      if (typeof message === "string" && message) {
        return { message, detail: body.detail };
      }
    }
    if (Array.isArray(body.detail)) {
      const messages = body.detail
        .map((item) => (item && typeof item === "object" && "msg" in item ? String(item.msg) : ""))
        .filter(Boolean);
      if (messages.length) {
        return { message: messages.join("；"), detail: body.detail };
      }
    }
  } catch {
    /* 非 JSON 响应：忽略 */
  }
  return { message: `请求失败（${response.status}）`, detail: null };
}

async function apiError(response: Response): Promise<ApiError> {
  const { message, detail } = await readErrorDetail(response);
  return new ApiError(message, response.status, detail);
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

  function toEnrollment(payload: MfaEnrollPayload): MfaEnrollment {
    return {
      secret: payload.secret,
      otpauthUri: payload.otpauth_uri,
      expiresIn: payload.expires_in,
    };
  }

  async function post<T>(
    path: string,
    body: unknown,
    opts?: { auth?: boolean; bearer?: string },
  ): Promise<T> {
    const bearer = opts?.bearer ?? (opts?.auth && tokens ? tokens.accessToken : undefined);
    const response = await fetchImpl(`${baseUrl}${path}`, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}),
      },
      body: JSON.stringify(body ?? {}),
    });
    if (!response.ok) {
      throw await apiError(response);
    }
    return (await response.json()) as T;
  }

  /** 公开 POST：只关心成功/失败，不解析响应体（申请 202、确认 204）。 */
  async function postEmpty(path: string, body: unknown): Promise<void> {
    const response = await fetchImpl(`${baseUrl}${path}`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    if (!response.ok) {
      throw await apiError(response);
    }
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
          throw await apiError(response);
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
      throw await apiError(response);
    }

    if (response.status === 403) {
      const error = await apiError(response);
      const code = response.headers?.get?.(PASSWORD_CHANGE_HEADER) ?? null;
      if (code === PASSWORD_CHANGE_CODE || error.message.includes(PASSWORD_CHANGE_MARKER)) {
        options.onPasswordChangeRequired?.();
      } else {
        options.onForbidden?.(error.message);
      }
      throw error;
    }

    if (!response.ok) {
      throw await apiError(response);
    }
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }

  /** 登录三态映射：只有 `authenticated` 才写入会话令牌。 */
  function toLoginOutcome(payload: LoginPayload): LoginOutcome {
    if (payload.status === "mfa_required") {
      return {
        status: "mfaRequired",
        mfaToken: payload.mfa_token,
        expiresIn: payload.expires_in,
        methods: payload.methods ?? ["totp"],
      };
    }
    if (payload.status === "mfa_enrollment_required") {
      return {
        status: "mfaEnrollmentRequired",
        enrollToken: payload.enroll_token,
        expiresIn: payload.expires_in,
      };
    }
    return { status: "authenticated", tokens: setTokens(toTokens(payload)) };
  }

  return {
    getAccessToken: () => tokens?.accessToken ?? null,
    getTokens: () => tokens,

    async loginTenant(input: TenantLoginInput): Promise<LoginOutcome> {
      const payload = await post<LoginPayload>("/api/v1/auth/login", {
        realm: "tenant",
        school_code: input.schoolCode,
        login_id: input.loginId,
        password: input.password,
      });
      return toLoginOutcome(payload);
    },

    async loginPlatform(input: PlatformLoginInput): Promise<LoginOutcome> {
      const payload = await post<LoginPayload>("/api/v1/auth/login", {
        realm: "platform",
        account: input.account,
        password: input.password,
      });
      return toLoginOutcome(payload);
    },

    async verifyMfa(mfaToken: string, code: string): Promise<AuthTokens> {
      const payload = await post<TokenPayload>("/api/v1/auth/mfa/verify", {
        mfa_token: mfaToken,
        code,
      });
      return setTokens(toTokens(payload));
    },

    async enrollMfa(currentPassword: string): Promise<MfaEnrollment> {
      const payload = await post<MfaEnrollPayload>(
        "/api/v1/auth/mfa/enroll",
        { current_password: currentPassword },
        { auth: true },
      );
      return toEnrollment(payload);
    },

    async enrollMfaWithToken(enrollToken: string): Promise<MfaEnrollment> {
      const payload = await post<MfaEnrollPayload>(
        "/api/v1/auth/mfa/enroll",
        {},
        { bearer: enrollToken },
      );
      return toEnrollment(payload);
    },

    async activateMfa(code: string, enrollToken?: string): Promise<MfaActivation> {
      const payload = await post<MfaActivatePayload>(
        "/api/v1/auth/mfa/activate",
        { code },
        enrollToken ? { bearer: enrollToken } : { auth: true },
      );
      return {
        tokens: setTokens(toTokens(payload)),
        recoveryCodes: payload.recovery_codes ?? [],
      };
    },

    async disableMfa(currentPassword: string, code: string): Promise<AuthTokens> {
      // 走 request()：访问令牌恰好过期时先刷新一次再重放
      const payload = await request<TokenPayload>("/api/v1/auth/mfa/disable", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current_password: currentPassword, code }),
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

    async requestPasswordReset(input: PasswordResetRequestInput): Promise<void> {
      await postEmpty("/api/v1/auth/password-reset/request", {
        realm: input.realm,
        ...(input.realm === "tenant"
          ? { school_code: input.schoolCode ?? "", login_id: input.loginId ?? "" }
          : { account: input.account ?? "" }),
      });
    },

    async confirmPasswordReset(token: string, newPassword: string): Promise<void> {
      await postEmpty("/api/v1/auth/password-reset/confirm", {
        token,
        new_password: newPassword,
      });
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
        mfaEnabled: payload.mfa_enabled ?? false,
        mfaRequired: payload.mfa_required ?? false,
      };
    },

    request,
  };
}
