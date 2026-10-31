import { expect, test, type Page } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";
import {
  DEFAULT_ACTOR_USER_ID,
  mockRbacApi,
  reasonLength,
  type AccountFixture,
  type RoleFixture,
} from "./helpers/rbac-mock";

// M2-UI3 TD-187g-C 权限账号页（接口 mock）：列表渲染 / 游标翻页 / 本地过滤与精确定位 /
// 角色改绑（含"已持有但不可授"的保留）/ 停用与启用 / 重置 MFA。
// mock 的判据是手抄后端的（见 rbac-mock.ts 的行号注释）——TD-253 的已知缺口，收口靠 TD-183/192。

const ACCOUNT_ONLY = ["school.permission_account.manage"];
const ACCOUNT_AND_MFA = ["school.permission_account.manage", "school.personnel.mfa.reset"];

const TEACHER_ID = "50000000-0000-0000-0000-000000000001";
const DISABLED_ID = "50000000-0000-0000-0000-000000000002";
const TEACHER_ROLE_ID = "40000000-0000-0000-0000-000000000003";
const SCHOOL_ADMIN_ROLE_ID = "40000000-0000-0000-0000-000000000002";

const TEACHER_ROLE: RoleFixture = {
  id: TEACHER_ROLE_ID,
  code: "teacher_b_end",
  name: "教师（B 端）",
  is_system: true,
  editable: true,
  lock_reason: null,
  version: 1,
  defaults_applied: true,
  user_count: 5,
  permission_codes: ["school.kb.upload"],
  bindable: true,
  resettable: true,
};

const SCHOOL_ADMIN_ROLE: RoleFixture = {
  ...TEACHER_ROLE,
  id: SCHOOL_ADMIN_ROLE_ID,
  code: "school_admin",
  name: "校方授权管理员",
  user_count: 2,
  permission_codes: ["school.dashboard.view"],
};

/**
 * 已持有但**不可授出**的角色（TD-245 假设的"可绑定标记收窄"形态）。
 * 今天后端只对超管角色给 `bindable=false`（判据是角色码，`binding.py:539`），而超管账号本就禁改，
 * 所以这个形态在真后端上暂时不可达；这里把它挂在一个普通教师账号上，才测得到
 * 「提交集合必须原样保留它，且服务端保存后它仍在持有」（§20.3 C3-A）。
 * mock 的 `ROLE_NOT_BINDABLE` 判据按**码**写死（与后端一致），不会因为这只角色的 `bindable=false`
 * 就把它拒掉——否则这条用例就成了 mock 自己造题自己答题。
 */
const LEGACY_ROLE: RoleFixture = {
  ...TEACHER_ROLE,
  id: "40000000-0000-0000-0000-0000000000aa",
  code: "legacy_grantor",
  name: "旧授权岗",
  bindable: false,
  permission_codes: ["school.kb.view"],
};

async function confirmMessageBox(page: Page): Promise<void> {
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
}

/** 用本页的本地过滤锁定一行，再点「管理」开抽屉（抽屉以绑定区渲染为打开信号）。 */
async function openRow(page: Page, keyword: string): Promise<void> {
  await page.getByTestId("account-filter").fill(keyword);
  await expect(page.getByTestId("account-manage")).toHaveCount(1);
  await page.getByTestId("account-manage").click();
  await expect(page.getByTestId("binding-section")).toBeVisible();
}

test("列表渲染：三档状态、超管标记，且不回显登录名", async ({ page }) => {
  await mockRbacApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ACCOUNT_ONLY } });

  await page.goto("/settings/accounts");
  await expect(page.getByTestId("account-table")).toBeVisible();
  await expect(page.getByTestId("account-manage")).toHaveCount(5);
  await expect(page.getByTestId("account-table")).toContainText("已锁定");
  await expect(page.getByTestId("account-table")).toContainText("已停用");
  await expect(page.getByTestId("account-super-tag")).toHaveCount(1);
  // 后端列表响应里没有 login_id（学号/工号属敏感标识符），界面也不能把它变出来
  await expect(page.getByText("E-9001")).toHaveCount(0);
  // 只持账号管理码：精确定位框不出现，但列表主路径（过滤 + 翻页）照常可用
  await expect(page.getByTestId("account-lookup-input")).toHaveCount(0);
});

