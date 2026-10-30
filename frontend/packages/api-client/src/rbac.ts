/** 角色权限配置与 MFA 管理员重置域 API（M2-UI2：TD-137/TD-143/TD-126）。
 *
 * 与后端 `app/api/school_rbac.py` 响应模型一一对齐。错误（409 版本冲突、
 * 422 校验、404 跨租）由调用方从异常 message 里取 `{code, message}` 展示。
 */

import { buildQuery, type AuthSessionRequest, type Page } from "./org";

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
  /**
   * 能否经绑定接口授出（TD-187d）：超管角色为 false。服务端下发，前端不按 code 硬编码。
   * TD-187g-C 消费（绑定抽屉里过滤可绑定角色）；本点（TD-187g-B）还没有消费方。
   */
  bindable: boolean;
  /** 能否「恢复出厂」（TD-200）：服务端派生 = 有出厂定义 **且** 角色可编辑（两道硬门都过）。 */
  resettable: boolean;
}

export interface PermissionView {
  code: string;
  module: string;
  action: string;
  description: string;
  /** 自扩权敏感码（TD-191）：写进自己所在角色时后端要求显式确认，前端据此提前弹框。 */
  sensitive_self_grant: boolean;
}

export interface RolePermissionsInput {
  permission_codes: string[];
  version: number;
  reason?: string | null;
  /** 自扩权显式确认：后端在结果集触达操作者本人且净增敏感码时校验（超管免）。 */
  self_grant_ack?: boolean;
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
  /** 自扩权显式确认（TD-194）：重置会同时重种权限与范围，范围侧对 `school_admin` 持有人是可达路径。 */
  self_grant_ack?: boolean;
}

/** 创建自定义角色请求（TD-187b）。初始权限二选一：`permission_codes` 或 `copy_from_role_id`。 */
export interface RoleCreateInput {
  code: string;
  name: string;
  /** 初始权限清单（空集 = 无权限）。与 `copy_from_role_id` 互斥。 */
  permission_codes?: string[];
  /** 复制同租户某个可编辑角色的权限。不可编辑的角色（超管/学生/教师）复制会 422。 */
  copy_from_role_id?: string | null;
  reason?: string | null;
}

/** 删除自定义角色的影响面（预览与执行返回同一形状，TD-199）。 */
export interface RoleDeletionPreview {
  role_code: string;
  name: string;
  version: number;
  affected_users: number;
  permission_codes: string[];
  /** 影响面指纹：挡「预览后持有人集合被改」，原样回传即可，服务端只做相等比较。 */
  fingerprint: string;
}

/** 删除自定义角色请求：逐字 code + 理由 + 版本 + 影响面指纹（TD-199 双判据 CAS）。 */
export interface RoleDeleteInput {
  confirm_role_code: string;
  reason: string;
  version: number;
  fingerprint: string;
}

/** 角色上的一条数据范围行（`editable=false` 只展示不可改）。 */
export interface RoleScopeRow {
  scope_type: string;
  scope_id: string | null;
  editable: boolean;
}

/** 范围替换的事前影响面（预览与执行返回同一语义）。 */
export interface ScopeChangePreview {
  version: number;
  added: string[];
  removed: string[];
  affected_users: number;
  /** 候选清单为空：该角色持有人将看不到任何数据。 */
  empties_scope: boolean;
  /** 因只读而保留的范围行（只有范围类型，不含 scope_id——后端不下发 id）。 */
  retained_non_editable: string[];
  /** 本次新增后新可达操作者本人的范围。 */
  newly_reaching_self: string[];
  requires_confirmation: boolean;
}

