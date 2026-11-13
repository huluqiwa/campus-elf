"""人群 ACL 纯函数单测（M3 KB-09A/09B）：payload 校验 / token 生成 / fail-closed
门禁判据 / 视角 token 结构校验（KB-09B）/ 人群解析器（KB-09B，fake 数据源）。"""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.kb.audience import (
    ACL_GATE_ALLOW,
    AudienceValidationError,
    StudentFacts,
    TeacherFacts,
    acl_gate,
    audience_tokens,
    parse_audience_items,
    parse_audience_tokens,
    resolve_audience_tokens,
)

UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"


def _code(exc: AudienceValidationError) -> str:
    return exc.code


def test_parse_accepts_each_type_in_own_payload() -> None:
    """七种类型逐个独立载荷可解析（all 单独成档——AUDIENCE_ALL_MIXED 拒混配）。"""

    for audience_type in ("department", "role", "class", "grade", "course", "user"):
        parsed = parse_audience_items([{"audience_type": audience_type, "audience_id": UUID_A}])
        assert parsed == [(audience_type, UUID_A)]
    assert parse_audience_items([{"audience_type": "all", "audience_id": None}]) == [("all", None)]


def test_parse_grade_takes_arbitrary_string_value() -> None:
    """grade 的 audience_id 是年级字符串（非 UUID；classes.grade 为 String(16)）。"""

    parsed = parse_audience_items([{"audience_type": "grade", "audience_id": "2026"}])
    assert parsed == [("grade", "2026")]


def test_parse_rejects_unknown_type() -> None:
    with pytest.raises(AudienceValidationError) as exc:
        parse_audience_items([{"audience_type": "group", "audience_id": UUID_A}])
    assert _code(exc.value) == "AUDIENCE_TYPE_INVALID"


def test_parse_rejects_all_mixed() -> None:
    """all 与其他类型混配 → 422 AUDIENCE_ALL_MIXED（简报 §3.1 细化决策）。"""

    with pytest.raises(AudienceValidationError) as exc:
        parse_audience_items(
            [
                {"audience_type": "all", "audience_id": None},
                {"audience_type": "department", "audience_id": UUID_A},
            ]
        )
    assert _code(exc.value) == "AUDIENCE_ALL_MIXED"


def test_parse_rejects_all_with_audience_id() -> None:
    with pytest.raises(AudienceValidationError) as exc:
        parse_audience_items([{"audience_type": "all", "audience_id": UUID_A}])
    assert _code(exc.value) == "AUDIENCE_ID_FORBIDDEN"


def test_parse_rejects_missing_audience_id() -> None:
    """非 all 类型必须有非空白 audience_id。"""

    for blank in (None, "", "   "):
        with pytest.raises(AudienceValidationError) as exc:
            parse_audience_items([{"audience_type": "department", "audience_id": blank}])
        assert _code(exc.value) == "AUDIENCE_ID_REQUIRED"


def test_parse_rejects_duplicate_entry() -> None:
    with pytest.raises(AudienceValidationError) as exc:
        parse_audience_items(
            [
                {"audience_type": "department", "audience_id": UUID_A},
                {"audience_type": "department", "audience_id": UUID_A},
            ]
        )
    assert _code(exc.value) == "AUDIENCE_DUPLICATE"


def test_parse_rejects_non_list_payload() -> None:
    with pytest.raises(AudienceValidationError):
        parse_audience_items({"audience_type": "all"})  # type: ignore[arg-type]


def test_audience_tokens_mapping() -> None:
    """token 形如 `department:<uuid>`；all → `all`（spec :310）。"""

    tokens = audience_tokens([("department", UUID_A), ("grade", "2026"), ("all", None)])
    assert tokens == [f"department:{UUID_A}", "grade:2026", "all"]


def test_acl_gate_required_when_empty() -> None:
    """0 条 → ACL_REQUIRED（无论确认态；spec :307）。"""

    confirmed_at = datetime(2026, 11, 13, tzinfo=UTC)
    assert acl_gate(0, None) == "ACL_REQUIRED"
    assert acl_gate(0, confirmed_at) == "ACL_REQUIRED"


def test_acl_gate_unconfirmed() -> None:
    assert acl_gate(2, None) == "ACL_UNCONFIRMED"


def test_acl_gate_pass() -> None:
    assert acl_gate(2, datetime(2026, 11, 13, tzinfo=UTC)) == ACL_GATE_ALLOW


