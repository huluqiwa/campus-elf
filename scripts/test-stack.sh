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
    echo "TEST_APP_PASSWORD=$(openssl rand -hex 16)"
    echo "TEST_PLATFORM_PASSWORD=$(openssl rand -hex 16)"
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

ensure_app_password() {
  if [[ -z "$(read_env_value TEST_APP_PASSWORD)" ]]; then
    echo "TEST_APP_PASSWORD=$(openssl rand -hex 16)" >> "${ENV_FILE}"
  fi
  if [[ -z "$(read_env_value TEST_PLATFORM_PASSWORD)" ]]; then
    echo "TEST_PLATFORM_PASSWORD=$(openssl rand -hex 16)" >> "${ENV_FILE}"
  fi
  if [[ -z "$(read_env_value TEST_PLATFORM_REVIEW_TOKEN)" ]]; then
    echo "TEST_PLATFORM_REVIEW_TOKEN=$(openssl rand -hex 24)" >> "${ENV_FILE}"
  fi
}

cleanup_legacy_roles() {
  # 清理早期版本遗留的角色（它们对审计表仍带 UPDATE/DELETE 权限，且 pg_default_acl 不随迁移回滚）。
  # 仅测试集群执行；逐条语句容错（角色不存在/无权限都不应阻断启动）。
  local pg_user pg_db psql_run
  pg_user="$(read_env_value TEST_POSTGRES_USER)"
  pg_db="$(read_env_value TEST_POSTGRES_DB)"
  psql_run() {
    docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" exec -T postgres       psql -U "${pg_user:-campus_elf_test}" -d "${pg_db:-campus_elf_test}" -q -c "$1" >/dev/null 2>&1 || true
  }
  for legacy in campus_elf_app campus_elf_test_app campus_elf_tenant_login campus_elf_platform_login; do
    psql_run "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ${legacy}"
    psql_run "REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ${legacy}"
    psql_run "REVOKE ALL ON SCHEMA public FROM ${legacy}"
    psql_run "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM ${legacy}"
    psql_run "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM ${legacy}"
    psql_run "DROP ROLE IF EXISTS ${legacy}"
  done
}

ensure_app_roles() {
  # 应用必须用**非超级用户**连接，RLS 才真正生效（超级用户连 FORCE RLS 也绕过）。
  # 且平台侧与租户侧分离到两个登录角色（对应迁移创建的组角色
  # campus_elf_tenant / campus_elf_platform），口令随机、只在本机 .env.test。
  cleanup_legacy_roles
  ensure_app_password
  local tenant_pw platform_pw pg_user pg_db
  tenant_pw="$(read_env_value TEST_APP_PASSWORD)"
  platform_pw="$(read_env_value TEST_PLATFORM_PASSWORD)"
  pg_user="$(read_env_value TEST_POSTGRES_USER)"
  pg_db="$(read_env_value TEST_POSTGRES_DB)"
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" exec -T postgres \
    psql -U "${pg_user:-campus_elf_test}" -d "${pg_db:-campus_elf_test}" -v ON_ERROR_STOP=1 -q \
    -c "DO \$\$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'campus_elf_test_tenant') THEN
            CREATE ROLE campus_elf_test_tenant LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE PASSWORD '${tenant_pw}';
          ELSE
            ALTER ROLE campus_elf_test_tenant PASSWORD '${tenant_pw}';
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'campus_elf_test_platform') THEN
            CREATE ROLE campus_elf_test_platform LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE PASSWORD '${platform_pw}';
          ELSE
            ALTER ROLE campus_elf_test_platform PASSWORD '${platform_pw}';
          END IF;
        END \$\$;" \
    -c "DO \$\$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'campus_elf_tenant') THEN
            EXECUTE 'GRANT campus_elf_tenant TO campus_elf_test_tenant';
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'campus_elf_platform') THEN
            EXECUTE 'GRANT campus_elf_platform TO campus_elf_test_platform';
          END IF;
        END \$\$;" >/dev/null
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
  local pg_pw redis_pw app_pw platform_pw pg_user pg_db
  pg_pw="$(read_env_value TEST_POSTGRES_PASSWORD)"
  redis_pw="$(read_env_value TEST_REDIS_PASSWORD)"
  app_pw="$(read_env_value TEST_APP_PASSWORD)"
  platform_pw="$(read_env_value TEST_PLATFORM_PASSWORD)"
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
# 非超级用户连接（RLS 只在非超级用户下生效）：租户侧与平台侧分离
export TEST_APP_DATABASE_URL="postgresql+asyncpg://campus_elf_test_tenant:${app_pw}@127.0.0.1:55432/${pg_db}"
export TEST_PLATFORM_DATABASE_URL="postgresql+asyncpg://campus_elf_test_platform:${platform_pw}@127.0.0.1:55432/${pg_db}"
export TEST_SMTP_HOST="127.0.0.1"
export TEST_SMTP_PORT="51025"
export TEST_MAILHOG_API="http://127.0.0.1:58025"
export TEST_OBJECT_STORE_DIR="${REPO_ROOT}/.zcode/tmp/objects-test"
export TEST_PLATFORM_REVIEW_TOKEN="$(read_env_value TEST_PLATFORM_REVIEW_TOKEN)"
EOF
}

