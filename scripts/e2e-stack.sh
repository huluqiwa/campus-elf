#!/usr/bin/env bash
# M1 验收（TD-110）：全栈 E2E 专用编排。
#
# 用法：
#   scripts/e2e-stack.sh up      # 确保测试栈 → 重置测试库/Redis → 迁移 → 起 backend 容器 → seed
#   scripts/e2e-stack.sh down    # 移除 backend 容器（依赖栈不动，归 test-stack.sh 管）
#   scripts/e2e-stack.sh status  # backend 容器与端口状态
#
# 说明：
# - backend 容器编排独立在 deploy/docker-compose.e2e.yml（project=campus-elf-e2e），
#   通过 external 网络 campus-elf-test-net 复用 campus-elf-test 依赖栈；
# - 不 `down -v`：按 docs/handoff.md 坑 #4 的流程重置测试库（drop schema public cascade），
#   并清空 backend 所用 Redis DB；数据本就在 tmpfs；
# - E2E_JWT_SECRET / E2E_ENCRYPTION_KEK 首次运行随机生成并追加进 deploy/.env.test（git 忽略）；
# - 种子账号与口令写入 deploy/.e2e-accounts.json（git 忽略），供 frontend/e2e-fullstack 用例读取。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="${REPO_ROOT}/deploy"
ENV_FILE="${DEPLOY_DIR}/.env.test"
TEST_COMPOSE_FILE="${DEPLOY_DIR}/docker-compose.test.yml"
E2E_COMPOSE_FILE="${DEPLOY_DIR}/docker-compose.e2e.yml"
BACKEND_PORT=8000
ACCOUNTS_FILE="${DEPLOY_DIR}/.e2e-accounts.json"

e2e_compose() {
  docker compose --env-file "${ENV_FILE}" -f "${E2E_COMPOSE_FILE}" "$@"
}

test_compose() {
  docker compose --env-file "${ENV_FILE}" -f "${TEST_COMPOSE_FILE}" "$@"
}

read_env_value() {
  grep -E "^${1}=" "${ENV_FILE}" 2>/dev/null | head -1 | cut -d= -f2-
}

ensure_e2e_secrets() {
  # E2E 专用 JWT/KEK：仅测试域凭据，生成一次、只存 git 忽略的 .env.test。
  if [[ -z "$(read_env_value E2E_JWT_SECRET)" ]]; then
    echo "E2E_JWT_SECRET=$(openssl rand -hex 32)" >> "${ENV_FILE}"
    echo "[e2e-stack] 已生成 E2E_JWT_SECRET（写入 ${ENV_FILE}，git 忽略）"
  fi
  if [[ -z "$(read_env_value E2E_ENCRYPTION_KEK)" ]]; then
    echo "E2E_ENCRYPTION_KEK=$(openssl rand -base64 32)" >> "${ENV_FILE}"
    echo "[e2e-stack] 已生成 E2E_ENCRYPTION_KEK（写入 ${ENV_FILE}，git 忽略）"
  fi
}

assert_backend_port_free() {
  if (exec 3<>"/dev/tcp/127.0.0.1/${BACKEND_PORT}") 2>/dev/null; then
    exec 3>&- 3<&- || true
    echo "[e2e-stack] 错误：127.0.0.1:${BACKEND_PORT} 已被占用（手动 dev 后端？）。" >&2
    echo "[e2e-stack] 停掉占用进程后重试，本脚本不做端口改配。" >&2
    exit 1
  fi
}

reset_test_database() {
  # 与 docs/handoff.md 坑 #4 相同的文档化流程（仅测试库；owner 角色执行）。
  # 同时清空 backend 所用 Redis DB（失败锁定/吊销水位等状态不跨运行残留）。
  local pg_user pg_db redis_pw
  pg_user="$(read_env_value TEST_POSTGRES_USER)"
  pg_db="$(read_env_value TEST_POSTGRES_DB)"
  redis_pw="$(read_env_value TEST_REDIS_PASSWORD)"
  echo "[e2e-stack] 重置测试库 schema（drop/create public）…"
  test_compose exec -T postgres psql -U "${pg_user:-campus_elf_test}" -d "${pg_db:-campus_elf_test}" -v ON_ERROR_STOP=1 -q \
    -c "drop schema public cascade;" \
    -c "create schema public;" \
    -c "grant usage on schema public to public;" >/dev/null
  echo "[e2e-stack] 清空 backend 所用 Redis DB 0 …"
  test_compose exec -T redis redis-cli --no-auth-warning -a "${redis_pw}" -n 0 FLUSHDB >/dev/null
}

