<script setup lang="ts">
/**
 * 角色权限配置 + MFA 管理员重置（M2-UI2：TD-137/TD-143/TD-126）。
 * - 只允许编辑后端标记 editable 的角色（超管/学生/教师只读并给出原因）；
 * - 保存是"全量替换 + 版本锁"：409 时用后端回传的 latest 刷新本地，绝不静默覆盖；
 * - 恢复出厂先预览影响面，再输入角色编码 + ≥10 字理由二次确认；只对内置角色开——
 *   自定义角色没有出厂定义，服务端恒 409 `ROLE_DEFAULTS_UNAVAILABLE`，按钮亮着就是死路；
 * - 自扩权（TD-191）：本地预检命中就先挡；服务端判定与本页快照不一致（422 SELF_GRANT_*）时
 *   强制展开确认条并保留草稿——重新 load() 会把勾选和理由一起清掉，那是更糟的"修复"；
 * - MFA 重置按登录名精确定位、展示目标后填理由执行（仅持专用权限可见）。
 */
import { toUserMessage } from "@campus-elf/api-client";
import type {
  PermissionView,
  ResetPreview,
  RoleView,
  UserLookupView,
} from "@campus-elf/api-client";
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, onMounted, ref } from "vue";

import { rbacApi } from "../api";
import {
  conflictedLatestRole,
  groupPermissions,
  isSelfLockRemoval,
  permissionDiff,
  resetConfirmError,
  ROLE_MANAGE_CODE,
  selfExpansionCodes,
  selfGrantGate,
  selfGrantRejectionMessage,
} from "../rbac-logic";
import { useAuthStore } from "../stores/auth";

const auth = useAuthStore();

const roles = ref<RoleView[]>([]);
const permissions = ref<PermissionView[]>([]);
const loading = ref(false);
const selectedRoleCode = ref<string | null>(null);
const draft = ref<string[]>([]);
const saveReason = ref("");
const saving = ref(false);
const selfGrantAck = ref(false);

const selectedRole = computed(
  () => roles.value.find((role) => role.code === selectedRoleCode.value) ?? null,
);
const groups = computed(() => groupPermissions(permissions.value));
const changes = computed(() =>
  permissionDiff(selectedRole.value?.permission_codes ?? [], draft.value),
);
const dirty = computed(() => changes.value.added.length > 0 || changes.value.removed.length > 0);

// 自扩权（TD-191）：敏感码清单由后端目录下发，前端不复刻一份，否则后端加码这里就静默失效
const sensitiveCodes = computed(
  () =>
    new Set(permissions.value.filter((item) => item.sensitive_self_grant).map((item) => item.code)),
);
const actorRoles = computed(() => auth.user?.roles ?? []);
const selfExpansion = computed(() =>
  selectedRole.value
    ? selfExpansionCodes(
        changes.value.added,
        selectedRole.value.code,
        actorRoles.value,
        auth.user?.permissions ?? [],
        sensitiveCodes.value,
      )
    : [],
);
// 与后端同一道豁免：超管主体后端不校验 ack/理由，界面也不能把保存禁成多余的勾选
const selfGrant = computed(() => selfGrantGate(selfExpansion.value, actorRoles.value));
/**
 * 服务端已经以 SELF_GRANT_* 拒过一次：本地快照落后于库，判据只能信服务端。
 * 存的是拒答原文（含具体码集），确认条上方按原文提示；不按当前 draft 重算就清零——
 * 重算用的正是那份被证明落后的快照，宁可多要一次勾选，不能给出"改回原样就能存"的假出口。
 */
const forcedSelfGrant = ref<string | null>(null);
const needsSelfGrantAck = computed(
  () => selfGrant.value.kind === "ack_required" || forcedSelfGrant.value !== null,
);
const auditOnlySelfGrant = computed(() => selfGrant.value.kind === "audit_only");
const selfGrantCodes = computed(() => selfGrant.value.codes.join("、"));
/** 快照落后时本地算不出码集（`selfGrantCodes` 为空），文案改指服务端判定，避免渲染出空括号。 */
const selfGrantCodesText = computed(() =>
  selfGrantCodes.value ? `（${selfGrantCodes.value}）` : "（服务端判定的敏感权限）",
);
const selfGrantReady = computed(
  () => !needsSelfGrantAck.value || (selfGrantAck.value && saveReason.value.trim().length > 0),
);

