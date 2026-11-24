/** C 端聊天 mock（M4-UI1）：SSE 流 route.fulfill + 会话列表/历史/清空。
 *
 * SSE mock 形态（简报 D6）：一次性返回完整 `text/event-stream` 文本——前端
 * 解析器按流处理不受影响；停止弧（流中断）mock 无法模拟，移交全栈 E2E（UI2）。
 * mock 判据手抄后端（TD-253 已知缺口，全栈链⑤收口）。
 */

import type { Page } from "@playwright/test";

export const CHAT_PERMISSION = "campus.kb.qa";

const SSE_NORMAL = [
  'event: meta\ndata: {"turn_id":"t-e2e","trace_id":"r-e2e","conversation_id":"conv-e2e"}',
  'event: step\ndata: {"stage":"retrieve","label":"正在检索学校知识库"}',
  'event: reasoning_summary\ndata: {"summary":"已找到 2 条相关校规，正在核对引用"}',
  'event: delta\ndata: {"text":"根据学校规定，"}',
  'event: delta\ndata: {"text":"差旅报销需要发票和审批单 [1]。"}',
  'event: citation\ndata: {"index":1,"chunk_id":"chunk-uuid-0001-abcdef","document":"财务报销手册","page":3,"section":"第一章"}',
  'event: done\ndata: {"turn_id":"t-e2e","trace_id":"r-e2e","conversation_id":"conv-e2e","tokens_in":120,"tokens_out":30,"first_token_ms":300,"latency_ms":900,"stop_reason":"FINAL_ANSWER"}',
].join("\n\n");

const SSE_FALLBACK = [
  'event: meta\ndata: {"turn_id":"t-fb","trace_id":"r-fb","conversation_id":"conv-e2e"}',
  'event: delta\ndata: {"text":"抱歉，我暂时没有在学校知识库中找到与这个问题相关的内容。"}',
  'event: done\ndata: {"turn_id":"t-fb","trace_id":"r-fb","conversation_id":"conv-e2e","tokens_in":50,"tokens_out":25,"first_token_ms":120,"latency_ms":200,"stop_reason":"CAPABILITY_FALLBACK"}',
].join("\n\n");

const SSE_ERROR = [
  'event: meta\ndata: {"turn_id":"t-err","trace_id":"r-err","conversation_id":"conv-e2e"}',
  'event: delta\ndata: {"text":"根据学校规定，"}',
  'event: error\ndata: {"code":"LLM_TIMEOUT","message":"生成超时，请重试","retryable":true,"partial":true}',
].join("\n\n");

export interface ChatMockOptions {
  /** 聊天入口权限（默认有码；false = 无码用户走权限渲染弧）。 */
  withPermission?: boolean;
  /** POST /chat/turns 的响应形态。 */
  mode?: "normal" | "fallback" | "error";
  /** 是否 mock abort（403）：无码用户直发请求时后端语义。 */
  rejectTurn?: boolean;
  /**
   * 分段慢速下发（M4-UI2 审查 Blocker 回归）：首段只发 meta + 第一个 delta，
   * 停 `holdMs` 再发其余。用于断言「流式途中气泡已有内容」——一次性 fulfill
   * 的 mock 无法暴露「增量写入未触发渲染」这类缺陷（内容会在 done 后一次出现）。
   */
  slowStream?: { holdMs?: number };
}

/** 记录请求体，供断言提交载荷。 */
export interface ChatMockHandles {
  turnBodies: Record<string, unknown>[];
  clearCalls: string[];
}

/**
 * 分段慢速 SSE（M4-UI2 审查 Blocker 的回归测试夹具）：
 * 覆写 window.fetch，对 /chat/turns 返回**真正分块**的 ReadableStream。
 * 分块顺序是刻意的：① meta（建立占位气泡，伴随一次 ref 赋值→一次重渲染）；
 * ② 首个 delta 单独一块，之后 holdMs 内不再有任何其它 ref 变化。于是「正文在
 * hold 期间可见」只可能来自 delta 写入本身触发了渲染——用 raw 对象写增量时屏幕
 * 仍是空气泡。`route.fulfill` 一次性给全文，结构上无法暴露该缺陷。
 */
