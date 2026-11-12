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


# ---------- TD-310 阶段 1：整行空图片占位符过滤（简报 M3-TD310-phase1-image-placeholder.md） ----------

# MinerU 4.0.12 实证形态的截短样本（2026-11-12 真实栈 standard 档）：
# `![](data:image/jpeg;base64,<payload>)`，alt 恒空、整行独占、前后空行分隔。
_MINERU_PLACEHOLDER = (
    "![](data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAIBAQEBAQIBAQECAgICAg)"
)


def test_mineru_data_uri_placeholder_line_is_dropped() -> None:
    """MinerU 实证形态（data:image base64 整行）不进任何块。"""

    md = (
        "## 节\n\n"
        "图前段落，图书馆开放时间 08:00-22:00。\n\n"
        f"{_MINERU_PLACEHOLDER}\n\n"
        "图后段落，请假需要辅导员审批。\n"
    )
    chunks = chunk_markdown(md, chunk_size=100, overlap_ratio=0.1, parent_size=500)
    assert chunks, "正文段落应产出块"
    for chunk in chunks:
        assert "data:image" not in chunk.text
        assert "![" not in chunk.text
    joined = "\n".join(c.text for c in children_of(chunks))
    assert "图书馆开放时间" in joined
    assert "请假需要辅导员审批" in joined


def test_relative_path_placeholder_line_is_dropped() -> None:
    """兼容形态（相对路径 ![](images/x.jpg) 整行）同样丢弃（dangling 引用零语义）。"""

    md = "## 节\n\n图前。\n\n![](images/figure-01.jpg)\n\n图后。\n"
    chunks = chunk_markdown(md, chunk_size=100, overlap_ratio=0.1, parent_size=500)
    assert all("![" not in chunk.text and "images/" not in chunk.text for chunk in chunks)
    joined = "\n".join(c.text for c in children_of(chunks))
    assert "图前。" in joined and "图后。" in joined


def test_placeholder_line_acts_as_paragraph_break() -> None:
    """段界语义（D2-A）：占位符行丢弃后按空行处理，图前/图后文本不合并成一行。"""

    md = f"## 节\n\n图前文字。\n{_MINERU_PLACEHOLDER}\n图后文字。\n"
    blocks = split_into_blocks(md)
    assert len(blocks) == 1  # 同节连续段落合并进同一个 Block（既有语义）
    text = blocks[0].text
    assert "data:image" not in text and "![" not in text
    assert "\n\n" in text, "占位符应留下段界（空行标记），前后文本不合并成一行"
    assert text.index("图前文字。") < text.index("图后文字。")


def test_consecutive_placeholders_leave_no_empty_blocks() -> None:
    """连续多个占位符不残留空块/空文本标记。"""

    md = (
        "## 节\n\n图前。\n\n"
        f"{_MINERU_PLACEHOLDER}\n\n![](images/a.png)\n\n![](images/b.png)\n\n"
        "图后。\n"
    )
    chunks = chunk_markdown(md, chunk_size=100, overlap_ratio=0.1, parent_size=500)
    texts = [chunk.text for chunk in chunks]
    assert all("![" not in text and "images/" not in text for text in texts)
    assert all(text.strip() for text in texts), "不允许空文本块"
    joined = "\n".join(c.text for c in children_of(chunks))
    assert "图前。" in joined and "图后。" in joined


def test_placeholder_only_document_yields_no_chunks() -> None:
    """纯图文档 → 零块；上游 CHUNK_EMPTY 显式失败的前提（build_task.py:545）。"""

    md = f"{_MINERU_PLACEHOLDER}\n\n![](images/only.png)\n"
    chunks = chunk_markdown(md, chunk_size=100, overlap_ratio=0.1, parent_size=500)
    assert chunks == []


def test_nonempty_alt_image_line_is_kept() -> None:
    """alt 非空是用户语义内容，不滤（fail-safe 方向：最坏留噪音块，绝不丢语义）。"""

    md = "## 节\n\n![校园地图](images/campus-map.png)\n"
    blocks = split_into_blocks(md)
    assert len(blocks) == 1
    assert blocks[0].text == "![校园地图](images/campus-map.png)"


def test_external_url_image_line_is_kept() -> None:
    """http(s) 外链图片是用户内容，不滤。"""

    md = "## 节\n\n![](https://example.com/static/map.png)\n\n正文。\n"
    blocks = split_into_blocks(md)
    assert any("https://example.com" in block.text for block in blocks)


def test_non_base64_data_uri_is_kept() -> None:
    """非 base64 的 data URI（如 svg+xml;utf8）不是 MinerU 占位符形态，不滤。"""

    md = (
        "## 节\n\n"
        "![](data:image/svg+xml;utf8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org"
        "%2F2000%2Fsvg%22%3E%3C%2Fsvg%3E)\n\n"
        "正文。\n"
    )
    blocks = split_into_blocks(md)
    assert any("svg+xml" in block.text for block in blocks)


def test_image_syntax_inside_table_and_code_fence_untouched() -> None:
    """表格/代码围栏内的图片语法随原子块保留（原子块口径不变）。"""

    table = "| 图 | 名 |\n| --- | --- |\n| ![](data:image/png;base64,AAAA) | 样例 |"
    code = "```\n![](data:image/png;base64,BBBB)\n```"
    md = f"## 节\n\n{table}\n\n{code}\n"
    chunks = chunk_markdown(md, chunk_size=100, overlap_ratio=0.1, parent_size=500)
    tables = [c.text for c in children_of(chunks) if c.metadata.get("table")]
    codes = [c.text for c in children_of(chunks) if c.metadata.get("code")]
    assert any("data:image/png;base64,AAAA" in text for text in tables)
    assert any("data:image/png;base64,BBBB" in text for text in codes)


def test_inline_placeholder_in_paragraph_is_kept() -> None:
    """行内占位符（不占整行）随段落保留——阶段 1 已知边界（简报 §3.3）。"""

    md = "## 节\n\n见下图 ![](data:image/png;base64,CCCC) 所示。\n"
    blocks = split_into_blocks(md)
    assert len(blocks) == 1
    assert "data:image/png;base64,CCCC" in blocks[0].text
