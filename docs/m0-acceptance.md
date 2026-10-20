# M0 验收报告（M0-F 收口）

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-20 |
| 范围 | M0-A ~ M0-F（基座、依赖栈、Harness 骨架、前端脚手架、兼容性验证、测试基础设施与验收） |
| 结论 | **M0 全部退出标准达成**（1 项按用户确认调整口径），遗留项均已登记技术债并指明里程碑 |
| 依据 | `docs/milestone.md` §3.3、`docs/test-plan.md`、`docs/review-log.md`、`docs/tech-debt.md`、各功能点评审记录 |

## 1. 退出标准逐条核对

| # | 退出标准（milestone.md §3.3） | 状态 | 证据 |
|---|---|---|---|
| 1 | `docker compose up` 一键启动 core；健康检查通过 | 达成 | 9 个容器全部 healthy（postgres/redis/minio/mailhog/clamav/qdrant/tei-embed/tei-rerank/mineru）；`docs/review-log.md` M0-B |
| 2 | MinerU V1 API `/v1/health` 通过；PDF 在 6GB 下跑通 Standard | 达成 | standard/basic/flash 三档真实解析通过（热态 1.9s/2.0s/0.9s），显存 455–485MiB 未降级；`docs/design-briefs/M0-E-compatibility.md` §8 |
| 3 | 空壳 Loop 发 `meta/step/done` SSE，记录 stop_reason | 达成 | `POST /api/v1/campus/chat/turns`（`DEV_SKELETON_LOOP_ENABLED` 控制，生产默认关）；`backend/tests/test_skeleton_loop.py`、BS-M0-01 |
| 4 | 本地 trace 必发；Langfuse 关闭不影响运行，开启后映射正确（mock 端点） | 达成 | trace sink 注入式断言（BS-M0-01）；Langfuse 4.63.0 自托管栈冷启动 + `evals/scripts/langfuse_smoke.py` 可复现；应用未接入 SDK，关闭态为默认 |
| 5 | Python 3.13 下核心依赖可导入/启动；不兼容项已报告，不静默替换 | 达成 | 分组依赖断言（web/db、rag、agent、auth）；ragas/langchain-community 冲突 → 独立 `evals/` 项目（TD-042） |
| 6 | 前端 admin/campus 可启动；Playwright 可跑通一个空页面 | 达成 | `pnpm build`/`typecheck`/`lint`/`test:unit` 通过；Playwright **5 passed**（含失败态与接口 mock） |
| 7 | 业务场景测试骨架跑通一条示例链路（如"健康检查→trace"） | 达成 | BS-M0-01（3 用例，真实测试栈 + 审计链校验）；`backend/tests/business/test_bs_m0_01_health_to_trace.py` |

补充（M0-E/F 新增验证，超出原清单）：

- **Langfuse 4.63.0 自托管可用性**：冷启动全组件 healthy，SDK 写入→v2 Observations 读取闭环（`docs/review-log.md` M0-E）。
- **CI 门禁**：4 个 GitHub Actions 工作流（backend/frontend/evals/security）+ 本地等效 `scripts/ci.sh`（TD-047 收口）。
- **CPU 嵌入/重排基线**：`docs/benchmarks-m0.md`（并据此暴露重排延迟风险 TD-056）。
- **独立测试环境**：`deploy/docker-compose.test.yml` + `scripts/test-stack.sh`（独立 project/端口/库名/凭据，tmpfs 存储）。

## 2. 测试与门禁总览（M0 收口时）

| 层级 | 命令 | 结果 |
|---|---|---|
| 后端单元/集成/业务场景 | `cd backend && uv run pytest` | **84 passed**（84 = 单元 80 + 业务场景 3 + 集成 1，含真实 PG/Redis/Qdrant；业务场景用例同时带 integration 标记） |
| 后端静态检查 | `uv run ruff check app tests`、`uv run mypy app tests` | 全部通过（mypy strict，44 文件） |
| 评估项目 | `cd evals && uv run ruff check . && uv run pytest` | 通过（2 passed）；用例集校验 `python scripts/check_eval_cases.py` → 195 条、每类 ≥20 |
| 前端质量门禁 | `pnpm lint` / `format:check` / `typecheck` / `test:unit` / `build` / `audit` | 通过（Vitest 5 passed；Prettier 全绿；audit：No known vulnerabilities found） |
| E2E | `pnpm test:e2e` | 5 passed（admin 渲染、campus 渲染、health mock 200/503/超时失败态） |
| 一键本地 CI | `bash scripts/ci.sh` | 见 §4 执行记录 |
| **远端 CI（GitHub Actions）** | `gh run list` | **4 条工作流全绿**（2026-10-20 首次运行修复 4 处配置问题后）：backend 46s、evals 22s、security 40s、frontend（含 e2e）4m45s；路径过滤已生效 |

