"""M4-AI04 单元测试：引用语义相关性校验（FakeEmbedder，无外部依赖）。

覆盖：parse_cited 解析口径（去重升序+边界）；RefSemanticChecker 三态
（全保留/剔除/零引用不触发嵌入）；余弦纯函数；阈值校验；协议可检。
"""

import pytest

from app.harness.ref_semantics import (
    EmbedPort,
    RefSemanticChecker,
    cosine_similarity,
    parse_cited,
)
from app.kb.chat_retrieval import ChatEvidence


class FakeEmbedder:
    """可控嵌入替身：按注册表返回向量；记录调用批次。"""

    provider_name = "fake"

    def __init__(self, *, table: dict[str, list[float]], fail: bool = False) -> None:
        self._table = table
        self._fail = fail
        self.calls: list[list[str]] = []

    async def embed(self, texts):  # type: ignore[no-untyped-def]
        self.calls.append(list(texts))
        if self._fail:
            raise RuntimeError("embedder boom")
        return [self._table[text] for text in texts]


def evidence(chunk_id: str, text: str) -> ChatEvidence:
    return ChatEvidence(
        chunk_id=chunk_id, score=0.9, text=text, doc_title="校历",
        section_path="第一章", page_start=3, page_end=3, chunk_type="text",
        version_id="v1",
    )


def _make_checker(
    table: dict[str, list[float]], *, min_similarity: float = 0.4, fail: bool = False,
) -> tuple[RefSemanticChecker, FakeEmbedder]:
    embedder = FakeEmbedder(table=table, fail=fail)
    checker = RefSemanticChecker(embedder=embedder, min_similarity=min_similarity)
    return checker, embedder


# ---- parse_cited ----


def test_parse_cited_dedup_sorted_and_bounds() -> None:
    text = "见 [2] 与 [1]，重复 [2]，超界 [9]（哨兵已剔除，防御性再滤）"
    assert parse_cited(text, evidence_count=3) == [1, 2]


def test_parse_cited_empty_and_zero_evidence() -> None:
    assert parse_cited("没有任何引用", evidence_count=3) == []
    assert parse_cited("见 [1]", evidence_count=0) == []


# ---- 余弦 ----


def test_cosine_similarity_identical_orthogonal_and_zero_vector() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


# ---- RefSemanticChecker ----


@pytest.mark.asyncio
async def test_check_all_relevant_keeps_every_citation() -> None:
    """答案与两个被引块同主题（向量同向）→ 全部保留；一次批量嵌入。"""

    topic_a = [1.0, 0.0]
    table = {
        "报销需要发票 [1]。审批单要提前交 [2]。": topic_a,
        "报销需要发票。": topic_a,
        "审批单要提前交。": topic_a,
    }
    checker, embedder = _make_checker(table)
    ev = [evidence("c1", "报销需要发票。"), evidence("c2", "审批单要提前交。")]
    result = await checker.check("报销需要发票 [1]。审批单要提前交 [2]。", ev)

    assert result.checked is True
    assert result.cited == [1, 2]
    assert result.removed == []
    assert all(v.relevant for v in result.verdicts)
    assert embedder.calls == [
        ["报销需要发票 [1]。审批单要提前交 [2]。", "报销需要发票。", "审批单要提前交。"],
    ]


@pytest.mark.asyncio
async def test_check_irrelevant_removed() -> None:
    """幻觉引用：[2] 与答案语义无关（正交）→ removed；verdicts 保留判定明细。"""

    table = {
        "报销流程 [1]。考勤打卡 [2]。": [1.0, 0.0],
        "报销流程。": [1.0, 0.0],
        "考勤打卡时间。": [0.0, 1.0],
    }
    checker, _ = _make_checker(table)
    ev = [evidence("c1", "报销流程。"), evidence("c2", "考勤打卡时间。")]
    result = await checker.check("报销流程 [1]。考勤打卡 [2]。", ev)

    assert result.cited == [1, 2]
    assert [v.index for v in result.removed] == [2]
    assert result.verdicts[0].relevant is True
    assert result.verdicts[1].relevant is False
    assert result.verdicts[1].similarity == pytest.approx(0.0, abs=1e-6)
    assert result.verdicts[1].chunk_id == "c2"


@pytest.mark.asyncio
async def test_check_zero_cited_skips_embedding() -> None:
    """正文零引用 → 不触发嵌入调用（零延迟成本），cited 空。"""

    checker, embedder = _make_checker({"报销流程。": [1.0, 0.0]})
    ev = [evidence("c1", "报销流程。")]
    result = await checker.check("这段回答没带引用标记。", ev)

    assert result.cited == []
    assert result.removed == []
    assert result.checked is True
    assert embedder.calls == []


@pytest.mark.asyncio
async def test_check_nav_entry_exempt_from_semantic_check() -> None:
    """nav: 入口证据豁免（决策器主动获取的确定性证据；短标签语义校验必然误杀）。

    只嵌入 KB 块；入口 verdict 直接 relevant=True（sim=1.0）；无关 KB 块照常剔除。
    """

    table = {
        "报销流程 [1]。考勤打卡 [2]。入口 [3]。": [1.0, 0.0],
        "报销流程。": [1.0, 0.0],
        "考勤打卡时间。": [0.0, 1.0],
    }
    checker, embedder = _make_checker(table)
    nav_ev = ChatEvidence(
        chunk_id="nav:请假", score=1.0, text="请假：/leave", doc_title="业务入口",
        section_path=None, page_start=None, page_end=None, chunk_type="entry",
        version_id="v1",
    )
    ev = [evidence("c1", "报销流程。"), evidence("c2", "考勤打卡时间。"), nav_ev]
    result = await checker.check("报销流程 [1]。考勤打卡 [2]。入口 [3]。", ev)

    assert result.cited == [1, 2, 3]
    # 只嵌入答案 + 两个 KB 块（nav 豁免不进嵌入批次）
    assert embedder.calls == [
        ["报销流程 [1]。考勤打卡 [2]。入口 [3]。", "报销流程。", "考勤打卡时间。"],
    ]
    assert [v.index for v in result.removed] == [2]
    nav_verdict = result.verdicts[2]
    assert nav_verdict.chunk_id == "nav:请假"
    assert nav_verdict.relevant is True
    assert nav_verdict.similarity == 1.0


def test_min_similarity_bounds_validated() -> None:
    with pytest.raises(ValueError):
        RefSemanticChecker(embedder=FakeEmbedder(table={}), min_similarity=1.4)
    with pytest.raises(ValueError):
        RefSemanticChecker(embedder=FakeEmbedder(table={}), min_similarity=-0.1)


def test_embed_port_protocol_satisfied_by_fake() -> None:
    assert isinstance(FakeEmbedder(table={}), EmbedPort)

@pytest.mark.asyncio
async def test_check_similarity_equal_threshold_is_relevant() -> None:
    """sim 恰等于阈值 → 相关（>= 判据，简报 §5 步 1 的边界弧）。

    同向单位向量余弦恒为精确 1.0（无浮点漂移），阈值取 1.0 构造精确等值。
    """

    table = {
        "答案 [1]。": [1.0, 0.0],
        "答案块。": [1.0, 0.0],
    }
    checker, _ = _make_checker(table, min_similarity=1.0)
    ev = [evidence("c1", "答案块。")]
    result = await checker.check("答案 [1]。", ev)

    assert result.removed == []
    assert result.verdicts[0].relevant is True
    assert result.verdicts[0].similarity == pytest.approx(1.0)
