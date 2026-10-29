import type { Page } from "@playwright/test";

/**
 * 角色权限 / MFA 重置 / 自定义角色 / 数据范围接口 mock（M2-UI2 + M2-UI3 TD-187g-B）：
 * 与 org-mock 同风格的路径路由 + 请求体捕获。
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
  /** 能否经绑定接口授出（TD-187d）：超管角色为 false。 */
  bindable: boolean;
  /** 能否「恢复出厂」（TD-200）：服务端派生 = 有出厂定义 **且** 角色可编辑。 */
  resettable: boolean;
}

export interface PermissionFixture {
  code: string;
  module: string;
  action: string;
  description: string;
  sensitive_self_grant: boolean;
}

export interface MfaUserFixture {
  id: string;
  display_name: string | null;
  user_type: string;
  status: string;
  mfa_enabled: boolean;
  roles: string[];
}

/** 角色上的一条数据范围行（`editable=false` 是只读行，后端不允许本端点改它）。 */
export interface ScopeRowFixture {
  scope_type: string;
  scope_id: string | null;
  editable: boolean;
}

/** 敏感范围类型（后端 `app/rbac/self_grant.py` 的 `SENSITIVE_SCOPE_TYPES`）：只有 `school` 敏感。 */
const SENSITIVE_SCOPE_TYPES = ["school"];

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
  bindable: false,
  // 锁定角色：过不了 `_assert_editable` 这道门，服务端恒给 false
  resettable: false,
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
  bindable: true,
  resettable: true,
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
  bindable: true,
  resettable: true,
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
  bindable: true,
  resettable: false,
};

export const FIXTURE_PERMISSIONS: PermissionFixture[] = [
  {
    code: "school.dashboard.view",
    module: "school",
    action: "view",
    description: "查看校方基础看板",
    sensitive_self_grant: false,
  },
  {
    code: "school.personnel.view",
    module: "school",
    action: "view",
    description: "查看人员档案（按数据范围）",
    sensitive_self_grant: false,
  },
  {
    code: "school.personnel.mfa.reset",
    module: "school",
    action: "reset",
    description: "重置用户 MFA 绑定（需理由并审计）",
    // 与后端 SENSITIVE_SELF_GRANT_CODES 同口径：给自己补这一格需要显式确认
    sensitive_self_grant: true,
  },
  {
    code: "school.role.manage",
    module: "school",
    action: "manage",
    description: "维护角色与权限分配",
    sensitive_self_grant: true,
  },
  {
    code: "school.role.delete",
    module: "school",
    action: "delete",
    description: "删除自定义角色（需影响面指纹）",
    sensitive_self_grant: true,
  },
  {
    code: "school.kb.view",
    module: "school",
    action: "view",
    description: "查看知识库文档与构建进度",
    sensitive_self_grant: false,
  },
  {
    code: "school.kb.upload",
    module: "school",
    action: "upload",
    description: "上传知识库文档",
    sensitive_self_grant: false,
  },
  {
    code: "school.message.view",
    module: "school",
    action: "view",
    description: "查看校方消息",
    sensitive_self_grant: false,
  },
  {
    code: "school.message.send",
    module: "school",
    action: "send",
    description: "发送校方消息",
    sensitive_self_grant: false,
  },
  {
    code: "school.message.read",
    module: "school",
    action: "read",
    description: "标记校方消息已读",
    sensitive_self_grant: false,
  },
  {
    code: "campus.chat.use",
    module: "campus",
    action: "use",
    description: "使用 AI 对话",
    sensitive_self_grant: false,
  },
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
  createRole: Record<string, unknown>[];
  deletePreview: string[];
  deleteRole: Record<string, unknown>[];
  scopeList: string[];
  scopePreview: Record<string, unknown>[];
  scopeSave: Record<string, unknown>[];
}

