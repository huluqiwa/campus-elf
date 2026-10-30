import { describe, expect, it, vi } from "vitest";

import { createRbacApi } from "./rbac";

function sessionStub(handler: (path: string, init?: RequestInit) => unknown): {
  session: { request: <T>(path: string, init?: RequestInit) => Promise<T> };
  calls: { path: string; init?: RequestInit }[];
} {
  const calls: { path: string; init?: RequestInit }[] = [];
  const request = vi.fn(async <T>(path: string, init?: RequestInit): Promise<T> => {
    calls.push({ path, init });
    return handler(path, init) as T;
  });
  return {
    session: { request: request as <T>(path: string, init?: RequestInit) => Promise<T> },
    calls,
  };
}

const ROLE_VIEW = {
  id: "r-1",
  code: "school_admin",
  name: "校方管理员",
  is_system: true,
  editable: true,
  lock_reason: null,
  version: 3,
  defaults_applied: true,
  user_count: 2,
  permission_codes: ["school.dashboard.view"],
  bindable: true,
  resettable: true,
};

const BINDINGS_VIEW = {
  user_id: "u-1",
  display_name: "张三",
  user_type: "teacher",
  status: "active",
  roles_version: 4,
  roles: [{ id: "r-1", code: "teacher", name: "教师", is_system: true, bindable: true }],
  permission_codes: ["school.personnel.view"],
};

