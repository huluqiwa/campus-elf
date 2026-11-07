<script setup lang="ts">
/**
 * 知识库文档详情抽屉（M3 KB-01B 简报 D5；KB-02B 增审批入口）：
 * 打开即读详情 + 第一页版本；版本列表按 `next_cursor` 追加；文档不可见（404）时提示并让列表刷新。
 *
 * KB-02B 入口（简报 D2-A）：按钮按「文档状态 + 权限码」渲染，**不做服务端状态预判**——
 * 同版本已有待审批单/活跃构建时按钮仍在，点了吃后端 409（服务端是唯一门禁）；
 * 提交/创建成功后在本抽屉会话内记忆并禁用按钮（重开抽屉会复位，届时 409 兜底）；
 * REJECTED 的驳回理由不直显（前端没有按 version 查审批单的端点，TD-291），给审批中心指引。
 */
import { ApiError, toUserMessage } from "@campus-elf/api-client";
import type { KbDocumentView, KbVersionView, KbVersionApprovalView } from "@campus-elf/api-client";
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, ref, watch } from "vue";

import { approvalsApi, kbApi, testApi } from "../api";
import { reportError } from "../error-toast";
import {
  KB_BUILD_CODE,
  KB_UPLOAD_CODE,
  REJECT_REASON_GUIDANCE,
  kbEntryConflictMessage,
} from "../approval-logic";
import { KB_PAGE_SIZE, documentStatusMeta, formatBytes } from "../kb-logic";
import { useAuthStore } from "../stores/auth";
import {
  caseSummaries,
  passRateLabel,
  testRunStatusMeta,
  testSetFileError,
  testSectionGate,
  type KbTestRunDetail,
} from "../test-logic";
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

const auth = useAuthStore();
const permissions = computed(() => auth.user?.permissions ?? []);

const approvals = ref<KbVersionApprovalView[]>([]);
const testRun = ref<KbTestRunDetail | null>(null);
const testSetName = ref("");
const testFile = ref<File | null>(null);
const testFileError = ref<string | null>(null);
const uploadingTestSet = ref(false);
const startingTestRun = ref(false);

const detail = ref<KbDocumentView | null>(null);
const loading = ref(false);
const versions = ref<KbVersionView[]>([]);
const versionsLoading = ref(false);
const nextCursor = ref<string | null>(null);
let goneNotified = false;
/** 每次打开/切换文档自增；迟到的响应与 404 一律作废，避免把 A 的数据/结论写进 B。 */
let loadGeneration = 0;

/** 本抽屉会话内已成功提交/已建任务的版本（重开抽屉复位；真正的状态以审批中心/后端为准）。 */
const submittedInSession = ref<Set<string>>(new Set());
const buildCreatedInSession = ref<Set<string>>(new Set());
const submitting = ref(false);
const building = ref(false);

const testGate = computed(() =>
  detail.value
    ? testSectionGate(detail.value.status, permissions.value)
    : { visible: false, hint: null },
);
const latestApproval = computed(() => approvals.value[0] ?? null);

const canSubmitEntry = computed(() => {
  const version = detail.value?.current_version;
  return (
    permissions.value.includes(KB_UPLOAD_CODE) &&
    detail.value?.status === "PENDING_APPROVAL" &&
    version !== null &&
    version !== undefined &&
    !submittedInSession.value.has(version.id)
  );
});

const canBuildEntry = computed(() => {
  const version = detail.value?.current_version;
  return (
    permissions.value.includes(KB_BUILD_CODE) &&
    detail.value?.status === "APPROVED" &&
    version !== null &&
    version !== undefined &&
    !buildCreatedInSession.value.has(version.id)
  );
});

