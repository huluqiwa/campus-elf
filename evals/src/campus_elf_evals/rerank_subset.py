"""TD-294 rerank 对照（降规模子集版——TEI CPU rerank 单 case 100-250s，全量 32 case
双组需小时级；16 个代表 case 双组 ≈ 40 分钟，对照结论方向性足够）。

每文档取 1-2 条代表 case（覆盖含 800 格劣化场景的章节），rerank 双组：
default=500/0.15（现默认格）+ best=300/0.10（网格最优格）。
产出：evals/reports/td294-rerank-<date>.json
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import time

from app.kb.embedding import TEIEmbedder
from grid_runner import (
    QDRANT_URL,
    TEI_EMBED_URL,
    _cell_collection,
    evaluate_cell,
    evaluate_cell_with_rerank,
    index_cell,
    load_cases,
    load_corpus,
)
from qdrant_client import AsyncQdrantClient

from campus_elf_evals.metrics import aggregate

REPORTS_DIR = pathlib.Path(__file__).resolve().parents[2] / "reports"
SUBSET_IDS = {
    # 每文档 1-2 条：覆盖全部分档主题（含长表/代码/终态场景）
    "chunk-grid-001", "chunk-grid-003", "chunk-grid-004", "chunk-grid-007",
    "chunk-grid-010", "chunk-grid-011", "chunk-grid-013", "chunk-grid-016",
    "chunk-grid-018", "chunk-grid-019", "chunk-grid-022", "chunk-grid-024",
    "chunk-grid-026", "chunk-grid-028", "chunk-grid-030", "chunk-grid-032",
}
CELLS = [(500, 0.15, 1000, "default"), (300, 0.10, 1000, "best")]


async def run_async() -> dict[str, object]:
    docs = load_corpus()
    all_cases = load_cases()
    cases = [c for c in all_cases if c.case_id in SUBSET_IDS]
    print(f"rerank subset: {len(cases)} cases (of {len(all_cases)})", flush=True)
    client = AsyncQdrantClient(url=QDRANT_URL)
    embedder = TEIEmbedder(base_url=TEI_EMBED_URL, timeout_seconds=120, batch_size=8)
    created: list[str] = []
    cells: list[dict[str, object]] = []
    try:
        for index, (cs, ov, ps, tag) in enumerate(CELLS):
            collection = _cell_collection(client, "rr", index)
            created.append(collection)
            await index_cell(
                client,
                collection=collection,
                docs=docs,
                chunk_size=cs,
                overlap_ratio=ov,
                parent_size=ps,
                embedder=embedder,
            )
            base = aggregate(await evaluate_cell(client, collection=collection, cases=cases, embedder=embedder))
            print(f"[base {tag}] cs={cs} ov={ov}: p@3={base['precision']:.3f} recall={base['recall_rate']:.3f}", flush=True)
            rerank_results, degraded = await evaluate_cell_with_rerank(
                client, collection=collection, cases=cases, embedder=embedder
            )
            rr = aggregate(rerank_results)
            print(f"[rerank {tag}] p@3={rr['precision']:.3f} recall={rr['recall_rate']:.3f} degraded={len(degraded)}", flush=True)
            cells.append(
                {
                    "chunk_size": cs,
                    "overlap_ratio": ov,
                    "parent_size": ps,
                    "tag": tag,
                    "cases": len(cases),
                    "rerank_degraded": len(degraded),
                    "without_rerank": base,
                    "with_rerank": rr,
                }
            )
            await client.delete_collection(collection)
    finally:
        for name in created:
            with contextlib.suppress(Exception):
                await client.delete_collection(name)
        await client.close()
    return {"subset_ids": sorted(SUBSET_IDS), "cells": cells}


def main() -> None:
    report = asyncio.run(run_async())
    REPORTS_DIR.mkdir(exist_ok=True)
    out = REPORTS_DIR / f"td294-rerank-{time.strftime('%Y%m%d')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print(f"report written: {out}", flush=True)


if __name__ == "__main__":
    main()
