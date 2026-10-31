/** 角色权限页的纯逻辑（M2-UI2 + M2-UI3 TD-187g-B）：与后端校验同口径的前端预检，便于单元测试。 */

import { ApiError } from "@campus-elf/api-client";
import type {
  PermissionView,
  RoleDeletionPreview,
  RoleScopeRow,
  RoleView,
} from "@campus-elf/api-client";

import { codePointLength } from "./logic";

export interface PermissionGroup {
  module: string;
  label: string;
  items: PermissionView[];
}

const MODULE_LABELS: Record<string, string> = {
  school: "校方后台",
  campus: "学生 / 教师端",
};

/** 撤掉它 = 关掉本页入口；操作者本人在该角色里即为自锁，保存前必须提示。 */
export const ROLE_MANAGE_CODE = "school.role.manage";

/** 按权限码前缀（module）分组；模块顺序稳定，便于界面与测试断言。 */
export function groupPermissions(permissions: PermissionView[]): PermissionGroup[] {
  const byModule = new Map<string, PermissionView[]>();
  for (const item of permissions) {
    const list = byModule.get(item.module) ?? [];
    list.push(item);
    byModule.set(item.module, list);
  }
  return [...byModule.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([module, items]) => ({ module, label: MODULE_LABELS[module] ?? module, items }));
}

/** 全量替换的差异（保持提交顺序，便于提示"新增/移除"数量与内容）。 */
export function permissionDiff(
  current: string[],
  next: string[],
): { added: string[]; removed: string[] } {
  const currentSet = new Set(current);
  const nextSet = new Set(next);
  return {
    added: next.filter((code) => !currentSet.has(code)),
    removed: current.filter((code) => !nextSet.has(code)),
  };
}

/** 重置出厂的本地预检（镜像后端 ROLE_CODE_MISMATCH / RESET_REASON_REQUIRED）。 */
export function resetConfirmError(
  roleCode: string,
  confirmCode: string,
  reason: string,
): string | null {
  if (confirmCode.trim() !== roleCode) return "请输入完整角色编码以确认";
  if (codePointLength(reason.trim()) < REASON_MIN_LENGTH) {
    return `重置理由至少 ${REASON_MIN_LENGTH} 个字`;
  }
  return null;
}

/** 撤掉本页入口凭证且操作者本人就在该角色里 = 自锁（下一请求即失去本页访问权）。 */
export function isSelfLockRemoval(
  roleCode: string,
  removed: readonly string[],
  actorRoles: readonly string[],
): boolean {
  return removed.includes(ROLE_MANAGE_CODE) && actorRoles.includes(roleCode);
}

/**
 * 自扩权预检（TD-191 的前端镜像）：操作者本人就在被编辑的角色里，且本次新增里有**他自己
 * 还没有**的敏感码 → 后端会要求显式确认，界面先弹框而不是等一个 422。
 *
 * 与后端同用「净增」判据：`auth.user.permissions` 是 /auth/me 从库里算出的角色并集，
 * 已经持有的敏感码补进本角色没有扩大能力面（日常配置），不该被逼勾选确认。取不到该并集时
 * 退回"照提示"（`?? []`）——多勾一次框可以接受，反过来漏报会把合法操作变成界面上的死路。
 * 敏感码清单由 `/school/permissions` 下发，不在前端复刻一份。
 */
export function selfExpansionCodes(
  added: readonly string[],
  roleCode: string,
  actorRoles: readonly string[],
  actorPermissions: readonly string[],
  sensitive: ReadonlySet<string>,
): string[] {
  if (!actorRoles.includes(roleCode)) return [];
  const held = new Set(actorPermissions);
  return added.filter((code) => sensitive.has(code) && !held.has(code));
}

/** 与后端 `app/rbac/admin.py:SUPER_ADMIN_ROLE_CODE` 同一个码（超管免打断，不免留痕）。 */
export const SUPER_ADMIN_ROLE_CODE = "school_super_admin";

/**
 * 自扩权的界面处置（镜像后端 `_assert_self_grant` 的豁免规则）：
 *
 * - `none`：没有净增敏感码流向自己 → 正常保存，不多问；
 * - `ack_required`：后端会 422，界面必须先拿到勾选 + 理由；
 * - `audit_only`：超管主体后端**不校验** ack/理由，但同事务仍写 `school.role.self_grant`。
 *
 * 豁免规则必须在前端跟着：只按「净增敏感码」把关，会把超管的正常保存禁成一个多余的勾选框
 * 加一段后端根本不看的理由——界面与后端对同一动作给出两套预期，比多一条提示更糟。
 */