export interface RbacMockOptions {
  roles?: RoleFixture[];
  permissions?: PermissionFixture[];
  mfaUsers?: Record<string, MfaUserFixture>;
  /** 覆盖保存接口响应（默认 200 + 应用后的角色）。 */
  saveResponse?: { status: number; body?: unknown };
  /**
   * 只让**第一次**保存失败（之后回到有状态默认行为）。
   * 用于"服务端判定与本页快照不一致"的路径：真实后端就是拒一次、补上确认再放行；
   * 用 saveResponse 会把两次都拒掉，测不出界面有没有把出口还给操作者。
   */
  saveRejectOnce?: { status: number; body: unknown };
  /** 覆盖重置执行响应（默认 200 + 影响面）。 */
  resetResponse?: { status: number; body?: unknown };
  /** 每个角色码的初始数据范围行（不给则用出厂默认）。 */
  scopes?: Record<string, ScopeRowFixture[]>;
  /**
   * 操作者所在角色码（自扩权判定用：改的是不是操作者自己所在的角色）。
   * 不给 = 操作者不在任何角色里 → 范围侧的自扩权一律不触发。
   *
   * **必须与 `mockAuth` 的 `user.roles` 写同一份**：真实后端两者本来就是同一个人的角色，
   * mock 拆成了两个来源——不同步会测出"界面以为操作者在角色里、服务端却不在"这种假象
   * （例如界面按超管免打断渲染，服务端却按普通主体要 ack）。
   */
  actorRoleCodes?: string[];
  /**
   * 操作者当前生效的范围类型并集（自扩权判据的"净增"基准）。
   * 不给则按"操作者所在角色的范围并集"推——真实后端就是从库里算这个并集。
   */
  actorScopeTypes?: string[];
  /** 覆盖范围保存响应（默认 200 + 应用后的角色）。 */
  scopeSaveResponse?: { status: number; body?: unknown };
  /**
   * 只让第一次范围保存失败（用于"本地快照落后于库"的路径）。
   * 真实后端就是拒一次、补上确认再放行；用 scopeSaveResponse 会把两次都拒掉，
   * 测不出界面有没有把出口还给操作者。
   */
  scopeSaveRejectOnce?: { status: number; body: unknown };
  /** 只让第一次删除执行失败（用于"影响面已变化"的重试路径）。 */
  deleteRejectOnce?: { status: number; body: unknown };
  /**
   * 第一次删除执行撞版本冲突（模拟"预览之后角色被别人改了"）。
   * mock 会**真的**把版本推高一格：否则重取预览拿到的还是同一个版本，
   * 测不出"重取预览"这一步有没有发生。
   */
  deleteVersionConflictOnce?: boolean;
}

export interface RbacMock {
  captures: RbacMockCaptures;
}

function roleIdFrom(url: string, roles: RoleFixture[]): RoleFixture | undefined {
  const path = new URL(url).pathname;
  const match = path.match(/\/school\/roles\/([^/]+)/);
  return match ? roles.find((role) => role.id === match[1]) : undefined;
}

