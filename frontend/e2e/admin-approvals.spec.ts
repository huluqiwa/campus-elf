import { expect, test, type Page } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";
import {
  FIXTURE_APPROVAL_REQUESTS,
  approvalPayload,
  approvalRequest,
  mockApprovalsApi,
} from "./helpers/approval-mock";

// M3 KB-02B 审批中心（接口 mock）：待办/已办、游标翻页、详情抽屉（快照/步骤/留痕）、
// 通过/驳回/撤销、自批 403、缺码门禁、路由守卫。
// mock 的判据是手抄后端的（见 approval-mock.ts 的函数名锚点注释）——TD-253 的已知缺口，收口靠 TD-292。

const TODO_ONLY = ["school.approval.todo"];
const TODO_AND_APPROVE = ["school.approval.todo", "school.kb.approval"];
/** 审批人 + 提交/撤销自助（超管或被显式授权的完整形态）。 */
const ALL = ["school.approval.todo", "school.kb.approval", "school.kb.upload"];

const PENDING_OTHER = FIXTURE_APPROVAL_REQUESTS[0]?.id ?? "";
const PENDING_SELF = FIXTURE_APPROVAL_REQUESTS[1]?.id ?? "";

async function confirmMessageBox(page: Page): Promise<void> {
  await page.locator(".el-message-box").getByRole("button", { name: "OK" }).click();
}

/** 打开某一行详情（抽屉以「提交时版本快照」渲染为打开信号）。 */
async function openRow(page: Page, requestId: string): Promise<void> {
  await page.getByTestId(`approval-row-${requestId}`).click();
  await expect(page.getByTestId("approval-drawer")).toBeVisible();
  await expect(page.getByTestId("approval-snapshot")).toBeVisible();
}

test("菜单与待办列表：两条 PENDING、状态标签、未知类型不出现", async ({ page }) => {
  await mockApprovalsApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ALL } });

  await page.goto("/");
  await expect(page.getByRole("menuitem", { name: "审批中心" })).toBeVisible();

  await page.goto("/approvals");
  await expect(page.getByTestId("approval-table")).toBeVisible();
  await expect(page.getByTestId("approval-table").locator(".el-table__row")).toHaveCount(2);
  await expect(page.getByTestId("approval-table")).toContainText("待审批");
  await expect(page.getByTestId("approval-table")).toContainText("校园安全手册");
  await expect(page.getByTestId("approval-table")).toContainText("开发样例");
});

test("已办 tab：自己的提交与历史决定都在；切回待办状态保持", async ({ page }) => {
  await mockApprovalsApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ALL } });

  await page.goto("/approvals");
  await expect(page.getByTestId("approval-page-info-todo")).toContainText("第 1 页");

  await page.getByRole("tab", { name: "已办" }).click();
  await expect(page.getByTestId("approval-page-info-done")).toContainText("第 1 页");
  const doneRows = page.getByTestId("approval-table").locator(".el-table__row");
  // 已办 = 与本人有过任何动作往来的单：0002 自己提交、0003 自己批的、0004 自己驳的、0005 自己撤的
  await expect(doneRows).toHaveCount(4);
  await expect(page.getByTestId("approval-table")).toContainText("已通过");
  await expect(page.getByTestId("approval-table")).toContainText("已驳回");
  await expect(page.getByTestId("approval-table")).toContainText("已撤销");

  // 切回待办：两条 PENDING 原样在（切换是重取，不是丢状态）
  await page.getByRole("tab", { name: "待办" }).click();
  await expect(page.getByTestId("approval-table").locator(".el-table__row")).toHaveCount(2);
});

test("游标翻页：填满一页才有下一页，下一页带的是服务端回的游标", async ({ page }) => {
  const many = Array.from({ length: 25 }, (_, index) =>
    approvalRequest({
      id: `91000000-0000-0000-0000-${String(index).padStart(12, "0")}`,
      title: `待办样例 ${index}`,
      submitter_id: "95000000-0000-0000-0000-000000000009",
      payload: approvalPayload(),
    }),
  );
  await mockApprovalsApi(page, { requests: many });
  await mockAuth(page, { authenticated: true, user: { permissions: ALL } });

  await page.goto("/approvals");
  await expect(page.getByTestId("approval-table").locator(".el-table__row")).toHaveCount(20);
  await expect(page.getByTestId("approval-next-todo")).toBeEnabled();

  await page.getByTestId("approval-next-todo").click();
  await expect(page.getByTestId("approval-page-info-todo")).toContainText("第 2 页");
  await expect(page.getByTestId("approval-table").locator(".el-table__row")).toHaveCount(5);
  await expect(page.getByTestId("approval-next-todo")).toBeDisabled();
  await expect(page.getByTestId("approval-prev-todo")).toBeEnabled();
});

