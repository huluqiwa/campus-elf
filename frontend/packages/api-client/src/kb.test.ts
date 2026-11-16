import { describe, expect, it, vi } from "vitest";

import { ApiError } from "./auth";
import { createKbApi } from "./kb";
import type {
  KbChunkDetailView,
  KbChunkStatsView,
  KbChunkView,
  KbDocumentView,
  KbPublishResult,
  KbRetrievalTestResult,
  KbVersionStateResult,
  KbVersionView,
} from "./kb";

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

  it("详情按文档 id 取路径，回包含 active_version_id（KB-08A 契约）", async () => {
    const { session, calls } = sessionStub(() => ({
      body: { ...DOCUMENT, active_version_id: "v-1" },
    }));
    const document = await createKbApi(session).getDocument("d-1");
    expect(calls[0].path).toBe("/api/v1/school/kb/documents/d-1");
    expect(calls[0].init).toBeUndefined();
    expect(document.title).toBe("课程大纲");
    expect(document.active_version_id).toBe("v-1");
  });

  it("详情无活跃发布时 active_version_id 为 null（fail-closed）", async () => {
    const { session } = sessionStub(() => ({
      body: { ...DOCUMENT, active_version_id: null },
    }));
    const document = await createKbApi(session).getDocument("d-1");
    expect(document.active_version_id).toBeNull();
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

// KB-05B：发布/回滚/下线/删除四动作（后端契约 school_kb.py:854/879/903/928）。
const PUBLISH_RESULT: KbPublishResult = {
  document_id: "d-1",
  version_id: "v-1",
  version_no: 1,
  status: "PUBLISHED",
  alias: "tenant_abc_kb_doc_active",
  collection: "tenant_abc_kb_v1",
  previous_version_id: null,
  published_at: "2026-11-10T00:00:00Z",
  replayed: false,
  alias_synced: true,
};

const STATE_RESULT: KbVersionStateResult = {
  document_id: "d-1",
  version_id: "v-1",
  status: "OFFLINE",
  was_active: true,
};

describe("createKbApi 发布动作", () => {
  it("publish POST 到 /versions/{id}/publish，无请求体", async () => {
    const { session, calls } = sessionStub(() => ({ body: PUBLISH_RESULT }));
    const result = await createKbApi(session).publishVersion("v-1");
    expect(calls[0].path).toBe("/api/v1/school/kb/versions/v-1/publish");
    expect(calls[0].init?.method).toBe("POST");
    expect(calls[0].init?.body).toBeUndefined();
    expect(result.alias_synced).toBe(true);
  });

  it("rollback 的 path id 是回滚目标版本（D2-A），无请求体", async () => {
    const { session, calls } = sessionStub(() => ({
      body: { ...PUBLISH_RESULT, version_id: "v-0" },
    }));
    const result = await createKbApi(session).rollbackVersion("v-0");
    expect(calls[0].path).toBe("/api/v1/school/kb/versions/v-0/rollback");
    expect(calls[0].init?.method).toBe("POST");
    expect(result.version_id).toBe("v-0");
  });

  it("publish 409 KB_NOT_TEST_PASSED 原样抛出", async () => {
    const detail = { code: "KB_NOT_TEST_PASSED", message: "该版本未通过测试，不能发布" };
    const { session } = sessionStub(() => ({ status: 409, body: detail }));
    await expect(createKbApi(session).publishVersion("v-1")).rejects.toMatchObject({
      status: 409,
      detail,
    });
  });
});

describe("createKbApi 状态变更动作", () => {
  it("offline POST 到 /versions/{id}/offline，返回 was_active", async () => {
    const { session, calls } = sessionStub(() => ({ body: STATE_RESULT }));
    const result = await createKbApi(session).offlineVersion("v-1");
    expect(calls[0].path).toBe("/api/v1/school/kb/versions/v-1/offline");
    expect(calls[0].init?.method).toBe("POST");
    expect(result.was_active).toBe(true);
    expect(result.status).toBe("OFFLINE");
  });

  it("deleteVersion 用 DELETE 方法且无请求体", async () => {
    const { session, calls } = sessionStub(() => ({
      body: { ...STATE_RESULT, status: "DELETED" },
    }));
    const result = await createKbApi(session).deleteVersion("v-1");
    expect(calls[0].path).toBe("/api/v1/school/kb/versions/v-1");
    expect(calls[0].init?.method).toBe("DELETE");
    expect(result.status).toBe("DELETED");
  });

  it("offline 409 KB_NOT_PUBLISHED 原样抛出", async () => {
    const detail = { code: "KB_NOT_PUBLISHED", message: "只有已发布版本可以下线" };
    const { session } = sessionStub(() => ({ status: 409, body: detail }));
    await expect(createKbApi(session).offlineVersion("v-1")).rejects.toMatchObject({
      status: 409,
      detail,
    });
  });
});

// ---------------------------------------------------------------------------
// KB-06B：分块预览三读（后端契约 school_kb.py:1064/1098/1119；游标与筛选语义见 chunk_read.py）
// ---------------------------------------------------------------------------

const CHUNK: KbChunkView = {
  id: "c-1",
  chunk_type: "parent",
  chunk_index: 0,
  parent_id: null,
  text: "配置项1已生效。\n\n配置项2已生效。",
  text_markdown: "配置项1已生效。\n\n配置项2已生效。",
  section_path: "操作手册 > 第二章 配置",
  page_start: 1,
  page_end: 1,
  token_count: 12,
  char_count: 24,
  embedding_status: "ready",
  created_at: "2026-11-10T00:00:00Z",
  is_human_edited: false,
  edited_by: null,
  edited_at: null,
  chunk_hash: "a".repeat(64),
};

const CHUNK_DETAIL: KbChunkDetailView = {
  ...CHUNK,
  document_version_id: "v-1",
  parent: null,
  children: [{ id: "c-2", chunk_index: 1 }],
};

const CHUNK_STATS: KbChunkStatsView = {
  total_chunks: 10,
  parent_count: 5,
  child_count: 5,
  embedding_status: { pending: 5, ready: 5, failed: 0 },
  total_tokens: 120,
  total_chars: 240,
  section_count: 3,
  sections: [{ section_path: "操作手册 > 第一章 安装", chunk_count: 4 }],
};

describe("createKbApi 分块预览", () => {
  it("listChunks GET /versions/{id}/chunks，无 query 时无查询串", async () => {
    const { session, calls } = sessionStub(() => ({ body: { items: [CHUNK], next_cursor: null } }));
    const result = await createKbApi(session).listChunks("v-1");
    expect(calls[0].path).toBe("/api/v1/school/kb/versions/v-1/chunks");
    expect(calls[0].init?.method).toBeUndefined();
    expect(result.items[0]?.chunk_type).toBe("parent");
    expect(result.next_cursor).toBeNull();
  });

  it("listChunks 序列化四筛选与分页参数（undefined 省略）", async () => {
    const { session, calls } = sessionStub(() => ({ body: { items: [], next_cursor: "cur" } }));
    await createKbApi(session).listChunks("v-1", {
      limit: 50,
      cursor: "cur-0",
      q: "配置项",
      section_path: "操作手册 > 第二章 配置",
      chunk_type: "child",
      parent_id: "c-9",
    });
    expect(calls[0].path).toBe(
      "/api/v1/school/kb/versions/v-1/chunks?limit=50&cursor=cur-0&q=%E9%85%8D%E7%BD%AE%E9%A1%B9" +
        "&section_path=%E6%93%8D%E4%BD%9C%E6%89%8B%E5%86%8C+%3E+%E7%AC%AC%E4%BA%8C%E7%AB%A0+%E9%85%8D%E7%BD%AE" +
        "&chunk_type=child&parent_id=c-9",
    );
  });

  it("getChunk GET /chunks/{id}", async () => {
    const { session, calls } = sessionStub(() => ({ body: CHUNK_DETAIL }));
    const detail = await createKbApi(session).getChunk("c-1");
    expect(calls[0].path).toBe("/api/v1/school/kb/chunks/c-1");
    expect(detail.document_version_id).toBe("v-1");
    expect(detail.children).toEqual([{ id: "c-2", chunk_index: 1 }]);
  });

  it("getChunkStats GET /versions/{id}/chunks/stats", async () => {
    const { session, calls } = sessionStub(() => ({ body: CHUNK_STATS }));
    const stats = await createKbApi(session).getChunkStats("v-1");
    expect(calls[0].path).toBe("/api/v1/school/kb/versions/v-1/chunks/stats");
    expect(stats.total_chunks).toBe(10);
    expect(stats.embedding_status).toEqual({ pending: 5, ready: 5, failed: 0 });
    expect(stats.sections[0]?.chunk_count).toBe(4);
  });

  it("chunks 404 CHUNK_NOT_FOUND 原样抛出", async () => {
    const detail = { code: "CHUNK_NOT_FOUND", message: "分块不存在" };
    const { session } = sessionStub(() => ({ status: 404, body: detail }));
    await expect(createKbApi(session).getChunk("c-x")).rejects.toMatchObject({
      status: 404,
      detail,
    });
  });

  it("chunks 400 INVALID_CURSOR 原样抛出", async () => {
    const detail = { code: "INVALID_CURSOR", message: "分页游标无效" };
    const { session } = sessionStub(() => ({ status: 400, body: detail }));
    await expect(
      createKbApi(session).listChunks("v-1", { cursor: "garbage" }),
    ).rejects.toMatchObject({ status: 400, detail });
  });
});

// ---------------------------------------------------------------------------
// KB-07B：检索测试（后端契约 school_kb.py POST /versions/{id}/retrieval-test；编排 retrieval_test.py）
// ---------------------------------------------------------------------------

const RETRIEVAL_RESULT: KbRetrievalTestResult = {
  version_id: "v-1",
  query: "配置项",
  top_k: 10,
  with_rerank: false,
  rerank_status: "disabled",
  latency_ms: 12.3,
  pool_size: 2,
  resolved_audience_tokens: ["all"],
  acl_filter_applied: true,
  results: [
    {
      chunk_id: "c-1",
      chunk_index: 0,
      chunk_type: "child",
      section_path: "操作手册 > 第二章 配置",
      page_start: 1,
      page_end: 1,
      text_preview: "配置项1已生效。",
      rrf_score: 0.0325,
      dense: { score: 0.9786, rank: 1 },
      sparse: { score: 3.2, rank: 1 },
      rerank_score: null,
      final_rank: 1,
      is_final_reference: true,
      acl_matched_tokens: ["all"],
    },
    {
      chunk_id: "c-2",
      chunk_index: 1,
      chunk_type: "child",
      section_path: null,
      page_start: null,
      page_end: null,
      text_preview: "无关内容",
      rrf_score: 0.0163,
      dense: null,
      sparse: null,
      rerank_score: null,
      final_rank: 2,
      is_final_reference: true,
      acl_matched_tokens: [],
    },
  ],
};

describe("createKbApi.retrievalTest", () => {
  it("POST /versions/{id}/retrieval-test，JSON body 按输入序列化（缺省 top_k/with_rerank 省略）", async () => {
    const { session, calls } = sessionStub(() => ({ body: RETRIEVAL_RESULT }));
    const result = await createKbApi(session).retrievalTest("v-1", { query: "配置项" });
    expect(calls[0].path).toBe("/api/v1/school/kb/versions/v-1/retrieval-test");
    expect(calls[0].init?.method).toBe("POST");
    expect(calls[0].init?.headers).toMatchObject({ "Content-Type": "application/json" });
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ query: "配置项" });
    expect(result.rerank_status).toBe("disabled");
    expect(result.results[0]?.dense?.rank).toBe(1);
  });

  it("top_k/with_rerank 提供时并入 body", async () => {
    const { session, calls } = sessionStub(() => ({
      body: { ...RETRIEVAL_RESULT, with_rerank: true, rerank_status: "applied", top_k: 5 },
    }));
    const result = await createKbApi(session).retrievalTest("v-1", {
      query: "奖学金条件",
      top_k: 5,
      with_rerank: true,
    });
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      query: "奖学金条件",
      top_k: 5,
      with_rerank: true,
    });
    expect(result.rerank_status).toBe("applied");
  });

  it("404 VERSION_NOT_FOUND 原样抛出", async () => {
    const detail = { code: "VERSION_NOT_FOUND", message: "文档版本不存在" };
    const { session } = sessionStub(() => ({ status: 404, body: detail }));
    await expect(createKbApi(session).retrievalTest("v-x", { query: "q" })).rejects.toMatchObject({
      status: 404,
      detail,
    });
  });

  it("422（如 ACL 预留参数被后端 fail-closed 拒绝）原样抛出", async () => {
    const detail = { code: "RETRIEVAL_TEST_ACL_UNAVAILABLE", message: "ACL 视角检索未开放" };
    const { session } = sessionStub(() => ({ status: 422, body: detail }));
    await expect(createKbApi(session).retrievalTest("v-1", { query: "q" })).rejects.toMatchObject({
      status: 422,
      detail,
    });
  });

  it("502 基础设施故障（SEARCH_FAILED）原样抛出", async () => {
    const detail = { code: "RETRIEVAL_TEST_SEARCH_FAILED", message: "向量检索失败" };
    const { session } = sessionStub(() => ({ status: 502, body: detail }));
    await expect(createKbApi(session).retrievalTest("v-1", { query: "q" })).rejects.toMatchObject({
      status: 502,
      detail,
    });
  });
});

