import { describe, expect, it } from "vitest";

import { ApiError } from "@campus-elf/api-client";

import {
  ACCOUNT_MANAGE_CODE,
  MFA_RESET_CODE,
  accountGates,
  accountReasonError,
  bindingPlan,
  bindingVersionConflict,
  filterAccountsLocally,
  statusLabel,
  typeLabel,
} from "./account-logic";
import type { BoundRoleView, PermissionAccountView } from "@campus-elf/api-client";

const BOTH_CODES = [ACCOUNT_MANAGE_CODE, MFA_RESET_CODE];

function account(overrides: Partial<PermissionAccountView> = {}): PermissionAccountView {
  return {
    id: "u-1",
    display_name: "张三",
    user_type: "teacher",
    status: "active",
    role_codes: ["teacher"],
    roles_version: 4,
    mfa_enabled: true,
    is_super_admin: false,
    ...overrides,
  };
}

function boundRole(overrides: Partial<BoundRoleView> = {}): BoundRoleView {
  return {
    id: "r-1",
    code: "teacher",
    name: "教师",
    is_system: true,
    bindable: true,
    ...overrides,
  };
}

describe("statusLabel / typeLabel", () => {
  it("三档状态与已知账号类型有中文标签", () => {
    expect(statusLabel("active")).toBe("启用");
    expect(statusLabel("locked")).toBe("已锁定");
    expect(statusLabel("disabled")).toBe("已停用");
    expect(typeLabel("teacher")).toBe("教师");
    expect(typeLabel("school_admin")).toBe("校方管理员");
    expect(typeLabel("student")).toBe("学生");
  });

  it("出现没见过的档位时显示原码并标注未知，不能默认成「启用」", () => {
    expect(statusLabel("pending")).toContain("pending");
    expect(statusLabel("pending")).not.toBe("启用");
    expect(typeLabel("parent")).toContain("parent");
  });
});

describe("accountReasonError", () => {
  it("空串、纯空白、全角空格都拦下（与后端 strip() 同口径）", () => {
    expect(accountReasonError("", "停用")).toContain("至少");
    expect(accountReasonError("   ", "停用")).toContain("至少");
    expect(accountReasonError("\u3000\u3000", "停用")).toContain("至少");
  });

  it("9 个字拦、10 个字放行（含中文按字符计数）", () => {
    expect(accountReasonError("一二三四五六七八九", "停用")).toContain("至少");
    expect(accountReasonError("一二三四五六七八九十", "停用")).toBeNull();
  });

  it("两端空白不计入长度", () => {
    expect(accountReasonError("  一二三四五六七八九十  ", "停用")).toBeNull();
    expect(accountReasonError("  一二三四五六七八九  ", "停用")).toContain("至少");
  });

  it("文案带上动作名，停用一个入口不会说成重置", () => {
    expect(accountReasonError("", "重置 MFA")).toContain("重置 MFA");
  });

  it("按 Unicode 码点计数：9 个 emoji 拦下、10 个放行（后端 len() 同口径，TD-259）", () => {
    // 😀 在 JS 里是 2 个 UTF-16 码元、1 个码点；按 .length 判会把 9 个算成 18 字放行
    const nine = "😀".repeat(9);
    const ten = "😀".repeat(10);
    expect(accountReasonError(nine, "停用")).toContain("当前 9 个字");
    expect(accountReasonError(ten, "停用")).toBeNull();
  });
});

describe("bindingPlan", () => {
  it("只勾新增时已持有的角色全部保留", () => {
    const roles = [boundRole(), boundRole({ id: "r-2", code: "school_admin", name: "校方管理员" })];
    const plan = bindingPlan(roles, ["r-1", "r-2", "r-9"]);
    expect(plan.submitted).toEqual(["r-1", "r-2", "r-9"]);
    expect(plan.added).toEqual(["r-9"]);
    expect(plan.removed).toEqual([]);
    expect(plan.emptiesAll).toBe(false);
  });

  it("撤掉一个可撤的已持有角色会进 removed", () => {
    const roles = [boundRole(), boundRole({ id: "r-2", code: "school_admin", name: "校方管理员" })];
    const plan = bindingPlan(roles, ["r-1"]);
    expect(plan.removed).toEqual(["r-2"]);
    expect(plan.submitted).toEqual(["r-1"]);
  });

  it("当前持有但不可授的角色即使不在勾选清单里也必须留在提交集合（不静默收回）", () => {
    const locked = boundRole({ id: "r-locked", code: "school_super_admin", bindable: false });
    const plan = bindingPlan([boundRole(), locked], ["r-1"]);
    expect(plan.heldUnbindable.map((role) => role.id)).toEqual(["r-locked"]);
    expect(plan.submitted).toContain("r-locked");
    expect(plan.removed).not.toContain("r-locked");
  });

  it("只留不可授的行时 emptiesAll 为 false（还有角色，不是收回全部）", () => {
    const locked = boundRole({ id: "r-locked", bindable: false });
    const plan = bindingPlan([locked], []);
    expect(plan.submitted).toEqual(["r-locked"]);
    expect(plan.emptiesAll).toBe(false);
  });

  it("把可撤的都撤掉 → emptiesAll（界面要提示「收回全部权限」）", () => {
    const plan = bindingPlan([boundRole()], []);
    expect(plan.submitted).toEqual([]);
    expect(plan.emptiesAll).toBe(true);
    expect(plan.removed).toEqual(["r-1"]);
  });

  it("重复的勾选不会让提交清单出现同一个 id", () => {
    const plan = bindingPlan([boundRole()], ["r-1", "r-1"]);
    expect(plan.submitted).toEqual(["r-1"]);
  });
});

