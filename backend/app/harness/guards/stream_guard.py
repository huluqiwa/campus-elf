"""分块哨兵缓冲（M4-AI09 步 3；卡 1-A/D1——TD-332 的解法）。

GENERATE 流式时逐 delta 投喂；缓冲按**句末标点 + 最长 120 字符**切片，
切片先过七类检测再发出：

- redact 级命中 → 在缓冲文本上应用替换后发出（用户看到脱敏版）
- block 级命中 → **终止生成**：原文不发出，`replacement_text` 为安全话术/
  支持性话术，loop 据此发 delta + done(GUARD_BLOCK)
- `flush_final()`：finish 时冲刷尾片（无句末标点的末段也校验）

已发出内容不可撤回——哨兵只保证「发出的都是校验过的」（块内先校验后发）。
跨切片拼接威胁（如 `<scr`+`ipt>` 分块）由缓冲合并后检出——未遇句末标点
前原文滞留缓冲（自然防线，test_cross_slice_xss_caught_by_buffer 钉住）。
"""

from dataclasses import dataclass, field

from app.harness.contracts import GuardResult
from app.harness.guards.output import scan_output
from app.harness.guards.rules import MAX_SLICE_CHARS, SENTENCE_ENDINGS


@dataclass
class GuardedChunk:
    """一次 feed/flush 的产出（loop 消费）。"""

    text: str = ""
    terminated: bool = False
    replacement_text: str | None = None
    results: list[GuardResult] = field(default_factory=list)


def _ends_with_sentence(text: str) -> bool:
    return bool(text) and text[-1] in SENTENCE_ENDINGS


class StreamSentinel:
    """滚动哨兵缓冲（单回合单实例；纯内存无 IO——处置归 loop）。"""

    def __init__(self, *, evidence_count: int) -> None:
        self._evidence_count = evidence_count
        self._pending = ""
        self._done = False

    def feed(self, delta: str) -> GuardedChunk:
        """投喂一个 delta；返回可发出文本（可能为空=缓冲持有）。"""

        if self._done:
            return GuardedChunk()
        self._pending += delta
        if not (_ends_with_sentence(self._pending) or len(self._pending) >= MAX_SLICE_CHARS):
            return GuardedChunk()
        return self._flush_pending()

    def flush_final(self) -> GuardedChunk:
        """finish 冲刷：尾片校验发出；幂等（冲尽后返回空块）。"""

        if self._done:
            return GuardedChunk()
        chunk = self._flush_pending()
        self._done = True
        return chunk

    def _flush_pending(self) -> GuardedChunk:
        pending, self._pending = self._pending, ""
        scan = scan_output(pending, evidence_count=self._evidence_count)
        if scan.blocking:
            self._done = True
            return GuardedChunk(
                text="",
                terminated=True,
                replacement_text=self._safety_script(scan.results),
                results=scan.results,
            )
        return GuardedChunk(text=scan.redacted_text, results=scan.results)

    def _safety_script(self, results: list[GuardResult]) -> str:
        """block 级命中的整段替换话术（按 rule 选取；自伤=支持性话术）。"""

        from app.harness.guards.scripts import safety_script_for

        return safety_script_for({r.rule for r in results})
