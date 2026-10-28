# 设计简报：批次直推 → 每功能点 PR（2026-10-28）

## 1. 目标与验收

- 目标：把交付流程从「功能点 commit 在 feature 分支累积 → 批次末 `git branch -f main` 直推」迁移为主流 PR 流；每个功能点一个短分支、一个 PR，合并条件是「PR 的远端 CI 全绿 + 独立审查无 Blocker/Major」。
- 验收：
  1. backend/frontend/evals 只在「面向 `main` 的 PR」与「`main` 推送」自动触发；四个 workflow 均保留 `workflow_dispatch` 手动触发；`security` 另保留每周一定时；不再因 feature 分支 push 与 PR 双跑。
  2. 同一 PR 的新推送取消旧运行（`concurrency`，`cancel-in-progress` 仅对 PR 生效）。
  3. 仓库有 PR 模板；AGENTS/requirements/handoff/README/tech-debt 与实际触发和流程一致。
  4. 流程迁移本身走 PR（#7；#2–#6 已是 Dependabot 开出的 Actions 升级 PR），并在合并后验证 `main` 推送 CI。
  5. 分支保护缺口登记 TD-219，不假装已机器强制。

## 2. 候选方案

### A. 维持批次直推（现状）

- 做法：功能点 commit 在 feature 分支累积，批次末 `git branch -f main` + `git push origin main`。
- 优点：单人操作步骤少；CI 只在批次末跑，Actions 分钟少。
- 缺点：合并前没有 PR 维度的 CI/审查载体；出问题只能在 main 上修；审查结论与 diff 不绑定。
- 风险：批次越大，回滚和定位成本越高；与用户要求的「主流 AI 编程工作流」不符。

### B. PR 流 + GitHub Free（选中）

- 做法：每个功能点新建短分支 → 提交 → push → 开 PR → 远端 CI + 独立审查 → rebase/squash 合并 → 删分支；workflow 只对 PR-to-main 与 main push 触发；PR 模板固化验证/风险/安全清单。
- 优点：合并前有机器门禁与审查载体；历史线性；回滚以 PR 为单位；与主流做法一致。
- 缺点：私有库 + GitHub Free 无法启用 branch protection/ruleset，「CI 绿才合并、禁直推」只能靠纪律；涉及代码的 PR 至少跑受影响侧全量（后端或前端，按路径过滤），Actions 分钟增加。
- 风险：纪律被绕过无机器拦截；纯文档 PR 只触发 `security`（路径过滤的预期行为）。

### C. PR 流 + 升级 GitHub Pro + branch protection

- 做法：B + 升级 Pro，启用 required checks、禁直推。
- 优点：机器强制合并条件，消除纪律缺口。
- 缺点：产生订阅成本；当前单人项目收益有限。
- 风险：升级后需逐项配置 required checks，路径过滤的 workflow 不能直接作为 required check（未触发会挂起）。

## 3. 选择结论

- 选 **B**：满足用户 2026-10-28「每功能点一个 PR」的决定，且不引入额外订阅成本。
- 不选 A：与用户明确要求冲突，且合并前无门禁。
- 暂不选 C：用户 2026-10-28 决定「先不升级」；机器强制缺口登记 **TD-219**，升级 Pro 后再补 required checks 并做「直推被拒」验证。
- 对 spec/design 的影响：更新 `docs/requirements.md` §6.1 与 A11（原「模式 A」废止）、AGENTS §1.6/§6.2/§7；不改业务 spec。

## 4. 实现顺序

1. 数据/配置：四个 workflow 的触发与 `concurrency`；Dependabot Actions 只允许 minor/patch（`ignore` major）。
2. 接口/载体：`.github/pull_request_template.md`（验证、风险与回滚、迁移验证、安全清单）。
3. 文档：AGENTS、requirements、handoff、README、tech-debt、P0 简报；把 PR #1 的合并事实与 rebase 后哈希回填。
4. 流程收口：PR #1 以 PR 合并（已完成）→ 本迁移 PR（#7）→ 合并后验证 main 推送 CI → 关闭与新策略冲突的存量 Dependabot Actions major PR（#2–#6）。

## 5. 测试策略

- 单元/集成/业务场景：本迁移不改业务代码，不新增用例；PR #7 的 CI 复用现有全套。
- 工作流验证：5 个 YAML 用 PyYAML 解析；PR #7 本身验证 PR 触发与 `concurrency`；合并后验证 main 推送只跑一次、`security` 仍触发。
- E2E/评估/性能：不适用——不改业务代码、不改测试栈，无新增链路或性能面。
- 回滚：revert PR #7 恢复旧触发与旧文档；无数据库迁移。
- 纯文档 PR 的预期：只跑 `security`，这是路径过滤的设计结果，不是漏跑。

## 6. 预判技术债

- **TD-219**：无 branch protection，合并条件靠纪律；升级 Pro 后补机器强制。
- 存量 Dependabot Actions major PR（checkout 4→7 等）：与本策略冲突，PR #7 合并后关闭、不合并。
- **TD-222**：`ignore` major 后 Actions 大版本升级不再自动开 PR，需人工按 §2 版本纪律评估；目标 M6（或下一次 Actions 大版本，以先到者为准）。
- 全栈 E2E 仍只在本地/待挂 nightly：**TD-207**（与 TD-183/192 同批）。

## 7. 高风险确认

- 本迁移不触碰认证、租户隔离、权限、状态机、删除、发布、加密、AI 护栏代码；只改 CI 触发与流程文档。
- 用户 2026-10-28 已通过 AskUserQuestion 确认：GitHub Pro「先不升级」、PR 粒度「每功能点一个 PR」。因此本简报无需再次等待确认即可实施。
