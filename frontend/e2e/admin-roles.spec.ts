import { expect, test } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";
import { mockRbacApi, SCHOOL_ADMIN_DEFAULTS } from "./helpers/rbac-mock";

// M2-UI2 角色权限配置（接口 mock）：只读角色、全量保存、版本冲突、恢复出厂、MFA 重置。

const ROLE_MANAGER = ["school.role.manage"];
const ROLE_AND_MFA = ["school.role.manage", "school.personnel.mfa.reset"];

async function confirmMessageBox(page: import("@playwright/test").Page): Promise<void> {
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
}

test("锁定角色只读并给出原因；可编辑角色勾选后保存并版本号前进", async ({ page }) => {
  const rbac = await mockRbacApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_AND_MFA } });

  await page.goto("/settings/roles");
  await expect(page.getByTestId("role-list")).toBeVisible();

  // 锁定角色：勾选禁用 + 原因透出 + 无保存/重置入口
  await page.getByTestId("role-school_super_admin").click();
  await expect(page.getByTestId("role-lock-reason")).toBeVisible();
  await expect(page.getByTestId("perm-school.role.manage").locator("input")).toBeDisabled();
  await expect(page.getByTestId("role-save")).toHaveCount(0);

  // 可编辑角色：勾一项 → 保存按钮显示数量 → 二次确认后落库
  await page.getByTestId("role-school_admin").click();
  await expect(page.getByTestId("perm-school.dashboard.view").locator("input")).toBeChecked();
  await page.getByTestId("perm-school.personnel.view").click();
  await page.getByTestId("perm-school.role.manage").click();
  await expect(page.getByTestId("role-save")).toContainText("新增 2");
  await page.getByTestId("role-save-reason").fill("按岗位需要补充人员查看与角色配置权限");
  await page.getByTestId("role-save").click();
  await confirmMessageBox(page);

  await expect(page.getByText("已保存：新增 2 项、移除 0 项")).toBeVisible();
  expect(rbac.captures.savePermissions).toHaveLength(1);
  expect(rbac.captures.savePermissions[0]).toMatchObject({
    version: 3,
    reason: "按岗位需要补充人员查看与角色配置权限",
  });
  expect(rbac.captures.savePermissions[0]?.permission_codes).toEqual([
    "school.dashboard.view",
    "school.personnel.view",
    "school.role.manage",
  ]);
  // 保存返回的角色快照直接回写界面：版本前进、无未保存变更
  await expect(page.getByText("版本 4")).toBeVisible();
  await expect(page.getByTestId("role-save")).toBeDisabled();
});

test("保存遇到 409 版本冲突：载入后端回传的最新版本，不静默覆盖", async ({ page }) => {
  const rbac = await mockRbacApi(page, {
    saveResponse: {
      status: 409,
      body: {
        detail: {
          code: "ROLE_VERSION_CONFLICT",
          message: "角色配置已被他人修改，请刷新后重试",
          latest: {
            id: "40000000-0000-0000-0000-000000000002",
            code: "school_admin",
            name: "校方授权管理员",
            is_system: true,
            editable: true,
            lock_reason: null,
            version: 7,
            defaults_applied: true,
            user_count: 3,
            permission_codes: ["school.dashboard.view", "school.kb.view"],
          },
        },
      },
    },
  });
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER } });

  await page.goto("/settings/roles");
  await page.getByTestId("role-school_admin").click();
  await page.getByTestId("perm-school.role.manage").click();
  await page.getByTestId("role-save").click();
  await confirmMessageBox(page);

  await expect(page.getByText("角色配置已被他人修改")).toBeVisible();
  await expect(page.getByText("版本 7")).toBeVisible();
  // 采用 latest 后草稿与最新一致：不保留被拒的勾选，避免"看起来保存了"
  await expect(page.getByTestId("perm-school.role.manage").locator("input")).not.toBeChecked();
  await expect(page.getByTestId("role-save")).toBeDisabled();
  expect(rbac.captures.savePermissions).toHaveLength(1);
});

