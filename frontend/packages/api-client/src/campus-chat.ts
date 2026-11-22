/** C 端聊天 API 客户端（M4-UI1）：SSE 流式回合 + 会话列表/历史/清空（AI-06/07/05）。
 *
 * SSE 消费（D1）：fetch POST + ReadableStream 手写解析——EventSource 不支持
 * POST/Authorization。分帧按 `\n\n`，残包跨 chunk 缓冲；事件 data 为 snake_case
 * JSON，解析层映射为 camelCase discriminated union。
 * 停止（D5）：AbortController 由调用方持有；abort 后 fetch 抛 AbortError，
 * streamTurn 归一为 ChatAbortError（UI 标「已停止」，服务端 USER_CANCELLED）。
 */

export interface CampusChatApiOptions {
  /** API 根地址；空串表示同源相对路径。 */
  baseUrl: string;
  fetchImpl?: typeof fetch;
}

export interface StreamTurnInput {
  message: string;
  conversationId?: string;
}

export interface ChatMetaEvent {
  type: "meta";
  turnId: string;
  traceId: string;
  conversationId: string;
}
export interface ChatStepEvent {
  type: "step";
  stage: string;
  label?: string;
}
export interface ChatToolCallEvent {
  type: "tool_call";
  tool: string;
  status: string;
}
export interface ChatToolResultEvent {
  type: "tool_result";
  tool: string;
  status: string;
  durationMs?: number;
  hits?: number;
}
export interface ChatReasoningEvent {
  type: "reasoning_summary";
  summary: string;
}
export interface ChatDeltaEvent {
  type: "delta";
  text: string;
}
export interface ChatCitationEvent {
  type: "citation";
  index: number;
  chunkId: string;
  document: string;
  page: number | null;
  section: string | null;
}
export interface ChatDoneEvent {
  type: "done";
  turnId?: string;
  traceId?: string;
  conversationId?: string;
  tokensIn: number;
  tokensOut: number;
  firstTokenMs: number;
  latencyMs: number;
  stopReason: string;
}
export interface ChatErrorEvent {
  type: "error";
  code: string;
  message: string;
  retryable: boolean;
  partial: boolean;
}

export type ChatStreamEvent =
  | ChatMetaEvent
  | ChatStepEvent
  | ChatToolCallEvent
  | ChatToolResultEvent
  | ChatReasoningEvent
  | ChatDeltaEvent
  | ChatCitationEvent
  | ChatDoneEvent
  | ChatErrorEvent;

/** 流中断/HTTP 层错误（非 error 事件——error 事件经 onEvent 下发）。 */
export class ChatApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "ChatApiError";
  }
}

/** 客户端主动停止（UI 捕获后标「已停止」，不按错误呈现）。 */
export class ChatAbortError extends Error {
  constructor() {
    super("chat stream aborted");
    this.name = "ChatAbortError";
  }
}

interface RawFrame {
  event: string;
  data: string;
}

function mapEvent(raw: RawFrame): ChatStreamEvent | null {
  let body: Record<string, unknown>;
  try {
    body = JSON.parse(raw.data) as Record<string, unknown>;
  } catch {
    return null; // 非 JSON data：忽略（健壮性——后端保证 JSON）
  }
  const num = (v: unknown): number => (typeof v === "number" ? v : 0);
  const str = (v: unknown): string => (typeof v === "string" ? v : "");
  const optStr = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);
  const optNum = (v: unknown): number | undefined => (typeof v === "number" ? v : undefined);
  switch (raw.event) {
    case "meta":
      return {
        type: "meta",
        turnId: str(body.turn_id),
        traceId: str(body.trace_id),
        conversationId: str(body.conversation_id),
      };
    case "step":
      return { type: "step", stage: str(body.stage), label: optStr(body.label) };
    case "tool_call":
      return { type: "tool_call", tool: str(body.tool), status: str(body.status) };
    case "tool_result":
      return {
        type: "tool_result",
        tool: str(body.tool),
        status: str(body.status),
        durationMs: optNum(body.duration_ms),
        hits: optNum(body.hits),
      };
    case "reasoning_summary":
      return { type: "reasoning_summary", summary: str(body.summary) };
    case "delta":
      return { type: "delta", text: str(body.text) };
    case "citation":
      return {
        type: "citation",
        index: num(body.index),
        chunkId: str(body.chunk_id),
        document: str(body.document),
        page: (typeof body.page === "number" ? body.page : null) as number | null,
        section: (typeof body.section === "string" ? body.section : null) as string | null,
      };
    case "done":
      return {
        type: "done",
        turnId: optStr(body.turn_id),
        traceId: optStr(body.trace_id),
        conversationId: optStr(body.conversation_id),
        tokensIn: num(body.tokens_in),
        tokensOut: num(body.tokens_out),
        firstTokenMs: num(body.first_token_ms),
        latencyMs: num(body.latency_ms),
        stopReason: str(body.stop_reason),
      };
    case "error":
      return {
        type: "error",
        code: str(body.code),
        message: str(body.message),
        retryable: body.retryable === true,
        partial: body.partial === true,
      };
    default:
      return null; // 未知事件类型：忽略（前向兼容）
  }
}

