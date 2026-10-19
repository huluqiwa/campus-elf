# 从 MinIO 官方源码构建固定 RELEASE 镜像。
# 不使用 Docker Hub 的 minio/minio（当前不可匿名拉取），也不使用 dl.min.io 二进制（410 Gone）。
# 构建上下文：deploy/minio（本文件所在目录）；源码在构建阶段克隆，干净 checkout 可直接 build。
# 示例：
#   docker build -f deploy/minio/Dockerfile \
#     --build-arg MINIO_RELEASE=RELEASE.2025-10-15T17-29-55Z \
#     -t minio:RELEASE.2025-10-15T17-29-55Z deploy/minio

FROM golang:1.24-alpine@sha256:8bee1901f1e530bfb4a7850aa7a479d17ae3a18beb6e09064ed54cfd245b7191 AS build

ARG TARGETARCH=amd64
ARG MINIO_RELEASE=RELEASE.2025-10-15T17-29-55Z

RUN apk add --no-cache ca-certificates git

WORKDIR /src
RUN git clone --depth 1 --branch "${MINIO_RELEASE}" \
    https://github.com/minio/minio.git /src

RUN COMMIT_ID=$(git rev-parse HEAD) && \
    SHORT_COMMIT_ID=$(echo "$COMMIT_ID" | cut -c1-12) && \
    CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} \
    go build -tags kqueue -trimpath \
      -ldflags "-s -w \
        -X github.com/minio/minio/cmd.Version=${MINIO_RELEASE} \
        -X github.com/minio/minio/cmd.ReleaseTag=${MINIO_RELEASE} \
        -X github.com/minio/minio/cmd.CommitID=${COMMIT_ID} \
        -X github.com/minio/minio/cmd.ShortCommitID=${SHORT_COMMIT_ID}" \
      -o /out/minio .

# 运行时复用已固定的 Go 基础镜像，避免额外拉取 alpine 的认证问题。
# 以非 root 用户运行；/data 卷在镜像内预创建并授权。
FROM golang:1.24-alpine@sha256:8bee1901f1e530bfb4a7850aa7a479d17ae3a18beb6e09064ed54cfd245b7191

ARG MINIO_RELEASE=RELEASE.2025-10-15T17-29-55Z

LABEL org.opencontainers.image.title="minio" \
      org.opencontainers.image.version="${MINIO_RELEASE}" \
      org.opencontainers.image.source="https://github.com/minio/minio"

RUN apk add --no-cache ca-certificates curl && \
    addgroup -S minio && \
    adduser -S -G minio -h /home/minio minio && \
    mkdir -p /data /home/minio && \
    chown -R minio:minio /data /home/minio

COPY --from=build /out/minio /usr/bin/minio

ENV HOME=/home/minio

VOLUME ["/data"]
EXPOSE 9000 9001
USER minio

ENTRYPOINT ["/usr/bin/minio"]
CMD ["server", "/data", "--console-address", ":9001"]
