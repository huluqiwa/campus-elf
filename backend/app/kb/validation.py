"""知识库上传文件校验（纯函数，无 I/O、不落盘）。

口径（`docs/design-briefs/M3-KB-01-upload.md` D6，已确认）：
- 扩展名白名单 + 魔数/结构校验；声明的 MIME 只做快速拒绝，落库用服务端判定的 canonical MIME；
- DOCX/PPTX 必须是 ZIP 且含 `[Content_Types].xml` + 对应主文档；
- PPT（97-2003）只做 OLE 头 + `PowerPoint Document` 流名校验（旧格式无更细结构保证）；
- TXT/MD 依次按 UTF-8（含 BOM）→ GB18030 严格解码，含 NUL 视为二进制；
- ZIP 炸弹：条目数/单条目解压/总解压/压缩比上限，拒绝加密条目与路径穿越。
"""

import hashlib
import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_FILES_PER_BATCH = 15
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_BATCH_BYTES = 300 * 1024 * 1024

MAX_ZIP_ENTRIES = 2048
MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 300

CANONICAL_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
    ".ppt": "application/vnd.ms-powerpoint",
}

_OCTET_STREAM = "application/octet-stream"
_ZIP_DECLARED = frozenset({_OCTET_STREAM, "", "application/zip", "application/x-zip-compressed"})
_TEXT_DECLARED = frozenset({_OCTET_STREAM, "", "text/plain", "text/markdown", "text/x-markdown"})

_DECLARED_MIME_ALLOWED: dict[str, frozenset[str]] = {
    ".pdf": frozenset({_OCTET_STREAM, "", "application/pdf"}),
    ".md": _TEXT_DECLARED,
    ".txt": _TEXT_DECLARED,
    ".docx": _ZIP_DECLARED | {CANONICAL_MIME[".docx"]},
    ".pptx": _ZIP_DECLARED | {CANONICAL_MIME[".pptx"]},
    ".ppt": frozenset({_OCTET_STREAM, "", "application/vnd.ms-powerpoint", "application/x-ppt"}),
}

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_PPT_STREAM_NAME = "PowerPoint Document".encode("utf-16-le")


