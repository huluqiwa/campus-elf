<script setup lang="ts">
/**
 * 校管后台布局（M2-UI1 D3-A + TD-141）：
 * - 侧边菜单按 /auth/me 的 permissions 渲染——无权限的入口直接不出现
 *   （菜单只是体验；直连 URL 由路由守卫挡到 403 页，真正的边界是后端 403）；
 * - 平台运营账号没有任何 school.* 权限，本布局对其显示空菜单。
 */
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";

import { useAuthStore } from "../stores/auth";

interface MenuItem {
  index: string;
  title: string;
  permission: string;
}

const MENU: MenuItem[] = [
  { index: "/people", title: "人员管理", permission: "school.personnel.view" },
  { index: "/classes", title: "班级与课程", permission: "school.personnel.view" },
  { index: "/departments", title: "部门树", permission: "school.personnel.view" },
  { index: "/imports", title: "批量导入", permission: "school.personnel.import" },
  { index: "/rollover", title: "学年流转", permission: "school.org.manage" },
  { index: "/settings/roles", title: "角色权限", permission: "school.role.manage" },
  {
    index: "/settings/accounts",
    title: "权限账号",
    permission: "school.permission_account.manage",
  },
];

const auth = useAuthStore();
const route = useRoute();
const router = useRouter();

const permissions = computed(() => new Set(auth.user?.permissions ?? []));
const visibleMenu = computed(() => MENU.filter((item) => permissions.value.has(item.permission)));
const activeIndex = computed(() => route.path);

const identity = computed(() => {
  if (!auth.user) return "";
  const roles = auth.user.roles.length > 0 ? auth.user.roles.join("、") : "无角色";
  return `${roles}`;
});

async function onLogout(): Promise<void> {
  await auth.logout();
  await router.replace({ name: "login" });
}
</script>

<template>
  <el-container class="layout">
    <el-aside width="220px" class="aside">
      <div class="brand">校园陪伴精灵 · 校管后台</div>
      <el-menu :default-active="activeIndex" router>
        <el-menu-item v-for="item in visibleMenu" :key="item.index" :index="item.index">
          {{ item.title }}
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="header">
        <span class="identity">{{ identity }}</span>
        <el-button link type="danger" @click="onLogout">退出登录</el-button>
      </el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.layout {
  min-height: 100vh;
}
.aside {
  border-right: 1px solid var(--el-border-color-light);
}
.brand {
  padding: 16px;
  font-weight: 600;
  color: var(--el-color-primary);
}
.header {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  border-bottom: 1px solid var(--el-border-color-light);
}
.identity {
  color: var(--el-text-color-secondary);
}
</style>
