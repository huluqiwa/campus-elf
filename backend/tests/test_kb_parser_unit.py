"""解析层单测（M3 KB-03A）：降级链编排 + 规范化纯函数（fake adapters，无网络）。"""

import asyncio

import pytest

from app.kb.parser import (
    MarkdownNormalizer,
    MinerUParser,
    ParsedDocument,
    ParserError,
    TieredParser,
)
from app.kb.parser.dispatch import ParserUnavailableError, select_parser


class FakeParser:
    """脚本化单档适配器：`outcome` 为 None 时抛 ParserError。"""

    def __init__(self, outcome: ParsedDocument | None, error: ParserError | None = None) -> None:
        self.outcome = outcome
        self.error = error
        self.calls = 0

    async def parse(self, *, data: bytes, filename: str) -> ParsedDocument:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.outcome is not None
        return self.outcome


def test_tiered_parser_first_tier_success_has_no_fallback() -> None:
    chain = TieredParser(
        [
            ("standard", FakeParser(ParsedDocument(markdown="# ok"))),
            ("basic", FakeParser(None, ParserError("MINERU_HTTP_ERROR", "不该被调"))),
        ]
    )
    outcome = asyncio.run(chain.parse(data=b"x", filename="a.pdf"))
    assert outcome.tier == "standard"
    assert outcome.fallback_reason is None


def test_tiered_parser_degrades_with_chained_reason() -> None:
    chain = TieredParser(
        [
            ("standard", FakeParser(None, ParserError("MINERU_HTTP_ERROR", "HTTP 502"))),
            ("basic", FakeParser(None, ParserError("MINERU_PARSE_FAILED", "任务失败"))),
            ("flash", FakeParser(ParsedDocument(markdown="# flash ok"))),
        ]
    )
    outcome = asyncio.run(chain.parse(data=b"x", filename="a.pdf"))
    assert outcome.tier == "flash"
    assert outcome.fallback_reason == "standard: HTTP 502; basic: 任务失败"


def test_tiered_parser_all_tiers_fail_raises_last_error() -> None:
    chain = TieredParser(
        [
            ("standard", FakeParser(None, ParserError("MINERU_HTTP_ERROR", "e1"))),
            ("basic", FakeParser(None, ParserError("MINERU_PARSE_FAILED", "e2"))),
        ]
    )
    with pytest.raises(ParserError) as caught:
        asyncio.run(chain.parse(data=b"x", filename="a.pdf"))
    assert "e1" in str(caught.value) and "e2" in str(caught.value)


def test_tiered_parser_rejects_empty_chain() -> None:
    with pytest.raises(ValueError):
        TieredParser([])


def test_dispatch_pdf_goes_to_chain() -> None:
    chain = TieredParser([("standard", FakeParser(ParsedDocument(markdown="x")))])
    parser, tiered = select_parser(
        mime="application/pdf", mineru_chain=chain, normalizer=MarkdownNormalizer()
    )
    assert tiered is True
    assert parser is chain


@pytest.mark.parametrize("mime", ["text/markdown", "text/plain"])
def test_dispatch_text_goes_to_normalizer(mime: str) -> None:
    chain = TieredParser([("standard", FakeParser(ParsedDocument(markdown="x")))])
    parser, tiered = select_parser(
        mime=mime, mineru_chain=chain, normalizer=MarkdownNormalizer()
    )
    assert tiered is False
    assert isinstance(parser, MarkdownNormalizer)


@pytest.mark.parametrize(
    "mime",
    [
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-powerpoint",
    ],
)
def test_dispatch_docx_pptx_is_honest_failure(mime: str) -> None:
    """TD-293：DOCX/PPTX 本点诚实失败（PARSER_UNAVAILABLE），不静默吞。"""

    chain = TieredParser([("standard", FakeParser(ParsedDocument(markdown="x")))])
    with pytest.raises(ParserUnavailableError) as caught:
        select_parser(mime=mime, mineru_chain=chain, normalizer=MarkdownNormalizer())
    assert caught.value.code == "PARSER_UNAVAILABLE"


def test_normalizer_direct_call_matches_pipeline_branch() -> None:
    """_run_pipeline 的 non-tiered 分支：直接调 normalizer（tier=None）。"""

    outcome_document = asyncio.run(
        MarkdownNormalizer().parse(data="正文".encode(), filename="a.txt")
    )
    assert outcome_document.markdown.strip() == "正文"


def test_mineru_adapter_rejects_empty_input() -> None:
    import httpx

    adapter = MinerUParser(
        tier="standard",
        base_url="http://127.0.0.1:1",
        timeout_seconds=1,
        client=httpx.AsyncClient(),
    )
    import asyncio

    async def run() -> None:
        with pytest.raises(ParserError) as caught:
            await adapter.parse(data=b"", filename="a.pdf")
        assert caught.value.code == "MINERU_EMPTY_INPUT"

    asyncio.run(run())
