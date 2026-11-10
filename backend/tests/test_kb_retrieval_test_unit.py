"""KB-07A 检索测试纯函数单元测试（简报 `docs/design-briefs/M3-KB-07.md` §4 第 3 步）。

覆盖：码点 preview 截断、单路结果 → (score, rank) 映射、融合序行构造
（逐路分数归位 / PG 缺行跳过且名次连续）、重排稳定降序（同分保融合序）与
条数护栏。
"""

from uuid import uuid4

import pytest

from app.db.models.kb import KbChunk
from app.kb.rerank import RerankError
from app.kb.retrieval_test import apply_rerank, build_rows, route_rank_map, text_preview


def _chunk(chunk_index: int = 0) -> KbChunk:
    return KbChunk(
        id=uuid4(),
        document_version_id=uuid4(),
        chunk_type="child",
        chunk_index=chunk_index,
        text=f"第{chunk_index}块正文内容",
        text_markdown=f"第{chunk_index}块正文内容",
        section_path=f"第一章 > 1.{chunk_index}",
        page_start=1,
        page_end=1,
        token_count=10,
        char_count=10,
        embedding_status="ready",
    )


def _hit(chunk_id: object, score: float) -> dict[str, object]:
    return {"chunk_id": str(chunk_id), "score": score}


def test_text_preview_truncates_by_code_points() -> None:
    assert text_preview("短文本") == "短文本"
    long_ascii = "a" * 250
    assert len(text_preview(long_ascii)) == 200
    # 非 BMP 字符按码点度量（Python len 即码点；前端预检用 codePointLength 对齐）
    emoji = "👍" * 250
    assert len(text_preview(emoji)) == 200


def test_route_rank_map_positions_are_one_based() -> None:
    hits = [_hit("c2", 0.9), _hit("c1", 0.4)]
    mapped = route_rank_map(hits)
    assert mapped["c2"].rank == 1 and mapped["c2"].score == 0.9
    assert mapped["c1"].rank == 2 and mapped["c1"].score == 0.4
    assert route_rank_map([]) == {}


def test_build_rows_attaches_route_hits_and_contiguous_ranks() -> None:
    first = _chunk(0)
    second = _chunk(1)
    fused = [_hit(second.id, 0.0227), _hit(first.id, 0.0164)]
    dense_hits = [_hit(first.id, 0.98), _hit(second.id, 0.51)]
    sparse_hits = [_hit(second.id, 3.2)]

    rows = build_rows(
        fused=fused,
        chunk_rows={first.id: first, second.id: second},
        dense_hits=dense_hits,
        sparse_hits=sparse_hits,
    )

    assert [row.final_rank for row in rows] == [1, 2]
    assert rows[0].chunk_id == second.id
    assert rows[0].rrf_score == 0.0227
    assert rows[0].dense is not None and rows[0].dense.rank == 2
    assert rows[0].sparse is not None and rows[0].sparse.rank == 1
    assert rows[0].rerank_score is None
    assert rows[1].chunk_id == first.id
    assert rows[1].dense is not None and rows[1].dense.rank == 1
    assert rows[1].sparse is None


def test_build_rows_skips_missing_pg_rows_and_keeps_ranks_contiguous() -> None:
    first = _chunk(0)
    fused = [_hit(uuid4(), 0.02), _hit(first.id, 0.0164)]

    rows = build_rows(
        fused=fused,
        chunk_rows={first.id: first},
        dense_hits=[],
        sparse_hits=[],
    )

    assert [row.chunk_id for row in rows] == [first.id]
    assert [row.final_rank for row in rows] == [1]


def test_apply_rerank_sorts_descending_stable_and_reassigns_ranks() -> None:
    first = _chunk(0)
    second = _chunk(1)
    third = _chunk(2)
    rows = build_rows(
        fused=[_hit(first.id, 0.03), _hit(second.id, 0.02), _hit(third.id, 0.01)],
        chunk_rows={first.id: first, second.id: second, third.id: third},
        dense_hits=[],
        sparse_hits=[],
    )

    ranked = apply_rerank(rows, [0.1, 0.9, 0.1])

    # 降序：second(0.9) 第一；first/third 同分 0.1 → 稳定保融合序
    assert [row.chunk_id for row in ranked] == [second.id, first.id, third.id]
    assert [row.final_rank for row in ranked] == [1, 2, 3]
    assert [row.rerank_score for row in ranked] == [0.9, 0.1, 0.1]


def test_apply_rerank_count_mismatch_is_rejected() -> None:
    first = _chunk(0)
    rows = build_rows(
        fused=[_hit(first.id, 0.03)],
        chunk_rows={first.id: first},
        dense_hits=[],
        sparse_hits=[],
    )
    with pytest.raises(RerankError):
        apply_rerank(rows, [0.1, 0.2])