describe("createRbacApi", () => {
  it("listRoles / listPermissions 走 GET 且不带查询参数", async () => {
    const { session, calls } = sessionStub(() => []);
    const api = createRbacApi(session);
    await api.listRoles();
    await api.listPermissions();
    expect(calls.map((call) => call.path)).toEqual([
      "/api/v1/school/roles",
      "/api/v1/school/permissions",
    ]);
    expect(calls[0].init).toBeUndefined();
  });

  it("replaceRolePermissions 以 PUT 全量提交权限码与版本", async () => {
    const { session, calls } = sessionStub(() => ROLE_VIEW);
    const view = await createRbacApi(session).replaceRolePermissions("r-1", {
      permission_codes: ["school.dashboard.view", "school.role.manage"],
      version: 3,
      reason: "新增角色权限管理",
    });
    expect(view).toEqual(ROLE_VIEW);
    expect(calls[0].path).toBe("/api/v1/school/roles/r-1/permissions");
    expect(calls[0].init?.method).toBe("PUT");
    expect(calls[0].init?.headers).toEqual({ "Content-Type": "application/json" });
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      permission_codes: ["school.dashboard.view", "school.role.manage"],
      version: 3,
      reason: "新增角色权限管理",
    });
  });

  it("previewRoleReset 是无请求体的 POST（只读预览）", async () => {
    const { session, calls } = sessionStub(() => ({
      version: 3,
      permissions_added: ["school.personnel.view"],
      permissions_removed: [],
      scopes_added: ["school"],
      scopes_removed: [],
      affected_users: 2,
    }));
    const preview = await createRbacApi(session).previewRoleReset("r-1");
    expect(calls[0].path).toBe("/api/v1/school/roles/r-1/reset/preview");
    expect(calls[0].init).toEqual({ method: "POST" });
    expect(preview.affected_users).toBe(2);
  });

  it("resetRole 提交角色 code 二次确认与理由", async () => {
    const { session, calls } = sessionStub(() => ({
      version: 4,
      permissions_added: [],
      permissions_removed: [],
      scopes_added: [],
      scopes_removed: [],
      affected_users: 2,
    }));
    await createRbacApi(session).resetRole("r-1", {
      confirm_role_code: "school_admin",
      reason: "误改后恢复出厂默认权限",
      version: 3,
      self_grant_ack: true,
    });
    expect(calls[0].path).toBe("/api/v1/school/roles/r-1/reset");
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      confirm_role_code: "school_admin",
      reason: "误改后恢复出厂默认权限",
      version: 3,
      self_grant_ack: true,
    });
  });

  it("lookupUser 把登录名编码进查询串", async () => {
    const { session, calls } = sessionStub(() => ({
      id: "u-1",
      display_name: "王老师",
      user_type: "teacher",
      status: "active",
      mfa_enabled: true,
      roles: [],
    }));
    const user = await createRbacApi(session).lookupUser("E 9001/+");
    expect(calls[0].path).toBe("/api/v1/school/users/lookup?login_id=E+9001%2F%2B");
    expect(user.mfa_enabled).toBe(true);
  });

  it("resetUserMfa 走 POST 且理由在请求体，响应为空", async () => {
    const { session, calls } = sessionStub(() => undefined);
    const result = await createRbacApi(session).resetUserMfa("u-1", {
      reason: "员工换手机无法接收动态码",
    });
    expect(result).toBeUndefined();
    expect(calls[0].path).toBe("/api/v1/school/users/u-1/mfa/reset");
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ reason: "员工换手机无法接收动态码" });
  });

  it("会话层错误原样传播（409 detail 由调用方解析）", async () => {
    const failure = new Error('HTTP 409: {"code":"ROLE_VERSION_CONFLICT"}');
    const request = vi.fn(async () => {
      throw failure;
    });
    const api = createRbacApi({
      request: request as <T>(path: string, init?: RequestInit) => Promise<T>,
    });
    await expect(
      api.replaceRolePermissions("r-1", { permission_codes: [], version: 1 }),
    ).rejects.toThrow("ROLE_VERSION_CONFLICT");
  });

  it("createRole 以 POST 提交 code/name 与初始权限来源", async () => {
    const { session, calls } = sessionStub(() => ROLE_VIEW);
    const created = await createRbacApi(session).createRole({
      code: "campus_operator",
      name: "校区运营",
      permission_codes: ["school.kb.view"],
      reason: "新设校区运营岗",
    });
    expect(created).toEqual(ROLE_VIEW);
    expect(calls[0].path).toBe("/api/v1/school/roles");
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      code: "campus_operator",
      name: "校区运营",
      permission_codes: ["school.kb.view"],
      reason: "新设校区运营岗",
    });
  });

  it("createRole 复制模式只提交 copy_from_role_id（不与 permission_codes 同时给）", async () => {
    const { session, calls } = sessionStub(() => ROLE_VIEW);
    await createRbacApi(session).createRole({
      code: "campus_operator",
      name: "校区运营",
      copy_from_role_id: "r-9",
    });
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      code: "campus_operator",
      name: "校区运营",
      copy_from_role_id: "r-9",
    });
  });

  it("previewRoleDeletion 是无请求体的 POST（只读预览）", async () => {
    const { session, calls } = sessionStub(() => ({
      role_code: "campus_operator",
      name: "校区运营",
      version: 2,
      affected_users: 3,
      permission_codes: ["school.kb.view"],
      fingerprint: "a".repeat(64),
    }));
    const preview = await createRbacApi(session).previewRoleDeletion("r-1");
    expect(calls[0].path).toBe("/api/v1/school/roles/r-1/delete/preview");
    expect(calls[0].init).toEqual({ method: "POST" });
    expect(preview.fingerprint).toHaveLength(64);
    expect(preview.affected_users).toBe(3);
  });

  it("deleteRole 原样回传指纹与版本", async () => {
    const { session, calls } = sessionStub(() => ({
      role_code: "campus_operator",
      name: "校区运营",
      version: 2,
      affected_users: 3,
      permission_codes: [],
      fingerprint: "a".repeat(64),
    }));
    await createRbacApi(session).deleteRole("r-1", {
      confirm_role_code: "campus_operator",
      reason: "岗位撤销，角色不再使用",
      version: 2,
      fingerprint: "a".repeat(64),
    });
    expect(calls[0].path).toBe("/api/v1/school/roles/r-1/delete");
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      confirm_role_code: "campus_operator",
      reason: "岗位撤销，角色不再使用",
      version: 2,
      fingerprint: "a".repeat(64),
    });
  });

  it("listRoleScopes 走 GET 且读回范围行", async () => {
    const { session, calls } = sessionStub(() => [
      { scope_type: "school", scope_id: null, editable: true },
      { scope_type: "class", scope_id: "c-1", editable: false },
    ]);
    const rows = await createRbacApi(session).listRoleScopes("r-1");
    expect(calls[0].path).toBe("/api/v1/school/roles/r-1/scopes");
    expect(calls[0].init).toBeUndefined();
    expect(rows).toHaveLength(2);
    expect(rows[1].editable).toBe(false);
  });

  it("previewRoleScopes 以 POST 提交候选范围清单", async () => {
    const { session, calls } = sessionStub(() => ({
      version: 4,
      added: ["self"],
      removed: ["school"],
      affected_users: 2,
      empties_scope: false,
      retained_non_editable: ["class:c-1"],
      newly_reaching_self: ["self"],
      requires_confirmation: true,
    }));
    const preview = await createRbacApi(session).previewRoleScopes("r-1", ["self"]);
    expect(calls[0].path).toBe("/api/v1/school/roles/r-1/scopes/preview");
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ scope_types: ["self"] });
    expect(preview.empties_scope).toBe(false);
    expect(preview.requires_confirmation).toBe(true);
  });

  it("replaceRoleScopes 以 PUT 提交范围清单与版本", async () => {
    const { session, calls } = sessionStub(() => ROLE_VIEW);
    const updated = await createRbacApi(session).replaceRoleScopes("r-1", {
      scope_types: ["school", "self"],
      version: 3,
      reason: "调整到全校可见",
      self_grant_ack: true,
    });
    expect(updated).toEqual(ROLE_VIEW);
    expect(calls[0].path).toBe("/api/v1/school/roles/r-1/scopes");
    expect(calls[0].init?.method).toBe("PUT");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      scope_types: ["school", "self"],
      version: 3,
      reason: "调整到全校可见",
      self_grant_ack: true,
    });
  });

  it("listPermissionAccounts 只带 limit/cursor（后端没有搜索参数）", async () => {
    const { session, calls } = sessionStub(() => ({ items: [], next_cursor: null }));
    const api = createRbacApi(session);
    await api.listPermissionAccounts({ limit: 50, cursor: "cur-2" });
    await api.listPermissionAccounts();
    expect(calls[0].path).toBe("/api/v1/school/permission-accounts?limit=50&cursor=cur-2");
    expect(calls[1].path).toBe("/api/v1/school/permission-accounts");
    expect(calls[0].init).toBeUndefined();
  });

  it("listBindableRoles 走 GET，返回的身份清单不含权限矩阵", async () => {
    const { session, calls } = sessionStub(() => [
      { id: "r-1", code: "teacher", name: "教师", is_system: true },
    ]);
    const roles = await createRbacApi(session).listBindableRoles();
    expect(calls[0].path).toBe("/api/v1/school/bindable-roles");
    expect(calls[0].init).toBeUndefined();
    expect(roles).toEqual([{ id: "r-1", code: "teacher", name: "教师", is_system: true }]);
    expect(Object.keys(roles[0])).not.toContain("permission_codes");
  });

  it("getUserBindings 读回快照：每行带 bindable、整体带 roles_version", async () => {
    const { session, calls } = sessionStub(() => BINDINGS_VIEW);
    const view = await createRbacApi(session).getUserBindings("u-1");
    expect(calls[0].path).toBe("/api/v1/school/users/u-1/roles");
    expect(calls[0].init).toBeUndefined();
    expect(view.roles_version).toBe(4);
    expect(view.roles[0].bindable).toBe(true);
    expect(view.permission_codes).toEqual(["school.personnel.view"]);
  });

  it("replaceUserBindings 以 PUT 全量提交 role_ids 与 version（空数组=收回全部，照样发）", async () => {
    const { session, calls } = sessionStub(() => ({
      ...BINDINGS_VIEW,
      roles_version: 5,
      added_role_codes: [],
      removed_role_codes: ["teacher"],
      added_permission_codes: [],
      removed_permission_codes: ["school.personnel.view"],
    }));
    const result = await createRbacApi(session).replaceUserBindings("u-1", {
      role_ids: [],
      version: 4,
    });
    expect(calls[0].path).toBe("/api/v1/school/users/u-1/roles");
    expect(calls[0].init?.method).toBe("PUT");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ role_ids: [], version: 4 });
    expect(result.roles_version).toBe(5);
    expect(result.removed_role_codes).toEqual(["teacher"]);
  });

  it("deactivateUser 以 POST 提交理由，204 不解析响应体", async () => {
    const { session, calls } = sessionStub(() => undefined);
    await expect(
      createRbacApi(session).deactivateUser("u-1", { reason: "教师离职，交接已完成" }),
    ).resolves.toBeUndefined();
    expect(calls[0].path).toBe("/api/v1/school/users/u-1/deactivate");
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ reason: "教师离职，交接已完成" });
  });

  it("activateUser 发 POST 但不带任何请求体（后端连 body 参数都不接）", async () => {
    const { session, calls } = sessionStub(() => undefined);
    await expect(createRbacApi(session).activateUser("u-1")).resolves.toBeUndefined();
    expect(calls[0].path).toBe("/api/v1/school/users/u-1/activate");
    expect(calls[0].init).toEqual({ method: "POST" });
    expect(calls[0].init?.body).toBeUndefined();
  });
});
