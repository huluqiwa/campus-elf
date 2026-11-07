import { describe, expect, it } from "vitest";

import { ApiError } from "@campus-elf/api-client";
import type { ApprovalRequestView } from "@campus-elf/api-client";

import {
  approvalErrorCode,
  approvalReasonError,
  approvalStatusMeta,
  actionLabel,
  approvalTypeLabel,
  cancelGate,
  decideGate,
  kbEntryConflictMessage,
} from "./approval-logic";

function request(overrides: Partial<ApprovalRequestView> = {}): ApprovalRequestView {
  return {
    id: "r-1",
    type_code: "KB_DOCUMENT",
    title: "知识库文档审批：课程大纲",
    status: "PENDING",
    result: null,
    reject_reason: null,
    submitter_id: "u-1",
    document_version_id: "v-1",
    payload: null,
    current_step: 1,
    created_at: "2026-11-07T00:00:00Z",
    completed_at: null,
    ...overrides,
  };
}

describe("approvalStatusMeta", () => {
  it("四档状态各有人话与色调；未知档位显示原码", () => {
    expect(approvalStatusMeta("PENDING")).toEqual({ label: "待审批", tone: "warning" });
    expect(approvalStatusMeta("APPROVED")).toEqual({ label: "已通过", tone: "success" });
    expect(approvalStatusMeta("REJECTED")).toEqual({ label: "已驳回", tone: "danger" });
    expect(approvalStatusMeta("CANCELLED")).toEqual({ label: "已撤销", tone: "info" });
    expect(approvalStatusMeta("SOMETHING_ELSE")).toEqual({
      label: "未知状态（SOMETHING_ELSE）",
      tone: "warning",
    });
  });
});

describe("actionLabel / approvalTypeLabel", () => {
  it("已知动作与类型给文案，未知显示原码", () => {
    expect(actionLabel("submit")).toBe("提交");
    expect(actionLabel("approve")).toBe("通过");
    expect(actionLabel("reject")).toBe("驳回");
    expect(actionLabel("cancel")).toBe("撤销");
    expect(actionLabel("???")).toBe("未知动作（???）");
    expect(approvalTypeLabel("KB_DOCUMENT")).toBe("知识库文档");
    expect(approvalTypeLabel("LEAVE")).toBe("未知类型（LEAVE）");
  });
});

describe("approvalReasonError", () => {
  it("空/纯空白拒绝；超长按码点计数拒绝；正常通过", () => {
    expect(approvalReasonError("")).toBe("驳回必须填写理由");
    expect(approvalReasonError("   ")).toBe("驳回必须填写理由");
    // 500 个单码点 emoji = 500 码点：放行（与后端 len() 同口径，TD-259）
    expect(approvalReasonError("😀".repeat(500))).toBeNull();
    expect(approvalReasonError("😀".repeat(501))).toBe("驳回理由不能超过 500 个字");
    expect(approvalReasonError(" 内容与学校口径不符 ")).toBeNull();
  });
});

describe("decideGate", () => {
  it("缺 school.kb.approval → 拒绝并给权限名提示", () => {
    const gate = decideGate(request(), ["school.approval.todo"]);
    expect(gate.allowed).toBe(false);
    expect(gate.hint).toContain("知识库审批");
  });

  it("非 PENDING（终态/已撤销）→ 拒绝并带状态文案", () => {
    for (const status of ["APPROVED", "REJECTED", "CANCELLED"]) {
      const gate = decideGate(request({ status }), ["school.kb.approval"]);
      expect(gate.allowed).toBe(false);
      expect(gate.hint).toContain(approvalStatusMeta(status).label);
    }
  });

  it("本页不支持的类型 → 拒绝（后端 APPROVAL_TYPE_NOT_SUPPORTED 同款判据）", () => {
    const gate = decideGate(request({ type_code: "LEAVE" }), ["school.kb.approval"]);
    expect(gate.allowed).toBe(false);
    expect(gate.hint).toContain("暂不支持");
  });

  it("有码 + PENDING + KB_DOCUMENT → 放行；不做自批预判（后端 403 兜）", () => {
    const gate = decideGate(request({ submitter_id: "self" }), [
      "school.kb.approval",
      "school.approval.todo",
    ]);
    expect(gate.allowed).toBe(true);
    expect(gate.hint).toBeNull();
  });
});

describe("cancelGate", () => {
  it("缺 school.kb.upload → 拒绝", () => {
    const gate = cancelGate(request(), "u-1", ["school.kb.approval"]);
    expect(gate.allowed).toBe(false);
    expect(gate.hint).toContain("知识库上传");
  });

  it("非提交人 → 拒绝（后端 APPROVAL_CANCEL_FORBIDDEN 同款判据）", () => {
    const gate = cancelGate(request({ submitter_id: "someone-else" }), "u-1", ["school.kb.upload"]);
    expect(gate.allowed).toBe(false);
    expect(gate.hint).toContain("只有提交人");
  });

  it("终态单不可撤销；自己提交的 PENDING 单放行", () => {
    const decided = cancelGate(request({ status: "REJECTED" }), "u-1", ["school.kb.upload"]);
    expect(decided.allowed).toBe(false);
    expect(cancelGate(request(), "u-1", ["school.kb.upload"]).allowed).toBe(true);
  });

  it("不知道自己是谁（selfUserId=null）→ 拒绝，不放行危险按钮", () => {
    expect(cancelGate(request(), null, ["school.kb.upload"]).allowed).toBe(false);
  });
});

describe("approvalErrorCode / kbEntryConflictMessage", () => {
  it("从 ApiError detail 取码；非 ApiError 返回 null", () => {
    const error = new ApiError("失败", 409, { code: "BUILD_ALREADY_ACTIVE", message: "x" });
    expect(approvalErrorCode(error)).toBe("BUILD_ALREADY_ACTIVE");
    expect(approvalErrorCode(new Error("普通错误"))).toBeNull();
  });

  it("409/409/403 类码映射成人话，未知码走兜底", () => {
    const withCode = (code: string) => new ApiError("失败", 409, { code, message: "服务端文案" });
    expect(kbEntryConflictMessage(withCode("APPROVAL_ALREADY_PENDING"), "兜底")).toContain(
      "无需重复提交",
    );
    expect(kbEntryConflictMessage(withCode("BUILD_ALREADY_ACTIVE"), "兜底")).toContain(
      "已有进行中的构建任务",
    );
    expect(kbEntryConflictMessage(withCode("KB_NOT_SUBMITTABLE"), "兜底")).toContain("未通过扫描");
    expect(kbEntryConflictMessage(withCode("KB_NOT_APPROVED"), "兜底")).toContain("还未审批通过");
    expect(kbEntryConflictMessage(withCode("SOMETHING_NEW"), "兜底")).toBe("兜底");
  });
});
