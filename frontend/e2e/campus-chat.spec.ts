// M4-UI1 C 端聊天核心（接口 mock）：权限渲染、流式正文、兜底/error+重试、
// 会话列表/清空、提交载荷。停止弧（流中断）mock 无法模拟，移交全栈 E2E（UI2）。
// mock 的判据是手抄后端的（见 helpers/chat-mock.ts 注释）——收口靠全栈链⑤。

import { expect, test } from "@playwright/test";

import { mockAuth } from "./helpers/auth-mock";
import { CHAT_PERMISSION, mockChatApi, mockSlowStream } from "./helpers/chat-mock";

test.describe("聊天入口权限渲染", () => {
  test("持 campus.kb.qa → 显示「开始 AI 问答」入口", async ({ page }) => {
    mockAuth(page, {
      user: { ...{ realm: "tenant", roles: ["student"] }, permissions: [CHAT_PERMISSION] },
    });
    await page.goto("/");
    await expect(page.getByTestId("go-chat")).toBeVisible();
  });

  test("无 campus.kb.qa → 入口不渲染", async ({ page }) => {
    mockAuth(page, {
      user: { realm: "tenant", roles: ["student"], permissions: ["campus.chat.use"] },
    });
    await page.goto("/");
    await expect(page.getByTestId("go-chat")).toHaveCount(0);
    await expect(page.getByTestId("go-conversations")).toBeVisible();
  });
});

test.describe("聊天页", () => {
  test.beforeEach(async ({ page }) => {
    mockAuth(page, {
      user: {
        ...{ realm: "tenant", roles: ["student"] },
        permissions: [CHAT_PERMISSION, "campus.chat.use"],
      },
    });
  });

  test("正常流：流式正文 + 步骤提示 + 完成态（meta 后 URL 更新）", async ({ page }) => {
    mockChatApi(page, { mode: "normal" });
    await page.goto("/chat");
    await page.getByTestId("chat-input").locator("textarea").fill("差旅报销怎么办理？");
    await page.getByTestId("send-button").click();

    await expect(page.getByTestId("messages")).toContainText(
      "根据学校规定，差旅报销需要发票和审批单",
    );
    await expect(page.getByTestId("reasoning-collapse")).toContainText("思考过程 (1)");
    await page.getByTestId("reasoning-collapse").click();
    await expect(page.getByText("已找到 2 条相关校规")).toBeVisible();
    // meta 回显 conversation_id → URL 更新（D4）
    await expect(page).toHaveURL(/\/chat\/conv-e2e/);
  });

  test("兜底流：CAPABILITY_FALLBACK 话术 + 尾标", async ({ page }) => {
    mockChatApi(page, { mode: "fallback" });
    await page.goto("/chat");
    await page.getByTestId("chat-input").locator("textarea").fill("量子力学是什么？");
    await page.getByTestId("send-button").click();
    await expect(page.getByTestId("messages")).toContainText("抱歉，我暂时没有在学校知识库中找到");
    await expect(page.getByTestId("messages")).toContainText("（暂无相关资料）");
  });

  test("error 事件：失败态 + 重新提问按钮（重试发新回合）", async ({ page }) => {
    const { handles } = mockChatApi(page, { mode: "error" });
    await page.goto("/chat");
    await page.getByTestId("chat-input").locator("textarea").fill("差旅报销怎么办理？");
    await page.getByTestId("send-button").click();
    await expect(page.getByTestId("retry-button")).toBeVisible();
    await expect(page.getByTestId("messages")).toContainText("回答失败");

    await page.getByTestId("retry-button").click();
    // 重试 = 清掉失败气泡发新回合（turnBodies 两次提交）
    await page.waitForTimeout(200);
    expect(handles.turnBodies.length).toBe(2);
    expect(handles.turnBodies[1]).toMatchObject({ message: "差旅报销怎么办理？" });
  });

  test("提交载荷：message 原样、无多余字段（conversation_id 仅续聊带）", async ({ page }) => {
    const { handles } = mockChatApi(page, { mode: "normal" });
    await page.goto("/chat");
    await page.getByTestId("chat-input").locator("textarea").fill("差旅报销怎么办理？");
    await page.getByTestId("send-button").click();
    await page.waitForTimeout(200);
    expect(handles.turnBodies[0]).toEqual({ message: "差旅报销怎么办理？" });
  });
});

