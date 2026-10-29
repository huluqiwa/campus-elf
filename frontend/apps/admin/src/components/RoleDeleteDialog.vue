<script setup lang="ts">
/**
 * 删除自定义角色对话框（TD-187g-B）。
 *
 * 两步：先只读预览影响面（持有人数 + 权限码 + 指纹），再逐字确认角色码 + ≥10 字理由。
 * 提交带 `version` 与 `fingerprint` 两套判据（TD-199）：前者挡"预览后角色权限/范围被改"，
 * 后者挡"预览后持有人集合被改"（绑定不推进角色版本，只能靠指纹）。
 * 预览过期（409 `ROLE_DELETE_PREVIEW_STALE`）时后端回带最新影响面，界面就地刷新让人重新确认，
 * 不必先跳一次 GET 再回来；版本过期（409 `ROLE_VERSION_CONFLICT`）则重取一次预览——
 * 那份回带的角色快照里没有指纹，救不了下一次提交。
 *
 * 只读模式（决策 B4）：后端把「看影响面」（`school.role.manage`）与「真删」
 * （`school.role.delete`）拆成两个码。没有删除码时仍然给影响面预览，但不给执行入口——
 * 给一个必然 403 的按钮是死路，把影响面也藏掉则运营连影响面都看不到。
 */
import { toUserMessage } from "@campus-elf/api-client";
import type { RoleDeletionPreview, RoleView } from "@campus-elf/api-client";
import { ElMessage } from "element-plus";
import { computed, ref, watch } from "vue";

import { rbacApi } from "../api";
import { deleteConfirmError, deleteVersionConflict, staleDeletionPreview } from "../rbac-logic";

const props = defineProps<{
  modelValue: boolean;
  role: RoleView | null;
  /** 有没有删除执行权（`school.role.delete`）。没有就只给只读影响面。 */
  canExecute: boolean;
  /** 没有执行权时给操作者看的说明（决策 B4）。 */
  deniedReason: string | null;
}>();

const emit = defineEmits<{
  "update:modelValue": [value: boolean];
  deleted: [roleId: string];
}>();

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit("update:modelValue", value),
});

const preview = ref<RoleDeletionPreview | null>(null);
const previewLoading = ref(false);
const confirmCode = ref("");
const reason = ref("");
const submitting = ref(false);

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return;
    preview.value = null;
    confirmCode.value = "";
    reason.value = "";
    void loadPreview();
  },
);

async function loadPreview(): Promise<void> {
  const role = props.role;
  if (!role) return;
  previewLoading.value = true;
  try {
    preview.value = await rbacApi.previewRoleDeletion(role.id);
  } catch (error) {
    visible.value = false;
    ElMessage.error(toUserMessage(error, "删除影响面预览失败"));
  } finally {
    previewLoading.value = false;
  }
}

async function submit(): Promise<void> {
  const role = props.role;
  const current = preview.value;
  if (!role || !current) return;
  if (!props.canExecute) return;
  const invalid = deleteConfirmError(role.code, confirmCode.value, reason.value);
  if (invalid) {
    ElMessage.warning(invalid);
    return;
  }
  submitting.value = true;
  try {
    await rbacApi.deleteRole(role.id, {
      confirm_role_code: confirmCode.value.trim(),
      reason: reason.value.trim(),
      version: current.version,
      fingerprint: current.fingerprint,
    });
    visible.value = false;
    ElMessage.success(`已删除角色「${current.name}」`);
    emit("deleted", role.id);
  } catch (error) {
    const latest = staleDeletionPreview(error);
    if (latest) {
      // 影响面变了：就地换成后端回带的最新预览，让人重新确认一次（人已经填好的码与理由保留）
      preview.value = latest;
      ElMessage.warning("删除影响面已变化（持有人集合与预览不一致），请核对后重新确认");
    } else if (deleteVersionConflict(error)) {
      // 角色配置变了：预览里的版本号已失效，重取一次预览（顺带刷新指纹）再让人确认
      await loadPreview();
      ElMessage.warning("角色已被他人修改，已刷新影响面，请核对后重新确认");
    } else {
      ElMessage.error(toUserMessage(error, "删除角色失败"));
    }
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="canExecute ? '删除自定义角色' : '删除影响面（只读预览）'"
    width="620px"
    data-testid="role-delete-dialog"
  >
    <el-alert
      type="warning"
      show-icon
      :closable="false"
      title="删除是不可逆的：该角色的权限、数据范围与账号绑定会一并清理。"
    />
    <el-alert
      v-if="!canExecute && deniedReason"
      type="info"
      show-icon
      :closable="false"
      :title="deniedReason"
      data-testid="role-delete-readonly"
    />
    <div v-if="preview" class="preview" data-testid="role-delete-preview">
      <p>目标角色：{{ preview.name }}（{{ preview.role_code }}）</p>
      <p>影响用户：{{ preview.affected_users }} 人</p>
      <p>将被移除的权限：{{ preview.permission_codes.join("、") || "无" }}</p>
      <p>目标版本：{{ preview.version }}</p>
    </div>
    <p v-else-if="previewLoading" class="hint">正在计算影响面…</p>
    <el-form v-if="canExecute" label-width="120px" class="delete-form">
      <el-form-item label="输入角色编码" required>
        <el-input
          v-model="confirmCode"
          data-testid="role-delete-code"
          :placeholder="role ? `请输入 ${role.code}` : ''"
          maxlength="64"
        />
      </el-form-item>
      <el-form-item label="删除理由（≥10 字）" required>
        <el-input
          v-model="reason"
          data-testid="role-delete-reason"
          type="textarea"
          :rows="2"
          maxlength="500"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">{{ canExecute ? "取消" : "关闭" }}</el-button>
      <el-button
        v-if="canExecute"
        type="danger"
        data-testid="role-delete-submit"
        :disabled="!preview"
        :loading="submitting"
        @click="submit"
      >
        确认删除
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.preview p {
  margin: 4px 0;
  word-break: break-all;
}
.delete-form {
  margin-top: 16px;
}
.hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
