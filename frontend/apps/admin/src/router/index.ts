import { createRouter, createWebHistory } from "vue-router";

import { useAuthStore } from "../stores/auth";
import AdminLayout from "../views/AdminLayout.vue";
import ChangePasswordView from "../views/ChangePasswordView.vue";
import ClassesView from "../views/ClassesView.vue";
import DepartmentsView from "../views/DepartmentsView.vue";
import ForbiddenView from "../views/ForbiddenView.vue";
import ForgotPasswordView from "../views/ForgotPasswordView.vue";
import HomeView from "../views/HomeView.vue";
import ImportsView from "../views/ImportsView.vue";
import KbDocumentsView from "../views/KbDocumentsView.vue";
import KbUploadView from "../views/KbUploadView.vue";
import LoginView from "../views/LoginView.vue";
import MfaEnrollView from "../views/MfaEnrollView.vue";
import PeopleView from "../views/PeopleView.vue";
import PermissionAccountsView from "../views/PermissionAccountsView.vue";
import ResetPasswordView from "../views/ResetPasswordView.vue";
import RoleSettingsView from "../views/RoleSettingsView.vue";
import RolloverView from "../views/RolloverView.vue";
import SecurityView from "../views/SecurityView.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", name: "login", component: LoginView },
    // 重置相关页面公开访问（邮件链接通常来自未登录状态）
    { path: "/forgot-password", name: "forgot-password", component: ForgotPasswordView },
    { path: "/reset-password", name: "reset-password", component: ResetPasswordView },
    // 强制绑定页：只有登录给出的一次性绑定令牌，没有会话，因此不要求登录态
    { path: "/mfa/enroll", name: "mfa-enroll", component: MfaEnrollView },
    {
      path: "/change-password",
      name: "change-password",
      component: ChangePasswordView,
      meta: { requiresAuth: true },
    },
    {
      path: "/security",
      name: "security",
      component: SecurityView,
      meta: { requiresAuth: true },
    },
    {
      // 业务页面共用后台布局（M2-UI1）：children 的 meta.permission 由守卫与菜单共同消费
      path: "/",
      component: AdminLayout,
      meta: { requiresAuth: true },
      children: [
        { path: "", name: "home", component: HomeView },
        {
          path: "people",
          name: "people",
          component: PeopleView,
          meta: { requiresAuth: true, permission: "school.personnel.view" },
        },
        {
          path: "classes",
          name: "classes",
          component: ClassesView,
          meta: { requiresAuth: true, permission: "school.personnel.view" },
        },
        {
          path: "departments",
          name: "departments",
          component: DepartmentsView,
          meta: { requiresAuth: true, permission: "school.personnel.view" },
        },
        {
          // M3 KB-01B：知识库列表与上传是两个权限码，各自一条路由
          path: "kb/documents",
          name: "kb-documents",
          component: KbDocumentsView,
          meta: { requiresAuth: true, permission: "school.kb.view" },
        },
        {
          path: "kb/upload",
          name: "kb-upload",
          component: KbUploadView,
          meta: { requiresAuth: true, permission: "school.kb.upload" },
        },
        {
          path: "imports",
          name: "imports",
          component: ImportsView,
          meta: { requiresAuth: true, permission: "school.personnel.import" },
        },
        {
          path: "rollover",
          name: "rollover",
          component: RolloverView,
          meta: { requiresAuth: true, permission: "school.org.manage" },
        },
        {
          path: "settings/roles",
          name: "role-settings",
          component: RoleSettingsView,
          meta: { requiresAuth: true, permission: "school.role.manage" },
        },
        {
          // TD-187g-C：权限账号页（改绑 / 启停 / MFA 重置）
          path: "settings/accounts",
          name: "permission-accounts",
          component: PermissionAccountsView,
          meta: { requiresAuth: true, permission: "school.permission_account.manage" },
        },
      ],
    },
    {
      path: "/forbidden",
      name: "forbidden",
      component: ForbiddenView,
      meta: { requiresAuth: true },
    },
  ],
});

/**
 * 路由守卫（M1-C-UI + M1-D2 + M2-UI1/TD-141）：
 * - 页面刷新后访问受保护路由时，先用 HttpOnly Cookie 静默恢复一次会话；
 * - 未登录 → 登录页；首登未改密 → 强制改密页（公开的重置页仍放行）；
 * - 策略要求绑定但未绑定 → 强制绑定页（绑定完成前不给进后台）；
 * - 已登录访问登录页 → 首页；
 * - 带 meta.permission 的路由：库值权限不足 → 403 页（菜单只是体验，
 *   这里是第二道体验层；真正的边界是后端逐请求 403）。
 */
const PUBLIC_WHILE_MUST_CHANGE = new Set([
  "change-password",
  "forgot-password",
  "reset-password",
  "mfa-enroll",
]);
const PUBLIC_WHILE_MFA_ENROLL = new Set([
  "mfa-enroll",
  "login",
  "forgot-password",
  "reset-password",
]);

router.beforeEach(async (to) => {
  const auth = useAuthStore();
  if (!auth.restored) {
    await auth.restore();
  }
  if (to.meta.requiresAuth && !auth.isAuthenticated) {
    return { name: "login" };
  }
  const name = String(to.name ?? "");
  if (auth.mustEnrollMfa && !PUBLIC_WHILE_MFA_ENROLL.has(name)) {
    return { name: "mfa-enroll" };
  }
  if (auth.isAuthenticated && auth.mustChangePassword && !PUBLIC_WHILE_MUST_CHANGE.has(name)) {
    return { name: "change-password" };
  }
  if (to.name === "login" && auth.isAuthenticated) {
    return { name: "home" };
  }
  const permission = to.meta.permission as string | undefined;
  if (permission !== undefined && !auth.user?.permissions.includes(permission)) {
    return { name: "forbidden" };
  }
  return true;
});
