"""单元：学年流转规划纯函数（M2 ORG-06，无 DB）。

覆盖简报红→绿清单的规划分支：正常升级/归档/先归档后升级共存/链式映射无级联/
入学年份过滤/全部校验拒绝路径。
"""

from uuid import uuid4

import pytest

from app.org.academic_year import ClassSnapshot, RolloverAdvance, plan_rollover
from app.org.service import OrgError


def _snap(entry_year: int, name: str, grade: str) -> ClassSnapshot:
    return ClassSnapshot(id=uuid4(), entry_year=entry_year, name=name, grade=grade)


def test_plan_normal_advance() -> None:
    """高一→高二：只动当前年级为高一的活跃班。"""

    snapshots = (
        _snap(2025, "1班", "高一"),
        _snap(2025, "2班", "高一"),
        _snap(2024, "1班", "高二"),
    )
    plan = plan_rollover(snapshots, (RolloverAdvance("高一", "高二"),), ())
    assert {(c.entry_year, c.name, c.to_grade) for c in plan.advances} == {
        (2025, "1班", "高二"),
        (2025, "2班", "高二"),
    }
    assert plan.archives == ()


def test_plan_archive() -> None:
    """归档：命中归档年级的班进入 archives（from=to=当前年级，仅状态变化）。"""

    snapshots = (_snap(2023, "1班", "高三"), _snap(2024, "1班", "高二"))
    plan = plan_rollover(snapshots, (), ("高三",))
    assert len(plan.archives) == 1
    assert plan.archives[0].entry_year == 2023
    assert plan.advances == ()


def test_plan_archive_then_advance_coexist() -> None:
    """先归档后升级共存：归档旧高三，同时高二→高三补位（简报确认语义）。"""

    snapshots = (
        _snap(2023, "1班", "高三"),
        _snap(2024, "1班", "高二"),
    )
    plan = plan_rollover(snapshots, (RolloverAdvance("高二", "高三"),), ("高三",))
    assert [c.entry_year for c in plan.archives] == [2023]
    assert [c.entry_year for c in plan.advances] == [2024]
    assert plan.advances[0].to_grade == "高三"


def test_plan_chained_mappings_no_double_jump() -> None:
    """链式映射（高一→高二 & 高二→高三）：按原年级同时匹配，各班只命中一条。"""

    snapshots = (
        _snap(2025, "1班", "高一"),
        _snap(2024, "1班", "高二"),
    )
    plan = plan_rollover(
        snapshots,
        (RolloverAdvance("高一", "高二"), RolloverAdvance("高二", "高三")),
        (),
    )
    by_entry = {c.entry_year: c.to_grade for c in plan.advances}
    assert by_entry == {2025: "高二", 2024: "高三"}


def test_plan_entry_year_filter_spares_new_cohort() -> None:
    """入学年份过滤：先建的新届高一班不被旧届映射误伤。"""

    snapshots = (
        _snap(2026, "1班", "高一"),  # 新届（已提前建班）
        _snap(2025, "1班", "高一"),  # 旧届待升
    )
    plan = plan_rollover(snapshots, (RolloverAdvance("高一", "高二", entry_year=2025),), ())
    assert [c.entry_year for c in plan.advances] == [2025]


def test_plan_empty_payload_rejected() -> None:
    with pytest.raises(OrgError) as exc:
        plan_rollover((), (), ())
    assert exc.value.code == "ROLLOVER_EMPTY"


def test_plan_same_grade_rejected() -> None:
    with pytest.raises(OrgError) as exc:
        plan_rollover((), (RolloverAdvance("高一", "高一"),), ())
    assert exc.value.code == "ROLLOVER_SAME_GRADE"


def test_plan_duplicate_mapping_rejected() -> None:
    with pytest.raises(OrgError) as exc:
        plan_rollover((), (RolloverAdvance("高一", "高二"), RolloverAdvance("高一", "高二")), ())
    assert exc.value.code == "ROLLOVER_DUPLICATE_MAPPING"


def test_plan_conflicting_same_from_rejected() -> None:
    """同起点两条不同去向（高一→高二 & 高一→高三）语义矛盾，拒绝。"""

    with pytest.raises(OrgError) as exc:
        plan_rollover((), (RolloverAdvance("高一", "高二"), RolloverAdvance("高一", "高三")), ())
    assert exc.value.code == "ROLLOVER_CONFLICTING_MAPPING"


def test_plan_from_in_archive_rejected() -> None:
    """归档年级不能同时是升级起点（先归档后升级语义下无对象）。"""

    with pytest.raises(OrgError) as exc:
        plan_rollover((), (RolloverAdvance("高三", "毕业"),), ("高三",))
    assert exc.value.code == "ROLLOVER_CONFLICT"


def test_plan_duplicate_archive_rejected() -> None:
    with pytest.raises(OrgError) as exc:
        plan_rollover((), (), ("高三", "高三"))
    assert exc.value.code == "ROLLOVER_DUPLICATE_ARCHIVE"


def test_plan_entry_year_out_of_range_rejected() -> None:
    with pytest.raises(OrgError) as exc:
        plan_rollover((), (RolloverAdvance("高一", "高二", entry_year=1800),), ())
    assert exc.value.code == "ENTRY_YEAR_INVALID"
