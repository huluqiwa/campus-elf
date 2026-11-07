"""档位降级编排（M3 KB-03A；`docs/design.md` §7.1「Standard → Basic → Flash」）。

语义（简报 D5）：按 `Settings.kb_parser_tiers()` 的顺序逐档尝试；当前档
`ParserError` → 记录原因并降下一档；**全部档位失败 → 末档错误上抛**（编排层
据此把 job 置 FAILED）。`fallback_reason` 只在非首选档成功时非空，形如
`standard: HTTP 502; basic: 网络错误`（链式摘要，供运营在快照里读）。
"""

from app.kb.parser.ports import (
    DocumentParserPort,
    ParserError,
    ParseTierOutcome,
)


class TieredParser:
    """跨档降级编排（一个实例绑定一条有序档位链与对应适配器）。"""

    def __init__(self, tiers: list[tuple[str, DocumentParserPort]]) -> None:
        """参数: tiers: `(档位名, 适配器)` 有序列表（至少 1 项）。"""

        if not tiers:
            raise ValueError("解析档位链不能为空")
        self._tiers = tiers

    @property
    def first_tier(self) -> str:
        """首选档位名（job.tier 的缺省展示值）。"""

        return self._tiers[0][0]

    async def parse(self, *, data: bytes, filename: str) -> ParseTierOutcome:
        """逐档解析；全部失败时抛**末档**错误（前几档原因拼入 message）。"""

        failures: list[str] = []
        last_error: ParserError | None = None
        for tier_name, parser in self._tiers:
            try:
                document = await parser.parse(data=data, filename=filename)
            except ParserError as exc:
                failures.append(f"{tier_name}: {exc.message}")
                last_error = exc
                continue
            fallback_reason = "; ".join(failures) if failures else None
            return ParseTierOutcome(
                document=document, tier=tier_name, fallback_reason=fallback_reason
            )
        if last_error is None:  # tiers 非空由 __init__ 保证；防御分支让 bandit/类型双满意
            raise ParserError("PARSER_CHAIN_EMPTY", "解析档位链为空")
        raise ParserError(
            last_error.code,
            f"全部解析档位失败（{'；'.join(failures)}）",
        ) from last_error
