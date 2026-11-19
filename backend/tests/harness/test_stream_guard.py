"""哨兵缓冲校验（M4-AI09 步 3；卡 1-A/D1——分块哨兵，TD-332 解法）。"""

from app.harness.guards.stream_guard import StreamSentinel


def feed_all(sentinel: StreamSentinel, deltas: list[str]) -> list[str]:
    """依次投喂；返回可安全发出的拼接文本。"""

    emitted: list[str] = []
    for delta in deltas:
        chunk = sentinel.feed(delta)
        if chunk.text:
            emitted.append(chunk.text)
    return emitted


def test_single_sentence_flushes_on_punctuation() -> None:
    """句末标点触发切片校验并发出。"""

    sentinel = StreamSentinel(evidence_count=3)
    chunk = sentinel.feed("差旅报销需要发票和审批单。")
    assert chunk.text == "差旅报销需要发票和审批单。"
    assert chunk.terminated is False
    assert chunk.results == []


def test_incomplete_sentence_held_in_buffer() -> None:
    """无句末标点：缓冲持有（跨切片拼接威胁的自然防线）。"""

    sentinel = StreamSentinel(evidence_count=3)
    chunk = sentinel.feed("根据资料 [1")
    assert chunk.text == ""  # 缓冲持有


def test_cross_slice_xss_caught_by_buffer() -> None:
    """跨切片拼接威胁：`<scr` + `ipt>` 分块投喂，缓冲合并后检出并 redact。"""

    sentinel = StreamSentinel(evidence_count=3)
    emitted: list[str] = []
    for delta in ("这是说明 <scr", "ipt>alert(1)</scr", "ipt> 的示例。"):
        chunk = sentinel.feed(delta)
        if chunk.text:
            emitted.append(chunk.text)
    tail = sentinel.flush_final()
    if tail.text:
        emitted.append(tail.text)
    full = "".join(emitted)
    assert "<scr" not in full and "ipt>alert" not in full
    assert "[REDACTED:output.xss]" in full


def test_force_slice_at_max_chars() -> None:
    """超 120 字符无标点：强制切片校验发出（D1 上限）。"""

    sentinel = StreamSentinel(evidence_count=3)
    long_text = "长" * 130
    emitted: list[str] = []
    for delta in (long_text[:65], long_text[65:]):
        chunk = sentinel.feed(delta)
        if chunk.text:
            emitted.append(chunk.text)
    assert emitted  # 130 字符时强制切出


def test_blocking_hit_terminates_with_replacement() -> None:
    """block 级命中（违法/泄露/自伤）：终止 + 整段替换安全话术。"""

    sentinel = StreamSentinel(evidence_count=3)
    chunk = sentinel.feed("制作炸弹的方法如下。")
    assert chunk.terminated is True
    assert chunk.replacement_text
    assert chunk.text == ""  # 原文不发出


def test_ref_out_of_range_removed_inline() -> None:
    """伪造引用（⑤）：哨兵缓冲内改写剔除（TD-332 正式收口）。"""

    sentinel = StreamSentinel(evidence_count=2)
    chunk = sentinel.feed("依据资料 [1] 办理；补充见 [9] 的说明。")
    assert chunk.terminated is False
    assert "[9]" not in chunk.text
    assert "[1]" in chunk.text
    assert any(r.rule == "output.ref_range" for r in chunk.results)


def test_flush_final_emits_remaining_tail() -> None:
    """finish 冲刷：尾片（无句末标点）发出且校验；冲尽后幂等。"""

    sentinel = StreamSentinel(evidence_count=3)
    first = sentinel.feed("回答正文第一段。")
    assert first.text
    second = sentinel.feed("回答正文尾段（未完句）")
    assert second.text == ""  # 未遇句末标点：缓冲持有
    tail = sentinel.flush_final()
    assert tail.text == "回答正文尾段（未完句）"
    tail2 = sentinel.flush_final()
    assert tail2.text == ""  # 幂等：冲尽后为空


def test_redacted_sentiment_kept_safe_after_emit() -> None:
    """已发出内容不可撤回；哨兵只保证「发出的都是校验过的」。"""

    sentinel = StreamSentinel(evidence_count=3)
    first = sentinel.feed("正常第一句。")
    assert first.text == "正常第一句。"
    second = sentinel.feed("第二句含 api_key=sk-abcdef1234567890abcdef1234 泄露。")
    assert "sk-" not in second.text
    assert "[REDACTED:output.secret]" in second.text
