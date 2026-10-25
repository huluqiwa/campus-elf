import { describe, expect, it, vi } from "vitest";

import { buildQuery, createOrgApi } from "./org";

/** 会话请求替身：记录调用并按路径/方法返回脚本化结果。 */
function sessionStub(
  handler: (path: string, init?: RequestInit) => { status?: number; body?: unknown } | undefined,
): { session: { request: <T>(path: string, init?: RequestInit) => Promise<T> }; calls: { path: string; init?: RequestInit }[] } {
  const calls: { path: string; init?: RequestInit }[] = [];
  const request = vi.fn(async <T,>(path: string, init?: RequestInit): Promise<T> => {
    calls.push({ path, init });
    const scripted = handler(path, init);
    if (scripted?.status !== undefined && scripted.status >= 400) {
      throw new Error(`HTTP ${scripted.status}`);
    }
    return (scripted?.body ?? {}) as T;
  });
  return { session: { request: request as <T>(path: string, init?: RequestInit) => Promise<T> }, calls };
}

const CLASS_VIEW = {
  id: "c-1",
  entry_year: 2025,
  grade: "高一",
  name: "1班",
  status: "active",
  head_teacher_id: null,
  counselor_id: null,
};

describe("buildQuery", () => {
  it("跳过 undefined/null，保留其余参数", () => {
    expect(buildQuery({ a: "1", b: undefined, c: null, d: 2, e: true })).toBe("?a=1&d=2&e=true");
    expect(buildQuery({})).toBe("");
  });
});

describe("createOrgApi", () => {
  it("listClasses 透传 cursor/limit/entry_year/include_archived 查询参数", async () => {
    const { session, calls } = sessionStub(() => ({ body: { items: [CLASS_VIEW], next_cursor: null } }));
    await createOrgApi(session).listClasses({ limit: 20, cursor: "cur-1", entryYear: 2025, includeArchived: true });
    expect(calls[0].path).toBe("/api/v1/school/classes?limit=20&cursor=cur-1&entry_year=2025&include_archived=true");
  });

  it("createClass 以 JSON POST 提交并携带 Content-Type", async () => {
    const { session, calls } = sessionStub(() => ({ body: CLASS_VIEW }));
    const input = { entry_year: 2025, grade: "高一", name: "1班" };
    const view = await createOrgApi(session).createClass(input);
    expect(view).toEqual(CLASS_VIEW);
    expect(calls[0].path).toBe("/api/v1/school/classes");
    expect(calls[0].init?.method).toBe("POST");
    expect(calls[0].init?.headers).toEqual({ "Content-Type": "application/json" });
    expect(JSON.parse(String(calls[0].init?.body))).toEqual(input);
  });

  it("updateClass 走 PUT，deleteClass 走 DELETE", async () => {
    const { session, calls } = sessionStub(() => ({ body: CLASS_VIEW }));
    await createOrgApi(session).updateClass("c-1", { grade: "高二" });
    await createOrgApi(session).deleteClass("c-1");
    expect(calls[0]).toMatchObject({ path: "/api/v1/school/classes/c-1", init: { method: "PUT" } });
    expect(calls[1]).toMatchObject({ path: "/api/v1/school/classes/c-1", init: { method: "DELETE" } });
  });

  it("rollover 原样提交映射与幂等键，返回影响清单", async () => {
    const { session, calls } = sessionStub(() => ({
      body: { dry_run: false, advanced: [], archived: [], advanced_count: 0, archived_count: 0, rollover_id: "r-1" },
    }));
    const input = {
      advances: [{ from_grade: "高一", to_grade: "高二", entry_year: 2025 }],
      archive_grades: ["高三"],
      idempotency_key: "rollover-2026-summer",
      dry_run: false,
    };
    const result = await createOrgApi(session).rollover(input);
    expect(calls[0].path).toBe("/api/v1/school/classes/rollover");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual(input);
    expect(result.rollover_id).toBe("r-1");
  });

  it("transferStudent 的 class_id=null（转未分班）不会被查询序列化丢弃", async () => {
    const { session, calls } = sessionStub(() => ({ body: {} }));
    await createOrgApi(session).transferStudent("s-1", null);
    expect(calls[0].path).toBe("/api/v1/school/students/s-1/transfer");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ class_id: null });
  });

  it("readIdCard 走 POST 且理由在请求体（不进 URL）", async () => {
    const { session, calls } = sessionStub(() => ({ body: { number: "110101199003077758", mask: "110***********7758" } }));
    await createOrgApi(session).readIdCard("p-1", "医保办理需要查看完整号码");
    expect(calls[0].path).toBe("/api/v1/school/personnel/p-1/id-card");
    expect(calls[0].init?.method).toBe("POST");
    expect(calls[0].path).not.toContain("reason");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ reason: "医保办理需要查看完整号码" });
  });

  it("listPersonnel 使用 offset 分页参数", async () => {
    const { session, calls } = sessionStub(() => ({ body: [] }));
    await createOrgApi(session).listPersonnel({ limit: 50, offset: 100, userType: "teacher" });
    expect(calls[0].path).toBe("/api/v1/school/personnel?limit=50&offset=100&user_type=teacher");
  });

  it("listDepartments 返回数组（整树扁平结构）", async () => {
    const { session } = sessionStub(() => ({ body: [{ id: "d-1", parent_id: null, name: "教务处", code: null, path: "/d-1", status: "active" }] }));
    const tree = await createOrgApi(session).listDepartments();
    expect(tree).toHaveLength(1);
    expect(tree[0].name).toBe("教务处");
  });

  it("会话层抛出的错误原样向上传播（401 刷新/403 分流在会话层处理）", async () => {
    const failure = new Error("HTTP 409");
    const request = vi.fn(async () => {
      throw failure;
    });
    const api = createOrgApi({ request: request as <T>(path: string, init?: RequestInit) => Promise<T> });
    await expect(api.createClass({ entry_year: 2025, grade: "高一", name: "1班" })).rejects.toThrow("HTTP 409");
  });
});
