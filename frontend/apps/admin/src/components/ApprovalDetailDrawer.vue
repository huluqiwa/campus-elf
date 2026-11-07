<script setup lang="ts">
/**
 * 审批单详情抽屉（M3 KB-02B 简报 D1/D3）。
 *
 * - 打开即取详情（`GET /school/approvals/{id}`），generation 守卫防切换串数据（KB 抽屉同款）；
 * - 通过/驳回/撤销的门禁见 `decideGate`/`cancelGate`——后端是唯一门禁，这里只把
 *   必然失败的按钮从界面上拿掉（自批不做前端预判：上传者归属前端没有数据，403 交给后端）；
 * - 驳回理由前端先拦（strip 非空 + ≤500 码点），通过是一次确认（动作终态不可撤）；
 * - 409 `APPROVAL_ALREADY_DECIDED` 说明别人已处理：提示后重读详情，不让用户对着旧状态操作。
 */
import type { ApprovalDetailView } from "@campus-elf/api-client";
import { ApiError, toUserMessage } from "@campus-elf/api-client";
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, ref, watch } from "vue";

import { approvalsApi } from "../api";
import { reportError } from "../error-toast";
import {
  actionLabel,
  approvalReasonError,
  approvalStatusMeta,
  approvalTypeLabel,
  cancelGate,
  decideGate,
} from "../approval-logic";

const props = defineProps<{
  modelValue: boolean;
  requestId: string | null;
  selfUserId: string | null;
  permissions: string[];
}>();

const emit = defineEmits<{
  "update:modelValue": [visible: boolean];
  /** 任一写动作成功或单据不可见：父页面重读当前 tab 当前页。 */
  changed: [];
}>();

const detail = ref<ApprovalDetailView | null>(null);
const loading = ref(false);
/** 每次打开自增；迟到的响应一律作废（KbDocumentDrawer 同款）。 */
let loadGeneration = 0;

const rejectReason = ref("");
const deciding = ref(false);
const cancelling = ref(false);

const request = computed(() => detail.value?.request ?? null);
const gates = computed(() => {
  const current = request.value;
  return {
    approve: current ? decideGate(current, props.permissions) : { allowed: false, hint: null },
    cancel: current
      ? cancelGate(current, props.selfUserId, props.permissions)
      : { allowed: false, hint: null },
  };
});
const statusMeta = computed(() =>
  request.value ? approvalStatusMeta(request.value.status) : null,
);

/** 提交时快照里的已知字段（`_version_snapshot`）；快照是提交时的定格，缺失字段显示 —。 */
const snapshot = computed(() => {
  const payload = request.value?.payload;
  if (!payload) return null;
  const text = (key: string): string | null => {
    const value = payload[key];
    return typeof value === "string" && value ? value : null;
  };
  const versionNo = payload["version_no"];
  const tags = payload["tags"];
  return {
    documentTitle: text("document_title"),
    versionNo: typeof versionNo === "number" ? versionNo : null,
    filename: text("source_filename"),
    sourceHash: text("source_hash"),
    mime: text("mime"),
    scanEngine: text("scan_engine"),
    description: text("description"),
    tags: Array.isArray(tags) ? tags.filter((tag): tag is string => typeof tag === "string") : [],
  };
});

async function loadDetail(requestId: string, generation: number): Promise<void> {
  loading.value = true;
  try {
    const fresh = await approvalsApi.getDetail(requestId);
    if (generation !== loadGeneration) return;
    detail.value = fresh;
    rejectReason.value = "";
  } catch (error) {
    if (generation !== loadGeneration) return;
    detail.value = null;
    if (error instanceof ApiError && error.status === 404) {
      ElMessage.warning("审批单不存在或已不可见，列表已刷新");
      emit("changed");
      emit("update:modelValue", false);
      return;
    }
    reportError(error, "审批单详情加载失败");
  } finally {
    if (generation === loadGeneration) loading.value = false;
  }
}

watch(
  [() => props.modelValue, () => props.requestId],
  ([open, id]) => {
    loadGeneration += 1;
    const generation = loadGeneration;
    detail.value = null;
    rejectReason.value = "";
    loading.value = false;
    if (!open || id === null) return;
    void loadDetail(id, generation);
  },
  { immediate: true },
);

/** 终态后重读详情：状态、步骤与动作留痕都以服务端为准。 */
async function reload(): Promise<void> {
  const id = props.requestId;
  if (id === null) return;
  await loadDetail(id, loadGeneration);
}

