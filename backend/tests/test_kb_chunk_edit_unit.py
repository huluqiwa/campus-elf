"""KB-10A 块编辑纯函数单测：内容校验 / 乐观锁判据 / 派生判据（简报 M3-KB-10 §3）。"""

import pytest

from app.kb.chunk_edit import (
    ChunkEditValidationError,
    check_optimistic_lock,
    derive_gate,
    derive_target_version_no,
    validate_edit_content,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


def _code(exc: ChunkEditValidationError) -> str:
    return exc.code


class TestValidateEditContent:
    """内容校验（卡 D5-A：非空/长度上限/围栏配对/表格完整；trim 规范化）。"""

    def test_plain_text_trimmed_and_accepted(self) -> None:
        assert validate_edit_content("  新内容  ") == "新内容"

    def test_table_and_code_blocks_accepted(self) -> None:
        table = "| 参数 | 默认值 |\n| --- | --- |\n| 超时 | 30 |"
        assert validate_edit_content(table) == table
        code = "```python\nprint(1)\n```"
        assert validate_edit_content(code) == code

    def test_blank_rejected(self) -> None:
        for blank in ("", "   ", "\n\t "):
            with pytest.raises(ChunkEditValidationError) as exc:
                validate_edit_content(blank)
            assert _code(exc.value) == "CHUNK_CONTENT_INVALID"
            assert "不能为空" in str(exc.value)

    def test_over_length_rejected(self) -> None:
        with pytest.raises(ChunkEditValidationError) as exc:
            validate_edit_content("字" * 501)
        assert _code(exc.value) == "CHUNK_CONTENT_INVALID"
        assert "长度" in str(exc.value)
        # 恰好 500 码点合法
        assert validate_edit_content("字" * 500) == "字" * 500

    def test_unclosed_code_fence_rejected(self) -> None:
        with pytest.raises(ChunkEditValidationError) as exc:
            validate_edit_content("```python\nprint(1)\n")
        assert _code(exc.value) == "CHUNK_CONTENT_INVALID"
        assert "围栏" in str(exc.value)

    def test_incomplete_table_row_rejected(self) -> None:
        # 表格行组中以 | 开头但无收尾管道的行 → 结构断裂
        bad = "| 参数 | 默认值 |\n| --- 无尾管道"
        with pytest.raises(ChunkEditValidationError) as exc:
            validate_edit_content(bad)
        assert _code(exc.value) == "CHUNK_CONTENT_INVALID"
        assert "表格" in str(exc.value)

    def test_row_without_pipe_terminates_table_group_honest_boundary(self) -> None:
        # 组中间无管道行终止表格组（1 行组亦合法）——轻量静态校验不检测此形态
        # （无法区分「用户重组内容」与「表格断裂」；结构性破坏由「首尾管道齐备」覆盖）
        ok = "| 参数 | 默认值 |\n参数 无管道\n| --- | --- |"
        assert validate_edit_content(ok) == ok

    def test_inline_pipe_does_not_trigger_table_check(self) -> None:
        # 行内单个 | 不构成表格组（≥2 行含 | 才检查）
        assert validate_edit_content("a | b") == "a | b"


class TestOptimisticLock:
    """base_hash 乐观锁（spec :324——409 CHUNK_CONFLICT）。"""

    def test_same_hash_passes(self) -> None:
        assert check_optimistic_lock(HASH_A, HASH_A) is None

    def test_stale_base_hash_conflicts(self) -> None:
        assert check_optimistic_lock(HASH_A, HASH_B) == "CHUNK_CONFLICT"


class TestDeriveGate:
    """派生判据（spec :326-327——无提交 409 DERIVE_NO_EDITS / 重复确认 409 CHUNK_EDIT_STATE）。"""

    def test_no_submitted_edits_rejected(self) -> None:
        assert derive_gate(0, False) == "DERIVE_NO_EDITS"
        # 重复派生检测优先于提交数（confirmed 记录在，即使全部翻转也 409）
        assert derive_gate(0, True) == "CHUNK_EDIT_STATE"
        assert derive_gate(2, True) == "CHUNK_EDIT_STATE"

    def test_repeat_derive_rejected(self) -> None:
        assert derive_gate(2, True) == "CHUNK_EDIT_STATE"

    def test_submitted_edits_pass(self) -> None:
        assert derive_gate(1, False) is None


class TestDeriveTargetVersionNo:
    """派生版本号 = max+1（D2-A）。"""

    def test_next_after_max(self) -> None:
        assert derive_target_version_no([1, 3, 2]) == 4

    def test_empty_versions_starts_at_1(self) -> None:
        assert derive_target_version_no([]) == 1
