#!/usr/bin/env python3
"""TEI 模型离线预置（TD-010）。

按已固定 revision 把 BGE-M3 / bge-reranker-v2-m3 下载到 `tei_models` 卷对应目录，
供本机/CI 预热；生产镜像烘焙在 M6 与 MinerU（TD-054）一起收口。

用法：

    # 直接下载到本地目录（宿主机；目录即 HF_HOME 缓存布局）
    cd backend && uv run python ../deploy/tei/prefetch.py --target ../deploy/tei/hf-home

    # 或在 rag profile 运行中，通过临时容器把权重写进 tei_models 卷
    python deploy/tei/prefetch.py --volume campus-elf_tei_models

说明：`hf-mirror.com` 不返回 ETag（TEI 会报 `Header etag is missing`），
因此默认直连 HuggingFace；如需镜像源，用 `--endpoint` 指定支持 ETag 的源。
"""

from __future__ import annotations

import argparse
import subprocess
import sys

# 与 deploy/docker-compose.yml 中固定的 revision 保持一致。
MODELS: dict[str, str] = {
    "BAAI/bge-m3": "5617a9f61b028005a4858fdac845db406aefb181",
    "BAAI/bge-reranker-v2-m3": "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
}
# TEI 镜像是 Rust 二进制、不含 python；预取用固定的 slim 镜像（与 deploy/mineru 同一 digest）。
PY_IMAGE = "python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f"


def _download_local(target: str, endpoint: str | None) -> int:
    """用 huggingface_hub 下载到本地目录。"""

    try:
        from huggingface_hub import snapshot_download
    except ImportError:  # pragma: no cover - 依赖提示
        print("需要 huggingface_hub：请在 backend 的 uv 环境中运行（uv run python ...）", file=sys.stderr)
        return 2

    for repo_id, revision in MODELS.items():
        print(f"[prefetch] {repo_id}@{revision[:12]} -> {target}（HF 缓存布局）")
        # 用 cache_dir 而非 local_dir：TEI 读取的是 HF_HOME 缓存布局
        # （models--org--name/snapshots/<sha>），local_dir 的文件树它认不出来。
        snapshot_download(
            repo_id=repo_id,
            revision=revision,
            cache_dir=target.rstrip("/"),
            endpoint=endpoint,
        )
    print("[prefetch] 完成。把该目录作为 HF_HOME 挂载/指向即可离线加载。")
    return 0


def _download_into_volume(volume: str, endpoint: str | None) -> int:
    """通过临时容器把权重写入 docker 卷（TEI 实际使用的 HF_HOME）。"""

    endpoint_env = ["-e", f"HF_ENDPOINT={endpoint}"] if endpoint else []
    script = (
        "import subprocess, sys;"
        "subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'huggingface_hub']);"
        "from huggingface_hub import snapshot_download;"
        f"[snapshot_download(repo_id=r, revision=v, cache_dir='/data') for r, v in {MODELS!r}.items()];"
        "print('prefetch done')"
    )
    command = [
        "docker",
        "run",
        "--rm",
        *endpoint_env,
        "-v",
        f"{volume}:/data",
        "--entrypoint",
        "python",
        PY_IMAGE,
        "-c",
        script,
    ]
    print(f"[prefetch] 通过临时容器写入卷 {volume}（HF_HOME=/data，已缓存则快速校验）")
    return subprocess.call(command)


def main() -> int:
    """解析参数并执行下载。"""

    parser = argparse.ArgumentParser(description="TEI 模型离线预置（TD-010）")
    parser.add_argument("--target", help="本地目标目录（与 --volume 二选一）")
    parser.add_argument("--volume", help="docker 卷名（TEI 的 HF_HOME 卷，如 campus-elf_tei_models）")
    parser.add_argument("--endpoint", default=None, help="HF 镜像源；需支持 ETag（hf-mirror 不支持）")
    args = parser.parse_args()

    if args.target and args.volume:
        parser.error("--target 与 --volume 只能选一个")
    if args.volume:
        return _download_into_volume(args.volume, args.endpoint)
    return _download_local(args.target or "deploy/tei/models", args.endpoint)


if __name__ == "__main__":
    sys.exit(main())
