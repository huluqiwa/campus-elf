"""AI 反馈域服务（M4-AI05）：提交/匿名化/快照加密/B 端处理。

所有函数在 `db_session(tenant_id=...)` 上下文中执行（租户定域 + RLS）；
审计与业务写同事务提交（清空端点先例）。

加密（D2）：快照 AES-256-GCM，复用 `ENCRYPTION_KEK`，AAD=`ai_feedback:{tenant}:{feedback}`
与 MFA（mfa:）/身份证（idcard:）同 KEK 不同 AAD 域——跨域密文不可解（AAD 绑定）。
脱敏（卡 3）：comment 提交时过输出扫描 PII 规则（复用 guards 检测器，仅取 redact 后文本）。
"""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import tuple_

from app.audit.writer import append_audit
from app.db.models.ai import (
    AiFeedback,
    AiFeedbackReply,
    ChatTrace,
    Conversation,
    ConversationMessage,
)
from app.db.repository import ScopeBypass, TenantScopedRepository
from app.db.session import db_session
from app.harness.guards.output import scan_output
from app.security.encryption import decrypt_secret, encrypt_secret

RATING_VALUES = frozenset({"helpful", "unhelpful"})
REASON_TAGS = frozenset({"inaccurate", "irrelevant", "outdated", "harmful", "other"})
COMMENT_MAX_CHARS = 500
REPLY_MAX_CHARS = 1000

_STATUS_SUBMITTED = "submitted"
_STATUS_HANDLED = "handled"
_STATUS_ANONYMIZED = "anonymized"


