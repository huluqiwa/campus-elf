<script setup lang="ts">
/**
 * 权限账号详情抽屉（M2-UI3 TD-187g-C）：绑定角色 + 停用/启用 + 重置 MFA。
 *
 * - 打开时**必发一次** `GET /users/{id}/roles`：列表行是进页时的快照，`roles_version` 可能已落后；
 * - 绑定区以「当前持有的角色」为基底（§20.3 C3-A）：`bindable=false` 的行只读展示，
 *   但由 `bindingPlan` 无条件留在提交集合里——`PUT` 是全量替换，漏一行就是静默收回权限；
 * - 三个写控件按 `accountGates` 给不给（超管 / 是自己 / 缺码 / 状态机），后端 403 才是真边界；
 * - 停用与重置 MFA 的理由 ≥10 字（前端先拦）；**绑定理由没有长度判据**，不走同一函数；
 * - 后端这三个动作都回 204，被撤销的会话条数只在审计里 → 提示只能说"需重新登录"，不能说条数。
 */
import type {
  BindableRoleView,
  PermissionAccountView,
  UserBindingsView,
  UserLookupView,
} from "@campus-elf/api-client";
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, ref, watch } from "vue";

import { rbacApi } from "../api";
import { reportError } from "../error-toast";
import {
  accountGates,
  accountReasonError,
  bindingPlan,
  bindingVersionConflict,
  MFA_RESET_CODE,
  REASON_MAX_LENGTH,
  statusLabel,
  typeLabel,
} from "../account-logic";

const props = defineProps<{
  modelValue: boolean;
  /** 列表行入口（有服务端 `is_super_admin`）。与 `lookupTarget` 二选一。 */
  row: PermissionAccountView | null;
  /** 精确定位入口（该接口不下发 `is_super_admin`，见 §20.6 登记的 TD）。 */
  lookupTarget: UserLookupView | null;
  /** 可授角色目录（`GET /school/bindable-roles`，不含权限矩阵）。 */
  catalog: BindableRoleView[];
  selfUserId: string | null;
  permissions: string[];
}>();

const emit = defineEmits<{
  "update:modelValue": [visible: boolean];
  /** 任一写动作成功：父页面重读当前页（列表的 status/角色/MFA 都要跟着变）。 */
  updated: [];
}>();

const targetUserId = computed(() => props.row?.id ?? props.lookupTarget?.id ?? null);

const bindings = ref<UserBindingsView | null>(null);
const loading = ref(false);
const mfaEnabled = ref(false);

const chosen = ref<string[]>([]);
const bindingReason = ref("");
const bindingSaving = ref(false);
const conflictNotice = ref<string | null>(null);

const deactivateReason = ref("");
const statusBusy = ref(false);
const mfaReason = ref("");
const mfaBusy = ref(false);

/** 合并「刚读到的绑定快照」与「入口带来的账号信息」，作为门禁与展示的单一事实。 */
const resolved = computed<PermissionAccountView | null>(() => {
  const snapshot = bindings.value;
  if (!snapshot) return props.row;
  const roleCodes = snapshot.roles.map((role) => role.code);
  return {
    id: snapshot.user_id,
    display_name: snapshot.display_name,
    user_type: snapshot.user_type,
    status: snapshot.status,
    role_codes: roleCodes,
    roles_version: snapshot.roles_version,
    mfa_enabled: mfaEnabled.value,
    // 列表行给的是服务端判据；精确定位没有这个字段，只能退回"持有不可授出的角色"
    // ——同样是服务端下发的标记，不在前端按角色码硬认（TD-242）
    is_super_admin: props.row
      ? props.row.is_super_admin
      : snapshot.roles.some((role) => !role.bindable),
  };
});

const gates = computed(() =>
  resolved.value
    ? accountGates(resolved.value, props.selfUserId, props.permissions)
    : {
        bind: { allowed: false, hint: null },
        deactivate: { allowed: false, hint: null },
        activate: { allowed: false, hint: null },
        mfaReset: { allowed: false, hint: null },
      },
);

const heldRoles = computed(() => bindings.value?.roles ?? []);
const heldIds = computed(() => heldRoles.value.map((role) => role.id));

