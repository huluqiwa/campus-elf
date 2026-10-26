import { ApiError } from "@campus-elf/api-client";
import { describe, expect, it } from "vitest";

import {
  conflictedLatestRole,
  groupPermissions,
  isSelfLockRemoval,
  permissionDiff,
  resetConfirmError,
  selfExpansionCodes,
  selfGrantGate,
  selfGrantRejectionMessage,
} from "./rbac-logic";

const SENSITIVE = new Set(["school.personnel.sensitive.read", "school.role.delete"]);

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
      {
        code: "school.a.b",
        module: "school",
        action: "b",
        description: "某校方权限",
        sensitive_self_grant: false,
      },
      {
        code: "campus.a.c",
        module: "campus",
        action: "c",
        description: "某 C 端权限",
        sensitive_self_grant: false,
      },
      {
        code: "school.a.d",
        module: "school",
        action: "d",
        description: "另一校方权限",
        sensitive_self_grant: true,
      },
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

describe("selfGrantGate（超管免打断不免留痕：与后端 _assert_self_grant 同一豁免）", () => {
  it("普通主体命中 → 必须勾选确认", () => {
    expect(selfGrantGate(["school.personnel.mfa.reset"], ["school_admin"])).toEqual({
      kind: "ack_required",
      codes: ["school.personnel.mfa.reset"],
    });
  });

  it("校方超管命中 → 只提示留痕，不逼勾选：后端根本不校验 ack，界面不能把它禁成死路", () => {
    expect(selfGrantGate(["school.role.delete"], ["school_super_admin", "school_admin"])).toEqual({
      kind: "audit_only",
      codes: ["school.role.delete"],
    });
  });

  it("没有命中 → 不打扰正常保存", () => {
    expect(selfGrantGate([], ["school_admin"])).toEqual({ kind: "none", codes: [] });
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

describe("selfGrantRejectionMessage（服务端打断与本地预检不一致时的兜底）", () => {
  it("两个自扩权判据码都回原文：界面据此强制展开确认条，不能只弹 toast", () => {
    const confirmation = new ApiError("需确认", 422, {
      code: "SELF_GRANT_CONFIRMATION_REQUIRED",
      message:
        "本次修改会给自己新增敏感权限：school.personnel.sensitive.read；请显式确认并填写理由",
    });
    expect(selfGrantRejectionMessage(confirmation)).toContain("school.personnel.sensitive.read");
    expect(
      selfGrantRejectionMessage(
        new ApiError("需理由", 422, {
          code: "SELF_GRANT_REASON_REQUIRED",
          message: "自扩权必须填写理由",
        }),
      ),
    ).toBe("自扩权必须填写理由");
  });

  it("其他 422（同状态码、不同出路）不回原文：把无关错误逼成确认框，比不提示更危险", () => {
    expect(
      selfGrantRejectionMessage(
        new ApiError("未知码", 422, {
          code: "UNKNOWN_PERMISSION_CODE",
          message: "权限码不存在",
        }),
      ),
    ).toBeNull();
    expect(
      selfGrantRejectionMessage(
        new ApiError("冲突", 409, { code: "SELF_GRANT_REASON_REQUIRED", message: "x" }),
      ),
    ).toBeNull();
    expect(selfGrantRejectionMessage(new ApiError("空 body", 422, undefined))).toBeNull();
    expect(selfGrantRejectionMessage(new Error("网络错误"))).toBeNull();
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

describe("selfExpansionCodes（TD-191 自扩权：与后端同一判据的前端预检）", () => {
  it("本人所在角色新增自己没有的敏感码 → 列出这些码（界面据此要求勾选确认）", () => {
    expect(
      selfExpansionCodes(
        ["school.personnel.sensitive.read", "school.dashboard.export"],
        "school_admin",
        ["school_admin"],
        ["school.role.manage"],
        SENSITIVE,
      ),
    ).toEqual(["school.personnel.sensitive.read"]);
  });

  it("该敏感码已经从别的角色拿到了 → 不提示：与后端「净增」同口径，日常补配不该被逼确认", () => {
    expect(
      selfExpansionCodes(
        ["school.personnel.sensitive.read"],
        "school_admin",
        ["school_admin", "teacher_b_end"],
        ["school.personnel.sensitive.read"],
        SENSITIVE,
      ),
    ).toEqual([]);
  });

  it("改的不是自己所在的角色 → 不提示：给教师岗配敏感码是日常配置", () => {
    expect(
      selfExpansionCodes(["school.role.delete"], "teacher_b_end", ["school_admin"], [], SENSITIVE),
    ).toEqual([]);
  });

  it("新增的都是非敏感码 → 不提示", () => {
    expect(
      selfExpansionCodes(
        ["school.dashboard.view"],
        "school_admin",
        ["school_admin"],
        [],
        SENSITIVE,
      ),
    ).toEqual([]);
  });

  it("目录未下发敏感标记时不提示（清单只有后端一份，前端不得自行补码）", () => {
    expect(
      selfExpansionCodes(["school.role.delete"], "school_admin", ["school_admin"], [], new Set()),
    ).toEqual([]);
  });
});
