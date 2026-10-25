import { expect, test } from "@playwright/test";

import { field } from "../e2e/helpers/locators";
import { apiGet, apiLogin, apiPost, loadAccounts } from "./helpers/support";

// M2-UI1 全栈 E2E（真实后端）：学年流转链路 + 批量导入链路。
// 接口侧走 Node 真实登录（与 M1 验收同模式）；浏览器侧验证关键页面可达。

test("学年流转链路：建三届班 → dry_run 预览 → 执行 → 归档届退出默认列表 → 重复幂等键 409", async () => {
  const accounts = loadAccounts();
  const token = await apiLogin(accounts, {
    realm: "tenant",
    school_code: accounts.school_ops.school_code,
    login_id: accounts.school_ops.login_id,
    password: accounts.school_ops.password,
  });

  // 建三届班级（跨届同名共存，验证 ORG-06 唯一键语义）
  const created: string[] = [];
  const cohorts: Array<[number, string, string]> = [
    [2023, "高三", "1班"],
    [2024, "高二", "1班"],
    [2025, "高一", "1班"],
  ];
  for (const [entryYear, grade, name] of cohorts) {
    const response = await apiPost(accounts, "/api/v1/school/classes", token, {
      entry_year: entryYear,
      grade,
      name,
    });
    expect(response.status).toBe(201);
    created.push((response.json as { id: string }).id);
  }
  // 同届同名 → 409
  const dup = await apiPost(accounts, "/api/v1/school/classes", token, {
    entry_year: 2025,
    grade: "高一",
    name: "1班",
  });
  expect(dup.status).toBe(409);

  // dry_run 预览
  const key = `e2e-rollover-${Date.now()}`;
  const preview = await apiPost(accounts, "/api/v1/school/classes/rollover", token, {
    advances: [{ from_grade: "高一", to_grade: "高二" }],
    archive_grades: ["高三"],
    idempotency_key: key,
    dry_run: true,
  });
  expect(preview.status).toBe(200);
  const previewBody = preview.json as {
    dry_run: boolean;
    advanced: unknown[];
    archived: unknown[];
  };
  expect(previewBody.dry_run).toBe(true);
  expect(previewBody.advanced).toHaveLength(1); // 2025级1班
  expect(previewBody.archived).toHaveLength(1); // 2023级1班

  // 执行
  const done = await apiPost(accounts, "/api/v1/school/classes/rollover", token, {
    advances: [{ from_grade: "高一", to_grade: "高二" }],
    archive_grades: ["高三"],
    idempotency_key: key,
    dry_run: false,
  });
  expect(done.status).toBe(200);
  expect((done.json as { advanced_count: number }).advanced_count).toBe(1);

  // 归档届退出默认列表、显式可见
  const list = await apiGet(accounts, "/api/v1/school/classes?limit=100", token);
  const listIds = (list.json as { items: Array<{ id: string }> }).items.map((item) => item.id);
  expect(listIds).not.toContain(created[0]);
  const listAll = await apiGet(
    accounts,
    "/api/v1/school/classes?limit=100&include_archived=true",
    token,
  );
  const archived = (listAll.json as { items: Array<{ id: string; status: string }> }).items.find(
    (item) => item.id === created[0],
  );
  expect(archived?.status).toBe("archived");

  // 重复幂等键 → 409
  const replay = await apiPost(accounts, "/api/v1/school/classes/rollover", token, {
    advances: [{ from_grade: "高一", to_grade: "高二" }],
    archive_grades: ["高三"],
    idempotency_key: key,
    dry_run: false,
  });
  expect(replay.status).toBe(409);
});

test("学年流转页面：校管登录后可从菜单进入并向导第一步渲染", async ({ page }) => {
  const accounts = loadAccounts();
  await page.goto("/login");
  await field(page, "school-code").fill(accounts.school_ops.school_code);
  await field(page, "login-id").fill(accounts.school_ops.login_id);
  await field(page, "password").fill(accounts.school_ops.password);
  await page.getByTestId("login-submit").click();

  await expect(page.getByTestId("identity")).toContainText("校方");
  await page.getByRole("menuitem", { name: "学年流转" }).click();
  await expect(page.getByText("升年级映射")).toBeVisible();
  await expect(page.getByTestId("rollover-preview")).toBeDisabled();
});

test("批量导入链路：上传教师 CSV → 轮询完成 → 成功计数正确", async () => {
  const accounts = loadAccounts();
  const token = await apiLogin(accounts, {
    realm: "tenant",
    school_code: accounts.school_ops.school_code,
    login_id: accounts.school_ops.login_id,
    password: accounts.school_ops.password,
  });

  const csv = ["工号,姓名", `E-${Date.now() % 100000},导入老师`].join("\n");
  const form = new FormData();
  form.set("type", "teachers");
  form.set("conflict", "error");
  form.set("file", new Blob([csv], { type: "text/csv" }), "teachers-e2e.csv");
  const upload = await fetch(`${accounts.e2e_base_url}/api/v1/school/imports`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}` },
    body: form,
  });
  expect(upload.status).toBe(201);
  const jobId = ((await upload.json()) as { id: string }).id;

  // 轮询到终态（bcrypt 建档较慢，上限 60s）
  let job: { status: string; success_rows: number } | null = null;
  for (let i = 0; i < 60; i += 1) {
    const poll = await apiGet(accounts, `/api/v1/school/imports/${jobId}`, token);
    job = poll.json as { status: string; success_rows: number };
    if (job.status !== "pending" && job.status !== "running") break;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  expect(job?.status).toBe("completed");
  expect(job?.success_rows).toBe(1);
});