/** 全量替换角色级数据范围（`version` 与权限编辑共用同一列，TD-202）。 */
export interface RoleScopeInput {
  scope_types: string[];
  version: number;
  reason?: string | null;
  self_grant_ack?: boolean;
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

/**
 * 权限账号列表的一行（TD-187g 决策 1，对齐 `PermissionAccountResponse`）：
 * **没有 `login_id`**——学号/工号属敏感标识符，后端故意不回显。
 */
export interface PermissionAccountView {
  id: string;
  display_name: string | null;
  user_type: string;
  /** `active` / `locked` / `disabled`（`deleted` 不列，DB CHECK 里没有 `pending`）。 */
  status: string;
  role_codes: string[];
  roles_version: number;
  mfa_enabled: boolean;
  /** 是否持有校方超管角色（服务端派生）：账号页据此关掉全部写控件，不按 `role_codes` 自己认。 */
  is_super_admin: boolean;
}

/** 可经绑定接口授出的角色（决策 2）：**只有身份，没有权限码清单**——矩阵属 `school.role.manage`。 */
export interface BindableRoleView {
  id: string;
  code: string;
  name: string;
  is_system: boolean;
}

/** 账号当前绑定的一个角色。`bindable=false` 的行只读展示，**不能从提交集合里静默删掉**（§20.3 C3）。 */
export interface BoundRoleView {
  id: string;
  code: string;
  name: string;
  is_system: boolean;
  bindable: boolean;
}

/** 一个账号的绑定快照 + 生效权限并集（`roles_version` 就是 `PUT` 要回传的 `version`）。 */
export interface UserBindingsView {
  user_id: string;
  display_name: string | null;
  user_type: string;
  status: string;
  roles_version: number;
  roles: BoundRoleView[];
  permission_codes: string[];
}

/**
 * 全量替换绑定：`role_ids` 是**替换后**的完整集合，空数组＝收回全部权限。
 * 绑定理由**没有 ≥10 字判据**（服务层只做去空白转 `None`），与停用 / MFA 重置不同一门。
 */
export interface BindingUpdateInput {
  role_ids: string[];
  version: number;
  reason?: string | null;
}

/** 替换结果：最新快照（`roles_version` 已 +1）+ 本次差异。 */
export interface BindingUpdateResult extends UserBindingsView {
  added_role_codes: string[];
  removed_role_codes: string[];
  added_permission_codes: string[];
  removed_permission_codes: string[];
}

/** 停用账号的请求体（服务层要求去空白后 ≥10 字；启用没有请求体）。 */
export interface DeactivateInput {
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
  /** 创建自定义角色（TD-187b）。 */
  createRole(input: RoleCreateInput): Promise<RoleView>;
  /** 删除影响面预览（只读；执行删除另需 `school.role.delete`）。 */
  previewRoleDeletion(roleId: string): Promise<RoleDeletionPreview>;
  deleteRole(roleId: string, input: RoleDeleteInput): Promise<RoleDeletionPreview>;
  /** 角色当前数据范围行（含只读行，供配置页渲染）。 */
  listRoleScopes(roleId: string): Promise<RoleScopeRow[]>;
  /** 范围替换的影响面预览（只读，不写库、不推高版本）。 */
  previewRoleScopes(roleId: string, scopeTypes: string[]): Promise<ScopeChangePreview>;
  replaceRoleScopes(roleId: string, input: RoleScopeInput): Promise<RoleView>;
  /** 本校权限账号列表（教师 + 校方管理员；**没有搜索参数**，只有游标分页）。 */
  listPermissionAccounts(query?: {
    limit?: number;
    cursor?: string;
  }): Promise<Page<PermissionAccountView>>;
  /** 可授角色目录（只持 `school.permission_account.manage` 也能独立绑定，不外泄权限矩阵）。 */
  listBindableRoles(): Promise<BindableRoleView[]>;
  getUserBindings(userId: string): Promise<UserBindingsView>;
  replaceUserBindings(userId: string, input: BindingUpdateInput): Promise<BindingUpdateResult>;
  /** 停用并撤销其会话：后端回 204，被撤销的会话条数只在审计里（拿不到）。 */
  deactivateUser(userId: string, input: DeactivateInput): Promise<void>;
  /** 启用：后端不接请求体，也不要求理由；回 204。 */
  activateUser(userId: string): Promise<void>;
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
    async createRole(input) {
      return request<RoleView>("/api/v1/school/roles", jsonInit("POST", input));
    },
    async previewRoleDeletion(roleId) {
      return request<RoleDeletionPreview>(`/api/v1/school/roles/${roleId}/delete/preview`, {
        method: "POST",
      });
    },
    async deleteRole(roleId, input) {
      return request<RoleDeletionPreview>(
        `/api/v1/school/roles/${roleId}/delete`,
        jsonInit("POST", input),
      );
    },
    async listRoleScopes(roleId) {
      return request<RoleScopeRow[]>(`/api/v1/school/roles/${roleId}/scopes`);
    },
    async previewRoleScopes(roleId, scopeTypes) {
      return request<ScopeChangePreview>(
        `/api/v1/school/roles/${roleId}/scopes/preview`,
        jsonInit("POST", { scope_types: scopeTypes }),
      );
    },
    async replaceRoleScopes(roleId, input) {
      return request<RoleView>(`/api/v1/school/roles/${roleId}/scopes`, jsonInit("PUT", input));
    },
    async listPermissionAccounts(query = {}) {
      const qs = buildQuery({ limit: query.limit, cursor: query.cursor });
      return request<Page<PermissionAccountView>>(`/api/v1/school/permission-accounts${qs}`);
    },
    async listBindableRoles() {
      return request<BindableRoleView[]>("/api/v1/school/bindable-roles");
    },
    async getUserBindings(userId) {
      return request<UserBindingsView>(`/api/v1/school/users/${userId}/roles`);
    },
    async replaceUserBindings(userId, input) {
      return request<BindingUpdateResult>(
        `/api/v1/school/users/${userId}/roles`,
        jsonInit("PUT", input),
      );
    },
    async deactivateUser(userId, input) {
      await request<void>(`/api/v1/school/users/${userId}/deactivate`, jsonInit("POST", input));
    },
    async activateUser(userId) {
      await request<void>(`/api/v1/school/users/${userId}/activate`, { method: "POST" });
    },
  };
}
