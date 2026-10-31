"""KB 上传文件校验单测（扩展名/魔数/MIME/ZIP 炸弹/文本编码）。"""

import io
import zipfile

import pytest

from app.kb import validation
from app.kb.validation import FileValidationError, validate_file


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _docx_bytes() -> bytes:
    return _zip_bytes(
        {
            "[Content_Types].xml": b"<Types/>",
            "word/document.xml": b"<document/>",
        }
    )


def _pptx_bytes() -> bytes:
    return _zip_bytes(
        {
            "[Content_Types].xml": b"<Types/>",
            "ppt/presentation.xml": b"<presentation/>",
        }
    )


def _ppt_bytes() -> bytes:
    return (
        validation._OLE_MAGIC
        + b"\x00" * 128
        + "PowerPoint Document".encode("utf-16-le")
        + b"\x00" * 32
    )


def _validate(name: str, data: bytes, declared: str | None = None) -> validation.ValidatedFile:
    return validate_file(filename=name, declared_mime=declared, data=data)


def _error_code(name: str, data: bytes, declared: str | None = None) -> str:
    with pytest.raises(FileValidationError) as excinfo:
        _validate(name, data, declared)
    return excinfo.value.code


def test_each_allowed_extension_returns_canonical_mime() -> None:
    """六种白名单扩展名各自返回服务端判定的 canonical MIME 与 SHA-256。"""

    cases = {
        "a.pdf": b"%PDF-1.7\ncontent",
        "a.md": "# 标题".encode(),
        "a.txt": "纯文本".encode(),
        "a.docx": _docx_bytes(),
        "a.pptx": _pptx_bytes(),
        "a.ppt": _ppt_bytes(),
    }
    mimes = {
        "a.pdf": "application/pdf",
        "a.md": "text/markdown",
        "a.txt": "text/plain",
        "a.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "a.pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "a.ppt": "application/vnd.ms-powerpoint",
    }

    for name, data in cases.items():
        result = _validate(name, data)
        assert result.mime == mimes[name]
        assert result.extension == "." + name.rsplit(".", 1)[1]
        assert result.size_bytes == len(data)
        assert len(result.sha256) == 64


def test_declared_mime_is_fast_reject_only() -> None:
    """声明 MIME 为 octet-stream/空/带 charset 的同族值放行；已知冲突类型拒绝。"""

    pdf = b"%PDF-1.7\ncontent"
    assert _validate("a.pdf", pdf, "application/pdf; charset=binary").mime == "application/pdf"
    assert _validate("a.pdf", pdf, "application/octet-stream").mime == "application/pdf"
    assert _validate("a.pdf", pdf, None).mime == "application/pdf"
    assert _validate("a.md", b"# ok", "text/plain").mime == "text/markdown"

    assert _error_code("a.pdf", pdf, "text/plain") == "MIME_MISMATCH"
    assert _error_code("a.docx", _docx_bytes(), "application/msword") == "MIME_MISMATCH"


def test_extension_whitelist_and_missing_extension() -> None:
    """非白名单扩展名与无扩展名都拒绝。"""

    assert _error_code("evil.exe", b"MZ") == "EXTENSION_NOT_ALLOWED"
    assert _error_code("noext", b"data") == "EXTENSION_NOT_ALLOWED"


def test_filename_is_sanitized() -> None:
    """路径分隔符/控制字符被清理；空文件名与超长名拒绝。"""

    result = _validate("../../etc/passwd.pdf", b"%PDF-1.7\ncontent")
    assert result.filename == "passwd.pdf"
    assert _validate("evil\\report.pdf", b"%PDF-1.7\ncontent").filename == "report.pdf"
    assert _validate("a\x00b.pdf", b"%PDF-1.7\ncontent").filename == "ab.pdf"

    assert _error_code("", b"%PDF-1.7") == "INVALID_FILENAME"
    assert _error_code(".", b"%PDF-1.7") == "INVALID_FILENAME"
    assert _error_code("x" * 252 + ".pdf", b"%PDF-1.7") == "INVALID_FILENAME"


