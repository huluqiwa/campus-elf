# MinerU 4.0.12 自定义镜像（6GB 显存适配）。
#
# 偏离说明：官方 docker/global/Dockerfile 基于 vllm/vllm-openai，官方标注需要 8GB+ 显存；
# 本机 RTX 4050 6GB 不适合 vLLM Standard。这里改为 python:3.13-slim + mineru[torch]，
# 小模型走 torch 后端，VLM 引擎按 `auto` 解析（已装 mineru-llama-cpp），
# 按已确认策略 Standard→Basic→Flash 自动降级。
# 该偏离记录在 docs/design-briefs/M0-E-compatibility.md 与 docs/tech-debt.md。

FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

ARG MINERU_VERSION=4.0.12

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "mineru[torch]==${MINERU_VERSION}" && \
    pip cache purge

# 非 root 运行：PDF 属不可信输入，容器持有 GPU 设备与持久卷。
# 模型/缓存全部落在 /data（compose 持久卷），避免容器重建后重复下载数 GB 模型。
RUN useradd --create-home --shell /bin/bash mineru && \
    mkdir -p /data/mineru /data/hf /data/modelscope /data/uploads && \
    chown -R mineru:mineru /data

ENV HOME=/home/mineru \
    MINERU_MODEL_SOURCE=auto \
    MINERU_MODEL_SMALL_BACKEND=torch \
    MINERU_MODEL_VLM_ENGINE=auto \
    MINERU_HOME=/data/mineru \
    HF_HOME=/data/hf \
    MODELSCOPE_CACHE=/data/modelscope

USER mineru

# 内层服务的 upload-dir 默认空串会被 click 解析为当前目录并校验可写性，
# 因此 cwd 必须落在 mineru 用户可写目录（/data）。
WORKDIR /data

VOLUME ["/data"]
EXPOSE 8000

# 让 compose 的 command 覆盖后仍能作为完整 argv 执行（entrypoint 负责 exec "$@"）。
ENTRYPOINT ["/bin/bash", "-c", "exec \"$@\"", "--"]
CMD ["mineru-kit", "api-server", "--host", "0.0.0.0", "--port", "8000"]