/** 解析一个完整 SSE 帧（多行 data 按规范拼接；无 event 名的帧忽略）。 */
function parseFrame(block: string): RawFrame | null {
  let event = "";
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).replace(/^ /, ""));
    }
    // 注释行（:开头）与其他字段（id:/retry:）忽略
  }
  if (!event || dataLines.length === 0) return null;
  return { event, data: dataLines.join("\n") };
}

export interface CampusConversationView {
  id: string;
  title: string | null;
  status: string;
  lastMessageAt: string | null;
  createdAt: string;
}

export interface CampusConversationPage {
  conversations: CampusConversationView[];
  nextCursor: string | null;
}

export interface CampusMessageView {
  role: string;
  content: string;
}

export interface CampusChatApi {
  /** 流式对话回合（SSE）；abort 由调用方 signal 控制。 */
  streamTurn(input: StreamTurnInput, handlers: { onEvent: (e: ChatStreamEvent) => void; signal?: AbortSignal }): Promise<void>;
  /** 会话列表（keyset 游标；M4-AI06/07）。 */
  listConversations(query?: { limit?: number; cursor?: string }): Promise<CampusConversationPage>;
  /** 会话历史（排除 deleted/failed）。 */
  getConversationMessages(conversationId: string): Promise<{ messages: CampusMessageView[] }>;
  /** 清空会话（AI-07；幂等）。 */
  clearConversation(conversationId: string): Promise<{ cleared: number }>;
}

export function createCampusChatApi(
  session: {
    getAccessToken(): string | null;
    refresh(): Promise<unknown>;
    request<T>(path: string, init?: RequestInit): Promise<T>;
  },
  options: CampusChatApiOptions,
): CampusChatApi {
  const baseUrl = options.baseUrl.replace(/\/+$/, "");
  const fetchImpl = options.fetchImpl ?? fetch;

  async function authedFetch(path: string, init: RequestInit): Promise<Response> {
    const token = session.getAccessToken();
    if (!token) throw new ChatApiError("未登录", 401);
    let response = await fetchImpl(`${baseUrl}${path}`, {
      ...init,
      headers: { ...(init.headers as Record<string, string>), Authorization: `Bearer ${token}` },
    });
    if (response.status === 401) {
      await session.refresh();
      const retryToken = session.getAccessToken();
      if (!retryToken) throw new ChatApiError("未登录", 401);
      response = await fetchImpl(`${baseUrl}${path}`, {
        ...init,
        headers: {
          ...(init.headers as Record<string, string>),
          Authorization: `Bearer ${retryToken}`,
        },
      });
    }
    return response;
  }

  return {
    async streamTurn(input, handlers) {
      let response: Response;
      try {
        response = await authedFetch("/api/v1/campus/chat/turns", {
          method: "POST",
          headers: { Accept: "text/event-stream", "Content-Type": "application/json" },
          body: JSON.stringify({
            message: input.message,
            ...(input.conversationId ? { conversation_id: input.conversationId } : {}),
          }),
          signal: handlers.signal,
        });
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") throw new ChatAbortError();
        throw err instanceof ChatApiError ? err : new ChatApiError(String(err));
      }
      if (!response.ok || !response.body) {
        // 错误信封（code/message）优先
        let detail = `chat request failed: ${response.status}`;
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") detail = body.detail;
        } catch {
          /* 非 JSON：保留默认文案 */
        }
        throw new ChatApiError(detail, response.status);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      try {
        for (;;) {
          let chunk: ReadableStreamReadResult<Uint8Array>;
          try {
            chunk = await reader.read();
          } catch (err) {
            if (err instanceof DOMException && err.name === "AbortError") throw new ChatAbortError();
            throw err;
          }
          if (chunk.done) break;
          buffer += decoder.decode(chunk.value, { stream: true });
          // 事件以空行分隔；最后一个残包留缓冲
          let separator = buffer.indexOf("\n\n");
          while (separator >= 0) {
            const block = buffer.slice(0, separator);
            buffer = buffer.slice(separator + 2);
            const frame = parseFrame(block);
            if (frame) {
              const event = mapEvent(frame);
              if (event) handlers.onEvent(event);
            }
            separator = buffer.indexOf("\n\n");
          }
        }
        // 流正常结束：残包若含完整帧（SSE 允许流尾无终止空行）也要 flush
        buffer += decoder.decode();
        if (buffer.trim()) {
          const frame = parseFrame(buffer);
          if (frame) {
            const event = mapEvent(frame);
            if (event) handlers.onEvent(event);
          }
        }
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") throw new ChatAbortError();
        throw err;
      }
    },

    async listConversations(query = {}) {
      const params = new URLSearchParams();
      if (query.limit !== undefined) params.set("limit", String(query.limit));
      if (query.cursor) params.set("cursor", query.cursor);
      const qs = params.toString();
      return session.request<CampusConversationPage>(
        `/api/v1/campus/conversations${qs ? `?${qs}` : ""}`,
      );
    },

    async getConversationMessages(conversationId) {
      return session.request<{ messages: CampusMessageView[] }>(
        `/api/v1/campus/conversations/${conversationId}/messages`,
      );
    },

    async clearConversation(conversationId) {
      return session.request<{ cleared: number }>(
        `/api/v1/campus/conversations/${conversationId}/messages`,
        { method: "DELETE" },
      );
    },
  };
}
