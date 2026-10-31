/** 权限账号页的纯逻辑（M2-UI3 TD-187g-C）：与后端同口径的前端预检与写控件门禁，便于单元测试。 */

import { ApiError } from "@campus-elf/api-client";
import type {
  BoundRoleView,
  PermissionAccountView,
  UserBindingsView,
} from "@campus-elf/api-client";

import { codePointLength } from "./logic";
import { REASON_MIN_LENGTH, rbacErrorCode } from "./rbac-logic";

/** 本页入口与账号侧三个写动作共用的权限码（后端 `AccountManagerDep` 的判据）。 */
export const ACCOUNT_MANAGE_CODE = "school.permission_account.manage";

/** 重置 MFA 与「按登录名精确定位」用的码——它**不是**账号管理码，缺它只关掉这两个入口。 */
export const MFA_RESET_CODE = "school.personnel.mfa.reset";

/** 理由长度上限，与后端 `DeactivateAccountPayload` / `MfaResetPayload` 的 `max_length=500` 同界。 */
export const REASON_MAX_LENGTH = 500;

/**
 * 状态标签（三档）。可达集合由后端决定：`deleted` 不列（`binding.py:402`），
 * DB CHECK 里没有 `pending`（`779f731db342:153`）。
 */
export const ACCOUNT_STATUS_LABELS: Record<string, string> = {
  active: "启用",
  locked: "已锁定",
  disabled: "已停用",
};

/**
 * 账号类型标签。列表端点只可能给出 `MANAGED_USER_TYPES` 那两档（`binding.py:405` 按类型过滤），
 * 但**精确定位端点不过滤类型**（`school_rbac.py:809`），学生账号会从那条路进来——所以标签要盖住
 * 学生，其余没见过的档位走 `typeLabel` 的兜底分支（显示原码，不默认成某个已知类型）。
 */
export const USER_TYPE_LABELS: Record<string, string> = {
  teacher: "教师",
  school_admin: "校方管理员",
  student: "学生",
};

/** 后端 `BINDABLE_USER_TYPES`（`binding.py:44`）：改绑与启停只覆盖这两类账号。 */
export const MANAGED_USER_TYPES: readonly string[] = ["teacher", "school_admin"];

/**
 * 没见过的档位显示原码并标注未知，**不默认成「启用」**。
 * 静默把未知状态显示成正常态，会让运营对着一个已停用的账号以为一切正常。
 */
export function statusLabel(status: string): string {
  return ACCOUNT_STATUS_LABELS[status] ?? `未知状态（${status}）`;
}

export function typeLabel(userType: string): string {
  return USER_TYPE_LABELS[userType] ?? `未知类型（${userType}）`;
}

/**
 * 停用 / 重置 MFA 的理由预检：后端两条判据都是「去空白后 ≥10 字」（`school_rbac.py:917` 的
 * `DEACTIVATE_REASON_REQUIRED`、`:891` 的 `RESET_REASON_REQUIRED`），两处共用这一个函数。
 *
 * **绑定理由不走这里**：`replace_user_bindings` 只做 `strip()` 后空 → `None`
 * （`binding.py:462`），没有长度判据，绑定区不能出现「至少 10 字」这句话。
 */
export function accountReasonError(reason: string, actionLabel: string): string | null {
  const value = reason.trim();
  // 按码点而不是 UTF-16 码元计数：与后端 len() 同口径，否则 9 个 emoji 的理由前端放行、后端 422（TD-259）
  const length = codePointLength(value);
  if (length < REASON_MIN_LENGTH) {
    return `${actionLabel}理由至少 ${REASON_MIN_LENGTH} 个字（当前 ${length} 个字）`;
  }
  if (length > REASON_MAX_LENGTH) {
    return `${actionLabel}理由不能超过 ${REASON_MAX_LENGTH} 个字`;
  }
  return null;
}

export interface BindingPlan {
  /** 提交给后端的完整角色 id 集合（全量替换语义）。 */
  submitted: string[];
  added: string[];
  removed: string[];
  /** 提交后该账号一个角色都没有＝收回全部权限（会立刻失去所有后台入口）。 */
  emptiesAll: boolean;
  /** 当前持有但**不可授**（`bindable=false`）的角色：只读展示，且必须原样留在提交集合里。 */
  heldUnbindable: BoundRoleView[];
}

/**
 * 绑定区的提交计划（§20.3 C3-A）：以「当前持有」为基底，候选目录只补新增。
 *
 * 不可授的已持有角色**由本函数无条件保留**，不吃 `chosenIds`——如果只按 `bindable-roles` 目录
 * 渲染勾选框，这些角色在界面上根本不存在，提交时就会被静默收回（后端 `PUT` 是全量替换）。
 * 今天「已持有集合 ⊆ 可授目录」在超管以外的账号上恰好成立（判据 `code != school_super_admin`），
 * 但那是巧合不是契约（TD-245 明说将来自定义角色可能加「可绑定」标记）。
 */
export function bindingPlan(
  roles: readonly BoundRoleView[],
  chosenIds: readonly string[],
): BindingPlan {
  const heldIds = roles.map((role) => role.id);
  const heldUnbindable = roles.filter((role) => !role.bindable);
  const protectedIds = heldUnbindable.map((role) => role.id);
  const chosen = [...new Set(chosenIds)];
  const submitted = [...protectedIds, ...chosen.filter((id) => !protectedIds.includes(id))];
  return {
    submitted,
    added: submitted.filter((id) => !heldIds.includes(id)),
    removed: heldIds.filter((id) => !submitted.includes(id)),
    emptiesAll: submitted.length === 0,
    heldUnbindable,
  };
}