export async function mockSlowStream(page: Page, options: { holdMs?: number } = {}): Promise<void> {
  const holdMs = options.holdMs ?? 1500;
  const chunkMeta =
    'event: meta\ndata: {"turn_id":"t-e2e","trace_id":"r-e2e","conversation_id":"conv-e2e"}\n\n';
  const chunkFirstDelta = 'event: delta\ndata: {"text":"根据学校规定，"}\n\n';
  const chunkRest = [
    'event: delta\ndata: {"text":"差旅报销需要发票和审批单 [1]。"}',
    'event: citation\ndata: {"index":1,"chunk_id":"chunk-uuid-0001-abcdef","document":"财务报销手册","page":3,"section":"第一章"}',
    'event: done\ndata: {"turn_id":"t-e2e","trace_id":"r-e2e","conversation_id":"conv-e2e","tokens_in":120,"tokens_out":30,"first_token_ms":300,"latency_ms":900,"stop_reason":"FINAL_ANSWER"}',
  ].join("\n\n");

  await page.addInitScript(
    ({ holdMs, chunkMeta, chunkFirstDelta, chunkRest }) => {
      const originalFetch = window.fetch.bind(window);
      window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
        if (!url.includes("/api/v1/campus/chat/turns") || (init?.method ?? "GET") !== "POST") {
          return originalFetch(input, init);
        }
        const encoder = new TextEncoder();
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            // meta 先落地并留 400ms：让「占位气泡建立 + phase=streaming」那一批
            // 重渲染彻底走完，随后的 delta 才是对「增量写入是否触发渲染」的独立检验。
            controller.enqueue(encoder.encode(chunkMeta));
            setTimeout(() => controller.enqueue(encoder.encode(chunkFirstDelta)), 400);
            setTimeout(() => {
              controller.enqueue(encoder.encode(chunkRest));
              controller.close();
            }, 400 + holdMs);
          },
        });
        return new Response(stream, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      };
    },
    { holdMs, chunkMeta, chunkFirstDelta, chunkRest },
  );
}

export function mockChatApi(
  page: Page,
  options: ChatMockOptions = {},
): ChatMockOptions & { handles: ChatMockHandles } {
  const handles: ChatMockHandles = { turnBodies: [], clearCalls: [] };

  void page.route("**/api/v1/campus/chat/turns", async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    handles.turnBodies.push(body);
    if (options.rejectTurn) {
      await route.fulfill({
        status: 403,
        contentType: "application/json",
        body: '{"detail":"缺少权限"}',
      });
      return;
    }
    const mode = options.mode ?? "normal";
    const sse = mode === "fallback" ? SSE_FALLBACK : mode === "error" ? SSE_ERROR : SSE_NORMAL;
    await route.fulfill({ status: 200, contentType: "text/event-stream", body: sse });
  });

  void page.route("**/api/v1/campus/conversations*", async (route) => {
    if (route.request().method() === "DELETE") {
      const url = new URL(route.request().url());
      handles.clearCalls.push(url.pathname);
      await route.fulfill({ status: 200, contentType: "application/json", body: '{"cleared":2}' });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        conversations: [
          {
            id: "conv-e2e",
            title: null,
            status: "active",
            last_message_at: "2026-11-22T10:00:00+08:00",
            created_at: "2026-11-22T09:00:00+08:00",
          },
        ],
        next_cursor: null,
      }),
    });
  });

  void page.route("**/api/v1/campus/conversations/*/messages*", async (route) => {
    if (route.request().method() === "DELETE") {
      handles.clearCalls.push(route.request().url());
      await route.fulfill({ status: 200, contentType: "application/json", body: '{"cleared":2}' });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        messages: [
          { role: "user", content: "差旅报销怎么办理？" },
          { role: "assistant", content: "差旅报销需要发票和审批单 [1]。" },
        ],
      }),
    });
  });

  return { ...options, handles };
}
