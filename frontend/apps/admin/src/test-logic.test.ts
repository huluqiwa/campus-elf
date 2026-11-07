import { describe, expect, it } from "vitest";

import {
  caseSummaries,
  latestApprovalSummary,
  passRateLabel,
  testRunStatusMeta,
  testSectionGate,
  testSetFileError,
} from "./test-logic";

describe("testRunStatusMeta", () => {
  it("四档状态各有人话与色调；未知显示原码", () => {
    expect(testRunStatusMeta("TESTING")).toEqual({ label: "测试进行中", tone: "warning" });
    expect(testRunStatusMeta("TEST_PASSED")).toEqual({ label: "测试通过", tone: "success" });
    expect(testRunStatusMeta("TEST_FAILED")).toEqual({ label: "测试未通过", tone: "danger" });
    expect(testRunStatusMeta("STALE")).toEqual({ label: "测试中断", tone: "danger" });
    expect(testRunStatusMeta("???")).toEqual({ label: "未知状态（???）", tone: "warning" });
  });
});

describe("testSetFileError", () => {
  it("合法文件无错；非法格式/空文件/超大文件给错误", () => {
    const csv = new File(["a,b"], "test.csv", { type: "text/csv" });
    Object.defineProperty(csv, "size", { value: 100 });
    expect(testSetFileError(csv)).toBeNull();

    const bad = new File(["x"], "test.doc", { type: "text/plain" });
    expect(testSetFileError(bad)).toContain("CSV、XLSX 或 JSON");

    const empty = new File([], "empty.csv", { type: "text/csv" });
    expect(testSetFileError(empty)).toContain("为空");
  });
});

describe("testSectionGate", () => {
  it("缺码不可见", () => {
    expect(testSectionGate("READY", ["school.kb.view"]).visible).toBe(false);
  });
  it("READY/TEST_* 可见且无 hint", () => {
    for (const status of ["READY", "TEST_FAILED", "TEST_PASSED"]) {
      const gate = testSectionGate(status, ["school.kb.test"]);
      expect(gate.visible).toBe(true);
      expect(gate.hint).toBeNull();
    }
  });
  it("TESTING 给提示", () => {
    const gate = testSectionGate("TESTING", ["school.kb.test"]);
    expect(gate.visible).toBe(true);
    expect(gate.hint).toContain("测试正在进行中");
  });
  it("APPROVED 给不可发起提示", () => {
    const gate = testSectionGate("APPROVED", ["school.kb.test"]);
    expect(gate.visible).toBe(true);
    expect(gate.hint).toContain("不可发起测试");
  });
});

describe("passRateLabel / caseSummaries", () => {
  it("metrics 有值返回百分比；null 返回 —", () => {
    expect(passRateLabel({ pass_rate: 0.95 })).toBe("95%");
    expect(passRateLabel(null)).toBe("—");
  });
  it("caseSummaries 过滤非对象项", () => {
    expect(
      caseSummaries({ cases: [{ row_no: 1, question: "q", pass: true, latency_ms: 10 }] }),
    ).toHaveLength(1);
    expect(caseSummaries({ cases: ["not-object"] })).toHaveLength(0);
    expect(caseSummaries(null)).toHaveLength(0);
  });
});

describe("latestApprovalSummary", () => {
  it("取第一项；空列表返回 null", () => {
    const approvals = [
      { status: "REJECTED", reject_reason: "不符合" },
      { status: "PENDING", reject_reason: null },
    ];
    expect(latestApprovalSummary(approvals)?.reject_reason).toBe("不符合");
    expect(latestApprovalSummary([])).toBeNull();
  });
});
