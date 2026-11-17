"""TD-041 分词器对照（单格顺序双跑，无并发；jieba=生产口径 vs bigram=无词典基线）。

pkuseg/HanLP 在 Python 3.13/Windows 无法构建（无 wheel + C++ 扩展编译失败，
TD-041 台账留痕）——可行替代基线为字级 2-gram。

用法：cd backend && uv run python ../evals/src/campus_elf_evals/tokenizer_compare.py
产出：evals/reports/td294-tokenizer-<date>.json
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import time

from app.kb.embedding import TEIEmbedder
from grid_runner import (  # type: ignore[import-not-use]
    QDRANT_URL,
    TEI_EMBED_URL,
    _cell_collection,
    evaluate_cell,
    index_cell,
    load_cases,
    load_corpus,
    use_tokenizer,
)
from qdrant_client import AsyncQdrantClient

from campus_elf_evals.metrics import aggregate

REPORTS_DIR = EVALS_ROOT = pathlib.Path(__file__).resolve().parents[2] / "reports"
BEST_CELL = (500, 0.15)  # 首轮网格最优格（报告确认后如不同可改参数重跑）


async def run_once(client: AsyncQdrantClient, embedder: TEIEmbedder, tokenizer: str) -> dict[str, object]:
    use_tokenizer(tokenizer)
    docs = load_corpus()
    cases = load_cases()
    chunk_size, overlap = BEST_CELL
    collection = _cell_collection(client, "tok", 0 if tokenizer == "jieba" else 1)
    await index_cell(
        client,
        collection=collection,
        docs=docs,
        chunk_size=chunk_size,
        overlap_ratio=overlap,
        parent_size=1000,
        embedder=embedder,
    )
    results = await evaluate_cell(client, collection=collection, cases=cases, embedder=embedder)
    await client.delete_collection(collection)
    return {"tokenizer": tokenizer, **aggregate(results)}


async def main_async() -> dict[str, object]:
    docs_count = len(load_corpus())
    client = AsyncQdrantClient(url=QDRANT_URL)
    embedder = TEIEmbedder(base_url=TEI_EMBED_URL, timeout_seconds=120, batch_size=8)
    try:
        jieba_summary = await run_once(client, embedder, "jieba")
        print(f"[tokenizer] jieba:  {jieba_summary}", flush=True)
        bigram_summary = await run_once(client, embedder, "bigram")
        print(f"[tokenizer] bigram: {bigram_summary}", flush=True)
    finally:
        await client.close()
    return {"best_cell": BEST_CELL, "corpus_docs": docs_count, "runs": [jieba_summary, bigram_summary]}


def main() -> None:
    report = asyncio.run(main_async())
    REPORTS_DIR.mkdir(exist_ok=True)
    out = REPORTS_DIR / f"td294-tokenizer-{time.strftime('%Y%m%d')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print(f"report written: {out}", flush=True)


if __name__ == "__main__":
    main()
