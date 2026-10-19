"""审计事件与哈希链。

M0-C 为内存实现：HMAC + 序号 + event_id + 链头/计数，便于测试与后续落库。
生产必须使用 PostgreSQL 仅追加表 + 外部锚点/WORM；内存实现不承担生产审计。
"""

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "id_card",
    "idcard",
    "phone",
    "mobile",
    "email",
    "address",
    "cookie",
    "chat",
    "content",
    "prompt",
    "answer",
    "completion",
    "credential",
    "session",
    "otp",
    "verify_code",
    "student_no",
    "employee_no",
)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d(?:\s?\d{4}){2}(?!\d)")
_ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_ID_CARD_15_RE = re.compile(r"(?<!\d)\d{15}(?!\d)")
_BEARER_RE = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._-]+")


def _redact_string(value: str) -> str:
    """对字符串值做模式脱敏。"""

    value = _BEARER_RE.sub("Bearer ***", value)
    value = _EMAIL_RE.sub("***", value)
    value = _ID_CARD_RE.sub("***", value)
    value = _ID_CARD_15_RE.sub("***", value)
    return _MOBILE_RE.sub("***", value)


def redact_mapping(value: Any) -> Any:
    """递归脱敏敏感 key 与字符串值。

    参数:
        value: dict/list/tuple/标量。

    返回值:
        脱敏后的同结构数据。

    异常:
        无。
    """

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = key.lower()
            if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
                result[key] = "***"
            else:
                result[key] = redact_mapping(item)
        return result
    if isinstance(value, list):
        return [redact_mapping(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_mapping(item) for item in value)
    if isinstance(value, str):
        return _redact_string(value)
    return value


class AuditEvent(BaseModel):
    """一条审计事件。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    actor_id: UUID
    action: str = Field(min_length=1)
    resource_type: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    result: str = Field(min_length=1)
    trace_id: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_id: UUID = Field(default_factory=uuid4)
    sequence: int = Field(default=0, ge=0)
    prev_hash: str = ""
    hash: str = ""

    @field_validator("created_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        """要求 created_at 带时区，避免 naive datetime 造成排序/哈希歧义。"""

        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return value


class AuditSink(Protocol):
    """审计写入端口。"""

    def append(self, event: AuditEvent) -> AuditEvent:
        """追加事件并返回带哈希的存储副本。"""


class InMemoryAuditSink:
    """内存 HMAC 哈希链审计 sink（仅开发/测试）。"""

    def __init__(self, secret_key: str) -> None:
        """初始化空链；secret_key 至少 16 字符。"""

        if len(secret_key) < 16:
            raise ValueError("audit secret_key must be at least 16 characters")
        self._secret_key = secret_key.encode("utf-8")
        self._events: list[AuditEvent] = []

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        """返回已追加事件的深拷贝。"""

        return tuple(event.model_copy(deep=True) for event in self._events)

    @property
    def head_hash(self) -> str:
        """返回链头哈希，供外部锚点/WORM 记录。"""

        return self._events[-1].hash if self._events else ""

    @property
    def count(self) -> int:
        """返回已追加事件数。"""

        return len(self._events)

    def _canonical(self, event: AuditEvent) -> str:
        """生成稳定 canonical JSON（不含 hash 字段）。"""

        payload = event.model_dump(mode="json", exclude={"prev_hash", "hash"})
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _digest(self, prev_hash: str, canonical: str) -> str:
        """计算 HMAC-SHA256 链哈希。"""

        return hmac.new(
            self._secret_key,
            (prev_hash + canonical).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def append(self, event: AuditEvent) -> AuditEvent:
        """追加事件，服务端设置时间/序号并计算 HMAC。"""

        if any(item.event_id == event.event_id for item in self._events):
            raise ValueError("duplicate audit event_id")
        sequence = len(self._events) + 1
        base = event.model_copy(
            update={
                "created_at": datetime.now(UTC),
                "sequence": sequence,
            }
        )
        payload = base.model_copy(
            update={"metadata": redact_mapping(base.metadata)}
        )
        canonical = self._canonical(payload)
        prev_hash = self.head_hash
        digest = self._digest(prev_hash, canonical)
        stored = payload.model_copy(
            update={"prev_hash": prev_hash, "hash": digest}
        )
        self._events.append(stored)
        return stored.model_copy(deep=True)

    def verify(
        self,
        *,
        expected_head: str | None = None,
        expected_count: int | None = None,
    ) -> bool:
        """校验序号、event_id 唯一性、prev_hash 与 HMAC 链。

        传入 ``expected_head``/``expected_count``（来自外部 WORM/锚点）时，
        可检测尾部截断或重放；不传时只能验证当前内存链自洽。
        """

        if expected_count is not None and len(self._events) != expected_count:
            return False
        prev_hash = ""
        seen_ids: set[UUID] = set()
        for index, event in enumerate(self._events, start=1):
            if event.sequence != index:
                return False
            if event.event_id in seen_ids:
                return False
            if event.prev_hash != prev_hash:
                return False
            canonical = self._canonical(event)
            if event.hash != self._digest(prev_hash, canonical):
                return False
            seen_ids.add(event.event_id)
            prev_hash = event.hash
        return expected_head is None or prev_hash == expected_head