def test_empty_and_oversized_files_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 字节拒绝；超过 50MB 上限拒绝（用缩小常量验证边界）。"""

    assert _error_code("a.pdf", b"") == "EMPTY_FILE"

    monkeypatch.setattr(validation, "MAX_FILE_BYTES", 10)
    assert _error_code("a.pdf", b"%PDF-1.7 extra") == "FILE_TOO_LARGE"


def test_magic_mismatch_is_rejected() -> None:
    """扩展名与内容不符（含 docx/pptx 互认失败）一律拒绝。"""

    assert _error_code("a.pdf", b"not a pdf") == "MAGIC_MISMATCH"
    assert _error_code("a.pptx", _docx_bytes()) == "MAGIC_MISMATCH"
    assert _error_code("a.docx", _pptx_bytes()) == "MAGIC_MISMATCH"
    assert _error_code("a.doc", _docx_bytes()) == "EXTENSION_NOT_ALLOWED"
    assert _error_code("a.ppt", b"not ole") == "MAGIC_MISMATCH"


def test_ooxml_requires_content_types_and_main_document() -> None:
    """OOXML 缺 `[Content_Types].xml` 或主文档都不算有效。"""

    without_content_types = _zip_bytes({"word/document.xml": b"<document/>"})
    without_document = _zip_bytes({"[Content_Types].xml": b"<Types/>"})

    assert _error_code("a.docx", without_content_types) == "MAGIC_MISMATCH"
    assert _error_code("a.docx", without_document) == "MAGIC_MISMATCH"
    assert _error_code("a.docx", b"not a zip") == "ZIP_INVALID"


def test_zip_entry_count_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """ZIP 条目数超限即拒绝（防高条目数炸弹）。"""

    monkeypatch.setattr(validation, "MAX_ZIP_ENTRIES", 2)
    data = _zip_bytes(
        {"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<d/>", "extra.xml": b"x"}
    )

    assert _error_code("a.docx", data) == "ZIP_TOO_MANY_ENTRIES"


def test_zip_entry_uncompressed_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """单条目解压后超限拒绝。"""

    monkeypatch.setattr(validation, "MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES", 10)
    data = _zip_bytes({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"x" * 11})

    assert _error_code("a.docx", data) == "ZIP_ENTRY_TOO_LARGE"


def test_zip_total_uncompressed_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """总解压量超限拒绝。"""

    monkeypatch.setattr(validation, "MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES", 10)
    data = _zip_bytes({"[Content_Types].xml": b"x" * 6, "word/document.xml": b"y" * 6})

    assert _error_code("a.docx", data) == "ZIP_TOTAL_TOO_LARGE"


def test_zip_compression_ratio_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """总压缩比超限（高度重复内容）拒绝。"""

    monkeypatch.setattr(validation, "MAX_ZIP_COMPRESSION_RATIO", 1)
    data = _zip_bytes({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"0" * 4096})

    assert _error_code("a.docx", data) == "ZIP_RATIO_TOO_HIGH"


def test_zip_encrypted_and_traversal_entries_are_rejected() -> None:
    """加密条目与 `..`/绝对路径条目拒绝。"""

    encrypted = bytearray(_docx_bytes())
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        index = encrypted.find(signature)
        while index != -1:
            flags = int.from_bytes(encrypted[index + flag_offset : index + flag_offset + 2], "little")
            encrypted[index + flag_offset : index + flag_offset + 2] = (flags | 0x1).to_bytes(
                2, "little"
            )
            index = encrypted.find(signature, index + 4)
    assert _error_code("a.docx", bytes(encrypted)) == "ZIP_ENCRYPTED"

    traversal = _zip_bytes(
        {"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<d/>", "../evil.xml": b"x"}
    )
    assert _error_code("a.docx", traversal) == "ZIP_UNSAFE_PATH"


def test_text_encoding_rules() -> None:
    """UTF-8（含 BOM）与 GB18030 可解码；非法编码与 NUL 内容拒绝。"""

    assert _validate("a.txt", "中文内容".encode()).mime == "text/plain"
    assert _validate("a.txt", b"\xef\xbb\xbfhello").mime == "text/plain"
    assert _validate("a.md", "中文内容".encode("gb18030")).mime == "text/markdown"

    assert _error_code("a.txt", b"\xff\xff\xff") == "TEXT_ENCODING_INVALID"
    assert _error_code("a.txt", b"abc\x00def") == "TEXT_BINARY_CONTENT"
