"""学年流转服务（M2 ORG-06）：夏季开学的升年级与毕业归档。

口径（`docs/design-briefs/M2-ORG06-academic-year.md`，已确认）：

- **规划与执行分离**：`plan_rollover` 是纯函数——校验映射、按**原始当前年级**同时
  计算受影响集合（链式映射 高一→高二 & 高二→高三 各班只命中一条，天然无级联双跳）；
  执行按 id 集合应用，与匹配顺序无关；
- **幂等**：`(tenant_id, idempotency_key)` 唯一（批次表），重复提交 409——若校方
  「先建新届（高一）再流转」，二次执行会把新届也升上去，此为唯一数据库防线；
  dry_run 是操作主防线（预览清单带入学年份供人工核对）；
- **不变量**：学生、班主任/辅导员指派、class 范围行**全部不动**（班级身份与关系
  不变，只有 grade/status 两列变化）；留级/跳级走既有学生重指派，不在本服务；
- **审计**：一次流转一条审计（含完整映射、归档年级、影响计数与班级 id 清单）。

裸 SQL 守卫豁免：批量流转是跨行集合操作（按年级匹配整批 UPDATE），无仓储行原语；
单班 CRUD 仍走 `app/org/service.py` 仓储。
"""

import logging
from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.db.models.org import AcademicYearRollover, OrgClass
from app.db.repository import ScopeBypass, TenantScopedRepository
from app.db.session import db_session
from app.org.service import OrgActor, OrgError, _audit, _require_school_scope, _require_tenant

logger = logging.getLogger(__name__)

_ENTRY_YEAR_MIN, _ENTRY_YEAR_MAX = 1970, 2100


@dataclass(frozen=True, slots=True)
class RolloverAdvance:
    """一条升年级映射（可选按入学年份过滤，防误伤新届班）。"""

    from_grade: str
    to_grade: str
    entry_year: int | None = None


@dataclass(frozen=True, slots=True)
class ClassSnapshot:
    """规划输入：一个活跃班级的关键列（脱离 ORM，纯函数可测）。"""

    id: UUID
    entry_year: int
    name: str
    grade: str


@dataclass(frozen=True, slots=True)
class GradeChange:
    """一个班的一次年级变化（升级）或归档。"""

    id: UUID
    entry_year: int
    name: str
    from_grade: str
    to_grade: str


@dataclass(frozen=True, slots=True)
class RolloverPlan:
    """流转计划：升级与归档清单（校验通过后的受影响集合）。"""

    advances: tuple[GradeChange, ...]
    archives: tuple[GradeChange, ...]


def _validate_inputs(
    advances: tuple[RolloverAdvance, ...], archive_grades: tuple[str, ...]
) -> None:
    if not advances and not archive_grades:
        raise OrgError("ROLLOVER_EMPTY", "升学映射与归档年级不能同时为空", 422)
    seen_mappings: set[tuple[str, str]] = set()
    for item in advances:
        if not item.from_grade or not item.to_grade:
            raise OrgError("ROLLOVER_INVALID", "年级映射不能为空串", 422, field="advances")
        if item.from_grade == item.to_grade:
            raise OrgError(
                "ROLLOVER_SAME_GRADE",
                f"升年级映射的起止年级相同：{item.from_grade}",
                422,
                field="advances",
            )
        if item.entry_year is not None and not (
            _ENTRY_YEAR_MIN <= item.entry_year <= _ENTRY_YEAR_MAX
        ):
            raise OrgError(
                "ENTRY_YEAR_INVALID",
                f"入学年份过滤须为 {_ENTRY_YEAR_MIN}~{_ENTRY_YEAR_MAX}",
                422,
                field="advances",
            )
        if (item.from_grade, item.to_grade) in seen_mappings:
            raise OrgError(
                "ROLLOVER_DUPLICATE_MAPPING",
                f"重复的年级映射：{item.from_grade}→{item.to_grade}",
                422,
                field="advances",
            )
        seen_mappings.add((item.from_grade, item.to_grade))
    seen_froms: set[str] = set()
    for item in advances:
        if item.from_grade in seen_froms:
            # 同起点两条不同映射会让同一个班命中两次（双跳），语义矛盾，直接拒绝
            raise OrgError(
                "ROLLOVER_CONFLICTING_MAPPING",
                f"年级 {item.from_grade} 出现在多条升学映射中，请确认唯一去向",
                422,
                field="advances",
            )
        seen_froms.add(item.from_grade)
    seen_archives: set[str] = set()
    for grade in archive_grades:
        if not grade:
            raise OrgError("ROLLOVER_INVALID", "归档年级不能为空串", 422, field="archive_grades")
        if grade in seen_archives:
            raise OrgError(
                "ROLLOVER_DUPLICATE_ARCHIVE",
                f"重复的归档年级：{grade}",
                422,
                field="archive_grades",
            )
        if any(item.from_grade == grade for item in advances):
            # 先归档后升级的语义下，from 已全部归档，再升级无对象——视为映射矛盾
            raise OrgError(
                "ROLLOVER_CONFLICT",
                f"年级 {grade} 同时出现在归档清单与升学映射的起点，请确认语义",
                422,
                field="advances",
            )
        seen_archives.add(grade)


