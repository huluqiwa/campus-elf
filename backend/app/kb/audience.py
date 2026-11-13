"""文档查阅人群（K4）纯函数层——M3 KB-09A/09B。

`docs/design-briefs/M3-KB-09.md`（已确认 D1-A 门禁形态 / D2-B payload 单一事实源 /
卡 5 工程事实；§3.2 细化节＝KB-09B 决策卡 B1-B4，2026-11-14 经用户拍板）。本模块
只放**纯函数与解析器协议**（不碰库/网络）；同租存在性校验、门禁接线与 Qdrant 同步
在 `service.py` / `build_task.py`，解析器的仓储数据源实装在 `audience_source.py`
（`RepositoryAudienceSource`，bare-SQL 守卫口径）。

口径（spec KB-09 :306-310）：
- `audience_type ∈ {department, role, class, grade, course, user, all}`，多条为
  **并集**；`all` 显式选择且不带 audience_id，与其他类型混配拒绝（简报 §3.1）；
- token 形如 `department:<uuid>`（`all` → `all`）——构建期写入 Qdrant payload
  `acl_tokens`，检索期 `MatchAny(用户 tokens)` 匹配（min_should=1）；
- fail-closed 门禁判据：0 条 → `ACL_REQUIRED`（无论确认态）；非空未确认 →
  `ACL_UNCONFIRMED`（spec :307）。

KB-09B 解析器口径（简报 §3.2.1，design :593）：B 端教师 = 部门（自身+祖先链）+
角色 + 任教/班主任班级 + 课程 + 自身 + all；学生 = 班级 + 年级 + 自身 + all
（**不含 role**——学生不可绑定自定义角色）；school_admin（无档案表）= 角色 +
自身 + all；users 行不存在（含平台账号）→ 空集 fail-closed。实时解析无缓存
（TD-098 缓存归 M4 chat），调岗立即生效。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.db.models.kb import KB_AUDIENCE_TYPES

ACL_GATE_ALLOW = ""  # 放行哨兵（不命名为 *_PASS——bandit B105 会把 PASS 字样误读为口令）
GATE_ACL_REQUIRED = "ACL_REQUIRED"
GATE_ACL_UNCONFIRMED = "ACL_UNCONFIRMED"

# UUID 型 audience_id 的 token 规范化：配置侧存原样字符串、解析侧 `str(uuid)`
# 为小写——不统一会静默失配（token 全等匹配）。grade 年级值保持原样。
_UUID_TOKEN_TYPES = frozenset({"department", "role", "class", "course", "user"})
# 视角 token（as_audiences）允许的前缀（all 单独成档）
AUDIENCE_TOKEN_PREFIXES = frozenset(_UUID_TOKEN_TYPES | {"grade"})


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


def audience_tokens(parsed: Sequence[tuple[str, str | None]]) -> list[str]:
    """规范化条目 → Qdrant payload token 列表（`type:<id>`；`all` → `all`）。

    UUID 型 audience_id 规范化为小写（`str(UUID(v))`）——与解析侧 `str(uuid)`
    同构，防止配置侧存大写导致 token 全等匹配静默失配（KB-09B 登记修正）。
    调用前条目应已过 `parse_audience_items` + 同租存在性校验（UUID 型必合法）。
    """

    tokens: list[str] = []
    for audience_type, audience_id in parsed:
        if audience_id is None:
            tokens.append(audience_type)
            continue
        if audience_type in _UUID_TOKEN_TYPES:
            tokens.append(f"{audience_type}:{UUID(audience_id)}")
        else:
            tokens.append(f"{audience_type}:{audience_id}")
    return tokens


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


# ---------------------------------------------------------------------------
# KB-09B：视角 token 结构校验（as_audiences）与人群解析器（简报 §3.2）
# ---------------------------------------------------------------------------


def parse_audience_tokens(tokens: list[Any]) -> list[str]:
    """结构校验检索测试 `as_audiences`（诊断视角 token 字符串数组）→ 规范化列表。

    只做**结构校验**（简报 §3.2.3 决策卡 B3）：前缀白名单 / `all` 单独成档 /
    id 非空 / UUID 型 id 必须合法并规范化小写 / 去重（规范化后判重）。
    **不做同租存在性校验**——collection 租户定域，伪造 token 只会零召回
    （fail-closed 方向无害），且省 4 类存在性查询。

    422 族：`AUDIENCE_TOKEN_PAYLOAD_INVALID` / `AUDIENCE_TOKEN_INVALID` /
    `AUDIENCE_TOKEN_DUPLICATE`（服务层映射 KbError）。
    """

    if not isinstance(tokens, list):
        raise AudienceValidationError(
            "AUDIENCE_TOKEN_PAYLOAD_INVALID", "人群 token 必须是字符串数组"
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in tokens:
        if not isinstance(raw, str):
            raise AudienceValidationError(
                "AUDIENCE_TOKEN_PAYLOAD_INVALID", "人群 token 必须是字符串"
            )
        # 局部变量不命名 `token`——bandit B105 会把「名为 token 的变量与字符串
        # 比较」误读为硬编码口令（KB-09A ACL_GATE_PASS 同款教训）
        candidate = raw.strip()
        if not candidate:
            raise AudienceValidationError("AUDIENCE_TOKEN_INVALID", "人群 token 不能为空白")
        if candidate != "all":
            prefix, sep, value = candidate.partition(":")
            value = value.strip()
            if not sep or prefix not in AUDIENCE_TOKEN_PREFIXES or not value:
                raise AudienceValidationError(
                    "AUDIENCE_TOKEN_INVALID", f"非法人群 token：{raw!r}"
                )
            if prefix in _UUID_TOKEN_TYPES:
                try:
                    value = str(UUID(value))
                except ValueError as exc:
                    raise AudienceValidationError(
                        "AUDIENCE_TOKEN_INVALID", f"非法人群 token：{raw!r}"
                    ) from exc
            candidate = f"{prefix}:{value}"
        if candidate in seen:
            raise AudienceValidationError("AUDIENCE_TOKEN_DUPLICATE", "人群 token 重复")
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


@dataclass(frozen=True, slots=True)
class TeacherFacts:
    """教师人群事实（数据源一次取齐；部门为空 = 未归属，关系为空 = 未任教）。"""

    department_path: str | None
    taught_class_ids: tuple[UUID, ...]
    course_ids: tuple[UUID, ...]
    head_class_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class StudentFacts:
    """学生人群事实（class_id 可空 = 未分班；grade 为班级现值）。"""

    class_id: UUID | None
    grade: str | None


class AudienceSource(Protocol):
    """人群解析数据源协议（仓储实装见 `service.py`；单测用内存 fake）。"""

    async def load_user_type(self, user_id: UUID) -> str | None: ...

    async def load_teacher_facts(self, user_id: UUID) -> TeacherFacts | None: ...

    async def load_student_facts(self, user_id: UUID) -> StudentFacts | None: ...

    async def load_role_ids(self, user_id: UUID) -> tuple[UUID, ...]: ...


async def resolve_audience_tokens(source: AudienceSource, user_id: UUID) -> list[str]:
    """解析用户的检索人群 token 集（实时、无缓存；去重排序确定序）。

    users 行不存在（含平台 realm——平台账号不在 users 表）→ 空集 fail-closed。
    解析**异常不在此吞**：调用方按「解析异常=不召回」口径捕获转空集（简报
    §3.2.3），本函数保持诚实上抛以便编排层记日志。
    """

    user_type = await source.load_user_type(user_id)
    if user_type is None:
        return []
    if user_type == "teacher":
        teacher = await source.load_teacher_facts(user_id)
        if teacher is None:
            teacher = TeacherFacts(None, (), (), ())
        role_ids = await source.load_role_ids(user_id)
        return facts_to_tokens(
            TeacherFacts(
                department_path=teacher.department_path,
                taught_class_ids=teacher.taught_class_ids,
                course_ids=teacher.course_ids,
                head_class_ids=teacher.head_class_ids,
            ),
            role_ids=role_ids,
            user_id=user_id,
        )
    if user_type == "student":
        student = await source.load_student_facts(user_id)
        if student is None:
            student = StudentFacts(None, None)
        # C 端学生口径（design :593）：班级+年级+self+all——不含 role
        return facts_to_tokens(student, role_ids=(), user_id=user_id)
    # school_admin（无档案表，org.py 注释）与其余未知类型：仅 role + user + all
    role_ids = await source.load_role_ids(user_id)
    return facts_to_tokens(None, role_ids=role_ids, user_id=user_id)


def facts_to_tokens(
    profile: TeacherFacts | StudentFacts | None, *, role_ids: tuple[UUID, ...], user_id: UUID
) -> list[str]:
    """人群事实 → token 集（`all` 恒注入；排序输出保证确定性）。"""

    tokens: set[str] = {"all", f"user:{user_id}"}
    tokens.update(f"role:{role_id}" for role_id in role_ids)
    if isinstance(profile, TeacherFacts):
        if profile.department_path:
            # 祖先链＝物化路径前缀段集（`/{root}/…/{own}`，含自身）——
            # 「父部门配置 → 子成员命中」（spec :316 层级）；段 id 为服务端
            # 维护的本租部门 id，零额外查询
            for part in profile.department_path.split("/"):
                if part:
                    tokens.add(f"department:{part}")
        for class_id in (*profile.taught_class_ids, *profile.head_class_ids):
            tokens.add(f"class:{class_id}")
        tokens.update(f"course:{course_id}" for course_id in profile.course_ids)
    elif isinstance(profile, StudentFacts):
        if profile.class_id is not None:
            tokens.add(f"class:{profile.class_id}")
        if profile.grade:
            tokens.add(f"grade:{profile.grade}")
    return sorted(tokens)