test("翻页：下一页带的是上一页回下来的游标；没填满一页就没有下一页", async ({ page }) => {
  const many: AccountFixture[] = Array.from({ length: 51 }, (_, index) => ({
    id: `60000000-0000-0000-0000-0000000000${String(index).padStart(2, "0")}`,
    login_id: `E-7${String(index).padStart(3, "0")}`,
    display_name: `教师 ${index}`,
    user_type: "teacher",
    status: "active",
    mfa_enabled: false,
    role_ids: [],
    roles_version: 0,
  }));
  const rbac = await mockRbacApi(page, { accounts: many });
  await mockAuth(page, { authenticated: true, user: { permissions: ACCOUNT_ONLY } });

  await page.goto("/settings/accounts");
  await expect(page.getByTestId("account-page-info")).toContainText("第 1 页");
  await expect(page.getByTestId("account-prev")).toBeDisabled();
  await expect(page.getByTestId("account-next")).toBeEnabled();

  await page.getByTestId("account-next").click();
  await expect(page.getByTestId("account-page-info")).toContainText("第 2 页");
  // 第二页请求带的 cursor 必须是第一页响应里的 next_cursor，而不是前端自己按页码拼的偏移
  expect(new URL(rbac.captures.accountsList[1]).searchParams.get("cursor")).toBe("o-50");
  // 第二页只有 1 行＝没填满 limit → 后端不回游标 → 「下一页」禁用（按条数猜就会判成还能翻）
  await expect(page.getByTestId("account-next")).toBeDisabled();

  await page.getByTestId("account-prev").click();
  await expect(page.getByTestId("account-page-info")).toContainText("第 1 页");
});

test("翻页失败：旧行保留但明说「不是最新」，重试重发失败的那一页", async ({ page }) => {
  const many: AccountFixture[] = Array.from({ length: 51 }, (_, index) => ({
    id: `61000000-0000-0000-0000-0000000000${String(index).padStart(2, "0")}`,
    login_id: `E-8${String(index).padStart(3, "0")}`,
    display_name: `教师 ${index}`,
    user_type: "teacher",
    status: "active",
    mfa_enabled: false,
    role_ids: [],
    roles_version: 0,
  }));
  // 第 1 次（进页）成功、第 2 次（翻下一页）失败 → 表格继续显示第 1 页的行
  const rbac = await mockRbacApi(page, { accounts: many, accountsListFailOnRequest: 2 });
  await mockAuth(page, { authenticated: true, user: { permissions: ACCOUNT_ONLY } });

  await page.goto("/settings/accounts");
  await expect(page.getByTestId("account-page-info")).toContainText("第 1 页");
  await page.getByTestId("account-next").click();

  // 行与页码仍然一致（都是第 1 页），但界面必须自报这批行是旧的（TD-260）
  await expect(page.getByTestId("account-page-info")).toContainText("第 1 页");
  await expect(page.getByTestId("account-manage")).toHaveCount(50);
  await expect(page.getByTestId("account-stale-alert")).toContainText("数据可能不是最新");
  await expect(page.getByTestId("account-stale-alert")).toContainText(/上次成功 \d{2}:\d{2}:\d{2}/);

  await page.getByTestId("account-stale-retry").click();
  await expect(page.getByTestId("account-page-info")).toContainText("第 2 页");
  await expect(page.getByTestId("account-manage")).toHaveCount(1);
  await expect(page.getByTestId("account-stale-alert")).toHaveCount(0);
  // 进页 + 失败的一次 + 重试 = 3 次；第 2 页出现且带 o-50 才算真的重发了失败页
  expect(rbac.captures.accountsList).toHaveLength(3);
  expect(new URL(rbac.captures.accountsList.at(-1)!).searchParams.get("cursor")).toBe("o-50");
});