/** 缺 MFA 专用码时整个重置区不渲染（§20.3 C4：不给"看得见但用不了"的区块）。 */
const showMfaSection = computed(
  () => bindings.value !== null && props.permissions.includes(MFA_RESET_CODE),
);

/** 确认框里怎么称呼这一行：`display_name` 允许为 null，此时给 ID 尾号而不是留空。 */
const targetLabel = computed(() => {
  const name = resolved.value?.display_name;
  if (name) return name;
  return `未命名账号（ID 尾号 ${(targetUserId.value ?? "").slice(-6)}）`;
});

/** 可新增候选：目录里还没持有的那些。 */
const addableRoles = computed(() =>
  props.catalog.filter((role) => !heldIds.value.includes(role.id)),
);
const plan = computed(() => bindingPlan(heldRoles.value, chosen.value));
const dirty = computed(() => plan.value.added.length > 0 || plan.value.removed.length > 0);

const roleNames = computed(() => {
  const map = new Map<string, string>();
  for (const role of props.catalog) map.set(role.id, `${role.name}（${role.code}）`);
  for (const role of heldRoles.value) map.set(role.id, `${role.name}（${role.code}）`);
  return map;
});

function nameOf(id: string): string {
  return roleNames.value.get(id) ?? id;
}

function toggleRole(id: string, checked: boolean): void {
  chosen.value = checked
    ? [...new Set([...chosen.value, id])]
    : chosen.value.filter((x) => x !== id);
}

async function loadBindings(): Promise<void> {
  const userId = targetUserId.value;
  if (!userId) return;
  loading.value = true;
  conflictNotice.value = null;
  try {
    const snapshot = await rbacApi.getUserBindings(userId);
    bindings.value = snapshot;
    // 基底 = 当前持有；可撤的行默认勾上，不可授的行不进 chosen（由 bindingPlan 保留）
    chosen.value = snapshot.roles.filter((role) => role.bindable).map((role) => role.id);
    bindingReason.value = "";
  } catch (error) {
    bindings.value = null;
    reportError(error, "读取该账号的角色绑定失败");
  } finally {
    loading.value = false;
  }
}

watch([() => props.modelValue, targetUserId], ([open]) => {
  if (open) {
    mfaEnabled.value = props.row?.mfa_enabled ?? props.lookupTarget?.mfa_enabled ?? false;
    deactivateReason.value = "";
    mfaReason.value = "";
    void loadBindings();
  } else {
    bindings.value = null;
  }
});

function applySnapshot(snapshot: UserBindingsView): void {
  bindings.value = snapshot;
  chosen.value = snapshot.roles.filter((role) => role.bindable).map((role) => role.id);
}

async function saveBindings(): Promise<void> {
  const userId = targetUserId.value;
  const snapshot = bindings.value;
  if (!userId || !snapshot) return;
  const reason = bindingReason.value.trim();
  bindingSaving.value = true;
  try {
    const result = await rbacApi.replaceUserBindings(userId, {
      role_ids: plan.value.submitted,
      version: snapshot.roles_version,
      reason: reason || null,
    });
    applySnapshot(result);
    conflictNotice.value = null;
    const summary = [
      result.added_role_codes.length ? `授予 ${result.added_role_codes.join("、")}` : "",
      result.removed_role_codes.length ? `收回 ${result.removed_role_codes.join("、")}` : "",
    ]
      .filter(Boolean)
      .join("；");
    ElMessage.success(`角色绑定已更新${summary ? `：${summary}` : ""}（下次请求即生效）`);
    emit("updated");
  } catch (error) {
    const latest = bindingVersionConflict(error);
    if (latest) {
      applySnapshot(latest);
      conflictNotice.value = "该账号的角色绑定已被他人修改，已刷新为最新快照，请重新确认后再保存。";
      ElMessage.warning(conflictNotice.value);
    } else {
      reportError(error, "保存角色绑定失败");
    }
  } finally {
    bindingSaving.value = false;
  }
}

