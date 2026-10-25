<script setup lang="ts">
/**
 * 学年流转向导（M2-UI1 D6-A）：① 配置映射 → ② dry_run 预览逐班核对 → ③ 二次确认执行。
 * 幂等键在进入执行步时一次性生成（crypto.randomUUID），重复提交由后端 409 友好提示。
 */
import { toUserMessage } from "@campus-elf/api-client";
import type { GradeChange, RolloverAdvance } from "@campus-elf/api-client";
import { ElMessage } from "element-plus";
import { computed, ref } from "vue";

import { orgApi } from "../api";
import { rolloverMappingError } from "../logic";

const step = ref(0);

const advances = ref<RolloverAdvance[]>([{ from_grade: "", to_grade: "" }]);
const archiveGrades = ref<string[]>([]);
const newArchiveGrade = ref("");

const preview = ref<{ advanced: GradeChange[]; archived: GradeChange[] } | null>(null);
const previewLoading = ref(false);
const confirmed = ref(false);
// 幂等键在预览成功（进入确认步）时生成一次：跨「响应丢失后重试」保持不变，
// 后端凭 (tenant_id, key) 唯一性拦截重复执行——每次点击重新生成会击穿该防线
const idempotencyKey = ref("");

const executing = ref(false);
const executed = ref<{ advanced_count: number; archived_count: number } | null>(null);

const mappingError = computed(() => rolloverMappingError(advances.value, archiveGrades.value));
const mappingValid = computed(() => mappingError.value === null);

function addAdvance(): void {
  advances.value.push({ from_grade: "", to_grade: "" });
}

function removeAdvance(index: number): void {
  advances.value.splice(index, 1);
}

function addArchiveGrade(): void {
  const value = newArchiveGrade.value.trim();
  if (!value || archiveGrades.value.includes(value)) {
    newArchiveGrade.value = "";
    return;
  }
  archiveGrades.value.push(value);
  newArchiveGrade.value = "";
}

function removeArchiveGrade(grade: string): void {
  archiveGrades.value = archiveGrades.value.filter((g) => g !== grade);
}

async function goPreview(): Promise<void> {
  previewLoading.value = true;
  try {
    const result = await orgApi.rollover({
      advances: advances.value,
      archive_grades: archiveGrades.value,
      idempotency_key: "preview-only", // dry_run 不落批次表，键不参与幂等
      dry_run: true,
    });
    preview.value = { advanced: result.advanced, archived: result.archived };
    if (result.advanced.length === 0 && result.archived.length === 0) {
      ElMessage.warning("没有命中的班级，请检查年级标签是否与现有班级一致");
      return;
    }
    confirmed.value = false;
    idempotencyKey.value = crypto.randomUUID();
    step.value = 1;
  } catch (error) {
    ElMessage.error(toUserMessage(error, "预览失败，请检查映射"));
  } finally {
    previewLoading.value = false;
  }
}

async function execute(): Promise<void> {
  executing.value = true;
  try {
    const result = await orgApi.rollover({
      advances: advances.value,
      archive_grades: archiveGrades.value,
      idempotency_key: idempotencyKey.value,
      dry_run: false,
    });
    executed.value = {
      advanced_count: result.advanced_count,
      archived_count: result.archived_count,
    };
    step.value = 2;
  } catch (error) {
    ElMessage.error(toUserMessage(error, "执行失败（重复提交会提示已执行过）"));
  } finally {
    executing.value = false;
  }
}

function reset(): void {
  step.value = 0;
  advances.value = [{ from_grade: "", to_grade: "" }];
  archiveGrades.value = [];
  preview.value = null;
  confirmed.value = false;
  executed.value = null;
  idempotencyKey.value = "";
}
</script>

