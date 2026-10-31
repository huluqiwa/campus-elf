import { describe, expect, it, vi } from "vitest";

import { ApiError } from "./auth";
import { createKbApi } from "./kb";
import type { KbDocumentView, KbVersionView } from "./kb";

function sessionStub(
  handler: (path: string, init?: RequestInit) => { status?: number; body?: unknown } | undefined,
): {
  session: { request: <T>(path: string, init?: RequestInit) => Promise<T> };
  calls: { path: string; init?: RequestInit }[];
} {
  const calls: { path: string; init?: RequestInit }[] = [];
  const request = vi.fn(async <T>(path: string, init?: RequestInit): Promise<T> => {
    calls.push({ path, init });
    const scripted = handler(path, init);
    if (scripted?.status !== undefined && scripted.status >= 400) {
      throw new ApiError("失败", scripted.status, scripted.body);
    }
    return (scripted?.body ?? {}) as T;
  });
  return {
    session: { request: request as <T>(path: string, init?: RequestInit) => Promise<T> },
    calls,
  };
}

const SCAN = {
  state: "passed",
  engine: "clamav",
  signature: null,
  error_code: null,
  attempts: 1,
  scanned_at: "2026-10-31T00:00:00Z",
};

const VERSION: KbVersionView = {
  id: "v-1",
  version_no: 1,
  filename: "课程大纲.pdf",
  mime: "application/pdf",
  size_bytes: 1024,
  status: "PENDING_APPROVAL",
  scan: SCAN,
  created_at: "2026-10-31T00:00:00Z",
};

const DOCUMENT: KbDocumentView = {
  id: "d-1",
  title: "课程大纲",
  tags: ["教学"],
  description: null,
  status: "PENDING_APPROVAL",
  current_version: VERSION,
  created_at: "2026-10-31T00:00:00Z",
  updated_at: "2026-10-31T00:00:00Z",
};

describe("createKbApi.upload", () => {
  it("多个文件按顺序 append 到同一个 files 字段，且不手动设置 Content-Type", async () => {
    const { session, calls } = sessionStub(() => ({
      body: { batch_id: "b-1", replayed: false, documents: [DOCUMENT] },
    }));
    const first = new File(["pdf"], "课程大纲.pdf", { type: "application/pdf" });
    const second = new File(["md"], "说明.md", { type: "text/markdown" });

    const result = await createKbApi(session).upload({
      files: [first, second],
      idempotencyKey: "2f6a4b8c-1111-4222-8333-444455556666",
    });

    expect(calls[0].path).toBe("/api/v1/school/kb/documents");
    expect(calls[0].init?.method).toBe("POST");
    const headers = calls[0].init?.headers as Record<string, string> | undefined;
    expect(headers?.["Idempotency-Key"]).toBe("2f6a4b8c-1111-4222-8333-444455556666");
    expect(headers?.["Content-Type"]).toBeUndefined();
    const form = calls[0].init?.body as FormData;
    expect(form.getAll("files")).toEqual([first, second]);
    // 未编辑任何元数据 → 整段省略（后端按文件名派生标题）
    expect(form.has("metadata")).toBe(false);
    expect(result.batch_id).toBe("b-1");
  });

  it("有元数据时按 JSON 字符串发送，元素顺序与文件一致", async () => {
    const { session, calls } = sessionStub(() => ({
      body: { batch_id: "b-1", replayed: true, documents: [DOCUMENT] },
    }));
    const file = new File(["pdf"], "a.pdf", { type: "application/pdf" });

    const result = await createKbApi(session).upload({
      files: [file],
      idempotencyKey: "key-1234",
      metadata: [{ title: "自定义标题", tags: ["甲", "乙"], description: "" }],
    });

    const form = calls[0].init?.body as FormData;
    expect(JSON.parse(String(form.get("metadata")))).toEqual([
      { title: "自定义标题", tags: ["甲", "乙"], description: "" },
    ]);
    expect(result.replayed).toBe(true);
  });

  it("后端错误原样抛出（逐文件 BATCH_VALIDATION_FAILED 不被吞）", async () => {
    const detail = {
      code: "BATCH_VALIDATION_FAILED",
      message: "1 个文件未通过校验，整批未保存",
      files: [{ index: 0, filename: "a.pdf", code: "MAGIC_MISMATCH", message: "内容不是 PDF" }],
    };
    const { session } = sessionStub(() => ({ status: 422, body: detail }));

    await expect(
      createKbApi(session).upload({
        files: [new File(["x"], "a.pdf", { type: "application/pdf" })],
        idempotencyKey: "key-1234",
      }),
    ).rejects.toMatchObject({ status: 422, detail });
  });
});

describe("createKbApi 读取接口", () => {
  it("列表不带参数时不拼 query", async () => {
    const { session, calls } = sessionStub(() => ({
      body: { items: [DOCUMENT], next_cursor: null },
    }));
    const page = await createKbApi(session).listDocuments();
    expect(calls[0].path).toBe("/api/v1/school/kb/documents");
    expect(page.items).toHaveLength(1);
  });

  it("列表 limit/cursor 以 query 传递，cursor 里的特殊字符被编码", async () => {
    const { session, calls } = sessionStub(() => ({ body: { items: [], next_cursor: null } }));
    await createKbApi(session).listDocuments({ limit: 20, cursor: "2026-10-31 00:00:00+00&x" });
    expect(calls[0].path).toBe(
      "/api/v1/school/kb/documents?limit=20&cursor=2026-10-31+00%3A00%3A00%2B00%26x",
    );
  });

  it("详情按文档 id 取路径", async () => {
    const { session, calls } = sessionStub(() => ({ body: DOCUMENT }));
    const document = await createKbApi(session).getDocument("d-1");
    expect(calls[0].path).toBe("/api/v1/school/kb/documents/d-1");
    expect(calls[0].init).toBeUndefined();
    expect(document.title).toBe("课程大纲");
  });

  it("版本列表带分页参数", async () => {
    const { session, calls } = sessionStub(() => ({
      body: { items: [VERSION], next_cursor: "o-2" },
    }));
    const page = await createKbApi(session).listVersions("d-1", { limit: 20, cursor: "o-1" });
    expect(calls[0].path).toBe("/api/v1/school/kb/documents/d-1/versions?limit=20&cursor=o-1");
    expect(page.next_cursor).toBe("o-2");
  });
});