/** 影响面指纹：真实后端是 SHA-256(role_id ∪ 排序后的持有人 id)，mock 只要稳定且可区分。 */
function fingerprintOf(role: RoleFixture): string {
  return `fp-${role.id}-${role.user_count}`;
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
    createRole: [],
    deletePreview: [],
    deleteRole: [],
    scopeList: [],
    scopePreview: [],
    scopeSave: [],
  };
  // 角色当前数据范围（mock 内部状态，不在 API 响应里）：初始=出厂，重置后回到出厂。
  const scopesByRole = new Map<string, ScopeRowFixture[]>(
    roles.map((role) => [
      role.id,
      (
        options.scopes?.[role.code] ??
        (FACTORY_SCOPES[role.code] ?? []).map((scopeType) => ({
          scope_type: scopeType,
          scope_id: null,
          editable: true,
        }))
      ).map((row) => ({ ...row })),
    ]),
  );
  let createdCount = 0;

  const json = (status: number, body: unknown) => ({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  const previewOf = (role: RoleFixture) => {
    const defaults = FACTORY_DEFAULTS[role.code] ?? [];
    const defaultScopes = FACTORY_SCOPES[role.code] ?? [];
    const currentScopes = (scopesByRole.get(role.id) ?? [])
      .filter((row) => row.editable)
      .map((row) => row.scope_type);
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

  const scopeRowsOf = (role: RoleFixture): ScopeRowFixture[] =>
    (scopesByRole.get(role.id) ?? []).map((row) => ({ ...row }));

  /**
   * 操作者当前生效的范围类型并集（自扩权判据的"净增"基准）。
   * 真实后端从库里算（`_principal_scope_types`），mock 按操作者所在角色的范围并集推。
   */
  const actorScopeUnion = (): string[] => {
    if (options.actorScopeTypes) return [...options.actorScopeTypes];
    const types = new Set<string>();
    for (const code of options.actorRoleCodes ?? []) {
      const role = roles.find((item) => item.code === code);
      if (!role) continue;
      for (const row of scopesByRole.get(role.id) ?? []) {
        if (row.editable) types.add(row.scope_type);
      }
    }
    return [...types];
  };

  /** 操作者是不是校方超管（后端 `_assert_self_grant` 对超管免打断、免留痕之外的校验）。 */
  const actorIsSuperAdmin = (): boolean =>
    (options.actorRoleCodes ?? []).includes("school_super_admin");

  const scopePreviewOf = (role: RoleFixture, submitted: string[]) => {
    const existing = scopeRowsOf(role);
    const before = new Set(existing.map((row) => row.scope_type));
    // 只读行不参与删除，替换后仍然生效（后端 `_resulting_scope_types` 同口径）
    const resulting = new Set([
      ...submitted,
      ...existing.filter((row) => !row.editable).map((row) => row.scope_type),
    ]);
    // 自扩权按后端 `evaluate_scope_self_grant` 实时算：改的是不是自己所在的角色 ×
    // 结果集里自己**还没有**的敏感范围（只有 `school` 敏感）。
    const actorInRole = (options.actorRoleCodes ?? []).includes(role.code);
    const held = new Set(actorScopeUnion());
    const selfGrant = actorInRole
      ? [...resulting]
          .filter((type) => SENSITIVE_SCOPE_TYPES.includes(type) && !held.has(type))
          .sort()
      : [];
    return {
      version: role.version,
      added: [...resulting].filter((type) => !before.has(type)).sort(),
      removed: [...before].filter((type) => !resulting.has(type)).sort(),
      affected_users: role.user_count,
      empties_scope: resulting.size === 0,
      retained_non_editable: existing
        .filter((row) => !row.editable)
        .map((row) => row.scope_type)
        .sort(),
      newly_reaching_self: selfGrant,
      requires_confirmation: selfGrant.length > 0,
    };
  };

  await page.route("**/api/v1/school/roles**", (route) => {
    const method = route.request().method();
    const url = route.request().url();
    const path = new URL(url).pathname;
    const role = roleIdFrom(url, roles);

    // 创建自定义角色：POST /school/roles（路径里没有 role id 段）
    if (method === "POST" && path === "/api/v1/school/roles") {
      const payload = route.request().postDataJSON() as {
        code?: string;
        name?: string;
        permission_codes?: string[];
        copy_from_role_id?: string;
      };
      captures.createRole.push(payload);
      if (payload.copy_from_role_id && (payload.permission_codes ?? []).length > 0) {
        return route.fulfill(
          json(422, {
            detail: {
              code: "ROLE_INITIAL_SOURCE_AMBIGUOUS",
              message: "初始权限只能二选一：`permission_codes` 或 `copy_from_role_id`",
            },
          }),
        );
      }
      if (roles.some((item) => item.code === payload.code)) {
        return route.fulfill(
          json(409, { detail: { code: "ROLE_CODE_TAKEN", message: "同码角色已存在" } }),
        );
      }
      const source = payload.copy_from_role_id
        ? roles.find((item) => item.id === payload.copy_from_role_id)
        : undefined;
      createdCount += 1;
      const created: RoleFixture = {
        id: `40000000-0000-0000-0000-00000000${String(createdCount).padStart(4, "0")}`,
        code: String(payload.code ?? ""),
        name: String(payload.name ?? ""),
        is_system: false,
        editable: true,
        lock_reason: null,
        version: 0,
        defaults_applied: false,
        user_count: 0,
        permission_codes: source
          ? [...source.permission_codes]
          : [...(payload.permission_codes ?? [])],
        bindable: true,
        // 自定义角色没有出厂定义 → 恢复出厂按钮不亮（TD-200）
        resettable: false,
      };
      roles.push(created);
      scopesByRole.set(created.id, []);
      return route.fulfill(json(201, { ...created }));
    }

    if (url.includes("/delete/preview")) {
      captures.deletePreview.push(url);
      if (!role)
        return route.fulfill(
          json(404, { detail: { code: "ROLE_NOT_FOUND", message: "角色不存在" } }),
        );
      if (role.is_system) {
        return route.fulfill(
          json(409, { detail: { code: "ROLE_NOT_DELETABLE", message: "内置角色不可删除" } }),
        );
      }
      return route.fulfill(
        json(200, {
          role_code: role.code,
          name: role.name,
          version: role.version,
          affected_users: role.user_count,
          permission_codes: [...role.permission_codes],
          fingerprint: fingerprintOf(role),
        }),
      );
    }
    if (url.includes("/delete")) {
      const payload = route.request().postDataJSON() as Record<string, unknown>;
      captures.deleteRole.push(payload);
      if (options.deleteRejectOnce && captures.deleteRole.length === 1) {
        return route.fulfill(json(options.deleteRejectOnce.status, options.deleteRejectOnce.body));
      }
      if (options.deleteVersionConflictOnce && captures.deleteRole.length === 1 && role) {
        role.version += 1;
        return route.fulfill(
          json(409, {
            detail: {
              code: "ROLE_VERSION_CONFLICT",
              message: "角色已被他人修改，请刷新后重试",
              latest: { ...role },
            },
          }),
        );
      }
      // 校验顺序与后端一致（`backend/app/rbac/admin.py` 的 delete_role）：
      // 理由 ≥10 字 → 404 → 409 内置 → 确认码 → 版本 → 指纹。
      // 顺序不同的话，"理由太短"与"确认码打错"会返回相反的码，界面分支就测错了。
      if (String(payload.reason ?? "").trim().length < 10) {
        return route.fulfill(
          json(422, {
            detail: { code: "ROLE_DELETE_REASON_REQUIRED", message: "删除理由至少 10 个字符" },
          }),
        );
      }
      if (!role) {
        return route.fulfill(
          json(404, { detail: { code: "ROLE_NOT_FOUND", message: "角色不存在" } }),
        );
      }
      if (role.is_system) {
        return route.fulfill(
          json(409, { detail: { code: "ROLE_NOT_DELETABLE", message: "内置角色不可删除" } }),
        );
      }
      if (payload.confirm_role_code !== role.code) {
        return route.fulfill(
          json(422, {
            detail: { code: "ROLE_CODE_MISMATCH", message: "确认角色码与目标角色不一致" },
          }),
        );
      }
      // 双判据 CAS（TD-199）：版本挡"预览后角色配置被改"，指纹挡"预览后持有人集合被改"。
      // 少了这道校验，界面就测不出"预览过期会自己把人带回正路"。
      if (Number(payload.version) !== role.version) {
        return route.fulfill(
          json(409, {
            detail: {
              code: "ROLE_VERSION_CONFLICT",
              message: "角色已被他人修改，请刷新后重试",
              latest: { ...role },
            },
          }),
        );
      }
      if (payload.fingerprint !== fingerprintOf(role)) {
        return route.fulfill(
          json(409, {
            detail: {
              code: "ROLE_DELETE_PREVIEW_STALE",
              message: "删除影响面已变化（持有人集合与预览不一致），请刷新后重新确认",
              latest_preview: {
                role_code: role.code,
                name: role.name,
                version: role.version,
                affected_users: role.user_count,
                permission_codes: [...role.permission_codes],
                fingerprint: fingerprintOf(role),
              },
            },
          }),
        );
      }
      const index = roles.findIndex((item) => item.id === role.id);
      roles.splice(index, 1);
      scopesByRole.delete(role.id);
      return route.fulfill(
        json(200, {
          role_code: role.code,
          name: role.name,
          version: role.version,
          affected_users: role.user_count,
          permission_codes: [...role.permission_codes],
          fingerprint: fingerprintOf(role),
        }),
      );
    }

    if (url.includes("/scopes/preview")) {
      const payload = route.request().postDataJSON() as { scope_types?: string[] };
      captures.scopePreview.push(payload);
      if (!role) {
        return route.fulfill(
          json(404, { detail: { code: "ROLE_NOT_FOUND", message: "角色不存在" } }),
        );
      }
      return route.fulfill(json(200, scopePreviewOf(role, payload.scope_types ?? [])));
    }
    if (url.includes("/scopes")) {
      if (!role) {
        return route.fulfill(
          json(404, { detail: { code: "ROLE_NOT_FOUND", message: "角色不存在" } }),
        );
      }
      if (method === "GET") {
        captures.scopeList.push(url);
        return route.fulfill(json(200, scopeRowsOf(role)));
      }
      const payload = route.request().postDataJSON() as Record<string, unknown>;
      captures.scopeSave.push(payload);
      if (options.scopeSaveResponse) {
        return route.fulfill(
          json(options.scopeSaveResponse.status, options.scopeSaveResponse.body ?? { detail: {} }),
        );
      }
      if (options.scopeSaveRejectOnce && captures.scopeSave.length === 1) {
        return route.fulfill(
          json(
            options.scopeSaveRejectOnce.status,
            options.scopeSaveRejectOnce.body ?? { detail: {} },
          ),
        );
      }
      const submitted = (payload.scope_types ?? []) as string[];
      const preview = scopePreviewOf(role, submitted);
      // 版本 CAS（TD-202：权限区与范围区共用 `roles.version`）
      if (Number(payload.version) !== role.version) {
        return route.fulfill(
          json(409, {
            detail: {
              code: "ROLE_VERSION_CONFLICT",
              message: "角色已被他人修改，请刷新后重试",
              latest: { ...role },
            },
          }),
        );
      }
      // 超管主体后端免 ack（`_assert_self_grant` 的豁免），mock 同口径，否则超管的正常保存会被
      // mock 拒成 422——测出来的是 mock 的错，不是界面的错。
      if (preview.requires_confirmation && !actorIsSuperAdmin()) {
        if (payload.self_grant_ack !== true) {
          return route.fulfill(
            json(422, {
              detail: {
                code: "SELF_GRANT_CONFIRMATION_REQUIRED",
                message: `本次修改会扩大自己可见的数据范围：${preview.newly_reaching_self.join("、")}；请显式确认（self_grant_ack）并填写理由`,
              },
            }),
          );
        }
        // 勾了确认但没填理由：后端 `_assert_self_grant` 第二道判据（理由非空即可，不要求 10 字）。
        if (!String(payload.reason ?? "").trim()) {
          return route.fulfill(
            json(422, {
              detail: { code: "SELF_GRANT_REASON_REQUIRED", message: "自扩权必须填写理由" },
            }),
          );
        }
      }
      // 只写可编辑那两类；只读行不归这条写路径所有
      scopesByRole.set(role.id, [
        ...scopeRowsOf(role).filter((row) => !row.editable),
        ...submitted.map((scopeType) => ({
          scope_type: scopeType,
          scope_id: null,
          editable: true,
        })),
      ]);
      role.version += 1;
      return route.fulfill(json(200, { ...role }));
    }

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
      scopesByRole.set(
        role.id,
        (FACTORY_SCOPES[role.code] ?? []).map((scopeType) => ({
          scope_type: scopeType,
          scope_id: null,
          editable: true,
        })),
      );
      role.version += 1;
      return route.fulfill(json(200, preview));
    }
    if (url.includes("/permissions")) {
      captures.savePermissions.push(route.request().postDataJSON() as Record<string, unknown>);
      if (options.saveRejectOnce && captures.savePermissions.length === 1) {
        return route.fulfill(json(options.saveRejectOnce.status, options.saveRejectOnce.body));
      }
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
