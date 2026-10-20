"""对象存储抽象：开发/测试用本地目录，生产用 S3 兼容存储（MinIO/OSS）。

为什么抽象：CI 无法拉取本项目自建的 MinIO 镜像（官方仓库不可匿名拉取），
因此开发/测试使用本地目录保证 CI 可跑；生产显式配置 `OBJECT_STORE_BACKEND=s3`。
两边实现同一接口，key 语义一致（`onboarding/<request_id>/...`、`reviews/<record_id>/...`）。
"""

import shutil
from pathlib import Path
from typing import Protocol

import aioboto3
from botocore.exceptions import ClientError  # noqa: F401  供调用方捕获

from app.config import Settings


class ObjectStoreError(RuntimeError):
    """对象存储操作失败。"""


class ObjectStore(Protocol):
    """对象存储最小接口。"""

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        """写入对象。"""

    async def get(self, key: str) -> bytes:
        """读取对象。"""

    async def copy(self, source_key: str, dest_key: str) -> None:
        """复制对象（审核快照用）。"""

    async def delete(self, key: str) -> None:
        """删除对象（幂等）。"""


def _validate_key(key: str) -> str:
    """拒绝对路径逃逸/绝对路径的 key（对象存储安全边界）。"""

    # Windows 盘符（C:...）、NTFS 数据流（file:stream）、反斜杠与绝对路径一律拒绝
    drive_letter = len(key) > 1 and key[0].isalpha() and key[1] == ":"
    if not key or key.startswith("/") or "\\" in key or ":" in key or drive_letter:
        raise ObjectStoreError(f"非法对象 key: {key!r}")
    if any(part in ("", ".", "..") for part in key.split("/")):
        raise ObjectStoreError(f"非法对象 key: {key!r}")
    return key


class LocalObjectStore:
    """本地目录实现（开发/测试；CI 可用）。"""

    def __init__(self, root: Path) -> None:
        """绑定根目录。"""

        self._root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self._root / _validate_key(key)).resolve()
        if path != self._root and self._root not in path.parents:
            raise ObjectStoreError(f"对象 key 逃逸根目录: {key!r}")
        return path

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        """写入本地文件（自动建目录）。"""

        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def get(self, key: str) -> bytes:
        """读取本地文件。

        异常:
            ObjectStoreError: 文件不存在。
        """

        path = self._path(key)
        if not path.is_file():
            raise ObjectStoreError(f"对象不存在: {key}")
        return path.read_bytes()

    async def copy(self, source_key: str, dest_key: str) -> None:
        """复制本地文件。"""

        source = self._path(source_key)
        if not source.is_file():
            raise ObjectStoreError(f"对象不存在: {source_key}")
        dest = self._path(dest_key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)

    async def delete(self, key: str) -> None:
        """删除本地文件（不存在时忽略）。"""

        self._path(key).unlink(missing_ok=True)


class S3ObjectStore:
    """S3 兼容实现（MinIO/OSS；生产）。"""

    def __init__(self, settings: Settings) -> None:
        """绑定连接参数与桶。"""

        if not settings.s3_endpoint_url or not settings.s3_access_key or not settings.s3_secret_key:
            raise ObjectStoreError("S3 配置不完整（endpoint/access_key/secret_key）")
        self._endpoint = settings.s3_endpoint_url
        self._region = settings.s3_region
        self._access_key = settings.s3_access_key
        self._secret_key = (
            settings.s3_secret_key.get_secret_value()
            if settings.s3_secret_key
            else ""
        )
        self._bucket = settings.s3_bucket

    def _client(self) -> aioboto3.Session:
        session = aioboto3.Session()
        return session.client(
            "s3",
            endpoint_url=self._endpoint,
            region_name=self._region,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
        )

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        """上传对象。"""

        async with self._client() as s3:
            await s3.put_object(
                Bucket=self._bucket,
                Key=_validate_key(key),
                Body=data,
                ContentType=content_type,
            )

    async def get(self, key: str) -> bytes:
        """下载对象。"""

        async with self._client() as s3:
            response = await s3.get_object(Bucket=self._bucket, Key=_validate_key(key))
            payload: bytes = await response["Body"].read()
            return payload

    async def copy(self, source_key: str, dest_key: str) -> None:
        """服务端复制对象。"""

        async with self._client() as s3:
            await s3.copy_object(
                Bucket=self._bucket,
                Key=_validate_key(dest_key),
                CopySource={"Bucket": self._bucket, "Key": _validate_key(source_key)},
            )

    async def delete(self, key: str) -> None:
        """删除对象（幂等）。"""

        async with self._client() as s3:
            await s3.delete_object(Bucket=self._bucket, Key=_validate_key(key))


def build_object_store(settings: Settings) -> ObjectStore:
    """按配置构造对象存储实现。"""

    if settings.object_store_backend == "s3":
        return S3ObjectStore(settings)
    return LocalObjectStore(Path(settings.object_store_dir))