test("首次加载失败：空表 + 「尚未成功加载过」，重试成功后横幅消失", async ({ page }) => {
  const rbac = await mockRbacApi(page, { accountsListFailOnRequest: 1 });
  await mockAuth(page, { authenticated: true, user: { permissions: ACCOUNT_ONLY } });

  await page.goto("/settings/accounts");
  await expect(page.getByTestId("account-stale-alert")).toContainText("尚未成功加载过");
  await expect(page.getByTestId("account-manage")).toHaveCount(0);

  await page.getByTestId("account-stale-retry").click();
  await expect(page.getByTestId("account-manage")).toHaveCount(5);
  await expect(page.getByTestId("account-stale-alert")).toHaveCount(0);
  expect(rbac.captures.accountsList).toHaveLength(2);
});

test("mock 的理由长度按码点算（与后端 len() 同口径，TD-259）", () => {
  expect(reasonLength("😀".repeat(9))).toBe(9);
  expect(reasonLength("😀".repeat(10))).toBe(10);
  expect(reasonLength("  校园精灵  ")).toBe(4);
});

test("本地过滤：只作用当前页，姓名与角色码都能命中（大小写不敏感）", async ({ page }) => {
  await mockRbacApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ACCOUNT_ONLY } });

  await page.goto("/settings/accounts");
  await page.getByTestId("account-filter").fill("王");
  await expect(page.getByTestId("account-manage")).toHaveCount(1);

  await page.getByTestId("account-filter").fill("TEACHER_B_END");
  await expect(page.getByTestId("account-manage")).toHaveCount(1);

  await page.getByTestId("account-filter").fill("不存在的人");
  await expect(page.getByTestId("account-manage")).toHaveCount(0);
  await expect(page.getByText("没有符合条件的权限账号")).toBeVisible();
});

test("精确定位：命中后直接开抽屉并重读绑定快照；已停用行给启用而不是停用", async ({ page }) => {
  const rbac = await mockRbacApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ACCOUNT_AND_MFA } });

  await page.goto("/settings/accounts");
  await expect(page.getByTestId("account-lookup-input")).toBeVisible();
  // 查无此人：后端回 404，界面说"没这个人"并且不开抽屉
  await page.getByTestId("account-lookup-input").fill("E-0000");
  await page.getByTestId("account-lookup-submit").click();
  await expect(page.getByText("用户不存在")).toBeVisible();
  await expect(page.getByTestId("binding-section")).toHaveCount(0);

  await page.getByTestId("account-lookup-input").fill("E-9002");
  await page.getByTestId("account-lookup-submit").click();

  expect(rbac.captures.lookup).toEqual(["E-0000", "E-9002"]);
  await expect(page.getByTestId("binding-section")).toBeVisible();
  // 打开抽屉必发一次 GET：列表行是进页时的快照，version 可能已经落后
  expect(rbac.captures.bindingsList).toHaveLength(1);
  expect(rbac.captures.bindingsList[0]).toContain(`/school/users/${DISABLED_ID}/roles`);
  await expect(page.getByTestId("account-activate")).toBeVisible();
  await expect(page.getByTestId("deactivate-submit")).toHaveCount(0);
  // 没开 MFA 的账号：重置区不给"写了也会被拒"的按钮
  await expect(page.getByTestId("mfa-section")).toContainText("没有开启 MFA");
});

test("改绑：PUT 带完整角色集合与快照版本，成功后列表角色跟着服务端变", async ({ page }) => {
  const rbac = await mockRbacApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ACCOUNT_ONLY } });

  await page.goto("/settings/accounts");
  await openRow(page, "王老师");
  await expect(page.getByTestId("binding-held-teacher_b_end").locator("input")).toBeChecked();

  await page.getByTestId("binding-add-school_admin").click();
  await expect(page.getByTestId("binding-diff")).toContainText("授予");
  await page.getByTestId("binding-save").click();

  await expect(page.getByText("角色绑定已更新")).toBeVisible();
  expect(rbac.captures.bindingSave).toHaveLength(1);
  expect(rbac.captures.bindingSave[0]).toMatchObject({
    role_ids: [TEACHER_ROLE_ID, SCHOOL_ADMIN_ROLE_ID],
    version: 4,
  });
  // 列表是重读来的（不是前端手改）：这一行的角色码列出现了新角色
  await expect(page.getByTestId("account-table")).toContainText("school_admin");
});

