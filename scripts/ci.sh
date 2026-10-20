#!/usr/bin/env bash
# 本地 CI 等效脚本（M0-F）：在开发机上跑与 .github/workflows 相同的门禁步骤。
#
# 用法：
#   scripts/ci.sh                 # 全量：后端 + evals + 前端 + 安全
#   scripts/ci.sh --with-e2e      # 追加 Playwright e2e（较慢，默认跳过）
#   scripts/ci.sh --no-frontend   # 跳过前端（未安装 pnpm 依赖时）
#   scripts/ci.sh --no-security   # 跳过安全扫描（缺 gitleaks/网络受限时）
#
# 与 .github/workflows 的有意差异：
#   - e2e 默认跳过，用 --with-e2e 开启（CI 中为独立 job）；
#   - gitleaks 本机未安装时 SKIP（CI 由 gitleaks-action 执行）；
#   - frontend 依赖沿用本机 node_modules（CI 会先 pnpm install --frozen-lockfile；缺失时前端整块 SKIP）；
#   - 其余步骤（uv sync --frozen / ruff / mypy / pytest / evals / lint / typecheck /
#     unit / format:check / build / audit / bandit / pip-audit）与 CI 一一对应。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

WITH_FRONTEND=1
WITH_SECURITY=1
WITH_E2E=0
for arg in "$@"; do
  case "${arg}" in
    --with-e2e) WITH_E2E=1 ;;
    --no-frontend) WITH_FRONTEND=0 ;;
    --no-security) WITH_SECURITY=0 ;;
    *) echo "未知参数：${arg}" >&2; exit 2 ;;
  esac
done

FAILURES=0

step() {
  local name="$1"
  shift
  echo ""
  echo "=== ${name} ==="
  if "$@"; then
    echo "[ci] PASS ${name}"
  else
    echo "[ci] FAIL ${name}" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

skip() {
  echo ""
  echo "=== ${1} ==="
  echo "[ci] SKIP ${1}：${2}"
}

has_cmd() { command -v "$1" >/dev/null 2>&1; }

# ---------- 后端 ----------
if has_cmd uv; then
  step "backend: lock check" bash -c "cd backend && uv lock --check"
  step "backend: sync --frozen" bash -c "cd backend && uv sync --frozen"
  step "backend: ruff" bash -c "cd backend && uv run ruff check app tests"
  step "backend: mypy" bash -c "cd backend && uv run mypy app tests"
  step "backend: 测试栈 up" bash scripts/test-stack.sh up
  eval "$(bash scripts/test-stack.sh env)"
  export TEST_DATABASE_URL TEST_REDIS_URL TEST_QDRANT_URL
  # 不加 -q：addopts 已含 -q，叠加会导致汇总行被跳过（CI 日志看不到通过数）。
  step "backend: pytest（单元+集成+业务场景）" bash -c "cd backend && uv run pytest"
else
  skip "backend" "未找到 uv"
fi

# ---------- evals ----------
if has_cmd uv; then
  step "evals: lock check" bash -c "cd evals && uv lock --check"
  step "evals: sync --frozen" bash -c "cd evals && uv sync --frozen"
  step "evals: ruff" bash -c "cd evals && uv run ruff check src tests scripts"
  step "evals: pytest" bash -c "cd evals && uv run pytest"
  step "evals: 用例集完整性" bash -c "cd backend && uv run python ../scripts/check_eval_cases.py"
else
  skip "evals" "未找到 uv"
fi

# ---------- 前端 ----------
audit_frontend() {
  # 本地常用的 npmmirror 不实现 audit endpoint；失败时退回官方 registry 重试。
  local out
  if out=$(cd frontend && pnpm audit --audit-level high 2>&1); then
    echo "${out}" | tail -3
    return 0
  fi
  if echo "${out}" | grep -q "AUDIT_ENDPOINT_NOT_EXISTS"; then
    echo "[ci] 当前 registry 不支持 audit endpoint，改用 registry.npmjs.org 重试"
    if out=$(cd frontend && pnpm audit --registry=https://registry.npmjs.org --audit-level high 2>&1); then
      echo "${out}" | tail -3
      return 0
    fi
  fi
  echo "${out}" >&2
  return 1
}

if [[ "${WITH_FRONTEND}" -eq 1 ]]; then
  if has_cmd pnpm && [[ -d frontend/node_modules ]]; then
    step "frontend: lint" bash -c "cd frontend && pnpm lint"
    step "frontend: typecheck" bash -c "cd frontend && pnpm typecheck"
    step "frontend: unit" bash -c "cd frontend && pnpm test:unit"
    step "frontend: format:check" bash -c "cd frontend && pnpm format:check"
    step "frontend: build" bash -c "cd frontend && pnpm build"
    step "frontend: audit" audit_frontend
    if [[ "${WITH_E2E}" -eq 1 ]]; then
      step "frontend: e2e" bash -c "cd frontend && pnpm test:e2e"
    else
      skip "frontend: e2e" "默认跳过；用 --with-e2e 开启（CI 中为独立 job）"
    fi
  else
    skip "frontend" "未找到 pnpm 或未执行 pnpm install"
  fi
fi

# ---------- 安全 ----------
if [[ "${WITH_SECURITY}" -eq 1 ]]; then
  if has_cmd gitleaks; then
    # 与 CI 一致：全历史 + 合并提交跳过 + 命中即失败（exit-code=2）。
    step "security: gitleaks" gitleaks detect --config .gitleaks.toml --redact -v --no-banner --exit-code=2 --log-opts=--no-merges
  else
    skip "security: gitleaks" "本机未安装 gitleaks（CI 由 gitleaks-action 执行）"
  fi
  step "security: bandit" bash -c "cd backend && uv run --with bandit bandit -q -r app -x tests"
  step "security: pip-audit（backend）" bash -c "cd backend && uv run --with pip-audit pip-audit --skip-editable"
  # 忽略项见 TD-060（上游最新版且无修复；evals 不进生产）
  step "security: pip-audit（evals）" bash -c "cd evals && uv run --with pip-audit pip-audit --skip-editable --ignore-vuln PYSEC-2026-2447 --ignore-vuln PYSEC-2026-3046"
fi

echo ""
if [[ "${FAILURES}" -gt 0 ]]; then
  echo "[ci] 结果：失败 ${FAILURES} 项" >&2
  exit 1
fi
echo "[ci] 结果：全部通过"
