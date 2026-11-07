# M3-KB-04B — 测试沙箱前端（KB-04 前端点）实现前设计简报

| 项 | 内容 |
|---|---|
| 日期 | 2026-11-08 |
| 状态 | 已确认（按推荐项执行；用户未逐项响应，沿用惯例） |
| 关联功能点 | M3 第 9 点：KB-04B（KB-04 前端点；KB-04A 后端已合并 `4f569df`）。纯前端，`backend/` 零改动 |
| 验收引用 | `docs/design-briefs/M3-KB-04A.md`（后端契约）、KB-01B/KB-02B 前端先例、TD-291/TD-296 |
| 不做的事 | SSE 事件流前端消费（TD-296 延后至有真实需求）；审核中心前端改动（KB-02B 已完成） |

## 0. 现状

- api-client 无 `testSet`/`testRun` 方法；
- KB 抽屉无测试相关 UI；TD-291 静态指引待替换；
- 既有前端模式（KB-01B/KB-02B）：api-client → 判据纯函数层 → 视图/抽屉 → mock E2E。

## 1. 目标

1. api-client：`testSet.ts` 三方法（upload/run/query）+ 契约类型。
2. KB 抽屉测试区：上传测试集（file input + name input）→ 运行按钮 → 轮询结果 → TEST_PASSED/FAILED 展示；TD-291 静态指引替换为真实审批状态与驳回理由。
3. 判据纯函数层：`test-logic.ts`（状态文案、testGate、fileValidation）。
4. mock E2E：`test-mock.ts` + `admin-kb.spec.ts` 追加用例。

## 2. 候选方案

### D1 测试 UI 位置
- **A（推荐）：KB 文档抽屉内新增「测试」区块**（status=READY/TEST_* 时显示）——与审批/构建入口同一位置，一致性最好。
- B：独立 `/kb/testing` 路由。缺点：导航跳转打断文档上下文。不选。

### D2 TD-291 静态指引替换
- **A（推荐）：调用 approvals 端点取最近一单，渲染审批状态 + 驳回理由**；无审批单时不显示。
- B：维持静态指引。缺点：TD-291 已有端点，不消费等于欠账。不选。

## 3. 选择结论
- **D1 A／D2 A**。用户未逐项响应，按推荐执行。

## 4. 测试策略
- 单测：api-client + test-logic。
- mock E2E：`admin-kb.spec.ts` 追加上传→运行→轮询→结果→TD-291 直显用例。
- 静态门禁：typecheck/lint/format/build。

## 5. 预判技术债
| TD | 条目 | 处置 |
|---|---|---|
| TD-301 | SSE 进度条未做（TD-296 延后口径不变） | 维持延后 |
EOF

## 9. 交付事实（2026-11-08）

- **PR #49**（`feature/m3-kb04b-test-frontend`，head `ad1b3c9`）→ rebase 合并，分支已删除。合并授权：用户在 PR CI 全绿后确认。
- **PR CI**：frontend/e2e run（单测 **85 + 146 passed**、Playwright **139 passed / 3m17s**）、security run success。
- **本地门禁**：typecheck/lint/format/build 全绿；单测 **231**（85+146，+14）；mock E2E **139 passed**（基线 137 + 测试沙箱 2 条）；`backend/` 零改动。
- **TD**：TD-291 已兑现（前端直显审批驳回理由，替换 KB-02B 静态指引）；TD-296 维持延后。
