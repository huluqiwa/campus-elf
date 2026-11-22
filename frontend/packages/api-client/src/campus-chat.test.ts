/** campus-chat 单元测试：SSE 分帧（半包/跨 chunk/多事件一帧）/abort/错误信封/事件映射。 */

import { describe, expect, it, vi } from "vitest";

import {
  ChatAbortError,
  ChatApiError,
  createCampusChatApi,
  type ChatStreamEvent,
} from "./campus-chat";

const SSE_FULL = [
  'event: meta\ndata: {"turn_id":"t1","trace_id":"r1","conversation_id":"c1"}',
  'event: step\ndata: {"stage":"retrieve","label":"正在检索"}',
  'event: reasoning_summary\ndata: {"summary":"已找到 2 条"}',
  'event: delta\ndata: {"text":"根据学校规定，"}',
  'event: delta\ndata: {"text":"差旅报销需审批 [1]。"}',
  'event: citation\ndata: {"index":1,"chunk_id":"c1","document":"财务报销手册","page":3,"section":"第一章"}',
  'event: done\ndata: {"tokens_in":100,"tokens_out":20,"first_token_ms":300,"latency_ms":900,"stop_reason":"FINAL_ANSWER"}',
].join("\n\n");

function sseResponse(body: string, init?: { status?: number }): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(body));
      controller.close();
    },
  });
  return new Response(stream, {
    status: init?.status ?? 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

function makeApi(fetchImpl: () => Response) {
  const asyncFetch: typeof fetch = (() =>
    Promise.resolve(fetchImpl())) as unknown as typeof fetch;
  const session = {
    getAccessToken: vi.fn(() => "tok"),
    refresh: vi.fn(async () => undefined),
    request: vi.fn(async <T>(): Promise<T> => {
      return { conversations: [], nextCursor: null } as T;
    }),
  };
  // vi.fn 的泛型与 AuthSession 结构类型兼容性差：测试桩按结构断言（运行时形状正确）
  return {
    api: createCampusChatApi(
      session as unknown as Parameters<typeof createCampusChatApi>[0],
      { baseUrl: "", fetchImpl: asyncFetch },
    ),
    session,
  };
}

async function collect(body: string): Promise<ChatStreamEvent[]> {
  const { api } = makeApi(() => sseResponse(body));
  const events: ChatStreamEvent[] = [];
  await api.streamTurn({ message: "差旅报销怎么办理？" }, { onEvent: (e) => events.push(e) });
  return events;
}

describe("campusChat.streamTurn", () => {
  it("完整流：9 类事件按序映射为 camelCase union", async () => {
    const events = await collect(SSE_FULL);
    expect(events.map((e) => e.type)).toEqual([
      "meta",
      "step",
      "reasoning_summary",
      "delta",
      "delta",
      "citation",
      "done",
    ]);
    const meta = events[0];
    expect(meta.type === "meta" && meta.turnId === "t1" && meta.conversationId === "c1").toBe(true);
    const citation = events[5];
    expect(citation.type === "citation" && citation.document === "财务报销手册").toBe(true);
    const done = events[6];
    expect(done.type === "done" && done.stopReason === "FINAL_ANSWER" && done.tokensOut === 20).toBe(
      true,
    );
  });

  it("半包：事件被 chunk 边界切开仍正确分帧", async () => {
    // 在 delta 帧中间切断
    const cut = SSE_FULL.indexOf('data: {"text":"根据学校规定，"}') + 10;
    const first = SSE_FULL.slice(0, cut);
    const second = SSE_FULL.slice(cut);
    const { api } = makeApi(() => {
      const chunks = [first, second].map((c) => new TextEncoder().encode(c));
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          for (const c of chunks) controller.enqueue(c);
          controller.close();
        },
      });
      return new Response(stream, { status: 200 });
    });
    const events: ChatStreamEvent[] = [];
    await api.streamTurn({ message: "问" }, { onEvent: (e) => events.push(e) });
    expect(events.filter((e) => e.type === "delta")).toHaveLength(2);
    expect(events[events.length - 1].type).toBe("done");
  });

  it("一个 chunk 内多帧 + 尾部无空行残包", async () => {
    // 全部事件一个 chunk（无 \n\n 结尾的残包不含完整帧——最后一个 done 帧带 \n\n）
    const events = await collect(SSE_FULL + "\n\n");
    expect(events).toHaveLength(7);
  });

  it("未知事件类型与非 JSON data 忽略（前向兼容）", async () => {
    const events = await collect(
      'event: future_thing\ndata: {"x":1}\n\nevent: delta\ndata: {broken json\n\nevent: delta\ndata: {"text":"好"}\n\n',
    );
    expect(events).toEqual([{ type: "delta", text: "好" }]);
  });

  it("abort → ChatAbortError（不按普通错误抛）", async () => {
    const controller = new AbortController();
    let streamController!: ReadableStreamDefaultController<Uint8Array>;
    const { api } = makeApi(() => {
      const stream = new ReadableStream<Uint8Array>({
        start(c) {
          streamController = c;
          c.enqueue(new TextEncoder().encode(SSE_FULL));
          // 不 close：模拟长流
        },
      });
      return new Response(stream, { status: 200 });
    });
    const pending = api.streamTurn({ message: "问" }, {
      onEvent: () => {
        // 模拟真实 fetch：signal abort → body reader 抛 AbortError
        controller.abort();
        streamController.error(new DOMException("This operation was aborted", "AbortError"));
      },
      signal: controller.signal,
    });
    await expect(pending).rejects.toBeInstanceOf(ChatAbortError);
  });

  it("HTTP 403 错误信封 → ChatApiError(status)", async () => {
    const { api } = makeApi(() => new Response('{"detail":"缺少权限"}', { status: 403 }));
    const pending = api.streamTurn({ message: "问" }, { onEvent: vi.fn() });
    await expect(pending).rejects.toBeInstanceOf(ChatApiError);
    await expect(pending).rejects.toMatchObject({ status: 403 });
  });

  it("401 → 自动刷新一次并重放", async () => {
    let calls = 0;
    const { api, session } = makeApi(() => {
      calls += 1;
      if (calls === 1) return new Response('{"detail":"expired"}', { status: 401 });
      return sseResponse('event: meta\ndata: {"turn_id":"t","trace_id":"r","conversation_id":"c"}\n\n');
    });
    const events: ChatStreamEvent[] = [];
    await api.streamTurn({ message: "问" }, { onEvent: (e) => events.push(e) });
    expect(session.refresh).toHaveBeenCalledTimes(1);
    expect(events[0].type).toBe("meta");
  });
});

describe("campusChat 会话方法", () => {
  it("listConversations / getConversationMessages / clearConversation 走 request 基建", async () => {
    const request = vi.fn(async <T>(): Promise<T> => ({ conversations: [], nextCursor: null }) as T);
    const session = { request, getAccessToken: vi.fn(() => "tok"), refresh: vi.fn() };
    const api = createCampusChatApi(
      session as unknown as Parameters<typeof createCampusChatApi>[0],
      { baseUrl: "" },
    );
    await api.listConversations({ limit: 5, cursor: "abc" });
    expect(request).toHaveBeenCalledWith("/api/v1/campus/conversations?limit=5&cursor=abc");
    await api.getConversationMessages("conv1");
    expect(request).toHaveBeenLastCalledWith("/api/v1/campus/conversations/conv1/messages");
    await api.clearConversation("conv1");
    expect(request).toHaveBeenLastCalledWith("/api/v1/campus/conversations/conv1/messages", {
      method: "DELETE",
    });
  });
});
