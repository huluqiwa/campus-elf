<script setup lang="ts">
/**
 * 知识库上传（M3 KB-01B，简报 `docs/design-briefs/M3-KB-01B-frontend.md` D2/D3/D4/D7）：
 * 批量选择 → 本地预检 → 可选元数据 → 上传（整批原子 + 幂等键）→ 扫描状态轮询。
 *
 * - 后端仍是唯一权威：本地预检只拦「必然 422」的数量/大小/扩展名，内容级校验以服务端为准；
 * - 幂等键随「文件 + 元数据」签名变化更换：网络重试复用；409 后强制换新键；
 * - 元数据开关关闭 = 本次不发送 metadata（已填内容保留在表单里，重新打开可继续编辑）；
 * - 只有持 `school.kb.view` 才轮询扫描结论；默认教师只有 upload，看到的是静态说明。
 */
import { ApiError } from "@campus-elf/api-client";
import type { KbDocumentView, KbUploadResult } from "@campus-elf/api-client";
import { ElMessage } from "element-plus";
import { computed, onBeforeUnmount, ref } from "vue";

import { kbApi } from "../api";
import KbScanTag from "../components/KbScanTag.vue";
import {
  SCAN_POLL_INTERVAL_MS,
  SCAN_POLL_WINDOW_MS,
  batchPreflightError,
  buildMetadata,
  defaultTitle,
  documentStatusMeta,
  filePreflightError,
  formatBytes,
  kbErrorInfo,
  metadataError,
  pendingScanDocumentIds,
  shouldPollScan,
  uploadAttemptFor,
  uploadSignature,
  type KbErrorInfo,
  type KbMetadataRow,
  type UploadAttempt,
} from "../kb-logic";
import { useAuthStore } from "../stores/auth";

interface UploadRow extends KbMetadataRow {
  uid: number;
  file: File;
}

const auth = useAuthStore();
const hasView = computed(() => auth.user?.permissions.includes("school.kb.view") ?? false);

const rows = ref<UploadRow[]>([]);
const metaEnabled = ref(false);
const attempted = ref(false);
const uploading = ref(false);
const attempt = ref<UploadAttempt | null>(null);
const result = ref<KbUploadResult | null>(null);
const errorInfo = ref<KbErrorInfo | null>(null);

let nextUid = 1;
let disposed = false;

function onFileChange(uploadFile: { raw?: File }): void {
  const raw = uploadFile.raw;
  if (!raw) return;
  rows.value.push({
    uid: nextUid++,
    file: raw,
    // 标题留空 = 用后端默认标题（文件名去扩展名）；输入框里用 placeholder 展示该默认值。
    // 这样「没有编辑过的行」提交 `{}`，不会把每个文件的默认标题都显式发一遍。
    title: "",
    tagsInput: "",
    description: "",
  });
  attempted.value = false;
  errorInfo.value = null;
}

function removeRow(uid: number): void {
  rows.value = rows.value.filter((row) => row.uid !== uid);
  errorInfo.value = null;
}

/** 开关关闭时整段不发送，用户填过的值仍留在表单里，重新打开可继续编辑。 */
function metadataRows(): KbMetadataRow[] {
  if (!metaEnabled.value) return [];
  return rows.value.map((row) => ({
    title: row.title,
    tagsInput: row.tagsInput,
    description: row.description,
  }));
}

/** 行内红字：文件预检优先，其次元数据边界；后端 422 才是最终判据。 */
function rowIssue(row: UploadRow): string | null {
  const fileError = filePreflightError({ name: row.file.name, size: row.file.size });
  if (fileError) return fileError;
  if (!metaEnabled.value) return null;
  const metaError = metadataError([
    { title: row.title, tagsInput: row.tagsInput, description: row.description },
  ]);
  return metaError ? metaError.message : null;
}

function rowClass({ row }: { row: UploadRow }): string {
  return attempted.value && rowIssue(row) !== null ? "invalid-row" : "";
}

const signature = computed(() =>
  uploadSignature(
    rows.value.map((row) => ({
      name: row.file.name,
      size: row.file.size,
      lastModified: row.file.lastModified,
    })),
    buildMetadata(metadataRows()),
  ),
);