test.describe("会话列表与清空", () => {
  test.beforeEach(async ({ page }) => {
    mockAuth(page, {
      user: {
        ...{ realm: "tenant", roles: ["student"] },
        permissions: [CHAT_PERMISSION, "campus.chat.use"],
      },
    });
  });

  test("列表渲染 + 进入会话加载历史", async ({ page }) => {
    mockChatApi(page, {});
    await page.goto("/conversations");
    await expect(page.getByText("AI 对话")).toBeVisible();
    await page.getByText("AI 对话").click();
    await expect(page).toHaveURL(/\/chat\/conv-e2e/);
    await expect(page.getByTestId("messages")).toContainText("差旅报销怎么办理？");
    await expect(page.getByTestId("messages")).toContainText("差旅报销需要发票和审批单");
  });

  test("多页翻页：cursor 携带 + 两页不重不漏 + 时间展示", async ({ page }) => {
    const cursors: string[] = [];
    void page.route("**/api/v1/campus/conversations*", async (route) => {
      if (route.request().method() === "DELETE") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: '{"cleared":0}',
        });
        return;
      }
      const url = new URL(route.request().url());
      const cursor = url.searchParams.get("cursor");
      const isFirst = !cursor;
      if (cursor) cursors.push(cursor);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          conversations: [
            isFirst
              ? {
                  id: "conv-p1",
                  title: null,
                  status: "active",
                  last_message_at: "2026-11-22T10:00:00+08:00",
                  created_at: "2026-11-22T09:00:00+08:00",
                }
              : {
                  id: "conv-p2",
                  title: null,
                  status: "active",
                  last_message_at: "2026-11-21T10:00:00+08:00",
                  created_at: "2026-11-21T09:00:00+08:00",
                },
          ],
          next_cursor: isFirst ? "cursor-page2" : null,
        }),
      });
    });
    await page.goto("/conversations");
    await expect(page.getByTestId("conversation-conv-p1")).toBeVisible();
    // vant @load 触发第二页（滚动或 check）
    await page.waitForTimeout(500);
    expect(cursors).toContain("cursor-page2");
    await expect(page.getByTestId("conversation-conv-p2")).toBeVisible();
    await expect(page.getByTestId("conversation-conv-p1")).toContainText("2026/11/22");
  });

  test("新对话复位：SPA 内导航（聊过 A → 列表 → 新对话）气泡为空", async ({ page }) => {
    mockChatApi(page, { mode: "normal" });
    await page.goto("/chat");
    await page.getByTestId("chat-input").locator("textarea").fill("差旅报销怎么办理？");
    await page.getByTestId("send-button").click();
    await expect(page).toHaveURL(/\/chat\/conv-e2e/);
    await expect(page.getByTestId("messages")).toContainText("差旅报销需要发票和审批单");

    // SPA 内导航真路径（第 3 轮审查条件 1：goto 全页重载会清 Pinia，测不到复位）：
    // 聊天页 → 会话列表（组件切换）→「新对话」→ /chat 重挂复位
    await page.getByTestId("conversations-link").click();
    await expect(page).toHaveURL(/\/conversations/);
    await page.getByTestId("new-chat-link").click();
    await expect(page).toHaveURL(/\/chat$/);
    await expect(page.getByTestId("messages")).toContainText("有问题就问我吧～");
    await expect(page.getByTestId("messages")).not.toContainText("差旅报销");

    // 新回合载荷不带旧 conversation_id（store 已复位）
    const turnPromise = page.waitForRequest(
      (r) => r.url().includes("/chat/turns") && r.method() === "POST",
    );
    await page.getByTestId("chat-input").locator("textarea").fill("新问题");
    await page.getByTestId("send-button").click();
    const lastBody = JSON.parse((await turnPromise).postData() ?? "{}");
    expect(lastBody).toEqual({ message: "新问题" });
  });

  test("清空会话：确认弹层 → DELETE → 列表移除", async ({ page }) => {
    const { handles } = mockChatApi(page, {});
    await page.goto("/conversations");
    await page.getByTestId("clear-button").click();
    // van-dialog 确认
    await page.getByRole("button", { name: "确认" }).click();
    await page.waitForTimeout(200);
    expect(handles.clearCalls.length).toBe(1);
    expect(handles.clearCalls[0]).toContain("/api/v1/campus/conversations/conv-e2e/messages");
    // 列表移除
    await expect(page.getByText("AI 对话")).toHaveCount(0);
  });
});