# ---------------------------------------------------------------------------
# KB-09B：构建侧 token 规范化（UUID 型小写——配置侧存原样字符串、解析侧
# `str(uuid)` 小写，不规范化会静默失配；grade 年级值保持原样）
# ---------------------------------------------------------------------------


def test_audience_tokens_normalizes_uuid_ids_lowercase() -> None:
    upper = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
    tokens = audience_tokens(
        [("department", upper), ("grade", "2026"), ("all", None)]
    )
    assert tokens == [
        f"department:{UUID(upper)!s}",
        "grade:2026",
        "all",
    ]


# ---------------------------------------------------------------------------
# KB-09B：视角 token 结构校验（as_audiences；422 族，不做同租存在性校验）
# ---------------------------------------------------------------------------

UUID_UPPER = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
UUID_LOWER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_parse_tokens_accepts_valid_forms_and_normalizes_uuid() -> None:
    parsed = parse_audience_tokens(
        ["department:" + UUID_UPPER, "grade:2026", "all", " user:" + UUID_LOWER + " "]
    )
    assert parsed == ["department:" + UUID_LOWER, "grade:2026", "all", f"user:{UUID_LOWER}"]


def test_parse_tokens_rejects_unknown_prefix() -> None:
    with pytest.raises(AudienceValidationError) as exc:
        parse_audience_tokens(["group:x"])
    assert _code(exc.value) == "AUDIENCE_TOKEN_INVALID"


def test_parse_tokens_rejects_missing_separator_or_empty_id() -> None:
    for bad in ("department", "department:", ":x", "all:extra"):
        with pytest.raises(AudienceValidationError) as exc:
            parse_audience_tokens([bad])
        assert _code(exc.value) == "AUDIENCE_TOKEN_INVALID", bad


def test_parse_tokens_rejects_blank_and_non_string() -> None:
    with pytest.raises(AudienceValidationError) as exc:
        parse_audience_tokens(["   "])
    assert _code(exc.value) == "AUDIENCE_TOKEN_INVALID"
    with pytest.raises(AudienceValidationError) as exc:
        parse_audience_tokens([123])
    assert _code(exc.value) == "AUDIENCE_TOKEN_PAYLOAD_INVALID"
    with pytest.raises(AudienceValidationError):
        parse_audience_tokens("department:x")  # type: ignore[arg-type]


def test_parse_tokens_rejects_invalid_uuid_for_uuid_typed_prefix() -> None:
    with pytest.raises(AudienceValidationError) as exc:
        parse_audience_tokens(["department:not-a-uuid"])
    assert _code(exc.value) == "AUDIENCE_TOKEN_INVALID"


def test_parse_tokens_rejects_duplicate_after_normalization() -> None:
    """同 id 大小写不同 → 规范化后同 token → 重复拒绝。"""

    with pytest.raises(AudienceValidationError) as exc:
        parse_audience_tokens([f"user:{UUID_UPPER}", f"user:{UUID_LOWER}"])
    assert _code(exc.value) == "AUDIENCE_TOKEN_DUPLICATE"


# ---------------------------------------------------------------------------
# KB-09B：人群解析器（fake 数据源；教师部门祖先链 / 学分支 / 管理员分支 /
# 平台与缺失用户空集 / 调岗即时——实时解析无缓存）
# ---------------------------------------------------------------------------

UUID_C = UUID("33333333-3333-3333-3333-333333333333")
UUID_D = UUID("44444444-4444-4444-4444-444444444444")
UUID_E = UUID("55555555-5555-5555-5555-555555555555")


class FakeSource:
    """AudienceSource 协议的内存实现（单测不碰库）。"""

    def __init__(
        self,
        *,
        user_type: str | None,
        teacher: TeacherFacts | None = None,
        student: StudentFacts | None = None,
        role_ids: tuple[UUID, ...] = (),
    ) -> None:
        self.user_type = user_type
        self.teacher = teacher
        self.student = student
        self.role_ids = role_ids

    async def load_user_type(self, user_id: UUID) -> str | None:
        return self.user_type

    async def load_teacher_facts(self, user_id: UUID) -> TeacherFacts | None:
        return self.teacher

    async def load_student_facts(self, user_id: UUID) -> StudentFacts | None:
        return self.student

    async def load_role_ids(self, user_id: UUID) -> tuple[UUID, ...]:
        return self.role_ids


