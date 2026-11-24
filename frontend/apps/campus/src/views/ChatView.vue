<!-- C 端聊天页（M4-UI1/UI2）：流式正文/停止/重试/兜底/error 态 + 引用抽屉 +
     反馈入口（AI-05）+ 思考折叠 + 待机精灵（纯 CSS；真 3D 模型延后，见
     M4-UI2 简报 §6）。 -->

<script setup lang="ts">
import { showDialog, showToast } from "vant";
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { FEEDBACK_TAGS } from "@campus-elf/api-client";

import { useAuthStore } from "../stores/auth";
import { useChatStore, type ChatMessage } from "../stores/chat";

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const chat = useChatStore();

const input = ref("");
const scrollBox = ref<HTMLElement | null>(null);
const showCitations = ref(false);
const showFeedbackForm = ref(false);
/** 引用抽屉的数据源气泡 id（与反馈目标分开——两个弹层各自独立，防状态串扰）。 */
const citationMsgId = ref<string | null>(null);
const feedbackMsgId = ref<string | null>(null);
const feedbackRating = ref<"helpful" | "unhelpful">("helpful");
const feedbackTags = ref<string[]>([]);
const feedbackComment = ref("");
const feedbackError = ref("");

const canChat = computed(() => auth.user?.permissions?.includes("campus.kb.qa") ?? false);
/** 提交反馈需 campus.feedback.create（后端强制）；没有该码就不显示入口，避免白填表单。 */
const canFeedbackPerm = computed(
  () => auth.user?.permissions?.includes("campus.feedback.create") ?? false,
);
/** 引用抽屉的数据源（打开时锁定的气泡）。 */
const drawerCitations = computed(() => {
  const msg = chat.messages.find((m) => m.id === citationMsgId.value);
  return msg?.citations ?? [];
});

onMounted(() => {
  const id = route.params.conversationId;
  if (typeof id === "string" && id) {
    void chat.loadConversation(id).catch(() => {
      void showDialog({ message: "会话不存在或已被清空" });
      void router.replace({ name: "chat" });
    });
  } else {
    chat.newConversation();
  }
});

// M3（UI1 第 1 轮审查）：同组件路由复用不重挂——/chat/:id ↔ /chat 切换须复位；
// 流式中离开/卸载 → 中止在途流（防后台流写残状态）
watch(
  () => route.params.conversationId,
  (id) => {
    if (typeof id === "string" && id) {
      // meta 回显场景（本轮 URL 对齐 store.conversationId）不得重拉历史——
      // 会把刚生成的流式消息覆盖掉（UI1 第 2 轮审查 Major-3 修复的回归教训）
      if (chat.conversationId === id) return;
      void chat.loadConversation(id).catch(() => {
        void showDialog({ message: "会话不存在或已被清空" });
        void router.replace({ name: "chat" });
      });
    } else {
      // 同组件流式中跳 /chat：先中止在途流再复位（复位守卫要求 idle）
      chat.abortInFlight();
      chat.newConversation();
    }
  },
);
onBeforeUnmount(() => chat.abortInFlight());

// meta 回显 conversation_id → URL 更新（D4：刷新可续聊）；复位（null）不触发
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

const TAG_LABELS: Record<string, string> = {
  inaccurate: "内容不准确",
  irrelevant: "答非所问",
  outdated: "内容过时",
  harmful: "有害内容",
  other: "其他",
};

/** 反馈入口（卡 3）：final/fallback 且已回填 turn_id 且未反馈 → 显示 👍/👎。
 *
 * 不限制「必须是最后一条」：早先只在末条显示，导致用户追问后上一条回答的入口
 * 凭空消失、再也无法评价（独立审查 Major）。turnId 是反馈键，任何终态气泡都可用。 */
function canFeedback(msg: ChatMessage): boolean {
  return (
    canFeedbackPerm.value &&
    (msg.status === "final" || msg.status === "fallback") &&
    !!msg.turnId &&
    !msg.feedbackRating
  );
}

function openFeedback(msg: ChatMessage, rating: "helpful" | "unhelpful"): void {
  feedbackMsgId.value = msg.id;
  feedbackRating.value = rating;
  feedbackTags.value = [];
  feedbackComment.value = "";
  feedbackError.value = "";
  showFeedbackForm.value = true;
}

function openCitations(msg: ChatMessage): void {
  citationMsgId.value = msg.id;
  showCitations.value = true;
}

