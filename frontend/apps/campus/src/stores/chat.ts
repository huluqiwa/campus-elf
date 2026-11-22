/** 聊天状态（M4-UI1）：SSE 流式回合 + 消息模型 + 停止/重试 + 会话加载/清空。
 *
 * 消息模型（D3）：meta 事件后预插入 assistant 占位气泡；delta 逐字追加；
 * reasoning_summary 轻量步骤条（折叠面板归 UI2）；citation 数据记录（抽屉归 UI2）。
 * 停止（D5）：AbortController → ChatAbortError → status=stopped；abort 后事件忽略。
 */

import type {
  CampusConversationPage,
  CampusConversationView,
  CampusMessageView,
  ChatApiError,
  ChatStreamEvent,
} from "@campus-elf/api-client";
import { ChatAbortError, createCampusChatApi } from "@campus-elf/api-client";
import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { session } from "../session";

export interface ChatCitation {
  index: number;
  chunkId: string;
  document: string;
  page: number | null;
  section: string | null;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  steps: string[];
  citations: ChatCitation[];
  status: "streaming" | "final" | "fallback" | "stopped" | "error";
  errorInfo?: { code: string; message: string; retryable: boolean };
}

const chatApi = createCampusChatApi(session, { baseUrl: import.meta.env.VITE_API_BASE_URL ?? "" });

let messageSeq = 0;
function nextId(): string {
  messageSeq += 1;
  return `msg-${messageSeq}`;
}

export const useChatStore = defineStore("chat", () => {
  const messages = ref<ChatMessage[]>([]);
  const conversationId = ref<string | null>(null);
  /** idle=无进行中回合；streaming=流式中；终态为最后一条 assistant 消息的 status。 */
  const phase = ref<"idle" | "streaming">("idle");
  const errorInfo = ref<{ code: string; message: string; retryable: boolean } | null>(null);
  /** 最近一次未完成的用户输入（重试用）。 */
  const lastUserMessage = ref<string | null>(null);
  let controller: AbortController | null = null;

  const isStreaming = computed(() => phase.value === "streaming");
  const lastAssistant = computed(
    () => [...messages.value].reverse().find((m) => m.role === "assistant"),
  );

  function _placeholder(): ChatMessage {
    return {
      id: nextId(),
      role: "assistant",
      content: "",
      steps: [],
      citations: [],
      status: "streaming",
    };
  }

  function _applyEvent(msg: ChatMessage, event: ChatStreamEvent): void {
    switch (event.type) {
      case "meta": {
        if (!conversationId.value) conversationId.value = event.conversationId;
        break;
      }
      case "reasoning_summary":
        msg.steps.push(event.summary);
        break;
      case "delta":
        msg.content += event.text;
        break;
      case "citation": {
        if (!msg.citations.some((c) => c.index === event.index)) {
          msg.citations.push({
            index: event.index,
            chunkId: event.chunkId,
            document: event.document,
            page: event.page,
            section: event.section,
          });
        }
        break;
      }
      case "done":
        msg.status = event.stopReason === "FINAL_ANSWER" ? "final" : "fallback";
        break;
      case "error":
        msg.status = "error";
        errorInfo.value = {
          code: event.code,
          message: event.message,
          retryable: event.retryable,
        };
        break;
      default:
        break; // step/tool_call/tool_result：UI1 轻量处理，不展示
    }
  }

  async function send(text: string): Promise<void> {
    if (phase.value === "streaming" || !text.trim()) return;
    lastUserMessage.value = text.trim();
    errorInfo.value = null;
    messages.value.push({ id: nextId(), role: "user", content: text.trim(), steps: [], citations: [], status: "final" });
    const placeholder = _placeholder();
    messages.value.push(placeholder);
    phase.value = "streaming";
    controller = new AbortController();
    try {
      await chatApi.streamTurn(
        {
          message: text.trim(),
          ...(conversationId.value ? { conversationId: conversationId.value } : {}),
        },
        {
          signal: controller.signal,
          onEvent: (event) => _applyEvent(placeholder, event),
        },
      );
      if (placeholder.status === "streaming") {
        // 流结束但无 done（服务端断流）：按不完整处理（design §5.8 重试口径）
        placeholder.status = "error";
        errorInfo.value = { code: "STREAM_INCOMPLETE", message: "回答不完整，请重试", retryable: true };
      }
    } catch (err) {
      if (err instanceof ChatAbortError) {
        placeholder.status = "stopped";
      } else {
        const apiErr = err as ChatApiError;
        placeholder.status = "error";
        errorInfo.value = {
          code: "CHAT_REQUEST_FAILED",
          message: apiErr.message || "请求失败，请重试",
          retryable: true,
        };
      }
    } finally {
      phase.value = "idle";
      controller = null;
    }
  }

  function stop(): void {
    controller?.abort();
  }

  /** 重试（A5）：清掉失败/停止的 assistant 气泡，以原输入发新回合。 */
  async function retry(): Promise<void> {
    if (!lastUserMessage.value || phase.value === "streaming") return;
    const last = messages.value[messages.value.length - 1];
    if (last && last.role === "assistant") messages.value.pop();
    await send(lastUserMessage.value);
  }

  /** 进入既有会话：拉历史并渲染（A6）。 */
  async function loadConversation(id: string): Promise<void> {
    if (phase.value === "streaming") return;
    conversationId.value = id;
    messages.value = [];
    const { messages: history } = await chatApi.getConversationMessages(id);
    messages.value = history.map((m: CampusMessageView) => ({
      id: nextId(),
      role: m.role === "assistant" ? ("assistant" as const) : ("user" as const),
      content: m.content,
      steps: [],
      citations: [],
      status: "final" as const,
    }));
  }

  function newConversation(): void {
    if (phase.value === "streaming") return;
    conversationId.value = null;
    messages.value = [];
  }

  /** 清空当前会话（A6；AI-07 幂等）。 */
  async function clearCurrent(): Promise<void> {
    if (!conversationId.value) return;
    await chatApi.clearConversation(conversationId.value);
    messages.value = [];
  }

  /** 按会话 id 清空（会话列表页用；不触碰当前打开的对话状态）。 */
  async function clearById(id: string): Promise<void> {
    await chatApi.clearConversation(id);
  }

  return {
    messages,
    conversationId,
    phase,
    isStreaming,
    errorInfo,
    lastAssistant,
    send,
    stop,
    retry,
    loadConversation,
    newConversation,
    clearCurrent,
    clearById,
  };
});

/** 会话列表（ConversationsView 自取；放 store 外避免状态纠缠）。 */
export async function fetchConversations(query?: {
  limit?: number;
  cursor?: string;
}): Promise<CampusConversationPage> {
  return chatApi.listConversations(query);
}

export type { CampusConversationView };
