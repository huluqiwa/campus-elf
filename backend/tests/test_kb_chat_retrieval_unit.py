"""chat 侧检索服务纯函数校验（M4-AI01b 步 1 单元层）。

跨 collection 归并的确定性序（分数降序 + chunk_id 升序 tie-break，简报 TD-P4）
与 top_k 截断；错误码分类。编排层（发现/解析/fan-out/回查）需真实 PG+Qdrant，
见 tests/integration/test_kb_chat_retrieval.py。
"""

import pytest

from app.kb.chat_retrieval import ChatEvidence, ChatRetrievalError, merge_hits


def hit(chunk_id: str, score: float) -> dict[str, object]:
    """构造检索命中（形状对齐 `search_points` 回包）。"""

    return {"chunk_id": chunk_id, "score": score, "payload": {"doc_title": "t"}}


def test_merge_hits_orders_by_score_desc() -> None:
    """多 collection 归并：分数降序。"""

    merged = merge_hits(
        [hit("a", 0.03), hit("b", 0.06), hit("c", 0.05)],
        top_k=10,
    )
    assert [item["chunk_id"] for item in merged] == ["b", "c", "a"]


def test_merge_hits_tie_break_by_chunk_id_asc() -> None:
    """同分 tie-break：chunk_id 升序（跨 collection 并发返回序不影响结果）。"""

    merged = merge_hits(
        [hit("b", 0.05), hit("a", 0.05), hit("c", 0.05)],
        top_k=10,
    )
    assert [item["chunk_id"] for item in merged] == ["a", "b", "c"]


def test_merge_hits_trims_to_top_k() -> None:
    """归并后截 top_k。"""

    merged = merge_hits(
        [hit(f"c{i}", 0.1 - i * 0.01) for i in range(8)],
        top_k=5,
    )
    assert len(merged) == 5


def test_chat_evidence_is_frozen() -> None:
    """证据对象不可变（frozen dataclass）。"""

    evidence = ChatEvidence(
        chunk_id="c1",
        score=0.5,
        text="内容",
        doc_title="校历",
        section_path=None,
        page_start=1,
        page_end=2,
        chunk_type="text",
        version_id="v1",
    )
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        evidence.text = "改动"  # type: ignore[misc]


def test_chat_retrieval_error_carries_code_only() -> None:
    """错误只携带内部码（文本不透传细节——loop 层映射安全话术）。"""

    error = ChatRetrievalError("CHAT_SEARCH_FAILED")
    assert error.code == "CHAT_SEARCH_FAILED"
    assert str(error) == "CHAT_SEARCH_FAILED"
