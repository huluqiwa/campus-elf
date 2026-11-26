"""M4-EVAL 跑批 CLI（简报步 6）：种子 → 收集 → 评分 → 报告。

用法（e2e 栈 + 真实 LLM，先 `set -a && . ./.env && set +a`）：
    cd evals
    uv run python -m campus_elf_evals.qa_runner                  # 默认每类前 2 条 ≈18
    uv run python -m campus_elf_evals.qa_runner --full           # 195 条
    uv run python -m campus_elf_evals.qa_runner --full --collect-only --concurrency 1
    uv run python -m campus_elf_evals.qa_runner --full --score-only --collected <json>

收集与评分分离：重评（改 judge 口径）不重花 answer 钱。
退出码：0 = 五线全达标；1 = 有线未达标或运行失败（如实报告，不放松阈值）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
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
from campus_elf_evals.qa_score import (
    JudgeVerdict,
    OpenAIJudge,
    case_passed,
    deterministic_check,
    finalize_deterministic_check,
)
from campus_elf_evals.qa_seed import TokenRefresher, fetch_chunk_text, seed_kb
from campus_elf_evals.temp_model import RotatingAsyncOpenAI, load_temp_model_settings

EVALS_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = EVALS_ROOT.parent


def sanitize_exception(exc: BaseException) -> str:
    """对异常做脱敏：只保留异常类型名，剥离原始消息（可能含 URL、主机名、堆栈片段）。

    结构化报告字段（transport_error / error.message / judge.reason / faithfulness_error）
    不得回显底层异常原文，与 gate note 及 AGENTS §3/§5 脱敏纪律保持同一口径。
    """
    return f"{type(exc).__name__}"


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
    current_token = token
    issued_at = time.monotonic()

    async def fresh_token() -> str:
        nonlocal current_token, issued_at
        if refresh_token is None:
            return current_token
        async with lock:
            if time.monotonic() - issued_at >= refresh_interval_s:
                current_token = await refresh_token()
                issued_at = time.monotonic()
            return current_token

    async def one(case: QaCase) -> tuple[str, TurnResult]:
        async with semaphore:
            try:
                tok = await fresh_token()
            except Exception as exc:  # 鉴权/刷新失败不取消整批，单条标记为传输错误
                return case.id, TurnResult(
                    answer="",
                    citations=[],
                    done=None,
                    error={"code": "token_refresh_failed", "message": sanitize_exception(exc)},
                    meta=None,
                    transport_error=sanitize_exception(exc),
                    first_delta_ms=None,
                    latency_ms=0,
                )
            try:
                turn = await collect_turn(
                    client, base_url=base_url, token=tok, message=case.input, timeout=timeout
                )
            except Exception as exc:  # 单条回合传输错误不取消整批 gather
                return case.id, TurnResult(
                    answer="",
                    citations=[],
                    done=None,
                    error={"code": "turn_transport_failed", "message": sanitize_exception(exc)},
                    meta=None,
                    transport_error=sanitize_exception(exc),
                    first_delta_ms=None,
                    latency_ms=0,
                )
            return case.id, turn

    pairs = await asyncio.gather(*(one(c) for c in cases))
    return dict(pairs)


def save_collected(turns: dict[str, TurnResult], path: Path) -> str:
    """保存收集并返回写入字节的 SHA-256；调用方可绑定本次评分快照。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        {cid: asdict(t) for cid, t in turns.items()}, ensure_ascii=False
    ).encode()
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """构造 JSON 对象时拒绝重复键，避免后值静默覆盖前值。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("collected JSON 含重复对象键")
        result[key] = value
    return result


def _bounded_id_summary(ids: set[str], *, label: str) -> str:
    ordered = sorted(ids)
    samples = ", ".join(repr(value[:80]) for value in ordered[:5])
    suffix = f", ... (+{len(ordered) - 5})" if len(ordered) > 5 else ""
    return f"{label}={samples}{suffix}" if samples else f"{label}=[]"


def _bounded_exception_summary(exc: Exception, *, limit: int = 500) -> str:
    """把校验异常压成单行有界摘要，避免任意大段输入污染 CLI 日志。"""
    text = " ".join(str(exc).splitlines()).strip() or type(exc).__name__
    if len(text) <= limit:
        return text
    suffix = " ... (truncated)"
    if len(suffix) >= limit:
        return suffix[:limit]
    return text[: limit - len(suffix)] + suffix


def _validate_collected_turn_payload(cid: str, data: dict) -> None:
    def fail(field: str, expected: str) -> None:
        raise ValueError(
            f"collected 条目 {cid[:80]!r} 字段 {field} 类型无效（要求 {expected}）"
        )

    if "answer" in data and type(data["answer"]) is not str:
        fail("answer", "string")
    citations = data.get("citations", [])
    if type(citations) is not list or any(type(item) is not dict for item in citations):
        fail("citations", "list[object]")
    for field in ("done", "error", "meta"):
        value = data.get(field)
        if value is not None and type(value) is not dict:
            fail(field, "object|null")
    value = data.get("transport_error")
    if value is not None and type(value) is not str:
        fail("transport_error", "string|null")
    first_delta_ms = data.get("first_delta_ms")
    if first_delta_ms is not None and (
        type(first_delta_ms) is not int or first_delta_ms < 0
    ):
        fail("first_delta_ms", "non-negative integer|null")
    latency_ms = data.get("latency_ms")
    if type(latency_ms) is not int or latency_ms < 0:
        fail("latency_ms", "non-negative integer")


def _parse_collected_bytes(encoded: bytes) -> dict[str, TurnResult]:
    try:
        text = encoded.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("collected 文件不是 UTF-8") from None
    try:
        raw = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"collected JSON 解析失败（line={exc.lineno}, column={exc.colno}）"
        ) from None
    if not isinstance(raw, dict) or not raw:
        raise ValueError("collected JSON 根必须是非空对象")

    turns: dict[str, TurnResult] = {}
    for cid, data in raw.items():
        if not isinstance(cid, str) or not cid.strip():
            raise ValueError("collected JSON 含空或非字符串 ID")
        if not isinstance(data, dict):
            raise ValueError(f"collected 条目 {cid[:80]!r} 必须是对象")
        _validate_collected_turn_payload(cid, data)
        try:
            turns[cid] = TurnResult(**data)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"collected 条目 {cid[:80]!r} 格式无效（{type(exc).__name__}）"
            ) from None
    return turns


def load_collected_snapshot(path: Path) -> tuple[dict[str, TurnResult], str]:
    """一次读取 collected，返回内存结果和该次读取原文的 SHA-256。"""
    try:
        encoded = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"collected 文件不可读（{type(exc).__name__}）") from None
    return _parse_collected_bytes(encoded), hashlib.sha256(encoded).hexdigest()


def load_collected(path: Path) -> dict[str, TurnResult]:
    """严格加载 collected；拒绝非法 JSON 根、任意层级重复键和条目结构错误。"""
    return load_collected_snapshot(path)[0]

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
        try:
            verdict = await judge.score_case(case, turn)
        except Exception as exc:  # 纵深防御：单条 judge 故障保守判 0，不拖垮整批
            verdict = JudgeVerdict(
                score=0.0,
                redline_hit=False,
                reason=f"judge_exception:{sanitize_exception(exc)}",
                stable=False,
                attempts=0,
                successful_attempts=0,
            )
        det = finalize_deterministic_check(case, turn, det, verdict)
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
                faith_error = sanitize_exception(exc)
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


def _validate_source_case_set(cases: list[QaCase]) -> None:
    """源 cases 必须精确覆盖固定 manifest 的 ID 与逐条类别。"""
    try:
        manifest = json.loads(
            (EVALS_ROOT / "fixtures" / "m4_eval_v1_manifest.json").read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        manifest_cases = manifest["cases"]
        if not isinstance(manifest_cases, dict) or not manifest_cases:
            raise ValueError("manifest cases 必须是非空对象")
        expected = set(manifest_cases)
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        raise ValueError(f"评估基准 manifest 无效（{type(exc).__name__}）") from None

    invalid_id_rows = [
        index
        for index, case in enumerate(cases, start=1)
        if type(case.id) is not str or not case.id.strip()
    ]
    if invalid_id_rows:
        samples = ", ".join(str(index) for index in invalid_id_rows[:5])
        suffix = (
            f"；... (+{len(invalid_id_rows) - 5})"
            if len(invalid_id_rows) > 5
            else ""
        )
        raise ValueError(
            f"源用例 ID 必须是非空字符串：invalid={len(invalid_id_rows)}；"
            f"row_indices={samples}{suffix}"
        )

    case_ids = [case.id for case in cases]
    actual = set(case_ids)
    duplicate_ids = {
        case_id for case_id in actual if case_ids.count(case_id) > 1
    }
    missing = expected - actual
    extra = actual - expected
    if duplicate_ids or missing or extra:
        details = [
            f"manifest_expected={len(expected)}",
            f"loaded_rows={len(cases)}",
            f"loaded_ids={len(actual)}",
            f"duplicate={len(duplicate_ids)}",
            f"missing={len(missing)}",
            f"extra={len(extra)}",
        ]
        if duplicate_ids:
            details.append(_bounded_id_summary(duplicate_ids, label="duplicate_ids"))
        if missing:
            details.append(_bounded_id_summary(missing, label="missing_ids"))
        if extra:
            details.append(_bounded_id_summary(extra, label="extra_ids"))
        raise ValueError("源用例 ID 集合与基准 manifest 不一致：" + "；".join(details))

    category_mismatches = {
        case.id: (case.id.rsplit("-", 1)[0], case.category)
        for case in cases
        if case.category != case.id.rsplit("-", 1)[0]
    }
    if category_mismatches:
        examples = "；".join(
            f"{cid!r}: expected={expected_category!r}, loaded={loaded_category!r}"
            for cid, (expected_category, loaded_category) in sorted(
                category_mismatches.items()
            )[:5]
        )
        suffix = (
            f"；... (+{len(category_mismatches) - 5})"
            if len(category_mismatches) > 5
            else ""
        )
        raise ValueError(
            f"源用例 ID 的逐条类别与基准 manifest 不一致：{len(category_mismatches)} 条；"
            f"{examples}{suffix}"
        )

    manifest_categories: dict[str, int] = {}
    for cid in expected:
        category = cid.rsplit("-", 1)[0]
        manifest_categories[category] = manifest_categories.get(category, 0) + 1
    loaded_categories: dict[str, int] = {}
    for case in cases:
        loaded_categories[case.category] = loaded_categories.get(case.category, 0) + 1
    if loaded_categories != manifest_categories:
        raise ValueError(
            "源用例类别分布与基准 manifest 不一致："
            f"manifest={dict(sorted(manifest_categories.items()))}；"
            f"loaded={dict(sorted(loaded_categories.items()))}"
        )


def _validate_case_id_set(cases: list[QaCase], turns: dict[str, TurnResult]) -> None:
    """正式 full/smoke 必须与目标 case 集合双向精确相等。"""
    expected = {case.id for case in cases}
    collected = set(turns)
    missing = expected - collected
    extra = collected - expected
    if missing or extra:
        details = [
            f"expected={len(expected)}",
            f"collected={len(collected)}",
            f"missing={len(missing)}",
            f"extra={len(extra)}",
        ]
        if missing:
            details.append(_bounded_id_summary(missing, label="missing_ids"))
        if extra:
            details.append(_bounded_id_summary(extra, label="extra_ids"))
        raise ValueError("ID 集合不匹配：" + "；".join(details))


async def _run(args: argparse.Namespace) -> int:
    if type(args.judge_repeats) is not int or args.judge_repeats < 5:
        print("正式评估 --judge-repeats 至少 5 轮", file=sys.stderr)
        return 1
    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        cases = load_cases(EVALS_ROOT / "cases")
        _validate_source_case_set(cases)
    except (OSError, KeyError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, ValueError):
            detail = "：" + _bounded_exception_summary(exc)
        else:
            detail = f"（{type(exc).__name__}）"
        print(f"源用例输入无效{detail}", file=sys.stderr)
        return 1
    mode = "full" if args.full else "smoke"
    if not args.full:
        cases = _smoke_subset(cases)
    base_url = args.base_url
    reports_dir = Path(args.reports_dir)

    turns: dict[str, TurnResult] | None = None
    collected_path: Path | None = None
    source_collection_sha256: str | None = None
    if args.score_only:
        if not args.collected:
            print("--score-only 需要 --collected <json>", file=sys.stderr)
            return 1
        collected_path = Path(args.collected)
        try:
            turns, source_collection_sha256 = load_collected_snapshot(collected_path)
            _validate_case_id_set(cases, turns)
        except ValueError as exc:
            print(f"收集输入无效：{exc}", file=sys.stderr)
            return 1

    accounts: dict | None = None
    if not args.score_only or not args.no_faithfulness:
        try:
            raw_accounts = json.loads(Path(args.accounts_file).read_text(encoding="utf-8"))
            if not isinstance(raw_accounts, dict):
                raise ValueError("根必须是对象")
            accounts = raw_accounts
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"账户配置无效（{type(exc).__name__}）", file=sys.stderr)
            return 1

    # trust_env=False：后端在 localhost，不继承环境代理（亦规避 httpx 解析
    # NO_PROXY 中括号 IPv6 [::1] 的 InvalidURL 崩溃）；judge/ragas 的外联客户端另建。
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0), trust_env=False) as client:
        if not args.score_only:
            if accounts is None:
                raise AssertionError("收集路径缺少账户配置")
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
            try:
                _validate_case_id_set(cases, turns)
            except ValueError as exc:
                print(f"收集输入无效：{exc}", file=sys.stderr)
                return 1
            collected_path = reports_dir / f"collected-{mode}-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
            source_collection_sha256 = save_collected(turns, collected_path)
            print(f"[collect] {len(turns)} 回合 → {collected_path}")
            if args.collect_only:
                return 0

        if turns is None or collected_path is None:
            raise AssertionError("评分路径缺少已校验收集")

        try:
            temp_model = load_temp_model_settings(REPO_ROOT / ".env")
        except ValueError as exc:
            print(f"评分配置无效：{exc}", file=sys.stderr)
            return 1
        model = temp_model.model

        # judge/ragas 外联客户端同样 trust_env=False（直连 provider，已实测可达）：
        # 规避环境 NO_PROXY 括号 IPv6 触发的 httpx InvalidURL；显式持有以便关闭。
        async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(120.0)) as judge_http:
            openai_client = RotatingAsyncOpenAI(
                api_keys=temp_model.api_keys,
                base_url=temp_model.base_url,
                http_client=judge_http,
                max_retries=0,
            )
            judge = OpenAIJudge(
                client=openai_client,
                model=model,
                seed=args.seed,
                repeats=args.judge_repeats,
            )

            faith_fn: Callable[[QaCase, TurnResult], Awaitable[float]] | None = None
            if not args.no_faithfulness:
                if accounts is None:
                    raise AssertionError("faithfulness 路径缺少账户配置")
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
        model=model, seed=args.seed, judge_repeats=args.judge_repeats,
        base_url=base_url, mode=mode,
        started_at=started_at, duration_s=time.monotonic() - started,
        cases=cases, cases_dir=EVALS_ROOT / "cases", collected_path=collected_path,
        source_collection_sha256=source_collection_sha256,
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
    parser.add_argument(
        "--judge-repeats",
        type=int,
        default=5,
        help="每个用例独立 judge 次数；默认严格 5 轮（生产评估不得靠重试覆盖失败）",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