wait_backend_healthy() {
  # 注意：compose v2 没有 `inspect` 子命令；用 `ps --format` 取 Health 列。
  local status="" i
  echo "[e2e-stack] 等待 backend 容器 healthy …"
  for i in $(seq 1 60); do
    status="$(e2e_compose ps --format '{{.Health}}' backend 2>/dev/null | head -1 || true)"
    if [[ "${status}" == "healthy" ]]; then
      echo "[e2e-stack] backend healthy"
      return 0
    fi
    sleep 2
  done
  echo "[e2e-stack] 错误：backend 未在 120s 内 healthy（最后状态：${status:-unknown}）。" >&2
  e2e_compose logs --tail 50 backend >&2 || true
  exit 1
}

cmd_up() {
  # 1) 依赖栈（幂等；同时修正角色口令漂移）；其 compose 文件不含 E2E 必填变量，
  #    先于 ensure_e2e_secrets 执行也不会插值失败。
  bash "${REPO_ROOT}/scripts/test-stack.sh" up
  # shellcheck disable=SC1091
  eval "$(bash "${REPO_ROOT}/scripts/test-stack.sh" env)"

  ensure_e2e_secrets
  # 先移除上一轮的 e2e backend 容器（仅本 project，不动依赖栈），
  # 否则它会占着 8000 触发端口预检；真 dev 后端仍会被预检拦住。
  e2e_compose down --remove-orphans >/dev/null 2>&1 || true
  assert_backend_port_free
  reset_test_database

  # 2) 迁移（宿主机 uv，沿用 handoff §2 的文档化命令）
  echo "[e2e-stack] 迁移测试库到 head …"
  (cd "${REPO_ROOT}/backend" && uv run alembic -x db_url="${TEST_DATABASE_URL}" upgrade head)

  # 3) 起真实后端容器（首次会构建镜像；--build 保证代码变更后重建）
  echo "[e2e-stack] 启动 backend 容器（campus-elf-e2e）…"
  e2e_compose up -d --build backend
  wait_backend_healthy

  # 4) seed（真实 HTTP 注册→审核→开通 + 直写平台/租户账号）
  echo "[e2e-stack] 生成种子数据 …"
  (cd "${REPO_ROOT}/backend" && \
    TEST_DATABASE_URL="${TEST_DATABASE_URL}" \
    TEST_APP_DATABASE_URL="${TEST_APP_DATABASE_URL}" \
    TEST_PLATFORM_DATABASE_URL="${TEST_PLATFORM_DATABASE_URL}" \
    TEST_MAILHOG_API="${TEST_MAILHOG_API}" \
    E2E_BASE_URL="http://127.0.0.1:${BACKEND_PORT}" \
    E2E_ACCOUNTS_FILE="${ACCOUNTS_FILE}" \
    uv run python scripts/e2e_seed.py)

  echo "[e2e-stack] 就绪：backend=http://127.0.0.1:${BACKEND_PORT}，账号文件=${ACCOUNTS_FILE}"
  echo "[e2e-stack] 跑全栈 E2E：cd frontend && pnpm test:e2e:fullstack"
}

cmd_down() {
  e2e_compose down --remove-orphans || true
  echo "[e2e-stack] backend 容器已移除（依赖栈保留；如需一并停止用 scripts/test-stack.sh down）"
}

cmd_status() {
  e2e_compose ps --format '{{.Name}} {{.State}} {{.Health}}' 2>/dev/null || true
  if (exec 3<>"/dev/tcp/127.0.0.1/${BACKEND_PORT}") 2>/dev/null; then
    exec 3>&- 3<&- || true
    echo "127.0.0.1:${BACKEND_PORT} 可连接"
  else
    echo "127.0.0.1:${BACKEND_PORT} 无服务"
  fi
  [[ -f "${ACCOUNTS_FILE}" ]] && echo "账号文件存在：${ACCOUNTS_FILE}" || echo "账号文件不存在（先 up）"
}

case "${1:-}" in
  up) cmd_up ;;
  down) cmd_down ;;
  status) cmd_status ;;
  *) echo "用法：scripts/e2e-stack.sh {up|down|status}" >&2; exit 2 ;;
esac
