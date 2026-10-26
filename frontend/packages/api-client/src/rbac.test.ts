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
    });
    expect(calls[0].path).toBe("/api/v1/school/roles/r-1/reset");
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      confirm_role_code: "school_admin",
      reason: "误改后恢复出厂默认权限",
      version: 3,
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
});