describe("createKbApi.audiences（KB-09C）", () => {
  const ACL_VIEW = {
    version_id: "v-1",
    audiences: [{ audience_type: "department", audience_id: "d-1" }],
    acl_confirmed: true,
    acl_confirmed_by: "u-9",
    acl_confirmed_at: "2026-11-14T00:00:00Z",
  };

  it("getAudiences 按 version GET", async () => {
    const { session, calls } = sessionStub(() => ({ body: ACL_VIEW }));
    const result = await createKbApi(session).getAudiences("v-1");

    expect(calls[0].path).toBe("/api/v1/school/kb/document-versions/v-1/audiences");
    expect(calls[0].init?.method).toBeUndefined();
    expect(result.acl_confirmed).toBe(true);
    expect(result.audiences[0].audience_type).toBe("department");
  });

  it("replaceAudiences PUT 全量替换（JSON 体 snake_case）", async () => {
    const { session, calls } = sessionStub(() => ({ body: ACL_VIEW }));
    const result = await createKbApi(session).replaceAudiences("v-1", {
      audiences: [
        { audience_type: "department", audience_id: "d-1" },
        { audience_type: "all", audience_id: null },
      ],
    });

    expect(calls[0].path).toBe("/api/v1/school/kb/document-versions/v-1/audiences");
    expect(calls[0].init?.method).toBe("PUT");
    const headers = calls[0].init?.headers as Record<string, string>;
    expect(headers["Content-Type"]).toBe("application/json");
    const body = JSON.parse(calls[0].init?.body as string) as {
      audiences: { audience_type: string; audience_id: string | null }[];
    };
    expect(body.audiences).toHaveLength(2);
    expect(body.audiences[1]).toEqual({ audience_type: "all", audience_id: null });
    expect(result.acl_confirmed).toBe(true);
  });

  it("confirmAudiences POST confirm", async () => {
    const { session, calls } = sessionStub(() => ({ body: ACL_VIEW }));
    const result = await createKbApi(session).confirmAudiences("v-1");

    expect(calls[0].path).toBe("/api/v1/school/kb/document-versions/v-1/audiences/confirm");
    expect(calls[0].init?.method).toBe("POST");
    expect(result.version_id).toBe("v-1");
  });

  it("跨租/不存在 404 VERSION_NOT_FOUND 原样抛出", async () => {
    const detail = { code: "VERSION_NOT_FOUND", message: "文档版本不存在" };
    const { session } = sessionStub(() => ({ status: 404, body: detail }));
    await expect(createKbApi(session).getAudiences("v-x")).rejects.toMatchObject({
      status: 404,
      detail,
    });
  });

  it("PUT 422 AUDIENCE_ALL_MIXED 原样抛出（服务端为唯一门禁）", async () => {
    const detail = { code: "AUDIENCE_ALL_MIXED", message: "all（显式全体）不得与其他人群类型混配" };
    const { session } = sessionStub(() => ({ status: 422, body: detail }));
    await expect(
      createKbApi(session).replaceAudiences("v-1", {
        audiences: [{ audience_type: "all", audience_id: null }],
      }),
    ).rejects.toMatchObject({ status: 422, detail });
  });
});