async function submitApproval(): Promise<void> {
  const version = detail.value?.current_version;
  if (!version) return;
  submitting.value = true;
  try {
    await approvalsApi.submitKbVersion(version.id);
    submittedInSession.value = new Set([...submittedInSession.value, version.id]);
    ElMessage.success("已提交审批，可在「审批中心 → 已办」查看进度或撤销");
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      // 服务端 message 优先（KB_NOT_SUBMITTABLE 覆盖多种不可提交状态，固定译文会在
      // 「并发被驳回后提交」这类窗口误导）。kbEntryConflictMessage 是可读化兜底：
      // detail.message 缺失时 ApiError.message 会是「请求失败（409）」，实际先于映射到达用户
      // （复审 Minor 4 的如实注记；后端与 mock 恒带 message，现网无行为差异）。
      ElMessage.warning(toUserMessage(error, kbEntryConflictMessage(error, "提交审批失败")));
      return;
    }
    reportError(error, "提交审批失败");
  } finally {
    submitting.value = false;
  }
}

async function createBuild(): Promise<void> {
  const version = detail.value?.current_version;
  if (!version) return;
  const confirmed = await ElMessageBox.confirm(
    "确认创建构建任务？同一版本同时只能有一个进行中的构建，创建后不可在本页取消。",
    "创建构建确认",
    { type: "warning" },
  ).catch(() => false);
  if (!confirmed) return;
  building.value = true;
  try {
    const job = await approvalsApi.createKbBuild(version.id);
    buildCreatedInSession.value = new Set([...buildCreatedInSession.value, version.id]);
    ElMessage.success(`构建任务已创建（${job.status}），解析与进度由 KB-03 提供`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      ElMessage.warning(toUserMessage(error, kbEntryConflictMessage(error, "创建构建失败")));
      return;
    }
    reportError(error, "创建构建失败");
  } finally {
    building.value = false;
  }
}

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
  submittedInSession.value = new Set();
  buildCreatedInSession.value = new Set();
  testRun.value = null;
  testSetName.value = "";
  testFile.value = null;
  testFileError.value = null;
  if (!open || id === null) return;
  // TD-291：加载审批单（最近一单直显状态与驳回理由）
  void loadApprovals(id, generation);
  loadLatestTestRun();
  // 只在打开时重置：关闭抽屉不能被当成新一轮，否则并发的两个 404 会二次提示
  goneNotified = false;
  void loadDetail(id, generation);
  void loadVersions(id, generation);
});

async function loadApprovals(docId: string, generation: number): Promise<void> {
  try {
    const items = await testApi.listVersionApprovals(docId);
    if (generation !== loadGeneration) return;
    approvals.value = items;
  } catch {
    if (generation === loadGeneration) approvals.value = [];
  }
}

function loadLatestTestRun(): void {
  // KB-04A 没有按 version 查最新 run 的端点——抽屉暂不预载（TD-296 前端进度随后续）
}

function onTestFileChange(event: Event): void {
  const input = event.target as HTMLInputElement;
  testFile.value = input.files?.[0] ?? null;
  testFileError.value = testFile.value ? testSetFileError(testFile.value) : null;
}

async function uploadAndRun(): Promise<void> {
  const version = detail.value?.current_version;
  if (!version || !testFile.value || testFileError.value) return;
  const name = testSetName.value.trim() || testFile.value.name.replace(/\.[^.]+$/, "");
  uploadingTestSet.value = true;
  try {
    const testSet = await testApi.uploadTestSet(version.id, {
      name,
      file: testFile.value,
    });
    uploadingTestSet.value = false;
    startingTestRun.value = true;
    const run = await testApi.startTestRun(version.id, testSet.test_set_id);
    testRun.value = {
      ...run,
      metrics_json: null,
      started_at: new Date().toISOString(),
      finished_at: null,
    };
    ElMessage.success(`测试集已上传（${testSet.case_count} 条），测试运行已发起`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      ElMessage.warning(toUserMessage(error, "该版本已有进行中的测试运行"));
    } else {
      reportError(error, "测试集上传或测试发起失败");
    }
  } finally {
    uploadingTestSet.value = false;
    startingTestRun.value = false;
  }
}

