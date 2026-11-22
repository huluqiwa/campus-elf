<!-- C 端会话列表（M4-UI1-A6）：keyset 游标分页 + 清空（AI-07）+ 新对话。 -->

<script setup lang="ts">
import { showDialog } from "vant";
import { ref } from "vue";
import { useRouter } from "vue-router";

import { fetchConversations, useChatStore, type CampusConversationView } from "../stores/chat";

const router = useRouter();
const chat = useChatStore();
const items = ref<CampusConversationView[]>([]);
const nextCursor = ref<string | null>(null);
const loading = ref(false);
const finished = ref(false);
const loadError = ref(false);

// van-list @load 模式（第 1 轮审查 Major-2：v-model:loading 与 @update:loading
// 双绑 + 守卫互斥导致加载死锁）；网络错误 → 错误态可重试（Minor-2）
async function loadPage(): Promise<void> {
  if (finished.value) return;
  try {
    const page = await fetchConversations({
      limit: 20,
      ...(nextCursor.value ? { cursor: nextCursor.value } : {}),
    });
    items.value = items.value.concat(page.conversations);
    nextCursor.value = page.nextCursor;
    finished.value = page.nextCursor === null;
    loadError.value = false;
  } catch {
    loadError.value = true;
    finished.value = true;
  } finally {
    // vant List 无自动复位：onLoad 完成后必须手动置回（第 2 轮审查 Major-2）
    loading.value = false;
  }
}

function retryLoad(): void {
  finished.value = false;
  loadError.value = false;
  loading.value = true; // 交由 loadPage 的 finally 复位（第 3 轮审查 Minor）
  void loadPage();
}

function formatTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("zh-CN", { dateStyle: "short", timeStyle: "short" });
}

async function onClear(item: CampusConversationView): Promise<void> {
  try {
    const dialog = await showDialog({
      title: "清空会话",
      message: "清空后该会话的消息将不可恢复（匿名保留对话记录）？确认清空？",
      showCancelButton: true,
    });
    if (dialog !== "confirm") return;
  } catch {
    return; // 取消/遮罩关闭：vant showDialog 对非 confirm 一律 reject
  }
  try {
    await chat.clearById(item.id);
  } catch {
    await showDialog({ message: "清空失败，请重试" });
    return;
  }
  items.value = items.value.filter((c) => c.id !== item.id);
}
</script>

<template>
  <div class="conversations-page">
    <van-nav-bar title="历史会话" left-arrow @click-left="router.back()">
      <template #right>
        <span class="nav-link" data-testid="new-chat-link" @click="router.push({ name: 'chat' })"
          >新对话</span
        >
      </template>
    </van-nav-bar>

    <van-list
      v-model:loading="loading"
      :finished="finished"
      :finished-text="loadError ? '' : '没有更多了'"
      @load="loadPage"
    >
      <van-cell-group inset style="margin-top: 12px">
        <van-cell
          v-for="item in items"
          :key="item.id"
          :title="item.title ?? 'AI 对话'"
          :label="formatTime(item.lastMessageAt ?? item.createdAt)"
          is-link
          :data-testid="`conversation-${item.id}`"
          @click="router.push({ name: 'chat-detail', params: { conversationId: item.id } })"
        >
          <template #value>
            <van-button
              size="small"
              plain
              type="danger"
              data-testid="clear-button"
              @click.stop="onClear(item)"
            >
              清空
            </van-button>
          </template>
        </van-cell>
      </van-cell-group>
    </van-list>
    <div v-if="loadError" class="empty">
      加载失败
      <van-button size="small" plain type="primary" data-testid="retry-load" @click="retryLoad">
        重试
      </van-button>
    </div>
    <div v-else-if="!loading && items.length === 0 && finished" class="empty">
      还没有会话，去问问看吧～
    </div>
  </div>
</template>

<style scoped>
.conversations-page {
  height: 100vh;
}
.nav-link {
  color: #1989fa;
}
.empty {
  text-align: center;
  color: #969799;
  margin-top: 40px;
}
</style>
