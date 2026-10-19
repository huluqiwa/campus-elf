# MinIO 固定版本镜像

当前 `minio/minio` Docker Hub 仓库不可匿名拉取，`dl.min.io` 的二进制归档返回 410。因此本项目从 MinIO 官方源码构建固定 RELEASE 镜像，避免使用不可复现的 `latest` 或本地缓存。

## 构建

```bash
# 1. 克隆官方源码（tag 已确认存在）
git clone --depth 1 --branch RELEASE.2025-10-15T17-29-55Z \
  https://github.com/minio/minio.git vendor/minio

# 2. 从源码构建
docker build -f deploy/minio/Dockerfile \
  --build-arg MINIO_RELEASE=RELEASE.2025-10-15T17-29-55Z \
  -t minio:RELEASE.2025-10-15T17-29-55Z vendor/minio
```

## 运行

Compose 默认使用：

```text
image: ${MINIO_IMAGE:-minio:RELEASE.2025-10-15T17-29-55Z}
```

`docker compose up -d minio` 即可。

## 说明

- 该 RELEASE 是官方标记的安全修复版本（2025-10-15）。
- 不使用 `minio/minio:latest`；本镜像也不依赖 Docker Hub 的 MinIO 仓库。
- 若构建环境无法访问 Go module proxy，`go mod download` 会失败；需配置代理或预先缓存模块。
- 本地已有的 `campus-elf/minio:DEVELOPMENT.2025-05-24T17-08-30Z` 是旧 Bitnami 缓存，含已知旧版本，不作为生产/验收镜像。