test("详情抽屉：状态、快照、步骤与动作留痕逐项可见", async ({ page }) => {
  await mockApprovalsApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ALL } });

  await page.goto("/approvals");
  await openRow(page, PENDING_OTHER);

  const drawer = page.getByTestId("approval-drawer");
  await expect(drawer.getByTestId("approval-status")).toContainText("待审批");
  await expect(drawer.getByTestId("approval-snapshot")).toContainText("校园安全手册");
  await expect(drawer.getByTestId("approval-snapshot")).toContainText("校园安全手册.pdf");
  await expect(drawer.getByTestId("approval-snapshot")).toContainText("ClamAV");
  await expect(drawer.getByTestId("approval-steps")).toContainText("第 1 步");
  await expect(drawer.getByTestId("approval-actions")).toContainText("提交");
});

test("通过：一次确认后单据出待办、入已办、抽屉状态刷新", async ({ page }) => {
  const mock = await mockApprovalsApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: TODO_AND_APPROVE } });

  await page.goto("/approvals");
  await openRow(page, PENDING_OTHER);

  await page.getByTestId("approval-approve").click();
  await confirmMessageBox(page);

  await expect(page.getByTestId("approval-drawer").getByTestId("approval-status")).toContainText(
    "已通过",
  );
  expect(mock.captures.decideCalls).toEqual([{ id: PENDING_OTHER, action: "approve", body: {} }]);

  // 关抽屉后：待办少一条（changed 触发当前页重读）
  await page.getByTestId("approval-drawer-close").click();
  await expect(page.getByTestId("approval-table").locator(".el-table__row")).toHaveCount(1);
});

test("驳回：空理由前端拦截不发请求；有理由后单据 REJECTED 且理由可见", async ({ page }) => {
  const mock = await mockApprovalsApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: TODO_AND_APPROVE } });

  await page.goto("/approvals");
  await openRow(page, PENDING_OTHER);

  await page.getByTestId("approval-reject-reason-input").fill("   ");
  await page.getByTestId("approval-reject").click();
  await expect(page.getByText("驳回必须填写理由")).toBeVisible();
  expect(mock.captures.decideCalls).toHaveLength(0);

  await page.getByTestId("approval-reject-reason-input").fill("内容与学校口径不符，退回重写");
  await page.getByTestId("approval-reject").click();

  const drawer = page.getByTestId("approval-drawer");
  await expect(drawer.getByTestId("approval-status")).toContainText("已驳回");
  await expect(drawer.getByTestId("approval-reject-reason")).toContainText(
    "内容与学校口径不符，退回重写",
  );
  expect(mock.captures.decideCalls).toEqual([
    {
      id: PENDING_OTHER,
      action: "reject",
      body: { reason: "内容与学校口径不符，退回重写" },
    },
  ]);
});

test("自批：审批自己提交的单 → 后端 403 toast，单据与版本原状", async ({ page }) => {
  const mock = await mockApprovalsApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: TODO_AND_APPROVE } });

  await page.goto("/approvals");
  await openRow(page, PENDING_SELF);

  await page.getByTestId("approval-approve").click();
  await confirmMessageBox(page);

  // 服务端文案进 toast（403 由全局回调提示一次，TD-258）；抽屉状态不变
  await expect(page.getByText("提交人或上传者不能审批该文档版本（无例外通道）")).toBeVisible();
  await expect(page.getByTestId("approval-drawer").getByTestId("approval-status")).toContainText(
    "待审批",
  );
  const decided = mock.captures.decideCalls.filter((call) => call.id === PENDING_SELF);
  expect(decided).toHaveLength(1);
  // mock 的库内状态也没动（自批判据在 mutate 之前）
  const selfRequest = mock.state.requests.find((item) => item.id === PENDING_SELF);
  expect(selfRequest?.status).toBe("PENDING");
});

test("撤销：提交人在已办看到自己的单并撤销成功；非提交人不见撤销按钮", async ({ page }) => {
  const mock = await mockApprovalsApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ALL } });

  await page.goto("/approvals");
  await page.getByRole("tab", { name: "已办" }).click();
  await expect(page.getByTestId("approval-table").locator(".el-table__row")).toHaveCount(4);

  // 自己提交的 0002：抽屉里给撤销
  await openRow(page, PENDING_SELF);
  await page.getByTestId("approval-cancel").click();
  await confirmMessageBox(page);
  await expect(page.getByTestId("approval-drawer").getByTestId("approval-status")).toContainText(
    "已撤销",
  );
  expect(mock.captures.cancelIds).toEqual([PENDING_SELF]);
  await page.getByTestId("approval-drawer-close").click();

  // 他人提交的 0001（在待办里）：没有撤销按钮，只有通过/驳回
  await page.getByRole("tab", { name: "待办" }).click();
  await openRow(page, PENDING_OTHER);
  await expect(page.getByTestId("approval-cancel")).toHaveCount(0);
  await expect(page.getByTestId("approval-approve")).toBeVisible();
});

