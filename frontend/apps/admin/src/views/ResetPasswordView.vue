<script setup lang="ts">
/** 重置密码（M1-D1）：从邮件链接进入，校验 fragment 里的单次令牌。 */
import { passwordPolicyError, toUserMessage } from "@campus-elf/api-client";
import { ElMessage } from "element-plus";
import { reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { session } from "../session";
import { useAuthStore } from "../stores/auth";

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

const token = new URLSearchParams(route.hash.replace(/^#/, "")).get("token") ?? "";
const submitting = ref(false);
const errorMessage = ref(token ? "" : "重置链接缺少令牌，请重新申请");
const form = reactive({ newPassword: "", confirmPassword: "" });

async function submit(): Promise<void> {
  if (submitting.value || !token) return;
  errorMessage.value = "";
  if (!form.newPassword) {
    errorMessage.value = "请输入新密码";
    return;
  }
  const policy = passwordPolicyError(form.newPassword);
  if (policy) {
    errorMessage.value = policy;
    return;
  }
  if (form.newPassword !== form.confirmPassword) {
    errorMessage.value = "两次输入的新密码不一致";
    return;
  }

  submitting.value = true;
  try {
    await session.confirmPasswordReset(token, form.newPassword);
    // 重置会吊销全部会话；清掉内存令牌，避免守卫把登录页当成已登录跳回首页
    auth.clear();
    ElMessage.success("密码已重置，请使用新密码重新登录");
    await router.replace({ name: "login" });
  } catch (error) {
    errorMessage.value = toUserMessage(error, "重置失败，请重新申请或稍后重试");
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <main class="reset-page">
    <h1>重置密码</h1>
    <el-card shadow="never">
      <p class="hint">请设置新密码，提交后需要使用新密码重新登录。</p>
      <el-form :model="form" label-position="top" @submit.prevent="submit">
        <el-form-item label="新密码（至少 10 位）">
          <el-input
            v-model="form.newPassword"
            type="password"
            show-password
            data-testid="reset-new-password"
          />
        </el-form-item>
        <el-form-item label="确认新密码">
          <el-input
            v-model="form.confirmPassword"
            type="password"
            show-password
            data-testid="reset-confirm-password"
          />
        </el-form-item>
        <p v-if="errorMessage" class="error" role="alert" data-testid="reset-confirm-error">
          {{ errorMessage }}
        </p>
        <el-button
          type="primary"
          class="submit"
          :loading="submitting"
          :disabled="!token"
          data-testid="reset-confirm-submit"
          @click="submit"
        >
          提交
        </el-button>
        <router-link class="back" :to="{ name: 'login' }" data-testid="back-to-login">
          返回登录
        </router-link>
      </el-form>
    </el-card>
  </main>
</template>

<style scoped>
.reset-page {
  max-width: 420px;
  margin: 0 auto;
  padding: 64px 24px;
}
h1 {
  text-align: center;
  font-size: 20px;
  margin-bottom: 24px;
}
.hint {
  color: #666;
  margin: 0 0 16px;
}
.submit {
  width: 100%;
}
.error {
  color: var(--el-color-danger);
  margin: 0 0 8px;
}
.back {
  display: inline-block;
  margin-top: 12px;
  color: var(--el-color-primary);
  text-decoration: none;
  font-size: 14px;
}
</style>
