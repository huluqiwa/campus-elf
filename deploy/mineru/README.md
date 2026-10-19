# MinerU 4.0.12 本地镜像（6GB 显存适配版）

> **偏离说明**：官方 `docker/global/Dockerfile` 基于 `vllm/vllm-openai`，官方标注需要 8GB+ 显存；本机 RTX 4050 6GB 不适用。
> 本项目改用 `deploy/mineru/Dockerfile`（`python:3.13-slim` + `mineru[torch]`），小模型走 torch 后端、
> VLM 引擎按 `auto` 解析（镜像内含 `mineru-llama-cpp`），按已确认策略 Standard→Basic→Flash 自动降级。
> 该偏离登记在 `docs/tech-debt.md`（TD-043）与 `docs/design-briefs/M0-E-compatibility.md`。

## 构建

```bash
cd deploy
docker compose --profile parser build mineru
```

镜像 tag 由 `MINERU_IMAGE` 控制（默认 `mineru:4.0.12`）；这是本地构建产物，Docker Hub 上没有同名公开 tag。

## 运行

```bash
cd deploy
docker compose --profile parser up -d mineru
docker compose logs -f mineru
```

健康检查：`curl -f http://127.0.0.1:8100/v1/health`。

> 健康检查只声明服务能力（tier/features），**不等于模型可用**。验收必须包含一次真实 `/v1/file_parse` 调用。

## 镜像内配置（`deploy/mineru/Dockerfile`）

| 项 | 值 | 说明 |
|---|---|---|
| `MINERU_MODEL_SOURCE` | `auto` | 按网络可达性在 `huggingface`/`modelscope` 间解析并持久化；`local` 表示只用已下载模型，缺失即失败 |
| `MINERU_MODEL_SMALL_BACKEND` | `torch` | 小模型推理后端（合法值 `auto`/`onnx`/`torch`） |
| `MINERU_MODEL_VLM_ENGINE` | `auto` | 合法值仅 `auto`/`llama-cpp`/`vllm`/`lmdeploy`/`mlx`；`torch` 是非法值（会造成导入期崩溃） |
| `MINERU_HOME` | `/data/mineru` | 模型目录 `model.base_dir` = `$MINERU_HOME/models` |
| `HF_HOME` / `MODELSCOPE_CACHE` | `/data/hf`、`/data/modelscope` | 下载缓存，与模型目录一起落在持久卷 `/data` |

容器以非 root 用户 `mineru` 运行；GPU 通过 compose 的 `deploy.resources.reservations.devices` 传递。

## 官方 API 服务命令

官方 compose 通过 entrypoint 调用 `mineru-kit`，本项目在 `command` 中给出完整命令：

```text
mineru-kit api-server --host 0.0.0.0 --port 8000 --upload-dir /data/uploads --concurrency 1
```

注意：`api-server` 是 `mineru-kit` 的子命令，不存在独立的 `api-server` 可执行文件。

## 模型与显存

- 模型**不随镜像分发**，首次解析时下载到 `mineru_data` 卷（数 GB，耗时取决于网络）；重建容器不会重复下载。
- 当前目标机 RTX 4050 Laptop 6GB，MinerU 单并发（`--concurrency 1`）。
- Standard 可能 OOM；按已确认策略 Standard→Basic→Flash 自动降级，实际 tier 写入构建元数据。
- 若启动失败，先检查 NVIDIA Container Toolkit / Docker Desktop GPU 支持，再检查 `mineru_data` 卷可写性（非 root 用户）。

## 历史记录（已废弃，仅作偏离证据）

M0-E 最初尝试官方 global 镜像（vLLM 基座）：

```bash
git clone --depth 1 --branch mineru-4.0.12-released https://github.com/opendatalab/MinerU.git vendor/MinerU
docker build -f vendor/MinerU/docker/global/Dockerfile -t mineru:4.0.12 vendor/MinerU
```

该路径已被否决：vLLM 基座要求 8GB+ 显存，且 tag 与本项目 `mineru:4.0.12` 冲突会静默顶替本镜像。**不要执行**，改用上面的 compose 构建路径。
