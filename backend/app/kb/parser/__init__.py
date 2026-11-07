"""文档解析端口与适配器（M3 KB-03A，`docs/design.md` §7.1）。

- `DocumentParserPort`：业务层只依赖该协议（worker 编排注入，CI 用 fake）；
- `MinerUParser`：MinerU 4.0.12 V1 `/v1/file_parse` 真实适配器（单档）；
- `TieredParser`：档位降级编排（standard→basic→flash，逐档尝试 + 链式 fallback_reason）；
- `MarkdownNormalizer`：TXT/MD 直接规范化（无外部引擎）；
- `select_parser`：按 MIME 分发（PDF → 降级链；TXT/MD → 规范化；
  DOCX/PPTX → 诚实失败 `PARSER_UNAVAILABLE`，TD-293）。

失败语义：任何一档失败都抛 `ParserError`（携带机器可读 code），由编排层决定
降级或失败；`MinerUParser` 只报错不重试（重试是编排层的降级链，不是同档重试）。
"""

from app.kb.parser.dispatch import ParserUnavailableError, select_parser
from app.kb.parser.mineru import MinerUParser
from app.kb.parser.normalizer import MarkdownNormalizer
from app.kb.parser.ports import DocumentParserPort, ParsedDocument, ParserError, ParseTierOutcome
from app.kb.parser.tiered import TieredParser

__all__ = [
    "DocumentParserPort",
    "MarkdownNormalizer",
    "MinerUParser",
    "ParseTierOutcome",
    "ParsedDocument",
    "ParserError",
    "ParserUnavailableError",
    "TieredParser",
    "select_parser",
]
