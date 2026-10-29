<script setup lang="ts">
/**
 * 新建自定义角色对话框（TD-187g-B）。
 *
 * - 初始权限**二选一**（决策 B3）：显式清单 / 复制同租户可编辑角色。后端拒"两份非空来源同时给"
 *   （`ROLE_INITIAL_SOURCE_AMBIGUOUS`），所以这里是单选而不是两个勾选框。
 * - code / name 的本地校验与后端**同一判据**：后端这两条撞 Pydantic（数组形 422），
 *   不预检用户只会看到读不懂的英文报错。
 * - `school.role.manage` 只有校方超管能转授（后端 `_assert_delegable`），非超管在勾选阶段就被挡住。
 */
import { toUserMessage } from "@campus-elf/api-client";
import type { PermissionView, RoleView } from "@campus-elf/api-client";
import { ElMessage } from "element-plus";
import { computed, ref, watch } from "vue";

import { rbacApi } from "../api";
import {
  canCopyRoleCodes,
  canDelegateCode,
  groupPermissions,
  roleCodeError,
  roleNameError,
} from "../rbac-logic";

const props = defineProps<{
  modelValue: boolean;
  roles: RoleView[];
  permissions: PermissionView[];
  actorRoles: string[];
}>();

const emit = defineEmits<{
  "update:modelValue": [value: boolean];
  created: [role: RoleView];
}>();

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit("update:modelValue", value),
});

const code = ref("");
const name = ref("");
const reason = ref("");
const sourceMode = ref<"empty" | "copy">("empty");
const copySourceId = ref<string | null>(null);
const initialPermissions = ref<string[]>([]);
const submitting = ref(false);

const groups = computed(() => groupPermissions(props.permissions));
/**
 * 复制源只能是**可编辑**角色（锁定角色后端恒 422 `ROLE_COPY_SOURCE_NOT_EDITABLE`），
 * 且它的权限码必须是自己能转授的（后端 `_assert_delegable` 对复制来的码一样查）——
 * 否则界面给的是一个"点得动、提交必炸"的选项。
 */
const copySources = computed(() =>
  props.roles.filter((role) => role.editable && canCopyRoleCodes(role, props.actorRoles)),
);
const codeError = computed(() => roleCodeError(code.value));
const nameError = computed(() => roleNameError(name.value));
const sourceError = computed(() => {
  if (sourceMode.value !== "copy") return null;
  if (!copySourceId.value) return "请选择要复制的角色";
  // 已选中的复制源可能因为角色列表变了而不再可选（此时下拉里已选不中）
  if (!copySources.value.some((role) => role.id === copySourceId.value)) {
    return "所选复制源不可用（含你没有权限转授的权限码），请另选一个";
  }
  return null;
});
const canSubmit = computed(
  () => !codeError.value && !nameError.value && !sourceError.value && !submitting.value,
);

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return;
    code.value = "";
    name.value = "";
    reason.value = "";
    sourceMode.value = "empty";
    copySourceId.value = null;
    initialPermissions.value = [];
  },
);

async function submit(): Promise<void> {
  const invalid = codeError.value ?? nameError.value ?? sourceError.value;
  if (invalid) {
    ElMessage.warning(invalid);
    return;
  }
  submitting.value = true;
  try {
    // 复制模式只给 copy_from_role_id：两份来源同时给会被后端判成"来源二义"拒掉
    const created =
      sourceMode.value === "copy"
        ? await rbacApi.createRole({
            code: code.value.trim(),
            name: name.value.trim(),
            copy_from_role_id: copySourceId.value,
            reason: reason.value.trim() || null,
          })
        : await rbacApi.createRole({
            code: code.value.trim(),
            name: name.value.trim(),
            permission_codes: initialPermissions.value,
            reason: reason.value.trim() || null,
          });
    visible.value = false;
    ElMessage.success(`已创建角色「${created.name}」`);
    emit("created", created);
  } catch (error) {
    ElMessage.error(toUserMessage(error, "创建角色失败"));
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="新建自定义角色"
    width="640px"
    data-testid="role-create-dialog"
  >
    <el-form label-width="100px" class="create-form">
      <el-form-item label="角色编码" required>
        <el-input
          v-model="code"
          data-testid="role-create-code"
          placeholder="小写字母开头，只含小写字母/数字/下划线，3–63 位"
          maxlength="63"
        />
        <p v-if="codeError" class="field-error" data-testid="role-create-code-error">
          {{ codeError }}
        </p>
      </el-form-item>
      <el-form-item label="角色名称" required>
        <el-input
          v-model="name"
          data-testid="role-create-name"
          placeholder="给人看的名字，例如「校区运营」"
          maxlength="100"
        />
        <p v-if="nameError" class="field-error" data-testid="role-create-name-error">
          {{ nameError }}
        </p>
      </el-form-item>
      <el-form-item label="初始权限">
        <el-radio-group v-model="sourceMode" data-testid="role-create-source">
          <el-radio value="empty" data-testid="role-create-source-empty">空白（稍后配置）</el-radio>
          <el-radio value="copy" data-testid="role-create-source-copy">复制已有角色</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item v-if="sourceMode === 'copy'" label="复制源">
        <el-select
          v-model="copySourceId"
          data-testid="role-create-copy-source"
          placeholder="选择要复制的角色"
          class="full-width"
        >
          <el-option
            v-for="role in copySources"
            :key="role.id"
            :label="`${role.name}（${role.code}）`"
            :value="role.id"
          />
        </el-select>
        <p v-if="sourceError" class="field-error" data-testid="role-create-source-error">
          {{ sourceError }}
        </p>
      </el-form-item>
      <el-form-item v-else label="权限清单">
        <p class="hint">不勾任何一项就是空白角色，保存后可在权限区再配。</p>
        <div v-for="group in groups" :key="group.module" class="perm-group">
          <h4>{{ group.label }}</h4>
          <el-checkbox-group v-model="initialPermissions">
            <el-checkbox
              v-for="perm in group.items"
              :key="perm.code"
              :value="perm.code"
              :disabled="!canDelegateCode(perm.code, actorRoles)"
              :data-testid="`role-create-perm-${perm.code}`"
            >
              {{ perm.description }}
              <span class="perm-code">{{ perm.code }}</span>
            </el-checkbox>
          </el-checkbox-group>
        </div>
      </el-form-item>
      <el-form-item label="创建理由">
        <el-input
          v-model="reason"
          data-testid="role-create-reason"
          type="textarea"
          :rows="2"
          maxlength="500"
          placeholder="可选，写清为什么新增这个角色（写入审计）"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button
        type="primary"
        data-testid="role-create-submit"
        :disabled="!canSubmit"
        :loading="submitting"
        @click="submit"
      >
        创建角色
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.create-form {
  max-height: 60vh;
  overflow-y: auto;
}
.full-width {
  width: 100%;
}
.field-error {
  margin: 4px 0 0;
  color: var(--el-color-danger);
  font-size: 12px;
}
.perm-group h4 {
  margin: 8px 0;
}
.perm-code {
  margin-left: 8px;
  color: var(--el-text-color-placeholder);
  font-size: 12px;
}
.hint {
  margin: 0 0 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
