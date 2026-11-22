<!-- C 端聊天页（M4-UI1）：流式正文/停止/重试/兜底展示；引用抽屉与思考折叠归 UI2。 -->

<script setup lang="ts">
import { showDialog } from "vant";
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { useAuthStore } from "../stores/auth";
import { useChatStore } from "../stores/chat";

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const chat = useChatStore();

const input = ref("");
const scrollBox = ref<HTMLElement | null>(null);

const canChat = computed(() => auth.user?.permissions?.includes("campus.kb.qa") ?? false);

onMounted(() => {
  const id = route.params.conversationId;
  if (typeof id === "string" && id) {
    void chat.loadConversation(id).catch(() => {
      void showDialog({ message: "会话不存在或已被清空" });
      void router.replace({ name: "chat" });
    });
  }
});

// meta 回显 conversation_id → URL 更新（D4：刷新可续聊）
watch(
  () => chat.conversationId,
  (id) => {
    if (id && route.params.conversationId !== id) {
      void router.replace({ name: "chat-detail", params: { conversationId: id } });
    }
  },
);

// 新消息自动滚动到底
watch(
  () => chat.messages.map((m) => m.content.length).join("|") + chat.messages.length,
  async () => {
    await new Promise((r) => setTimeout(r, 0));
    scrollBox.value?.scrollTo({ top: scrollBox.value.scrollHeight });
  },
);

async function onSend(): Promise<void> {
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  await chat.send(text);
}

async function onRetry(): Promise<void> {
  await chat.retry();
}

function statusTail(status: string): string {
  if (status === "stopped") return "（已停止）";
  if (status === "fallback") return "（暂无相关资料）";
  if (status === "error") return "（回答失败）";
  return "";
}
</script>

<template>
  <div class="chat-page">
    <van-nav-bar
      title="AI 助手"
      left-arrow
      @click-left="router.back()"
    >
      <template #right>
        <span class="nav-link" data-testid="conversations-link" @click="router.push({ name: 'conversations' })">会话</span>
      </template>
    </van-nav-bar>

    <div v-if="!canChat" class="no-permission" data-testid="no-permission">
      你的账号暂未开通 AI 问答，请联系管理员开通。
    </div>

    <template v-else>
      <div ref="scrollBox" class="messages" data-testid="messages">
        <div
          v-for="msg in chat.messages"
          :key="msg.id"
          class="bubble-row"
          :class="msg.role"
        >
          <div class="bubble" :data-role="msg.role" :data-status="msg.status">
            <div v-if="msg.steps.length" class="steps" data-testid="reasoning-steps">
              <div v-for="(step, i) in msg.steps" :key="i" class="step-line">· {{ step }}</div>
            </div>
            <div class="content">{{ msg.content }}<span v-if="msg.status === 'streaming'" class="cursor">▌</span></div>
            <div v-if="statusTail(msg.status)" class="tail">{{ statusTail(msg.status) }}</div>
            <div v-if="msg.status === 'error' && chat.errorInfo?.retryable" class="retry-row">
              <van-button size="small" plain type="primary" data-testid="retry-button" @click="onRetry">
                重新提问
              </van-button>
            </div>
          </div>
        </div>
        <div v-if="chat.messages.length === 0" class="empty">有问题就问我吧～</div>
      </div>

      <div class="input-bar">
        <van-field
          v-model="input"
          type="textarea"
          rows="1"
          autosize
          maxlength="2000"
          placeholder="输入你的问题（≤2000 字）"
          :disabled="chat.isStreaming"
          data-testid="chat-input"
        />
        <van-button
          v-if="chat.isStreaming"
          type="danger"
          size="small"
          data-testid="stop-button"
          @click="chat.stop()"
        >
          停止
        </van-button>
        <van-button
          v-else
          type="primary"
          size="small"
          :disabled="!input.trim()"
          data-testid="send-button"
          @click="onSend"
        >
          发送
        </van-button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.chat-page { display: flex; flex-direction: column; height: 100vh; }
.nav-link { color: #1989fa; }
.messages { flex: 1; overflow-y: auto; padding: 12px; }
.bubble-row { display: flex; margin-bottom: 10px; }
.bubble-row.user { justify-content: flex-end; }
.bubble { max-width: 80%; padding: 8px 12px; border-radius: 10px; background: #f2f3f5; font-size: 14px; }
.bubble-row.user .bubble { background: #e8f3ff; }
.steps { border-left: 2px solid #c8c9cc; padding-left: 8px; margin-bottom: 6px; color: #969799; font-size: 12px; }
.step-line { line-height: 1.6; }
.content { white-space: pre-wrap; word-break: break-word; }
.cursor { animation: blink 1s infinite; }
@keyframes blink { 50% { opacity: 0; } }
.tail { margin-top: 4px; color: #969799; font-size: 12px; }
.retry-row { margin-top: 6px; }
.empty { text-align: center; color: #969799; margin-top: 40px; }
.input-bar { display: flex; gap: 8px; padding: 8px 12px; border-top: 1px solid #ebedf0; align-items: flex-end; }
.input-bar .van-field { flex: 1; }
.no-permission { padding: 40px 20px; text-align: center; color: #969799; }
</style>