function newKey(): string {
  return crypto.randomUUID();
}

async function submit(): Promise<void> {
  attempted.value = true;
  const files = rows.value.map((row) => row.file);
  const batchError = batchPreflightError(files);
  if (batchError !== null) {
    ElMessage.warning(batchError);
    return;
  }
  const metaError = metadataError(metadataRows());
  if (metaError !== null) {
    ElMessage.warning(`第 ${metaError.index + 1} 个文件：${metaError.message}`);
    return;
  }

  const metadata = buildMetadata(metadataRows());
  const currentSignature = signature.value;
  attempt.value = uploadAttemptFor(attempt.value, currentSignature, newKey);
  uploading.value = true;
  errorInfo.value = null;
  try {
    const outcome = await kbApi.upload({
      files,
      idempotencyKey: attempt.value.key,
      metadata,
    });
    if (disposed) return;
    result.value = outcome;
    rows.value = [];
    attempted.value = false;
    attempt.value = null;
    startPolling();
    if (outcome.replayed) {
      ElMessage.warning("该提交此前已上传（replayed=true），本次未创建新文档");
    } else {
      ElMessage.success(`已上传 ${outcome.documents.length} 个文件`);
    }
  } catch (error) {
    // 403 由全局回调统一提示（error-toast 的同一口径），这里不叠第二条
    if (error instanceof ApiError && error.status === 403) return;
    const info = kbErrorInfo(error, "上传失败，请稍后重试");
    errorInfo.value = info;
    if (info.code === "IDEMPOTENCY_KEY_REUSED") {
      attempt.value = uploadAttemptFor(attempt.value, currentSignature, newKey, true);
      ElMessage.error("该提交编号已被另一批文件使用，已自动换新编号，请重试");
    } else if (info.files.length > 0) {
      ElMessage.error("整批未保存，请按逐文件错误修正后重试");
    } else {
      ElMessage.error(info.message);
    }
  } finally {
    uploading.value = false;
  }
}

// ---- 上传结果页的扫描轮询（简报 D4）：仅 view 权限、最多 15 个 pending、5 秒一次、5 分钟截止 ----
let pollTimer: ReturnType<typeof setTimeout> | null = null;
let pollStartedAt = 0;
let pollGeneration = 0;
let pollInFlight = false;
const failedPollIds = new Set<string>();
const pollStopReason = ref<"" | "timeout" | "forbidden">("");

function stopPolling(reason: "" | "timeout" | "forbidden" = ""): void {
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
  pollGeneration += 1;
  if (reason) pollStopReason.value = reason;
}

/** 一轮结束后才排下一轮（不用 setInterval），慢接口下不会叠出并发请求。 */
function scheduleNextPoll(generation: number): void {
  if (disposed || generation !== pollGeneration) return;
  pollTimer = setTimeout(() => {
    pollTimer = null;
    void pollTick(generation);
  }, SCAN_POLL_INTERVAL_MS);
}

function startPolling(): void {
  stopPolling();
  failedPollIds.clear();
  pollStopReason.value = "";
  const current = result.value;
  if (current === null || !hasView.value) return;
  if (pendingScanDocumentIds(current.documents).length === 0) return;
  pollStartedAt = Date.now();
  scheduleNextPoll(pollGeneration);
}

