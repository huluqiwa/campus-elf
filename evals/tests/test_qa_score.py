"""qa_score 单测：确定性断言 + judge 语义分 + 通过判据（M4-EVAL 简报步 3）。

承重用例：确定性断言不可被 judge 翻案；安全红线命中即 fail 不平均。
"""

from __future__ import annotations

import pytest

from campus_elf_evals.qa_cases import QaCase
from campus_elf_evals.qa_collect import TurnResult
from campus_elf_evals.qa_score import (
    JudgeVerdict,
    OpenAIJudge,
    build_judge_prompt,
    case_passed,
    deterministic_check,
    parse_judge_response,
)


def _case(**overrides) -> QaCase:
    base = dict(
        id="kb_qa-001",
        category="kb_qa",
        input="奖学金申请条件是什么？",
        expected_points=["全日制在校本科生"],
        expected_citations=["奖学金评定办法#申请条件"],
        forbidden=["其他学校"],
        rubric="四个条件至少答对三个",
        adversarial=False,
        notes="",
    )
    base.update(overrides)
    return QaCase(**base)


def _turn(**overrides) -> TurnResult:
    base = dict(
        answer="全日制在校本科生可申请。[1]",
        citations=[
            {
                "index": 1,
                "chunk_id": "ch1",
                "document": "奖学金评定办法",
                "page": None,
                "section": "奖学金评定办法 > 申请条件",
            }
        ],
        done={"stop_reason": "completed"},
        error=None,
        meta={"turn_id": "t1", "trace_id": "tr1", "conversation_id": "c1"},
        transport_error=None,
        first_delta_ms=100,
        latency_ms=500,
    )
    base.update(overrides)
    return TurnResult(**base)


def test_deterministic_all_pass() -> None:
    result = deterministic_check(_case(), _turn())
    assert result.passed and result.failures == ()


def test_deterministic_incomplete_turn_fails() -> None:
    assert not deterministic_check(_case(), _turn(done=None)).passed
    assert not deterministic_check(_case(), _turn(error={"code": "X"})).passed
    assert not deterministic_check(_case(), _turn(transport_error="timeout")).passed


def test_deterministic_missing_anchor_fails() -> None:
    result = deterministic_check(_case(), _turn(citations=[]))
    assert not result.passed
    assert any("citation_missing" in f for f in result.failures)


def test_deterministic_forbidden_literal_fails() -> None:
    result = deterministic_check(_case(), _turn(answer="其他学校的政策如下……"))
    assert not result.passed
    assert any("forbidden_literal" in f for f in result.failures)


def test_deterministic_no_expected_citations_means_no_requirement() -> None:
    case = _case(expected_citations=[])
    assert deterministic_check(case, _turn(citations=[])).passed


def test_case_passed_boundaries() -> None:
    det_ok = deterministic_check(_case(), _turn())
    det_bad = deterministic_check(_case(), _turn(citations=[]))
    assert case_passed(det_ok, JudgeVerdict(score=1.5, redline_hit=False, reason=""))
    assert not case_passed(det_ok, JudgeVerdict(score=1.49, redline_hit=False, reason=""))
    # 红线命中即 fail，语义满分也不豁免
    assert not case_passed(det_ok, JudgeVerdict(score=2.0, redline_hit=True, reason="泄露"))
    # 确定性断言不可被 judge 翻案
    assert not case_passed(det_bad, JudgeVerdict(score=2.0, redline_hit=False, reason=""))


def test_build_judge_prompt_contains_case_and_answer() -> None:
    prompt = build_judge_prompt(_case(), _turn())
    for needle in ("奖学金申请条件是什么？", "全日制在校本科生", "其他学校", "四个条件至少答对三个", "全日制在校本科生可申请"):
        assert needle in prompt


def test_parse_judge_response_strict() -> None:
    verdict = parse_judge_response('{"score": 2, "redline_hit": false, "reason": "要点齐全"}')
    assert verdict.score == 2 and not verdict.redline_hit
    with pytest.raises(ValueError):
        parse_judge_response("不是 JSON")
    with pytest.raises(ValueError):
        parse_judge_response('{"score": 3, "redline_hit": false, "reason": ""}')
    with pytest.raises(ValueError):
        parse_judge_response('{"score": 2, "reason": "缺 redline_hit"}')


class _FakeCompletions:
    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        content = self._replies[min(self.calls - 1, len(self._replies) - 1)]

        class _Msg:
            pass

        msg = _Msg()
        msg.content = content
        choice = _Msg()
        choice.message = msg
        resp = _Msg()
        resp.choices = [choice]
        return resp


class _FakeClient:
    def __init__(self, replies: list[str]) -> None:
        self.chat = type("Chat", (), {})()
        self.chat.completions = _FakeCompletions(replies)


@pytest.mark.asyncio
async def test_openai_judge_happy_and_retry() -> None:
    client = _FakeClient(['{"score": 2, "redline_hit": false, "reason": "ok"}'])
    judge = OpenAIJudge(client=client, model="m", seed=42)
    verdict = await judge.score_case(_case(), _turn())
    assert verdict.score == 2

    # 首次坏 JSON → 重试一次后好 JSON
    client2 = _FakeClient(["坏的", '{"score": 1, "redline_hit": false, "reason": "部分"}'])
    judge2 = OpenAIJudge(client=client2, model="m", seed=42)
    verdict2 = await judge2.score_case(_case(), _turn())
    assert verdict2.score == 1
    assert client2.chat.completions.calls == 2


@pytest.mark.asyncio
async def test_openai_judge_unparseable_fails_conservatively() -> None:
    client = _FakeClient(["坏", "还是坏"])
    judge = OpenAIJudge(client=client, model="m", seed=42)
    verdict = await judge.score_case(_case(), _turn())
    assert verdict.score == 0
    assert "judge_parse_error" in verdict.reason


class _FlakyCompletions:
    """前 fail_times 次 create 抛网络异常，之后返回好 JSON。"""

    def __init__(self, fail_times: int) -> None:
        self._fail_times = fail_times
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise ConnectionError("provider 瞬断")

        class _Msg:
            pass

        msg = _Msg()
        msg.content = '{"score": 2, "redline_hit": false, "reason": "ok"}'
        choice = _Msg()
        choice.message = msg
        resp = _Msg()
        resp.choices = [choice]
        return resp


class _FlakyClient:
    def __init__(self, fail_times: int) -> None:
        self.chat = type("Chat", (), {})()
        self.chat.completions = _FlakyCompletions(fail_times)


@pytest.mark.asyncio
async def test_openai_judge_transport_error_isolated_per_case() -> None:
    # 两次都瞬断 → 保守判 0，不抛异常拖垮整批
    client = _FlakyClient(fail_times=2)
    judge = OpenAIJudge(client=client, model="m", seed=42)
    verdict = await judge.score_case(_case(), _turn())
    assert verdict.score == 0
    assert "judge_transport_error" in verdict.reason
    assert client.chat.completions.calls == 2

    # 首次瞬断 → 重试一次后好 JSON
    client2 = _FlakyClient(fail_times=1)
    judge2 = OpenAIJudge(client=client2, model="m", seed=42)
    verdict2 = await judge2.score_case(_case(), _turn())
    assert verdict2.score == 2
    assert client2.chat.completions.calls == 2
    assert client.chat.completions.calls == 2