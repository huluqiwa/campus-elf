import { createRouter, createWebHistory } from "vue-router";

import { useAuthStore } from "../stores/auth";
import ChangePasswordView from "../views/ChangePasswordView.vue";
import ForgotPasswordView from "../views/ForgotPasswordView.vue";
import HomeView from "../views/HomeView.vue";
import LoginView from "../views/LoginView.vue";
import ResetPasswordView from "../views/ResetPasswordView.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", name: "login", component: LoginView },
    // 重置相关页面公开访问（邮件链接通常来自未登录状态）
    { path: "/forgot-password", name: "forgot-password", component: ForgotPasswordView },
    { path: "/reset-password", name: "reset-password", component: ResetPasswordView },
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
 * - 未登录 → 登录页；首登未改密 → 强制改密页（公开的重置页仍放行）；
 * - 已登录访问登录页 → 首页。
 */
const PUBLIC_WHILE_MUST_CHANGE = new Set(["change-password", "forgot-password", "reset-password"]);

router.beforeEach(async (to) => {
  const auth = useAuthStore();
  if (!auth.restored) {
    await auth.restore();
  }
  if (to.meta.requiresAuth && !auth.isAuthenticated) {
    return { name: "login" };
  }
  if (
    auth.isAuthenticated &&
    auth.mustChangePassword &&
    !PUBLIC_WHILE_MUST_CHANGE.has(String(to.name ?? ""))
  ) {
    return { name: "change-password" };
  }
  if (to.name === "login" && auth.isAuthenticated) {
    return { name: "home" };
  }
  return true;
});
