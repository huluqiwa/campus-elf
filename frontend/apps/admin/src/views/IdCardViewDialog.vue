<script setup lang="ts">
/**
 * 身份证受控查看弹窗（M2-UI1 D5-A）：
 * - 理由必填 ≥10 字（前端预校验，后端仍为最终权威）；
 * - 明文只存在于本组件 ref，弹窗关闭/组件卸载即清空；不写任何浏览器存储。
 */
import { toUserMessage } from "@campus-elf/api-client";
import type { IdCardFull } from "@campus-elf/api-client";
import { computed, ref, watch } from "vue";

import { orgApi } from "../api";
import { reasonValid as reasonCheck } from "../logic";

const props = defineProps<{ personnelId: string | null }>();
const emit = defineEmits<{ closed: [] }>();

const visible = defineModel<boolean>({ default: false });
const reason = ref("");
const result = ref<IdCardFull | null>(null);
const loading = ref(false);
const errorText = ref("");

const reasonValid = computed(() => reasonCheck(reason.value));

watch(visible, (open) => {
  // 打开与关闭都清空（D5-A：明文只在弹窗打开期间存在于组件 ref；不落任何持久层）
  result.value = null;
  reason.value = "";
  errorText.value = "";
  if (!open) emit("closed");
});

async function onView(): Promise<void> {
  if (props.personnelId === null || !reasonValid.value) return;
  loading.value = true;
  errorText.value = "";
  try {
    const view = await orgApi.readIdCard(props.personnelId, reason.value.trim());
    // 复审 M2：请求在途时弹窗被关闭 → 丢弃迟到响应，防明文残留/串人
    if (!visible.value) return;
    result.value = view;
  } catch (error) {
    errorText.value = toUserMessage(error, "查看失败，请稍后重试");
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="身份证完整查看"
    width="480px"
    :close-on-click-modal="false"
    @closed="emit('closed')"
  >
    <el-form label-width="80px">
      <el-form-item label="查看理由" required>
        <el-input
          v-model="reason"
          data-testid="id-card-reason"
          type="textarea"
          :rows="2"
          maxlength="200"
          show-word-limit
          placeholder="请填写查看理由（至少 10 个字），将写入审计日志"
        />
      </el-form-item>
      <el-form-item v-if="result" label="身份证号">
        <span class="full-number" data-testid="id-card-number">{{ result.number }}</span>
        <span class="mask-hint">（掩码 {{ result.mask }}）</span>
      </el-form-item>
      <el-form-item v-if="errorText" label="">
        <el-text type="danger">{{ errorText }}</el-text>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">关闭</el-button>
      <el-button
        type="primary"
        data-testid="id-card-submit"
        :disabled="!reasonValid"
        :loading="loading"
        @click="onView"
      >
        查看明文
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.full-number {
  font-family: monospace;
  font-size: 16px;
  letter-spacing: 1px;
}
.mask-hint {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
