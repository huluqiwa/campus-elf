/** 测试沙箱纯逻辑（M3 KB-04B）：状态文案、门禁与文件预检。 */

import type { KbTestRunDetail } from "@campus-elf/api-client";

export const KB_TEST_CODE = "school.kb.test";

export interface TestStatusMeta {
  label: string;
  tone: "success" | "warning" | "danger" | "info";
}

const TEST_RUN_STATUS_META: Record<string, TestStatusMeta> = {
  TESTING: { label: "测试进行中", tone: "warning" },
  TEST_PASSED: { label: "测试通过", tone: "success" },
  TEST_FAILED: { label: "测试未通过", tone: "danger" },
  STALE: { label: "测试中断", tone: "danger" },
};

export function testRunStatusMeta(status: string): TestStatusMeta {
  return TEST_RUN_STATUS_META[status] ?? { label: `未知状态（${status}）`, tone: "warning" };
}

/** 上传预检：后端限制 200 条/200KB 上限由后端校验，前端只查非空 + 格式。 */
export function testSetFileError(file: File): string | null {
  const lower = file.name.toLowerCase();
  if (!lower.endsWith(".csv") && !lower.endsWith(".xlsx") && !lower.endsWith(".json")) {
    return "测试集必须是 CSV、XLSX 或 JSON 文件";
  }
  if (file.size === 0) return "测试集文件为空";
  if (file.size > 10 * 1024 * 1024) return "测试集文件不能超过 10MB";
  return null;
}

export interface TestSectionGate {
  visible: boolean;
  hint: string | null;
}

/** 测试区块是否显示（status + 权限码）。 */
export function testSectionGate(
  documentStatus: string,
  permissions: readonly string[],
): TestSectionGate {
  if (!permissions.includes(KB_TEST_CODE)) {
    return { visible: false, hint: null };
  }
  const testable = ["READY", "TEST_FAILED", "TEST_PASSED"];
  if (!testable.includes(documentStatus)) {
    return {
      visible: true,
      hint:
        documentStatus === "TESTING" ? "测试正在进行中" : `当前状态 ${documentStatus} 不可发起测试`,
    };
  }
  return { visible: true, hint: null };
}

/** 从 metrics_json 提取通过率（后端已算好）。 */
export function passRateLabel(metrics: Record<string, unknown> | null): string {
  if (!metrics || typeof metrics.pass_rate !== "number") return "—";
  return `${Math.round(metrics.pass_rate * 100)}%`;
}

/** metrics 里的逐 case 摘要（用于报告展开）。 */
export function caseSummaries(metrics: Record<string, unknown> | null): Array<{
  row_no: number;
  question: string;
  pass: boolean;
  latency_ms: number;
}> {
  if (!metrics || !Array.isArray(metrics.cases)) return [];
  return metrics.cases.filter(
    (item): item is { row_no: number; question: string; pass: boolean; latency_ms: number } =>
      typeof item === "object" && item !== null && "row_no" in item,
  );
}

/** TD-291：最近审批单摘要（状态 + 驳回理由）。 */
export function latestApprovalSummary(
  approvals: Array<{ status: string; reject_reason: string | null }>,
): { status: string; reject_reason: string | null } | null {
  return approvals[0] ?? null;
}

export type { KbTestRunDetail };
