"""按 MIME 分发解析器（M3 KB-03A 简报 D2：PDF/MD/TXT 先行；DOCX/PPTX → TD-293）。

- `application/pdf` → MinerU 降级链（tier 顺序来自 Settings）；
- `text/markdown` / `text/plain` → MarkdownNormalizer（无引擎、tier=None）；
- DOCX/PPT(X) → `ParserUnavailableError`（诚实失败：构建任务 FAILED
  `PARSER_UNAVAILABLE`，可重试；路径随 TD-293 的 LibreOffice 容器补齐）。
"""

from app.kb.parser.ports import DocumentParserPort, ParserError
from app.kb.parser.tiered import TieredParser

DOCX_PPTX_MIMES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-powerpoint",
        "application/msword",
    }
)


class ParserUnavailableError(ParserError):
    """该格式解析器未接入（TD-293）；构建任务按 FAILED 收口，可重试。"""

    def __init__(self) -> None:
        super().__init__(
            "PARSER_UNAVAILABLE",
            "该文档格式的解析器尚未接入（DOCX/PPTX 支持见 TD-293），构建已终止；可稍后重试",
        )


def select_parser(
    *, mime: str, mineru_chain: TieredParser, normalizer: DocumentParserPort
) -> tuple[DocumentParserPort | TieredParser, bool]:
    """按 MIME 返回 `(解析器, 是否带档位)`。

    第二个返回值告诉编排层：解析成功后 job.tier 应取 `TieredParser` 的实际档位
    （True），还是置 None（TXT/MD 无档位概念，False）。
    """

    normalized = mime.split(";")[0].strip().lower()
    if normalized == "application/pdf":
        return mineru_chain, True
    if normalized in ("text/markdown", "text/plain"):
        return normalizer, False
    if normalized in DOCX_PPTX_MIMES:
        raise ParserUnavailableError()
    # 上传白名单之外的类型理论上到不了这里；防御性诚实失败而不是猜。
    raise ParserUnavailableError()


