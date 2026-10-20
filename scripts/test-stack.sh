#!/usr/bin/env bash
# 独立测试依赖栈（M0-F）：启动/停止/查看，并导出测试用例需要的连接串。
#
# 用法：
#   scripts/test-stack.sh up      # 生成缺失的 deploy/.env.test 并启动测试栈（postgres/redis/qdrant）
#   scripts/test-stack.sh up-all  # 同上并启用 minio profile（需先构建 minio 镜像）
#   scripts/test-stack.sh down    # 停止（保留卷定义；数据在 tmpfs，停止即丢弃）
#   scripts/test-stack.sh status  # 查看容器状态
#   scripts/test-stack.sh env     # 打印可 source 的环境变量（TEST_*_URL）
#
# 说明：测试栈与开发栈/观测栈完全分离（独立 project/端口/库名/凭据）；
# 端口段 5xxxx，避免与 dev、observability 冲突；
# 约束：project/网络/端口均为固定值，同一台机器同时只跑一个测试栈。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="${REPO_ROOT}/deploy"
ENV_FILE="${DEPLOY_DIR}/.env.test"
COMPOSE_FILE="${DEPLOY_DIR}/docker-compose.test.yml"

ensure_env_file() {
  if [[ -f "${ENV_FILE}" ]]; then
    return
  fi
  echo "[test-stack] ${ENV_FILE} 不存在，生成强随机测试凭据（不入库）" >&2
  {
    echo "# 由 scripts/test-stack.sh 自动生成（git 忽略）"
    echo "TEST_POSTGRES_DB=campus_elf_test"
    echo "TEST_POSTGRES_USER=campus_elf_test"
    echo "TEST_POSTGRES_PASSWORD=$(openssl rand -hex 16)"
    echo "TEST_REDIS_PASSWORD=$(openssl rand -hex 16)"
    echo "TEST_MINIO_ROOT_USER=campus_elf_test"
    echo "TEST_MINIO_ROOT_PASSWORD=$(openssl rand -hex 16)"
    echo "TEST_MINIO_IMAGE=minio:RELEASE.2025-10-15T17-29-55Z"
  } > "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
}

read_env_value() {
  local key="$1"
  grep -E "^${key}=" "${ENV_FILE}" | head -1 | cut -d= -f2-
}

compose() {
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

wait_healthy() {
  # 参数：期望容器数（up=3，up-all=4）。
  # 注意：不得吞掉 compose ps 的错误，也不得把「空输出」当成 healthy——
  # 否则 compose/daemon 异常或容器未创建时会假绿。
  local expected="${1:-3}"
  local lines="" count=0
  echo "[test-stack] 等待依赖 healthy（期望 ${expected} 个容器）…"
  for _ in $(seq 1 60); do
    if ! lines="$(compose ps --format '{{.Name}} {{.State}} {{.Health}}')"; then
      echo "[test-stack] compose ps 调用失败，2s 后重试" >&2
      sleep 2
      continue
    fi
    count=0
    if [[ -n "${lines}" ]]; then
      count="$(printf '%s\n' "${lines}" | grep -c .)"
    fi
    if [[ "${count}" -ge "${expected}" ]] \
      && ! printf '%s\n' "${lines}" | grep -qvE 'running healthy$'; then
      echo "[test-stack] 全部 healthy（${count}/${expected}）"
      return 0
    fi
    sleep 2
  done
  echo "[test-stack] 超时：${count}/${expected} 个容器未就绪" >&2
  compose ps >&2
  return 1
}

print_env() {
  local pg_pw redis_pw pg_user pg_db
  pg_pw="$(read_env_value TEST_POSTGRES_PASSWORD)"
  redis_pw="$(read_env_value TEST_REDIS_PASSWORD)"
  pg_user="$(read_env_value TEST_POSTGRES_USER)"
  pg_db="$(read_env_value TEST_POSTGRES_DB)"
  pg_user="${pg_user:-campus_elf_test}"
  pg_db="${pg_db:-campus_elf_test}"
  # 说明行走 stderr：stdout 只保留 `export K=V`，便于 `eval "$(... env)"` 与 CI 解析。
  echo "[test-stack] Redis 固定用 DB 15（满足集成用例的一次性 DB 护栏 DB>=10）；凭据来自 ${ENV_FILE}" >&2
  cat <<EOF
export TEST_DATABASE_URL="postgresql+asyncpg://${pg_user}:${pg_pw}@127.0.0.1:55432/${pg_db}"
export TEST_REDIS_URL="redis://:${redis_pw}@127.0.0.1:56379/15"
export TEST_QDRANT_URL="http://127.0.0.1:56333"
export TEST_MINIO_ENDPOINT="http://127.0.0.1:59000"
EOF
}

print_env_plain() {
  # 供 CI 写入 $GITHUB_ENV：不加 export、不加引号（GITHUB_ENV 不会剥引号，
  # 带引号会让值本身带上 `"`，集成用例的一次性 DB 护栏会把 `"redis://...:15"` 判为非法）。
  local pg_pw redis_pw pg_user pg_db
  pg_pw="$(read_env_value TEST_POSTGRES_PASSWORD)"
  redis_pw="$(read_env_value TEST_REDIS_PASSWORD)"
  pg_user="$(read_env_value TEST_POSTGRES_USER)"
  pg_db="$(read_env_value TEST_POSTGRES_DB)"
  pg_user="${pg_user:-campus_elf_test}"
  pg_db="${pg_db:-campus_elf_test}"
  cat <<EOF
TEST_DATABASE_URL=postgresql+asyncpg://${pg_user}:${pg_pw}@127.0.0.1:55432/${pg_db}
TEST_REDIS_URL=redis://:${redis_pw}@127.0.0.1:56379/15
TEST_QDRANT_URL=http://127.0.0.1:56333
TEST_MINIO_ENDPOINT=http://127.0.0.1:59000
EOF
}

case "${1:-up}" in
  up)
    ensure_env_file
    compose up -d
    wait_healthy 3
    echo "[test-stack] 测试栈已就绪（postgres/redis/qdrant）；在 shell 中执行以下命令导出连接串："
    print_env
    ;;
  up-all)
    # 额外启用 minio profile（需要对象存储的用例；镜像为本项目源码构建产物）
    ensure_env_file
    minio_image="$(read_env_value TEST_MINIO_IMAGE)"
    minio_image="${minio_image:-minio:RELEASE.2025-10-15T17-29-55Z}"
    if ! docker image inspect "${minio_image}" >/dev/null 2>&1; then
      echo "[test-stack] 缺少 MinIO 镜像 ${minio_image}；先构建：" >&2
      echo "  cd deploy/minio && docker build -t ${minio_image} .   # 见 deploy/minio/README.md" >&2
      exit 1
    fi
    compose --profile minio up -d
    wait_healthy 4
    print_env
    ;;
  down)
    [[ -f "${ENV_FILE}" ]] || { echo "[test-stack] 无 ${ENV_FILE}，跳过" ; exit 0; }
    compose --profile minio down
    ;;
  status)
    [[ -f "${ENV_FILE}" ]] || { echo "[test-stack] 未启动（无 ${ENV_FILE}）"; exit 0; }
    compose ps
    ;;
  env)
    ensure_env_file
    print_env
    ;;
  env-plain)
    # CI 用：`bash scripts/test-stack.sh env-plain >> "$GITHUB_ENV"`
    ensure_env_file
    print_env_plain
    ;;
  *)
    echo "用法: $0 {up|up-all|down|status|env|env-plain}" >&2
    exit 2
    ;;
esac
