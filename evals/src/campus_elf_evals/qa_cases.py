"""QA 评估用例加载与合成 KB 拆分（M4-EVAL 简报步 1）。

- `load_cases`：读 `evals/cases/*.jsonl`，跳过 `chunk_grid`（TD-294 检索直评估专用）。
- `split_sample_kb`：把 `fixtures/sample_kb.md` 按 `##` 顶层章节拆成独立文档，
  使 citation 的 document/section 与用例锚点 `文档名#章节名` 对齐（简报 D1-A）。
- `anchor_matches`：锚点 ↔ citation 事件字段匹配（section_path 形如「标题 > 子标题」）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

SAFETY_CATEGORIES = frozenset({"prompt_injection", "cross_tenant", "sensitive_content"})
CLASSIFICATION_CATEGORIES = frozenset({"leave_navigation", "capability_fallback", "multi_intent"})
FAITHFULNESS_CATEGORIES = frozenset({"kb_qa", "citation"})
REFUSAL_CATEGORY = "refusal"

_SKIP_FILES = frozenset({"chunk_grid.jsonl"})


@dataclass(frozen=True)
class QaCase:
    id: str
    category: str
    input: str
    expected_points: list[str]
    expected_citations: list[str]
    forbidden: list[str]
    rubric: str
    adversarial: bool
    notes: str


@dataclass(frozen=True)
class KbDoc:
    title: str
    markdown: str


def load_cases(cases_dir: Path) -> list[QaCase]:
    """加载 9 类 QA 用例；字段缺失/空值直接报错（黄金集完整性由 CI 脚本另行门禁）。"""
    cases: list[QaCase] = []
    for path in sorted(cases_dir.glob("*.jsonl")):
        if path.name in _SKIP_FILES:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            cases.append(
                QaCase(
                    id=raw["id"],
                    category=raw["category"],
                    input=raw["input"],
                    expected_points=list(raw.get("expected_points", [])),
                    expected_citations=list(raw.get("expected_citations", [])),
                    forbidden=list(raw.get("forbidden", [])),
                    rubric=raw["rubric"],
                    adversarial=bool(raw.get("adversarial", False)),
                    notes=raw.get("notes", ""),
                )
            )
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("用例 id 存在重复")
    return cases


def split_sample_kb(text: str) -> list[KbDoc]:
    """按 `## ` 顶层标题拆分；代码围栏内的 `##` 不分界；丢弃首个标题前的引言。

    异常:
        ValueError: 没有任何 `##` 章节。
    """
    docs: list[KbDoc] = []
    title: str | None = None
    buf: list[str] = []
    in_fence = False

    def flush() -> None:
        if title is None:
            return
        body = "\n".join(buf).strip()
        # 空章节（只有标题没有正文）不产出文档
        if body != f"## {title}":
            docs.append(KbDoc(title=title, markdown=body + "\n"))

    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            flush()
            title = line[3:].strip()
            buf = [line]
        elif title is not None:
            buf.append(line)
    flush()
    if not docs:
        raise ValueError("sample_kb 文本没有任何 ## 章节")
    return docs


def parse_anchor(anchor: str) -> tuple[str, str]:
    """`文档名#章节名` → (文档名, 章节名)。"""
    if "#" not in anchor:
        raise ValueError(f"锚点缺少 # 分隔: {anchor!r}")
    doc, section = anchor.split("#", 1)
    return doc.strip(), section.strip()


def anchor_matches(anchor: str, document: str, section_path: str | None) -> bool:
    """citation 事件是否命中锚点：文档名精确相等 + 章节名在 section_path 层级中。"""
    expected_doc, expected_section = parse_anchor(anchor)
    if document != expected_doc or not section_path:
        return False
    return expected_section in [part.strip() for part in section_path.split(">")]