import { expect, test, type Page } from "@playwright/test";

import { field } from "../e2e/helpers/locators";
import {
  apiGet,
  apiLogin,
  apiLoginMfa,
  apiPut,
  loadAccounts,
  type E2EAccounts,
} from "./helpers/support";

// M2-UI2 / M2-UI3 全栈 E2E（TD-192）：UI 写操作 × 真实后端 × 另一会话。
// 三条链路都会改内置 school_admin 或 mfa_target 的状态，必须串行。

test.describe.configure({ mode: "serial" });

const PERSONNEL_VIEW = "school.personnel.view";

interface RoleSummary {
  id: string;
  code: string;
  version: number;
  permission_codes: string[];
}

interface MeView {
  permissions: string[];
  mfa_enabled: boolean;
}

/**
 * role_admin 的 API 会话：每条用例各登一次，登录后不再复用更早的 token。
 * tenant 设备限额 3，新登录会踢最久未用的会话——旧 token 只可用于「登录前」的前置操作。
 */
async function roleAdminLogin(accounts: E2EAccounts): Promise<string> {
  return apiLogin(accounts, {
    realm: "tenant",
    school_code: accounts.role_admin.school_code,
    login_id: accounts.role_admin.login_id,
    password: accounts.role_admin.password,
  });
}

async function schoolAdminRole(accounts: E2EAccounts, token: string): Promise<RoleSummary> {
  const response = await apiGet(accounts, "/api/v1/school/roles", token);
  expect(response.status).toBe(200);
  const role = (response.json as RoleSummary[]).find((item) => item.code === "school_admin");
  if (!role) throw new Error("种子数据缺少内置 school_admin 角色");
  return role;
}

/** 前置：把 school_admin 的 personnel.view 调整到指定状态（用例重跑也不依赖上一轮残留）。 */
async function ensurePersonnelView(
  accounts: E2EAccounts,
  token: string,
  present: boolean,
): Promise<void> {
  const role = await schoolAdminRole(accounts, token);
  if (role.permission_codes.includes(PERSONNEL_VIEW) === present) return;
  const codes = present
    ? [...role.permission_codes, PERSONNEL_VIEW]
    : role.permission_codes.filter((code) => code !== PERSONNEL_VIEW);
  const response = await apiPut(accounts, `/api/v1/school/roles/${role.id}/permissions`, token, {
    permission_codes: codes,
    version: role.version,
    reason: "E2E 前置：调整校方授权管理员的人员查看权限",
    self_grant_ack: false,
  });
  expect(response.status).toBe(200);
}

async function loginAsRoleAdmin(page: Page, accounts: E2EAccounts): Promise<void> {
  await page.goto("/login");
  await field(page, "school-code").fill(accounts.role_admin.school_code);
  await field(page, "login-id").fill(accounts.role_admin.login_id);
  await field(page, "password").fill(accounts.role_admin.password);
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("identity")).toContainText("校方");
}

async function openSchoolAdminRole(page: Page): Promise<void> {
  await page.goto("/settings/roles");
  await expect(page.getByTestId("role-list")).toBeVisible();
  await page.getByTestId("role-school_admin").click();
  // Element Plus 的原生 input 是隐藏的，可见性看外层勾选行；勾选状态由调用方用 toBeChecked 断言。
  await expect(page.getByTestId(`perm-${PERSONNEL_VIEW}`)).toBeVisible();
}

test("撤掉角色权限：保存后持有人下一个请求即 403，但会话不断（/auth/me 仍 200）", async ({
  page,
}) => {
  const accounts = loadAccounts();
  const managerToken = await roleAdminLogin(accounts);
  await ensurePersonnelView(accounts, managerToken, true);

  const targetToken = await apiLogin(accounts, {
    realm: "tenant",
    school_code: accounts.role_target.school_code,
    login_id: accounts.role_target.login_id,
    password: accounts.role_target.password,
  });
  expect((await apiGet(accounts, "/api/v1/school/personnel?limit=1", targetToken)).status).toBe(
    200,
  );

  await loginAsRoleAdmin(page, accounts);
  await openSchoolAdminRole(page);
  await expect(page.getByTestId(`perm-${PERSONNEL_VIEW}`).locator("input")).toBeChecked();
  await page.getByTestId(`perm-${PERSONNEL_VIEW}`).click();
  await page
    .getByTestId("role-save-reason")
    .fill("E2E 验收：撤销人员查看权限以验证下一个请求即生效");
  await page.getByTestId("role-save").click();
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
  await expect(page.getByText("已保存：新增 0 项、移除 1 项")).toBeVisible();

  // 同一个旧 access 的下一个请求立刻失去权限；但会话本身不被撤销
  expect((await apiGet(accounts, "/api/v1/school/personnel?limit=1", targetToken)).status).toBe(
    403,
  );
  const me = await apiGet(accounts, "/api/v1/auth/me", targetToken);
  expect(me.status).toBe(200);
  expect((me.json as MeView).permissions).not.toContain(PERSONNEL_VIEW);
});