async function onApprove(): Promise<void> {
  const current = request.value;
  if (!current) return;
  const confirmed = await ElMessageBox.confirm(
    `确认通过「${current.title}」？通过后该版本即可创建构建任务，且不能再撤销本审批。`,
    "通过确认",
    { type: "warning" },
  ).catch(() => false);
  if (!confirmed) return;
  deciding.value = true;
  try {
    await approvalsApi.approve(current.id, {});
    ElMessage.success("已通过该审批单");
    emit("changed");
    await reload();
  } catch (error) {
    await handleActionError(error, "通过失败");
  } finally {
    deciding.value = false;
  }
}

async function onReject(): Promise<void> {
  const current = request.value;
  if (!current) return;
  const reasonError = approvalReasonError(rejectReason.value);
  if (reasonError) {
    ElMessage.warning(reasonError);
    return;
  }
  deciding.value = true;
  try {
    await approvalsApi.reject(current.id, { reason: rejectReason.value.trim() });
    ElMessage.success("已驳回该审批单，提交人会在已办列表看到理由");
    emit("changed");
    await reload();
  } catch (error) {
    await handleActionError(error, "驳回失败");
  } finally {
    deciding.value = false;
  }
}

async function onCancel(): Promise<void> {
  const current = request.value;
  if (!current) return;
  const confirmed = await ElMessageBox.confirm(
    `确认撤销「${current.title}」？撤销后可重新提交审批。`,
    "撤销确认",
    { type: "warning" },
  ).catch(() => false);
  if (!confirmed) return;
  cancelling.value = true;
  try {
    await approvalsApi.cancel(current.id);
    ElMessage.success("已撤销该审批单，版本回到待审批");
    emit("changed");
    await reload();
  } catch (error) {
    await handleActionError(error, "撤销失败");
  } finally {
    cancelling.value = false;
  }
}

/** 409 = 别人已处理（状态已变）：提示服务端文案并重读详情；403 全局已提示（TD-258），这里不再叠。 */
async function handleActionError(error: unknown, fallback: string): Promise<void> {
  if (error instanceof ApiError && error.status === 409) {
    ElMessage.warning(toUserMessage(error, "该审批单已被他人处理，已刷新为最新状态"));
    // 列表也要重同步：单据已终态化，留在待办/已办的旧行会误导（与成功路径同一 emit）
    emit("changed");
    await reload();
    return;
  }
  reportError(error, fallback);
}

function actorLabel(actorId: string | null): string {
  if (!actorId) return "—";
  return `ID 尾号 ${actorId.slice(-6)}`;
}

function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function scanEngineLabel(engine: string | null): string {
  if (!engine) return "—";
  if (engine === "clamav") return "ClamAV";
  if (engine === "dev-skip") return "开发环境跳过扫描";
  if (engine === "dev-degraded") return "扫描不可用，开发环境降级";
  return engine;
}

function stepStatusMeta(status: string) {
  return approvalStatusMeta(status);
}
</script>

