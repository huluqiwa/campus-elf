import { createRouter, createWebHistory } from "vue-router";

import { useAuthStore } from "../stores/auth";
import ChangePasswordView from "../views/ChangePasswordView.vue";
import HomeView from "../views/HomeView.vue";
import LoginView from "../views/LoginView.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", name: "login", component: LoginView },
    {
      path: "/change-password",
      name: "change-password",
      component: ChangePasswordView,
      meta: { requiresAuth: true },
    },
    { path: "/", name: "home", component: HomeView, meta: { requiresAuth: true } },
  ],
});

/**
 * 路由守卫（M1-C-UI）：
 * - 页面刷新后访问受保护路由时，先用 HttpOnly Cookie 静默恢复一次会话；
 * - 未登录 → 登录页；首登未改密 → 强制改密页（改密/登出之外都拦）；已登录访问登录页 → 首页。
 */
router.beforeEach(async (to) => {
  const auth = useAuthStore();
  if (!auth.restored) {
    await auth.restore();
  }
  if (to.meta.requiresAuth && !auth.isAuthenticated) {
    return { name: "login" };
  }
  if (auth.isAuthenticated && auth.mustChangePassword && to.name !== "change-password") {
    return { name: "change-password" };
  }
  if (to.name === "login" && auth.isAuthenticated) {
    return { name: "home" };
  }
  return true;
});