<template>
  <el-steps :active="step" align-center class="steps">
    <el-step title="配置映射" />
    <el-step title="预览核对" />
    <el-step title="确认执行" />
  </el-steps>

  <!-- 步骤 1：配置 -->
  <div v-if="step === 0">
    <h4>升年级映射</h4>
    <div v-for="(item, index) in advances" :key="index" class="mapping-row">
      <el-input
        v-model="item.from_grade"
        data-testid="mapping-from-0"
        placeholder="当前年级（如：高一）"
        maxlength="16"
        style="width: 180px"
      />
      <span class="arrow">→</span>
      <el-input
        v-model="item.to_grade"
        data-testid="mapping-to-0"
        placeholder="新年级（如：高二）"
        maxlength="16"
        style="width: 180px"
      />
      <el-input-number
        v-model="item.entry_year"
        :min="1970"
        :max="2100"
        placeholder="仅升某届"
        style="width: 150px"
      />
      <el-button link type="danger" :disabled="advances.length === 1" @click="removeAdvance(index)"
        >移除</el-button
      >
    </div>
    <el-button link type="primary" @click="addAdvance">+ 添加映射</el-button>
    <div v-if="mappingError" data-testid="mapping-error">
      <el-text type="danger">{{ mappingError }}</el-text>
    </div>

    <h4 class="section">归档年级（毕业届）</h4>
    <el-tag
      v-for="grade in archiveGrades"
      :key="grade"
      closable
      class="archive-tag"
      @close="removeArchiveGrade(grade)"
    >
      {{ grade }}
    </el-tag>
    <el-input
      v-model="newArchiveGrade"
      placeholder="输入年级后回车，如：高三"
      maxlength="16"
      style="width: 220px"
      @keydown.enter="addArchiveGrade"
    />

    <div class="footer">
      <el-button
        type="primary"
        data-testid="rollover-preview"
        :disabled="!mappingValid"
        :loading="previewLoading"
        @click="goPreview"
      >
        预览影响面
      </el-button>
    </div>
  </div>

  <!-- 步骤 2：预览核对 -->
  <div v-else-if="step === 1">
    <h4>将升年级（{{ preview?.advanced.length ?? 0 }} 个班）</h4>
    <el-table
      :data="preview?.advanced ?? []"
      data-testid="rollover-preview-advanced"
      max-height="300"
    >
      <el-table-column prop="entry_year" label="入学年份" width="110" />
      <el-table-column prop="name" label="班名" width="120" />
      <el-table-column prop="from_grade" label="当前年级" width="110" />
      <el-table-column prop="to_grade" label="将变为" width="110" />
    </el-table>
    <h4 class="section">将归档（{{ preview?.archived.length ?? 0 }} 个班）</h4>
    <el-table
      :data="preview?.archived ?? []"
      data-testid="rollover-preview-archived"
      max-height="240"
    >
      <el-table-column prop="entry_year" label="入学年份" width="110" />
      <el-table-column prop="name" label="班名" width="120" />
      <el-table-column prop="from_grade" label="毕业时年级" width="110" />
    </el-table>
    <el-checkbox v-model="confirmed" data-testid="rollover-confirm" class="confirm-box">
      我已逐班核对入学年份，确认执行（学生/任教/权限不受影响，操作不可自动撤销）
    </el-checkbox>
    <div class="footer">
      <el-button @click="step = 0">上一步</el-button>
      <el-button
        type="danger"
        data-testid="rollover-execute"
        :disabled="!confirmed"
        :loading="executing"
        @click="execute"
      >
        确认执行
      </el-button>
    </div>
  </div>

  <!-- 步骤 3：结果 -->
  <div v-else>
    <el-result icon="success" title="学年流转完成">
      <template #sub-title>
        已升级 {{ executed?.advanced_count ?? 0 }} 个班，归档
        {{ executed?.archived_count ?? 0 }} 个班。
        学生、任教关系与权限范围未受影响；操作已写入审计。
      </template>
      <template #extra>
        <el-button type="primary" @click="reset">再执行一次（下一学年）</el-button>
      </template>
    </el-result>
  </div>
</template>

<style scoped>
.steps {
  margin: 16px 0 24px;
}
.mapping-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.arrow {
  color: var(--el-text-color-secondary);
}
.section {
  margin-top: 20px;
}
.archive-tag {
  margin-right: 8px;
}
.confirm-box {
  display: block;
  margin: 16px 0;
}
.footer {
  margin-top: 16px;
}
</style>
