"""chat Loop 装配（M4-AI01b 步 5；use-site 构造对齐 trace_sink/llm_port 先例）。"""

from app.config import Settings
from app.harness.audit_db import DbAuditSink
from app.harness.llm import LlmPort
from app.harness.loop import FastPathLoop
from app.harness.prompt_loading import build_default_registry
from app.harness.trace import TraceSink
from app.kb.chat_retrieval import ChatRetriever
from app.kb.embedding import TEIEmbedder


def build_chat_loop(
    settings: Settings, *, trace_sink: TraceSink, llm_port: LlmPort | None
) -> FastPathLoop:
    """按 Settings 构建快路径 Loop（create_app 缺省装配入口）。

    - 检索器：TEI 查询向量化 + Qdrant fan-out（`kb_chat_top_k` 最终引用）；
    - prompt：Git 版本化模板注册表（缺文件启动即失败——ADR-008）；
    - 审计：DbAuditSink（append 缓冲 + 回合收尾 flush 落库）；
    - LLM：未配置（None）时对话走 `LLM_NOT_CONFIGURED` 兜底（AI-01a D6 口径）。
    """

    embedder = TEIEmbedder(
        base_url=settings.tei_embed_url,
        timeout_seconds=settings.tei_embed_timeout_seconds,
        batch_size=settings.kb_embed_batch_size,
    )
    retriever = ChatRetriever(
        embedder=embedder,
        qdrant_url=settings.qdrant_url,
        top_k=settings.kb_chat_top_k,
    )
    return FastPathLoop(
        llm_port=llm_port,
        retriever=retriever,
        prompt_registry=build_default_registry(),
        trace_sink=trace_sink,
        audit_sink=DbAuditSink(actor_realm="tenant"),
    )
