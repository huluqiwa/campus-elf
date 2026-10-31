<script setup lang="ts">
/**
 * 知识库文档列表（M3 KB-01B 简报 D4/D5）：
 * cursor 游标栈分页（不伪造页码）、手动刷新；加载失败保留旧行 + 醒目的「非最新」横幅。
 * 数据范围（全校/本人）由后端按 `school.kb.view` 的主体裁剪，页面不提供切换。
 */
import type { KbDocumentView } from "@campus-elf/api-client";
import { onMounted, ref } from "vue";

import { kbApi } from "../api";
import KbDocumentDrawer from "../components/KbDocumentDrawer.vue";
import KbScanTag from "../components/KbScanTag.vue";
import { reportError } from "../error-toast";
import { KB_PAGE_SIZE, documentStatusMeta, formatBytes, staleBannerText } from "../kb-logic";

const documents = ref<KbDocumentView[]>([]);
/** 每一页发起请求时用的游标（第 0 页为 null）；只保存已探明的前缀。 */
const cursors = ref<(string | null)[]>([null]);
const pageIndex = ref(0);
const nextCursor = ref<string | null>(null);
const loading = ref(false);
const stale = ref(false);
const lastLoadedAt = ref<Date | null>(null);

const drawerVisible = ref(false);
const activeDocumentId = ref<string | null>(null);

/** 只让最后一次请求落地：按钮虽已在加载中禁用，仍防慢网下旧响应覆盖新页。 */
let loadSeq = 0;

async function loadPage(index: number): Promise<void> {
  const seq = ++loadSeq;
  loading.value = true;
  try {
    const cursor = cursors.value[index] ?? null;
    if (index > 0 && cursor === null) {
      // 正常路径不可达（上一页/下一页都按游标栈控制）；宁可报错也不静默把第一页当目标页
      stale.value = true;
      reportError(new Error("分页游标丢失"), "文档列表加载失败");
      return;
    }
    const pageData = await kbApi.listDocuments({
      limit: KB_PAGE_SIZE,
      cursor: cursor ?? undefined,
    });
    if (seq !== loadSeq) return;
    documents.value = pageData.items;
    nextCursor.value = pageData.next_cursor;
    pageIndex.value = index;
    cursors.value = cursors.value.slice(0, index + 1);
    if (pageData.next_cursor) cursors.value[index + 1] = pageData.next_cursor;
    lastLoadedAt.value = new Date();
    stale.value = false;
  } catch (error) {
    if (seq !== loadSeq) return;
    // TD-260 的教训：失败不能静默保留旧行，必须让人知道看到的不是最新数据
    stale.value = true;
    reportError(error, "文档列表加载失败");
  } finally {
    if (seq === loadSeq) loading.value = false;
  }
}

function openDocument(document: KbDocumentView): void {
  activeDocumentId.value = document.id;
  drawerVisible.value = true;
}

function onGone(): void {
  void loadPage(pageIndex.value);
}

function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { hour12: false });
}

onMounted(() => {
  void loadPage(0);
});
</script>

<template>
  <el-card>
    <template #header>
      <div class="head">
        <span>知识库文档</span>
        <div class="actions">
          <el-button :disabled="loading || pageIndex === 0" @click="loadPage(pageIndex - 1)"
            >上一页</el-button
          >
          <el-button :disabled="loading || nextCursor === null" @click="loadPage(pageIndex + 1)"
            >下一页</el-button
          >
          <el-button
            data-testid="kb-refresh"
            :loading="loading"
            :disabled="loading"
            @click="loadPage(pageIndex)"
            >刷新</el-button
          >
        </div>
      </div>
    </template>

    <div v-if="stale" class="stale" data-testid="kb-stale-banner">
      <el-alert type="warning" :closable="false" show-icon :title="staleBannerText(lastLoadedAt)" />
      <el-button size="small" data-testid="kb-stale-retry" @click="loadPage(pageIndex)"
        >重试</el-button
      >
    </div>

    <el-table v-loading="loading" :data="documents" data-testid="kb-document-table">
      <el-table-column prop="title" label="标题" min-width="180" />
      <el-table-column label="文档状态" width="140">
        <template #default="{ row }">
          <el-tag :type="documentStatusMeta(row.status).tone" size="small">
            {{ documentStatusMeta(row.status).label }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="当前版本" min-width="200">
        <template #default="{ row }">
          <template v-if="row.current_version">
            <div>{{ row.current_version.filename }}</div>
            <div class="sub">
              {{ formatBytes(row.current_version.size_bytes) }} · v{{
                row.current_version.version_no
              }}
            </div>
          </template>
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column label="扫描" min-width="220">
        <template #default="{ row }">
          <KbScanTag :scan="row.current_version?.scan ?? null" />
          <div v-if="row.current_version?.scan.error_code" class="sub">
            错误码：{{ row.current_version.scan.error_code }}
          </div>
          <div v-if="row.current_version?.scan.signature" class="sub">
            签名：{{ row.current_version.scan.signature }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="更新时间" min-width="170">
        <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90">
        <template #default="{ row }">
          <el-button link type="primary" @click="openDocument(row as KbDocumentView)"
            >查看</el-button
          >
        </template>
      </el-table-column>
    </el-table>

    <p v-if="!loading && documents.length === 0 && !stale" class="empty">暂无文档。</p>
  </el-card>

  <KbDocumentDrawer v-model="drawerVisible" :document-id="activeDocumentId" @gone="onGone" />
</template>

<style scoped>
.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.actions {
  display: flex;
  gap: 8px;
}
.stale {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.sub {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.empty {
  color: var(--el-text-color-secondary);
  text-align: center;
  padding: 16px 0;
}
</style>
