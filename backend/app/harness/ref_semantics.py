"""引用语义相关性校验（M4-AI04）：答案全文 vs 被引证据的嵌入余弦相似度。

spec AI-04「引用 chunk_id 必须真实存在、属于当前租户/版本、与答案相关」：
存在性/归属由 RETRIEVE 证据回查保证（租户+版本定域）、超范围 `[n]` 由哨兵
output.ref_range 剔除（TD-332）；本模块补最后一块——**语义相关性**（幻觉
引用防线：引用存在但内容与答案无关）。

时序（简报 §1.1）：流式 delta 不可撤回（TD-332 口径），citation 事件在流
结束后、DONE 之前发——全文校验在 citation 阶段执行；被剔除的引用从 citation
事件过滤 + 写 guard_events（A3），正文残留 `[n]` 标记为可接受降级。

fail-open（A4）：校验环节本身故障（embedder 异常）由**调用方**兜底——按
`parse_cited` 结果发 citation + warning 日志，质量检查不阻塞回合（M6 口径）；
本模块不吞异常，让调用方决定降级形态。
"""

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.harness.guards.rules import REF_MARKER
from app.kb.chat_retrieval import ChatEvidence

logger = logging.getLogger(__name__)

# 无效引用事件规则（guard_events/metrics 共用；direction="output"）
REF_IRRELEVANT_RULE = "output.ref_irrelevant"


@runtime_checkable
class EmbedPort(Protocol):
    """嵌入端口（TEIEmbedder 同签名；测试注入 FakeEmbedder）。"""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量嵌入：返回与 texts 等长的向量列表。"""
        ...


def parse_cited(answer_text: str, evidence_count: int) -> list[int]:
    """从答案正文解析被引序号（去重升序；超界防御性再滤——哨兵已先剔除）。"""

    cited = {
        int(match.group(1))
        for match in REF_MARKER.finditer(answer_text)
        if 1 <= int(match.group(1)) <= evidence_count
    }
    return sorted(cited)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（零向量 → 0.0——无方向即无相关性）。"""

    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass(frozen=True)
class RefVerdict:
    """单条被引证据的判定明细。"""

    index: int
    chunk_id: str
    similarity: float
    relevant: bool


@dataclass(frozen=True)
class RefCheckResult:
    """一次校验的结果（cited=正文引用序号；removed=被剔除明细）。"""

    cited: list[int] = field(default_factory=list)
    verdicts: list[RefVerdict] = field(default_factory=list)
    removed: list[RefVerdict] = field(default_factory=list)
    checked: bool = False


class RefSemanticChecker:
    """语义相关性判定器（D2：答案全文 vs 逐条被引证据；D5：阈值 Settings 化）。"""

    def __init__(self, *, embedder: EmbedPort, min_similarity: float) -> None:
        """绑定嵌入端口与阈值；阈值须在 [0, 1]（余弦值域）。"""

        if not 0.0 <= min_similarity <= 1.0:
            raise ValueError(f"min_similarity 必须在 [0, 1]：{min_similarity}")
        self._embedder = embedder
        self._min_similarity = min_similarity

    @property
    def min_similarity(self) -> float:
        """当前阈值（trace/事件记录用）。"""

        return self._min_similarity

    async def check(
        self, answer_text: str, evidence: Sequence[ChatEvidence],
    ) -> RefCheckResult:
        """校验答案中每个被引证据的语义相关性。

        流程（D3/D4）：解析被引序号 → 一次批量嵌入（答案 + 被引块文本）→
        逐条余弦 → `sim >= min_similarity` 判相关。零引用不触发嵌入
        （零延迟成本）。

        **nav: 豁免**（简报 §10 实现注记）：`chunk_id` 以 `nav:` 开头的业务入口
        证据是 ReAct 决策器**主动获取**的确定性证据（非检索相关性产物），且文本
        为 label:route 短标签——语义校验对它必然误杀；引用即视为相关（sim=1.0），
        不参与嵌入。
        """

        cited = parse_cited(answer_text, len(evidence))
        if not cited:
            return RefCheckResult(cited=[], verdicts=[], removed=[], checked=True)
        nav_indices = [
            i for i in cited if evidence[i - 1].chunk_id.startswith("nav:")
        ]
        checked_indices = [i for i in cited if i not in set(nav_indices)]
        verdicts: list[RefVerdict] = [
            RefVerdict(
                index=i, chunk_id=evidence[i - 1].chunk_id,
                similarity=1.0, relevant=True,
            )
            for i in nav_indices
        ]
        if checked_indices:
            texts = [answer_text, *(evidence[i - 1].text for i in checked_indices)]
            vectors = await self._embedder.embed(texts)
            answer_vec = vectors[0]
            for position, index in enumerate(checked_indices, start=1):
                similarity = cosine_similarity(answer_vec, vectors[position])
                verdicts.append(
                    RefVerdict(
                        index=index,
                        chunk_id=evidence[index - 1].chunk_id,
                        similarity=similarity,
                        relevant=similarity >= self._min_similarity,
                    )
                )
        verdicts.sort(key=lambda v: v.index)
        removed = [v for v in verdicts if not v.relevant]
        if removed:
            logger.info(
                "引用语义校验剔除 %d 条（阈值 %.2f）：%s",
                len(removed),
                self._min_similarity,
                ",".join(f"[{v.index}]sim={v.similarity:.2f}" for v in removed),
            )
        return RefCheckResult(
            cited=cited, verdicts=verdicts, removed=removed, checked=True
        )