print_env_plain() {
  # 供 CI 写入 $GITHUB_ENV：不加 export、不加引号（GITHUB_ENV 不会剥引号，
  # 带引号会让值本身带上 `"`，集成用例的一次性 DB 护栏会把 `"redis://...:15"` 判为非法）。
  local pg_pw redis_pw app_pw platform_pw pg_user pg_db
  pg_pw="$(read_env_value TEST_POSTGRES_PASSWORD)"
  redis_pw="$(read_env_value TEST_REDIS_PASSWORD)"
  app_pw="$(read_env_value TEST_APP_PASSWORD)"
  platform_pw="$(read_env_value TEST_PLATFORM_PASSWORD)"
  pg_user="$(read_env_value TEST_POSTGRES_USER)"
  pg_db="$(read_env_value TEST_POSTGRES_DB)"
  pg_user="${pg_user:-campus_elf_test}"
  pg_db="${pg_db:-campus_elf_test}"
  cat <<EOF
TEST_DATABASE_URL=postgresql+asyncpg://${pg_user}:${pg_pw}@127.0.0.1:55432/${pg_db}
TEST_REDIS_URL=redis://:${redis_pw}@127.0.0.1:56379/15
TEST_QDRANT_URL=http://127.0.0.1:56333
TEST_MINIO_ENDPOINT=http://127.0.0.1:59000
TEST_APP_DATABASE_URL=postgresql+asyncpg://campus_elf_test_tenant:${app_pw}@127.0.0.1:55432/${pg_db}
TEST_PLATFORM_DATABASE_URL=postgresql+asyncpg://campus_elf_test_platform:${platform_pw}@127.0.0.1:55432/${pg_db}
TEST_SMTP_HOST=127.0.0.1
TEST_SMTP_PORT=51025
TEST_MAILHOG_API=http://127.0.0.1:58025
TEST_OBJECT_STORE_DIR=${REPO_ROOT}/.zcode/tmp/objects-test
TEST_PLATFORM_REVIEW_TOKEN=$(read_env_value TEST_PLATFORM_REVIEW_TOKEN)
EOF
}

case "${1:-up}" in
  up)
    ensure_env_file
    compose up -d
    wait_healthy 4
    ensure_app_roles
    echo "[test-stack] 测试栈已就绪（postgres/redis/qdrant/mailhog）；在 shell 中执行以下命令导出连接串："
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
    wait_healthy 5
    ensure_app_roles
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
    ensure_app_password
    print_env
    ;;
  env-plain)
    # CI 用：`bash scripts/test-stack.sh env-plain >> "$GITHUB_ENV"`
    ensure_env_file
    ensure_app_password
    print_env_plain
    ;;
  *)
    echo "用法: $0 {up|up-all|down|status|env|env-plain}" >&2
    exit 2
    ;;
esac
