"""Markdown 切块器（M3 KB-03A；`docs/design.md` §7.2）。

**纯函数**（不碰库/网络），参数全部显式传入（`Settings` 可配，TD-294 临时默认
500/15%/1000）。结构约定：

- 按 markdown 标题（#..######）切节，`section_path` = 「标题 > 子标题」路径（≤3 层）；
- 表格（连续 `|` 行）与代码围栏（``` 块）**整体不拆**（§7.2 硬规则）——超过
  `chunk_size` 也保持整块（这是设计语义，不是 bug）；
- 整行空图片占位符（MinerU 的 `![](data:image/…;base64,…)` 或 dangling 相对路径引用；
  TD-310 阶段 1）**不进切块**，按空行段界处理——外链/非空 alt 的图片行是用户内容，保留；
- 普通段落按 `chunk_size`（码点）累积切子块，相邻子块携带 `overlap_ratio` 的重叠
  字符（按码点截取，TD-259 同口径）；
- 父块 = 同一节内连续子块的聚合（累计 ≤ `parent_size`；表格/代码子块独立成组），
  父块文本为子块文本按序拼接；
- 输出顺序 = 文档序：每组先父块（`chunk_type=parent`）后子块（`chunk_type=child`，
  带 `parent_id` 占位——id 由调用方分配后回填）。

`token_count` 用 jieba 分词数（真实分词，不是估算常数；TD-041 的 SyntaxWarning
在测试侧按类别过滤，见 `pyproject.toml` 注释）。
"""

import hashlib
import re
from dataclasses import dataclass, field

import jieba

CHUNK_TYPE_PARENT = "parent"
CHUNK_TYPE_CHILD = "child"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_CODE_FENCE_RE = re.compile(r"^\s*```")

# TD-310 阶段 1：整行空图片占位符。MinerU 4.0.12 实证（2026-11-12 真实栈 standard 档，
# 见 docs/design-briefs/M3-TD310-phase1-image-placeholder.md §0.2）：图片引用为
# `![](data:image/jpeg;base64,<payload>)`，alt 恒空、单行、前后空行分隔——现状会按普通
# 段落进块（base64 整段成为块文本，分词/嵌入/存储全是垃圾）。相对路径形态
# （`![](images/x.jpg)`）保留兼容。http(s) 外链、非空 alt、非 base64 的 data URI 是
# 用户内容，不属占位符（fail-safe 方向：最坏留噪音块，绝不丢语义）。
# base64 字符集不含 `)`，括号闭合判定安全；payload 不允许空白（MinerU 单行输出，
# 若未来出现换行包裹会漏滤——漏滤方向安全）。
_EMPTY_IMAGE_LINE_RE = re.compile(
    r"^\s*!\[\s*\]\(\s*"
    r"(?:data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+"  # MinerU 实证形态
    r"|[A-Za-z0-9_@%./+-]+\.[A-Za-z0-9]+"  # 相对路径（ASCII + 扩展名）
    r")\s*\)\s*$"
)

# jieba 分词器线程不安全且首次加载慢：进程内只初始化一次（worker 生命周期复用）。
_jieba_ready = False


def _ensure_jieba() -> None:
    global _jieba_ready
    if not _jieba_ready:
        jieba.initialize()
        _jieba_ready = True


@dataclass(frozen=True, slots=True)
class RawChunk:
    """切块器的输出（id/parent_id 由调用方分配；`chunk_index` 按输出序）。"""

    chunk_type: str
    text: str
    section_path: str | None
    page_start: int | None = None
    page_end: int | None = None
    overlap_prev: int | None = None
    overlap_next: int | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def token_count(self) -> int:
        _ensure_jieba()
        return len(jieba.lcut(self.text))

    @property
    def chunk_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Block:
    """节内的原子内容块（段落/表格/代码）。"""

    text: str
    kind: str  # paragraph / table / code
    section_path: str | None

    @property
    def is_atomic(self) -> bool:
        """表格/代码不拆（无论长度）。"""

        return self.kind in ("table", "code")