export type SelfGrantGate =
  | { kind: "none"; codes: readonly string[] }
  | { kind: "ack_required"; codes: readonly string[] }
  | { kind: "audit_only"; codes: readonly string[] };

export function selfGrantGate(
  expansion: readonly string[],
  actorRoles: readonly string[],
): SelfGrantGate {
  const codes = [...expansion];
  if (codes.length === 0) return { kind: "none", codes };
  return actorRoles.includes(SUPER_ADMIN_ROLE_CODE)
    ? { kind: "audit_only", codes }
    : { kind: "ack_required", codes };
}

/**
 * 从 `ApiError` 里取机器可读判据码（`detail.code`）；不是该形状就返回 null。
 *
 * 判据码与展示文案是两条线：文案给人看（`detail.message`），码给程序分支用。
 * 中间这层提取集中在一处，避免每个调用点各写一遍形状判断（写漏一个字段就静默失配）。
 */
export function rbacErrorCode(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  const detail = error.detail;
  if (!detail || typeof detail !== "object") return null;
  const { code } = detail as { code?: unknown };
  return typeof code === "string" ? code : null;
}

/** 版本冲突时后端回传的最新角色快照；其他错误返回 null（避免吞掉真正的失败）。 */
export function conflictedLatestRole(error: unknown): RoleView | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  if (rbacErrorCode(error) !== "ROLE_VERSION_CONFLICT") return null;
  const detail = error.detail as { latest?: unknown };
  if (!detail.latest || typeof detail.latest !== "object") return null;
  return detail.latest as RoleView;
}

/** `_assert_self_grant` 的两个打断判据码：只有这两个码意味着"补上确认+理由就能重试"。 */
const SELF_GRANT_REJECTION_CODES: ReadonlySet<string> = new Set([
  "SELF_GRANT_CONFIRMATION_REQUIRED",
  "SELF_GRANT_REASON_REQUIRED",
]);

/**
 * 后端自扩权打断的原文（`detail.message`），其他错误返回 null。
 *
 * 存在的原因：本页的预检吃的是**进入页面时**的角色快照与 `/auth/me` 权限并集，两者都可能落后于库
 * （典型场景：本次会话内别人把操作者绑进了被编辑的角色）。此时本地算出"无净增"、确认条不出现，
 * 服务端却照样拒 422 —— 若只把错误当 toast，界面就成了不刷新就走不出去的死路。
 * 判据码必须逐个匹配：`ROLE_VERSION_CONFLICT` 等 422/409 各有各的出路，不能一并"强制展开确认条"。
 */
export function selfGrantRejectionMessage(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.status !== 422) return null;
  const code = rbacErrorCode(error);
  if (code === null || !SELF_GRANT_REJECTION_CODES.has(code)) return null;
  const detail = error.detail as { message?: unknown };
  return typeof detail.message === "string" && detail.message ? detail.message : null;
}

/** 删除自定义角色另需的权限码（决策 #6：能改配置的人不一定能删角色）。 */
export const ROLE_DELETE_CODE = "school.role.delete";

/** 操作理由的最少字数（后端多处在服务层按 10 字校验，前端同一口径预检）。 */
export const REASON_MIN_LENGTH = 10;

/**
 * 角色码格式，与后端 `app/rbac/admin.py:75` 的 `ROLE_CODE_PATTERN` 逐字符同界
 * （3–63 位、小写字母开头、其余只允许小写字母/数字/下划线）。
 * 后端这条校验撞 Pydantic（数组形 422），不在前端预检，用户只会看到读不懂的英文报错。
 * 两份字面量的漂移风险登记在 `docs/tech-debt.md` TD-248（收口：后端下发或前端不判）。
 */
export const ROLE_CODE_PATTERN = /^[a-z][a-z0-9_]{2,62}$/;

/** 角色名长度上限，与后端 `RoleCreatePayload.name` 的 `max_length=100` 同界。 */
export const ROLE_NAME_MAX_LENGTH = 100;

/** 不可转授的权限码（后端 `NON_DELEGABLE_CODES`）：只有校方超管能把它授出去。 */
export const NON_DELEGABLE_CODES: readonly string[] = ["school.role.manage"];

/** 角色码的本地预检（镜像后端 `RoleCreatePayload.code` 的 pattern + 长度）。 */
export function roleCodeError(code: string): string | null {
  const value = code.trim();
  if (!value) return "请输入角色编码";
  if (!ROLE_CODE_PATTERN.test(value)) {
    return "角色编码需以小写字母开头，只含小写字母、数字与下划线，长度 3–63 位";
  }
  return null;
}

