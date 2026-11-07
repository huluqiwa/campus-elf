<script setup lang="ts">
/**
 * 审批中心（M3 KB-02B 简报 D1）：待办 / 已办两个 tab + 详情抽屉。
 *
 * - 后端 `GET /school/approvals/todo|done` 只有游标分页 → 「还有没有下一页」只认
 *   `next_cursor`（后端只在填满一页时才给游标，与权限账号页同一教训）；
 * - 已办 = 与本人有过**任何**动作往来的单：提交人提交后立刻在这里看到自己的单
 *   （撤销入口的可发现性来自这条定义，`app/approval/service.py::list_done`）；
 * - 两个 tab 各自独立的游标栈与失败横幅；写动作成功后重读**当前 tab** 的当前页
 *   （状态以服务端为准，不在本地手改），另一个 tab 在下次切入时整页重取；
 * - 加载失败不清空已显示的行，挂「数据可能不是最新」横幅 + 重试失败的那一页（TD-260）。
 */
import type { ApprovalRequestView } from "@campus-elf/api-client";
import { computed, onMounted, ref } from "vue";

import { approvalsApi } from "../api";
import { reportError } from "../error-toast";
import { actionLabel, approvalStatusMeta, approvalTypeLabel } from "../approval-logic";
import ApprovalDetailDrawer from "../components/ApprovalDetailDrawer.vue";
import { useAuthStore } from "../stores/auth";

const PAGE_SIZE = 20;

const auth = useAuthStore();
const permissions = computed(() => auth.user?.permissions ?? []);
const selfUserId = computed<string | null>(() => auth.user?.userId ?? null);

type TabKey = "todo" | "done";

interface TabState {
  rows: ApprovalRequestView[];
  nextCursor: string | null;
  /** 游标栈：`cursorStack[i]` 是第 i 页的入参游标（第 0 页为 null），与权限账号页同一做法。 */
  cursorStack: (string | null)[];
  page: number;
  loading: boolean;
  loaded: boolean;
  lastLoadedAt: Date | null;
  /** 最近一次失败的页码：重试要重发这一页。 */
  failedPage: number | null;
}

function emptyTab(): TabState {
  return {
    rows: [],
    nextCursor: null,
    cursorStack: [null],
    page: 0,
    loading: false,
    loaded: false,
    lastLoadedAt: null,
    failedPage: null,
  };
}

const activeTab = ref<TabKey>("todo");
const todo = ref<TabState>(emptyTab());
const done = ref<TabState>(emptyTab());

function stateOf(tab: TabKey): TabState {
  return tab === "todo" ? todo.value : done.value;
}

function clockOf(at: Date): string {
  return [at.getHours(), at.getMinutes(), at.getSeconds()]
    .map((part) => String(part).padStart(2, "0"))
    .join(":");
}

const staleHint = computed<string | null>(() => {
  const state = stateOf(activeTab.value);
  if (state.failedPage === null) return null;
  const at = state.lastLoadedAt;
  return at ? `数据可能不是最新（上次成功 ${clockOf(at)}）` : "数据可能不是最新（尚未成功加载过）";
});

/** 请求序号按 tab 分账：只让最新一次加载写该 tab 的状态（先发后到的旧响应不作数）。 */
const loadSeq: Record<TabKey, number> = { todo: 0, done: 0 };

async function loadPage(tab: TabKey, index: number): Promise<void> {
  const state = stateOf(tab);
  const seq = ++loadSeq[tab];
  state.loading = true;
  try {
    const cursor = state.cursorStack[index] ?? null;
    const data =
      tab === "todo"
        ? await approvalsApi.listTodo({ limit: PAGE_SIZE, cursor: cursor ?? undefined })
        : await approvalsApi.listDone({ limit: PAGE_SIZE, cursor: cursor ?? undefined });
    if (seq !== loadSeq[tab]) return;
    state.rows = data.items;
    state.nextCursor = data.next_cursor;
    state.cursorStack = state.cursorStack.slice(0, index + 1);
    if (data.next_cursor) state.cursorStack[index + 1] = data.next_cursor;
    state.page = index;
    state.lastLoadedAt = new Date();
    state.failedPage = null;
    state.loaded = true;
  } catch (error) {
    if (seq !== loadSeq[tab]) return;
    state.failedPage = index;
    reportError(error, tab === "todo" ? "待办列表加载失败" : "已办列表加载失败");
  } finally {
    if (seq === loadSeq[tab]) state.loading = false;
  }
}

