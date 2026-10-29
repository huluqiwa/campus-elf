<script setup lang="ts">
/**
 * 角色级数据范围编辑（TD-187g-B）。
 *
 * - 可改的只有 `school`（全校）与 `self`（本人）两类（后端 `EDITABLE_SCOPE_TYPES` 是唯一门禁，
 *   前端按同一份字面量复刻，TD-247）；`class`/`course` 只读展示并写清「为什么不可改」：
 *   这两类必须绑定具体班级/课程 id，本端点没有那个入参通道。
 * - 保存是「预览 → 确认 → PUT」：预览把"替换后持有人看不到任何数据""本次会扩大自己可见范围"
 *   这些后果先摊开，确认后才写。**改了勾选预览即作废**——提交的是新清单、界面显示的却是旧影响面，
 *   那不叫"确认过"。
 * - 与权限区共用 `roles.version`（TD-202）：权限区有未保存改动时不许提交范围——否则范围保存
 *   会推高版本，把权限区的草稿变成必然 409 的死路。
 * - 自扩权与权限区**同一个内核**（`selfGrantGate`）：超管免打断不免留痕，服务端 422 时把确认条
 *   逼出来并保留草稿——同页两套预期比多一条提示更糟。
 */
import { toUserMessage } from "@campus-elf/api-client";
import type { RoleScopeRow, RoleView, ScopeChangePreview } from "@campus-elf/api-client";
import { ElMessage } from "element-plus";
import { computed, ref, watch } from "vue";

import { rbacApi } from "../api";
import {
  conflictedLatestRole,
  editableScopeTypes,
  editableScopeUniverse,
  READ_ONLY_SCOPE_REASONS,
  SCOPE_LABELS,
  scopePlan,
  selfGrantGate,
  selfGrantRejectionMessage,
} from "../rbac-logic";

const props = defineProps<{
  role: RoleView | null;
  /** 权限区有未保存改动时的阻塞原因（TD-202）。 */
  blockedReason: string | null;
  /** 操作者所在角色（超管豁免自扩权打断，与权限区同一个判据内核）。 */
  actorRoles: string[];
}>();

const emit = defineEmits<{
  updated: [role: RoleView];
  conflict: [error: unknown];
}>();

const rows = ref<RoleScopeRow[]>([]);
const loading = ref(false);
const editing = ref(false);
const chosen = ref<string[]>([]);
const reason = ref("");
const preview = ref<ScopeChangePreview | null>(null);
const ack = ref(false);
const submitting = ref(false);
/** 服务端已以 SELF_GRANT_* 拒过一次：判据只能信服务端，强制展开确认条并保留草稿。 */
const forcedScopeSelfGrant = ref<string | null>(null);

/** 该角色当前已生效的可改范围类型（服务端下发的可编辑行）。 */
const currentEditableTypes = computed(() => editableScopeTypes(rows.value));
/** 可勾选的全集：已有可编辑行 + 没有行的那些（前端副本，TD-247）。 */
const offeredTypes = computed(() => editableScopeUniverse(rows.value));
const plan = computed(() => scopePlan(rows.value, chosen.value));
/** 只读行：与 `plan.retained` 同一份（评审 m6：原本这里另算一份，两处会漂）。 */
const retainedRows = computed(() => plan.value.retained);
const scopeReason = computed(() => reason.value.trim());

/**
 * 没预览过时的提示（评审 m1）：之前这行按「有没有勾选」判断，于是第一次打开对话框就显示
 * 「勾选已改动」，而把勾选全取消时反而不显示——两处都与事实相反。按「勾选与基线的差异」判断：
 * 有差异 = 改过（上一份预览已作废）；无差异 = 还没预览过。
 */
const scopeNeedPreviewText = computed(() =>
  plan.value.added.length || plan.value.removed.length
    ? "勾选已改动，请重新预览影响面再保存。"
    : "请先预览影响面再保存。",
);

/**
 * 自扩权门禁（与权限区同一个 `selfGrantGate`）：超管免打断（只提示仍会写审计），
 * 其他人必须勾确认 + 填理由。
 */
const scopeGate = computed(() =>
  selfGrantGate(preview.value?.newly_reaching_self ?? [], props.actorRoles),
);
const needsScopeAck = computed(
  () => forcedScopeSelfGrant.value !== null || scopeGate.value.kind === "ack_required",
);
const scopeAuditOnly = computed(
  () => forcedScopeSelfGrant.value === null && scopeGate.value.kind === "audit_only",
);
const ackReady = computed(
  () => !needsScopeAck.value || (ack.value && scopeReason.value.length > 0),
);

function scopeLabel(type: string): string {
  return SCOPE_LABELS[type] ?? type;
}

