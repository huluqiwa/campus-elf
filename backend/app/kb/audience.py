"""文档查阅人群（K4）纯函数层——M3 KB-09A。

`docs/design-briefs/M3-KB-09.md`（已确认 D1-A 门禁形态 / D2-B payload 单一事实源 /
卡 5 工程事实）。本模块只放**纯函数**（不碰库/网络）；同租存在性校验、门禁接线与
Qdrant 同步在 `service.py` / `build_task.py`（KB-09B 将在本模块追加人群解析器）。

口径（spec KB-09 :306-310）：
- `audience_type ∈ {department, role, class, grade, course, user, all}`，多条为
  **并集**；`all` 显式选择且不带 audience_id，与其他类型混配拒绝（简报 §3.1）；
- token 形如 `department:<uuid>`（`all` → `all`）——构建期写入 Qdrant payload
  `acl_tokens`，检索期 `MatchAny(用户 tokens)` 匹配（min_should=1）；
- fail-closed 门禁判据：0 条 → `ACL_REQUIRED`（无论确认态）；非空未确认 →
  `ACL_UNCONFIRMED`（spec :307）。
"""

from datetime import datetime
from typing import Any

from app.db.models.kb import KB_AUDIENCE_TYPES

ACL_GATE_ALLOW = ""  # 放行哨兵（不命名为 *_PASS——bandit B105 会把 PASS 字样误读为口令）
GATE_ACL_REQUIRED = "ACL_REQUIRED"
GATE_ACL_UNCONFIRMED = "ACL_UNCONFIRMED"


class AudienceValidationError(ValueError):
    """人群 payload 结构非法（`code` 人读错误码；服务层映射 KbError 422）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def parse_audience_items(items: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    """校验并规范化人群条目 → `[(audience_type, audience_id | None)]`（保序去重前）。

    422 族：`AUDIENCE_TYPE_INVALID` / `AUDIENCE_ALL_MIXED`（all 不得与其他类型
    混配）/ `AUDIENCE_ID_FORBIDDEN`（all 不得带 id）/ `AUDIENCE_ID_REQUIRED`
    （非 all 必须非空白 id）/ `AUDIENCE_DUPLICATE`（载荷内同 type+id 重复）。
    """

    if not isinstance(items, list):
        raise AudienceValidationError("AUDIENCE_PAYLOAD_INVALID", "人群条目必须是数组")
    parsed: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()
    for item in items:
        if not isinstance(item, dict):
            raise AudienceValidationError("AUDIENCE_PAYLOAD_INVALID", "人群条目必须是对象")
        audience_type = item.get("audience_type")
        if not isinstance(audience_type, str) or audience_type not in KB_AUDIENCE_TYPES:
            raise AudienceValidationError(
                "AUDIENCE_TYPE_INVALID", f"未知人群类型：{audience_type!r}"
            )
        audience_id = item.get("audience_id")
        if audience_type == "all":
            if audience_id is not None:
                raise AudienceValidationError(
                    "AUDIENCE_ID_FORBIDDEN", "all 人群不接受对象 id（显式全体）"
                )
        else:
            if not isinstance(audience_id, str) or not audience_id.strip():
                raise AudienceValidationError(
                    "AUDIENCE_ID_REQUIRED", f"{audience_type} 人群必须提供对象 id"
                )
            audience_id = audience_id.strip()
        key = (audience_type, audience_id)
        if key in seen:
            raise AudienceValidationError("AUDIENCE_DUPLICATE", "人群条目重复")
        seen.add(key)
        parsed.append((audience_type, audience_id))
    has_all = any(audience_type == "all" for audience_type, _ in parsed)
    if has_all and len(parsed) > 1:
        raise AudienceValidationError(
            "AUDIENCE_ALL_MIXED", "all（显式全体）不得与其他人群类型混配"
        )
    return parsed


def audience_tokens(parsed: list[tuple[str, str | None]]) -> list[str]:
    """规范化条目 → Qdrant payload token 列表（`type:<id>`；`all` → `all`）。"""

    return [
        audience_type if audience_id is None else f"{audience_type}:{audience_id}"
        for audience_type, audience_id in parsed
    ]


def acl_gate(audience_count: int, acl_confirmed_at: datetime | None) -> str:
    """fail-closed 门禁判据：返回 `ACL_GATE_ALLOW`（放行）或 409 错误码。

    0 条 → `ACL_REQUIRED`（无论确认态——spec :307）；非空未确认 →
    `ACL_UNCONFIRMED`；确认态放行。
    """

    if audience_count <= 0:
        return GATE_ACL_REQUIRED
    if acl_confirmed_at is None:
        return GATE_ACL_UNCONFIRMED
    return ACL_GATE_ALLOW
