/** 角色权限配置与 MFA 管理员重置域 API（M2-UI2：TD-137/TD-143/TD-126）。
 *
 * 与后端 `app/api/school_rbac.py` 响应模型一一对齐。错误（409 版本冲突、
 * 422 校验、404 跨租）由调用方从异常 message 里取 `{code, message}` 展示。
 */

import type { AuthSessionRequest } from "./org";

export interface RoleView {
  id: string;
  code: string;
  name: string;
  is_system: boolean;
  editable: boolean;
  lock_reason: string | null;
  version: number;
  defaults_applied: boolean;
  user_count: number;
  permission_codes: string[];
}

export interface PermissionView {
  code: string;
  module: string;
  action: string;
  description: string;
}

export interface RolePermissionsInput {
  permission_codes: string[];
  version: number;
  reason?: string | null;
}

export interface ResetPreview {
  version: number;
  permissions_added: string[];
  permissions_removed: string[];
  scopes_added: string[];
  scopes_removed: string[];
  affected_users: number;
}

export interface RoleResetInput {
  confirm_role_code: string;
  reason: string;
  version: number;
}

export interface UserLookupView {
  id: string;
  display_name: string | null;
  user_type: string;
  status: string;
  mfa_enabled: boolean;
  roles: string[];
}

export interface MfaResetInput {
  reason: string;
}

function jsonInit(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export interface RbacApi {
  listRoles(): Promise<RoleView[]>;
  listPermissions(): Promise<PermissionView[]>;
  replaceRolePermissions(roleId: string, input: RolePermissionsInput): Promise<RoleView>;
  previewRoleReset(roleId: string): Promise<ResetPreview>;
  resetRole(roleId: string, input: RoleResetInput): Promise<ResetPreview>;
  /** 按登录名精确定位本校用户（查询会留审计）。 */
  lookupUser(loginId: string): Promise<UserLookupView>;
  resetUserMfa(userId: string, input: MfaResetInput): Promise<void>;
}

export function createRbacApi(session: { request: AuthSessionRequest }): RbacApi {
  const request = session.request;

  return {
    async listRoles() {
      return request<RoleView[]>("/api/v1/school/roles");
    },
    async listPermissions() {
      return request<PermissionView[]>("/api/v1/school/permissions");
    },
    async replaceRolePermissions(roleId, input) {
      return request<RoleView>(
        `/api/v1/school/roles/${roleId}/permissions`,
        jsonInit("PUT", input),
      );
    },
    async previewRoleReset(roleId) {
      return request<ResetPreview>(`/api/v1/school/roles/${roleId}/reset/preview`, {
        method: "POST",
      });
    },
    async resetRole(roleId, input) {
      return request<ResetPreview>(`/api/v1/school/roles/${roleId}/reset`, jsonInit("POST", input));
    },
    async lookupUser(loginId) {
      const qs = new URLSearchParams({ login_id: loginId }).toString();
      return request<UserLookupView>(`/api/v1/school/users/lookup?${qs}`);
    },
    async resetUserMfa(userId, input) {
      await request<void>(`/api/v1/school/users/${userId}/mfa/reset`, jsonInit("POST", input));
    },
  };
}
