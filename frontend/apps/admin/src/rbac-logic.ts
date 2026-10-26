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

/** 版本冲突时后端回传的最新角色快照；其他错误返回 null（避免吞掉真正的失败）。 */
export function conflictedLatestRole(error: unknown): RoleView | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const detail = error.detail;
  if (!detail || typeof detail !== "object") return null;
  const { code, latest } = detail as { code?: unknown; latest?: unknown };
  if (code !== "ROLE_VERSION_CONFLICT" || !latest || typeof latest !== "object") return null;
  return latest as RoleView;
}
