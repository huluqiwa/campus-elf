"""TD-294 指标纯函数单测（判定口径钉住）。"""

from campus_elf_evals.metrics import (
    RetrievedChunk,
    aggregate,
    citation_match,
    context_coverage,
    evaluate_case,
    mrr,
    precision_at_k,
    recall_at_k,
)

CITE = "奖学金评定办法#申请条件"


def _chunk(doc: str, path: str | None, text: str = "正文") -> RetrievedChunk:
    return RetrievedChunk(doc_title=doc, section_path=path, text=text)


def test_citation_match_aligns_title_and_any_path_segment() -> None:
    assert citation_match(_chunk("奖学金评定办法", "奖学金评定办法 > 申请条件"), CITE)
    assert citation_match(_chunk("奖学金评定办法", "奖学金评定办法 > 评定标准 > 申请条件"), CITE)
    assert not citation_match(_chunk("奖学金评定办法", "奖学金评定办法 > 公示与申诉"), CITE)
    assert not citation_match(_chunk("图书馆借阅规则", "图书馆借阅规则 > 申请条件"), CITE)


def test_recall_at_k_window_only() -> None:
    hits = [_chunk("奖学金评定办法", "奖学金评定办法 > 申请条件")]
    misses = [_chunk("图书馆借阅规则", "图书馆借阅规则 > 借阅数量与期限")]
    assert recall_at_k(hits + misses * 5, [CITE], k=5) is True
    # 命中在第 6 位 → recall@5 不命中
    assert recall_at_k(misses * 5 + hits, [CITE], k=5) is False


def test_precision_and_mrr() -> None:
    hit = _chunk("奖学金评定办法", "奖学金评定办法 > 申请条件")
    miss = _chunk("图书馆借阅规则", "图书馆借阅规则 > 借阅数量与期限")
    assert precision_at_k([hit, miss, miss], [CITE], k=3) == 1 / 3
    assert mrr([miss, hit], [CITE]) == 0.5
    assert mrr([miss, miss], [CITE]) == 0.0


def test_coverage_requires_all_points_text() -> None:
    hit = _chunk("奖学金评定办法", "奖学金评定办法 > 申请条件", text="平均绩点须达到 3.5 及以上，且无违纪记录")
    points = ["平均绩点须达到 3.5", "无违纪记录", "绩点 4.0 要求"]
    assert context_coverage([hit], points, k=8) == 2 / 3
    assert context_coverage([hit], [], k=8) == 0.0


def test_evaluate_case_and_aggregate() -> None:
    hit = _chunk("奖学金评定办法", "奖学金评定办法 > 申请条件", text="绩点 3.5")
    r1 = evaluate_case("c1", [hit], expected_citations=[CITE], expected_points=["绩点 3.5"])
    assert r1.recall_hit is True and r1.precision == 1 / 3 and r1.reciprocal_rank == 1.0
    r2 = evaluate_case("c2", [], expected_citations=[CITE], expected_points=["绩点 3.5"])
    agg = aggregate([r1, r2])
    assert agg["recall_rate"] == 0.5
    assert agg["cases"] == 2.0
    assert agg["mrr"] == 0.5
