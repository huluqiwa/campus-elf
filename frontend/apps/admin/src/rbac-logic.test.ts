import { ApiError } from "@campus-elf/api-client";
import { describe, expect, it } from "vitest";

import {
  canCopyRoleCodes,
  canDelegateCode,
  conflictedLatestRole,
  deleteConfirmError,
  deleteVersionConflict,
  editableScopeTypes,
  editableScopeUniverse,
  groupPermissions,
  isSelfLockRemoval,
  permissionDiff,
  readOnlyScopeRows,
  resetConfirmError,
  resetUnavailableReason,
  roleCodeError,
  roleDeleteGate,
  roleNameError,
  scopeDiff,
  scopePlan,
  selfExpansionCodes,
  selfGrantGate,
  selfGrantRejectionMessage,
  staleDeletionPreview,
  EDITABLE_SCOPE_TYPES,
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
  bindable: true,
  resettable: true,
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

  it("按 Unicode 码点计数：9 个 emoji 拦下（TD-259）", () => {
    expect(resetConfirmError("school_admin", "school_admin", "😀".repeat(9))).toContain("10");
    expect(resetConfirmError("school_admin", "school_admin", "😀".repeat(10))).toBeNull();
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

describe("roleCodeError（角色码格式：与后端 ROLE_CODE_PATTERN 同界）", () => {
  it("合法码通过：小写字母开头 + 小写字母/数字/下划线，3 位起", () => {
    expect(roleCodeError("campus_operator")).toBeNull();
    expect(roleCodeError("a1_")).toBeNull();
  });

  it("空码给出输入提示", () => {
    expect(roleCodeError("   ")).toContain("请输入角色编码");
  });

  it("大写字母开头 / 含空格 / 含连字符 / 太短一律拒绝", () => {
    expect(roleCodeError("Campus")).toContain("小写字母");
    expect(roleCodeError("campus operator")).toContain("小写字母");
    expect(roleCodeError("campus-operator")).toContain("小写字母");
    expect(roleCodeError("ab")).toContain("小写字母");
  });

  it("超长（64 位）拒绝：后端列宽 64、校验上界 63，放行会让它撞库 CHECK 变 500", () => {
    expect(roleCodeError("a".repeat(64))).toContain("小写字母");
    expect(roleCodeError("a".repeat(63))).toBeNull();
  });
});

describe("roleNameError（TD-198：角色名去空白后必须非空）", () => {
  it("全空白名拒绝——与后端同名判据同侧（否则落库成一行没有名字的角色）", () => {
    expect(roleNameError("   ")).toContain("全空白");
    expect(roleNameError("")).toContain("全空白");
  });

  it("正常名通过", () => {
    expect(roleNameError("校区运营")).toBeNull();
  });

  it("超长（101 字）拒绝", () => {
    expect(roleNameError("角".repeat(101))).toContain("100");
    expect(roleNameError("角".repeat(100))).toBeNull();
  });
});

describe("deleteConfirmError（删除确认：逐字 code + 理由 ≥10 字）", () => {
  it("确认码不一致拒绝", () => {
    expect(deleteConfirmError("campus_operator", "campus-operator", "岗位撤销不再使用")).toContain(
      "角色编码",
    );
  });

  it("理由不足 10 字拒绝", () => {
    expect(deleteConfirmError("campus_operator", "campus_operator", "  撤销  ")).toContain("10");
  });

  it("两者都满足才通过", () => {
    expect(
      deleteConfirmError("campus_operator", " campus_operator ", "岗位撤销，角色不再使用"),
    ).toBeNull();
  });

  it("按 Unicode 码点计数：9 个 emoji 拦下（TD-259）", () => {
    expect(deleteConfirmError("campus_operator", "campus_operator", "😀".repeat(9))).toContain(
      "10",
    );
    expect(deleteConfirmError("campus_operator", "campus_operator", "😀".repeat(10))).toBeNull();
  });
});

describe("canDelegateCode（转授禁令：与后端 _assert_delegable 同口径）", () => {
  it("校方超管放行一切（含 school.role.manage）", () => {
    expect(canDelegateCode("school.role.manage", ["school_super_admin"])).toBe(true);
  });

  it("普通主体不能转授 school.role.manage", () => {
    expect(canDelegateCode("school.role.manage", ["school_admin"])).toBe(false);
  });

  it("其余权限码不受影响", () => {
    expect(canDelegateCode("school.kb.view", ["school_admin"])).toBe(true);
  });
});

describe("roleDeleteGate（删除入口门禁：真删要 school.role.delete，内置角色不可删）", () => {
  it("自定义角色 + 持删除码 → 入口可执行", () => {
    expect(
      roleDeleteGate({ is_system: false }, ["school.role.manage", "school.role.delete"]),
    ).toEqual({ visible: true, executable: true, reason: null });
  });

  it("内置角色不亮入口（后端恒 409 ROLE_NOT_DELETABLE）", () => {
    const gate = roleDeleteGate({ is_system: true }, ["school.role.delete"]);
    expect(gate.visible).toBe(false);
    expect(gate.executable).toBe(false);
    expect(gate.reason).toContain("内置角色");
  });

  it("只持配置权（无删除码）→ 入口仍在但只给影响面，并说清缺哪个码", () => {
    // 决策 B4：把入口整个藏掉就等于把「能预览影响面」也藏了（简报 §19.2 方案 C 被否的原因）
    const gate = roleDeleteGate({ is_system: false }, ["school.role.manage"]);
    expect(gate.visible).toBe(true);
    expect(gate.executable).toBe(false);
    expect(gate.reason).toContain("school.role.delete");
  });
});

describe("canCopyRoleCodes（复制源里的权限码也得自己授得出去）", () => {
  it("普通角色：不含不可转授码 → 可复制", () => {
    expect(canCopyRoleCodes({ permission_codes: ["school.kb.view"] }, ["school_admin"])).toBe(true);
  });

  it("含 school.role.manage 且非超管 → 不可复制（后端 _assert_delegable 会 422）", () => {
    expect(
      canCopyRoleCodes({ permission_codes: ["school.kb.view", "school.role.manage"] }, [
        "school_admin",
      ]),
    ).toBe(false);
  });

  it("超管不受限", () => {
    expect(
      canCopyRoleCodes({ permission_codes: ["school.role.manage"] }, ["school_super_admin"]),
    ).toBe(true);
  });
});

describe("deleteVersionConflict（删除时版本被推进：预览必须重取）", () => {
  it("409 ROLE_VERSION_CONFLICT → 是版本冲突", () => {
    const error = new ApiError("角色已被他人修改", 409, {
      code: "ROLE_VERSION_CONFLICT",
      message: "角色已被他人修改",
      latest: ROLE,
    });
    expect(deleteVersionConflict(error)).toBe(true);
  });

  it("409 影响面过期不是版本冲突（两者出路不同：后者回带最新影响面，前者没有指纹）", () => {
    const error = new ApiError("影响面已变化", 409, {
      code: "ROLE_DELETE_PREVIEW_STALE",
      message: "影响面已变化",
      latest_preview: {
        role_code: "x",
        name: "x",
        version: 1,
        affected_users: 0,
        permission_codes: [],
        fingerprint: "f",
      },
    });
    expect(deleteVersionConflict(error)).toBe(false);
  });

  it("422 不是版本冲突", () => {
    const error = new ApiError("理由不足", 422, { code: "ROLE_DELETE_REASON_REQUIRED" });
    expect(deleteVersionConflict(error)).toBe(false);
  });
});

describe("resetUnavailableReason（TD-200：判据跟 resettable 走，不跟 is_system 走）", () => {
  it("resettable=true → 没有不可用原因", () => {
    expect(resetUnavailableReason({ resettable: true, lock_reason: null })).toBeNull();
  });

  it("自定义角色（无出厂定义）→ 说清是出厂定义缺失，不是「内置才有的待遇」", () => {
    expect(resetUnavailableReason({ resettable: false, lock_reason: null })).toContain("出厂定义");
  });

  it("锁定内置角色 → 用服务端下发的 lock_reason", () => {
    expect(
      resetUnavailableReason({
        resettable: false,
        lock_reason: "内置角色，权限与数据范围由系统固定",
      }),
    ).toBe("内置角色，权限与数据范围由系统固定");
  });
});

describe("数据范围（可手工授类型清单 + 提交计划）", () => {
  it("可手工授的类型就是 school / self（前端副本，后端是唯一门禁）", () => {
    expect([...EDITABLE_SCOPE_TYPES]).toEqual(["school", "self"]);
  });

  it("按服务端下发的 editable 拆成可勾选与只读两档", () => {
    const rows = [
      { scope_type: "school", scope_id: null, editable: true },
      { scope_type: "self", scope_id: null, editable: true },
      { scope_type: "class", scope_id: "c-1", editable: false },
    ];
    expect(editableScopeTypes(rows)).toEqual(["school", "self"]);
    expect(readOnlyScopeRows(rows)).toEqual([
      { scope_type: "class", scope_id: "c-1", editable: false },
    ]);
  });

  it("范围差异与权限差异同算法", () => {
    expect(scopeDiff(["school", "self"], ["self"])).toEqual({
      added: [],
      removed: ["school"],
    });
  });

  it("提交计划只带可勾选类型：只读行不进提交清单（后端窄删除只清可编辑两类）", () => {
    const rows = [
      { scope_type: "school", scope_id: null, editable: true },
      { scope_type: "class", scope_id: "c-1", editable: false },
    ];
    const plan = scopePlan(rows, ["school"]);
    expect(plan.submitted).toEqual(["school"]);
    expect(plan.retained).toHaveLength(1);
    expect(plan.empties).toBe(false);
  });

  it("全部取消且没有只读行 → 提示该角色持有人将看不到任何数据", () => {
    const rows = [{ scope_type: "school", scope_id: null, editable: true }];
    const plan = scopePlan(rows, []);
    expect(plan.submitted).toEqual([]);
    expect(plan.empties).toBe(true);
    expect(plan.removed).toEqual(["school"]);
  });

  it("取消可勾选类型但仍有只读行 → 不算空集（与后端 empties_scope 同判据）", () => {
    const rows = [
      { scope_type: "school", scope_id: null, editable: true },
      { scope_type: "class", scope_id: "c-1", editable: false },
    ];
    expect(scopePlan(rows, []).empties).toBe(false);
  });

  it("可勾选全集 = 已有可编辑行 + 没有行的那些：只有 self 时也得能勾上 school", () => {
    const rows = [{ scope_type: "self", scope_id: null, editable: true }];
    expect(editableScopeUniverse(rows)).toEqual(["school", "self"]);
  });

  it("勾上一个还没有行的类型 → 提交清单要带上它（否则界面看着提交了、库里没变）", () => {
    const rows = [{ scope_type: "self", scope_id: null, editable: true }];
    const plan = scopePlan(rows, ["self", "school"]);
    expect(plan.submitted).toEqual(["school", "self"]);
    expect(plan.added).toEqual(["school"]);
    expect(plan.removed).toEqual([]);
  });

  it("服务端标了可编辑、前端副本没有的类型 → 以服务端口径为准收进来", () => {
    const rows = [{ scope_type: "campus", scope_id: null, editable: true }];
    expect(editableScopeUniverse(rows)).toEqual(["school", "self", "campus"]);
  });
});

describe("staleDeletionPreview（TD-199：删除影响面过期时后端回带最新预览）", () => {
  it("取出最新预览，界面据此刷新确认框再重试", () => {
    const preview = {
      role_code: "campus_operator",
      name: "校区运营",
      version: 5,
      affected_users: 4,
      permission_codes: [],
      fingerprint: "b".repeat(64),
    };
    const error = new ApiError("影响面已变化", 409, {
      code: "ROLE_DELETE_PREVIEW_STALE",
      message: "删除影响面已变化（持有人集合与预览不一致），请刷新后重新确认",
      latest_preview: preview,
    });
    expect(staleDeletionPreview(error)).toEqual(preview);
  });

  it("版本冲突 / 其他 409 不回预览：分流不能混", () => {
    expect(
      staleDeletionPreview(
        new ApiError("冲突", 409, { code: "ROLE_VERSION_CONFLICT", latest: ROLE }),
      ),
    ).toBeNull();
    expect(staleDeletionPreview(new ApiError("x", 404, { code: "ROLE_NOT_FOUND" }))).toBeNull();
    expect(staleDeletionPreview(new Error("网络错误"))).toBeNull();
  });
});