async function pollTick(generation: number): Promise<void> {
  if (disposed || generation !== pollGeneration) return;
  const current = result.value;
  if (current === null) {
    stopPolling();
    return;
  }
  const pending = pendingScanDocumentIds(current.documents).filter((id) => !failedPollIds.has(id));
  const visible = document.visibilityState === "visible";
  const elapsed = Date.now() - pollStartedAt;
  if (
    !shouldPollScan({
      hasPending: pending.length > 0,
      hasView: hasView.value,
      visible,
      elapsedMs: elapsed,
    })
  ) {
    if (pending.length === 0) stopPolling();
    else if (!visible)
      scheduleNextPoll(generation); // 隐藏期间不发请求，5 分钟窗口照常计时
    else if (elapsed >= SCAN_POLL_WINDOW_MS) stopPolling("timeout");
    else stopPolling("forbidden");
    return;
  }
  if (pollInFlight) {
    scheduleNextPoll(generation);
    return;
  }
  pollInFlight = true;
  try {
    const updates = new Map<string, KbDocumentView>();
    let denied = false;
    await Promise.all(
      pending.map(async (id) => {
        try {
          updates.set(id, await kbApi.getDocument(id));
        } catch (error) {
          if (error instanceof ApiError && error.status === 403) {
            denied = true;
            return;
          }
          // 404 = 文档已不可见：记下 id 不再重试（保留结果行），单次失败不打断其余文档
          failedPollIds.add(id);
        }
      }),
    );
    if (denied && generation === pollGeneration) {
      stopPolling("forbidden");
      return;
    }
    if (disposed || generation !== pollGeneration) return;
    const latest = result.value;
    if (latest !== null) {
      result.value = {
        ...latest,
        documents: latest.documents.map((item) => updates.get(item.id) ?? item),
      };
    }
    if (pendingScanDocumentIds(result.value?.documents ?? []).length === 0) {
      stopPolling();
      return;
    }
  } finally {
    pollInFlight = false;
  }
  scheduleNextPoll(generation);
}

const pollNote = computed(() => {
  if (result.value === null) return "";
  if (!hasView.value) {
    return "扫描在后台进行；你没有知识库查看权限，扫描结论由有查看权限的管理员确认。";
  }
  if (pendingScanDocumentIds(result.value.documents).length === 0) return "扫描已全部结束。";
  if (pollStopReason.value === "timeout") {
    return "自动刷新已停止（超过 5 分钟）；请到「知识库」列表手动刷新查看最新扫描结论。";
  }
  if (pollStopReason.value === "forbidden") {
    return "自动刷新已停止（无查看权限或被服务端拒绝）；请联系有查看权限的管理员确认。";
  }
  return "扫描状态每 5 秒自动刷新，最多持续 5 分钟；也可以到「知识库」列表手动刷新。";
});

async function copyBatchId(): Promise<void> {
  if (result.value === null) return;
  const text = result.value.batch_id;
  try {
    if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
    await navigator.clipboard.writeText(text);
    ElMessage.success("批次号已复制");
  } catch {
    ElMessage.warning("复制失败，请手动选择批次号");
  }
}

function versionOf(document: KbDocumentView) {
  return document.current_version;
}

onBeforeUnmount(() => {
  disposed = true;
  stopPolling();
});
</script>

