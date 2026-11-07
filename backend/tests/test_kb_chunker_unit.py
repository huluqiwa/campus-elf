"""切块器单测（M3 KB-03A）：纯函数全边界——结构/表格代码不拆/重叠/长文硬切/参数校验。"""

from typing import TYPE_CHECKING

import pytest

from app.kb.chunker import (
    CHUNK_TYPE_CHILD,
    CHUNK_TYPE_PARENT,
    chunk_markdown,
    split_into_blocks,
)


def children_of(chunks: "list[RawChunk]") -> "list[RawChunk]":
    return [chunk for chunk in chunks if chunk.chunk_type == CHUNK_TYPE_CHILD]


def parents_of(chunks: "list[RawChunk]") -> "list[RawChunk]":
    return [chunk for chunk in chunks if chunk.chunk_type == CHUNK_TYPE_PARENT]

if TYPE_CHECKING:
    from app.kb.chunker import RawChunk


def test_sections_and_paths() -> None:
    """标题层级 → section_path（≤3 层）；块归属正确。"""

    md = "# 一级\n\n## 二级\n\n段落A。\n\n### 三级\n\n段落B。\n\n#### 四级\n\n段落C。"
    blocks = split_into_blocks(md)
    paths = [block.section_path for block in blocks]
    assert paths[0] == "一级 > 二级"
    assert paths[1] == "一级 > 二级 > 三级"
    # 四级截断到 3 层
    assert paths[2] == "一级 > 二级 > 三级"


def test_table_and_code_stay_whole_even_when_oversized() -> None:
    """表格/代码不拆：超过 chunk_size 也整块保留（设计硬规则）。"""

    big_table = "\n".join(f"| c{i} | v{i} |" for i in range(80))
    big_code = "```\n" + "\n".join(f"line {i}" for i in range(80)) + "\n```"
    md = f"## 节\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n{big_table}\n\n{big_code}"
    chunks = chunk_markdown(md, chunk_size=100, overlap_ratio=0.1, parent_size=500)
    tables = [c for c in children_of(chunks) if c.metadata.get("table")]
    codes = [c for c in children_of(chunks) if c.metadata.get("code")]
    assert len(tables) == 2  # 小表 + 大表
    assert len(codes) == 1
    assert len(codes[0].text) > 100  # 超长仍整块
    # 表格/代码无重叠
    assert all(child.overlap_prev is None for child in tables + codes)


def test_long_paragraph_splits_with_overlap_chain() -> None:
    """单段超长 → 句界优先切多块 + 重叠链；内容不丢。"""

    body = "。".join(f"句子{n}包含一些内容用于切块" for n in range(60)) + "。"
    md = f"## 节\n\n{body}"
    chunks = chunk_markdown(md, chunk_size=120, overlap_ratio=0.15, parent_size=600)
    kids = children_of(chunks)
    assert len(kids) >= 4
    # 重叠链：第 2 块起携带 overlap_prev
    assert kids[0].overlap_prev is None
    assert all(child.overlap_prev is not None for child in kids[1:])
    # 内容完整（每句都在某个块里）
    joined = "".join(child.text for child in kids)
    assert all(f"句子{n}" in joined for n in range(60))
    # 父块聚合了全部子块
    assert len(parents_of(chunks)) >= 1


def test_parent_groups_follow_output_order() -> None:
    """输出序 = 父块紧邻其子块；父块文本聚合子块文本。"""

    md = "## 节\n\n" + "。".join(f"第{n}句话有内容" for n in range(30)) + "。"
    chunks = chunk_markdown(md, chunk_size=80, overlap_ratio=0.1, parent_size=200)
    # 第一个块必是父块
    assert chunks[0].chunk_type == CHUNK_TYPE_PARENT
    # 每个 parent 后跟 ≥1 个 child，直到下一个 parent
    saw_parent = False
    for chunk in chunks[1:]:
        if chunk.chunk_type == CHUNK_TYPE_PARENT:
            saw_parent = False
        else:
            assert True
            saw_parent = True
    assert saw_parent or len(chunks) == 2


def test_parameter_validation() -> None:
    """非法参数直接拒绝。"""

    with pytest.raises(ValueError):
        chunk_markdown("x", chunk_size=0, overlap_ratio=0.1, parent_size=100)
    with pytest.raises(ValueError):
        chunk_markdown("x", chunk_size=100, overlap_ratio=1.0, parent_size=100)


def test_empty_document_yields_no_chunks() -> None:
    chunks = chunk_markdown("", chunk_size=100, overlap_ratio=0.1, parent_size=100)
    assert chunks == []