/** 切 tab：重取该 tab 的当前页（保证切回来看到的是最新状态，位置不丢）。 */
function switchTab(tab: string | number): void {
  const key = tab === "done" ? "done" : "todo";
  activeTab.value = key;
  const state = stateOf(key);
  void loadPage(key, state.loaded ? state.page : 0);
}

function retryFailedPage(): void {
  const state = stateOf(activeTab.value);
  if (state.failedPage !== null) void loadPage(activeTab.value, state.failedPage);
}

const drawerVisible = ref(false);
const activeRequestId = ref<string | null>(null);

function openRow(row: ApprovalRequestView): void {
  activeRequestId.value = row.id;
  drawerVisible.value = true;
}

/** 抽屉里任一写动作成功：重读当前 tab 的当前页，并让下一次切入另一 tab 时整页重取。 */
function onChanged(): void {
  const state = stateOf(activeTab.value);
  void loadPage(activeTab.value, state.page);
}

function rowTestid(row: ApprovalRequestView): string {
  return `approval-row-${row.id}`;
}

const emptyText = computed(() =>
  activeTab.value === "done" ? "没有与你有过动作往来的审批单" : "没有待处理的审批单",
);

function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { hour12: false });
}

onMounted(() => {
  void loadPage("todo", 0);
});
</script>

<template>
  <div class="page">
    <el-card>
      <template #header>
        <div class="card-head">
          <span>审批中心</span>
          <span class="hint"
            >待办是全部待审批单；已办是与我有动作往来的单（自己提交的也在这里）。</span
          >
        </div>
      </template>

      <el-tabs :model-value="activeTab" data-testid="approval-tabs" @tab-change="switchTab">
        <el-tab-pane label="待办" name="todo" />
        <el-tab-pane label="已办" name="done" />
      </el-tabs>

      <div class="toolbar">
        <el-button
          :data-testid="`approval-reload-${activeTab}`"
          @click="loadPage(activeTab, stateOf(activeTab).page)"
        >
          刷新
        </el-button>
      </div>

      <el-alert
        v-if="staleHint"
        class="stale-alert"
        type="warning"
        :closable="false"
        show-icon
        data-testid="approval-stale-alert"
      >
        <div class="stale-body">
          <span>{{ staleHint }}</span>
          <el-button size="small" data-testid="approval-stale-retry" @click="retryFailedPage">
            重试
          </el-button>
        </div>
      </el-alert>

      <el-table
        v-loading="stateOf(activeTab).loading"
        :data="stateOf(activeTab).rows"
        data-testid="approval-table"
        :empty-text="emptyText"
        @row-click="openRow"
      >
        <el-table-column label="标题" min-width="240">
          <template #default="{ row }">
            <span :data-testid="rowTestid(row as ApprovalRequestView)">{{ row.title }}</span>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="120">
          <template #default="{ row }">{{ approvalTypeLabel(row.type_code) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="approvalStatusMeta(row.status).tone">
              {{ approvalStatusMeta(row.status).label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近动作" min-width="120">
          <template #default="{ row }">
            <span>{{ row.result ? actionLabel(row.result) : "—" }}</span>
          </template>
        </el-table-column>
        <el-table-column label="提交时间" min-width="160">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button
              size="small"
              data-testid="approval-open"
              @click.stop="openRow(row as ApprovalRequestView)"
              >详情</el-button
            >
          </template>
        </el-table-column>
      </el-table>

      <div class="pager">
        <el-button
          :data-testid="`approval-prev-${activeTab}`"
          :disabled="stateOf(activeTab).page === 0"
          @click="loadPage(activeTab, stateOf(activeTab).page - 1)"
        >
          上一页
        </el-button>
        <span class="page-info" :data-testid="`approval-page-info-${activeTab}`">
          第 {{ stateOf(activeTab).page + 1 }} 页
        </span>
        <el-button
          :data-testid="`approval-next-${activeTab}`"
          :disabled="!stateOf(activeTab).nextCursor"
          @click="loadPage(activeTab, stateOf(activeTab).page + 1)"
        >
          下一页
        </el-button>
      </div>
    </el-card>

    <ApprovalDetailDrawer
      v-model="drawerVisible"
      :request-id="activeRequestId"
      :self-user-id="selfUserId"
      :permissions="permissions"
      @changed="onChanged"
    />
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.card-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
}

.card-head .hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}

.stale-alert {
  margin-bottom: 12px;
}

.stale-body {
  display: flex;
  align-items: center;
  gap: 12px;
}

.pager {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
}

.page-info {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.approval-table :deep(.el-table__row) {
  cursor: pointer;
}
</style>
