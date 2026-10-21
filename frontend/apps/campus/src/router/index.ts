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

/** 与 admin 端一致的守卫：Cookie 静默恢复 → 未登录进登录页 → 首登未改密强制改密。 */
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