/** 角色名的本地预检（TD-198：去空白后必须非空；与后端同名判据同序）。 */
export function roleNameError(name: string): string | null {
  if (name.length > ROLE_NAME_MAX_LENGTH) return `角色名称不能超过 ${ROLE_NAME_MAX_LENGTH} 字`;
  if (!name.trim()) return "角色名称不能为空或全空白";
  return null;
}

/** 删除确认的本地预检（镜像后端 ROLE_CODE_MISMATCH / 删除理由 ≥10 字）。 */
export function deleteConfirmError(
  roleCode: string,
  confirmCode: string,
  reason: string,
): string | null {
  if (confirmCode.trim() !== roleCode) return "请输入完整角色编码以确认";
  if (codePointLength(reason.trim()) < REASON_MIN_LENGTH) {
    return `删除理由至少 ${REASON_MIN_LENGTH} 个字`;
  }
  return null;
}

/** 该权限码能否由当前主体转授（超管放行；与后端 `_assert_delegable` 同口径）。 */
export function canDelegateCode(code: string, actorRoles: readonly string[]): boolean {
  if (actorRoles.includes(SUPER_ADMIN_ROLE_CODE)) return true;
  return !NON_DELEGABLE_CODES.includes(code);
}

/**
 * 删除入口的门禁（决策 B4）：后端把「看影响面」与「真删」拆成两个码。
 *
 * `visible` 管**入口**：内置角色根本不可删，不给入口。
 * `executable` 管**能不能真删**：缺 `school.role.delete` 时仍然给只读影响面 + 一句说明
 * （简报 §19.2 决策 A）——把入口整个藏起来就等于把「能预览影响面」也藏了，
 * 运营连影响面都看不到，只会觉得"按钮坏了"。
 */
export interface RoleDeleteGate {
  visible: boolean;
  executable: boolean;
  reason: string | null;
}

export function roleDeleteGate(
  role: Pick<RoleView, "is_system">,
  permissions: readonly string[],
): RoleDeleteGate {
  if (role.is_system) {
    return { visible: false, executable: false, reason: "内置角色不可删除" };
  }
  if (!permissions.includes(ROLE_DELETE_CODE)) {
    return {
      visible: true,
      executable: false,
      reason: `你没有删除角色权限（${ROLE_DELETE_CODE}）：下面只显示影响面，不能执行删除`,
    };
  }
  return { visible: true, executable: true, reason: null };
}

/**
 * 该角色能否当「复制源」：复制过去的是它的权限码清单，判据与显式清单模式**同源**
 * （后端 `_assert_delegable` 对复制来的码一样查）。非超管复制一个含 `school.role.manage`
 * 的角色必被 422 拦下，界面不该给出"点得动、提交才炸"的选项。
 */
export function canCopyRoleCodes(
  role: Pick<RoleView, "permission_codes">,
  actorRoles: readonly string[],
): boolean {
  return role.permission_codes.every((code) => canDelegateCode(code, actorRoles));
}

/**
 * 「恢复出厂」不可用的原因（TD-200：判据跟服务端下发的 `resettable` 走，不跟 `is_system` 走）。
 * 自定义角色没有出厂定义，锁定内置角色有 `lock_reason`——两者都要说清为什么按钮不亮。
 *
 * 展示分工（评审 m3）：**锁定角色走的是另一条路径**——`RoleSettingsView` 的保存区只对
 * `editable=true` 的角色渲染，锁定角色因此看不到这一行，它的理由由页面顶部的 `role-lock-reason`
 * 承担。本函数实际服务的只有"可编辑但没有出厂定义"这一种（自定义角色）。
 */
export function resetUnavailableReason(
  role: Pick<RoleView, "resettable" | "lock_reason">,
): string | null {
  if (role.resettable) return null;
  return role.lock_reason ?? "该角色没有可用的出厂定义，无法恢复出厂设置";
}

/**
 * 可手工授的数据范围类型（前端副本，TD-247）。
 *
 * 后端唯一门禁在 `app/rbac/admin.py:83` 的 `EDITABLE_SCOPE_TYPES`，但没有把这个清单下发给前端；
 * 本页按同一份字面量复刻。不同步时的失败模式是「多/少一个可选项」（点不到、或点了被 422 拒），
 * 不会给出错误写入——后端仍是唯一门禁。
 */
export const EDITABLE_SCOPE_TYPES = ["school", "self"] as const;

/** 数据范围类型的中文名。 */
export const SCOPE_LABELS: Record<string, string> = {
  school: "全校",
  self: "本人",
  class: "指定班级",
  course: "指定课程",
};

