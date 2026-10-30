# M2 里程碑边界评审（2026-10-31）

依据：AGENTS.md §10.3（里程碑边界评审台账，未处置不得进入下一里程碑）、`docs/milestone.md` §5.4 最后两条
（TD-264 给出处置；`app/auth` 82% 只认 CI 产物）。

## 1. 目标与验收

1. M2 台账逐条给出处置（已完成 / 已采纳 / 已延后 / 已拒绝），每条有去向与理由。
2. M2-UI3 的 T1–T5 有「改前红 / 改后绿」证据或写明不适用；T6 已由 187f-B 收口。
3. 五条明确挂在边界的条目定口径：TD-233、TD-254、TD-262、TD-263、TD-264。
4. 验收只认 CI 产物：`app/auth` 82.41%（417/506）来自 PR #25 run `41078941427` 与门槛 PR #26 run `41086807009`。

## 2. 候选方案

### 2.1 TD-254（手动 `npx playwright test` 测旧 `dist/`）

- ① 文档与脚本只承认 `pnpm test:e2e`：零成本，但本项目已两次踩坑（187g-B/C），靠纪律不可靠。
- ② 配置加前检：`dist/index.html` 比源码旧就中止。成本是一次 `statSync` 递归（毫秒级），
  CI 不增加构建时间，手动跑会明确提示先 `pnpm build`。**选 ②**；
  不在 `webServer.command` 串 build——那会让 `pnpm test:e2e` 与 CI 各多构建一次，且两个 webServer 并发 build 会互相踩。

### 2.2 TD-263（`.gitattributes` 无文本行尾规则）

- (a) 显式规则 `* text=auto eol=lf`：blob 与工作区统一 LF，一次配置长期有效。实测索引内 461 个文本文件全部 `i/lf`、
  0 个 `i/crlf`，加规则后 `git status` 无额外改动，无重写风险。**选 (a)**。
- (b) 立规矩 + `git ls-files --eol` 人工校验：同一会话已失手三回，不选。

### 2.3 TD-264（本地 CPython 3.13.13 vs CI 3.13.18）

- ① 钉 `.python-version` 到 CI 补丁版本：所有人重下解释器、重建 venv，CI 镜像升级后仍会再分叉。
- ② CI 固定打印 `python -VV` 与 `coverage --version`：零依赖变更，下次读数争议有现成证据。**选 ②**。
  依据：TD-234 已实测两边逐弧完全一致（412/506），版本差异当前不造成读数差，钉版本收益低于成本。

### 2.4 TD-262（文档手抄「距 82% 还缺几条」）

- 脚本追加目标缺口列：82% 已达成、门槛已抬到 82，「目标缺口」概念本身消失，再加列是维护死逻辑。
- **选口径收口**：文档只写脚本输出里的实测 `percent`、`covered/total` 与门槛，不再手算目标缺口。
  本条在边界评审后即按此执行。

### 2.5 TD-233（4 个测试文件仍用全表审计助手）

- 现在收窄：预防性加固，但这些文件没有已知跨租子场景，改动要重跑业务场景套件。
- 延后：M3 首个动这些文件的后端点一并收窄，边界不变（精确条数按租户、必须为空保留全表）。**选延后**，
  理由与 TD-228 相同：没有现存缺陷，且本点边界评审的改动面已包含 E2E 前检与行尾规则，避免混入纯测试重构。

## 3. 实现顺序

1. 配置整改：TD-254 前检（两个 Playwright 配置 + 共享 helper）、TD-263 `.gitattributes`、TD-264 CI 版本打印。
2. 台账处置：更新 `docs/tech-debt.md` 的状态/去向/证据。
3. 验收记录：`docs/milestone.md` §5.4 把 T1 的「82% 未达标」改成最终读数，并补边界评审结论。
4. 状态回填：`docs/handoff.md` §1、`README.md`、`docs/review-log.md`。

## 4. 测试策略