describe("accountGates", () => {
  it("正常他人的启用账号：改绑/停用/重置 MFA 可给，启用不可给（状态机：非停用）", () => {
    const gates = accountGates(account(), "me-1", BOTH_CODES);
    expect(gates.bind.allowed).toBe(true);
    expect(gates.deactivate.allowed).toBe(true);
    expect(gates.mfaReset.allowed).toBe(true);
    expect(gates.activate.allowed).toBe(false);
    expect(gates.activate.hint).toContain("无需启用");
  });

  it("已停用：启用可给、停用不可给（重复停用必然 409）", () => {
    const gates = accountGates(account({ status: "disabled" }), "me-1", BOTH_CODES);
    expect(gates.activate.allowed).toBe(true);
    expect(gates.deactivate.allowed).toBe(false);
    expect(gates.deactivate.hint).toContain("已停用");
    expect(gates.activate.hint).toBeNull();
  });

  it("该账号没开 MFA 时重置入口不给（必然 409 MFA_NOT_ENABLED）", () => {
    const gates = accountGates(account({ mfa_enabled: false }), "me-1", BOTH_CODES);
    expect(gates.mfaReset.allowed).toBe(false);
    expect(gates.mfaReset.hint).toContain("MFA");
    expect(gates.bind.allowed).toBe(true);
  });

  it("已锁定仍允许停用（后端允许 active/locked 两个来源态）", () => {
    const gates = accountGates(account({ status: "locked" }), "me-1", BOTH_CODES);
    expect(gates.deactivate.allowed).toBe(true);
  });

  it("超管行四个写动作全关，且判据用服务端的 is_super_admin 而不是 role_codes", () => {
    const row = account({
      is_super_admin: true,
      // role_codes 里刻意不含超管码：若前端按码硬认，这条用例会绿、真实页面会错
      role_codes: ["teacher"],
    });
    const gates = accountGates(row, "me-1", BOTH_CODES);
    expect(gates.bind.allowed).toBe(false);
    expect(gates.deactivate.allowed).toBe(false);
    expect(gates.activate.allowed).toBe(false);
    expect(gates.mfaReset.allowed).toBe(false);
    expect(gates.bind.hint).toContain("超管");
  });

  it("自己的行：改绑/停用/重置 MFA 关，启用仍开（后端对启用没有自锁判据）", () => {
    const gates = accountGates(account({ id: "me-1", status: "disabled" }), "me-1", BOTH_CODES);
    expect(gates.bind.allowed).toBe(false);
    expect(gates.deactivate.allowed).toBe(false);
    expect(gates.mfaReset.allowed).toBe(false);
    expect(gates.activate.allowed).toBe(true);
    expect(gates.bind.hint).toContain("自己");
  });

  it("自己又是超管：先按「不能操作自己」解释（后端自锁判据先于读目标，TD-261）", () => {
    const gates = accountGates(account({ id: "me-1", is_super_admin: true }), "me-1", BOTH_CODES);
    expect(gates.bind.hint).toContain("自己");
    expect(gates.bind.hint).not.toContain("超管");
  });

  it("只持账号管理码：MFA 重置关，其余照常（不能因缺一个码让整页半死路）", () => {
    const gates = accountGates(account(), "me-1", [ACCOUNT_MANAGE_CODE]);
    expect(gates.mfaReset.allowed).toBe(false);
    // 提示里说人话，不印内部权限码——运营读不出 `school.personnel.mfa.reset` 对应哪个入口
    expect(gates.mfaReset.hint).toContain("MFA 重置");
    expect(gates.mfaReset.hint).not.toContain(MFA_RESET_CODE);
    expect(gates.bind.allowed).toBe(true);
    expect(gates.deactivate.allowed).toBe(true);
  });

  it("连账号管理码都没有时三个账号动作都关（路由守卫之外的第二道说明）", () => {
    const gates = accountGates(account(), "me-1", [MFA_RESET_CODE]);
    expect(gates.bind.allowed).toBe(false);
    expect(gates.deactivate.allowed).toBe(false);
    expect(gates.activate.allowed).toBe(false);
    expect(gates.bind.hint).toContain("权限账号管理");
    expect(gates.bind.hint).not.toContain(ACCOUNT_MANAGE_CODE);
    expect(gates.mfaReset.allowed).toBe(true);
  });

  it("学生账号：改绑与启停都不给（后端必然 422），MFA 重置仍给（后端没有类型判据）", () => {
    // 这条路真实可达：列表按类型过滤，但 `/users/lookup` 不过滤，学号能捞到学生
    const gates = accountGates(account({ user_type: "student" }), "me-1", BOTH_CODES);
    expect(gates.bind.allowed).toBe(false);
    expect(gates.bind.hint).toContain("学生");
    expect(gates.deactivate.allowed).toBe(false);
    expect(gates.activate.allowed).toBe(false);
    expect(gates.mfaReset.allowed).toBe(true);
  });

  it("没见过的账号类型同样不给写控件（按白名单判，不是按 student 硬枚举）", () => {
    const row = account({ user_type: "parent", status: "disabled" });
    const gates = accountGates(row, "me-1", BOTH_CODES);
    expect(gates.bind.allowed).toBe(false);
    expect(gates.activate.allowed).toBe(false);
    expect(gates.bind.hint).toContain("parent");
  });

  it("超管优先于类型挡（一行同时是超管且类型异常时，先说超管）", () => {
    const gates = accountGates(account({ user_type: "student", is_super_admin: true }), "me-1", [
      ACCOUNT_MANAGE_CODE,
      MFA_RESET_CODE,
    ]);
    expect(gates.bind.hint).toContain("超管");
  });
});