test.describe("UI2 体验收口", () => {
  test.beforeEach(async ({ page }) => {
    mockAuth(page, {
      user: {
        ...{ realm: "tenant", roles: ["student"] },
        // 反馈入口要求 campus.feedback.create（后端强制），与真实 student 角色一致
        permissions: [CHAT_PERMISSION, "campus.chat.use", "campus.feedback.create"],
      },
    });
  });

  test("流式增量渲染：首段 delta 到达即上屏（不等 done 整段出现）", async ({ page }) => {
    // 审查 Blocker 回归：store 曾用 push 前的 raw 对象写增量，Vue 不触发渲染，
    // 整个流式退化为「生成结束才整段出现」。夹具把 meta 与首个 delta 分块下发、
    // 之后 hold 1.5s，因此这个窗口内正文可见只可能来自 delta 自身的渲染。
    const HOLD_MS = 2500;
    await mockSlowStream(page, { holdMs: HOLD_MS });
    await page.goto("/chat");
    await page.getByTestId("chat-input").locator("textarea").fill("差旅报销怎么办理？");
    await page.getByTestId("send-button").click();

    const streamingBubble = page.locator('[data-role="assistant"][data-status="streaming"]');
    await expect(streamingBubble).toBeVisible({ timeout: 5_000 });
    // 关键：必须在 hold 窗口内做一次**即时快照**。toContainText 会一直重试到
    // 末段到达后（那时整段出现），对「增量没渲染」毫无分辨力——这是本用例
    // 第一版漏掉该缺陷的原因。这里等待「首段已写入但末段未到」的时刻再读 DOM。
    await page.waitForTimeout(600);
    const snapshot = await page.evaluate(() => {
      const bubble = document.querySelector('[data-role="assistant"][data-status="streaming"]');
      return bubble?.textContent ?? "";
    });
    // hold 期间末段（含 [1]）不可能出现：此刻文本必须只有首段
    expect(snapshot).toContain("根据学校规定，");
    expect(snapshot).not.toContain("[1]");

    // 末段到达后进入终态
    await expect(page.locator('[data-role="assistant"][data-status="final"]')).toContainText(
      "差旅报销需要发票和审批单",
      { timeout: 15_000 },
    );
  });

  test("待机精灵：空闲态渲染（data-testid），发消息后消失", async ({ page }) => {
    mockChatApi(page, { mode: "normal" });
    await page.goto("/chat");
    await expect(page.getByTestId("idle-mascot")).toBeVisible();
    await page.getByTestId("chat-input").locator("textarea").fill("差旅报销怎么办理？");
    await page.getByTestId("send-button").click();
    await expect(page.getByTestId("idle-mascot")).toHaveCount(0);
    await expect(page.getByTestId("messages")).toContainText("差旅报销需要发票和审批单");
  });

  test("待机精灵：进入无消息会话（清空后空态）重新出现", async ({ page }) => {
    // 清空/空历史是「回到待机态」的路径之一（另一条是 /chat 新对话），
    // 全栈链⑤ 覆盖了真库路径，这里用 mock 固定住 UI 判定：messages 为空即显示。
    mockChatApi(page, { mode: "normal" });
    await page.route("**/api/v1/campus/conversations/*/messages*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ messages: [] }),
      });
    });
    await page.goto("/chat/conv-e2e");
    await expect(page.getByTestId("idle-mascot")).toBeVisible();
  });

  test("思考折叠：步骤计数 + 默认收起 + 展开可见明细", async ({ page }) => {
    mockChatApi(page, { mode: "normal" });
    await page.goto("/chat");
    await page.getByTestId("chat-input").locator("textarea").fill("差旅报销怎么办理？");
    await page.getByTestId("send-button").click();
    const collapse = page.getByTestId("reasoning-collapse");
    await expect(collapse).toContainText("思考过程 (1)");
    // 默认收起：明细不可见
    await expect(page.getByText("已找到 2 条相关校规")).toHaveCount(0);
    await collapse.click();
    await expect(page.getByText("已找到 2 条相关校规")).toBeVisible();
  });

  test("引用抽屉：气泡尾入口 → 底部抽屉列明细（文档/章节/页码，chunk_id 不出现）", async ({
    page,
  }) => {
    mockChatApi(page, { mode: "normal" });
    await page.goto("/chat");
    await page.getByTestId("chat-input").locator("textarea").fill("差旅报销怎么办理？");
    await page.getByTestId("send-button").click();
    const btn = page.getByTestId("citation-button");
    await expect(btn).toContainText("引用来源 (1)");
    await btn.click();
    const drawer = page.getByTestId("citation-drawer");
    await expect(drawer).toBeVisible();
    await expect(drawer).toContainText("[1] 财务报销手册");
    await expect(drawer).toContainText("第一章");
    await expect(drawer).toContainText("第 3 页");
    // chunk_id 绝不进 UI：用完整长串断言，短串（如 "c1"）近乎恒真
    expect(await drawer.innerHTML()).not.toContain("chunk-uuid-0001-abcdef");
  });

  test("反馈入口：👍 → 表单提交 → 已反馈标记 + 幂等不再显示入口", async ({ page }) => {
    const { handles } = mockChatApi(page, { mode: "normal" });
    // 反馈提交走真实 POST /campus/feedback mock
    let fbBody: Record<string, unknown> | null = null;
    void page.route("**/api/v1/campus/feedback", async (route) => {
      fbBody = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: '{"feedback_id":"fb-e2e","replayed":false}',
      });
    });
    await page.goto("/chat");
    await page.getByTestId("chat-input").locator("textarea").fill("差旅报销怎么办理？");
    await page.getByTestId("send-button").click();
    const row = page.getByTestId("feedback-row");
    await expect(row).toBeVisible();
    await page.getByTestId("fb-unhelpful").click();
    const form = page.getByTestId("feedback-form");
    await expect(form).toBeVisible();
    // 无用反馈必须选原因
    await page.getByTestId("feedback-submit").click();
    await expect(form).toContainText("请至少选择一个原因标签");
    await page.locator(".van-checkbox").first().click();
    await page.getByTestId("feedback-comment").locator("textarea").fill("引用对不上");
    await page.getByTestId("feedback-submit").click();
    await expect(page.getByTestId("feedback-done")).toContainText("无用");
    await expect(page.getByTestId("feedback-row")).toHaveCount(0);
    expect(fbBody).toMatchObject({
      rating: "unhelpful",
      reason_tags: ["inaccurate"],
      comment: "引用对不上",
      turn_id: "t-e2e",
      trace_id: "r-e2e",
      conversation_id: "conv-e2e",
    });
    expect(handles.turnBodies.length).toBe(1); // 反馈不影响聊天回合
  });
});