- TD-254 前检：不 build 直接 `playwright test --list` → exit 1 且提示 `pnpm build`；`pnpm build` 后 → exit 0、96 例可枚举。
  再 `touch` 一个源文件制造「源码较新」→ 再次 exit 1（正反两向）。
- TD-263：`git ls-files --eol` 无 `i/crlf`；加规则前后 `git status` 不出现批量改写。
- TD-264：CI 日志出现两行版本输出（本 PR 的 backend CI 自证）。
- 前端全量 mock E2E 96 例、`typecheck` / `lint` / `format:check`。

## 5. 台账处置结论

M2 期间登记的 78 条（来源列含 M2）逐条处置，状态与证据以 `docs/tech-debt.md` 为准：

| 处置 | 条目 |
| --- | --- |
| 已完成（M2 交付，28 条） | TD-174、TD-181、TD-183、TD-187、TD-192、TD-194、TD-197、TD-198、TD-199、TD-200、TD-202、TD-205、TD-223、TD-227、TD-228、TD-229、TD-230、TD-231、TD-234、TD-242、TD-243、TD-244、TD-246、TD-252、TD-254、TD-262、TD-263、TD-264 |
| 已完成（跨期复核，5 条） | TD-069、TD-070、TD-141、TD-152、TD-172 |
| 已拒绝（3 条） | TD-182（不加二次确认，dry_run + 审计为最终防线）、TD-204、TD-267 |
| 已采纳 → M3（10 条 + TD-258） | TD-195、TD-226、TD-232、TD-245、TD-251、TD-253、TD-259、TD-260、TD-261、TD-265；TD-258 的新增代码部分已交付，既有页面部分随 M3 清理 |
| 已延后 → M3（17 条） | TD-184、TD-193、TD-203、TD-233、TD-235、TD-237、TD-238、TD-239、TD-240、TD-241、TD-247、TD-248、TD-249、TD-250、TD-255、TD-256、TD-257 |
| 已采纳 → M6（7 条） | TD-175、TD-176、TD-177、TD-180、TD-189、TD-191、TD-224 |
| 已延后 → M6（11 条） | TD-179、TD-185、TD-186、TD-188、TD-196、TD-201、TD-225、TD-266、TD-268、TD-269、TD-270 |
| 已延后 → M5/M6（1 条） | TD-178 |

跨期条目去向维持：TD-136、TD-144 → M3（已采纳）；TD-123 → M4；TD-129、TD-134、TD-139、TD-142、TD-146、TD-155、
TD-157、TD-158、TD-160、TD-161、TD-166、TD-168、TD-169 → M6（TD-139 为已采纳，其余已延后）。上述跨期条目不计入 78 条。

TD-264 的实现随本 PR 落地，CI 首次打印由本 PR 的 backend run 验证；合并后回填读数。

## 6. 预判技术债

- TD-233 延后到 M3 首个后端点；若届时该批仍不动这 4 个文件，必须重新指定落点，不能静默留在台账。
- TD-254 前检覆盖 `dist` 的 mtime：输入含两个 app 的 `src`/`index.html`/`tsconfig.json`/`vite.config.ts`/`package.json`/`.env*`、
  共享包（`packages/{api-client,ui}` 的 `src`/`package.json`、`packages/config/tsconfig.base.json`）与根 `package.json`/`pnpm-lock.yaml`，
  并把目录 mtime 计入以捕获新增/删除文件；单测与生成的 `d.ts` 不计入（新增/删除它们可能多提示一次 build，偏向安全）。
  仍不校验产物内容哈希，也无法感知由 shell 注入的构建变量——这两项成本所限接受，登记在 TD-254 证据栏。
- 无新增 TD。

## 7. 风险

- `.gitattributes` 改变工作区检出行尾：本机现有工作区仍是 CRLF，但索引已全 LF，`git status` 干净；
  下次重新检出会变 LF。若编辑器强制 CRLF，提交时会被 git 归一，不会污染 blob。
- 前检会让「改了源码但没构建」的直接跑 E2E 明确失败，属预期行为。