def plan_rollover(
    snapshots: tuple[ClassSnapshot, ...],
    advances: tuple[RolloverAdvance, ...],
    archive_grades: tuple[str, ...],
) -> RolloverPlan:
    """纯函数：按**原始当前年级**同时计算受影响集合（不写库、无 ORM）。

    匹配规则：归档 = status 视角外的活跃班 + 当前年级命中归档清单；
    升级 = 当前年级命中映射起点（可选入学年份过滤）。链式映射各班只命中一条。
    """

    _validate_inputs(advances, archive_grades)
    archive_set = set(archive_grades)
    archives: list[GradeChange] = []
    archived_ids: set[UUID] = set()
    for snapshot in snapshots:
        if snapshot.grade in archive_set:
            archives.append(
                GradeChange(
                    id=snapshot.id,
                    entry_year=snapshot.entry_year,
                    name=snapshot.name,
                    from_grade=snapshot.grade,
                    to_grade=snapshot.grade,
                )
            )
            archived_ids.add(snapshot.id)
    advances_out: list[GradeChange] = []
    for item in advances:
        for snapshot in snapshots:
            if snapshot.id in archived_ids:
                continue
            if snapshot.grade != item.from_grade:
                continue
            if item.entry_year is not None and snapshot.entry_year != item.entry_year:
                continue
            advances_out.append(
                GradeChange(
                    id=snapshot.id,
                    entry_year=snapshot.entry_year,
                    name=snapshot.name,
                    from_grade=item.from_grade,
                    to_grade=item.to_grade,
                )
            )
    return RolloverPlan(advances=tuple(advances_out), archives=tuple(archives))


@dataclass(frozen=True, slots=True)
class RolloverResult:
    """流转结果（dry_run 与执行共用形状；执行附批次 id）。"""

    dry_run: bool
    advances: tuple[GradeChange, ...]
    archives: tuple[GradeChange, ...]
    rollover_id: UUID | None


async def execute_rollover(
    principal: OrgActor,
    *,
    advances: tuple[RolloverAdvance, ...],
    archive_grades: tuple[str, ...],
    idempotency_key: str,
    dry_run: bool,
) -> RolloverResult:
    """学年流转（dry_run 仅预览；执行=单事务：批次行 + 按 id 集合批量应用 + 审计）。"""

    tenant_id = _require_tenant(principal)
    # ORG-06 复审 Minor 2：流转是全校维度操作，与 list_classes 同口径要求 school 范围
    _require_school_scope(principal)
    async with db_session(tenant_id=tenant_id) as session:
        if not dry_run:
            # ORG-06 复审 Minor 1：学年操作天然单飞——租户级事务咨询锁，
            # 串行化不同幂等键的并发执行（同键仍由唯一索引 409）
            await session.execute(
                sa.text("select pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"rollover:{tenant_id}"},
            )
        # 全校维度批量操作（school.org.manage 授权）：不受调用者数据范围收窄
        repository: TenantScopedRepository[OrgClass] = TenantScopedRepository(
            session,
            OrgClass,
            tenant_id,
            scope=ScopeBypass(reason="学年流转：按 school.org.manage 全校批量刷新当前年级"),
        )
        rows = (
            (await session.execute(repository.base_query().where(OrgClass.status == "active")))
            .scalars()
            .all()
        )
        snapshots = tuple(
            ClassSnapshot(id=row.id, entry_year=row.entry_year, name=row.name, grade=row.grade)
            for row in rows
        )
        plan = plan_rollover(snapshots, advances, archive_grades)
        if dry_run:
            return RolloverResult(
                dry_run=True, advances=plan.advances, archives=plan.archives, rollover_id=None
            )

        # 幂等防线：批次行唯一键（tenant_id, idempotency_key）拦截重复提交
        rollover_row = AcademicYearRollover(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            mapping_json=[
                {"from_grade": a.from_grade, "to_grade": a.to_grade, "entry_year": a.entry_year}
                for a in advances
            ],
            archive_grades_json=list(archive_grades),
            advanced_count=len(plan.advances),
            archived_count=len(plan.archives),
            operator_user_id=principal.user_id,
        )
        session.add(rollover_row)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise OrgError(
                "ROLLOVER_DUPLICATE_KEY",
                "该幂等键已执行过学年流转，请勿重复提交",
                409,
                field="idempotency_key",
            ) from exc

        # 先归档后升级（语义顺序）；按 id 集合应用，与匹配顺序无关
        archive_ids = [change.id for change in plan.archives]
        if archive_ids:
            await session.execute(
                sa.update(OrgClass).where(OrgClass.id.in_(archive_ids)).values(status="archived")
            )
        for item in advances:
            ids = [
                c.id
                for c in plan.advances
                if c.from_grade == item.from_grade
                and c.to_grade == item.to_grade
                and (item.entry_year is None or c.entry_year == item.entry_year)
            ]
            if not ids:
                continue
            await session.execute(
                sa.update(OrgClass).where(OrgClass.id.in_(ids)).values(grade=item.to_grade)
            )

        await _audit(
            session,
            principal,
            tenant_id,
            action="org.academic_year.rollover",
            resource_type="org_class",
            resource_id=str(rollover_row.id),
            metadata={
                "idempotency_key": idempotency_key,
                "mapping": [
                    {"from_grade": a.from_grade, "to_grade": a.to_grade, "entry_year": a.entry_year}
                    for a in advances
                ],
                "archive_grades": list(archive_grades),
                "advanced_count": len(plan.advances),
                "archived_count": len(plan.archives),
                "advanced_ids": [str(c.id) for c in plan.advances],
                "archived_ids": [str(c.id) for c in plan.archives],
            },
        )
        return RolloverResult(
            dry_run=False,
            advances=plan.advances,
            archives=plan.archives,
            rollover_id=rollover_row.id,
        )
