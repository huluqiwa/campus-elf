import { expect, test } from "@playwright/test";

import { field } from "../e2e/helpers/locators";
import { apiGet, apiLogin, apiPost, loadAccounts, type E2EAccounts } from "./helpers/support";

// M1 验收（TD-110）全栈 E2E：真实会话下的业务接口与 RBAC/数据范围/会话设备管理。
// 浏览器侧验证「被踢」效果；接口侧用 Node 真实登录拿真实令牌（独立 UA = 独立设备）。

interface TenantItem {
  school_code: string;
  name: string;
  status: string;
}

interface AuditLogItem {
  action: string;
  result: string;
}

interface PersonItem {
  user_type: string;
  display_name: string;
}

interface SessionItem {
  session_id?: string;
  id?: string;
}

async function loginTenant(
  accounts: E2EAccounts,
  who: { school_code: string; login_id: string; password: string },
): Promise<string> {
  return apiLogin(accounts, {
    realm: "tenant",
    school_code: who.school_code,
    login_id: who.login_id,
    password: who.password,
  });
}

test("平台业务接口：种子租户可查、审计可查本次登录事件", async () => {
  const accounts = loadAccounts();
  const token = await apiLogin(accounts, {
    realm: "platform",
    account: accounts.platform.account,
    password: accounts.platform.password,
  });

  // 租户列表：真实开通的种子租户在列
  const tenants = await apiGet(accounts, "/api/v1/platform/tenants", token);
  expect(tenants.status).toBe(200);
  const items = tenants.json as TenantItem[];
  const seeded = items.find((item) => item.school_code === accounts.school_code);
  expect(seeded, `租户列表应包含 ${accounts.school_code}`).toBeTruthy();
  expect(seeded?.name).toBe(accounts.school_name);
  expect(seeded?.status).toBe("active");

  // 审计列表：本次真实登录应已落审计（AUD-01 经真实栈可查）
  const audit = await apiGet(accounts, "/api/v1/platform/audit-logs?limit=50", token);
  expect(audit.status).toBe(200);
  const auditBody = audit.json as { items: AuditLogItem[]; total: number };
  expect(auditBody.total).toBeGreaterThan(0);
  expect(auditBody.items.some((row) => row.action === "auth.login.success")).toBe(true);
});

test("数据范围：运营管理员看全校、教师仅本人", async () => {
  const accounts = loadAccounts();
  const opsToken = await loginTenant(accounts, accounts.school_ops);
  const teacherToken = await loginTenant(accounts, accounts.teacher);

  const opsList = await apiGet(accounts, "/api/v1/school/personnel?limit=100", opsToken);
  expect(opsList.status).toBe(200);
  const opsItems = opsList.json as PersonItem[];
  expect(opsItems.length).toBeGreaterThanOrEqual(5);
  expect(opsItems.some((item) => item.user_type === "teacher")).toBe(true);
  expect(opsItems.some((item) => item.user_type === "student")).toBe(true);

  const teacherList = await apiGet(accounts, "/api/v1/school/personnel?limit=100", teacherToken);
  expect(teacherList.status).toBe(200);
  const teacherItems = teacherList.json as PersonItem[];
  expect(teacherItems).toHaveLength(1);
  expect(teacherItems[0].user_type).toBe("teacher");
});

test("越权拒绝：教师令牌调用平台接口被拒", async () => {
  const accounts = loadAccounts();
  const teacherToken = await loginTenant(accounts, accounts.teacher);

  const response = await apiGet(accounts, "/api/v1/platform/tenants", teacherToken);
  // require_permission 的 realm 判定先于权限判定，恒为 403（见 app/auth/principal.py）
  expect(response.status).toBe(403);
});

test("会话设备管理：退出其它设备后，被踢方下一请求失效并被踢回登录页", async ({ page }) => {
  const accounts = loadAccounts();

  // 设备 A：浏览器真实登录（专用账号，避免与其他并行用例的会话互踩）
  await page.goto("/login");
  await field(page, "school-code").fill(accounts.devices.school_code);
  await field(page, "login-id").fill(accounts.devices.login_id);
  await field(page, "password").fill(accounts.devices.password);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId("identity")).toContainText("校方");

  // 设备 B：Node 真实登录（独立 UA）
  const nodeToken = await loginTenant(accounts, accounts.devices);

  // 设备列表应有两台设备
  const before = await apiGet(accounts, "/api/v1/auth/sessions", nodeToken);
  expect(before.status).toBe(200);
  const beforeItems = (before.json as { sessions: SessionItem[] }).sessions;
  expect(beforeItems.length).toBeGreaterThanOrEqual(2);

  // 设备 B 退出其它设备 → 设备 A 的会话被吊销
  const revoke = await apiPost(accounts, "/api/v1/auth/sessions/revoke-others", nodeToken);
  expect(revoke.status).toBe(200);

  // 被踢方（浏览器）下一请求失效：刷新后 refresh cookie 已吊销 → 回登录页
  await page.reload();
  await expect(page).toHaveURL(/\/login$/);

  // 设备列表只剩发起方自己
  const after = await apiGet(accounts, "/api/v1/auth/sessions", nodeToken);
  expect(after.status).toBe(200);
  const afterItems = (after.json as { sessions: SessionItem[] }).sessions;
  expect(afterItems).toHaveLength(1);
});