async function pollTestRun(): Promise<void> {
  const current = testRun.value;
  if (!current) return;
  try {
    const fresh = await testApi.getTestRun(current.run_id);
    testRun.value = fresh;
  } catch {
    // 轮询失败不中断
  }
}

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

        <div
          v-if="detail.status === 'REJECTED'"
          class="reject-guidance"
          data-testid="kb-reject-guidance"
        >
          <el-alert
            v-if="latestApproval && latestApproval.reject_reason"
            type="error"
            :closable="false"
            show-icon
            :title="latestApproval.reject_reason"
            data-testid="kb-reject-reason-display"
          />
          <el-alert
            v-else
            type="info"
            :closable="false"
            show-icon
            :title="REJECT_REASON_GUIDANCE"
          />
        </div>

        <div
          v-if="detail.status === 'PENDING_APPROVAL' || detail.status === 'APPROVED'"
          class="entry-row"
        >
          <el-button
            v-if="canSubmitEntry"
            type="primary"
            :loading="submitting"
            data-testid="kb-submit-approval"
            @click="submitApproval"
          >
            提交审批
          </el-button>
          <el-button
            v-if="
              detail.status === 'PENDING_APPROVAL' &&
              !canSubmitEntry &&
              detail.current_version !== null &&
              permissions.includes(KB_UPLOAD_CODE)
            "
            disabled
            data-testid="kb-submit-approval-done"
          >
            已提交审批
          </el-button>
          <el-button
            v-if="canBuildEntry"
            type="primary"
            plain
            :loading="building"
            data-testid="kb-create-build"
            @click="createBuild"
          >
            创建构建
          </el-button>
          <el-button
            v-if="
              detail.status === 'APPROVED' &&
              !canBuildEntry &&
              detail.current_version !== null &&
              permissions.includes(KB_BUILD_CODE)
            "
            disabled
            data-testid="kb-create-build-done"
          >
            已创建构建任务
          </el-button>
        </div>

        <section v-if="testGate.visible" class="test-section" data-testid="kb-test-section">
          <h3>测试</h3>
          <p v-if="testGate.hint" class="muted" data-testid="kb-test-hint">{{ testGate.hint }}</p>
          <template v-if="testGate.visible && !testGate.hint">
            <p v-if="testRun" class="muted" data-testid="kb-test-run-status">
              {{ testRunStatusMeta(testRun.status).label }}
              <template v-if="testRun.finished_at">
                · 通过率 {{ passRateLabel(testRun.metrics_json) }} ·
                {{ caseSummaries(testRun.metrics_json).length }} 条
              </template>
            </p>
            <div v-if="!testRun" class="test-upload-row">
              <el-input
                v-model="testSetName"
                placeholder="测试集名称（可选）"
                size="small"
                class="test-name-input"
                data-testid="kb-test-set-name"
              />
              <input
                type="file"
                accept=".csv,.xlsx,.json"
                data-testid="kb-test-set-file"
                @change="onTestFileChange"
              />
            </div>
            <p v-if="testFileError" class="test-error" data-testid="kb-test-file-error">
              {{ testFileError }}
            </p>
            <el-button
              v-if="!testRun"
              type="primary"
              size="small"
              :disabled="!testFile || !!testFileError"
              :loading="uploadingTestSet || startingTestRun"
              data-testid="kb-test-start"
              @click="uploadAndRun"
            >
              上传并运行测试
            </el-button>
            <el-button
              v-if="testRun && testRun.status === 'TESTING'"
              size="small"
              data-testid="kb-test-poll"
              @click="pollTestRun"
            >
              刷新结果
            </el-button>
          </template>
        </section>

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
.reject-guidance {
  margin-bottom: 4px;
}
.entry-row {
  display: flex;
  gap: 8px;
}
.load-more {
  margin-top: 8px;
}
.test-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}
.test-section h3 {
  margin: 0;
  font-size: 14px;
}
.test-upload-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.test-name-input {
  width: 180px;
}
.test-error {
  color: var(--el-color-danger);
  font-size: 12px;
  margin: 0;
}
.reject-guidance {
  margin-bottom: 4px;
}
</style>
