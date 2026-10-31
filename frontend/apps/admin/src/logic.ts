/** B 端页面的纯逻辑（M2-UI1）：与后端校验同口径的前端预检，便于单元测试。 */

/**
 * 按 Unicode 码点计数（后端 Python `len()` 同口径）。
 *
 * JS 的 `String#length` 数的是 UTF-16 码元：emoji、BMP 外生僻字各算 2，后端算 1。
 * 用 `.length` 做「至少 10 字」的预检会把 9 个 emoji 的理由放过去、到后端 422（TD-259）。
 */
export function codePointLength(value: string): number {
  return Array.from(value).length;
}

/** 受控查看理由：去空白后 ≥10 字（与后端 IdCardReadRequest 同口径）。 */
export function reasonValid(reason: string): boolean {
  return codePointLength(reason.trim()) >= 10;
}

export interface RolloverMappingInput {
  from_grade: string;
  to_grade: string;
  entry_year?: number | null;
}

/**
 * 学年流转映射的本地预检（镜像后端 plan_rollover 的校验，M2-ORG06）：
 * 返回错误文案，null 表示通过。后端仍为最终权威。
 */
export function rolloverMappingError(
  advances: RolloverMappingInput[],
  archiveGrades: string[],
): string | null {
  if (advances.length === 0 && archiveGrades.length === 0) {
    return "升学映射与归档年级不能同时为空";
  }
  const seenFroms = new Set<string>();
  for (const item of advances) {
    if (!item.from_grade || !item.to_grade) return "年级映射不能为空串";
    if (item.from_grade === item.to_grade) return `升年级映射的起止年级相同：${item.from_grade}`;
    if (
      item.entry_year !== undefined &&
      item.entry_year !== null &&
      !(1970 <= item.entry_year && item.entry_year <= 2100)
    ) {
      return "入学年份过滤须为 1970~2100";
    }
    if (seenFroms.has(item.from_grade)) {
      return `年级 ${item.from_grade} 出现在多条升学映射中，请确认唯一去向`;
    }
    seenFroms.add(item.from_grade);
  }
  for (const grade of archiveGrades) {
    if (!grade) return "归档年级不能为空串";
    if (seenFroms.has(grade)) {
      return `年级 ${grade} 同时出现在归档清单与升学映射的起点，请确认语义`;
    }
  }
  return null;
}

/** 导入文件预检（与后端 ORG-02 同口径：≤10MB、.csv/.xlsx）。 */
export function importFileError(file: { name: string; size: number; type: string }): string | null {
  const MAX_FILE_BYTES = 10 * 1024 * 1024;
  if (file.size > MAX_FILE_BYTES) return "文件超过 10MB 上限";
  const lower = file.name.toLowerCase();
  if (!lower.endsWith(".csv") && !lower.endsWith(".xlsx")) return "仅支持 .csv / .xlsx";
  const allowed = new Set([
    "text/csv",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "",
  ]);
  if (file.type !== "" && !allowed.has(file.type)) return "文件类型不受支持";
  return null;
}