test("撤掉全部角色：保存前明说「不再持有任何角色」，提交体是空清单", async ({ page }) => {
  const rbac = await mockRbacApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ACCOUNT_ONLY } });

  await page.goto("/settings/accounts");
  await openRow(page, "王老师");
  await page.getByTestId("binding-held-teacher_b_end").click();
  await expect(page.getByTestId("binding-diff")).toContainText("不再持有任何角色");

  await page.getByTestId("binding-save").click();
  expect(rbac.captures.bindingSave).toHaveLength(1);
  expect(rbac.captures.bindingSave[0]).toMatchObject({ role_ids: [], version: 4 });
  await expect(page.getByTestId("account-table")).toContainText("（无角色）");
});

test("改绑撞版本冲突：采用服务端最新快照，不拿旧版本再发一遍", async ({ page }) => {
  const rbac = await mockRbacApi(page, { bindingVersionConflictOnce: true });
  await mockAuth(page, { authenticated: true, user: { permissions: ACCOUNT_ONLY } });

  await page.goto("/settings/accounts");
  await openRow(page, "王老师");
  await page.getByTestId("binding-add-school_admin").click();
  await page.getByTestId("binding-save").click();

  await expect(page.getByTestId("binding-conflict")).toContainText("已被他人修改");
  // 采用 latest 后草稿与快照一致 → 保存回到禁用，等用户重新确认；
  // 少了这一步，用户点第二次就是把过期的意图静默盖回去。
  await expect(page.getByTestId("binding-save")).toBeDisabled();
  expect(rbac.captures.bindingSave[0]).toMatchObject({ version: 4 });

  await page.getByTestId("binding-add-school_admin").click();
  await page.getByTestId("binding-save").click();
  // 第二次带的是冲突响应里的新版本 5
  expect(rbac.captures.bindingSave[1]).toMatchObject({
    role_ids: [TEACHER_ROLE_ID, SCHOOL_ADMIN_ROLE_ID],
    version: 5,
  });
});

test("服务端仍然拒绝时：透出后端原因，不谎报成功（前端门禁不是边界）", async ({ page }) => {
  // 模拟"进页后对方刚被提成超管"这类前端预判不到的竞态：门禁放行，后端仍 403
  const rbac = await mockRbacApi(page, {
    bindingRejectOnce: {
      status: 403,
      body: {
        detail: {
          code: "BINDING_SUPER_ADMIN_FORBIDDEN",
          message: "校方超管的角色绑定由平台开通流程管理，租户内不可修改",
        },
      },
    },
  });
  await mockAuth(page, { authenticated: true, user: { permissions: ACCOUNT_ONLY } });

  await page.goto("/settings/accounts");
  await openRow(page, "王老师");
  await page.getByTestId("binding-add-school_admin").click();
  await page.getByTestId("binding-save").click();

  await expect(page.getByText("校方超管的角色绑定由平台开通流程管理")).toBeVisible();
  await expect(page.getByText("角色绑定已更新")).toHaveCount(0);
  expect(rbac.captures.bindingSave).toHaveLength(1);
});

