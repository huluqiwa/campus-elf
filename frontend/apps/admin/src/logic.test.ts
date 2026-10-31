import { describe, expect, it } from "vitest";

import { codePointLength, importFileError, reasonValid, rolloverMappingError } from "./logic";

describe("reasonValid（受控查看理由，与后端 ≥10 字同口径）", () => {
  it("短理由不通过（含前后空白被忽略前仍不足）", () => {
    expect(reasonValid("查一下")).toBe(false);
    expect(reasonValid("医保办理")).toBe(false);
  });

  it("恰好 10 字（trim 后）通过，纯空白不通过", () => {
    expect(reasonValid("医保办理需要查看完整号码")).toBe(true);
    expect(reasonValid("          ")).toBe(false);
    expect(reasonValid("  医保办理需要查看完整号码  ")).toBe(true);
  });

  it("按 Unicode 码点计数：9 个 emoji 不通过、10 个通过（TD-259）", () => {
    expect(reasonValid("😀".repeat(9))).toBe(false);
    expect(reasonValid("😀".repeat(10))).toBe(true);
  });
});

describe("codePointLength（与后端 len() 同口径）", () => {
  it("BMP 字符一字符一码点，代理对只算一个", () => {
    expect(codePointLength("校园精灵")).toBe(4);
    expect(codePointLength("😀𠮷")).toBe(2);
    expect("😀".length).toBe(2);
  });
});

describe("rolloverMappingError（学年流转映射预检，镜像 M2-ORG06 后端校验）", () => {
  const advance = (from_grade: string, to_grade: string, entry_year?: number) => ({
    from_grade,
    to_grade,
    entry_year,
  });

  it("空载荷拒绝", () => {
    expect(rolloverMappingError([], [])).toContain("不能同时为空");
  });

  it("起止年级相同拒绝", () => {
    expect(rolloverMappingError([advance("高一", "高一")], [])).toContain("起止年级相同");
  });

  it("同起点多条映射拒绝（防双跳）", () => {
    expect(rolloverMappingError([advance("高一", "高二"), advance("高一", "高三")], [])).toContain(
      "唯一去向",
    );
  });

  it("归档年级同时是升级起点拒绝", () => {
    expect(rolloverMappingError([advance("高三", "毕业")], ["高三"])).toContain("请确认语义");
  });

  it("入学年份越界拒绝", () => {
    expect(rolloverMappingError([advance("高一", "高二", 1800)], [])).toContain("1970~2100");
  });

  it("合法链式映射通过（高一→高二 & 高二→高三）", () => {
    expect(rolloverMappingError([advance("高一", "高二"), advance("高二", "高三")], [])).toBeNull();
  });
});

describe("importFileError（与后端 ORG-02 同口径）", () => {
  it("超 10MB 拒绝", () => {
    expect(importFileError({ name: "a.csv", size: 11 * 1024 * 1024, type: "text/csv" })).toContain(
      "10MB",
    );
  });

  it("非 csv/xlsx 后缀拒绝", () => {
    expect(importFileError({ name: "a.exe", size: 1, type: "" })).toContain("仅支持");
  });

  it("合法 csv 通过", () => {
    expect(importFileError({ name: "teachers.csv", size: 10, type: "text/csv" })).toBeNull();
  });
});