describe("filterAccountsLocally", () => {
  const rows = [
    account({ id: "u-1", display_name: "张老师", role_codes: ["teacher"] }),
    account({ id: "u-2", display_name: null, role_codes: ["school_admin"] }),
  ];

  it("空关键词原样返回，不做过滤", () => {
    expect(filterAccountsLocally(rows, "")).toHaveLength(2);
    expect(filterAccountsLocally(rows, "   ")).toHaveLength(2);
  });

  it("按姓名与角色码都能命中，大小写不敏感", () => {
    expect(filterAccountsLocally(rows, "张老").map((row) => row.id)).toEqual(["u-1"]);
    expect(filterAccountsLocally(rows, "SCHOOL_ADMIN").map((row) => row.id)).toEqual(["u-2"]);
  });

  it("姓名为 null 的行不报错，仍可按角色码命中", () => {
    expect(() => filterAccountsLocally(rows, "张")).not.toThrow();
    expect(filterAccountsLocally(rows, "无此人")).toEqual([]);
  });
});

describe("bindingVersionConflict", () => {
  const latest = {
    user_id: "u-1",
    display_name: "张三",
    user_type: "teacher",
    status: "active",
    roles_version: 7,
    roles: [],
    permission_codes: [],
  };

  it("409 BINDING_VERSION_CONFLICT 且带 latest 时返回最新快照", () => {
    const error = new ApiError("冲突", 409, {
      code: "BINDING_VERSION_CONFLICT",
      message: "该账号的角色绑定已被他人修改，请刷新后重试",
      latest,
    });
    expect(bindingVersionConflict(error)).toEqual(latest);
  });

  it("其他 409 码、其他状态码或缺 latest 都返回 null（不吞掉真正的失败）", () => {
    expect(
      bindingVersionConflict(
        new ApiError("x", 409, { code: "ACCOUNT_ALREADY_DISABLED", message: "y" }),
      ),
    ).toBeNull();
    expect(
      bindingVersionConflict(
        new ApiError("x", 403, { code: "BINDING_VERSION_CONFLICT", message: "y" }),
      ),
    ).toBeNull();
    expect(
      bindingVersionConflict(
        new ApiError("x", 409, { code: "BINDING_VERSION_CONFLICT", message: "y" }),
      ),
    ).toBeNull();
    expect(bindingVersionConflict(new Error("普通错误"))).toBeNull();
  });
});