test("已持有但不可授的角色留在提交集合里：全量替换不会静默收回它", async ({ page }) => {
  const heldAccount: AccountFixture = {
    id: TEACHER_ID,
    login_id: "E-9001",
    display_name: "王老师",
    user_type: "teacher",
    status: "active",
    mfa_enabled: true,
    role_ids: [TEACHER_ROLE_ID, LEGACY_ROLE.id],
    roles_version: 4,
  };
  const rbac = await mockRbacApi(page, {
    roles: [SCHOOL_ADMIN_ROLE, TEACHER_ROLE, LEGACY_ROLE],
    accounts: [heldAccount],
  });
  await mockAuth(page, { authenticated: true, user: { permissions: ACCOUNT_ONLY } });

  await page.goto("/settings/accounts");
  await openRow(page, "王老师");
  // 不可变更的行只读展示，并说明它不会被保存收回
  await expect(page.getByTestId("binding-held-legacy_grantor")).toContainText("不可变更");
  await expect(page.getByTestId("binding-held-legacy_grantor").locator("input")).toBeDisabled();
  // 它也不出现在「可新增」候选里（目录本来就不含它）
  await expect(page.getByTestId("binding-add-legacy_grantor")).toHaveCount(0);

  await page.getByTestId("binding-held-teacher_b_end").click();
  await expect(page.getByTestId("binding-diff")).toContainText("收回");
  // 只剩不可授的那一行 ≠ 收回全部权限
  await expect(page.getByTestId("binding-diff")).not.toContainText("不再持有任何角色");

  await page.getByTestId("binding-save").click();
  expect(rbac.captures.bindingSave).toHaveLength(1);
  expect(rbac.captures.bindingSave[0]).toMatchObject({
    role_ids: [LEGACY_ROLE.id],
    version: 4,
  });
  // 保存必须真的成功（mock 的 ROLE_NOT_BINDABLE 按码判，与后端一致 → 这只角色不会被拒）
  await expect(page.getByText("角色绑定已更新")).toBeVisible();
  // 服务端状态跟着走：重读后列表这一行的角色码列仍是它，而不是被全量替换顺手收回
  await expect(page.getByTestId("account-table")).toContainText("legacy_grantor");
  await expect(page.getByTestId("account-table")).not.toContainText("teacher_b_end");
  await expect(page.getByTestId("binding-held-legacy_grantor")).toBeVisible();
});

test("精确定位捞到学生账号：只给读，不给点了必然 422 的写控件", async ({ page }) => {
  const rbac = await mockRbacApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ACCOUNT_AND_MFA } });

  await page.goto("/settings/accounts");
  // 列表端点按类型过滤，学生根本不在表里；`/users/lookup` 不过滤，学号能捞到
  await expect(page.getByTestId("account-table")).not.toContainText("陈同学");
  await page.getByTestId("account-lookup-input").fill("E-9005");
  await page.getByTestId("account-lookup-submit").click();
  await expect(page.getByTestId("binding-section")).toBeVisible();

  await expect(page.getByTestId("account-gate-hint")).toContainText("不在权限账号页的管理范围");
  await expect(page.getByTestId("binding-save")).toBeDisabled();
  await expect(page.getByTestId("deactivate-submit")).toBeDisabled();
  // MFA 重置**不吃类型判据**（后端 `mfa_admin.reset_user_mfa` 没有这一挡）：仍然可点
  await expect(page.getByTestId("mfa-reset-submit")).toBeEnabled();

  expect(rbac.captures.bindingSave).toHaveLength(0);
  expect(rbac.captures.deactivate).toHaveLength(0);
});

test("缺 school.personnel.mfa.reset：重置区整块不出现，不摆一个禁用按钮加内部权限码", async ({
  page,
}) => {
  await mockRbacApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ACCOUNT_ONLY } });

  await page.goto("/settings/accounts");
  await openRow(page, "王老师");
  await expect(page.getByTestId("mfa-section")).toHaveCount(0);
  await expect(page.getByTestId("account-table")).toBeVisible();
});

test("停用与启用：理由不足 10 字不发请求；启用不发请求体", async ({ page }) => {
  const rbac = await mockRbacApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ACCOUNT_ONLY } });

  await page.goto("/settings/accounts");
  await openRow(page, "王老师");

  await page.getByTestId("deactivate-reason").fill("换个环境");
  await page.getByTestId("deactivate-submit").click();
  await expect(page.getByText(/停用理由至少 10 个字/)).toBeVisible();
  expect(rbac.captures.deactivate).toHaveLength(0);

  await page.getByTestId("deactivate-reason").fill("长期外出，暂时收回后台入口，回来再启用");
  await page.getByTestId("deactivate-submit").click();
  await confirmMessageBox(page);
  await expect(page.getByText("已停用该账号并撤销其登录会话")).toBeVisible();
  expect(rbac.captures.deactivate[0]).toMatchObject({
    reason: "长期外出，暂时收回后台入口，回来再启用",
  });
  // 状态以服务端为准：重读后列表这一行显示已停用
  await expect(page.getByTestId("account-table")).toContainText("已停用");

  await page.getByTestId("account-activate").click();
  await confirmMessageBox(page);
  await expect(page.getByText("已启用该账号")).toBeVisible();
  expect(rbac.captures.activate).toHaveLength(1);
  expect(rbac.captures.activate[0].user_id).toBe(TEACHER_ID);
  // 后端这个端点连请求体都不接（school_rbac.py:948）：前端不能自己编一个 reason
  expect(rbac.captures.activate[0].body).toBeNull();
  await expect(page.getByTestId("account-table")).toContainText("启用");
});

