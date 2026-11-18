"""TD-294 切块参数网格评估 runner（简报 F1-A/F3/F4；检索直评估，零 LLM 生成依赖）。

形态：in-process harness——直调真实组件（chunker 纯函数 / TEI 真实嵌入 / Qdrant 临时
collection / 生产同款 `search_points` RRF 融合），绕过 HTTP/构建编排（上传/审批/任务
调度不是实验变量，简报 §3 取舍注记）。

用法（backend venv 下运行，需 dev 栈 qdrant + tei-embed [+ tei-rerank]）：
    cd backend
    uv run python ../evals/src/campus_elf_evals/grid_runner.py            # 12 格全跑
    uv run python ../evals/src/campus_elf_evals/grid_runner.py --smoke    # 单格 × 3 case
产出：evals/reports/td294-grid-<date>.json + 控制台摘要；评估 collection 用后即删。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import pathlib
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from uuid import uuid4

EVALS_ROOT = pathlib.Path(__file__).resolve().parents[2]  # evals/
BACKEND_ROOT = EVALS_ROOT.parent / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import qdrant_client.models as qmodels  # noqa: E402
from app.kb.chunker import chunk_markdown  # noqa: E402
from app.kb.embedding import TEIEmbedder, embed_input_text, version_sparse_weights  # noqa: E402
from app.kb.indexer import ensure_collection, search_points  # noqa: E402
from qdrant_client import AsyncQdrantClient  # noqa: E402

sys.path.insert(0, str(EVALS_ROOT / "src"))
from campus_elf_evals.metrics import (  # noqa: E402
    CaseResult,
    RetrievedChunk,
    aggregate,
    evaluate_case,
)

GRID: list[tuple[int, float]] = [
    (300, 0.10), (300, 0.15), (300, 0.20),
    (500, 0.10), (500, 0.15), (500, 0.20),
    (800, 0.10), (800, 0.15), (800, 0.20),
]
PARENT_SIZES = [800, 1000, 1200]
PARENT_BASE = (500, 0.15)

QDRANT_URL = "http://127.0.0.1:6333"
TEI_EMBED_URL = "http://127.0.0.1:8081"
TEI_RERANK_URL = "http://127.0.0.1:8082"
CORPUS_DIR = EVALS_ROOT / "fixtures" / "corpus"
CASES_FILE = EVALS_ROOT / "cases" / "chunk_grid.jsonl"
REPORTS_DIR = EVALS_ROOT / "reports"


@dataclass(frozen=True)
class CorpusDoc:
    title: str
    markdown: str


@dataclass(frozen=True)
class Case:
    case_id: str
    question: str
    citations: list[str]
    points: list[str]


def load_corpus() -> list[CorpusDoc]:
    docs = []
    for path in sorted(CORPUS_DIR.glob("*.md")):
        docs.append(CorpusDoc(title=path.stem, markdown=path.read_text(encoding="utf-8")))
    if not docs:
        raise SystemExit("语料为空——先运行 `uv run python -m campus_elf_evals.corpus`")
    return docs


def load_cases(limit: int | None = None) -> list[Case]:
    cases = []
    for line in CASES_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        cases.append(
            Case(
                case_id=row["id"],
                question=row["input"],
                citations=row["expected_citations"],
                points=row["expected_points"],
            )
        )
    return cases[:limit] if limit else cases


async def index_cell(
    client: AsyncQdrantClient,
    *,
    collection: str,
    docs: list[CorpusDoc],
    chunk_size: int,
    overlap_ratio: float,
    parent_size: int,
    embedder: TEIEmbedder,
) -> int:
    """单格：真实 chunker 切块 → TEI 嵌入 → BM25 → Qdrant 写入（子块 only——只索子块）。"""
    await ensure_collection(client, collection=collection, dense_dim=1024)
    dense_texts: list[str] = []
    raw_texts: list[str] = []
    metas: list[tuple[str, str | None]] = []
    for doc in docs:
        chunks = chunk_markdown(
            doc.markdown, chunk_size=chunk_size, overlap_ratio=overlap_ratio, parent_size=parent_size
        )
        for chunk in chunks:
            if chunk.chunk_type != "child":
                continue
            # dense 输入 = 生产 EMBED 语义（embed_input_text 前缀拼接，build_task EMBED 阶段）；
            # sparse 输入 = 子块原文（生产 build_task:712 `version_sparse_weights([chunk.text])`）；
            # payload text = 原文（生产 payload 无 text、文本在 PG——rerank/coverage 消费原文）。
            dense_texts.append(
                embed_input_text(document_title=doc.title, section_path=chunk.section_path, chunk_text=chunk.text)
            )
            raw_texts.append(chunk.text)
            metas.append((doc.title, chunk.section_path))
    dense_vectors = await embedder.embed(dense_texts)
    sparse_vectors = version_sparse_weights(raw_texts)
    point_ids = [str(uuid4()) for _ in raw_texts]
    points = [
        qmodels.PointStruct(
            id=point_id,
            vector={
                "dense": dense,
                "sparse": qmodels.SparseVector(indices=sparse.indices, values=sparse.values),
            },
            payload={"doc_title": title, "section_path": section_path, "text": raw_text},
        )
        for point_id, dense, sparse, (title, section_path), raw_text in zip(
            point_ids, dense_vectors, sparse_vectors, metas, raw_texts, strict=True
        )
    ]
    await client.upsert(collection, points=points, wait=True)
    return len(points)


def _stable_hits(hits: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """生产同款并列稳定化（retrieval_test.py:246 `_stable_hits`：score 降序 + chunk_id 升序）
    ——消除 Qdrant 并列排序不稳定（简报 §9 噪声 ±0.011 的根因）。"""
    return sorted(
        hits,
        key=lambda h: (-float(h["score"]), str(h["chunk_id"])),  # type: ignore[union-attr,index]
    )


def _to_retrieved(hits: Sequence[dict[str, object]]) -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            doc_title=str(hit["payload"].get("doc_title", "")),  # type: ignore[union-attr]
            section_path=hit["payload"].get("section_path"),  # type: ignore[union-attr]
            text=str(hit["payload"].get("text", "")),  # type: ignore[union-attr]
        )
        for hit in hits
    ]


async def evaluate_cell(
    client: AsyncQdrantClient,
    *,
    collection: str,
    cases: list[Case],
    embedder: TEIEmbedder,
) -> list[CaseResult]:
    """单格逐 case：真实检索（RRF 融合，top_k=8）→ 指标。"""
    results: list[CaseResult] = []
    for case in cases:
        dense = (await embedder.embed([case.question]))[0]
        sparse = version_sparse_weights([case.question])[0]
        hits = _stable_hits(
            await search_points(client, collection=collection, dense=dense, sparse=sparse, top_k=8)
        )
        results.append(
            evaluate_case(
                case.case_id,
                _to_retrieved(hits),
                expected_citations=case.citations,
                expected_points=case.points,
            )
        )
    return results


async def evaluate_cell_with_rerank(
    client: AsyncQdrantClient,
    *,
    collection: str,
    cases: list[Case],
    embedder: TEIEmbedder,
    rerank_pool: int = 20,
) -> tuple[list[CaseResult], list[str]]:
    """rerank 对照组：检索 top-20 候选 → TEI rerank → 重排后取 8。"""
    from app.kb.rerank import RerankError, TEIReranker

    reranker = TEIReranker(
        base_url=TEI_RERANK_URL, timeout_seconds=300, batch_size=8
    )
    results: list[CaseResult] = []
    rerank_degraded: list[str] = []
    for case in cases:
        dense = (await embedder.embed([case.question]))[0]
        sparse = version_sparse_weights([case.question])[0]
        hits = _stable_hits(
            await search_points(
                client, collection=collection, dense=dense, sparse=sparse, top_k=rerank_pool
            )
        )
        query = case.question
        try:
            scores = await reranker.rerank(
                query=query, texts=[str(h["payload"].get("text", "")) for h in hits]  # type: ignore[union-attr]
            )
            order = sorted(range(len(hits)), key=lambda i: -scores[i])
            reranked = [hits[i] for i in order][:8]
        except RerankError:
            # 评估韧性：单 case rerank 失败按检索原序 top-8 兜底并计数（报告注明降级数）
            reranked = list(hits)[:8]
            rerank_degraded.append(case.case_id)
        results.append(
            evaluate_case(
                case.case_id,
                _to_retrieved(reranked),
                expected_citations=case.citations,
                expected_points=case.points,
            )
        )
    return results, rerank_degraded


def _bigram_tokenize(text: str) -> list[str]:
    """字级 2-gram 切分（无词典基线——TD-041 分词器对照的可行替代口径）。"""

    chars = [c for c in text if not c.isspace()]
    if len(chars) < 2:
        return chars
    return [a + b for a, b in pairwise(chars)]


def use_tokenizer(name: str) -> None:
    """切换 sparse 分词器（进程内 monkeypatch，评估专用——不改生产文件）。

    jieba=现行生产口径；bigram=无词典基线。pkuseg/HanLP 在 Python 3.13/Windows
    无法构建（无 wheel + C++ 扩展编译失败，TD-041 台账留痕），客观不可对照。
    """

    import app.kb.embedding as embedding_module

    if name == "jieba":
        from app.kb.embedding import tokenize as jieba_tokenize

        embedding_module.tokenize = jieba_tokenize
    elif name == "bigram":
        embedding_module.tokenize = _bigram_tokenize
    else:
        raise SystemExit(f"未知分词器：{name}（可选 jieba/bigram）")


_TOKENIZER_NAME = "jieba"


def _cell_collection(client: AsyncQdrantClient, tag: str, index: int) -> str:
    return f"eval-td294-{tag}-{index}-{int(time.time()) % 100000}"


async def run_grid(
    *,
    smoke: bool,
    with_rerank: bool,
    rerank_case_limit: int = 0,
    report_path: pathlib.Path | None = None,
) -> dict[str, object]:
    docs = load_corpus()
    cases = load_cases(limit=3 if smoke else None)
    cells = [GRID[4]] if smoke else GRID  # smoke = 500/0.15 单格
    report: dict[str, object] = {
        "seed_case_count": len(cases),
        "tokenizer": _TOKENIZER_NAME,
        "cells": [],
        "notes": [],
    }

    def save_report() -> None:
        """逐格增量写盘（复审 Major 1：报告只在收尾写盘，中途 kill 即丢格级 JSON）。"""
        if report_path is None:
            return
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )

    client = AsyncQdrantClient(url=QDRANT_URL)
    # 与生产 kb_embed_batch_size=8 同口径（TEI max-client-batch-size）
    embedder = TEIEmbedder(base_url=TEI_EMBED_URL, timeout_seconds=300, batch_size=8)
    created: list[str] = []
    try:
        # 网格主跑（chunk_size × overlap）
        for index, (chunk_size, overlap) in enumerate(cells):
            collection = _cell_collection(client, "grid", index)
            created.append(collection)
            points = await index_cell(
                client,
                collection=collection,
                docs=docs,
                chunk_size=chunk_size,
                overlap_ratio=overlap,
                parent_size=1000,
                embedder=embedder,
            )
            results = await evaluate_cell(client, collection=collection, cases=cases, embedder=embedder)
            summary = aggregate(results)
            report["cells"].append(  # type: ignore[union-attr]
                {
                    "chunk_size": chunk_size,
                    "overlap_ratio": overlap,
                    "parent_size": 1000,
                    "indexed_children": points,
                    **summary,
                }
            )
            await client.delete_collection(collection)
            save_report()
            print(
                f"[grid] cs={chunk_size} ov={overlap}: recall@5={summary['recall_rate']:.3f} "
                f"p@3={summary['precision']:.3f} mrr={summary['mrr']:.3f} cov={summary['coverage']:.3f}"
            , flush=True)

        # 父块对照（最优重叠 0.15 × PARENT_SIZES；非 smoke 或 smoke 均跑）
        if not smoke:
            for index, parent_size in enumerate(PARENT_SIZES):
                collection = _cell_collection(client, "parent", index)
                created.append(collection)
                chunk_size, overlap = PARENT_BASE
                points = await index_cell(
                    client,
                    collection=collection,
                    docs=docs,
                    chunk_size=chunk_size,
                    overlap_ratio=overlap,
                    parent_size=parent_size,
                    embedder=embedder,
                )
                results = await evaluate_cell(client, collection=collection, cases=cases, embedder=embedder)
                summary = aggregate(results)
                report["cells"].append(  # type: ignore[union-attr]
                    {
                        "chunk_size": chunk_size,
                        "overlap_ratio": overlap,
                        "parent_size": parent_size,
                        "indexed_children": points,
                        **summary,
                    }
                )
                await client.delete_collection(collection)
                save_report()
                print(
                    f"[parent] parent={parent_size}: recall@5={summary['recall_rate']:.3f} "
                    f"p@3={summary['precision']:.3f} mrr={summary['mrr']:.3f} cov={summary['coverage']:.3f}"
                , flush=True)

        # rerank 对照（M2 修复：双组显式对照——默认格 500/0.15 + 网格最优格（p@3 最高），
        # 不再按 (recall, mrr) 选格——recall 饱和 + mrr 并列会误选（首轮复审 M2a 教训））
        grid_cells = [c for c in report["cells"] if "rerank" not in c]  # type: ignore[union-attr]
        best = max(grid_cells, key=lambda c: (c["precision"], c["mrr"]))  # type: ignore[index]
        rerank_targets = [
            (500, 0.15, 1000, "default"),
            (best["chunk_size"], best["overlap_ratio"], best["parent_size"], "best"),  # type: ignore[index]
        ]
        seen: set[tuple[int, float, int]] = set()
        for rerank_index, (cs, ov, ps, tag) in enumerate(rerank_targets):
            key = (cs, ov, ps)
            if key in seen:
                continue
            seen.add(key)
            collection = _cell_collection(client, "rerank", rerank_index)
            created.append(collection)
            points = await index_cell(
                client,
                collection=collection,
                docs=docs,
                chunk_size=cs,
                overlap_ratio=ov,
                parent_size=ps,
                embedder=embedder,
            )
            cases_rr = cases[:rerank_case_limit] if rerank_case_limit else cases
            if with_rerank:
                rerank_results, degraded = await evaluate_cell_with_rerank(
                    client, collection=collection, cases=cases_rr, embedder=embedder
                )
                if degraded:
                    report["notes"].append(f"rerank[{tag}] 降级 case：{degraded}")  # type: ignore[union-attr]
            else:
                rerank_results = await evaluate_cell(client, collection=collection, cases=cases_rr, embedder=embedder)
            rerank_summary = aggregate(rerank_results)
            report["cells"].append(  # type: ignore[union-attr]
                {
                    "chunk_size": cs,
                    "overlap_ratio": ov,
                    "parent_size": ps,
                    "rerank": with_rerank,
                    "rerank_tag": tag,
                    "indexed_children": points,
                    **rerank_summary,
                }
            )
            await client.delete_collection(collection)
            save_report()
            print(
                f"[rerank={with_rerank} {tag}] cs={cs} ov={ov}: "
                f"recall@5={rerank_summary['recall_rate']:.3f} p@3={rerank_summary['precision']:.3f} "
                f"mrr={rerank_summary['mrr']:.3f}",
                flush=True,
            )
    finally:
        # 中途异常也不泄漏临时 collection（复审 m10）
        for name in created:
            with contextlib.suppress(Exception):
                await client.delete_collection(name)
        await client.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="单格 × 3 case 冒烟")
    parser.add_argument("--no-rerank", action="store_true", help="rerank 对照组降级为原始检索（无 TEI rerank 栈时）")
    parser.add_argument(
        "--tokenizer", choices=["jieba", "bigram"], default="jieba", help="sparse 分词器对照（TD-041）"
    )
    parser.add_argument(
        "--rerank-subset",
        type=int,
        default=0,
        help="rerank 对照组降规模为前 N 个 case（0=全量；CPU rerank 全量双组需小时级——简报 §8 注记）",
    )
    args = parser.parse_args()
    global _TOKENIZER_NAME
    _TOKENIZER_NAME = args.tokenizer
    use_tokenizer(args.tokenizer)
    REPORTS_DIR.mkdir(exist_ok=True)
    report_path = REPORTS_DIR / f"td294-grid-{time.strftime('%Y%m%d')}.json"
    report = asyncio.run(
        run_grid(
            smoke=args.smoke,
            with_rerank=not args.no_rerank,
            rerank_case_limit=args.rerank_subset,
            report_path=None if args.smoke else report_path,
        )
    )
    if not args.smoke:
        print(f"report written: {report_path}", flush=True)
    if not args.smoke:
        REPORTS_DIR.mkdir(exist_ok=True)
        out = REPORTS_DIR / f"td294-grid-{time.strftime('%Y%m%d')}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        print(f"report written: {out}", flush=True)


if __name__ == "__main__":
    main()
