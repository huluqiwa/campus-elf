"""qa_cases 单测：用例加载 / sample_kb 拆分 / 引用锚点匹配（M4-EVAL 简报步 1）。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
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


def test_td362_loads_explicit_contract_and_preserves_legacy_defaults(tmp_path: Path) -> None:
    raw = asdict(load_cases(EVALS_ROOT / "cases")[0])
    for name in ("semantic_forbidden", "optional_points", "citation_policy"):
        raw.pop(name, None)
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8")
    legacy = load_cases(tmp_path)[0]
    assert legacy.semantic_forbidden == []
    assert legacy.optional_points == []
    assert legacy.citation_policy == "required"
    raw.update(semantic_forbidden=["不披露凭证值"], optional_points=["可选指引"])
    path.write_text(json.dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8")
    loaded = load_cases(tmp_path)[0]
    assert loaded.semantic_forbidden == ["不披露凭证值"]
    assert loaded.optional_points == ["可选指引"]


@pytest.mark.parametrize("field", ["semantic_forbidden", "optional_points"])
@pytest.mark.parametrize("value", [None, "不是列表", [1], [""], ["  "]])
def test_td362_rejects_invalid_new_list_fields(tmp_path: Path, field: str, value) -> None:
    raw = asdict(load_cases(EVALS_ROOT / "cases")[0])
    raw[field] = value
    (tmp_path / "bad.jsonl").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=field):
        load_cases(tmp_path)


@pytest.mark.parametrize("value", [None, [], "unknown", "always_exempt"])
def test_td362_unknown_citation_policy_is_rejected(tmp_path: Path, value) -> None:
    raw = asdict(load_cases(EVALS_ROOT / "cases")[0])
    raw["citation_policy"] = value
    (tmp_path / "bad.jsonl").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="citation_policy"):
        load_cases(tmp_path)


def test_td362_only_manifested_15_cases_change_and_195_identities_stay_fixed() -> None:
    manifest = json.loads((EVALS_ROOT / "fixtures/m4_eval_v1_manifest.json").read_text(encoding="utf-8"))
    expected_changed = {
        "cross_tenant-004", "cross_tenant-009", "cross_tenant-010", "cross_tenant-012",
        "cross_tenant-017", "cross_tenant-018", "cross_tenant-019", "cross_tenant-020",
        "prompt_injection-010", "prompt_injection-011", "prompt_injection-019", "prompt_injection-020",
        "sensitive_content-002", "sensitive_content-005", "sensitive_content-012",
    }

    def digest(raw: object) -> str:
        data = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(data.encode()).hexdigest()

    raw_cases = [
        json.loads(line)
        for path in sorted((EVALS_ROOT / "cases").glob("*.jsonl"))
        if path.name != "chunk_grid.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(raw_cases) == 195
    assert {raw["id"] for raw in raw_cases} == set(manifest["cases"])
    changed = set()
    identities = []
    for raw in raw_cases:
        identity = {key: raw[key] for key in ("id", "input", "category", "adversarial")}
        identities.append(identity)
        if digest(raw) != manifest["cases"][raw["id"]]:
            changed.add(raw["id"])
    assert digest(sorted(identities, key=lambda item: item["id"])) == manifest["identity_sha256"]
    assert changed == expected_changed
    loaded = {case.id: case for case in load_cases(EVALS_ROOT / "cases")}
    conditional = {case.id for case in loaded.values() if case.citation_policy != "required"}
    assert conditional == {"prompt_injection-010", "prompt_injection-020"}
    for cid in conditional:
        assert loaded[cid].expected_citations == ["学生请假管理办法#审批人"]
    optional = loaded["sensitive_content-002"]
    assert optional.optional_points == ["可提供预约方式"]
    assert optional.expected_points == ["不能诊断", "建议咨询心理中心/专业医生"]
    for cid in ("cross_tenant-009", "cross_tenant-017", "cross_tenant-018", "cross_tenant-019"):
        assert all("collection" not in point and "tenant-scoped" not in point for point in loaded[cid].expected_points)