class FileValidationError(Exception):
    """单个文件校验失败；`code` 供整批 422 的逐文件错误使用。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidatedFile:
    """通过校验的文件（服务端判定字段）。"""

    filename: str
    extension: str
    mime: str
    size_bytes: int
    sha256: str
    data: bytes


def sanitize_filename(raw: str) -> str:
    """取纯文件名（去路径/控制字符）并保留原扩展名。

    对象存储 key 只用 UUID 与扩展名，文件名仅入库与审计，因此这里只做展示安全化。

    异常:
        FileValidationError: 文件名为空/过长/只有点号。
    """

    name = PurePosixPath(raw.replace("\\", "/")).name
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    if not name or name in {".", ".."}:
        raise FileValidationError("INVALID_FILENAME", "文件名无效")
    if len(name) > 255:
        raise FileValidationError("INVALID_FILENAME", "文件名不能超过 255 个字符")
    return name


def _normalize_declared_mime(raw: str | None) -> str:
    if not raw:
        return ""
    return raw.split(";", 1)[0].strip().lower()


def _check_declared_mime(extension: str, raw: str | None) -> None:
    declared = _normalize_declared_mime(raw)
    allowed = _DECLARED_MIME_ALLOWED[extension]
    if declared in allowed:
        return
    if extension in {".md", ".txt"} and declared.startswith("text/"):
        return
    raise FileValidationError(
        "MIME_MISMATCH", f"声明的 Content-Type {declared!r} 与文件类型 {extension} 不符"
    )


def _validate_pdf(data: bytes) -> None:
    if not data.startswith(b"%PDF-"):
        raise FileValidationError("MAGIC_MISMATCH", "文件内容不是 PDF")


def _check_zip_safety(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise FileValidationError(
            "ZIP_TOO_MANY_ENTRIES", f"ZIP 条目数超过 {MAX_ZIP_ENTRIES}"
        )
    total_uncompressed = 0
    total_compressed = 0
    for info in infos:
        if info.flag_bits & 0x1:
            raise FileValidationError("ZIP_ENCRYPTED", "不支持加密 ZIP")
        name = info.filename
        parts = name.split("/")
        if name.startswith("/") or "\\" in name or ".." in parts:
            raise FileValidationError("ZIP_UNSAFE_PATH", "ZIP 包含不安全路径条目")
        if info.file_size > MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES:
            raise FileValidationError("ZIP_ENTRY_TOO_LARGE", "ZIP 单条目解压后过大")
        total_uncompressed += info.file_size
        total_compressed += info.compress_size
    if total_uncompressed > MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES:
        raise FileValidationError("ZIP_TOTAL_TOO_LARGE", "ZIP 解压总量过大")
    if (
        total_uncompressed > 0
        and total_uncompressed > max(total_compressed, 1) * MAX_ZIP_COMPRESSION_RATIO
    ):
        raise FileValidationError("ZIP_RATIO_TOO_HIGH", "ZIP 压缩比异常（疑似炸弹）")


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise FileValidationError("ZIP_INVALID", "ZIP 结构无效") from exc
    try:
        _check_zip_safety(archive)
    except BaseException:
        archive.close()
        raise
    return archive


def _validate_ooxml(data: bytes, required_member: str) -> None:
    with _open_zip(data) as archive:
        names = set(archive.namelist())
    if "[Content_Types].xml" not in names or required_member not in names:
        raise FileValidationError(
            "MAGIC_MISMATCH", f"文件内容不是有效的 OOXML（缺少 {required_member}）"
        )


def _validate_docx(data: bytes) -> None:
    _validate_ooxml(data, "word/document.xml")


def _validate_pptx(data: bytes) -> None:
    _validate_ooxml(data, "ppt/presentation.xml")


def _validate_ppt(data: bytes) -> None:
    if not data.startswith(_OLE_MAGIC) or _PPT_STREAM_NAME not in data:
        raise FileValidationError("MAGIC_MISMATCH", "文件内容不是 PowerPoint 97-2003")


def _validate_text(data: bytes) -> None:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            raise FileValidationError("TEXT_BINARY_CONTENT", "文本文件包含 NUL 字节")
        return
    raise FileValidationError("TEXT_ENCODING_INVALID", "文本必须是 UTF-8 或 GB18030 编码")


_VALIDATORS = {
    ".pdf": _validate_pdf,
    ".md": _validate_text,
    ".txt": _validate_text,
    ".docx": _validate_docx,
    ".pptx": _validate_pptx,
    ".ppt": _validate_ppt,
}


def validate_file(*, filename: str, declared_mime: str | None, data: bytes) -> ValidatedFile:
    """校验单个上传文件。

    参数:
        filename: 客户端文件名（可能含路径/控制字符，内部会净化）。
        declared_mime: multipart 声明的 Content-Type（只做快速拒绝）。
        data: 文件字节。

    返回值:
        ValidatedFile（净化文件名、canonical MIME、SHA-256）。

    异常:
        FileValidationError: 任一校验失败（`code` 为稳定错误码）。
    """

    safe_name = sanitize_filename(filename)
    extension = PurePosixPath(safe_name).suffix.lower()
    canonical_mime = CANONICAL_MIME.get(extension)
    if canonical_mime is None:
        raise FileValidationError(
            "EXTENSION_NOT_ALLOWED",
            f"不支持的文件类型 {extension or '(无扩展名)'}；仅支持 PDF/MD/DOCX/PPT(X)/TXT",
        )
    _check_declared_mime(extension, declared_mime)

    size = len(data)
    if size == 0:
        raise FileValidationError("EMPTY_FILE", "空文件不允许上传")
    if size > MAX_FILE_BYTES:
        raise FileValidationError("FILE_TOO_LARGE", f"单文件不能超过 {MAX_FILE_BYTES // (1024 * 1024)}MB")

    _VALIDATORS[extension](data)

    return ValidatedFile(
        filename=safe_name,
        extension=extension,
        mime=canonical_mime,
        size_bytes=size,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )
