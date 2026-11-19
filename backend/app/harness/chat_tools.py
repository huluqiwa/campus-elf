"""chat 三只读工具注册（M4-AI01b 步 2）：`kb.search` / `kb.read_chunk` / `campus.navigate`。

权限（简报 D2）：MVP 三工具挂既有码——`kb.search`/`kb.read_chunk` 挂
`campus.chat.use`（端点已强制，工具内重复断言同码等价）、`campus.navigate`
挂 `campus.nav`（目录已有、C 端持有）；`campus.kb.qa` 独立码延后 ReAct 点随
权限目录扩展（design §6.3 回写注记随实现 PR）。

**`kb.read_chunk` 证据白名单**：只允许读「本轮 RETRIEVE 命中且文本回查成功」
的块（`TurnToolState.evidence_by_id`，由 loop 在检索后填充）——否则凭块 id 可
越过人群过滤读任意块（ACL 旁路）。文本直接取自回合证据映射，不做二次 PG 读。

`kb.search` 工具面不含证据文本（卡 3-A：文本由编排器直调
`ChatRetriever.load_texts` 回查，不进工具/审计面）；原始命中（payload 内嵌）
只在 loop 内部流转，SSE 只外发计数。

`campus.navigate`：静态入口映射（route/label），MVP 内置于代码（不含敏感
信息）；未知目标结构化拒绝。
"""

from typing import Any

from app.harness.contracts import TenantContext, ToolCall, ToolResult, ToolSpec
from app.harness.registry import ToolHandler, ToolRegistry
from app.kb.chat_retrieval import ChatEvidence, ChatRetriever

KB_SEARCH = "kb.search"
KB_READ_CHUNK = "kb.read_chunk"
CAMPUS_NAVIGATE = "campus.navigate"

# 静态入口映射（AI-08 能力兜底的入口语义随后续点扩展；route 为前端路由）
_NAV_ENTRIES: dict[str, dict[str, str]] = {
    "请假": {"route": "/leave", "label": "请假申请"},
    "课表": {"route": "/timetable", "label": "我的课表"},
    "成绩": {"route": "/scores", "label": "成绩查询"},
}


def _kb_search_spec() -> ToolSpec:
    """kb.search 工具契约。"""

    return ToolSpec(
        name=KB_SEARCH,
        version="1",
        description="混合检索学校知识库（按人群 token 过滤；只读）",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 2000}},
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"hits": {"type": "array"}},
            "required": ["hits"],
            "additionalProperties": False,
        },
        side_effect=False,
        permission="campus.chat.use",
        data_scope="none",
        timeout_ms=5000,
        idempotent=True,
        audit_level="basic",
    )


def _kb_read_chunk_spec() -> ToolSpec:
    """kb.read_chunk 工具契约。"""

    return ToolSpec(
        name=KB_READ_CHUNK,
        version="1",
        description="读取本轮证据集内的分块文本（只读；越集拒绝）",
        input_schema={
            "type": "object",
            "properties": {"chunk_id": {"type": "string", "minLength": 1}},
            "required": ["chunk_id"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string"},
                "text": {"type": "string"},
                "version_id": {"type": "string"},
            },
            "required": ["chunk_id", "text", "version_id"],
            "additionalProperties": False,
        },
        side_effect=False,
        permission="campus.chat.use",
        data_scope="none",
        timeout_ms=2000,
        idempotent=True,
        audit_level="basic",
    )


def _navigate_spec() -> ToolSpec:
    """campus.navigate 工具契约。"""

    return ToolSpec(
        name=CAMPUS_NAVIGATE,
        version="1",
        description="返回业务入口的前端路由（只读）",
        input_schema={
            "type": "object",
            "properties": {"target": {"type": "string", "minLength": 1, "maxLength": 64}},
            "required": ["target"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"route": {"type": "string"}, "label": {"type": "string"}},
            "required": ["route", "label"],
            "additionalProperties": False,
        },
        side_effect=False,
        permission="campus.nav",
        data_scope="none",
        timeout_ms=1000,
        idempotent=True,
        audit_level="basic",
    )


class TurnToolState:
    """单回合工具状态：kb.read_chunk 的证据映射（RETRIEVE 后由 loop 填充）。

    白名单语义 = 本轮命中**且**文本回查成功的块——索引在、库行不在的块不进
    白名单（不存在幽灵引用）。
    """

    def __init__(self) -> None:
        self.evidence_by_id: dict[str, ChatEvidence] = {}

    def set_evidence(self, evidence: list[ChatEvidence]) -> None:
        """RETRIEVE 成功后由 loop 调用（覆盖式填充）。"""

        self.evidence_by_id = {item.chunk_id: item for item in evidence}


def build_turn_tools(*, retriever: ChatRetriever, state: TurnToolState) -> ToolRegistry:
    """构建回合工具注册表（每回合一个——kb.read_chunk 证据映射随回合走）。"""

    registry = ToolRegistry()
    registry.register(_kb_search_spec(), _kb_search_handler(retriever))
    registry.register(_kb_read_chunk_spec(), _kb_read_chunk_handler(state))
    registry.register(_navigate_spec(), _navigate_handler())
    return registry


def _kb_search_handler(retriever: ChatRetriever) -> ToolHandler:
    """kb.search：检索命中元数据（无文本）；基础设施异常 → 结构化错误码透传。"""

    from app.kb.chat_retrieval import ChatRetrievalError

    async def handler(call: ToolCall, ctx: TenantContext) -> ToolResult:
        query = str(call.args["query"])
        try:
            hits = await retriever.search_hits(
                tenant_id=ctx.tenant_id, user_id=ctx.user_id, query=query
            )
        except ChatRetrievalError as exc:
            return ToolResult(ok=False, error_code=exc.code)
        # 原始命中（payload 内嵌）只在本边界内部流转：loop 用它做证据回查；
        # SSE tool_result 只出计数（loop 层），payload 不外发。
        return ToolResult(
            ok=True,
            data={"hits": list(hits)},
            redacted_summary=f"检索到 {len(hits)} 条候选",
        )

    return handler


def _kb_read_chunk_handler(state: TurnToolState) -> ToolHandler:
    """kb.read_chunk：证据映射内直取文本；越集结构化拒绝（不泄露存在性）。"""

    async def handler(call: ToolCall, ctx: TenantContext) -> ToolResult:
        raw = str(call.args["chunk_id"])
        evidence = state.evidence_by_id.get(raw)
        if evidence is None:
            return ToolResult(ok=False, error_code="CHUNK_NOT_IN_EVIDENCE")
        return ToolResult(
            ok=True,
            data={
                "chunk_id": evidence.chunk_id,
                "text": evidence.text,
                "version_id": evidence.version_id,
            },
            redacted_summary="读取证据 1 条",
        )

    return handler


def _navigate_handler() -> ToolHandler:
    """campus.navigate：静态入口映射；未知目标结构化拒绝。"""

    async def handler(call: ToolCall, ctx: TenantContext) -> ToolResult:
        target = str(call.args["target"]).strip()
        entry = _NAV_ENTRIES.get(target)
        if entry is None:
            return ToolResult(ok=False, error_code="NAVIGATE_TARGET_UNKNOWN")
        return ToolResult(ok=True, data=dict(entry), redacted_summary=f"入口：{target}")

    return handler


def openai_tool_specs(registry: ToolRegistry) -> tuple[dict[str, Any], ...]:
    """ToolRegistry 转 OpenAI ``tools`` 参数形态（ReAct THINK 决策步透传）。

    顺序为注册表排序序（稳定）；input_schema 直接作为 parameters（JSON Schema）。
    """

    return tuple(
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_schema,
            },
        }
        for spec in registry.all()
    )