<template>
  <el-card>
    <template #header>上传知识库文件</template>
    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="一次最多 15 个文件、单个不超过 50MB、合计不超过 300MB；支持 PDF/MD/TXT/DOCX/PPT(X)。文件先扫描后审批，扫描通过前不会被解析。"
    />

    <div class="toolbar">
      <el-upload
        :auto-upload="false"
        :show-file-list="false"
        multiple
        accept=".pdf,.md,.txt,.docx,.pptx,.ppt"
        :on-change="onFileChange"
      >
        <el-button data-testid="kb-pick-files" :disabled="uploading">选择文件</el-button>
      </el-upload>
      <span class="hint">已选 {{ rows.length }} 个文件</span>
      <el-switch
        v-model="metaEnabled"
        active-text="填写元数据（可选）"
        :disabled="uploading"
        data-testid="kb-meta-toggle"
      />
      <el-button
        type="primary"
        data-testid="kb-submit"
        :disabled="rows.length === 0"
        :loading="uploading"
        @click="submit"
        >上传</el-button
      >
    </div>

    <el-table
      v-if="rows.length > 0"
      :data="rows"
      row-key="uid"
      :row-class-name="rowClass"
      data-testid="kb-upload-table"
    >
      <el-table-column label="文件" min-width="240">
        <template #default="{ row }">
          <div class="file-name">{{ row.file.name }}</div>
          <div class="file-size">{{ formatBytes(row.file.size) }}</div>
          <div v-if="rowIssue(row as UploadRow)" class="row-error" data-testid="kb-row-error">
            {{ rowIssue(row as UploadRow) }}
          </div>
        </template>
      </el-table-column>
      <el-table-column v-if="metaEnabled" label="标题" min-width="180">
        <template #default="{ row }">
          <el-input
            v-model="row.title"
            :disabled="uploading"
            :placeholder="`默认：${defaultTitle((row as UploadRow).file.name)}`"
          />
        </template>
      </el-table-column>
      <el-table-column v-if="metaEnabled" label="标签" min-width="180">
        <template #default="{ row }">
          <el-input
            v-model="row.tagsInput"
            :disabled="uploading"
            placeholder="逗号分隔，≤10 个，每个 ≤32 字"
          />
        </template>
      </el-table-column>
      <el-table-column v-if="metaEnabled" label="描述" min-width="220">
        <template #default="{ row }">
          <el-input
            v-model="row.description"
            type="textarea"
            :rows="1"
            :disabled="uploading"
            placeholder="可选"
          />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="80">
        <template #default="{ row }">
          <el-button link type="danger" :disabled="uploading" @click="removeRow(row.uid)"
            >移除</el-button
          >
        </template>
      </el-table-column>
    </el-table>

    <el-alert
      v-if="errorInfo"
      class="error-block"
      data-testid="kb-upload-error"
      type="error"
      :closable="false"
      show-icon
      :title="errorInfo.message"
    />
    <el-table
      v-if="errorInfo && errorInfo.files.length > 0"
      :data="errorInfo.files"
      class="error-block"
      data-testid="kb-file-errors"
    >
      <el-table-column label="序号" width="70">
        <template #default="{ row }">{{ row.index + 1 }}</template>
      </el-table-column>
      <el-table-column prop="filename" label="文件名" min-width="180" />
      <el-table-column prop="code" label="错误码" width="200" />
      <el-table-column prop="message" label="原因" min-width="240" />
    </el-table>
  </el-card>

  <el-card v-if="result !== null" class="result-card" data-testid="kb-upload-result">
    <template #header>
      <div class="result-head">
        <span>上传结果</span>
        <span class="batch" data-testid="kb-batch-id">批次：{{ result.batch_id }}</span>
        <el-button link type="primary" @click="copyBatchId">复制</el-button>
      </div>
    </template>

    <el-alert
      v-if="result.replayed"
      type="warning"
      :closable="false"
      show-icon
      data-testid="kb-replayed"
      title="该提交此前已上传（replayed=true）：未创建新文档，下面是原批次的文档。"
    />
    <p class="hint">{{ pollNote }}</p>

    <el-table :data="result.documents" data-testid="kb-result-table">
      <el-table-column prop="title" label="标题" min-width="160" />
      <el-table-column label="文件" min-width="180">
        <template #default="{ row }">
          <div>{{ versionOf(row as KbDocumentView)?.filename ?? "—" }}</div>
          <div class="file-size">
            {{
              versionOf(row as KbDocumentView)
                ? formatBytes(versionOf(row as KbDocumentView)!.size_bytes)
                : "—"
            }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="文档状态" width="140">
        <template #default="{ row }">
          <el-tag :type="documentStatusMeta(row.status).tone" size="small">
            {{ documentStatusMeta(row.status).label }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="扫描" min-width="240">
        <template #default="{ row }">
          <KbScanTag :scan="versionOf(row as KbDocumentView)?.scan ?? null" />
          <div v-if="versionOf(row as KbDocumentView)?.scan.error_code" class="scan-extra">
            错误码：{{ versionOf(row as KbDocumentView)!.scan.error_code }}
          </div>
          <div v-if="versionOf(row as KbDocumentView)?.scan.signature" class="scan-extra">
            签名：{{ versionOf(row as KbDocumentView)!.scan.signature }}
          </div>
        </template>
      </el-table-column>
    </el-table>
  </el-card>
</template>

<style scoped>
.toolbar {
  margin: 16px 0;
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.hint {
  color: var(--el-text-color-secondary);
}
.file-name {
  font-weight: 500;
}
.file-size {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.row-error {
  color: var(--el-color-danger);
  font-size: 12px;
}
:deep(.invalid-row) td {
  background: var(--el-color-danger-light-9);
}
.error-block {
  margin-top: 12px;
}
.result-card {
  margin-top: 16px;
}
.result-head {
  display: flex;
  align-items: center;
  gap: 12px;
}
.batch {
  font-family: monospace;
}
.scan-extra {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
