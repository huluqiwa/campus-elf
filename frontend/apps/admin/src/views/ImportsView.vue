<script setup lang="ts">
/** 批量导入（M2-UI1）：上传（≤10MB csv/xlsx）→ 1s 轮询进度 → 错误行 → 失败行重试。 */
import { toUserMessage } from "@campus-elf/api-client";
import type { ImportJobView, ImportRowErrorView } from "@campus-elf/api-client";
import { ElMessage } from "element-plus";
import { onBeforeUnmount, ref } from "vue";

import { importsApi } from "../api";
import { importFileError } from "../logic";

const importType = ref<"teachers" | "students">("teachers");
const conflict = ref<"error" | "skip">("error");
const pickedFile = ref<File | null>(null);
const uploading = ref(false);

const job = ref<ImportJobView | null>(null);
const errors = ref<ImportRowErrorView[]>([]);
let pollTimer: ReturnType<typeof setInterval> | null = null;
let pollFailureNoted = false;

function onFileChange(file: { raw?: File }): void {
  pickedFile.value = file.raw ?? null;
}

function validatePicked(): string | null {
  const file = pickedFile.value;
  if (file === null) return "请先选择文件";
  return importFileError({ name: file.name, size: file.size, type: file.type });
}

async function submit(): Promise<void> {
  const problem = validatePicked();
  if (problem !== null) {
    ElMessage.warning(problem);
    return;
  }
  uploading.value = true;
  try {
    job.value = await importsApi.upload({
      type: importType.value,
      conflict: conflict.value,
      file: pickedFile.value as File,
    });
    startPolling(job.value.id);
  } catch (error) {
    ElMessage.error(toUserMessage(error, "上传失败"));
  } finally {
    uploading.value = false;
  }
}

function startPolling(jobId: string): void {
  stopPolling();
  pollTimer = setInterval(() => {
    void refresh(jobId);
  }, 1000);
}

function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function refresh(jobId: string): Promise<void> {
  try {
    const current = await importsApi.get(jobId);
    job.value = current;
    if (
      current.status === "completed" ||
      current.status === "completed_with_errors" ||
      current.status === "failed"
    ) {
      stopPolling();
      if (current.status !== "completed") {
        await loadErrors(jobId);
      } else {
        errors.value = [];
      }
    }
  } catch {
    stopPolling();
    if (!pollFailureNoted) {
      pollFailureNoted = true;
      ElMessage.warning("进度查询中断，请刷新页面查看最新状态");
    }
  }
}

async function loadErrors(jobId: string): Promise<void> {
  try {
    const page = await importsApi.listErrors(jobId, { limit: 200 });
    errors.value = page.items;
  } catch (error) {
    ElMessage.error(toUserMessage(error, "错误行加载失败"));
  }
}

async function retry(): Promise<void> {
  if (job.value === null) return;
  try {
    // 后端返回派生引用（source/job），按新 job id 重新取进度并轮询
    const ref = await importsApi.retry(job.value.id);
    errors.value = [];
    job.value = await importsApi.get(ref.job_id);
    startPolling(ref.job_id);
    ElMessage.success("已派生重试任务");
  } catch (error) {
    ElMessage.error(toUserMessage(error, "重试失败"));
  }
}

const canRetry = (): boolean =>
  job.value !== null &&
  (job.value.status === "completed_with_errors" || job.value.status === "failed");

const progressPercent = (): number => {
  const current = job.value;
  if (current === null || current.total_rows === 0) return 0;
  const processed = current.success_rows + current.failed_rows + current.skipped_rows;
  return Math.round((processed / current.total_rows) * 100);
};

onBeforeUnmount(() => {
  stopPolling();
});
</script>

<template>
  <el-card>
    <template #header>上传人员数据（CSV/XLSX，≤10MB、≤5000 行）</template>
    <el-form inline>
      <el-form-item label="类型">
        <el-radio-group v-model="importType">
          <el-radio-button value="teachers">教师</el-radio-button>
          <el-radio-button value="students">学生</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="重复策略">
        <el-select v-model="conflict" style="width: 160px">
          <el-option value="error" label="报错（error）" />
          <el-option value="skip" label="跳过（skip）" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <el-upload
          :auto-upload="false"
          :show-file-list="false"
          :on-change="onFileChange"
          accept=".csv,.xlsx"
        >
          <el-button>{{ pickedFile === null ? "选择文件" : pickedFile.name }}</el-button>
        </el-upload>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" data-testid="import-submit" :loading="uploading" @click="submit"
          >上传并开始导入</el-button
        >
      </el-form-item>
    </el-form>
  </el-card>

  <el-card v-if="job !== null" class="job-card">
    <template #header>
      导入任务 {{ job.id }}（{{ job.type === "teachers" ? "教师" : "学生" }}）
    </template>
    <el-progress
      :percentage="progressPercent()"
      :status="job.status === 'failed' ? 'exception' : undefined"
    />
    <el-descriptions :column="4" class="counters">
      <el-descriptions-item label="状态">
        <el-tag>{{ job.status }}</el-tag>
      </el-descriptions-item>
      <el-descriptions-item label="总数">{{ job.total_rows }}</el-descriptions-item>
      <el-descriptions-item label="成功">{{ job.success_rows }}</el-descriptions-item>
      <el-descriptions-item label="失败/跳过"
        >{{ job.failed_rows }} / {{ job.skipped_rows }}</el-descriptions-item
      >
    </el-descriptions>
    <p v-if="job.message" class="job-message">{{ job.message }}</p>
    <el-button v-if="canRetry()" type="warning" @click="retry">对失败行发起重试</el-button>
  </el-card>

  <el-card v-if="errors.length > 0" class="job-card">
    <template #header>错误行（{{ errors.length }}）</template>
    <el-table :data="errors" data-testid="import-errors">
      <el-table-column prop="row_no" label="行号" width="80" />
      <el-table-column prop="field" label="字段" width="140" />
      <el-table-column prop="error_code" label="错误码" width="200" />
      <el-table-column prop="message" label="原因" min-width="240" />
    </el-table>
  </el-card>
</template>

<style scoped>
.job-card {
  margin-top: 16px;
}
.counters {
  margin-top: 12px;
}
.job-message {
  color: var(--el-color-warning);
}
</style>