def split_into_blocks(markdown: str) -> list[Block]:
    """把 markdown 切成原子块：标题定 section，段落/表格/代码为块。"""

    blocks: list[Block] = []
    section_path: str | None = None
    heading_stack: list[str] = []
    paragraph: list[str] = []
    table_lines: list[str] = []
    code_lines: list[str] | None = None

    def flush_paragraph() -> None:
        if paragraph:
            # 段内行以 \n 连接；空行标记（""）形成段界，3+ 连续换行收缩为段间 \n\n
            text = "\n".join(paragraph)
            text = re.sub(r"\n{3,}", "\n\n", text).strip("\n")
            if text.strip():
                blocks.append(Block(text=text, kind="paragraph", section_path=section_path))
            paragraph.clear()

    def flush_table() -> None:
        if table_lines:
            text = "\n".join(table_lines)
            if text.strip():
                blocks.append(Block(text=text, kind="table", section_path=section_path))
            table_lines.clear()

    def flush_code() -> None:
        nonlocal code_lines
        if code_lines is not None:
            text = "\n".join(code_lines)
            if text.strip():
                blocks.append(Block(text=text, kind="code", section_path=section_path))
            code_lines = None

    for raw_line in markdown.split("\n"):
        line = raw_line.rstrip()
        # 代码围栏优先：围栏内的 # 与 | 都不是结构
        if code_lines is not None:
            code_lines.append(raw_line)
            if _CODE_FENCE_RE.match(line) and len(code_lines) > 1:
                flush_code()
            continue
        if _CODE_FENCE_RE.match(line):
            flush_paragraph()
            flush_table()
            code_lines = [line]
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            flush_table()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            heading_stack = heading_stack[: level - 1]
            heading_stack.append(title)
            # 路径最多 3 层（design §5.7 的 section_path 形态）
            section_path = " > ".join(heading_stack[:3]) or None
            continue
        if _TABLE_LINE_RE.match(line):
            flush_paragraph()
            table_lines.append(line)
            continue
        flush_table()
        if _EMPTY_IMAGE_LINE_RE.match(line):
            # TD-310 阶段 1：空图片占位符不进切块——按「空行（段界）」处理：占位符无
            # 检索语义，丢弃文本但保留前后段落边界，防止跨图内容合并成同一段。
            if paragraph and paragraph[-1] != "":
                paragraph.append("")
            continue
        if line.strip() == "":
            # 空行是段界标记：同节连续段落合并进同一个 Block（由切块窗口统一处理），
            # 避免小段落各自成块导致父块/重叠大量复制
            if paragraph and paragraph[-1] != "":
                paragraph.append("")
            continue
        paragraph.append(line)
    flush_paragraph()
    flush_table()
    flush_code()
    return blocks


def _overlap_slice(text: str, length: int) -> str:
    """取文本末尾 `length` 个码点作为重叠（空串/超长安全）。"""

    if length <= 0 or not text:
        return ""
    return text[-length:]


_SENTENCE_END_RE = re.compile(r"(?<=[。！？!?\.])")


def _split_long_segment(segment: str, window: int) -> list[str]:
    """把超长单段切成 ≤ window 码点的片段：句界优先，无句界处硬切（绝不丢字符）。"""

    if len(segment) <= window:
        return [segment]
    pieces: list[str] = []
    cursor = 0
    n = len(segment)
    while cursor < n:
        if n - cursor <= window:
            pieces.append(segment[cursor:])
            break
        hard_end = cursor + window
        # 在窗口内从后往前找最后一个句界（句号后）；找不到就硬切
        window_text = segment[cursor:hard_end]
        sentence_ends = [m.end() for m in _SENTENCE_END_RE.finditer(window_text)]
        cut = sentence_ends[-1] if sentence_ends else window
        pieces.append(segment[cursor : cursor + cut])
        cursor += cut
    return [piece for piece in pieces if piece]


