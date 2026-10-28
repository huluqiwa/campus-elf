# P0 质量与安全运维改进（简报）

- 日期：2026-10-28
- 触发：用户批准 P0 四项改进（「好，按你说的做」），并补充要求「不合规的事要尽早用大白话主动说」。
- 范围：**不新增业务功能**，只补质量/安全运维的四条短板；TD-187d 顺延到本单元之后。

## 1. 目标与验收

| 项 | 目标 | 可验证的验收 |
|---|---|---|
| 覆盖率门槛 | 关键包（`app/rbac`、`app/auth`、`app/tenant`、`app/audit`）分支覆盖率不倒退 | CI 的 pytest 产出 `coverage.json`，再由 `backend/scripts/check_coverage.py` 逐包比较基线（`--cov-fail-under` 只看 TOTAL，会掩盖单包倒退）；改坏关键逻辑少测即红 |
| 安全扫描时机 | 密钥/依赖/静态问题在功能分支推送时就发现，不等批次末 | `security` 工作流对 `feature/**` 推送触发 |
| 依赖更新 | 漏洞版本有自动化 PR，且不违反「禁止擅自升降级」 | `.github/dependabot.yml` 存在；包生态仅安全更新，Actions 周更分组 |
| 最小可观测 | 运行时有数据可查、有告警规则可接 | `/metrics` 端点（默认关闭，内网启用）+ 4 类 5 条 Prometheus 告警规则（5xx 比例 + 低流量 5xx 绝对量分开，避免低流量全 5xx 被比例规则吞掉） |

## 2. 候选方案与选择

1. **覆盖率**：A 全仓门槛（噪声大、为数字写废测试）／B 关键包门槛（选）／C 只报告不设门槛（拦不住倒退）。
   选 B：只对租户隔离、认证、RBAC、审计设门槛；先跑基线再定阈值，阈值取基线整数下取整，形成棘轮。
   落地方式：pytest 用 `--cov=app.rbac --cov=app.auth --cov=app.tenant --cov=app.audit --cov-branch --cov-report=json:coverage.json`，再由 `backend/scripts/check_coverage.py` 从 json 逐包汇总分支数并与阈值比较——`--cov-fail-under` 只比较 TOTAL，单包退 10 个点可被另一包涨 20 个点掩盖（审查 M1）。
2. **安全扫描时机**：A `feature/**` 全跑（选）／B 只把 gitleaks 挪到功能分支（省额度，但依赖漏洞仍晚发现）／C 维持现状。
   选 A：代价是每次功能分支推送多几分钟 Actions；收益是泄漏与漏洞当次可见。保留 `main` 与每周一 03:00 定时不变。
3. **依赖更新**：A 全量版本更新（违反「禁止擅自升级」纪律）／B 包生态只开安全更新 + GitHub Actions 周更分组（选）／C 不做。
   选 B：`open-pull-requests-limit: 0` 关闭版本更新、保留安全更新；Actions 周更分组（低风险、CI 全验）。
4. **可观测**：A 直接上 Prometheus+Grafana+Alertmanager 全栈（本期过重）／B 先做 `/metrics` + 告警规则文件 + 接入说明（选）／C 只接 Sentry。
   选 B：先有数据；真实告警通道与看板登记 TD-214（M6）。标签只放有界低敏维度（方法/路由模板/状态码/reason），禁止 tenant_id、user_id、角色名。

## 3. 实现顺序

1. `app/core/metrics.py`：计数器 + ASGI 中间件（路由模板取 `scope["route"]`，避免路径参数造成高基数）。
2. `app/api/metrics.py`：`/metrics`（`METRICS_ENABLED` 默认 false，关闭时 404）。
3. 埋点：HTTP 请求、审计写入失败、权限拒绝、数据范围拒绝、登录失败与锁定。
4. CI：覆盖率步骤、security 触发分支、dependabot。
5. 告警规则：`deploy/observability/prometheus/alerts.yml` + README 接入说明。
6. 测试：指标端点开关、HTTP 计数、审计失败计数、拒绝计数接线；随后跑全量取覆盖率基线并回填阈值。

## 4. 测试策略