function selectRole(role: RoleView): void {
  selectedRoleCode.value = role.code;
  draft.value = [...role.permission_codes];
  saveReason.value = "";
  selfGrantAck.value = false;
  forcedSelfGrant.value = null;
}

function replaceRole(role: RoleView): void {
  const index = roles.value.findIndex((item) => item.id === role.id);
  if (index >= 0) {
    roles.value[index] = role;
  } else {
    roles.value.push(role);
  }
  selectRole(role);
}

async function load(): Promise<void> {
  loading.value = true;
  try {
    const [roleList, permissionList] = await Promise.all([
      rbacApi.listRoles(),
      rbacApi.listPermissions(),
    ]);
    roles.value = roleList;
    permissions.value = permissionList;
    const keep =
      roleList.find((role) => role.code === selectedRoleCode.value) ??
      roleList.find((role) => role.editable) ??
      roleList[0];
    if (keep) selectRole(keep);
  } catch (error) {
    ElMessage.error(toUserMessage(error, "角色权限加载失败"));
  } finally {
    loading.value = false;
  }
}

function applyConflict(error: unknown): boolean {
  const latest = conflictedLatestRole(error);
  if (!latest) return false;
  replaceRole(latest);
  ElMessage.warning("角色配置已被他人修改，已载入最新版本，请重新调整");
  return true;
}

async function save(): Promise<void> {
  const role = selectedRole.value;
  if (!role || !role.editable) return;
  if (!dirty.value) {
    ElMessage.info("没有需要保存的变更");
    return;
  }
  const { added, removed } = changes.value;
  const summary = `新增 ${added.length} 项、移除 ${removed.length} 项`;
  // 自扩权：后端会拒（422），界面先挡一道并说清是哪几个码
  if (needsSelfGrantAck.value && !selfGrantReady.value) {
    ElMessage.warning(
      `本次会给自己所在角色新增敏感权限${selfGrantCodesText.value}，需勾选确认并填写理由`,
    );
    return;
  }
  // 操作者本人在被改角色里且正要撤掉进门凭证 = 自锁（下一次请求即失去本页访问权）
  const selfLock = isSelfLockRemoval(role.code, removed, auth.user?.roles ?? []);
  const selfGrantHint = needsSelfGrantAck.value
    ? `该操作会给你本人新增敏感权限${selfGrantCodesText.value}，已按你的确认提交并单独写入审计。`
    : auditOnlySelfGrant.value
      ? `该操作会给你本人新增敏感权限（${selfGrantCodes.value}）；超管无需勾选确认，但仍会单独写入自扩权审计。`
      : "";
  const confirmed = await ElMessageBox.confirm(
    selfLock
      ? `确认保存「${role.name}」的权限变更？${summary}。你本人也在该角色中，移除 ${ROLE_MANAGE_CODE} 后本页面将立即不可访问（自锁），后续需由超管重新授权。${selfGrantHint}`
      : `确认保存「${role.name}」的权限变更？${summary}。保存后对该角色用户立即生效。${selfGrantHint}`,
    "保存确认",
    { type: "warning" },
  ).catch(() => false);
  if (!confirmed) return;

  saving.value = true;
  try {
    const updated = await rbacApi.replaceRolePermissions(role.id, {
      permission_codes: draft.value,
      version: role.version,
      reason: saveReason.value.trim() || null,
      self_grant_ack: selfGrantAck.value,
    });
    replaceRole(updated);
    ElMessage.success(`已保存：${summary}`);
    // 权限每请求读库：当前账号若在该角色里，菜单/守卫立即按新库值刷新
    await auth.loadUser();
  } catch (error) {
    if (applyConflict(error)) return;
    const rejected = selfGrantRejectionMessage(error);
    if (rejected !== null) {
      // 本地预检落后于库：不刷新页面（那会丢掉正在编辑的勾选与理由），直接把确认条逼出来。
      // ack 保持原值：REASON_REQUIRED 时勾已经打过了，再撤掉等于让操作者重做已完成的一步。
      forcedSelfGrant.value = rejected;
      ElMessage.warning("服务端判定本次为自扩权：勾选确认并填写理由后再次保存");
    } else {
      ElMessage.error(toUserMessage(error, "保存失败"));
    }
  } finally {
    saving.value = false;
  }
}

