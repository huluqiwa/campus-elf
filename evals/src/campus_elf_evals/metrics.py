"""TD-294 检索直评估指标（纯函数；简报 F3）。

判定口径：
- case 命中 = 检索块的（文档标题, 章节）与 expected_citations 之一对齐
  （section_path 形态 `标题 > 子标题`，`文档#章节` 对齐 = 标题相等 且 章节是
  section_path 的任一路径段）。
- coverage = expected_points 关键短语被 top-k 块文本包含的比例（字符串包含近似——
  简报 TD-294a 登记项，语义判定随 M4 ragas 批次替代）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RetrievedChunk:
    """检索命中块的最小判定面（payload 提取）。"""

    doc_title: str
    section_path: str | None
    text: str


def _sections_of(section_path: str | None) -> set[str]:
    if not section_path:
        return set()
    return {part.strip() for part in section_path.split(" > ")}


def citation_match(chunk: RetrievedChunk, citation: str) -> bool:
    """`文档标题#章节名` 是否对齐检索块（标题相等 且 章节为路径段）。"""
    title, _, section = citation.partition("#")
    return chunk.doc_title == title.strip() and section.strip() in _sections_of(chunk.section_path)


def recall_at_k(retrieved: Sequence[RetrievedChunk], expected_citations: Sequence[str], k: int = 5) -> bool:
    """期望引用（任一）是否进入 top-k——二值命中（主指标 recall@5）。"""
    window = retrieved[:k]
    return any(citation_match(chunk, citation) for chunk in window for citation in expected_citations)


def precision_at_k(retrieved: Sequence[RetrievedChunk], expected_citations: Sequence[str], k: int = 3) -> float:
    """top-k 中对齐期望引用的块占比（citation precision@3；无期望引用时记 0）。"""
    window = retrieved[:k]
    if not expected_citations:
        return 0.0
    hits = sum(
        1 for chunk in window if any(citation_match(chunk, citation) for citation in expected_citations)
    )
    return hits / k


def mrr(retrieved: Sequence[RetrievedChunk], expected_citations: Sequence[str]) -> float:
    """首个对齐块的倒数排名（Mean Reciprocal Rank 的 per-case 值；未命中 = 0）。"""
    for rank, chunk in enumerate(retrieved, start=1):
        if any(citation_match(chunk, citation) for citation in expected_citations):
            return 1.0 / rank
    return 0.0


def context_coverage(
    retrieved: Sequence[RetrievedChunk], expected_points: Sequence[str], k: int = 8
) -> float:
    """top-k 块文本覆盖 expected_points 关键短语的比例（包含近似，TD-294a）。"""
    window = retrieved[:k]
    if not expected_points:
        return 0.0
    haystack = "\n".join(chunk.text for chunk in window)
    covered = sum(1 for point in expected_points if point.strip() and point.strip() in haystack)
    return covered / len(expected_points)


@dataclass
class CaseResult:
    """单 case 单格结果（聚合层用）。"""

    case_id: str
    recall_hit: bool
    precision: float
    reciprocal_rank: float
    coverage: float
    detail: dict[str, object] = field(default_factory=dict)


def evaluate_case(
    case_id: str,
    retrieved: Sequence[RetrievedChunk],
    *,
    expected_citations: Sequence[str],
    expected_points: Sequence[str],
) -> CaseResult:
    """单 case 全指标（口径：recall@5 / precision@3 / MRR / coverage@8）。"""
    return CaseResult(
        case_id=case_id,
        recall_hit=recall_at_k(retrieved, expected_citations, k=5),
        precision=precision_at_k(retrieved, expected_citations, k=3),
        reciprocal_rank=mrr(retrieved, expected_citations),
        coverage=context_coverage(retrieved, expected_points, k=8),
    )


def aggregate(results: Sequence[CaseResult]) -> dict[str, float]:
    """格级聚合：recall 命中率 / precision / MRR / coverage 均值 + 命中 case 数。"""
    n = len(results)
    if n == 0:
        return {"recall_rate": 0.0, "precision": 0.0, "mrr": 0.0, "coverage": 0.0, "cases": 0.0}
    return {
        "recall_rate": sum(r.recall_hit for r in results) / n,
        "precision": sum(r.precision for r in results) / n,
        "mrr": sum(r.reciprocal_rank for r in results) / n,
        "coverage": sum(r.coverage for r in results) / n,
        "cases": float(n),
    }
