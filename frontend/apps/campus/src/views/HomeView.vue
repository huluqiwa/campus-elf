<script setup lang="ts">
import { createApiClient } from "@campus-elf/api-client";
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import { useAuthStore } from "../stores/auth";

const router = useRouter();
const auth = useAuthStore();
const health = ref("检测中…");
const api = createApiClient({ baseUrl: import.meta.env.VITE_API_BASE_URL ?? "" });

onMounted(async () => {
  try {
    const data = await api.getHealth();
    health.value = `${data.service} ${data.version} (${data.env})`;
  } catch {
    health.value = "后端未连接（M0 脚手架）";
  }
});

async function logout(): Promise<void> {
  await auth.logout();
  await router.replace({ name: "login" });
}
</script>

<template>
  <div class="campus-page">
    <van-nav-bar title="校园陪伴精灵" />
    <h1>C 端 H5 脚手架</h1>
    <van-cell-group inset>
      <van-cell title="后端健康" :value="health" />
      <van-cell title="当前用户" :value="auth.user?.userId ?? '-'" />
    </van-cell-group>
    <div class="actions">
      <van-button
        round
        block
        plain
        data-testid="go-change-password"
        @click="router.push({ name: 'change-password' })"
      >
        修改密码
      </van-button>
      <van-button round block type="primary" class="logout" data-testid="logout" @click="logout">
        退出登录
      </van-button>
    </div>
  </div>
</template>

<style scoped>
.campus-page {
  padding-bottom: 32px;
}
h1 {
  text-align: center;
  font-size: 18px;
  margin: 16px 0;
}
.actions {
  padding: 16px;
}
.logout {
  margin-top: 12px;
}
</style>
