"""M4-EVAL 跑批 CLI（简报步 6）：种子 → 收集 → 评分 → 报告。

用法（e2e 栈 + 真实 LLM，先 `set -a && . ./.env && set +a`）：
    cd evals
    uv run python -m campus_elf_evals.qa_runner --smoke          # 每类前 2 条 ≈18
    uv run python -m campus_elf_evals.qa_runner --full           # 195 条
    uv run python -m campus_elf_evals.qa_runner --full --collect-only
    uv run python -m campus_elf_evals.qa_runner --score-only --collected <json>

收集与评分分离：重评（改 judge 口径）不重花 answer 钱。
退出码：0 = 五线全达标；1 = 有线未达标或运行失败（如实报告，不放松阈值）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import httpx

from campus_elf_evals.qa_cases import (
    FAITHFULNESS_CATEGORIES,
    QaCase,
    load_cases,
    split_sample_kb,
)
from campus_elf_evals.qa_collect import TurnResult, collect_turn
from campus_elf_evals.qa_report import (
    CaseOutcome,
    aggregate,
    build_run_meta,
    write_reports,
)
from campus_elf_evals.qa_score import OpenAIJudge, case_passed, deterministic_check
from campus_elf_evals.qa_seed import TokenRefresher, fetch_chunk_text, seed_kb

EVALS_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = EVALS_ROOT.parent


async def collect_all(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    cases: list[QaCase],
    *,
    concurrency: int = 2,
    # 客户端读超时须大于后端回合硬超时（TD-354 dev/e2e 放宽到 120s），否则慢回答被客户端先掐断。
    timeout: float = 150.0,
    refresh_token: Callable[[], Awaitable[str]] | None = None,
    refresh_interval_s: float = 700.0,
) -> dict[str, TurnResult]:
    semaphore = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    holder = {"token": token, "issued": time.monotonic()}

    async def fresh_token() -> str:
        if refresh_token is None:
            return holder["token"]
        async with lock:
            if time.monotonic() - holder["issued"] >= refresh_interval_s:
                holder["token"] = await refresh_token()
                holder["issued"] = time.monotonic()
            return holder["token"]

    async def one(case: QaCase) -> tuple[str, TurnResult]:
        async with semaphore:
            tok = await fresh_token()
            turn = await collect_turn(
                client, base_url=base_url, token=tok, message=case.input, timeout=timeout
            )
            return case.id, turn

    pairs = await asyncio.gather(*(one(c) for c in cases))
    return dict(pairs)


def save_collected(turns: dict[str, TurnResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({cid: asdict(t) for cid, t in turns.items()}, ensure_ascii=False),
        encoding="utf-8",
    )


def load_collected(path: Path) -> dict[str, TurnResult]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {cid: TurnResult(**data) for cid, data in raw.items()}


async def score_all(
    cases: list[QaCase],
    turns: dict[str, TurnResult],
    judge: OpenAIJudge,
    faithfulness_fn: Callable[[QaCase, TurnResult], Awaitable[float]] | None = None,
) -> list[CaseOutcome]:
    outcomes: list[CaseOutcome] = []
    for case in cases:
        turn = turns[case.id]
        det = deterministic_check(case, turn)
        verdict = await judge.score_case(case, turn)
        faith: float | None = None
        faith_error: str | None = None
        if (
            faithfulness_fn is not None
            and case.category in FAITHFULNESS_CATEGORIES
            and turn.done is not None
            and turn.citations
        ):
            try:
                faith = await faithfulness_fn(case, turn)
            except Exception as exc:  # 单条评估故障不拖垮整批，如实记录
                faith_error = f"{type(exc).__name__}: {exc}"
        outcomes.append(
            CaseOutcome(
                case=case,
                turn=turn,
                det=det,
                verdict=verdict,
                passed=case_passed(det, verdict),
                faithfulness=faith,
                faithfulness_error=faith_error,
            )
        )
    return outcomes


def _smoke_subset(cases: list[QaCase]) -> list[QaCase]:
    seen: dict[str, int] = {}
    subset: list[QaCase] = []
    for case in cases:
        count = seen.get(case.category, 0)
        if count < 2:
            subset.append(case)
            seen[case.category] = count + 1
    return subset


async def _run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    cases = load_cases(EVALS_ROOT / "cases")
    mode = "full" if args.full else "smoke"
    if not args.full:
        cases = _smoke_subset(cases)
    accounts = json.loads(Path(args.accounts_file).read_text(encoding="utf-8"))
    base_url = args.base_url
    reports_dir = Path(args.reports_dir)

    # trust_env=False：后端在 localhost，不继承环境代理（亦规避 httpx 解析
    # NO_PROXY 中括号 IPv6 [::1] 的 InvalidURL 崩溃）；judge/ragas 的外联客户端另建。
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0), trust_env=False) as client:
        if args.score_only:
            if not args.collected:
                print("--score-only 需要 --collected <json>", file=sys.stderr)
                return 1
            turns = load_collected(Path(args.collected))
            cases = [c for c in cases if c.id in turns]
        else:
            docs = split_sample_kb(
                (EVALS_ROOT / "fixtures" / "sample_kb.md").read_text(encoding="utf-8")
            )
            seeded = await seed_kb(client, base_url, accounts, docs)
            print(f"[seed] {len(seeded)} 份文档已发布")
            chat_refresher = TokenRefresher(client, base_url, accounts["campus_chat"])
            chat_token = await chat_refresher.get()
            turns = await collect_all(
                client, base_url, chat_token, cases,
                concurrency=args.concurrency, refresh_token=chat_refresher.get,
            )
            collected_path = reports_dir / f"collected-{mode}-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
            save_collected(turns, collected_path)
            print(f"[collect] {len(turns)} 回合 → {collected_path}")
            if args.collect_only:
                return 0

        api_key = os.environ.get("LLM_API_KEY")
        judge_base = os.environ.get("LLM_BASE_URL")
        model = os.environ.get("LLM_MODEL")
        if not (api_key and judge_base and model):
            print("评分需要环境变量 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL", file=sys.stderr)
            return 1

        from openai import AsyncOpenAI

        # judge/ragas 外联客户端同样 trust_env=False（直连 provider，已实测可达）：
        # 规避环境 NO_PROXY 括号 IPv6 触发的 httpx InvalidURL；显式持有以便关闭。
        async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(120.0)) as judge_http:
            openai_client = AsyncOpenAI(
                api_key=api_key, base_url=judge_base, http_client=judge_http
            )
            judge = OpenAIJudge(client=openai_client, model=model, seed=args.seed)

            faith_fn: Callable[[QaCase, TurnResult], Awaitable[float]] | None = None
            if not args.no_faithfulness:
                from ragas.llms import llm_factory
                from ragas.metrics.collections import Faithfulness

                ragas_llm = llm_factory(
                    model, provider="openai", client=openai_client,
                    temperature=0, seed=args.seed,
                )
                metric = Faithfulness(llm=ragas_llm)
                # 分块预览需 school.kb.view_chunks：种子里 kb_publisher 持该码（kb_admin 只有
                # upload+view，无 view_chunks——简报 D3-A 原写 kb_admin 系事实错误，已修正）。
                chunk_refresher = TokenRefresher(client, base_url, accounts["kb_publisher"])

                async def _faith(case: QaCase, turn: TurnResult) -> float:
                    contexts: list[str] = []
                    seen: set[str] = set()
                    for cit in turn.citations:
                        chunk_id = str(cit["chunk_id"])
                        if chunk_id in seen:
                            continue
                        seen.add(chunk_id)
                        chunk_token = await chunk_refresher.get()
                        text = await fetch_chunk_text(client, base_url, chunk_token, chunk_id)
                        # 正文不含文档名/章节，补回引用锚点 metadata，否则 NLI 把"根据《X》"framing 判无依据→假 0。
                        header = f"文档：{cit.get('document', '')}\n章节：{cit.get('section', '')}"
                        contexts.append(f"{header}\n{text}")
                    result = await metric.ascore(
                        user_input=case.input, response=turn.answer, retrieved_contexts=contexts
                    )
                    return float(result.value)

                faith_fn = _faith

            outcomes = await score_all(cases, turns, judge, faith_fn)

    lines = aggregate(outcomes)
    meta = build_run_meta(
        model=model, seed=args.seed, base_url=base_url, mode=mode,
        started_at=started_at, duration_s=time.monotonic() - started,
    )
    json_path, md_path = write_reports(outcomes, lines, meta, reports_dir)
    for line in lines:
        mark = "PASS" if line.met else "FAIL"
        print(f"[gate] {line.name}: {line.value:.4f} (阈值 {line.threshold}) {mark} {line.note}")
    print(f"[report] {json_path}\n[report] {md_path}")
    return 0 if all(line.met for line in lines) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="M4 Ragas 评估跑批")
    parser.add_argument("--full", action="store_true", help="全量 195 条（缺省每类前 2 条冒烟）")
    parser.add_argument("--collect-only", action="store_true", help="只收集答案，不评分")
    parser.add_argument("--score-only", action="store_true", help="只评分（读 --collected）")
    parser.add_argument("--collected", help="已收集结果 JSON 路径")
    parser.add_argument("--no-faithfulness", action="store_true", help="跳过 ragas faithfulness")
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument(
        "--accounts-file", default=str(REPO_ROOT / "deploy" / ".e2e-accounts.json")
    )
    parser.add_argument("--reports-dir", default=str(EVALS_ROOT / "reports"))
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())