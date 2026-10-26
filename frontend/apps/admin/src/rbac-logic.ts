/** 角色权限页的纯逻辑（M2-UI2）：与后端校验同口径的前端预检，便于单元测试。 */

import { ApiError } from "@campus-elf/api-client";
import type { PermissionView, RoleView } from "@campus-elf/api-client";

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
  if (reason.trim().length < 10) return "重置理由至少 10 个字";
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

/** 版本冲突时后端回传的最新角色快照；其他错误返回 null（避免吞掉真正的失败）。 */
export function conflictedLatestRole(error: unknown): RoleView | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const detail = error.detail;
  if (!detail || typeof detail !== "object") return null;
  const { code, latest } = detail as { code?: unknown; latest?: unknown };
  if (code !== "ROLE_VERSION_CONFLICT" || !latest || typeof latest !== "object") return null;
  return latest as RoleView;
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
  const detail = error.detail;
  if (!detail || typeof detail !== "object") return null;
  const { code, message } = detail as { code?: unknown; message?: unknown };
  if (typeof code !== "string" || !SELF_GRANT_REJECTION_CODES.has(code)) return null;
  return typeof message === "string" && message ? message : null;
}
