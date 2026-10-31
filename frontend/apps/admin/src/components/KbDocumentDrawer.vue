<script setup lang="ts">
/**
 * 知识库文档详情抽屉（M3 KB-01B 简报 D5）：
 * 打开即读详情 + 第一页版本；版本列表按 `next_cursor` 追加；文档不可见（404）时提示并让列表刷新。
 */
import { ApiError } from "@campus-elf/api-client";
import type { KbDocumentView, KbVersionView } from "@campus-elf/api-client";
import { ElMessage } from "element-plus";
import { ref, watch } from "vue";

import { kbApi } from "../api";
import { reportError } from "../error-toast";
import { KB_PAGE_SIZE, documentStatusMeta, formatBytes } from "../kb-logic";
import KbScanTag from "./KbScanTag.vue";

const props = defineProps<{
  modelValue: boolean;
  documentId: string | null;
}>();

const emit = defineEmits<{
  "update:modelValue": [visible: boolean];
  /** 文档已不可见：父页面刷新当前页。 */
  gone: [];
}>();

const detail = ref<KbDocumentView | null>(null);
const loading = ref(false);
const versions = ref<KbVersionView[]>([]);
const versionsLoading = ref(false);
const nextCursor = ref<string | null>(null);
let goneNotified = false;
/** 每次打开/切换文档自增；迟到的响应与 404 一律作废，避免把 A 的数据/结论写进 B。 */
let loadGeneration = 0;

function notifyGone(): void {
  if (goneNotified) return;
  goneNotified = true;
  ElMessage.warning("文档已不可见，列表已刷新");
  emit("gone");
  emit("update:modelValue", false);
}

async function loadDetail(id: string, generation: number): Promise<void> {
  loading.value = true;
  try {
    const fresh = await kbApi.getDocument(id);
    if (generation !== loadGeneration) return;
    detail.value = fresh;
  } catch (error) {
    if (generation !== loadGeneration) return;
    if (error instanceof ApiError && error.status === 404) {
      notifyGone();
      return;
    }
    reportError(error, "文档详情加载失败");
  } finally {
    if (generation === loadGeneration) loading.value = false;
  }
}

async function loadVersions(id: string, generation: number, cursor?: string): Promise<void> {
  versionsLoading.value = true;
  try {
    const page = await kbApi.listVersions(id, { limit: KB_PAGE_SIZE, cursor });
    if (generation !== loadGeneration) return;
    versions.value = cursor === undefined ? page.items : [...versions.value, ...page.items];
    nextCursor.value = page.next_cursor;
  } catch (error) {
    if (generation !== loadGeneration) return;
    if (error instanceof ApiError && error.status === 404) {
      notifyGone();
      return;
    }
    reportError(error, "版本列表加载失败");
  } finally {
    if (generation === loadGeneration) versionsLoading.value = false;
  }
}

function loadMoreVersions(): void {
  const id = props.documentId;
  if (id === null || nextCursor.value === null) return;
  void loadVersions(id, loadGeneration, nextCursor.value);
}

watch([() => props.modelValue, () => props.documentId], ([open, id]) => {
  loadGeneration += 1;
  const generation = loadGeneration;
  detail.value = null;
  versions.value = [];
  nextCursor.value = null;
  loading.value = false;
  versionsLoading.value = false;
  if (!open || id === null) return;
  // 只在打开时重置：关闭抽屉不能被当成新一轮，否则并发的两个 404 会二次提示
  goneNotified = false;
  void loadDetail(id, generation);
  void loadVersions(id, generation);
});

function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function currentScan() {
  return detail.value?.current_version?.scan ?? null;
}
</script>

<template>
  <el-drawer
    :model-value="modelValue"
    size="560px"
    data-testid="kb-document-drawer"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <template #header>
      <div class="drawer-head">
        <span class="title">{{ detail?.title ?? "文档详情" }}</span>
        <el-tag v-if="detail" :type="documentStatusMeta(detail.status).tone" size="small">
          {{ documentStatusMeta(detail.status).label }}
        </el-tag>
      </div>
    </template>

    <div v-loading="loading" class="drawer-body">
      <template v-if="detail">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="文档 ID">
            <span class="mono">{{ detail.id }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="标签">
            <template v-if="detail.tags.length > 0">
              <el-tag v-for="tag in detail.tags" :key="tag" size="small" class="tag">{{
                tag
              }}</el-tag>
            </template>
            <span v-else>—</span>
          </el-descriptions-item>
          <el-descriptions-item label="描述">{{ detail.description ?? "—" }}</el-descriptions-item>
          <el-descriptions-item label="创建时间">
            {{ formatDateTime(detail.created_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="更新时间">
            {{ formatDateTime(detail.updated_at) }}
          </el-descriptions-item>
        </el-descriptions>

        <h3>当前版本</h3>
        <template v-if="detail.current_version">
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="版本">
              v{{ detail.current_version.version_no }}
            </el-descriptions-item>
            <el-descriptions-item label="文件名">
              {{ detail.current_version.filename }}
            </el-descriptions-item>
            <el-descriptions-item label="大小">
              {{ formatBytes(detail.current_version.size_bytes) }}
            </el-descriptions-item>
            <el-descriptions-item label="扫描">
              <KbScanTag :scan="currentScan()" />
              <div v-if="detail.current_version.scan.error_code" class="extra">
                错误码：{{ detail.current_version.scan.error_code }}
              </div>
              <div v-if="detail.current_version.scan.signature" class="extra">
                签名：{{ detail.current_version.scan.signature }}
              </div>
              <div class="extra">
                尝试次数：{{ detail.current_version.scan.attempts }} · 扫描时间：{{
                  detail.current_version.scan.scanned_at
                    ? formatDateTime(detail.current_version.scan.scanned_at)
                    : "—"
                }}
              </div>
            </el-descriptions-item>
          </el-descriptions>
        </template>
        <p v-else class="muted">暂无版本。</p>

        <h3>版本列表</h3>
        <el-table :data="versions" size="small" data-testid="kb-version-table">
          <el-table-column prop="version_no" label="版本" width="70" />
          <el-table-column prop="filename" label="文件名" min-width="150" />
          <el-table-column label="大小" width="90">
            <template #default="{ row }">{{ formatBytes(row.size_bytes) }}</template>
          </el-table-column>
          <el-table-column label="扫描" min-width="180">
            <template #default="{ row }"><KbScanTag :scan="row.scan" /></template>
          </el-table-column>
          <el-table-column label="创建时间" min-width="160">
            <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
          </el-table-column>
        </el-table>
        <el-button
          v-if="nextCursor !== null"
          class="load-more"
          size="small"
          :loading="versionsLoading"
          data-testid="kb-versions-more"
          @click="loadMoreVersions"
          >加载更多</el-button
        >
        <p v-else-if="versions.length === 0 && !versionsLoading" class="muted">暂无版本记录。</p>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.drawer-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.title {
  font-weight: 600;
}
.mono {
  font-family: monospace;
  font-size: 12px;
}
.tag {
  margin-right: 6px;
}
.extra {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.muted {
  color: var(--el-text-color-secondary);
}
.load-more {
  margin-top: 8px;
}
</style>
