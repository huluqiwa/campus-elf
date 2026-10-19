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
  <div class="page">
    <h1>校园陪伴精灵 · C 端 H5 脚手架</h1>
    <p>AI 对话、知识库问答、请假与消息将在后续里程碑接入。</p>
    <van-cell-group inset>
      <van-cell title="后端健康" :value="health" />
    </van-cell-group>
  </div>
</template>

<style scoped>
.page {
  padding: 24px 16px;
  font-family:
    system-ui,
    -apple-system,
    "Segoe UI",
    sans-serif;
}
h1 {
  font-size: 20px;
  margin-bottom: 8px;
}
p {
  color: #666;
  margin-bottom: 24px;
}
</style>