test("只有 todo 码：通过/驳回/撤销控件全部不出现，给缺码提示", async ({ page }) => {
  await mockApprovalsApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: TODO_ONLY } });

  await page.goto("/approvals");
  await openRow(page, PENDING_OTHER);

  await expect(page.getByTestId("approval-approve")).toHaveCount(0);
  await expect(page.getByTestId("approval-reject")).toHaveCount(0);
  await expect(page.getByTestId("approval-cancel")).toHaveCount(0);
  await expect(page.getByTestId("approval-decide-hint")).toContainText("知识库审批");
});

test("缺 todo 码：菜单不出现，直连 /approvals → 403 页", async ({ page }) => {
  await mockApprovalsApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: ["school.kb.view"] } });

  await page.goto("/");
  await expect(page.getByRole("menuitem", { name: "审批中心" })).toHaveCount(0);

  await page.goto("/approvals");
  await expect(page.getByText("你没有访问该页面的权限")).toBeVisible();
});

test("列表失败：保留旧行 + 「数据可能不是最新」横幅 + 重试成功后横幅消失", async ({ page }) => {
  await mockApprovalsApi(page, { listFailOnRequest: 2 });
  await mockAuth(page, { authenticated: true, user: { permissions: ALL } });

  await page.goto("/approvals");
  await expect(page.getByTestId("approval-table").locator(".el-table__row")).toHaveCount(2);

  // 第二次加载（切到已办）失败：保留空列表但挂横幅
  await page.getByRole("tab", { name: "已办" }).click();
  await expect(page.getByTestId("approval-stale-alert")).toBeVisible();
  await expect(page.getByTestId("approval-stale-alert")).toContainText("数据可能不是最新");

  await page.getByTestId("approval-stale-retry").click();
  await expect(page.getByTestId("approval-stale-alert")).toBeHidden();
  await expect(page.getByTestId("approval-table").locator(".el-table__row")).toHaveCount(4);
});

test("详情 404：提示一次、关抽屉并触发列表刷新", async ({ page }) => {
  const mock = await mockApprovalsApi(page, { detailMissingIds: [PENDING_OTHER] });
  await mockAuth(page, { authenticated: true, user: { permissions: ALL } });

  await page.goto("/approvals");
  const table = page.getByTestId("approval-table");
  await expect(table.locator(".el-table__row")).toHaveCount(2);
  expect(mock.captures.detailIds).toHaveLength(0);

  await page.getByTestId(`approval-row-${PENDING_OTHER}`).click();
  await expect(page.getByText("审批单不存在或已不可见，列表已刷新")).toBeVisible();
  await expect(page.getByTestId("approval-drawer")).toBeHidden();
  expect(mock.captures.detailIds).toHaveLength(1);
  // changed 触发列表重读（重读后该单仍在 mock 的 todo 里，行数不变，但刷新请求确实发生）
  await expect(table.locator(".el-table__row")).toHaveCount(2);
  expect(mock.captures.listUrls).toHaveLength(2);
});

test("409 已被他人处理：toast 服务端文案 + 抽屉刷新为最新状态；该单不入我的已办", async ({
  page,
}) => {
  const mock = await mockApprovalsApi(page);
  await mockAuth(page, { authenticated: true, user: { permissions: TODO_AND_APPROVE } });

  await page.goto("/approvals");
  await openRow(page, PENDING_OTHER);

  // 在抽屉打开期间，另一审批人把单处理掉（直接改 mock 的服务端状态）
  const target = mock.state.requests.find((item) => item.id === PENDING_OTHER);
  if (target) {
    target.status = "APPROVED";
    target.result = "approve";
    target.completed_at = "2026-11-07T09:00:00Z";
  }

  await page.getByTestId("approval-approve").click();
  await confirmMessageBox(page);

  // 409 的服务端文案进 toast；抽屉重读后的 hint 会同时含同前缀文案 → 取 first 防 strict 撞车
  await expect(page.getByText("该审批单已有最终结论").first()).toBeVisible();
  await expect(page.getByTestId("approval-drawer").getByTestId("approval-status")).toContainText(
    "已通过",
  );

  // 关抽屉后待办少一条（changed 重读当前页）
  await page.getByTestId("approval-drawer-close").click();
  await expect(page.getByTestId("approval-table").locator(".el-table__row")).toHaveCount(1);
  // 已办按「本人的动作往来」过滤：处理人是**另一名**审批人，这张单不出现在我的已办
  await page.getByRole("tab", { name: "已办" }).click();
  await expect(page.getByTestId("approval-table").locator(".el-table__row")).toHaveCount(4);
  await expect(page.getByTestId(`approval-row-${PENDING_OTHER}`)).toHaveCount(0);
});