def chunk_children(blocks: list[Block], chunk_size: int, overlap_chars: int) -> list[RawChunk]:
    """把原子块切成子块：段落按 size 累积 + 相邻重叠；表格/代码整块独立。

    重叠语义：后一块的开头携带前一块**末尾** `overlap_chars` 个码点（同节内；
    换节/原子块后不跨块重叠，`overlap_prev` 记录实际带入的字符数，无重叠为 None）。
    单段超过 `chunk_size` 时独立成块（不静默丢内容，也不无限切分）。
    """

    children: list[RawChunk] = []
    previous_tail = ""
    last_section: str | None = None
    for block in blocks:
        if block.section_path != last_section:
            # 换节不跨块重叠（docstring 承诺的语义；否则新节首块的嵌入前缀会带旧节文本）
            previous_tail = ""
            last_section = block.section_path
        if block.is_atomic:
            # 表格/代码是逐字引用的整块，不携带段落重叠（重叠语义只作用于连续散文）
            children.append(
                RawChunk(
                    chunk_type=CHUNK_TYPE_CHILD,
                    text=block.text,
                    section_path=block.section_path,
                    overlap_prev=None,
                    metadata={"table": block.kind == "table", "code": block.kind == "code"},
                )
            )
            previous_tail = ""
            continue
        segments = [seg.strip() for seg in block.text.split("\n\n") if seg.strip()]
        # 单段超长（一行无结构的长文）先按句界/码点切成窗口片段，再走统一累积逻辑
        window = max(chunk_size - overlap_chars, 1)
        segments = [
            piece for segment in segments for piece in _split_long_segment(segment, window)
        ]
        current = previous_tail
        current_overlap = len(current) if current else 0
        for segment in segments:
            candidate = f"{current}\n\n{segment}" if current else segment
            if current and len(candidate) > chunk_size:
                children.append(
                    RawChunk(
                        chunk_type=CHUNK_TYPE_CHILD,
                        text=current,
                        section_path=block.section_path,
                        overlap_prev=current_overlap if current_overlap else None,
                        metadata={"table": False, "code": False},
                    )
                )
                current = _overlap_slice(current, overlap_chars)
                current_overlap = len(current)
                candidate = f"{current}\n\n{segment}" if current else segment
            current = candidate
        if current.strip():
            children.append(
                RawChunk(
                    chunk_type=CHUNK_TYPE_CHILD,
                    text=current,
                    section_path=block.section_path,
                    overlap_prev=current_overlap if current_overlap else None,
                    metadata={"table": False, "code": False},
                )
            )
            previous_tail = _overlap_slice(current, overlap_chars)
        else:
            previous_tail = ""
    return children


def build_parents(
    children: list[RawChunk], parent_size: int
) -> list[tuple[RawChunk, list[RawChunk]]]:
    """把子块聚合成父子组：同节连续子块累计 ≤ parent_size 成一组（原子块独立成组）。

    返回有序 `(父块, 子块列表)`；父块文本 = 子块文本按序拼接。
    """

    groups: list[tuple[RawChunk, list[RawChunk]]] = []
    buffer: list[RawChunk] = []
    buffer_section: str | None = None

    def flush() -> None:
        nonlocal buffer, buffer_section
        if not buffer:
            return
        parent_text = "\n\n".join(child.text for child in buffer)
        groups.append(
            (
                RawChunk(
                    chunk_type=CHUNK_TYPE_PARENT,
                    text=parent_text,
                    section_path=buffer_section,
                    metadata={"table": False, "code": False},
                ),
                list(buffer),
            )
        )
        buffer = []
        buffer_section = None

    for child in children:
        is_atomic = bool(child.metadata.get("table") or child.metadata.get("code"))
        if is_atomic:
            flush()
            groups.append(
                (
                    RawChunk(
                        chunk_type=CHUNK_TYPE_PARENT,
                        text=child.text,
                        section_path=child.section_path,
                        metadata=dict(child.metadata),
                    ),
                    [child],
                )
            )
            continue
        section = child.section_path
        projected = sum(len(item.text) for item in buffer) + len(child.text)
        if buffer and (section != buffer_section or projected > parent_size):
            flush()
        if not buffer:
            buffer_section = section
        buffer.append(child)
    flush()
    return groups


def chunk_markdown(
    markdown: str, *, chunk_size: int, overlap_ratio: float, parent_size: int
) -> list[RawChunk]:
    """端到端纯函数：markdown → 有序块列表（每组父块在前，随后其子块）。

    `chunk_index` 不在这里分配（调用方按输出序回填）；`overlap_next` 由调用方
    根据相邻子块关系回填（本函数只标 `overlap_prev`）。
    """

    if chunk_size <= 0 or parent_size <= 0:
        raise ValueError("chunk_size / parent_size 必须为正")
    if not 0.0 <= overlap_ratio < 1.0:
        raise ValueError("overlap_ratio 必须在 [0, 1)")
    overlap_chars = int(chunk_size * overlap_ratio)
    blocks = split_into_blocks(markdown)
    children = chunk_children(blocks, chunk_size, overlap_chars)
    groups = build_parents(children, parent_size)
    ordered: list[RawChunk] = []
    for parent, kids in groups:
        ordered.append(parent)
        ordered.extend(kids)
    return ordered
