"""C 端对话 SSE（M0 空壳 Loop）。

M0 只验证骨架：事件序列 `meta → step* → done`、`stop_reason` 记录、本地 trace 必发；
不调用 LLM、不检索、不生成消息内容（消息文本不入 trace，避免隐私外泄）。

事件字段与 `docs/design.md` §5.8 契约对齐（meta: turn_id/trace_id/conversation_id；
step: stage/label；done: tokens_in/tokens_out/first_token_ms/latency_ms/stop_reason；
error: code/message/retryable/partial）。

鉴权（M1-E1 起）：`campus.chat.use` + 租户 realm（学生/教师 C 端角色默认持有）；
trace 里的租户与用户身份取自认证主体，不再是占位值。

开关：`DEV_SKELETON_LOOP_ENABLED`（默认 false）；关闭时返回 404。生产环境由
`Settings` 校验直接拒绝开启该开关——空壳不产生回答，上线它没有业务意义。真实 Loop 在
M4 替换本实现（届时同步去掉该开关）。
"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Annotated, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.auth.principal import Principal, require_permission
from app.config import get_settings
from app.core.sse import SSE_HEADERS, SSE_MEDIA_TYPE, format_sse
from app.harness.contracts import AgentEventType, StopReason, TraceRecord, TraceSpan
from app.harness.trace import TraceRecorder, TraceSink

router = APIRouter(prefix="/campus", tags=["campus"])

ChatTurnDep = require_permission("campus.chat.use", realm="tenant")

# 空壳不调用模型：token/首 token 等计数恒为 0（不用字面量，避免被误判为硬编码口令）。
_SKELETON_COUNTER = 0

# 空壳步骤：stage 与 label 与 design.md §5.8 的示例口径一致（不展示内部细节）。
SKELETON_STAGES: tuple[tuple[str, str], ...] = (
    ("route", "正在理解问题"),
    ("answer", "正在组织回答"),
)


class ChatTurnRequest(BaseModel):
    """对话请求（M0 空壳不落库）。"""

    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, max_length=64)


def _parse_uuid(value: str | None) -> UUID | None:
    """把客户端传入的会话标识解析为 UUID；非 UUID（或缺失）时返回 None，由调用方另分配。"""

    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _trace_sink(request: Request) -> TraceSink:
    """从应用状态取出 trace sink（由 create_app 注入）。

    异常:
        RuntimeError: sink 缺失（装配错误）。显式失败优于把 trace 静默改道到临时内存。
    """

    sink = getattr(request.app.state, "trace_sink", None)
    if sink is None:
        raise RuntimeError("trace sink 未注入；请通过 create_app(trace_sink=...) 装配")
    return cast(TraceSink, sink)


@router.post("/chat/turns")
async def create_chat_turn(
    request: Request,
    payload: ChatTurnRequest,
    principal: Annotated[Principal, ChatTurnDep],
) -> StreamingResponse:
    """空壳回合：SSE 返回 meta/step/done，并保证每个已开始回合写入本地 trace。

    参数:
        request: FastAPI 请求（用于读取 app.state.trace_sink）。
        payload: 对话请求体。
        principal: 已鉴权的租户主体（`campus.chat.use`）。

    返回值:
        StreamingResponse: `text/event-stream`。

    异常:
        HTTPException: 开关关闭时 404；请求体非法时 422；租户身份缺失时 403。
    """

    settings = get_settings()
    if not settings.dev_skeleton_loop_enabled:
        raise HTTPException(status_code=404, detail="not found")

    # realm 已由 require_permission 断言为 tenant，因此 tenant_id 必然存在；
    # 这里显式收窄类型而不是 assert（bandit 禁止在 app/ 用 assert 当运行时守卫）。
    tenant_id = principal.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="缺少租户上下文")

    turn_id = uuid4()
    trace_id = uuid4().hex
    # 客户端传入的 conversation_id 若为合法 UUID 则沿用；否则服务端另分配一个，
    # meta 回显「解析后」的值，保证 meta 与本地 trace 的 conversation_id 可关联。
    conversation_uuid = _parse_uuid(payload.conversation_id) or uuid4()
    recorder = TraceRecorder(_trace_sink(request))

    def record_turn(spans: list[TraceSpan], *, latency_ms: int, error_code: str | None) -> None:
        """写入回合 trace（正常、失败与中断路径共用）。"""

        recorder.record_turn(
            TraceRecord(
                trace_id=trace_id,
                tenant_id=tenant_id,
                user_id=principal.user_id,
                conversation_id=conversation_uuid,
                turn_id=turn_id,
                intents=["skeleton"],
                route_path="fast",
                stop_reason=None if error_code else StopReason.FINAL_ANSWER,
                error_code=error_code,
                latency_ms=latency_ms,
                spans=spans,
            )
        )

    async def event_stream() -> AsyncIterator[str]:
        """按 meta → step* → done 顺序产出事件；中断/失败路径同样落 trace。"""

        started = time.perf_counter()
        spans: list[TraceSpan] = []
        try:
            yield format_sse(
                AgentEventType.META.value,
                {
                    "turn_id": str(turn_id),
                    "trace_id": trace_id,
                    "conversation_id": str(conversation_uuid),
                },
            )

            for stage, label in SKELETON_STAGES:
                stage_started_ms = int((time.perf_counter() - started) * 1000)
                yield format_sse(AgentEventType.STEP.value, {"stage": stage, "label": label})
                stage_started = time.perf_counter()
                # 空壳不做事；仅记录阶段耗时，保证数据真实反映处理过程（不伪造进度）。
                spans.append(
                    TraceSpan(
                        trace_id=trace_id,
                        tenant_id=tenant_id,
                        span_type="stage",
                        name=stage,
                        start_ms=stage_started_ms,
                        duration_ms=int((time.perf_counter() - stage_started) * 1000),
                    )
                )
        except (GeneratorExit, asyncio.CancelledError):
            # 客户端断开/任务取消：不能再 yield，但「每个已开始回合必发 trace」仍然成立。
            # TODO(TD-018)：sink 改为异步/独立连接后，此处的写操作需 asyncio.shield 或独立任务，
            # 否则会在已取消的上下文中被再次取消而丢 trace。
            record_turn(
                spans,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error_code="CLIENT_CANCELLED",
            )
            raise
        except Exception:
            # 生成过程失败：落失败 trace，并以 error 事件告知客户端可重试。
            record_turn(
                spans,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error_code="STREAM_ERROR",
            )
            yield format_sse(
                AgentEventType.ERROR.value,
                {
                    "code": "STREAM_ERROR",
                    "message": "回答生成失败，请重试",
                    "retryable": True,
                    "partial": False,
                },
            )
            return

        latency_ms = int((time.perf_counter() - started) * 1000)
        record_turn(spans, latency_ms=latency_ms, error_code=None)
        yield format_sse(
            AgentEventType.DONE.value,
            {
                "turn_id": str(turn_id),
                "trace_id": trace_id,
                "tokens_in": _SKELETON_COUNTER,
                "tokens_out": _SKELETON_COUNTER,
                # 契约字段（design.md §5.8）；空壳无生成，计数恒为 0。
                "first_token_ms": _SKELETON_COUNTER,
                "latency_ms": latency_ms,
                "stop_reason": StopReason.FINAL_ANSWER.value,
            },
        )

    return StreamingResponse(
        event_stream(),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
    )