test("恢复出厂：权限在持有人的下一个请求热更新，无需重新登录", async ({ page }) => {
  const accounts = loadAccounts();
  const managerToken = await roleAdminLogin(accounts);
  await ensurePersonnelView(accounts, managerToken, false);

  const targetToken = await apiLogin(accounts, {
    realm: "tenant",
    school_code: accounts.role_target.school_code,
    login_id: accounts.role_target.login_id,
    password: accounts.role_target.password,
  });
  expect((await apiGet(accounts, "/api/v1/school/personnel?limit=1", targetToken)).status).toBe(
    403,
  );

  await loginAsRoleAdmin(page, accounts);
  await openSchoolAdminRole(page);
  await page.getByTestId("role-reset").click();
  await expect(page.getByTestId("reset-preview")).toContainText(PERSONNEL_VIEW);
  await page.getByTestId("reset-code").fill("school_admin");
  await page.getByTestId("reset-reason").fill("E2E 验收：恢复出厂后权限应在下一个请求生效");
  await page.getByTestId("reset-submit").click();
  await expect(page.getByText(/已恢复出厂/)).toBeVisible();

  expect((await apiGet(accounts, "/api/v1/school/personnel?limit=1", targetToken)).status).toBe(
    200,
  );
  const me = await apiGet(accounts, "/api/v1/auth/me", targetToken);
  expect(me.status).toBe(200);
  expect((me.json as MeView).permissions).toContain(PERSONNEL_VIEW);
});

test("MFA 重置：管理员清绑后目标旧 access 立即 401，重新登录不再要求动态码", async ({ page }) => {
  const accounts = loadAccounts();
  const targetToken = await apiLoginMfa(
    accounts,
    {
      realm: "tenant",
      school_code: accounts.mfa_target.school_code,
      login_id: accounts.mfa_target.login_id,
      password: accounts.mfa_target.password,
    },
    accounts.mfa_target.totp_secret,
  );
  const before = await apiGet(accounts, "/api/v1/auth/me", targetToken);
  expect(before.status).toBe(200);
  expect((before.json as MeView).mfa_enabled).toBe(true);

  await loginAsRoleAdmin(page, accounts);
  await page.goto("/settings/roles");
  await expect(page.getByText("安全工具 · MFA 重置")).toBeVisible();
  await page.getByTestId("mfa-lookup-input").fill(accounts.mfa_target.login_id);
  await page.getByTestId("mfa-lookup-submit").click();
  await expect(page.getByTestId("mfa-target")).toContainText("已绑定");
  await page.getByTestId("mfa-reset-reason").fill("E2E 验收：教师换手机，清除 MFA 绑定后重新登录");
  await page.getByTestId("mfa-reset-submit").click();
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
  await expect(page.getByText("已清除 MFA 绑定并撤销该用户会话")).toBeVisible();

  // 旧 access 立即失效（刷新家族撤销 + token_version 递增）
  expect((await apiGet(accounts, "/api/v1/auth/me", targetToken)).status).toBe(401);

  // 密码登录直接拿 access（不再停在 MFA 挑战），新会话 /auth/me 显示未绑定
  const reloginToken = await apiLogin(accounts, {
    realm: "tenant",
    school_code: accounts.mfa_target.school_code,
    login_id: accounts.mfa_target.login_id,
    password: accounts.mfa_target.password,
  });
  const after = await apiGet(accounts, "/api/v1/auth/me", reloginToken);
  expect(after.status).toBe(200);
  expect((after.json as MeView).mfa_enabled).toBe(false);
});
