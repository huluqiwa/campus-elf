/** 校园陪伴精灵 API 客户端骨架。 */

export interface HealthResponse {
  status: string;
  service: string;
  version: string;
  env: string;
}

export interface ApiClientOptions {
  /** API 根地址；留空表示同源相对路径（由 Vite/Nginx 代理）。 */
  baseUrl: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

export interface ApiClient {
  getHealth(): Promise<HealthResponse>;
}

const DEFAULT_TIMEOUT_MS = 5000;

/** 创建 API 客户端；后续按 OpenAPI 生成类型替换。 */
export function createApiClient(options: ApiClientOptions): ApiClient {
  const fetchImpl = options.fetchImpl ?? fetch;
  const baseUrl = options.baseUrl.replace(/\/+$/, "");
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  return {
    async getHealth(): Promise<HealthResponse> {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetchImpl(`${baseUrl}/health/live`, {
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error(`health request failed: ${response.status}`);
        }
        return (await response.json()) as HealthResponse;
      } finally {
        clearTimeout(timer);
      }
    },
  };
}

export {
  createAuthSession,
  passwordPolicyError,
  toUserMessage,
  type AuthSession,
  type AuthSessionOptions,
  type AuthTokens,
  type AuthUser,
  type LoginOutcome,
  type MfaActivation,
  type MfaChallengeOutcome,
  type MfaEnrollment,
  type MfaEnrollmentOutcome,
  type PasswordResetRequestInput,
  type PlatformLoginInput,
  type Realm,
  type TenantLoginInput,
} from "./auth";

export {
  buildQuery,
  createOrgApi,
  type AssignmentView,
  type ClassCreateInput,
  type ClassUpdateInput,
  type ClassView,
  type CourseView,
  type DepartmentCreateInput,
  type DepartmentUpdateInput,
  type DepartmentView,
  type GradeChange,
  type IdCardFull,
  type OrgApi,
  type Page,
  type PersonnelView,
  type RolloverAdvance,
  type RolloverInput,
  type RolloverResult,
  type StudentCreateInput,
  type StudentView,
  type TeacherCreateInput,
  type TeacherView,
} from "./org";

export {
  createImportsApi,
  type ImportErrorsPage,
  type ImportJobView,
  type ImportRowErrorView,
  type ImportUploadInput,
} from "./imports";
