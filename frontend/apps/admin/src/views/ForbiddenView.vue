<script setup lang="ts">
/** 403 页（TD-141）：前端菜单只是体验，直连无权限路由由守卫送到这里；真正的边界是后端 403。 */

import { useRouter } from "vue-router";

const router = useRouter();
</script>

<template>
  <el-result icon="warning" title="403" sub-title="你没有访问该页面的权限">
    <template #extra>
      <el-button type="primary" @click="router.replace({ name: 'home' })">返回首页</el-button>
    </template>
  </el-result>
</template>
