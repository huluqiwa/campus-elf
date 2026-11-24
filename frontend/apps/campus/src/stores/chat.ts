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
  FeedbackTag,
} from "@campus-elf/api-client";
import { createFeedbackApi } from "@campus-elf/api-client";
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
  /** meta 事件的 turn_id（AI-05 反馈键）——占位气泡由 meta 回填。 */
  turnId?: string;
  /** 已提交反馈（UI2：防重复提交；刷新后丢失可接受——后端幂等兜底 TD-P7）。 */
  feedbackRating?: "helpful" | "unhelpful";
  /** 思考折叠展开态（UI2 卡 3：默认收起——undefined 即收起）。 */
  stepsOpen?: boolean;
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
  /** turn_id → trace_id（AI-05 反馈载荷需要；meta 时记录）。 */
  const traceIdOfTurn = new Map<string, string>();

  const isStreaming = computed(() => phase.value === "streaming");
  const lastAssistant = computed(() =>
    [...messages.value].reverse().find((m) => m.role === "assistant"),
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
        // 占位气泡回填 turn_id（AI-05 反馈键）+ turn→trace 映射
        msg.turnId = event.turnId;
        traceIdOfTurn.set(event.turnId, event.traceId);
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
    messages.value.push({
      id: nextId(),
      role: "user",
      content: text.trim(),
      steps: [],
      citations: [],
      status: "final",
    });
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
        errorInfo.value = {
          code: "STREAM_INCOMPLETE",
          message: "回答不完整，请重试",
          retryable: true,
        };
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

  /** 组件卸载/路由切换时中止在途流（第 1 轮审查 Major-3：防后台流写残状态）。 */
  function abortInFlight(): void {
    if (phase.value === "streaming") controller?.abort();
  }

  /** 重试（A5）：仅末条 assistant（error/stopped）可重试；连 user 气泡一并移除
   * 后以原输入发新回合（第 1 轮审查 Minor-1：避免重复用户气泡/pop 错位）。 */
  async function retry(): Promise<void> {
    if (phase.value === "streaming" || !lastUserMessage.value) return;
    const last = messages.value[messages.value.length - 1];
    if (
      !last ||
      last.role !== "assistant" ||
      (last.status !== "error" && last.status !== "stopped")
    ) {
      return;
    }
    messages.value.pop();
    const prev = messages.value[messages.value.length - 1];
    if (prev && prev.role === "user") messages.value.pop();
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
    // 第 3 轮审查 Minor 收口：abort 后无条件清（同组件流式中跳 /chat 的复位
    // 不再被 phase 守卫吞掉；abort 后残余事件最多写入一行残留，send 的
    // catch 会随后把终态标为 stopped/error——自愈）
    controller?.abort();
    phase.value = "idle";
    conversationId.value = null;
    messages.value = [];
    errorInfo.value = null;
    traceIdOfTurn.clear();
  }

  /** 按会话 id 清空（会话列表页用；不触碰当前打开的对话状态）。 */
  async function clearById(id: string): Promise<void> {
    await chatApi.clearConversation(id);
  }

  // 反馈（UI2：D2——按 turn 内存记，刷新丢失可接受，后端幂等兜底 TD-P7）
  const feedbackApi = createFeedbackApi(session, {
    baseUrl: import.meta.env.VITE_API_BASE_URL ?? "",
  });
  const submittingFeedback = ref(false);

  async function submitFeedback(
    msgId: string,
    rating: "helpful" | "unhelpful",
    reasonTags: string[],
    comment?: string,
  ): Promise<void> {
    const msg = messages.value.find((m) => m.id === msgId);
    if (!msg?.turnId || !conversationId.value || submittingFeedback.value) return;
    if (msg.feedbackRating) return; // 已反馈防重复（幂等兜底在后端）
    submittingFeedback.value = true;
    try {
      await feedbackApi.submitFeedback({
        conversationId: conversationId.value,
        turnId: msg.turnId,
        traceId: traceIdOfTurn.get(msg.turnId) ?? "",
        rating,
        reasonTags: reasonTags as FeedbackTag[],
        comment,
      });
      msg.feedbackRating = rating;
    } finally {
      submittingFeedback.value = false;
    }
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
    abortInFlight,
    clearById,
    submitFeedback,
    submittingFeedback,
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