describe("createKbApi.retrievalTest（KB-09C 视角三参数）", () => {
  const ROW = {
    chunk_id: "c-1",
    chunk_index: 1,
    chunk_type: "child",
    section_path: "s",
    page_start: null,
    page_end: null,
    text_preview: "预览",
    rrf_score: 0.9,
    dense: { score: 1, rank: 1 },
    sparse: null,
    rerank_score: null,
    final_rank: 1,
    is_final_reference: true,
    acl_matched_tokens: ["all"],
  };
  const RESULT = {
    version_id: "v-1",
    query: "q",
    top_k: 10,
    with_rerank: false,
    rerank_status: "disabled",
    latency_ms: 1,
    pool_size: 1,
    results: [ROW],
    resolved_audience_tokens: ["all"],
    acl_filter_applied: true,
  };

  it("默认操作者视角：三参数缺省不进请求体", async () => {
    const { session, calls } = sessionStub(() => ({ body: RESULT }));
    await createKbApi(session).retrievalTest("v-1", { query: "q" });

    const body = JSON.parse(calls[0].init?.body as string) as Record<string, unknown>;
    expect(body.as_user_id).toBeUndefined();
    expect(body.as_audiences).toBeUndefined();
    expect(body.ignore_acl).toBeUndefined();
    expect(body.acl_filter_applied).toBeUndefined();
  });

  it("as_user_id 视角：snake_case 进请求体", async () => {
    const { session, calls } = sessionStub(() => ({ body: RESULT }));
    await createKbApi(session).retrievalTest("v-1", {
      query: "q",
      as_user_id: "u-7",
    });

    const body = JSON.parse(calls[0].init?.body as string) as Record<string, unknown>;
    expect(body.as_user_id).toBe("u-7");
    expect(body.as_audiences).toBeUndefined();
    expect(body.ignore_acl).toBeUndefined();
  });

  it("as_audiences + ignore_acl 进请求体", async () => {
    const { session, calls } = sessionStub(() => ({ body: RESULT }));
    await createKbApi(session).retrievalTest("v-1", {
      query: "q",
      as_audiences: ["department:d-1", "all"],
      ignore_acl: true,
    });

    const body = JSON.parse(calls[0].init?.body as string) as Record<string, unknown>;
    expect(body.as_audiences).toEqual(["department:d-1", "all"]);
    expect(body.ignore_acl).toBe(true);
  });

  it("响应回读 resolved_audience_tokens/acl_filter_applied 与逐行 acl_matched_tokens", async () => {
    const { session } = sessionStub(() => ({ body: RESULT }));
    const result = await createKbApi(session).retrievalTest("v-1", { query: "q" });

    expect(result.resolved_audience_tokens).toEqual(["all"]);
    expect(result.acl_filter_applied).toBe(true);
    expect(result.results[0].acl_matched_tokens).toEqual(["all"]);
  });
});