- 单元：`tests/test_metrics_unit.py`（端点开关、计数器增量、异常路径不重复计数）。
- 受影响集成：权限拒绝/数据范围拒绝/登录失败的既有用例复跑（埋点不得改变业务行为）。
- 全量后端一次（取覆盖率基线，同时验证中间件不破坏 SSE/异常处理）。
- 静态：ruff / mypy / bandit / `alembic check`（无迁移）。

## 5. 预判技术债

TD-207 全栈 E2E 进 CI/nightly（批次 B 末）、TD-208 变异测试抽查（M3 边界）、TD-209 备份恢复演练（M6）、TD-210 每批次威胁模型 diff（批次 B 起）、TD-211 DAST/镜像扫描/SBOM（M6）、TD-212 安全关键 diff 第二审查者（M3 起）、TD-213 文档表驱动校验（M3）、TD-214 Prometheus/Alertmanager 接入与告警通道（M6 随 OBS-02）。

## 6. 高风险确认

- `/metrics` 暴露面：默认关闭；生产只允许内网抓取，不经 Nginx 对外。
- 指标标签：禁止 PII/租户/用户/角色维度，违反即按安全缺陷处理。
- 覆盖率门槛：必须先测基线，阈值不得拍脑袋；低于基线说明用例有洞，先补测再抬门槛。
- 告警规则阈值是起始值，staging 实测后调；规则文件不接告警通道时不会自动生效（TD-214）。

## 7. 实现结果（2026-10-28 回填）

- 覆盖率基线（真实测试栈全量）：**755 passed / 1573.07s**（第二轮复审整改新增 2 条审计计数用例，未重跑全量；批次 A 末复跑），分支覆盖率
  `app/rbac` 73.87%（164/222）、`app/auth` 81.15%（409/504）、`app/tenant` 81.25%（13/16）、
  `app/audit` 66.67%（4/6）。阈值取向下取整：73 / 81 / 81 / 66，写在
  `backend/scripts/check_coverage.py`；`backend.yml` 与 `scripts/ci.sh` 的 pytest 步骤产出
  `coverage.json` 后调用该脚本。反证：把 `app/auth` 临时抬到 99% → 脚本 exit 1，恢复 81% → exit 0。
- 可观测实现：`app/core/metrics.py`（私有 `CollectorRegistry` + 7 个计数器 + ASGI 中间件）、
  `app/api/metrics.py`（`/metrics`，默认 404）、`METRICS_ENABLED`（默认 false，开启时启动告警）；
  埋点覆盖 HTTP、权限拒绝、数据范围拒绝、登录失败/锁定、审计写入失败（flush + commit 两层，
  best-effort 路径用 `flushed` 标记避免重复计数）。
- 告警：4 类 **5 条**——5xx 比例（有流量时）、低流量 5xx 绝对量（审查 m4 补充）、审计写入失败、
  权限/范围拒绝激增、登录失败或锁定激增；阈值与抓取/通知通道随 TD-214 在 staging 校准。
- Dependabot：包生态只开安全更新、Actions 周更分组 minor/patch；`security` 工作流补
  `pull_request`（main）与 `permissions: contents: read`；AGENTS §7 写明 Dependabot 自动创建的
  依赖 PR（安全更新 + Actions 周更）是「无人工 PR」的唯一例外。
- 未由代码完成、需仓库管理员操作：在 GitHub 仓库 Settings 打开 **Dependabot alerts** 与
  **security updates**，并用一次真实安全 PR 验证 `security` 工作流随 PR 运行；已写入
  `docs/handoff.md` §7 交接清单。`pull_request` 触发需推送远端后才生效。
- 审查联动：两轮独立审查 1 Blocker / 7 Major / 5 Minor + 0 Blocker / 0 Major / 8 Minor 全部处置
  （第二轮含 count-once 回归用例 2 条、标签名白名单断言、用例数与 README/AGENTS/handoff 事实更正）；
  新增 TD-216（标签值收敛，已延后→M3），TD-214 增补多进程、反代拒绝与流错误可见性校准，
  TD-206 已完成（commit `29de4ec` + `c8e60e1`）。