test("超管与自己两行：写控件全关，并说明为什么", async ({ page }) => {
  const rbac = await mockRbacApi(page);
  await mockAuth(page, {
    authenticated: true,
    user: { userId: DEFAULT_ACTOR_USER_ID, permissions: ACCOUNT_AND_MFA },
  });

  await page.goto("/settings/accounts");
  await openRow(page, "孙超管");
  await expect(page.getByTestId("account-gate-hint")).toContainText("平台开通流程");
  await expect(page.getByTestId("binding-save")).toBeDisabled();
  await expect(page.getByTestId("deactivate-submit")).toBeDisabled();
  await expect(page.getByTestId("mfa-reset-submit")).toBeDisabled();
  await page.getByTestId("account-drawer-close").click();
  await expect(page.getByTestId("binding-section")).toHaveCount(0);

  await openRow(page, "我自己");
  await expect(page.getByTestId("account-gate-hint")).toContainText("不能修改自己的账号");
  await expect(page.getByTestId("binding-save")).toBeDisabled();
  await expect(page.getByTestId("deactivate-submit")).toBeDisabled();
  // 这一行是 active → 也不给"启用"按钮（后端只有已停用可启用）
  await expect(page.getByTestId("account-activate")).toHaveCount(0);

  expect(rbac.captures.bindingSave).toHaveLength(0);
  expect(rbac.captures.deactivate).toHaveLength(0);
  expect(rbac.captures.mfaReset).toHaveLength(0);
});

test("重置 MFA：理由先拦，成功后提示不带设备条数（204 里没有这个数）", async ({ page }) => {
  const rbac = await mockRbacApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ACCOUNT_AND_MFA } });

  await page.goto("/settings/accounts");
  await openRow(page, "王老师");

  await page.getByTestId("mfa-reset-reason").fill("换手机");
  await page.getByTestId("mfa-reset-submit").click();
  await expect(page.getByText(/MFA 重置理由至少 10 个字/)).toBeVisible();
  expect(rbac.captures.mfaReset).toHaveLength(0);

  await page.getByTestId("mfa-reset-reason").fill("教师换新手机收不到动态码，需重新绑定");
  await page.getByTestId("mfa-reset-submit").click();
  await confirmMessageBox(page);
  const notice = page.getByText("已清除 MFA 绑定并撤销该账号的登录会话");
  await expect(notice).toBeVisible();
  // 停用/启用/重置都回 204，`sessions_revoked` 只进审计 → 界面不能编出"踢了几台"（TD-256）
  await expect(notice).not.toHaveText(/\d|台/);
  expect(rbac.captures.mfaReset).toHaveLength(1);
  await expect(page.getByTestId("mfa-section")).toContainText("没有开启 MFA");
});

test("没有 school.permission_account.manage：菜单不出现，直连被守卫挡在 403 页", async ({
  page,
}) => {
  await mockAuth(page, { authenticated: true, user: { permissions: ["school.role.manage"] } });
  await mockRbacApi(page);

  await page.goto("/");
  await expect(page.getByRole("menuitem", { name: "权限账号" })).toHaveCount(0);

  await page.goto("/settings/accounts");
  await expect(page.getByText("你没有访问该页面的权限")).toBeVisible();
  await expect(page.getByTestId("account-table")).toHaveCount(0);
});
