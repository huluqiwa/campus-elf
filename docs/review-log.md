# 校园陪伴精灵 — 代码审查日志（review-log.md）

> 每个功能点完成后追加一条；没有审查记录的功能点不得标记完成。
> 详细清单见 `docs/code-review-checklist.md`。

| 功能点 | 日期 | commit/diff | 审查人/agent | Blocker | Major | Minor | 修复/复测 | 结论 |
|---|---|---|---|---|---|---|---|---|
| 文档阶段（非代码） | 2026-10-19 | docs/* | 待用户确认 | 0 | 0 | 0 | N/A | 已确认 |
| M0-A 后端最小骨架 | 2026-10-19 | feature/M0-foundation | 独立 Explore review agent | 0 | 1 | 12 | Major 已修复；低成本 Minor 已修；其余记录 | 可提交 |

### M0-A 后端最小骨架

- 日期：2026-10-19
- 范围：`backend/pyproject.toml`、`app/config.py`、`app/core/logging.py`、`app/api/health.py`、`app/main.py`、`tests/test_health.py`、`backend/README.md`
- 审查发现：
  - **Major**：健康检查挂在 `/api/v1` 下，与 spec/部署契约 `/health/live|ready` 不一致，探针会 404。已修复为根路径挂载，并新增回归测试 `test_health_not_under_api_prefix`。
  - **Minor 已修**：配置 `env_file` 锚定项目根目录；日志级别显式校验 + `force=True`；健康响应 `Literal["ok"]`；`docs/redoc` 可按环境关闭；测试 fixture 类型、完整响应断言、根路径断言；README 同步；mypy 覆盖 `app tests`。
  - **Minor 记录/后续**：uvicorn/httpx 标准库日志暂未统一 JSON（M0-B 处理）；ready 依赖检查在 M0-B 接入；版本号重复、`extra="ignore"`、`ignore_missing_imports` 全局放宽、`create_app` 工厂化留待后续收紧。
- 复测：
  - `uv run ruff check .`：通过
  - `uv run pytest`：3 passed
  - `uv run mypy app tests`：通过
  - 健康检查：`/health/live`、`/health/ready` 200；`/api/v1/health/live` 404
- 结论：可提交。

## 记录模板

```markdown
### <功能点 ID / 名称>
- 日期：
- commit/diff：
- 审查人/agent：
- 范围：
- Findings：
  - Blocker：
  - Major：
  - Minor：
- 修复：
- 复测：
  - 单元：
  - 集成：
  - 业务场景：
  - E2E：
  - AI 评估：
- 复审：
- 结论：可提交 / 阻塞
```

## 高风险功能复审记录

| 功能点 | 复审人 | 日期 | 结论 |
|---|---|---|---|
| M1 认证/令牌 | 用户 | 待定 | 待定 |
| M1 租户隔离/RLS | 用户 | 待定 | 待定 |
| M3 KB 审批/发布 | 用户 | 待定 | 待定 |
| M4 AI 护栏/SSE | 用户 | 待定 | 待定 |
| M5 账号删除状态机 | 用户 | 待定 | 待定 |
| M6 配置双人确认 | 用户 | 待定 | 待定 |
| M6 Langfuse 外送/脱敏 | 用户 | 待定 | 待定 |
