import { ApiError } from "@campus-elf/api-client";
import { describe, expect, it } from "vitest";

import {
  conflictedLatestRole,
  groupPermissions,
  isSelfLockRemoval,
  permissionDiff,
  resetConfirmError,
} from "./rbac-logic";

const ROLE = {
  id: "r-1",
  code: "school_admin",
  name: "校方管理员",
  is_system: true,
  editable: true,
  lock_reason: null,
  version: 4,
  defaults_applied: true,
  user_count: 2,
  permission_codes: ["school.dashboard.view", "school.role.manage"],
};

describe("groupPermissions", () => {
  it("按 module 分组并给出中文标题，模块顺序稳定", () => {
    const groups = groupPermissions([
      { code: "school.a.b", module: "school", action: "b", description: "某校方权限" },
      { code: "campus.a.c", module: "campus", action: "c", description: "某 C 端权限" },
      { code: "school.a.d", module: "school", action: "d", description: "另一校方权限" },
    ]);
    expect(groups.map((group) => group.module)).toEqual(["campus", "school"]);
    expect(groups[1]).toMatchObject({ label: "校方后台" });
    expect(groups[1].items.map((item) => item.code)).toEqual(["school.a.b", "school.a.d"]);
  });
});

describe("permissionDiff（保存前展示新增/移除数量）", () => {
  it("分别列出新增与移除，保持提交顺序", () => {
    expect(
      permissionDiff(
        ["school.personnel.view", "school.kb.view"],
        ["school.kb.view", "school.role.manage"],
      ),
    ).toEqual({
      added: ["school.role.manage"],
      removed: ["school.personnel.view"],
    });
  });

  it("无变化时两边都为空", () => {
    expect(permissionDiff(["c"], ["c"])).toEqual({ added: [], removed: [] });
  });
});

describe("resetConfirmError（与后端 ROLE_CODE_MISMATCH / RESET_REASON_REQUIRED 同口径）", () => {
  it("角色编码不一致时拒绝", () => {
    expect(resetConfirmError("school_admin", "school-admin", "误改后恢复出厂默认权限")).toContain(
      "角色编码",
    );
  });

  it("理由不足 10 字（trim 后）时拒绝", () => {
    expect(resetConfirmError("school_admin", "school_admin", "  恢复默认  ")).toContain("10");
  });

  it("编码一致且理由够长时通过", () => {
    expect(
      resetConfirmError("school_admin", " school_admin ", "误改后恢复出厂默认权限"),
    ).toBeNull();
  });
});

describe("conflictedLatestRole（409 版本冲突消费后端 latest）", () => {
  it("从 ApiError.detail 取出最新角色快照", () => {
    const error = new ApiError("角色配置已被他人修改", 409, {
      code: "ROLE_VERSION_CONFLICT",
      message: "角色配置已被他人修改",
      latest: ROLE,
    });
    expect(conflictedLatestRole(error)).toEqual(ROLE);
  });

  it("非版本冲突 / 非 409 / 普通 Error 一律返回 null", () => {
    expect(
      conflictedLatestRole(new ApiError("锁定角色", 409, { code: "ROLE_IMMUTABLE" })),
    ).toBeNull();
    expect(conflictedLatestRole(new ApiError("不存在的角色", 404, { latest: ROLE }))).toBeNull();
    expect(conflictedLatestRole(new Error("网络错误"))).toBeNull();
  });
});

describe("isSelfLockRemoval（撤掉 role.manage 的自锁提示）", () => {
  it("本人所在角色被撤掉 school.role.manage 时判定为自锁", () => {
    expect(isSelfLockRemoval("school_admin", ["school.role.manage"], ["school_admin"])).toBe(true);
  });

  it("改的是别人的角色、或撤的不是进门凭证时不提示", () => {
    expect(isSelfLockRemoval("teacher_b_end", ["school.role.manage"], ["school_admin"])).toBe(
      false,
    );
    expect(isSelfLockRemoval("school_admin", ["school.personnel.view"], ["school_admin"])).toBe(
      false,
    );
  });
});