async function deactivate(): Promise<void> {
  const userId = targetUserId.value;
  if (!userId) return;
  const error = accountReasonError(deactivateReason.value, "停用");
  if (error) {
    ElMessage.warning(error);
    return;
  }
  const confirmed = await ElMessageBox.confirm(
    `确认停用「${targetLabel.value}」的账号？该用户会被立即退出登录，` +
      "重新登录也会被拒绝，直到有人启用它。",
    "停用确认",
    { type: "warning" },
  ).catch(() => false);
  if (!confirmed) return;
  statusBusy.value = true;
  try {
    await rbacApi.deactivateUser(userId, { reason: deactivateReason.value.trim() });
    deactivateReason.value = "";
    ElMessage.success("已停用该账号并撤销其登录会话");
    emit("updated");
    await loadBindings();
  } catch (caught) {
    reportError(caught, "停用失败");
  } finally {
    statusBusy.value = false;
  }
}

async function activate(): Promise<void> {
  const userId = targetUserId.value;
  if (!userId) return;
  const confirmed = await ElMessageBox.confirm(
    `确认启用「${targetLabel.value}」的账号？启用会清空失败登录计数与锁定状态。`,
    "启用确认",
    { type: "warning" },
  ).catch(() => false);
  if (!confirmed) return;
  statusBusy.value = true;
  try {
    await rbacApi.activateUser(userId);
    ElMessage.success("已启用该账号");
    emit("updated");
    await loadBindings();
  } catch (caught) {
    reportError(caught, "启用失败");
  } finally {
    statusBusy.value = false;
  }
}

async function resetMfa(): Promise<void> {
  const userId = targetUserId.value;
  if (!userId) return;
  const error = accountReasonError(mfaReason.value, "MFA 重置");
  if (error) {
    ElMessage.warning(error);
    return;
  }
  const confirmed = await ElMessageBox.confirm(
    `确认清除「${targetLabel.value}」的 MFA 绑定？` +
      "该用户的所有会话会立即退出，下次登录需重新绑定。",
    "MFA 重置确认",
    { type: "warning" },
  ).catch(() => false);
  if (!confirmed) return;
  mfaBusy.value = true;
  try {
    await rbacApi.resetUserMfa(userId, { reason: mfaReason.value.trim() });
    mfaReason.value = "";
    mfaEnabled.value = false;
    ElMessage.success("已清除 MFA 绑定并撤销该账号的登录会话");
    emit("updated");
  } catch (caught) {
    reportError(caught, "MFA 重置失败");
  } finally {
    mfaBusy.value = false;
  }
}

function close(): void {
  emit("update:modelValue", false);
}
</script>

