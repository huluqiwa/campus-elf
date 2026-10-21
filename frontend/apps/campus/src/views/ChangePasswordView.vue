<script setup lang="ts">
/** C 端强制改密页：首登必须改密后才能使用其他功能。 */
import { passwordPolicyError, toUserMessage } from "@campus-elf/api-client";
import { showToast } from "vant";
import { reactive, ref } from "vue";
import { useRouter } from "vue-router";

import { useAuthStore } from "../stores/auth";

const router = useRouter();
const auth = useAuthStore();
const submitting = ref(false);
const errorMessage = ref("");
const form = reactive({ currentPassword: "", newPassword: "", confirmPassword: "" });

async function submit(): Promise<void> {
  if (submitting.value) return;
  errorMessage.value = "";
  if (!form.currentPassword || !form.newPassword) {
    errorMessage.value = "请填写当前密码与新密码";
    return;
  }
  if (form.newPassword !== form.confirmPassword) {
    errorMessage.value = "两次输入的新密码不一致";
    return;
  }
  const policy = passwordPolicyError(form.newPassword);
  if (policy) {
    errorMessage.value = policy;
    return;
  }
  submitting.value = true;
  try {
    await auth.changePassword(form.currentPassword, form.newPassword);
    showToast("密码已更新");
    await router.replace({ name: "home" });
  } catch (error) {
    errorMessage.value = toUserMessage(error, "修改失败，请检查网络后重试");
  } finally {
    submitting.value = false;
  }
}

async function logout(): Promise<void> {
  await auth.logout();
  await router.replace({ name: "login" });
}
</script>

<template>
  <div class="campus-page">
    <h1>修改密码</h1>
    <p v-if="auth.mustChangePassword" class="hint" data-testid="force-hint">首次登录必须修改密码</p>
    <van-form @submit="submit">
      <van-cell-group inset>
        <van-field
          v-model="form.currentPassword"
          type="password"
          label="当前密码"
          data-testid="current-password"
        />
        <van-field
          v-model="form.newPassword"
          type="password"
          label="新密码"
          placeholder="至少 10 位"
          data-testid="new-password"
        />
        <van-field
          v-model="form.confirmPassword"
          type="password"
          label="确认新密码"
          data-testid="confirm-password"
        />
      </van-cell-group>
      <p v-if="errorMessage" class="error" role="alert" data-testid="change-error">
        {{ errorMessage }}
      </p>
      <div class="actions">
        <van-button
          round
          block
          type="primary"
          native-type="submit"
          :loading="submitting"
          data-testid="change-submit"
        >
          提交
        </van-button>
        <van-button round block plain class="logout" data-testid="change-logout" @click="logout">
          退出登录
        </van-button>
      </div>
    </van-form>
  </div>
</template>

<style scoped>
.campus-page {
  padding: 48px 0;
}
h1 {
  text-align: center;
  font-size: 20px;
  margin-bottom: 12px;
}
.hint {
  text-align: center;
  color: #b88230;
  margin-bottom: 8px;
}
.actions {
  padding: 16px;
}
.logout {
  margin-top: 12px;
}
.error {
  color: #d43030;
  text-align: center;
  margin: 8px 0 0;
}
</style>