export interface ActionGate {
  allowed: boolean;
  /** `allowed=false` 时给人看的一句话；`allowed=true` 时为 null。 */
  hint: string | null;
}

export interface AccountGates {
  bind: ActionGate;
  deactivate: ActionGate;
  activate: ActionGate;
  mfaReset: ActionGate;
}

const SUPER_ADMIN_HINT =
  "该账号是校方超管：角色绑定、启停与 MFA 重置都由平台开通流程管理，租户内不能操作";

const SELF_HINT = "不能修改自己的账号（改绑 / 停用 / 重置 MFA 需由其他管理员操作）";

/** 缺码提示说人话：内部权限码是给后端判的，印在界面上运营读不出它对应哪个入口。 */
const PERMISSION_LABELS: Record<string, string> = {
  [ACCOUNT_MANAGE_CODE]: "权限账号管理",
  [MFA_RESET_CODE]: "MFA 重置",
};

function missingPermissionHint(code: string): string {
  const label = PERMISSION_LABELS[code];
  return label ? `缺少「${label}」权限，这一项用不了` : "缺少相应权限，这一项用不了";
}

function allow(): ActionGate {
  return { allowed: true, hint: null };
}

function deny(hint: string): ActionGate {
  return { allowed: false, hint };
}

/**
 * 一行的四个写控件门禁（§20.3 C4：不给必然 403/409 的按钮，给一句为什么）。
 *
 * 判据优先级：缺权限码 → 是自己 → 超管 → 目标类型 → MFA/状态机。与后端同序：两条写路径都先判
 * 自锁再读目标（`binding.py:476`、`account_admin.py:234`、`mfa_admin.py:221`），所以操作者本人
 * 就是超管时，先听到「不能操作自己」，与后端真正返回的那句一致（TD-261）。
 * 后端 403/409 仍是真边界，这里只是把死路从界面上拿掉。
 */
export function accountGates(
  row: PermissionAccountView,
  selfUserId: string | null,
  permissions: readonly string[],
): AccountGates {
  const isSuperAdmin = row.is_super_admin;
  const isSelf = selfUserId !== null && row.id === selfUserId;

  function gate(
    code: string,
    options: {
      checkSelf?: boolean;
      /** 目标类型必须是教师/校方管理员。MFA 重置**不吃这条**——后端没有类型判据。 */
      checkType?: boolean;
      requiresMfa?: boolean;
      status?: "deactivate" | "activate";
    },
  ): ActionGate {
    if (!permissions.includes(code)) return deny(missingPermissionHint(code));
    if (options.checkSelf && isSelf) return deny(SELF_HINT);
    if (isSuperAdmin) return deny(SUPER_ADMIN_HINT);
    // 学生账号进不了列表，但 `/users/lookup` 按学号能捞到（那个端点不过滤类型）。
    // 不给这一挡，界面就会摆出两个点了必然 422 的按钮
    // （`BINDING_TARGET_NOT_ALLOWED` `binding.py:520`、`ACCOUNT_TARGET_NOT_ALLOWED` `account_admin.py:325`）。
    if (options.checkType && !MANAGED_USER_TYPES.includes(row.user_type)) {
      return deny(
        `该账号是${typeLabel(row.user_type)}，不在权限账号页的管理范围（只覆盖教师与校方管理员）`,
      );
    }
    if (options.requiresMfa && !row.mfa_enabled) return deny("该账号没有开启 MFA，无需重置");
    if (options.status === "deactivate" && row.status === "disabled") {
      return deny("该账号已停用，无需重复停用");
    }
    if (options.status === "activate" && row.status !== "disabled") {
      return deny(`只有已停用的账号可以启用（当前：${statusLabel(row.status)}），无需启用`);
    }
    return allow();
  }

  return {
    bind: gate(ACCOUNT_MANAGE_CODE, { checkSelf: true, checkType: true }),
    deactivate: gate(ACCOUNT_MANAGE_CODE, {
      checkSelf: true,
      checkType: true,
      status: "deactivate",
    }),
    // 启用没有自锁判据，也没有理由入参（`school_rbac.py:948` 连请求体都不接）
    activate: gate(ACCOUNT_MANAGE_CODE, { checkType: true, status: "activate" }),
    mfaReset: gate(MFA_RESET_CODE, { checkSelf: true, requiresMfa: true }),
  };
}

/**
 * 当前页的本地过滤（姓名 / 角色码，大小写不敏感）。
 *
 * 后端列表**没有搜索参数**，所以这里只作用在已加载的那一页（缺口登记 TD-255）；
 * 按工号精确定位走 `lookupUser`，那是另一个入口。
 */
export function filterAccountsLocally(
  rows: readonly PermissionAccountView[],
  keyword: string,
): PermissionAccountView[] {
  const needle = keyword.trim().toLowerCase();
  if (!needle) return [...rows];
  return rows.filter((row) => {
    const name = (row.display_name ?? "").toLowerCase();
    return (
      name.includes(needle) || row.role_codes.some((code) => code.toLowerCase().includes(needle))
    );
  });
}

/**
 * 绑定版本冲突（409 `BINDING_VERSION_CONFLICT`）时后端回带的最新快照。
 *
 * 只有这一个 409 带 `latest`（`_binding_error`）；启停与 MFA 的冲突载荷里没有它，
 * 所以那两个动作失败后只能重读列表，不能"采用最新"。
 */
export function bindingVersionConflict(error: unknown): UserBindingsView | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  if (rbacErrorCode(error) !== "BINDING_VERSION_CONFLICT") return null;
  const detail = error.detail as { latest?: unknown };
  if (!detail.latest || typeof detail.latest !== "object") return null;
  return detail.latest as UserBindingsView;
}