const resetVisible = ref(false);
const resetPreview = ref<ResetPreview | null>(null);
const resetCode = ref("");
const resetReason = ref("");
const resetting = ref(false);

async function openReset(): Promise<void> {
  const role = selectedRole.value;
  if (!role || !role.editable) return;
  resetVisible.value = true;
  resetPreview.value = null;
  resetCode.value = "";
  resetReason.value = "";
  try {
    resetPreview.value = await rbacApi.previewRoleReset(role.id);
  } catch (error) {
    resetVisible.value = false;
    ElMessage.error(toUserMessage(error, "重置预览失败"));
  }
}

// 出厂清单里没有 role.manage：被授权管理员重置自己所在角色 = 同样的自锁，预览已算出差集
const resetSelfLock = computed(() => {
  const role = selectedRole.value;
  const preview = resetPreview.value;
  if (!role || !preview) return false;
  return isSelfLockRemoval(role.code, preview.permissions_removed, auth.user?.roles ?? []);
});

async function submitReset(): Promise<void> {
  const role = selectedRole.value;
  const preview = resetPreview.value;
  if (!role || !preview) return;
  const invalid = resetConfirmError(role.code, resetCode.value, resetReason.value);
  if (invalid) {
    ElMessage.warning(invalid);
    return;
  }

  resetting.value = true;
  try {
    const result = await rbacApi.resetRole(role.id, {
      confirm_role_code: resetCode.value.trim(),
      reason: resetReason.value.trim(),
      version: preview.version,
    });
    resetVisible.value = false;
    await load();
    ElMessage.success(
      `已恢复出厂：新增 ${result.permissions_added.length} 项、移除 ${result.permissions_removed.length} 项`,
    );
    await auth.loadUser();
  } catch (error) {
    if (applyConflict(error)) {
      resetVisible.value = false;
    } else {
      ElMessage.error(toUserMessage(error, "重置失败"));
    }
  } finally {
    resetting.value = false;
  }
}

const canResetMfa = computed(
  () => auth.user?.permissions.includes("school.personnel.mfa.reset") ?? false,
);
const lookupLoginId = ref("");
const lookupLoading = ref(false);
const mfaTarget = ref<UserLookupView | null>(null);
const mfaReason = ref("");
const resettingMfa = ref(false);

async function lookupUser(): Promise<void> {
  const loginId = lookupLoginId.value.trim();
  if (!loginId) {
    ElMessage.warning("请输入登录名");
    return;
  }
  lookupLoading.value = true;
  mfaTarget.value = null;
  try {
    mfaTarget.value = await rbacApi.lookupUser(loginId);
  } catch (error) {
    ElMessage.error(toUserMessage(error, "未找到该用户"));
  } finally {
    lookupLoading.value = false;
  }
}

async function resetMfa(): Promise<void> {
  const user = mfaTarget.value;
  if (!user) return;
  if (mfaReason.value.trim().length < 10) {
    ElMessage.warning("重置理由至少 10 个字");
    return;
  }
  const confirmed = await ElMessageBox.confirm(
    `确认清除「${user.display_name ?? user.id}」的 MFA 绑定？该用户所有会话将立即退出，需重新绑定。`,
    "MFA 重置确认",
    { type: "warning" },
  ).catch(() => false);
  if (!confirmed) return;

  resettingMfa.value = true;
  try {
    await rbacApi.resetUserMfa(user.id, { reason: mfaReason.value.trim() });
    mfaTarget.value = { ...user, mfa_enabled: false };
    mfaReason.value = "";
    ElMessage.success("已清除 MFA 绑定并撤销该用户会话");
  } catch (error) {
    ElMessage.error(toUserMessage(error, "MFA 重置失败"));
  } finally {
    resettingMfa.value = false;
  }
}

onMounted(() => {
  void load();
});
</script>

