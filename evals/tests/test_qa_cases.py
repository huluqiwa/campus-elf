"""qa_cases 单测：用例加载 / sample_kb 拆分 / 引用锚点匹配（M4-EVAL 简报步 1）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from campus_elf_evals.qa_cases import (
    CLASSIFICATION_CATEGORIES,
    FAITHFULNESS_CATEGORIES,
    SAFETY_CATEGORIES,
    anchor_matches,
    load_cases,
    parse_anchor,
    split_sample_kb,
)

EVALS_ROOT = Path(__file__).resolve().parents[1]


def test_load_cases_skips_chunk_grid_and_counts_195() -> None:
    cases = load_cases(EVALS_ROOT / "cases")
    assert len(cases) == 195
    assert all(c.category != "chunk_grid" for c in cases)
    assert len({c.id for c in cases}) == 195
    assert {c.category for c in cases} == (
        SAFETY_CATEGORIES | CLASSIFICATION_CATEGORIES | FAITHFULNESS_CATEGORIES | {"refusal"}
    )


def test_split_real_fixture_yields_eight_documents() -> None:
    text = (EVALS_ROOT / "fixtures" / "sample_kb.md").read_text(encoding="utf-8")
    docs = split_sample_kb(text)
    assert [d.title for d in docs] == [
        "奖学金评定办法",
        "学生请假管理办法",
        "图书馆借阅规则",
        "考试纪律",
        "宿舍管理规定",
        "校园网服务",
        "心理咨询预约",
        "学生社团活动",
    ]
    assert all(d.markdown.startswith(f"## {d.title}") for d in docs)


def test_split_keeps_subheadings_and_drops_preamble() -> None:
    text = "# 手册\n\n> 合成数据声明\n\n## 甲办法\n\n### 条件\n\n正文甲。\n\n## 乙办法\n\n正文乙。\n"
    docs = split_sample_kb(text)
    assert [d.title for d in docs] == ["甲办法", "乙办法"]
    assert "### 条件" in docs[0].markdown
    assert "合成数据声明" not in docs[0].markdown
    assert "正文乙" in docs[1].markdown


def test_split_ignores_heading_inside_code_fence() -> None:
    text = "## 甲\n\n```\n## 不是分界\n```\n\n正文。\n\n## 乙\n\n乙正文。\n"
    docs = split_sample_kb(text)
    assert [d.title for d in docs] == ["甲", "乙"]
    assert "## 不是分界" in docs[0].markdown


def test_split_skips_empty_section_and_rejects_no_section() -> None:
    assert [d.title for d in split_sample_kb("## 空\n\n## 有\n\n正文。\n")] == ["有"]
    with pytest.raises(ValueError):
        split_sample_kb("# 只有引言\n\n没有任何二级标题。\n")


def test_parse_anchor_and_match() -> None:
    assert parse_anchor("奖学金评定办法#申请条件") == ("奖学金评定办法", "申请条件")
    assert anchor_matches("奖学金评定办法#申请条件", "奖学金评定办法", "奖学金评定办法 > 申请条件")
    assert not anchor_matches("奖学金评定办法#申请条件", "学生请假管理办法", "学生请假管理办法 > 申请条件")
    assert not anchor_matches("奖学金评定办法#申请条件", "奖学金评定办法", "奖学金评定办法 > 申请材料")
    assert not anchor_matches("奖学金评定办法#申请条件", "奖学金评定办法", None)


def test_parse_anchor_rejects_missing_separator() -> None:
    with pytest.raises(ValueError):
        parse_anchor("没有井号的锚点")