test("恢复出厂：先预览影响面，编码/理由任一不合格都不提交", async ({ page }) => {
  const rbac = await mockRbacApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER } });

  await page.goto("/settings/roles");
  await page.getByTestId("role-school_admin").click();
  await page.getByTestId("role-reset").click();

  await expect(page.getByTestId("reset-preview")).toContainText("影响用户：2 人");
  await expect(page.getByTestId("reset-preview")).toContainText(SCHOOL_ADMIN_DEFAULTS[1] ?? "");

  await page.getByTestId("reset-code").fill("school-admin");
  await page.getByTestId("reset-reason").fill("误改角色权限，需要恢复出厂默认配置");
  await page.getByTestId("reset-submit").click();
  await expect(page.getByText("请输入完整角色编码以确认")).toBeVisible();
  expect(rbac.captures.reset).toHaveLength(0);

  await page.getByTestId("reset-code").fill("school_admin");
  await page.getByTestId("reset-reason").fill("短");
  await page.getByTestId("reset-submit").click();
  await expect(page.getByText("重置理由至少 10 个字")).toBeVisible();
  expect(rbac.captures.reset).toHaveLength(0);

  await page.getByTestId("reset-reason").fill("误改角色权限，需要恢复出厂默认配置");
  await page.getByTestId("reset-submit").click();
  await expect(page.getByText("已恢复出厂")).toBeVisible();
  expect(rbac.captures.reset[0]).toMatchObject({
    confirm_role_code: "school_admin",
    version: 3,
  });
  // 重置后重新加载：出厂 6 项全部勾上，版本前进
  await expect(page.getByTestId("perm-school.message.send").locator("input")).toBeChecked();
  await expect(page.getByText("版本 4")).toBeVisible();
});

test("MFA 重置：按登录名定位目标、理由门禁、确认后清除绑定", async ({ page }) => {
  const rbac = await mockRbacApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_AND_MFA } });

  await page.goto("/settings/roles");
  await expect(page.getByText("安全工具 · MFA 重置")).toBeVisible();

  await page.getByTestId("mfa-lookup-input").fill("E-9001");
  await page.getByTestId("mfa-lookup-submit").click();
  await expect(page.getByTestId("mfa-target")).toContainText("王老师");
  await expect(page.getByTestId("mfa-target")).toContainText("已绑定");

  await page.getByTestId("mfa-reset-reason").fill("换手机");
  await page.getByTestId("mfa-reset-submit").click();
  await expect(page.getByText("重置理由至少 10 个字")).toBeVisible();
  expect(rbac.captures.mfaReset).toHaveLength(0);

  await page.getByTestId("mfa-reset-reason").fill("教师换新手机，无法接收动态码，需重新绑定");
  await page.getByTestId("mfa-reset-submit").click();
  await confirmMessageBox(page);
  await expect(page.getByText("已清除 MFA 绑定并撤销该用户会话")).toBeVisible();
  expect(rbac.captures.mfaReset[0]).toMatchObject({
    reason: "教师换新手机，无法接收动态码，需重新绑定",
  });
  await expect(page.getByTestId("mfa-target")).toContainText("未绑定");

  // 登录名不存在：透出后端原因，不泄露是否存在于其他租户
  await page.getByTestId("mfa-lookup-input").fill("E-0000");
  await page.getByTestId("mfa-lookup-submit").click();
  await expect(page.getByText("未找到该登录名对应的用户")).toBeVisible();
  expect(rbac.captures.lookup).toEqual(["E-9001", "E-0000"]);
});

test("无 school.role.manage 时菜单不出现、直连被守卫挡在 403 页", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: ["school.personnel.view"] } });
  await mockRbacApi(page);

  await page.goto("/");
  await expect(page.getByRole("menuitem", { name: "角色权限" })).toHaveCount(0);

  await page.goto("/settings/roles");
  await expect(page.getByText("你没有访问该页面的权限")).toBeVisible();
});

test("只有 role.manage、没有 mfa.reset 专用权限时不显示 MFA 工具", async ({ page }) => {
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER } });
  await mockRbacApi(page);

  await page.goto("/settings/roles");
  await expect(page.getByTestId("role-list")).toBeVisible();
  await expect(page.getByText("安全工具 · MFA 重置")).toHaveCount(0);
});

test("撤掉自己所在角色的 role.manage：二次确认明确自锁后果，取消则不发请求", async ({ page }) => {
  const rbac = await mockRbacApi(page, {
    roles: [
      {
        id: "40000000-0000-0000-0000-000000000002",
        code: "school_admin",
        name: "校方授权管理员",
        is_system: true,
        editable: true,
        lock_reason: null,
        version: 5,
        defaults_applied: true,
        user_count: 2,
        permission_codes: [...SCHOOL_ADMIN_DEFAULTS, "school.role.manage"],
      },
    ],
  });
  await mockAuth(page, {
    authenticated: true,
    user: { permissions: ROLE_MANAGER, roles: ["school_admin"] },
  });

  await page.goto("/settings/roles");
  await page.getByTestId("role-school_admin").click();
  await page.getByTestId("perm-school.role.manage").click();
  await page.getByTestId("role-save").click();

  // 与"改别人角色"不同的文案：明确下一步就进不来本页了
  await expect(page.locator(".el-message-box")).toContainText("自锁");
  await page.locator(".el-message-box").getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByTestId("role-save")).toBeEnabled();
  expect(rbac.captures.savePermissions).toHaveLength(0);

  await page.getByTestId("role-save").click();
  await confirmMessageBox(page);
  await expect(page.getByText("已保存：新增 0 项、移除 1 项")).toBeVisible();
  expect(rbac.captures.savePermissions[0]?.permission_codes).toEqual([...SCHOOL_ADMIN_DEFAULTS]);
});