function retainedLabel(row: RoleScopeRow): string {
  const label = scopeLabel(row.scope_type);
  return row.scope_id ? `${label}（${row.scope_id}）` : label;
}

/** 只读行为什么不可改（后端 `_SCOPE_TYPE_OWNERSHIP` 的前端说法，TD-250）。 */
function readonlyReason(type: string): string {
  return READ_ONLY_SCOPE_REASONS[type] ?? "该范围类型不由角色配置写入";
}

/**
 * 重读基线。**会丢弃未保存的范围草稿**（勾选与理由）。
 *
 * 调用点都在"基线已经失效"的场合（换角色、版本被推进、保存成功后），留着旧草稿等于让人
 * 对着一份已经不成立的基线提交。需要保留草稿的场景（服务端 422 自扩权）走另一条路：
 * 不刷新页面，直接把确认条逼出来。
 */
async function loadScopes(): Promise<void> {
  const role = props.role;
  if (!role) {
    rows.value = [];
    return;
  }
  loading.value = true;
  try {
    rows.value = await rbacApi.listRoleScopes(role.id);
    chosen.value = editableScopeTypes(rows.value);
  } catch (error) {
    ElMessage.error(toUserMessage(error, "数据范围加载失败"));
  } finally {
    loading.value = false;
  }
}

// 换角色、或角色版本被推进（别人改了 / 本页另一侧保存了）都要重读基线：
// 拿着旧基线提交必然 409，界面显示的"当前范围"也会与库不一致。
watch(
  () => [props.role?.id, props.role?.version],
  () => {
    void loadScopes();
  },
  { immediate: true },
);

// 勾选一变，之前那份影响面预览就不再对应当前清单 → 作废，逼人重新预览
watch(chosen, () => {
  preview.value = null;
});

function openEditor(): void {
  editing.value = true;
  chosen.value = editableScopeTypes(rows.value);
  reason.value = "";
  preview.value = null;
  ack.value = false;
  forcedScopeSelfGrant.value = null;
}

async function runPreview(): Promise<void> {
  const role = props.role;
  if (!role) return;
  submitting.value = true;
  try {
    preview.value = await rbacApi.previewRoleScopes(role.id, plan.value.submitted);
    // 新预览出来，服务端那次的拒绝就算处理完了（新的清单可能不再触发自扩权）
    forcedScopeSelfGrant.value = null;
  } catch (error) {
    ElMessage.error(toUserMessage(error, "影响面预览失败"));
  } finally {
    submitting.value = false;
  }
}

