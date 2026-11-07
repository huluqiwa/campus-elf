"""解析端口（M3 KB-03A）：业务层与引擎实现之间的最小契约。

`ParserError.code` 语义（编排层与 job.error_code 的取值域）：
- `MINERU_HTTP_ERROR`：HTTP 非 2xx（适配器抛）；
- `MINERU_PARSE_FAILED`：引擎返回 failed/partial 或文件级失败；
- `MINERU_EMPTY_OUTPUT`：引擎成功但 markdown 为空；
- `NORMALIZE_EMPTY_INPUT`：TXT/MD 规范化收到空输入；
- `PARSER_UNAVAILABLE`：该格式解析器未接入（TD-293，诚实失败）。
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """一次解析的统一输出（`docs/design.md` §7.1「统一输出 Markdown + 元数据」）。

    页码等细粒度元数据由切块器从 markdown 结构推导（KB-03A 不深挖 MinerU
    middle_json；页码在无显式标记时为 None，不伪造）。
    """

    markdown: str


@dataclass(frozen=True, slots=True)
class ParseTierOutcome:
    """带档位信息的解析结果（TieredParser 产出；tier 用于 job.tier 落库）。"""

    document: ParsedDocument
    tier: str | None
    """实际成功的档位；TXT/MD 规范化无档位概念 → None。"""
    fallback_reason: str | None
    """未用首选档时的链式原因摘要（如 `standard: HTTP 502`）；首选档成功 → None。"""


class ParserError(Exception):
    """解析失败（单档内；编排层据此降级或终止）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DocumentParserPort(Protocol):
    """单档解析端口：bytes → 规范化 markdown。"""

    async def parse(self, *, data: bytes, filename: str) -> ParsedDocument:
        """解析一份文档。

        参数:
            data: 源文件字节。
            filename: 原始文件名（引擎侧留痕/后缀判断）。

        异常:
            ParserError: 解析失败（调用方决定降级或终止）。
        """
        ...
