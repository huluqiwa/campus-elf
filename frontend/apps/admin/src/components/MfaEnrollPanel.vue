<script setup lang="ts">
/**
 * 绑定验证器面板（M1-D2）：取回 secret → 展示二维码/密钥 → 输 6 位码激活 → 一次性恢复码。
 *
 * `mode="forced"` 用于登录给出的绑定令牌（此时还没有会话）；
 * `mode="self"` 用于已登录用户自助绑定（需带当前密码）。
 * secret 与恢复码都只留在内存，组件卸载即消失，不写 localStorage。
 */
import type { MfaEnrollment } from "@campus-elf/api-client";
import { toUserMessage } from "@campus-elf/api-client";
import { ElMessage } from "element-plus";
import { reactive, ref } from "vue";

import { useAuthStore } from "../stores/auth";
import MfaQrCode from "./MfaQrCode.vue";

const props = defineProps<{
  mode: "forced" | "self";
  /** self 模式必填：绑定前重新验证的当前密码。 */
  currentPassword?: string;
}>();

const emit = defineEmits<{ activated: [] }>();

const auth = useAuthStore();
const state = reactive<{ step: "bind" | "codes"; enrollment: MfaEnrollment | null }>({
  step: "bind",
  enrollment: null,
});
const code = ref("");
const errorMessage = ref("");
const submitting = ref(false);
const recoveryCodes = ref<string[]>([]);

async function start(): Promise<void> {
  errorMessage.value = "";
  submitting.value = true;
  try {
    state.enrollment =
      props.mode === "forced"
        ? await auth.enrollWithToken()
        : await auth.enrollWithPassword(props.currentPassword ?? "");
  } catch (error) {
    errorMessage.value = toUserMessage(error, "无法开始绑定，请稍后重试");
    ElMessage.error(errorMessage.value);
  } finally {
    submitting.value = false;
  }
}

async function activate(): Promise<void> {
  if (!code.value.trim()) {
    errorMessage.value = "请输入验证器上的 6 位动态码";
    return;
  }
  errorMessage.value = "";
  submitting.value = true;
  try {
    recoveryCodes.value =
      props.mode === "forced"
        ? await auth.activateWithToken(code.value.trim())
        : await auth.activateWithPassword(code.value.trim());
    state.step = "codes";
    emit("activated");
  } catch (error) {
    errorMessage.value = toUserMessage(error, "动态码不正确，请重试");
    ElMessage.error(errorMessage.value);
  } finally {
    submitting.value = false;
  }
}

async function copyCodes(): Promise<void> {
  await navigator.clipboard.writeText(recoveryCodes.value.join("\n"));
  ElMessage.success("恢复码已复制到剪贴板，请妥善保存后离开本页");
}

function reset(): void {
  state.step = "bind";
  state.enrollment = null;
  code.value = "";
  recoveryCodes.value = [];
  errorMessage.value = "";
}

// 面板挂载即取密钥：self 模式由父组件在拿到密码后再调用 start()
if (props.mode === "forced") {
  void start();
}

defineExpose({ start, reset });
</script>

<template>
  <div class="mfa-panel">
    <template v-if="state.step === 'bind'">
      <template v-if="state.enrollment">
        <p class="hint">
          用验证器 App（如 Google Authenticator）扫描下面的二维码，或手动输入密钥； 然后在
          {{ Math.round(state.enrollment.expiresIn / 60) }} 分钟内完成激活。
        </p>
        <MfaQrCode :value="state.enrollment.otpauthUri" />
        <p class="secret" data-testid="mfa-secret">{{ state.enrollment.secret }}</p>
        <el-form label-position="top" @submit.prevent="activate">
          <el-form-item label="验证器动态码">
            <el-input
              v-model="code"
              placeholder="123456"
              autocomplete="one-time-code"
              data-testid="mfa-activate-code"
              @keyup.enter="activate"
            />
          </el-form-item>
        </el-form>
      </template>
      <el-button v-else type="primary" :loading="submitting" data-testid="mfa-start" @click="start">
        生成绑定二维码
      </el-button>

      <p v-if="errorMessage" class="error" role="alert" data-testid="mfa-error">
        {{ errorMessage }}
      </p>

      <el-button
        v-if="state.enrollment"
        type="primary"
        :loading="submitting"
        data-testid="mfa-activate-submit"
        @click="activate"
      >
        激活
      </el-button>
      <el-button v-if="state.enrollment" text data-testid="mfa-restart" @click="start">
        重新生成
      </el-button>
    </template>

    <template v-else>
      <h3 data-testid="mfa-recovery-title">一次性恢复码（只显示这一次）</h3>
      <p class="hint">手机丢了或验证器不可用时用恢复码登录；每个只能用一次，请离线保存。</p>
      <ol class="codes">
        <li v-for="item in recoveryCodes" :key="item" data-testid="mfa-recovery-code">
          {{ item }}
        </li>
      </ol>
      <el-button type="primary" data-testid="mfa-copy-codes" @click="copyCodes">
        复制全部恢复码
      </el-button>
    </template>
  </div>
</template>

<style scoped>
.hint {
  color: var(--el-text-color-secondary);
  font-size: 14px;
  margin: 0 0 8px;
}
.secret {
  font-family: ui-monospace, Menlo, Consolas, monospace;
  text-align: center;
  letter-spacing: 1px;
  margin: 0 0 12px;
  user-select: all;
}
.codes {
  columns: 2;
  gap: 24px;
  font-family: ui-monospace, Menlo, Consolas, monospace;
  margin: 0 0 12px;
  padding-left: 24px;
  user-select: all;
}
.error {
  color: var(--el-color-danger);
  margin: 0 0 8px;
}
</style>
