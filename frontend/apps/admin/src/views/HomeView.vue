<script setup lang="ts">
import { createApiClient } from "@campus-elf/api-client";
import { onMounted, ref } from "vue";

const health = ref("检测中…");
// 默认空 baseUrl = 同源相对路径，由 Vite/Nginx 代理 /health 与 /api 到后端。
const api = createApiClient({
  baseUrl: import.meta.env.VITE_API_BASE_URL ?? "",
});

onMounted(async () => {
  try {
    const data = await api.getHealth();
    health.value = `${data.service} ${data.version} (${data.env})`;
  } catch {
    health.value = "后端未连接（M0 脚手架）";
  }
});
</script>

<template>
  <main class="page">
    <h1>校园陪伴精灵 · 管理端脚手架</h1>
    <p>平台方与校方后台路由将在 M1 起逐步接入。</p>
    <el-card shadow="never">
      <template #header>后端健康检查</template>
      <span>{{ health }}</span>
    </el-card>
  </main>
</template>

<style scoped>
.page {
  max-width: 960px;
  margin: 0 auto;
  padding: 48px 24px;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
h1 {
  font-size: 24px;
  margin-bottom: 8px;
}
p {
  color: #666;
  margin-bottom: 24px;
}
</style>