async def test_resolve_teacher_tokens_include_department_ancestor_chain() -> None:
    """部门祖先链：path `/{root}/{mid}/{own}` → 三个 department token（父部门配置
    → 子成员命中，spec :316 层级）。"""

    user_id = UUID(UUID_A)
    source = FakeSource(
        user_type="teacher",
        teacher=TeacherFacts(
            department_path=f"/{UUID_A}/{UUID_B}/{UUID_C}",
            taught_class_ids=(UUID_D,),
            course_ids=(UUID_E,),
            head_class_ids=(UUID_D, UUID(UUID_A)),  # 与任教班级重叠 → 去重
        ),
        role_ids=(UUID(UUID_B),),
    )
    tokens = await resolve_audience_tokens(source, user_id)
    assert tokens == sorted(
        {
            "all",
            f"user:{UUID_A}",
            f"department:{UUID_A}",
            f"department:{UUID_B}",
            f"department:{UUID_C}",
            f"role:{UUID_B}",
            f"class:{UUID_D}",
            f"class:{UUID_A}",
            f"course:{UUID_E}",
        }
    )


async def test_resolve_teacher_without_department_or_profile() -> None:
    """未归属（department_path None）→ 无 department token；无档案行 → 仅
    role + user + all（降级不猜）。"""

    no_dept = await resolve_audience_tokens(
        FakeSource(
            user_type="teacher",
            teacher=TeacherFacts(None, (), (), ()),
            role_ids=(UUID(UUID_B),),
        ),
        UUID(UUID_A),
    )
    assert no_dept == sorted({"all", f"user:{UUID_A}", f"role:{UUID_B}"})
    no_profile = await resolve_audience_tokens(
        FakeSource(user_type="teacher", teacher=None, role_ids=(UUID(UUID_B),)),
        UUID(UUID_A),
    )
    assert no_profile == sorted({"all", f"user:{UUID_A}", f"role:{UUID_B}"})


async def test_resolve_student_tokens_class_grade_self_all() -> None:
    """C 端学生口径（design :593）：班级+年级+self+all——**不含 role**（学生
    不可绑定自定义角色，TD-187d 目标限 teacher/school_admin）。"""

    source = FakeSource(
        user_type="student",
        student=StudentFacts(class_id=UUID_D, grade="高一"),
        role_ids=(UUID(UUID_B),),
    )
    tokens = await resolve_audience_tokens(source, UUID(UUID_A))
    assert tokens == sorted({"all", f"user:{UUID_A}", f"class:{UUID_D}", "grade:高一"})


async def test_resolve_student_without_class_falls_back_to_self_all() -> None:
    source = FakeSource(user_type="student", student=StudentFacts(None, None))
    tokens = await resolve_audience_tokens(source, UUID(UUID_A))
    assert tokens == sorted({"all", f"user:{UUID_A}"})


async def test_resolve_school_admin_tokens_role_self_all() -> None:
    """school_admin 无档案表（org.py 注释）→ 仅 role + user + all。"""

    source = FakeSource(user_type="school_admin", role_ids=(UUID(UUID_B),))
    tokens = await resolve_audience_tokens(source, UUID(UUID_A))
    assert tokens == sorted({"all", f"user:{UUID_A}", f"role:{UUID_B}"})


async def test_resolve_unknown_or_missing_user_yields_empty_fail_closed() -> None:
    """users 行不存在（含 platform realm——平台账号不在 users 表）→ 空集
    fail-closed（不发查询；简报 §3.2.2）。"""

    assert await resolve_audience_tokens(FakeSource(user_type=None), UUID(UUID_A)) == []


async def test_resolve_is_realtime_no_cache_transfer_takes_effect_immediately() -> None:
    """调岗即时生效（TD-098 缓存归 M4）：同一 source 改部门后再次解析即新 token。"""

    source = FakeSource(
        user_type="teacher",
        teacher=TeacherFacts(f"/{UUID_B}", (), (), ()),
    )
    user_id = UUID(UUID_A)
    before = await resolve_audience_tokens(source, user_id)
    assert f"department:{UUID_B}" in before
    source.teacher = TeacherFacts(f"/{UUID_C}", (), (), ())
    after = await resolve_audience_tokens(source, user_id)
    assert f"department:{UUID_C}" in after
    assert f"department:{UUID_B}" not in after
