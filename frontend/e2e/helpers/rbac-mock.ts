import type { Page } from "@playwright/test";

/**
 * 角色权限 / MFA 重置接口 mock（M2-UI2）：与 org-mock 同风格的路径路由 + 请求体捕获。
 * 保存与重置是**有状态**的（与真实后端一致）：PUT 应用权限码并 version+1，
 * 重置恢复出厂清单，页面刷新前就能反映新状态，避免"只断言请求发出、不断言界面跟随"。
 */

export interface RoleFixture {
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

export interface PermissionFixture {
  code: string;
  module: string;
  action: string;
  description: string;
}

export interface MfaUserFixture {
  id: string;
  display_name: string | null;
  user_type: string;
  status: string;
  mfa_enabled: boolean;
  roles: string[];
}

const SUPER_ADMIN: RoleFixture = {
  id: "40000000-0000-0000-0000-000000000001",
  code: "school_super_admin",
  name: "校方超级管理员",
  is_system: true,
  editable: false,
  lock_reason: "超级管理员默认持有本校全部权限，只能由平台侧调整",
  version: 1,
  defaults_applied: true,
  user_count: 1,
  permission_codes: ["school.dashboard.view", "school.role.manage"],
};

export const SCHOOL_ADMIN_DEFAULTS = [
  "school.dashboard.view",
  "school.personnel.view",
  "school.kb.view",
  "school.message.view",
  "school.message.send",
  "school.message.read",
];

const SCHOOL_ADMIN: RoleFixture = {
  id: "40000000-0000-0000-0000-000000000002",
  code: "school_admin",
  name: "校方授权管理员",
  is_system: true,
  editable: true,
  lock_reason: null,
  version: 3,
  defaults_applied: true,
  user_count: 2,
  // 故意偏离出厂（便于断言重置预览的新增/移除）
  permission_codes: ["school.dashboard.view"],
};

const TEACHER_B_END: RoleFixture = {
  id: "40000000-0000-0000-0000-000000000003",
  code: "teacher_b_end",
  name: "教师（B 端）",
  is_system: true,
  editable: true,
  lock_reason: null,
  version: 1,
  defaults_applied: true,
  user_count: 5,
  permission_codes: ["school.kb.upload"],
};

const STUDENT: RoleFixture = {
  id: "40000000-0000-0000-0000-000000000004",
  code: "student",
  name: "学生",
  is_system: true,
  editable: false,
  lock_reason: "内置角色，权限与数据范围由系统固定",
  version: 1,
  defaults_applied: true,
  user_count: 300,
  permission_codes: ["campus.chat.use"],
};

export const FIXTURE_PERMISSIONS: PermissionFixture[] = [
  {
    code: "school.dashboard.view",
    module: "school",
    action: "view",
    description: "查看校方基础看板",
  },
  {
    code: "school.personnel.view",
    module: "school",
    action: "view",
    description: "查看人员档案（按数据范围）",
  },
  {
    code: "school.personnel.mfa.reset",
    module: "school",
    action: "reset",
    description: "重置用户 MFA 绑定（需理由并审计）",
  },
  {
    code: "school.role.manage",
    module: "school",
    action: "manage",
    description: "维护角色与权限分配",
  },
  {
    code: "school.kb.view",
    module: "school",
    action: "view",
    description: "查看知识库文档与构建进度",
  },
  { code: "school.kb.upload", module: "school", action: "upload", description: "上传知识库文档" },
  { code: "school.message.view", module: "school", action: "view", description: "查看校方消息" },
  { code: "school.message.send", module: "school", action: "send", description: "发送校方消息" },
  {
    code: "school.message.read",
    module: "school",
    action: "read",
    description: "标记校方消息已读",
  },
  { code: "campus.chat.use", module: "campus", action: "use", description: "使用 AI 对话" },
];

export const FIXTURE_MFA_USERS: Record<string, MfaUserFixture> = {
  "E-9001": {
    id: "50000000-0000-0000-0000-000000000001",
    display_name: "王老师",
    user_type: "teacher",
    status: "active",
    mfa_enabled: true,
    roles: ["teacher_b_end"],
  },
};

/** 出厂默认清单（重置据此重种，与后端 seeds 同口径）。 */
const FACTORY_DEFAULTS: Record<string, string[]> = {
  school_admin: SCHOOL_ADMIN_DEFAULTS,
  teacher_b_end: ["school.kb.upload"],
};

/**
 * 出厂数据范围（与后端 `rbac_builtin_role_scope_defaults` 同口径）：
 * `school_admin` 看全校、`teacher_b_end` 只看自己。预览按「出厂 - 当前」求差，
 * 未动过范围的角色两个差集都为空——真实后端就是这个形状，mock 不能恒定回 `["school"]`。
 */
const FACTORY_SCOPES: Record<string, string[]> = {
  school_admin: ["school"],
  teacher_b_end: ["self"],
};

export interface RbacMockCaptures {
  savePermissions: Record<string, unknown>[];
  resetPreview: string[];
  reset: Record<string, unknown>[];
  lookup: string[];
  mfaReset: Record<string, unknown>[];
}

export interface RbacMockOptions {
  roles?: RoleFixture[];
  permissions?: PermissionFixture[];
  mfaUsers?: Record<string, MfaUserFixture>;
  /** 覆盖保存接口响应（默认 200 + 应用后的角色）。 */
  saveResponse?: { status: number; body?: unknown };
  /** 覆盖重置执行响应（默认 200 + 影响面）。 */
  resetResponse?: { status: number; body?: unknown };
}

export interface RbacMock {
  captures: RbacMockCaptures;
}

function roleIdFrom(url: string, roles: RoleFixture[]): RoleFixture | undefined {
  const path = new URL(url).pathname;
  const match = path.match(/\/school\/roles\/([^/]+)/);
  return match ? roles.find((role) => role.id === match[1]) : undefined;
}

export async function mockRbacApi(page: Page, options: RbacMockOptions = {}): Promise<RbacMock> {
  const roles: RoleFixture[] = (
    options.roles ?? [SUPER_ADMIN, SCHOOL_ADMIN, TEACHER_B_END, STUDENT]
  ).map((role) => ({ ...role, permission_codes: [...role.permission_codes] }));
  const permissions = options.permissions ?? FIXTURE_PERMISSIONS;
  const mfaUsers = options.mfaUsers ?? FIXTURE_MFA_USERS;
  const captures: RbacMockCaptures = {
    savePermissions: [],
    resetPreview: [],
    reset: [],
    lookup: [],
    mfaReset: [],
  };
  // 角色当前数据范围（mock 内部状态，不在 API 响应里）：初始=出厂，重置后回到出厂。
  const scopesByRole = new Map<string, string[]>(
    roles.map((role) => [role.id, [...(FACTORY_SCOPES[role.code] ?? [])]]),
  );

  const json = (status: number, body: unknown) => ({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  const previewOf = (role: RoleFixture) => {
    const defaults = FACTORY_DEFAULTS[role.code] ?? [];
    const defaultScopes = FACTORY_SCOPES[role.code] ?? [];
    const currentScopes = scopesByRole.get(role.id) ?? [];
    const current = new Set(role.permission_codes);
    const defaultSet = new Set(defaults);
    const currentScopeSet = new Set(currentScopes);
    const defaultScopeSet = new Set(defaultScopes);
    return {
      version: role.version,
      permissions_added: defaults.filter((code) => !current.has(code)),
      permissions_removed: role.permission_codes.filter((code) => !defaultSet.has(code)),
      scopes_added: defaultScopes.filter((scope) => !currentScopeSet.has(scope)),
      scopes_removed: currentScopes.filter((scope) => !defaultScopeSet.has(scope)),
      affected_users: role.user_count,
    };
  };

  await page.route("**/api/v1/school/roles**", (route) => {
    const method = route.request().method();
    const url = route.request().url();
    const role = roleIdFrom(url, roles);

    if (url.includes("/reset/preview")) {
      captures.resetPreview.push(url);
      if (!role)
        return route.fulfill(
          json(404, { detail: { code: "ROLE_NOT_FOUND", message: "角色不存在" } }),
        );
      return route.fulfill(json(200, previewOf(role)));
    }
    if (url.includes("/reset")) {
      captures.reset.push(route.request().postDataJSON() as Record<string, unknown>);
      // 强制失败要先返回：真实后端失败时不改状态，否则后续断言会读到"假成功"后的状态。
      if (options.resetResponse) {
        return route.fulfill(json(options.resetResponse.status, options.resetResponse.body ?? {}));
      }
      if (!role || !role.editable) {
        return route.fulfill(
          json(409, { detail: { code: "ROLE_IMMUTABLE", message: "内置锁定角色不可重置" } }),
        );
      }
      const preview = previewOf(role);
      role.permission_codes = [...(FACTORY_DEFAULTS[role.code] ?? [])];
      scopesByRole.set(role.id, [...(FACTORY_SCOPES[role.code] ?? [])]);
      role.version += 1;
      return route.fulfill(json(200, preview));
    }
    if (url.includes("/permissions")) {
      captures.savePermissions.push(route.request().postDataJSON() as Record<string, unknown>);
      if (options.saveResponse) {
        return route.fulfill(
          json(options.saveResponse.status, options.saveResponse.body ?? { detail: {} }),
        );
      }
      if (!role) {
        return route.fulfill(
          json(404, { detail: { code: "ROLE_NOT_FOUND", message: "角色不存在或不属于本校" } }),
        );
      }
      const payload = captures.savePermissions.at(-1) as { permission_codes?: string[] };
      role.permission_codes = [...(payload.permission_codes ?? [])];
      role.version += 1;
      return route.fulfill(json(200, { ...role }));
    }
    if (method === "GET") {
      return route.fulfill(
        json(
          200,
          roles.map((listed) => ({ ...listed })),
        ),
      );
    }
    return route.fulfill(json(405, { detail: "unexpected request" }));
  });

  await page.route("**/api/v1/school/permissions**", (route) =>
    route.fulfill(json(200, permissions)),
  );

  await page.route("**/api/v1/school/users/lookup**", (route) => {
    const loginId = new URL(route.request().url()).searchParams.get("login_id") ?? "";
    captures.lookup.push(loginId);
    const user = mfaUsers[loginId];
    if (!user) {
      return route.fulfill(
        json(404, {
          detail: { code: "USER_NOT_FOUND", message: "未找到该登录名对应的用户" },
        }),
      );
    }
    return route.fulfill(json(200, user));
  });

  await page.route("**/api/v1/school/users/*/mfa/reset", (route) => {
    captures.mfaReset.push(route.request().postDataJSON() as Record<string, unknown>);
    const userId = new URL(route.request().url()).pathname.split("/").at(-3);
    const user = Object.values(mfaUsers).find((item) => item.id === userId);
    if (!user) {
      return route.fulfill(
        json(404, { detail: { code: "USER_NOT_FOUND", message: "用户不存在或不属于本校" } }),
      );
    }
    if (!user.mfa_enabled) {
      return route.fulfill(
        json(409, { detail: { code: "MFA_NOT_ENABLED", message: "该用户未绑定 MFA" } }),
      );
    }
    const reason = String((captures.mfaReset.at(-1) as { reason?: string }).reason ?? "");
    if (reason.trim().length < 10) {
      return route.fulfill(
        json(422, { detail: { code: "RESET_REASON_REQUIRED", message: "重置理由至少 10 个字" } }),
      );
    }
    user.mfa_enabled = false;
    return route.fulfill({ status: 204 });
  });

  return { captures };
}