<template>
  <el-drawer
    :model-value="modelValue"
    size="560px"
    data-testid="approval-drawer"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <template #header>
      <div class="drawer-head">
        <span class="title">{{ request?.title ?? "审批单详情" }}</span>
        <el-tag
          v-if="statusMeta"
          :type="statusMeta.tone"
          size="small"
          data-testid="approval-status"
        >
          {{ statusMeta.label }}
        </el-tag>
      </div>
    </template>

    <div v-loading="loading" class="drawer-body">
      <template v-if="request">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="类型">
            {{ approvalTypeLabel(request.type_code) }}
          </el-descriptions-item>
          <el-descriptions-item label="提交人">
            {{ actorLabel(request.submitter_id) }}
          </el-descriptions-item>
          <el-descriptions-item label="提交时间">
            {{ formatDateTime(request.created_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="完成时间">
            {{ formatDateTime(request.completed_at) }}
          </el-descriptions-item>
          <el-descriptions-item v-if="request.reject_reason" label="驳回理由">
            <span class="reject-reason" data-testid="approval-reject-reason">{{
              request.reject_reason
            }}</span>
          </el-descriptions-item>
        </el-descriptions>

        <section class="section" data-testid="approval-snapshot">
          <h3>提交时版本快照</h3>
          <template v-if="snapshot">
            <el-descriptions :column="1" border size="small">
              <el-descriptions-item label="文档标题">
                {{ snapshot.documentTitle ?? "—" }}
              </el-descriptions-item>
              <el-descriptions-item label="版本">
                {{ snapshot.versionNo !== null ? `v${snapshot.versionNo}` : "—" }}
              </el-descriptions-item>
              <el-descriptions-item label="文件名">
                {{ snapshot.filename ?? "—" }}
              </el-descriptions-item>
              <el-descriptions-item label="标签">
                <template v-if="snapshot.tags.length > 0">
                  <el-tag v-for="tag in snapshot.tags" :key="tag" size="small" class="tag">
                    {{ tag }}
                  </el-tag>
                </template>
                <span v-else>—</span>
              </el-descriptions-item>
              <el-descriptions-item label="描述">{{
                snapshot.description ?? "—"
              }}</el-descriptions-item>
              <el-descriptions-item label="扫描引擎">
                {{ scanEngineLabel(snapshot.scanEngine) }}
              </el-descriptions-item>
              <el-descriptions-item label="文件哈希">
                <span class="mono">{{ snapshot.sourceHash ?? "—" }}</span>
              </el-descriptions-item>
            </el-descriptions>
          </template>
          <p v-else class="muted">该审批单没有版本快照。</p>
        </section>

        <section class="section" data-testid="approval-steps">
          <h3>审批步骤</h3>
          <p v-for="step in detail?.steps ?? []" :key="step.id" class="step-row">
            第 {{ step.step_no }} 步 · {{ stepStatusMeta(step.status).label }}
            <template v-if="step.acted_by"> · {{ actorLabel(step.acted_by) }}</template>
            <template v-if="step.acted_at"> · {{ formatDateTime(step.acted_at) }}</template>
            <template v-if="step.comment"> · 理由：{{ step.comment }}</template>
          </p>
        </section>

        <section class="section" data-testid="approval-actions">
          <h3>动作留痕</h3>
          <p v-for="action in detail?.actions ?? []" :key="action.id" class="step-row">
            {{ actionLabel(action.action) }} · {{ actorLabel(action.actor_id) }} ·
            {{ formatDateTime(action.created_at) }}
            <template v-if="action.reason"> · 理由：{{ action.reason }}</template>
          </p>
        </section>

        <section
          v-if="
            gates.approve.allowed || gates.cancel.allowed || gates.approve.hint || gates.cancel.hint
          "
          class="section"
          data-testid="approval-act"
        >
          <h3>处理</h3>
          <template v-if="gates.approve.allowed">
            <el-input
              v-model="rejectReason"
              type="textarea"
              :rows="2"
              placeholder="驳回理由（必填，最长 500 个字；写入审批单与审计）"
              data-testid="approval-reject-reason-input"
            />
            <div class="act-row">
              <el-button
                type="primary"
                :loading="deciding"
                data-testid="approval-approve"
                @click="onApprove"
              >
                通过
              </el-button>
              <el-button
                type="danger"
                plain
                :loading="deciding"
                data-testid="approval-reject"
                @click="onReject"
              >
                驳回
              </el-button>
            </div>
          </template>
          <p v-else-if="gates.approve.hint" class="gate-hint" data-testid="approval-decide-hint">
            {{ gates.approve.hint }}
          </p>
          <template v-if="gates.cancel.allowed">
            <el-button plain :loading="cancelling" data-testid="approval-cancel" @click="onCancel">
              撤销我的提交
            </el-button>
          </template>
          <p v-else-if="gates.cancel.hint && !gates.approve.allowed" class="gate-hint">
            {{ gates.cancel.hint }}
          </p>
        </section>

        <div class="footer">
          <el-button data-testid="approval-drawer-close" @click="emit('update:modelValue', false)">
            关闭
          </el-button>
        </div>
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
.drawer-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.section {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}
.section h3 {
  margin: 0;
  font-size: 14px;
}
.step-row {
  margin: 0;
  font-size: 12px;
  line-height: 1.8;
}
.act-row {
  display: flex;
  gap: 8px;
}
.gate-hint,
.muted {
  margin: 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.reject-reason {
  color: var(--el-color-danger);
}
.tag {
  margin-right: 6px;
}
.mono {
  font-family: monospace;
  font-size: 12px;
  word-break: break-all;
}
.footer {
  display: flex;
  justify-content: flex-end;
}
</style>