/** 只读范围的不可改理由（后端 `_SCOPE_TYPE_OWNERSHIP` 的前端说法）。 */
export const READ_ONLY_SCOPE_REASONS: Record<string, string> = {
  class:
    "班级范围必须绑定到具体班级 id，角色配置这条路径没有「选哪个班」的入参通道，写下去是缺 id 的脏数据",
  course: "课程范围本期不贡献可见性（TD-151），配置了也不生效",
};

/** 当前可勾选的范围类型（服务端按行下发 `editable`，不让前端按类型名再猜一份）。 */
export function editableScopeTypes(rows: readonly RoleScopeRow[]): string[] {
  return rows.filter((row) => row.editable).map((row) => row.scope_type);
}

/**
 * 本页可勾选的范围类型全集 = 前端副本（TD-247）∪ 服务端标了可编辑的行。
 *
 * 服务端只在**已有行**上标 `editable`；没有行的类型它不说话。只按已有行渲染勾选框，
 * 就没人能新增一类范围（只有 `self` 的角色永远加不上 `school`），而后端 `PUT` 是收的。
 * 服务端标了可编辑的行一并收进来（那是服务端自己的口径），副本补齐没有行的那些。
 */
export function editableScopeUniverse(rows: readonly RoleScopeRow[]): string[] {
  const known: readonly string[] = EDITABLE_SCOPE_TYPES;
  const extra = editableScopeTypes(rows).filter((type) => !known.includes(type));
  return [...EDITABLE_SCOPE_TYPES, ...extra];
}

/** 只读范围行：只展示并写清「为什么不可改」。 */
export function readOnlyScopeRows(rows: readonly RoleScopeRow[]): RoleScopeRow[] {
  return rows.filter((row) => !row.editable);
}

/** 范围编辑的差异（与权限差异同算法，单独命名让调用点的意图自明）。 */
export function scopeDiff(
  current: string[],
  next: string[],
): { added: string[]; removed: string[] } {
  return permissionDiff(current, next);
}

export interface ScopePlan {
  /** 提交给后端的范围类型清单（只含可编辑类型；只读行由后端原样保留）。 */
  submitted: string[];
  /** 相对当前勾选的新增。 */
  added: string[];
  /** 相对当前勾选的移除。 */
  removed: string[];
  /** 替换后该角色持有人看不到任何数据（与后端 `empties_scope` 同判据：只读行也在结果集里）。 */
  empties: boolean;
  /** 只读行（原样保留，界面只展示）。 */
  retained: RoleScopeRow[];
}

/**
 * 范围编辑的提交计划：把「服务端下发的范围行」与「用户勾选的类型」合成一次提交。
 *
 * 只读行不进提交清单——后端 `replace_role_scopes` 的窄删除只清可编辑那两类，
 * 只读行不归这条写路径所有，提交里带上它们反而会被 `_assert_editable_scope_types` 拒。
 *
 * 提交清单按**可勾选全集**取交集而不是按已有行取：新增一类范围时它还没有行，
 * 若按已有行过滤，勾上的新类型会被悄悄丢掉（界面看着提交了，库里没变）。
 */
export function scopePlan(rows: readonly RoleScopeRow[], chosen: readonly string[]): ScopePlan {
  const submitted = editableScopeUniverse(rows).filter((type) => chosen.includes(type));
  const retained = readOnlyScopeRows(rows);
  const { added, removed } = scopeDiff(editableScopeTypes(rows), submitted);
  return {
    submitted,
    added,
    removed,
    empties: submitted.length === 0 && retained.length === 0,
    retained,
  };
}

/**
 * 删除预览过期（TD-199）时后端回带的最新影响面：界面据此刷新确认框再重试，
 * 不必先跳一次 GET 再回来。
 */
export function staleDeletionPreview(error: unknown): RoleDeletionPreview | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  if (rbacErrorCode(error) !== "ROLE_DELETE_PREVIEW_STALE") return null;
  const detail = error.detail as { latest_preview?: unknown };
  if (!detail.latest_preview || typeof detail.latest_preview !== "object") return null;
  return detail.latest_preview as RoleDeletionPreview;
}

/**
 * 删除执行时角色版本被推进（409 `ROLE_VERSION_CONFLICT`）：预览里那份 `version` 已失效。
 *
 * 与 `ROLE_DELETE_PREVIEW_STALE` 分流的原因：后者是**持有人集合**变了（指纹失配，回带最新影响面），
 * 前者是**角色配置**变了（版本失配，后端回带最新角色快照）。两者的出路不同——版本冲突不能靠
 * 后端回带的影响面救（那份影响面没有新版本号），必须重取一次预览，否则重试必然再 409。
 */
export function deleteVersionConflict(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status !== 409) return false;
  return rbacErrorCode(error) === "ROLE_VERSION_CONFLICT";
}
