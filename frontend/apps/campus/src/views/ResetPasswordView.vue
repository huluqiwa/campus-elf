<script setup lang="ts">
/** C 端重置密码（M1-D1）：邮件链接携带单次令牌（fragment），成功后必须重新登录。 */
import { passwordPolicyError, toUserMessage } from "@campus-elf/api-client";
import { showToast } from "vant";
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
    showToast("密码已重置，请使用新密码重新登录");
    await router.replace({ name: "login" });
  } catch (error) {
    errorMessage.value = toUserMessage(error, "重置失败，请重新申请或稍后重试");
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="campus-page">
    <h1>重置密码</h1>
    <p class="hint">请设置新密码，提交后需要使用新密码重新登录。</p>

    <van-form @submit="submit">
      <van-cell-group inset>
        <van-field
          v-model="form.newPassword"
          type="password"
          label="新密码"
          placeholder="至少 10 位"
          data-testid="reset-new-password"
        />
        <van-field
          v-model="form.confirmPassword"
          type="password"
          label="确认新密码"
          data-testid="reset-confirm-password"
        />
      </van-cell-group>
      <p v-if="errorMessage" class="error" role="alert" data-testid="reset-confirm-error">
        {{ errorMessage }}
      </p>
      <div class="actions">
        <van-button
          round
          block
          type="primary"
          native-type="submit"
          :loading="submitting"
          :disabled="!token"
          data-testid="reset-confirm-submit"
        >
          提交
        </van-button>
      </div>
    </van-form>

    <div class="actions">
      <router-link class="back" :to="{ name: 'login' }" data-testid="back-to-login">
        返回登录
      </router-link>
    </div>
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
  color: #666;
  margin-bottom: 16px;
  padding: 0 24px;
}
.actions {
  padding: 16px;
}
.error {
  color: #d43030;
  text-align: center;
  margin: 8px 0 0;
}
.back {
  display: block;
  text-align: center;
  color: #1989fa;
  text-decoration: none;
  font-size: 14px;
}
</style>