## 3. 关键工程结论（供 M1+ 使用）

1. **生产定位**：多租户 SaaS，由运营方统一部署一套（平台端 + 校方 B 端 + C 端），校方仅注册使用；Langfuse 为运营方自托管独立主机（`docs/deployment.md` §1/§7）。
2. **MinerU 形态**：官方 4.0.12 软件 + 自建 torch 镜像（非 root、模型源 `auto`、VLM llama-cpp CPU）；后端可在不改应用代码的前提下切换官方 vLLM 路径（`docs/deployment.md` §6.1）。
3. **性能风险前置**：CPU 重排 8 候选 p50≈11.7s，无法进入快路径预算 → TD-056 待 M3/M4 决策。
4. **启动契约**：`/health/live` 与 `/health/ready` 固定在根路径；ready 对 PG/Redis/Qdrant 逐项探活，失败 503 且不回显凭证（TD-005）。
5. **日志契约**：应用与 uvicorn/第三方日志统一单行 JSON（TD-002）。
6. **配置契约**：`REDIS_URL` 必填 fail-fast；`DATABASE_URL`/`QDRANT_URL` 开发可缺省、production 缺失即启动失败；`DEV_SKELETON_LOOP_ENABLED` 生产默认关闭。
7. **依赖安全**：`pip-audit` 首次运行发现真实漏洞并已修复（cryptography 47.0.0→50.0.2、pytest 8.4.2→9.1.1、aiosmtplib 4.0.2→5.1.3）；`evals` 侧 ragas/diskcache 为上游最新版且无修复版本，登记 TD-060 并在 CI 显式 ignore（上游发版后必须移除）。前端 `pnpm audit` 无已知漏洞。

## 4. 执行记录（可复现）

```bash
# 依赖栈
cd deploy && docker compose up -d && docker compose --profile rag up -d
docker compose --profile parser build mineru && docker compose --profile parser up -d

# 独立测试栈（集成/业务场景用）
bash scripts/test-stack.sh up && eval "$(bash scripts/test-stack.sh env)"

# 后端
cd backend && uv run pytest

# 前端
cd frontend && pnpm install && pnpm lint && pnpm typecheck && pnpm test:unit && pnpm test:e2e

# 基准
cd backend && uv run python ../benchmarks/embed_rerank_bench.py --markdown

# 一键
bash scripts/ci.sh
```

## 5. 技术债处置汇总（M0 边界评审）

| 处置 | 条目 |
|---|---|
| 已完成 | TD-004、TD-012、TD-013、TD-014、TD-027、TD-028、TD-037、TD-038、TD-040、TD-042、TD-046、TD-058、**TD-002、TD-005、TD-010、TD-029~035、TD-047、TD-052**（本次 M0-F） |
| 已采纳（进入后续里程碑） | TD-016、TD-018、TD-025、TD-026、TD-036、TD-043、TD-045、TD-048、TD-051、TD-054~057、TD-059、TD-061~063（目标里程碑见台账） |
| 已延后 | TD-001、TD-003、TD-006~009、TD-011、TD-015、TD-017、TD-019~024、TD-032、TD-041、TD-049、TD-053、TD-060 |
| 已缓解 | TD-044（Langfuse DATABASE_URL，M6 结构性收口） |
| 已拒绝 | TD-021（保留 E501 例外）、TD-039（不回溯历史简报）、TD-050（agent 组依赖不拆 optional） |

> 完整清单与理由见 `docs/tech-debt.md`。

## 6. 已知缺口与计划（不隐藏）

| 缺口 | 影响 | 计划 |
|---|---|---|
| CI 工作流尚无远端仓库，未真实执行（本地 `scripts/ci.sh` 已跑通） | 门禁证据需人工触发 | 仓库 push 到 GitHub 后自动生效；首次运行后回填证据 |
| 空壳 Loop 无认证、无真实 LLM | 仅骨架，不能当特性 | M4 用真实 Loop 替换并移除 `DEV_SKELETON_LOOP_ENABLED` |
| Langfuse 未接入 SDK；`LANGFUSE_ENABLED` 无消费方 | 观测能力尚未生效 | M3（评估联动）/M4（trace 上报）接入 |
| 生产部署形态（私有 registry、模型预置、digest 固定、观测主机）未定稿 | 上线前必须收口 | TD-048/049/053/054 + `docs/deployment.md`，M6 |
| CPU 重排延迟超预算 | 影响快路径性能目标 | TD-056，M3/M4 决策 + M6 压测复核 |
| MinerU 复杂文档质量对照未做 | 解析质量无对照数据 | TD-055，M3 |
| 前端 Vant 仍全量样式（199KB CSS） | H5 首屏体积偏大 | TD-032，M6 |
| 本地 npm 镜像不支持 audit endpoint | 本地审计需切官方源（脚本已处理） | 已文档化；CI 使用官方源 |