async function onSubmitFeedback(): Promise<void> {
  if (!feedbackMsgId.value) return;
  if (feedbackRating.value === "unhelpful" && feedbackTags.value.length === 0) {
    feedbackError.value = "请至少选择一个原因标签";
    return;
  }
  try {
    const submitted = await chat.submitFeedback(
      feedbackMsgId.value,
      feedbackRating.value,
      feedbackTags.value,
      feedbackComment.value.trim() || undefined,
    );
    // 未提交（已反馈/并发中）不提示成功——避免「toast 说成功但没落库」
    if (!submitted) return;
    showFeedbackForm.value = false;
    showToast("反馈已提交，谢谢你的帮助");
  } catch (err) {
    feedbackError.value = err instanceof Error ? err.message : "提交失败，请重试";
  }
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
    <van-nav-bar title="AI 助手" left-arrow @click-left="router.back()">
      <template #right>
        <span
          class="nav-link"
          data-testid="conversations-link"
          @click="router.push({ name: 'conversations' })"
          >会话</span
        >
      </template>
    </van-nav-bar>

    <div v-if="!canChat" class="no-permission" data-testid="no-permission">
      你的账号暂未开通 AI 问答，请联系管理员开通。
    </div>

    <template v-else>
      <div ref="scrollBox" class="messages" data-testid="messages">
        <!-- 待机精灵（M4-UI2 卡 5）：空闲态呼吸/眨眼；prefers-reduced-motion 由系统接管 -->
        <div
          v-if="chat.messages.length === 0 && !chat.isStreaming"
          class="idle-mascot"
          data-testid="idle-mascot"
        >
          <svg class="mascot" viewBox="0 0 120 120" aria-hidden="true">
            <g class="mascot-body">
              <ellipse cx="60" cy="66" rx="34" ry="36" fill="#e8f3ff" />
              <circle cx="60" cy="58" r="26" fill="#ffffff" />
              <g class="mascot-eyes">
                <ellipse class="eye" cx="50" cy="56" rx="3.2" ry="4.4" fill="#4a6b8a" />
                <ellipse class="eye" cx="70" cy="56" rx="3.2" ry="4.4" fill="#4a6b8a" />
              </g>
              <path
                class="mascot-smile"
                d="M52 68 Q60 74 68 68"
                stroke="#4a6b8a"
                stroke-width="2"
                fill="none"
                stroke-linecap="round"
              />
            </g>
          </svg>
          <div class="idle-hint">有问题就问我吧～</div>
        </div>

        <div v-for="msg in chat.messages" :key="msg.id" class="bubble-row" :class="msg.role">
          <div class="bubble" :data-role="msg.role" :data-status="msg.status">
            <!-- 思考折叠（M4-UI2 卡 3）：步骤 ≥1 时显示，默认收起 -->
            <van-collapse
              v-if="msg.steps.length"
              :model-value="msg.stepsOpen ? ['steps'] : []"
              class="steps-collapse"
              @update:model-value="(v) => (msg.stepsOpen = Array.isArray(v) && v.includes('steps'))"
            >
              <van-collapse-item
                :title="`思考过程 (${msg.steps.length})`"
                name="steps"
                data-testid="reasoning-collapse"
              >
                <div v-for="(step, i) in msg.steps" :key="i" class="step-line">· {{ step }}</div>
              </van-collapse-item>
            </van-collapse>
            <div class="content">
              {{ msg.content }}<span v-if="msg.status === 'streaming'" class="cursor">▌</span>
            </div>
            <div v-if="statusTail(msg.status)" class="tail">{{ statusTail(msg.status) }}</div>

            <!-- 引用来源（M4-UI2 卡 2）：final/fallback 且有引用 -->
            <div
              v-if="(msg.status === 'final' || msg.status === 'fallback') && msg.citations.length"
              class="citation-row"
            >
              <van-button
                size="small"
                plain
                data-testid="citation-button"
                @click="openCitations(msg)"
              >
                引用来源 ({{ msg.citations.length }})
              </van-button>
            </div>

            <!-- 反馈入口（M4-UI2 卡 3）：👍/👎；已反馈显示标记 -->
            <div v-if="canFeedback(msg)" class="feedback-row" data-testid="feedback-row">
              <van-button
                size="small"
                plain
                class="fb-btn"
                data-testid="fb-helpful"
                @click="openFeedback(msg, 'helpful')"
              >
                👍 有用
              </van-button>
              <van-button
                size="small"
                plain
                class="fb-btn"
                data-testid="fb-unhelpful"
                @click="openFeedback(msg, 'unhelpful')"
              >
                👎 无用
              </van-button>
            </div>
            <div v-else-if="msg.feedbackRating" class="tail" data-testid="feedback-done">
              已反馈（{{ msg.feedbackRating === "helpful" ? "有用" : "无用" }}）
            </div>

            <div v-if="msg.status === 'error' && chat.errorInfo?.retryable" class="retry-row">
              <van-button
                size="small"
                plain
                type="primary"
                data-testid="retry-button"
                @click="onRetry"
              >
                重新提问
              </van-button>
            </div>
          </div>
        </div>
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

    <!-- 引用抽屉（卡 2）：底部弹出，列明细（chunk_id 不进 UI） -->
    <van-popup v-model:show="showCitations" position="bottom" round :style="{ maxHeight: '60%' }">
      <div class="citation-drawer" data-testid="citation-drawer">
        <van-nav-bar title="引用来源" />
        <van-cell-group inset style="margin: 12px 0">
          <van-cell
            v-for="c in drawerCitations"
            :key="c.index"
            :title="`[${c.index}] ${c.document}`"
            :label="
              [c.section, c.page != null ? `第 ${c.page} 页` : null].filter(Boolean).join(' · ') ||
              '—'
            "
            data-testid="citation-item"
          />
        </van-cell-group>
      </div>
    </van-popup>

    <!-- 反馈表单（卡 3） -->
    <van-popup v-model:show="showFeedbackForm" position="bottom" round>
      <div class="feedback-form" data-testid="feedback-form">
        <van-nav-bar :title="feedbackRating === 'helpful' ? '反馈：有用' : '反馈：无用'" />
        <div class="form-body">
          <div v-if="feedbackRating === 'unhelpful'" class="tags-block">
            <div class="form-label">原因（可多选）</div>
            <van-checkbox-group v-model="feedbackTags" direction="horizontal">
              <van-checkbox
                v-for="tag in FEEDBACK_TAGS"
                :key="tag"
                :name="tag"
                shape="square"
                class="tag-check"
              >
                {{ TAG_LABELS[tag] }}
              </van-checkbox>
            </van-checkbox-group>
          </div>
          <div class="form-label">补充说明（可选，≤500 字）</div>
          <van-field
            v-model="feedbackComment"
            type="textarea"
            rows="2"
            autosize
            maxlength="500"
            placeholder="可选：告诉我们更多细节（不会展示给其他人）"
            data-testid="feedback-comment"
          />
          <div v-if="feedbackError" class="form-error">{{ feedbackError }}</div>
          <van-button
            block
            type="primary"
            data-testid="feedback-submit"
            :loading="chat.submittingFeedback"
            @click="onSubmitFeedback"
          >
            提交反馈
          </van-button>
        </div>
      </div>
    </van-popup>
  </div>
</template>

<style scoped>
.chat-page {
  display: flex;
  flex-direction: column;
  height: 100vh;
}
.nav-link {
  color: #1989fa;
}
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
}
.bubble-row {
  display: flex;
  margin-bottom: 10px;
}
.bubble-row.user {
  justify-content: flex-end;
}
.bubble {
  max-width: 80%;
  padding: 8px 12px;
  border-radius: 10px;
  background: #f2f3f5;
  font-size: 14px;
}
.bubble-row.user .bubble {
  background: #e8f3ff;
}
.steps-collapse {
  margin-bottom: 6px;
}
.steps-collapse :deep(.van-collapse-item__title) {
  padding: 6px 0;
  font-size: 12px;
  color: #969799;
}
.step-line {
  line-height: 1.6;
  color: #969799;
  font-size: 12px;
}
.content {
  white-space: pre-wrap;
  word-break: break-word;
}
.cursor {
  animation: blink 1s infinite;
}
@keyframes blink {
  50% {
    opacity: 0;
  }
}
.tail {
  margin-top: 4px;
  color: #969799;
  font-size: 12px;
}
.citation-row,
.feedback-row {
  margin-top: 6px;
  display: flex;
  gap: 10px;
  align-items: center;
}
.fb-btn {
  color: #1989fa;
  font-size: 13px;
}
.retry-row {
  margin-top: 6px;
}
.input-bar {
  display: flex;
  gap: 8px;
  padding: 8px 12px;
  border-top: 1px solid #ebedf0;
  align-items: flex-end;
}
.input-bar .van-field {
  flex: 1;
}
.no-permission {
  padding: 40px 20px;
  text-align: center;
  color: #969799;
}