class FeedbackError(ValueError):
    """反馈域领域异常（code 由端点层映射 HTTP 状态）。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def feedback_aad(tenant_id: UUID | str, feedback_id: UUID | str) -> bytes:
    """快照密文的附加认证数据（D2：绑定 tenant + feedback 行）。"""

    return f"ai_feedback:{tenant_id}:{feedback_id}".encode()


def redact_comment(comment: str) -> str:
    """comment 提交脱敏：过输出扫描，仅取 redact 后文本（卡 3）。

    blocking 类命中（自伤/注入等）不阻断反馈——反馈是用户表达通道，
    只做 PII 清除（身份证/手机号/邮箱/密钥/内网地址）。
    """

    scan = scan_output(comment, evidence_count=0)
    return scan.redacted_text or comment


def encrypt_snapshot(payload: dict[str, Any], *, kek: bytes, tenant_id: UUID, feedback_id: UUID) -> str:
    """快照 JSON → AES-256-GCM 密文（AAD 绑定 tenant+feedback）。"""

    return encrypt_secret(
        json.dumps(payload, ensure_ascii=False), kek=kek, aad=feedback_aad(tenant_id, feedback_id),
    )


def decrypt_snapshot(token: str, *, kek: bytes, tenant_id: UUID, feedback_id: UUID) -> dict[str, Any]:
    """快照密文 → JSON（AAD 不符 → SecretDecryptError → 上层 404/500）。"""

    payload: dict[str, Any] = json.loads(
        decrypt_secret(token, kek=kek, aad=feedback_aad(tenant_id, feedback_id))
    )
    return payload


def _validate_payload(rating: str, reason_tags: list[str], comment: str | None) -> str:
    """提交载荷校验（卡 5 枚举 + comment 上限）；返回脱敏后的 comment。"""

    if rating not in RATING_VALUES:
        raise FeedbackError("FEEDBACK_RATING_INVALID")
    unknown = set(reason_tags) - REASON_TAGS
    if unknown:
        raise FeedbackError("FEEDBACK_TAGS_INVALID")
    if comment is not None and len(comment) > COMMENT_MAX_CHARS:
        raise FeedbackError("FEEDBACK_COMMENT_TOO_LONG")
    return redact_comment(comment) if comment else ""


async def submit_feedback(
    *,
    tenant_id: UUID,
    user_id: UUID,
    conversation_id: UUID,
    turn_id: UUID,
    trace_id: str,
    rating: str,
    reason_tags: list[str],
    comment: str | None,
    kek: bytes,
) -> tuple[UUID, bool]:
    """提交反馈（A1：consent_at 服务端记录 + 三 id 归属校验 + 幂等重放）。

    返回 (feedback_id, replayed)；replayed=True 表示同 (conversation, turn) 已有
    反馈（幂等重放，返回既有行——KB 上传先例）。
    """

    clean_comment = _validate_payload(rating, reason_tags, comment)
    async with db_session(tenant_id=tenant_id) as session:
        # 幂等重放（卡 2：唯一约束 (tenant, conversation, turn)）
        repo = TenantScopedRepository(
            session, AiFeedback, tenant_id,
            scope=ScopeBypass(reason="AI 反馈：本人对自身会话回合的反馈"),
        )
        existing = (
            await session.execute(
                repo.base_query().where(
                    AiFeedback.conversation_id == conversation_id,
                    AiFeedback.turn_id == turn_id,
                )
            )
        ).scalars().first()
        if existing is not None:
            return existing.id, True

        # 归属校验（D5）：会话本人 + trace 存在且属于本会话本人（不满足 → 404 语义）
        conv_repo = TenantScopedRepository(
            session, Conversation, tenant_id,
            scope=ScopeBypass(reason="AI 反馈：归属校验（会话本人）"),
        )
        conv = await conv_repo.get(conversation_id)
        if conv is None or conv.user_id != user_id:
            raise FeedbackError("FEEDBACK_TARGET_NOT_FOUND")
        trace_repo = TenantScopedRepository(
            session, ChatTrace, tenant_id,
            scope=ScopeBypass(reason="AI 反馈：归属校验（回合 trace 属本人会话）"),
        )
        trace_row = (
            await session.execute(
                trace_repo.base_query().where(
                    ChatTrace.turn_id == turn_id,
                    ChatTrace.conversation_id == conversation_id,
                    ChatTrace.user_id == user_id,
                )
            )
        ).scalars().first()
        if trace_row is None:
            raise FeedbackError("FEEDBACK_TARGET_NOT_FOUND")

        # 快照（D1）：被反馈轮问/答/引用（trace_id 定位 conversation_messages）
        msg_repo = TenantScopedRepository(
            session, ConversationMessage, tenant_id,
            scope=ScopeBypass(reason="AI 反馈：读取本人会话被反馈轮消息构建快照"),
        )
        messages = (
            await session.execute(
                msg_repo.base_query()
                .where(
                    ConversationMessage.trace_id == trace_id,
                    ConversationMessage.conversation_id == conversation_id,
                    ConversationMessage.status != "deleted",
                )
                .order_by(ConversationMessage.created_at.asc())
            )
        ).scalars().all()
        if not messages:
            # trace_id 与会话消息对不上（客户端传错）→ 目标不存在语义（404）
            raise FeedbackError("FEEDBACK_TARGET_NOT_FOUND")
        question = next((m.content for m in messages if m.role == "user"), "")
        assistant_msg = next((m for m in messages if m.role == "assistant"), None)
        answer = assistant_msg.content if assistant_msg is not None else ""
        citations = list(assistant_msg.citations_json or []) if assistant_msg is not None else []

        feedback_id = uuid4()
        snapshot_enc = encrypt_snapshot(
            {"question": question, "answer": answer, "citations": citations},
            kek=kek, tenant_id=tenant_id, feedback_id=feedback_id,
        )
        feedback = AiFeedback(
            id=feedback_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            message_id=assistant_msg.id if assistant_msg is not None else None,
            trace_id=trace_id,
            rating=rating,
            reason_tags=list(reason_tags),
            comment=clean_comment or None,
            content_snapshot_enc=snapshot_enc,
            status=_STATUS_SUBMITTED,
        )
        session.add(feedback)
        await append_audit(
            session,
            action="campus.feedback.create",
            resource_type="ai_feedback",
            resource_id=str(feedback_id),
            actor_realm="tenant",
            actor_id=user_id,
            tenant_id=tenant_id,
            result="success",
            metadata={"rating": rating, "replayed": False},
        )
        await session.commit()
        return feedback.id, False


async def anonymize_feedback(
    *, tenant_id: UUID, user_id: UUID, feedback_id: UUID,
) -> None:
    """匿名化本人反馈（A6）：user_id 置空 + comment/快照清除 + status=anonymized。

    身份断链语义（第 1 轮实现注记，偏离简报原「已匿名化再删 → 200」）：匿名化
    后 user_id 已置空，**任何人（含原本人）再删一律 404**——保留「删除者名单」
    反而违背断链目的；幂等体现在效果层面（重复删除无可再清）。
    他人/不存在 → FEEDBACK_TARGET_NOT_FOUND（404）。
    """

    async with db_session(tenant_id=tenant_id) as session:
        repo = TenantScopedRepository(
            session, AiFeedback, tenant_id,
            scope=ScopeBypass(reason="AI 反馈：本人自助删除（匿名化）"),
        )
        feedback = await repo.get(feedback_id)
        if feedback is None or feedback.user_id != user_id:
            raise FeedbackError("FEEDBACK_TARGET_NOT_FOUND")
        if feedback.status == _STATUS_ANONYMIZED:
            raise FeedbackError("FEEDBACK_TARGET_NOT_FOUND")
        feedback.user_id = None
        feedback.comment = None
        feedback.content_snapshot_enc = None
        feedback.status = _STATUS_ANONYMIZED
        await append_audit(
            session,
            action="campus.feedback.delete",
            resource_type="ai_feedback",
            resource_id=str(feedback_id),
            actor_realm="tenant",
            actor_id=user_id,
            tenant_id=tenant_id,
            result="success",
            metadata={"anonymized": True},
        )
        await session.commit()


async def mark_handled(
    *, tenant_id: UUID, handler_id: UUID, feedback_id: UUID,
) -> bool:
    """标记已处理（A4）：submitted → handled 状态机；幂等（已处理返回 False）。"""

    async with db_session(tenant_id=tenant_id) as session:
        repo = TenantScopedRepository(
            session, AiFeedback, tenant_id,
            scope=ScopeBypass(reason="B 端反馈处理：校方授权管理员（school.feedback.update）"),
        )
        feedback = await repo.get(feedback_id)
        if feedback is None:
            raise FeedbackError("FEEDBACK_TARGET_NOT_FOUND")
        if feedback.status == _STATUS_HANDLED:
            return False
        if feedback.status == _STATUS_ANONYMIZED:
            raise FeedbackError("FEEDBACK_ANONYMIZED")
        feedback.status = _STATUS_HANDLED
        feedback.handled_by = handler_id
        feedback.handled_at = datetime.now(UTC)
        await append_audit(
            session,
            action="school.feedback.handled",
            resource_type="ai_feedback",
            resource_id=str(feedback_id),
            actor_realm="tenant",
            actor_id=handler_id,
            tenant_id=tenant_id,
            result="success",
            metadata={},
        )
        await session.commit()
        return True


async def reply_feedback(
    *, tenant_id: UUID, sender_id: UUID, feedback_id: UUID, content: str,
) -> UUID:
    """答复反馈（A5）：落 ai_feedback_replies + 审计；匿名化反馈不可答复（409 语义）。"""

    if not content or len(content) > REPLY_MAX_CHARS:
        raise FeedbackError("FEEDBACK_REPLY_INVALID")
    async with db_session(tenant_id=tenant_id) as session:
        repo = TenantScopedRepository(
            session, AiFeedback, tenant_id,
            scope=ScopeBypass(reason="B 端反馈答复：校方授权管理员（school.feedback.update）"),
        )
        feedback = await repo.get(feedback_id)
        if feedback is None:
            raise FeedbackError("FEEDBACK_TARGET_NOT_FOUND")
        if feedback.status == _STATUS_ANONYMIZED:
            raise FeedbackError("FEEDBACK_ANONYMIZED")
        reply = AiFeedbackReply(
            tenant_id=tenant_id,
            feedback_id=feedback_id,
            sender_id=sender_id,
            content=content,
        )
        session.add(reply)
        await append_audit(
            session,
            action="school.feedback.replied",
            resource_type="ai_feedback",
            resource_id=str(feedback_id),
            actor_realm="tenant",
            actor_id=sender_id,
            tenant_id=tenant_id,
            result="success",
            metadata={"reply_id": str(reply.id)},
        )
        await session.commit()
        return reply.id


async def get_feedback_detail(
    *, tenant_id: UUID, feedback_id: UUID, kek: bytes,
) -> dict[str, Any]:
    """B 端详情（A3）：rating/tags/status + 受控解密快照；匿名化反馈不返回快照/comment（A7）。"""

    async with db_session(tenant_id=tenant_id) as session:
        repo = TenantScopedRepository(
            session, AiFeedback, tenant_id,
            scope=ScopeBypass(reason="B 端反馈详情：校方授权管理员（school.feedback.view）"),
        )
        feedback = await repo.get(feedback_id)
        if feedback is None:
            raise FeedbackError("FEEDBACK_TARGET_NOT_FOUND")
        anonymized = feedback.status == _STATUS_ANONYMIZED
        snapshot: dict[str, Any] | None = None
        if not anonymized and feedback.content_snapshot_enc:
            snapshot = decrypt_snapshot(
                feedback.content_snapshot_enc, kek=kek, tenant_id=tenant_id, feedback_id=feedback.id,
            )
        return {
            "id": str(feedback.id),
            "conversation_id": str(feedback.conversation_id),
            "trace_id": feedback.trace_id,
            "rating": feedback.rating,
            "reason_tags": list(feedback.reason_tags),
            # 匿名化行 comment 必为 None（库层不变量），无需特判
            "comment": feedback.comment,
            "status": feedback.status,
            "handled_at": feedback.handled_at.isoformat() if feedback.handled_at else None,
            "anonymized": anonymized,
            "snapshot": snapshot,
        }


async def list_feedback(
    *, tenant_id: UUID, rating: str | None, limit: int, cursor: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """B 端列表（A3）：rating 可选筛选 + keyset 游标（created_at desc, id desc）。"""

    from app.db.repository import CursorError, decode_cursor

    async with db_session(tenant_id=tenant_id) as session:
        repo = TenantScopedRepository(
            session, AiFeedback, tenant_id,
            scope=ScopeBypass(reason="B 端反馈列表：校方授权管理员（school.feedback.view）"),
        )
        stmt = repo.base_query()
        if rating is not None:
            if rating not in RATING_VALUES:
                raise FeedbackError("FEEDBACK_RATING_INVALID")
            stmt = stmt.where(AiFeedback.rating == rating)
        if cursor:
            try:
                cursor_dt, cursor_id = decode_cursor(cursor)
            except CursorError as exc:
                raise FeedbackError("FEEDBACK_CURSOR_INVALID") from exc
            stmt = stmt.where(
                tuple_(AiFeedback.created_at, AiFeedback.id) < (cursor_dt, cursor_id),
            )
        stmt = stmt.order_by(AiFeedback.created_at.desc(), AiFeedback.id.desc()).limit(limit + 1)
        rows = (await session.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = (
        encode_feedback_cursor(items[-1].created_at, items[-1].id) if has_more and items else None
    )
    return [
        {
            "id": str(f.id),
            "conversation_id": str(f.conversation_id),
            "trace_id": f.trace_id,
            "rating": f.rating,
            "reason_tags": list(f.reason_tags),
            "status": f.status,
            "anonymized": f.status == _STATUS_ANONYMIZED,
            "created_at": f.created_at.isoformat(),
        }
        for f in items
    ], next_cursor


def encode_feedback_cursor(created_at: datetime, feedback_id: UUID) -> str:
    """列表游标（复用仓储 codec 的编解码对称封装）。"""

    from app.db.repository import encode_cursor

    return encode_cursor(created_at, feedback_id)

