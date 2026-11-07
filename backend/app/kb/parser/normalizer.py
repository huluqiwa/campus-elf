"""TXT/MD 直接规范化（M3 KB-03A；`docs/design.md` §7.1「TXT/MD 直接 Markdown 规范化」）。

规范化是**轻量的确定性清理**，不做内容改写：CRLF → LF、去 BOM、去行尾空白、
合并 3+ 连续空行为 1 行、确保单一文件尾换行。TXT（无结构）与 MD 共用同一路径——
TXT 本就是纯文本， headings 缺失时切块器按单节处理（section_path=None）。
"""

import unicodedata

from app.kb.parser.ports import ParsedDocument, ParserError


class MarkdownNormalizer:
    """无引擎规范化（tier=None：它不经过降级链，也无需引擎可用性）。"""

    async def parse(self, *, data: bytes, filename: str) -> ParsedDocument:
        """把 TXT/MD 字节规范化为 markdown 文本。"""

        if not data:
            raise ParserError("NORMALIZE_EMPTY_INPUT", "解析输入为空")
        decoded = data.decode("utf-8", errors="strict")
        return ParsedDocument(markdown=normalize_markdown(decoded))


def normalize_markdown(raw: str) -> str:
    """确定性清理：Unicode NFC、CRLF→LF、去 BOM/行尾空白、合并空行、单一尾换行。"""

    text = raw.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    lines = [line.rstrip() for line in text.split("\n")]
    collapsed: list[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        collapsed.append(line)
    while collapsed and collapsed[0] == "":
        collapsed.pop(0)
    while collapsed and collapsed[-1] == "":
        collapsed.pop()
    return "\n".join(collapsed) + ("\n" if collapsed else "")
