<script setup lang="ts">
/**
 * 强制绑定页（M1-D2）：平台策略要求必须绑定验证器，但账号还没绑定时使用。
 * 此时只持有登录给出的一次性绑定令牌，没有任何会话，因此本页不要求登录态。
 */
import { ref } from "vue";
import { useRouter } from "vue-router";

import MfaEnrollPanel from "../components/MfaEnrollPanel.vue";
import { useAuthStore } from "../stores/auth";

const auth = useAuthStore();
const router = useRouter();

// 绑定令牌在激活过程中会被清空；这里在挂载时捕获一次，避免面板在 await 中途被卸载。
const canEnroll = ref(auth.enrollToken !== null);
/** 激活成功后停留在本页，直到用户确认已保存一次性恢复码才跳转。 */
const activated = ref(false);

async function finish(): Promise<void> {
  if (auth.mustChangePassword) {
    await router.replace({ name: "change-password" });
    return;
  }
  await router.replace({ name: "home" });
}

async function backToLogin(): Promise<void> {
  auth.clear();
  await router.replace({ name: "login" });
}
</script>

<template>
  <main class="enroll-page">
    <h1>需要绑定验证器</h1>
    <el-card shadow="never" class="enroll-card">
      <template v-if="canEnroll">
        <p v-if="!activated" class="hint">
          平台要求运营账号启用两步验证：先绑定验证器，绑定完成后才能进入后台。
        </p>
        <!-- 激活后面板留在原地显示一次性恢复码，确认保存后才跳转 -->
        <MfaEnrollPanel mode="forced" @activated="activated = true" />
        <el-button v-if="activated" type="primary" data-testid="mfa-enroll-finish" @click="finish">
          我已保存恢复码，进入后台
        </el-button>
      </template>
      <template v-else>
        <p class="hint" data-testid="mfa-enroll-expired">绑定链接已过期或无效，请重新登录。</p>
      </template>
      <el-button v-if="!activated" text data-testid="mfa-enroll-back" @click="backToLogin">
        返回登录
      </el-button>
    </el-card>
  </main>
</template>

<style scoped>
.enroll-page {
  max-width: 460px;
  margin: 0 auto;
  padding: 64px 24px;
}
.enroll-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.hint {
  color: var(--el-text-color-secondary);
  font-size: 14px;
  margin: 0 0 8px;
}
</style>
