"""KB 上传服务纯函数单测：metadata 解析、幂等指纹、派生扫描状态。

`parse_metadata` / 指纹 / 扫描状态都不依赖数据库与对象存储，放在单元层，
保证错误码与「瞬时故障不出如今接口」的口径独立于集成链路。
"""

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models.kb import KbDocumentVersion
from app.kb.service import (
    FileMetadata,
    KbError,
    _integrity_constraint_name,
    _PreparedFile,
    _request_fingerprint,
    derive_scan_state,
    parse_metadata,
)


def _prepared(name: str, size: int, sha: str) -> _PreparedFile:
    return _PreparedFile(
        index=0,
        filename=name,
        extension=".pdf",
        mime="application/pdf",
        size_bytes=size,
        sha256=sha,
    )


def _version(**overrides: Any) -> KbDocumentVersion:
    values: dict[str, Any] = {
        "status": "UPLOADED",
        "scan_engine": None,
        "scan_signature": None,
        "scan_error_code": None,
    }
    values.update(overrides)
    return cast(KbDocumentVersion, SimpleNamespace(**values))


def test_parse_metadata_defaults_to_empty_entries() -> None:
    entries = parse_metadata(None, 2)
    assert len(entries) == 2
    assert entries[0].title is None
    assert entries[0].tags == []
    assert entries[0].description is None


def test_parse_metadata_accepts_valid_payload() -> None:
    raw = json.dumps(
        [{"title": "手册", "tags": ["安全", "手册"], "description": "说明"}, {}],
        ensure_ascii=False,
    )
    entries = parse_metadata(raw, 2)
    assert entries[0].title == "手册"
    assert entries[0].tags == ["安全", "手册"]
    assert entries[1].title is None


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("not-json", "JSON"),
        ('{"title": "x"}', "数组"),
        ("[]", "条数"),
        ('[{"extra": 1}]', "不合法"),
        ('[{"title": ""}]', "不合法"),
        (json.dumps([{"title": "x" * 201}]), "不合法"),
        (json.dumps([{"tags": [f"t{i}" for i in range(11)]}]), "不合法"),
        (json.dumps([{"tags": ["x" * 33]}]), "不合法"),
        (json.dumps([{"description": "x" * 1001}]), "不合法"),
    ],
)
def test_parse_metadata_rejects_invalid_payload(raw: str, reason: str) -> None:
    with pytest.raises(KbError) as caught:
        parse_metadata(raw, 1)
    assert caught.value.code == "METADATA_INVALID"
    assert caught.value.http_status == 422
    assert reason in caught.value.message


def _meta(**kwargs: Any) -> FileMetadata:
    return FileMetadata.model_validate(kwargs)


def test_request_fingerprint_is_order_sensitive_and_stable() -> None:
    first = _prepared("a.pdf", 10, "0" * 64)
    second = _prepared("b.pdf", 20, "1" * 64)
    empty = [FileMetadata(), FileMetadata()]
    assert _request_fingerprint([first, second], empty) == _request_fingerprint(
        [first, second], empty
    )
    assert _request_fingerprint([first, second], empty) != _request_fingerprint(
        [second, first], empty
    )
    assert len(_request_fingerprint([first], [FileMetadata()])) == 64


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        (_version(), "pending"),
        (_version(scan_error_code="SCAN_UNAVAILABLE"), "error"),
        (_version(status="PENDING_APPROVAL", scan_engine="clamav"), "passed"),
        (_version(status="PENDING_APPROVAL", scan_engine="dev-skip"), "skipped"),
        (_version(status="PENDING_APPROVAL", scan_engine="dev-degraded"), "skipped"),
        (_version(status="APPROVED", scan_engine="clamav"), "passed"),
        (_version(status="REJECTED", scan_engine="clamav"), "passed"),
        (_version(status="SCAN_FAILED", scan_signature="Eicar-Test-Signature"), "infected"),
        (_version(status="SCAN_FAILED"), "error"),
    ],
)
def test_derive_scan_state(version: KbDocumentVersion, expected: str) -> None:
    assert derive_scan_state(version) == expected


def test_fingerprint_uses_only_filename_size_and_hash() -> None:
    """幂等指纹只看 `filename|size|sha256` 与 metadata，不含顺序号/扩展名等可变字段。"""

    first = _prepared("a.pdf", 10, "0" * 64)
    same = _PreparedFile(
        index=7,
        filename="a.pdf",
        extension=".docx",
        mime="application/octet-stream",
        size_bytes=10,
        sha256="0" * 64,
    )
    assert _request_fingerprint([first], [FileMetadata()]) == _request_fingerprint(
        [same], [FileMetadata()]
    )


def test_fingerprint_changes_when_metadata_changes() -> None:
    """TD-277：同键同文件但元数据不同 → 指纹不同（不再静默吞改动）。"""

    prepared = _prepared("a.pdf", 10, "0" * 64)
    baseline = _request_fingerprint([prepared], [_meta(title="原标题", tags=["规章"])])
    retitled = _request_fingerprint([prepared], [_meta(title="新标题", tags=["规章"])])
    retagged = _request_fingerprint([prepared], [_meta(title="原标题", tags=["规章", "教学"])])
    redesc = _request_fingerprint([prepared], [_meta(title="原标题", tags=["规章"], description="x")])
    assert len({baseline, retitled, retagged, redesc}) == 4
    assert baseline != _request_fingerprint([prepared], [FileMetadata()])


def test_fingerprint_metadata_is_order_insensitive_for_tags_and_whitespace_stable() -> None:
    """标签是集合语义（排序后入哈希）；标题/描述按 strip 后值比较。"""

    prepared = _prepared("a.pdf", 10, "0" * 64)
    left = _request_fingerprint([prepared], [_meta(title="标题", tags=["b", "a"])])
    right = _request_fingerprint([prepared], [_meta(title="标题", tags=["a", "b"])])
    assert left == right
    stripped = _request_fingerprint([prepared], [_meta(title=" 标题 ", description=" 内容 ")])
    plain = _request_fingerprint([prepared], [_meta(title="标题", description="内容")])
    assert stripped == plain


def test_integrity_constraint_name_reads_driver_attribute_chain() -> None:
    """复审 Minor 1：约束名取自驱动原生异常（orig 或 orig.__cause__），不匹配文本。"""

    class _FakeDriverError(Exception):
        constraint_name = "uq_kb_build_jobs_tenant_version_active"

    direct = IntegrityError("stmt", {}, _FakeDriverError("dup"))
    assert _integrity_constraint_name(direct) == "uq_kb_build_jobs_tenant_version_active"

    wrapped = IntegrityError("stmt", {}, Exception("adapter"))
    adapter = wrapped.orig
    assert adapter is not None
    adapter.__cause__ = _FakeDriverError("dup")
    assert _integrity_constraint_name(wrapped) == "uq_kb_build_jobs_tenant_version_active"

    bare = IntegrityError("stmt", {}, Exception("no constraint info"))
    assert _integrity_constraint_name(bare) is None