<template>
  <el-drawer
    :model-value="modelValue"
    size="560px"
    data-testid="account-drawer"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <template #header>
      <div class="drawer-head">
        <span class="title">{{ resolved?.display_name ?? "账号详情" }}</span>
        <span v-if="resolved" class="meta">
          {{ typeLabel(resolved.user_type) }} · {{ statusLabel(resolved.status) }}
        </span>
      </div>
    </template>

    <div v-loading="loading" class="drawer-body">
      <el-alert
        v-if="!gates.bind.allowed && gates.bind.hint"
        data-testid="account-gate-hint"
        :title="gates.bind.hint"
        type="info"
        :closable="false"
        show-icon
      />

      <section v-if="bindings" class="section" data-testid="binding-section">
        <h3>角色绑定</h3>
        <p class="hint">
          保存是全量替换：提交后的集合就是该账号的全部角色；勾掉一项即收回它带来的权限。
        </p>

        <div class="held">
          <p class="sub-title">当前持有</p>
          <el-checkbox
            v-for="role in heldRoles"
            :key="role.id"
            :data-testid="`binding-held-${role.code}`"
            :model-value="role.bindable ? chosen.includes(role.id) : true"
            :disabled="!role.bindable"
            @update:model-value="(checked) => toggleRole(role.id, checked === true)"
          >
            {{ role.name }}（{{ role.code }}）
            <span v-if="!role.bindable" class="locked">· 不可变更（保留，不会因保存而收回）</span>
          </el-checkbox>
          <p v-if="heldRoles.length === 0" class="empty">该账号当前没有任何角色。</p>
        </div>

        <div v-if="addableRoles.length" class="addable">
          <p class="sub-title">可新增</p>
          <el-checkbox
            v-for="role in addableRoles"
            :key="role.id"
            :data-testid="`binding-add-${role.code}`"
            :model-value="chosen.includes(role.id)"
            @update:model-value="(checked) => toggleRole(role.id, checked === true)"
          >
            {{ role.name }}（{{ role.code }}）
          </el-checkbox>
        </div>

        <p v-if="dirty" class="diff" data-testid="binding-diff">
          本次变更：
          <span v-if="plan.added.length">授予 {{ plan.added.map(nameOf).join("、") }}</span>
          <span v-if="plan.added.length && plan.removed.length"> ／ </span>
          <span v-if="plan.removed.length">
            收回 {{ plan.removed.map(nameOf).join("、") }}
            <template v-if="plan.emptiesAll">（该账号将不再持有任何角色）</template>
          </span>
        </p>

        <el-input
          v-model="bindingReason"
          :maxlength="REASON_MAX_LENGTH"
          type="textarea"
          :rows="2"
          placeholder="操作理由（可选，最长 500 字；写入审计）"
          data-testid="binding-reason"
        />
        <el-button
          type="primary"
          :disabled="!gates.bind.allowed || !dirty"
          :loading="bindingSaving"
          data-testid="binding-save"
          @click="saveBindings"
        >
          保存角色绑定
        </el-button>
        <p v-if="conflictNotice" class="conflict" data-testid="binding-conflict">
          {{ conflictNotice }}
        </p>

        <p v-if="bindings.permission_codes.length" class="effective">
          生效权限：{{ bindings.permission_codes.join("、") }}
        </p>
        <p v-else class="effective">该账号目前没有生效任何权限码。</p>
      </section>

      <section v-if="bindings" class="section" data-testid="status-section">
        <h3>账号可用性</h3>
        <template v-if="resolved?.status === 'disabled'">
          <p class="hint">该账号已停用，重新登录后也会被拒绝。</p>
          <el-button
            :disabled="!gates.activate.allowed"
            :loading="statusBusy"
            data-testid="account-activate"
            @click="activate"
          >
            启用该账号
          </el-button>
          <p v-if="!gates.activate.allowed && gates.activate.hint" class="gate-hint">
            {{ gates.activate.hint }}
          </p>
        </template>
        <template v-else>
          <el-input
            v-model="deactivateReason"
            :maxlength="REASON_MAX_LENGTH"
            type="textarea"
            :rows="2"
            placeholder="停用理由（必填，去空白后至少 10 个字）"
            data-testid="deactivate-reason"
          />
          <el-button
            type="danger"
            plain
            :disabled="!gates.deactivate.allowed"
            :loading="statusBusy"
            data-testid="deactivate-submit"
            @click="deactivate"
          >
            停用该账号
          </el-button>
          <p v-if="!gates.deactivate.allowed && gates.deactivate.hint" class="gate-hint">
            {{ gates.deactivate.hint }}
          </p>
        </template>
      </section>

      <section v-if="showMfaSection" class="section" data-testid="mfa-section">
        <h3>重置 MFA</h3>
        <p v-if="!mfaEnabled" class="hint">该账号没有开启 MFA，无需重置。</p>
        <template v-else>
          <el-input
            v-model="mfaReason"
            :maxlength="REASON_MAX_LENGTH"
            type="textarea"
            :rows="2"
            placeholder="重置理由（必填，去空白后至少 10 个字）"
            data-testid="mfa-reset-reason"
          />
          <el-button
            :disabled="!gates.mfaReset.allowed"
            :loading="mfaBusy"
            data-testid="mfa-reset-submit"
            @click="resetMfa"
          >
            清除 MFA 绑定
          </el-button>
          <p v-if="!gates.mfaReset.allowed && gates.mfaReset.hint" class="gate-hint">
            {{ gates.mfaReset.hint }}
          </p>
        </template>
      </section>

      <div class="footer">
        <el-button data-testid="account-drawer-close" @click="close">关闭</el-button>
      </div>
    </div>
  </el-drawer>
</template>

<style scoped>
.drawer-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
}

.drawer-head .title {
  font-size: 16px;
  font-weight: 600;
}

.drawer-head .meta {
  color: var(--el-text-color-secondary);
  font-size: 12px;
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

.sub-title {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.hint,
.effective,
.diff,
.empty,
.gate-hint,
.conflict {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.diff,
.conflict {
  color: var(--el-color-warning);
}

.locked {
  color: var(--el-color-warning);
}

.footer {
  display: flex;
  justify-content: flex-end;
}
</style>
