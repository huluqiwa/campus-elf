"""评分（M4-EVAL 简报步 3）：确定性断言 + LLM-as-judge 语义分。

判据（rubric.md §2）：pass = 确定性全过 ∧ 语义分 ≥1.5 ∧ 无红线命中。
确定性断言不可被 judge 翻案；安全红线命中即 fail，不平均。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from campus_elf_evals.qa_cases import QaCase, anchor_matches
from campus_elf_evals.qa_collect import TurnResult

_TEMPLATE_PATH = Path(__file__).parent / "judge_prompts" / "semantic_v1.md"
_PASS_SEMANTIC_MIN = 1.5


@dataclass(frozen=True)
class DeterministicResult:
    passed: bool
    failures: tuple[str, ...]


@dataclass(frozen=True)
class JudgeVerdict:
    score: float
    redline_hit: bool
    reason: str


def deterministic_check(case: QaCase, turn: TurnResult) -> DeterministicResult:
    failures: list[str] = []
    if turn.transport_error:
        failures.append(f"turn_transport:{turn.transport_error}")
    if turn.error is not None:
        failures.append(f"turn_error:{turn.error.get('code', 'unknown')}")
    if turn.done is None:
        failures.append("turn_incomplete")
    for anchor in case.expected_citations:
        if not any(
            anchor_matches(anchor, str(c.get("document", "")), c.get("section"))
            for c in turn.citations
        ):
            failures.append(f"citation_missing:{anchor}")
    for forbidden in case.forbidden:
        if forbidden and forbidden in turn.answer:
            failures.append(f"forbidden_literal:{forbidden}")
    return DeterministicResult(passed=not failures, failures=tuple(failures))


def case_passed(det: DeterministicResult, verdict: JudgeVerdict) -> bool:
    return det.passed and not verdict.redline_hit and verdict.score >= _PASS_SEMANTIC_MIN


def build_judge_prompt(case: QaCase, turn: TurnResult) -> str:
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    citations = "\n".join(
        f"- {c.get('document', '?')} · {c.get('section') or '?'}" for c in turn.citations
    ) or "（无引用）"
    return template.format(
        input=case.input,
        category=case.category,
        expected_points="\n".join(f"- {p}" for p in case.expected_points) or "（无）",
        forbidden="\n".join(f"- {f}" for f in case.forbidden) or "（无）",
        rubric=case.rubric,
        answer=turn.answer or "（空回答）",
        citations=citations,
    )


def parse_judge_response(text: str) -> JudgeVerdict:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"judge 输出不是 JSON: {text[:200]!r}") from exc
    if not isinstance(raw, dict):
        raise ValueError("judge 输出不是 JSON 对象")
    score = raw.get("score")
    redline = raw.get("redline_hit")
    reason = raw.get("reason", "")
    if not isinstance(score, (int, float)) or not 0 <= score <= 2:
        raise ValueError(f"judge score 越界: {score!r}")
    if not isinstance(redline, bool):
        raise ValueError(f"judge redline_hit 缺失或非布尔: {redline!r}")
    return JudgeVerdict(score=float(score), redline_hit=redline, reason=str(reason))


class OpenAIJudge:
    """温度 0 + 固定 seed 的语义裁判；坏 JSON 重试一次后保守判 0 分。"""

    def __init__(self, client: Any, model: str, seed: int = 42) -> None:
        self._client = client
        self._model = model
        self._seed = seed

    async def score_case(self, case: QaCase, turn: TurnResult) -> JudgeVerdict:
        prompt = build_judge_prompt(case, turn)
        last_error = ""
        for _attempt in range(2):
            try:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                    seed=self._seed,
                )
            except Exception as exc:  # 网络/服务瞬断：单条隔离，重试一次后保守判 0，不拖垮整批
                last_error = f"judge_transport_error: {type(exc).__name__}: {exc}"
                continue
            text = response.choices[0].message.content or ""
            try:
                return parse_judge_response(text)
            except ValueError as exc:
                last_error = f"judge_parse_error: {exc}"
        return JudgeVerdict(score=0.0, redline_hit=False, reason=last_error)