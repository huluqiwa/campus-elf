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
  'event: citation\ndata: {"index":1,"chunk_id":"c1","document":"财务报销手册","page":3,"section":"第一章"}',
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
}

/** 记录请求体，供断言提交载荷。 */
export interface ChatMockHandles {
  turnBodies: Record<string, unknown>[];
  clearCalls: string[];
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