test("恢复出厂也会自锁：预览差集含 role.manage 且本人在该角色时给出提示", async ({ page }) => {
  const rbac = await mockRbacApi(page, {
    roles: [
      {
        id: "40000000-0000-0000-0000-000000000002",
        code: "school_admin",
        name: "校方授权管理员",
        is_system: true,
        editable: true,
        lock_reason: null,
        version: 5,
        defaults_applied: true,
        user_count: 2,
        permission_codes: [...SCHOOL_ADMIN_DEFAULTS, "school.role.manage"],
      },
    ],
  });
  await mockAuth(page, {
    authenticated: true,
    user: { permissions: ROLE_MANAGER, roles: ["school_admin"] },
  });

  await page.goto("/settings/roles");
  await page.getByTestId("role-school_admin").click();
  await page.getByTestId("role-reset").click();

  await expect(page.getByTestId("reset-selflock-warning")).toContainText("自锁风险");
  await expect(page.getByTestId("reset-selflock-warning")).toContainText("school.role.manage");

  await page.getByTestId("reset-code").fill("school_admin");
  await page.getByTestId("reset-reason").fill("角色权限被误改，恢复出厂默认配置基线");
  await page.getByTestId("reset-submit").click();
  await expect(page.getByText("已恢复出厂")).toBeVisible();
  expect(rbac.captures.reset).toHaveLength(1);
  // 出厂清单不含 role.manage：重置后该管理员确实被锁出本页（前端只提示，服务端才是 enforcing）
  await expect(page.getByTestId("perm-school.role.manage").locator("input")).not.toBeChecked();
});

test("不在被重置角色里时不自称自锁", async ({ page }) => {
  await mockRbacApi(page, {
    roles: [
      {
        id: "40000000-0000-0000-0000-000000000002",
        code: "school_admin",
        name: "校方授权管理员",
        is_system: true,
        editable: true,
        lock_reason: null,
        version: 5,
        defaults_applied: true,
        user_count: 2,
        permission_codes: [...SCHOOL_ADMIN_DEFAULTS, "school.role.manage"],
      },
    ],
  });
  // 超管本人角色是 school_super_admin，重置 school_admin 不影响自己的入口
  await mockAuth(page, {
    authenticated: true,
    user: { permissions: ROLE_MANAGER, roles: ["school_super_admin"] },
  });

  await page.goto("/settings/roles");
  await page.getByTestId("role-school_admin").click();
  await page.getByTestId("role-reset").click();
  await expect(page.getByTestId("reset-preview")).toBeVisible();
  await expect(page.getByTestId("reset-selflock-warning")).toHaveCount(0);
});

test("重置被后端拒绝时不产生半成品状态：预览不变、界面仍是原配置", async ({ page }) => {
  const rbac = await mockRbacApi(page, {
    resetResponse: {
      status: 409,
      body: {
        detail: { code: "ROLE_VERSION_CONFLICT", message: "角色已被他人修改，请刷新后重试" },
      },
    },
  });
  await mockAuth(page, { authenticated: true, user: { permissions: ROLE_MANAGER } });

  await page.goto("/settings/roles");
  await page.getByTestId("role-school_admin").click();
  await page.getByTestId("role-reset").click();
  // fixture 的 school_admin 故意只留 1 项：预览应显示要补 5 项
  await expect(page.getByTestId("reset-preview")).toContainText("恢复权限");
  await page.getByTestId("reset-code").fill("school_admin");
  await page.getByTestId("reset-reason").fill("误改角色权限，需要恢复出厂默认配置");
  await page.getByTestId("reset-submit").click();

  await expect(page.getByText("角色已被他人修改")).toBeVisible();
  expect(rbac.captures.reset).toHaveLength(1);
  // 失败后重新拉取的角色仍是偏离态（版本 3、只有 dashboard.view 勾选）
  await expect(page.getByTestId("perm-school.message.send").locator("input")).not.toBeChecked();
  await expect(page.getByTestId("perm-school.dashboard.view").locator("input")).toBeChecked();
  await expect(page.getByText("版本 3")).toBeVisible();
});
