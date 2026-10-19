# MinerU 4.0.12 本地镜像

官方 `docker/compose.yaml` 使用本地镜像 `mineru:4`，不是 Docker Hub 上的公开 tag。因此 `deploy/docker-compose.yml` 的 `mineru` 服务默认引用 `mineru:4.0.12`，需要先构建。

## 构建步骤（M0-E 执行）

```bash
# 1. 克隆官方仓库（tag 以官方 release 为准）
git clone --depth 1 --branch mineru-4.0.12-released \
  https://github.com/opendatalab/MinerU.git vendor/MinerU

# 2. 构建 global 镜像
docker build -f vendor/MinerU/docker/global/Dockerfile \
  -t mineru:4.0.12 vendor/MinerU
```

如果 tag 名称不同，用 `git ls-remote --tags` 确认；不要使用 `latest`。

## 运行

```bash
cd deploy
docker compose --profile parser up -d mineru
docker compose logs -f mineru
```

官方 compose 的 API 服务命令为：

```text
api-server --host 0.0.0.0 --port 8000
```

健康检查：`curl -f http://127.0.0.1:8100/v1/health`。

## GPU / 显存

- 当前目标机为 RTX 4050 Laptop 6GB，MinerU 单并发。
- Standard 可能 OOM；按已确认策略 Standard→Basic→Flash 自动降级。
- 若构建/启动失败，先检查 NVIDIA Container Toolkit / Docker Desktop GPU 支持，再检查模型缓存卷。
- 不在 M0-B 强行拉取/构建大镜像；M0-E 单独验证。
