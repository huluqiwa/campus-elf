<script setup lang="ts">
/** 强制改密页（首登必须改密，或用户主动修改）。成功后回到首页。 */
import { passwordPolicyError, toUserMessage } from "@campus-elf/api-client";
import { ElMessage } from "element-plus";
import { reactive, ref } from "vue";
import { useRouter } from "vue-router";

import { useAuthStore } from "../stores/auth";

const router = useRouter();
const auth = useAuthStore();

const form = reactive({ currentPassword: "", newPassword: "", confirmPassword: "" });
const submitting = ref(false);
const errorMessage = ref("");

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
    ElMessage.success("密码已更新");
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
  <main class="change-page">
    <h1>修改密码</h1>
    <p v-if="auth.mustChangePassword" class="hint" data-testid="force-hint">
      首次登录必须修改密码后才能使用其他功能。
    </p>
    <el-card shadow="never">
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="当前密码">
          <el-input
            v-model="form.currentPassword"
            type="password"
            show-password
            data-testid="current-password"
          />
        </el-form-item>
        <el-form-item label="新密码（至少 10 位）">
          <el-input
            v-model="form.newPassword"
            type="password"
            show-password
            data-testid="new-password"
          />
        </el-form-item>
        <el-form-item label="确认新密码">
          <el-input
            v-model="form.confirmPassword"
            type="password"
            show-password
            data-testid="confirm-password"
          />
        </el-form-item>
        <p v-if="errorMessage" class="error" role="alert" data-testid="change-error">
          {{ errorMessage }}
        </p>
        <el-button
          type="primary"
          class="submit"
          :loading="submitting"
          data-testid="change-submit"
          @click="submit"
        >
          提交
        </el-button>
        <el-button class="submit" data-testid="change-logout" @click="logout">退出登录</el-button>
      </el-form>
    </el-card>
  </main>
</template>

<style scoped>
.change-page {
  max-width: 420px;
  margin: 0 auto;
  padding: 64px 24px;
}
.hint {
  color: #b88230;
}
.error {
  color: var(--el-color-danger);
  margin: 0 0 8px;
}
.submit {
  width: 100%;
  margin-left: 0;
  margin-bottom: 8px;
}
</style>