<template>
  <div v-loading="loading" class="page">
    <el-card>
      <template #header>
        <div class="card-head">
          <span>角色权限</span>
          <span class="hint">权限按功能分组；保存后对该角色用户立即生效。</span>
        </div>
      </template>

      <div class="role-layout">
        <aside class="role-list" data-testid="role-list">
          <el-button
            v-for="role in roles"
            :key="role.id"
            :type="role.code === selectedRoleCode ? 'primary' : 'default'"
            :plain="role.code !== selectedRoleCode"
            class="role-item"
            :data-testid="`role-${role.code}`"
            @click="selectRole(role)"
          >
            <span class="role-name">{{ role.name }}</span>
            <span class="role-meta"
              >{{ role.user_count }} 人{{ role.editable ? "" : " · 只读" }}</span
            >
          </el-button>
        </aside>

        <section v-if="selectedRole" class="role-detail">
          <div class="role-head">
            <strong>{{ selectedRole.name }}</strong>
            <el-tag size="small" type="info">{{ selectedRole.code }}</el-tag>
            <span class="hint">版本 {{ selectedRole.version }}</span>
          </div>

          <el-alert
            v-if="selectedRole.lock_reason"
            :title="selectedRole.lock_reason"
            type="info"
            :closable="false"
            data-testid="role-lock-reason"
          />

          <div v-for="group in groups" :key="group.module" class="perm-group">
            <h4>{{ group.label }}</h4>
            <el-checkbox-group v-model="draft" :disabled="!selectedRole.editable">
              <el-checkbox
                v-for="perm in group.items"
                :key="perm.code"
                :value="perm.code"
                :data-testid="`perm-${perm.code}`"
                class="perm-item"
              >
                {{ perm.description }}
                <span class="perm-code">{{ perm.code }}</span>
              </el-checkbox>
            </el-checkbox-group>
          </div>

          <div v-if="selectedRole.editable" class="save-bar">
            <el-input
              v-model="saveReason"
              data-testid="role-save-reason"
              :placeholder="
                needsSelfGrantAck ? '变更理由（自扩权必填，≤500 字）' : '变更说明（可选，≤500 字）'
              "
              maxlength="500"
              class="reason-input"
            />
            <el-alert
              v-if="forcedSelfGrant !== null"
              data-testid="role-self-grant-forced"
              type="warning"
              show-icon
              :closable="false"
              :title="forcedSelfGrant"
            />
            <el-checkbox
              v-if="needsSelfGrantAck"
              v-model="selfGrantAck"
              data-testid="role-self-grant-ack"
              class="self-grant-ack"
            >
              我确认：这会给我本人新增敏感权限{{ selfGrantCodesText }}，并已填写理由
            </el-checkbox>
            <p
              v-else-if="auditOnlySelfGrant"
              data-testid="role-self-grant-notice"
              class="self-grant-ack"
            >
              你是校方超管，本次会给自己新增敏感权限（{{
                selfGrantCodes
              }}）：不必勾选确认，但会单独写入自扩权审计。
            </p>
            <el-button
              type="primary"
              data-testid="role-save"
              :disabled="!dirty || !selfGrantReady"
              :loading="saving"
              @click="save"
            >
              保存权限（新增 {{ changes.added.length }} / 移除 {{ changes.removed.length }}）
            </el-button>
            <el-button v-if="selectedRole.is_system" data-testid="role-reset" @click="openReset"
              >恢复出厂设置</el-button
            >
          </div>
        </section>
      </div>
    </el-card>

    <el-card v-if="canResetMfa" class="mfa-card">
      <template #header>
        <div class="card-head">
          <span>安全工具 · MFA 重置</span>
          <span class="hint">按登录名精确定位用户；操作需理由并写入审计。</span>
        </div>
      </template>
      <div class="lookup-bar">
        <el-input
          v-model="lookupLoginId"
          data-testid="mfa-lookup-input"
          placeholder="输入登录名 / 学工号"
          maxlength="254"
          class="reason-input"
          @keydown.enter="lookupUser"
        />
        <el-button data-testid="mfa-lookup-submit" :loading="lookupLoading" @click="lookupUser"
          >查询用户</el-button
        >
      </div>

      <div v-if="mfaTarget" class="mfa-target" data-testid="mfa-target">
        <p>
          {{ mfaTarget.display_name ?? "（未命名）" }} · {{ mfaTarget.user_type }} ·
          {{ mfaTarget.status }} · MFA
          {{ mfaTarget.mfa_enabled ? "已绑定" : "未绑定" }}
        </p>
        <el-input
          v-model="mfaReason"
          data-testid="mfa-reset-reason"
          type="textarea"
          :rows="2"
          maxlength="500"
          placeholder="重置理由（≥10 字，将写入审计）"
        />
        <el-button
          type="danger"
          data-testid="mfa-reset-submit"
          :disabled="!mfaTarget.mfa_enabled"
          :loading="resettingMfa"
          @click="resetMfa"
        >
          清除 MFA 绑定并撤销会话
        </el-button>
      </div>
    </el-card>

    <el-dialog v-model="resetVisible" title="恢复出厂设置" width="600px">
      <div v-if="resetPreview" data-testid="reset-preview" class="preview">
        <p>影响用户：{{ resetPreview.affected_users }} 人 · 目标版本：{{ resetPreview.version }}</p>
        <p>恢复权限：{{ resetPreview.permissions_added.join("、") || "无" }}</p>
        <p>移除权限：{{ resetPreview.permissions_removed.join("、") || "无" }}</p>
        <p>恢复数据范围：{{ resetPreview.scopes_added.join("、") || "无" }}</p>
        <p>移除数据范围：{{ resetPreview.scopes_removed.join("、") || "无" }}</p>
      </div>
      <p v-else class="hint">正在计算影响面…</p>
      <el-alert
        v-if="resetSelfLock"
        data-testid="reset-selflock-warning"
        type="warning"
        show-icon
        :closable="false"
        :title="`自锁风险：本次恢复出厂会移除 ${ROLE_MANAGE_CODE}，你本人也在该角色中，提交后本页面立即不可访问，需由校方超管重新授权。`"
      />
      <el-form label-width="120px" class="reset-form">
        <el-form-item label="输入角色编码" required>
          <el-input
            v-model="resetCode"
            data-testid="reset-code"
            :placeholder="selectedRole ? `请输入 ${selectedRole.code}` : ''"
            maxlength="64"
          />
        </el-form-item>
        <el-form-item label="理由（≥10 字）" required>
          <el-input
            v-model="resetReason"
            data-testid="reset-reason"
            type="textarea"
            :rows="2"
            maxlength="500"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="resetVisible = false">取消</el-button>
        <el-button
          type="danger"
          data-testid="reset-submit"
          :disabled="!resetPreview"
          :loading="resetting"
          @click="submitReset"
        >
          确认恢复出厂
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.card-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.role-layout {
  display: flex;
  gap: 20px;
  align-items: flex-start;
}
.role-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 220px;
  flex-shrink: 0;
}
.role-item {
  justify-content: space-between;
  width: 100%;
  margin: 0;
}
.role-name {
  font-weight: 600;
}
.role-meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.role-detail {
  flex: 1;
  min-width: 0;
}
.role-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.perm-group {
  margin-bottom: 12px;
}
.perm-group h4 {
  margin: 8px 0;
}
.perm-item {
  display: flex;
  width: 100%;
  height: auto;
  align-items: center;
  padding: 2px 0;
}
.perm-code {
  margin-left: 8px;
  color: var(--el-text-color-placeholder);
  font-size: 12px;
}
.save-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--el-border-color-lighter);
  flex-wrap: wrap;
}
.reason-input {
  max-width: 320px;
}
/* 自扩权确认是"占满一行的警示条"，不是行内控件：勾选框和哪几个码被点亮必须一眼看到 */
.self-grant-ack {
  width: 100%;
  color: var(--el-color-warning);
  font-weight: 600;
}
.lookup-bar {
  display: flex;
  align-items: center;
  gap: 12px;
}
.mfa-target {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 16px;
  align-items: flex-start;
}
.preview p {
  margin: 4px 0;
  word-break: break-all;
}
.reset-form {
  margin-top: 16px;
}
</style>
