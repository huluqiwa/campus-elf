<script setup lang="ts">
import { createApiClient } from "@campus-elf/api-client";
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import { useAuthStore } from "../stores/auth";

const router = useRouter();
const auth = useAuthStore();
const health = ref("检测中…");
// 默认空 baseUrl = 同源相对路径，由 Vite/Nginx 代理 /health 与 /api 到后端。
const api = createApiClient({
  baseUrl: import.meta.env.VITE_API_BASE_URL ?? "",
});

const identity = computed(() => {
  if (!auth.user) return "";
  const realm = auth.user.realm === "platform" ? "平台运营" : "校方";
  const roles = auth.user.roles.length > 0 ? auth.user.roles.join("、") : "无角色";
  return `${realm} · ${roles}`;
});

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
  <main class="page">
    <h1>校园陪伴精灵 · 管理后台</h1>
    <p data-testid="identity">当前身份：{{ identity }}</p>
    <el-card shadow="never">
      <template #header>后端健康检查</template>
      <span>{{ health }}</span>
    </el-card>
    <div class="actions">
      <el-button data-testid="go-change-password" @click="router.push({ name: 'change-password' })">
        修改密码
      </el-button>
      <el-button type="primary" data-testid="logout" @click="logout">退出登录</el-button>
    </div>
  </main>
</template>

<style scoped>
.page {
  max-width: 960px;
  margin: 0 auto;
  padding: 48px 24px;
  font-family:
    system-ui,
    -apple-system,
    "Segoe UI",
    sans-serif;
}
h1 {
  font-size: 24px;
  margin-bottom: 8px;
}
p {
  color: #666;
  margin-bottom: 24px;
}
.actions {
  margin-top: 24px;
  display: flex;
  gap: 12px;
}
</style>