async function submit(): Promise<void> {
  const role = props.role;
  const current = preview.value;
  if (!role || !current) return;
  if (props.blockedReason) {
    ElMessage.warning(props.blockedReason);
    return;
  }
  if (needsScopeAck.value && !ackReady.value) {
    ElMessage.warning("本次会扩大你本人可见的数据范围，请勾选确认并填写理由");
    return;
  }
  submitting.value = true;
  try {
    const updated = await rbacApi.replaceRoleScopes(role.id, {
      scope_types: plan.value.submitted,
      version: role.version,
      reason: scopeReason.value || null,
      self_grant_ack: ack.value,
    });
    editing.value = false;
    // 重读：本地那份 rows 还是保存前的快照，不重读界面就与库不一致（当前范围仍显示旧值）
    await loadScopes();
    ElMessage.success("已更新数据范围");
    emit("updated", updated);
  } catch (error) {
    const rejected = selfGrantRejectionMessage(error);
    if (rejected !== null) {
      // 本地预检落后于库：不刷新页面（那会丢掉已勾的范围与理由），直接把确认条逼出来
      forcedScopeSelfGrant.value = rejected;
      ElMessage.warning("服务端判定本次为范围自扩权：勾选确认并填写理由后再次提交");
    } else if (conflictedLatestRole(error)) {
      editing.value = false;
      await loadScopes();
      emit("conflict", error);
    } else {
      ElMessage.error(toUserMessage(error, "数据范围保存失败"));
    }
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="scope-panel" data-testid="role-scope-panel">
    <div class="scope-head">
      <strong>数据范围</strong>
      <span class="hint">决定该角色能看到哪些数据；只有「全校」「本人」两类可改。</span>
    </div>

    <div v-loading="loading" class="scope-body">
      <p class="scope-line" data-testid="role-scope-current">
        可改范围：{{ currentEditableTypes.map(scopeLabel).join("、") || "无" }}
      </p>

      <div v-if="retainedRows.length" class="readonly-block" data-testid="role-scope-readonly">
        <p class="readonly-line">
          只读范围：{{ retainedRows.map((row) => retainedLabel(row)).join("、") }}
        </p>
        <p
          v-for="row in retainedRows"
          :key="`${row.scope_type}-${row.scope_id ?? ''}`"
          class="hint"
        >
          {{ scopeLabel(row.scope_type) }}：{{
            row.scope_id ? `绑定到 ${row.scope_id}，` : ""
          }}不可改 —— {{ readonlyReason(row.scope_type) }}
        </p>
      </div>
    </div>

    <el-button
      data-testid="role-scope-edit"
      :disabled="!role || !role.editable"
      @click="openEditor"
    >
      编辑数据范围
    </el-button>

    <el-dialog v-model="editing" title="编辑数据范围" width="620px" data-testid="role-scope-dialog">
      <el-alert
        v-if="blockedReason"
        :title="blockedReason"
        type="warning"
        show-icon
        :closable="false"
        data-testid="role-scope-blocked"
      />
      <div class="scope-picker">
        <p v-if="plan.empties" class="danger" data-testid="role-scope-picker-empties">
          当前勾选：该角色持有人将看不到任何数据
        </p>
        <p v-else class="hint">勾选该角色应当看到的数据范围；两类都不勾 = 该角色看不到任何数据。</p>
        <el-checkbox-group v-model="chosen" data-testid="role-scope-choices">
          <el-checkbox
            v-for="type in offeredTypes"
            :key="type"
            :value="type"
            :data-testid="`role-scope-${type}`"
          >
            {{ scopeLabel(type) }}
            <span class="scope-code">{{ type }}</span>
          </el-checkbox>
        </el-checkbox-group>
      </div>

      <div v-if="retainedRows.length" class="readonly-block">
        <p class="readonly-line">
          保留（不可改）：{{ retainedRows.map((row) => retainedLabel(row)).join("、") }}
        </p>
      </div>

      <div v-if="preview" class="preview" data-testid="role-scope-preview">
        <p>影响用户：{{ preview.affected_users }} 人 · 目标版本：{{ preview.version }}</p>
        <p>新增范围：{{ preview.added.join("、") || "无" }}</p>
        <p>移除范围：{{ preview.removed.join("、") || "无" }}</p>
        <p v-if="preview.empties_scope" class="danger" data-testid="role-scope-empties">
          注意：该角色持有人将看不到任何数据
        </p>
        <p
          v-if="preview.newly_reaching_self.length"
          class="danger"
          data-testid="role-scope-self-warn"
        >
          本次会扩大你本人可见的范围：{{ preview.newly_reaching_self.join("、") }}
        </p>
      </div>
      <p v-else class="hint" data-testid="role-scope-need-preview">
        {{ scopeNeedPreviewText }}
      </p>

      <el-input
        v-model="reason"
        data-testid="role-scope-reason"
        type="textarea"
        :rows="2"
        maxlength="500"
        placeholder="变更理由（写入审计；本次会扩大本人可见范围时必填）"
      />
      <el-alert
        v-if="forcedScopeSelfGrant !== null"
        data-testid="role-scope-self-grant-forced"
        type="warning"
        show-icon
        :closable="false"
        :title="forcedScopeSelfGrant"
      />
      <el-checkbox
        v-if="needsScopeAck"
        v-model="ack"
        data-testid="role-scope-ack"
        class="self-grant-ack"
      >
        我确认：这会扩大我本人可见的数据范围，且已填写理由
      </el-checkbox>
      <p
        v-else-if="scopeAuditOnly"
        data-testid="role-scope-self-grant-notice"
        class="self-grant-ack"
      >
        你是校方超管，本次会扩大你本人可见的数据范围：不必勾选确认，但仍会单独写入自扩权审计。
      </p>

      <template #footer>
        <el-button @click="editing = false">取消</el-button>
        <el-button
          data-testid="role-scope-preview-submit"
          :loading="submitting"
          :disabled="Boolean(blockedReason)"
          @click="runPreview"
        >
          预览影响面
        </el-button>
        <el-button
          type="primary"
          data-testid="role-scope-submit"
          :loading="submitting"
          :disabled="!preview || !ackReady"
          @click="submit"
        >
          确认保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.scope-panel {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--el-border-color-lighter);
}
.scope-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 8px;
}
.hint {
  margin: 4px 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.scope-line {
  margin: 4px 0;
}
.readonly-block {
  margin: 8px 0;
}
.readonly-line {
  margin: 4px 0;
  color: var(--el-text-color-secondary);
}
.scope-picker {
  margin-bottom: 12px;
}
.scope-code {
  margin-left: 8px;
  color: var(--el-text-color-placeholder);
  font-size: 12px;
}
.preview {
  margin: 12px 0;
}
.preview p {
  margin: 4px 0;
  word-break: break-all;
}
.danger {
  color: var(--el-color-danger);
}
.self-grant-ack {
  width: 100%;
  margin-top: 8px;
  color: var(--el-color-warning);
  font-weight: 600;
}
</style>