/* 待机精灵（卡 5）：呼吸 + 眨眼；系统减弱动态效果时不动画 */
.idle-mascot {
  margin-top: 15vh;
  text-align: center;
}
.mascot {
  width: 120px;
  height: 120px;
  animation: breathe 3.2s ease-in-out infinite;
}
.mascot-eyes {
  animation: blink-eyes 4.6s infinite;
  transform-origin: 60px 56px;
}
@keyframes breathe {
  0%,
  100% {
    transform: scale(1);
  }
  50% {
    transform: scale(1.05);
  }
}
@keyframes blink-eyes {
  0%,
  92%,
  100% {
    transform: scaleY(1);
  }
  95% {
    transform: scaleY(0.1);
  }
}
@media (prefers-reduced-motion: reduce) {
  .mascot,
  .mascot-eyes,
  .cursor {
    animation: none;
  }
}
.idle-hint {
  margin-top: 12px;
  color: #969799;
  font-size: 14px;
}

.citation-drawer {
  padding-bottom: 24px;
}
.feedback-form {
  padding-bottom: 24px;
}
.form-body {
  padding: 12px 16px;
}
.form-label {
  font-size: 13px;
  color: #646566;
  margin: 8px 0;
}
.tags-block {
  margin-bottom: 4px;
}
.tag-check {
  margin: 4px 8px 4px 0;
}
.form-error {
  color: #ee0a24;
  font-size: 12px;
  margin: 8px 0;
}
</style>
