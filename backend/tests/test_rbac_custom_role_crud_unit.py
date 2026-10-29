"""自定义角色创建/删除的纯逻辑单元（M2-UI3 TD-187b）。

钉四条"不需要数据库就该成立"的不变量——它们一旦漂移，接口层的错误码与库层约束就会分叉：

1. 保留码名单**派生自** `seeds.py`，不是手抄的字面量（内置角色改名时这里自动跟着走）；
2. 接口层的 code 正则与迁移里的 `ck_roles_code_format_valid` 逐字符相同（两份真相迟早会不一致，
   而"接口放过、库撞 CHECK 变 500"是最难复现的一类线上故障）；长度上限 63 而非列宽 64；
3. `is_system=false` 的角色在 `_lock_reason` / `_to_summary` 里一律可编辑（M2-UI3 的翻转点），
   内置角色仍按 `EDITABLE_ROLE_CODES` / `LOCKED_ROLE_REASONS` 划分；
4. 复制源与显式权限清单互斥：这是入参语义检查，必须在碰库之前拒绝；
5. 角色名的"非空白"判据：库层 CHECK 的空白字符集 ⊆ 接口层那一份（TD-198），
   库层不得比接口层宽松（否则兜底反而漏掉"接口拒、库收"的行）；
6. `resettable` 由服务端从出厂默认表派生（TD-200），前端不再按 `is_system` 猜；
   它还要连"角色可编辑"这一道门一起算——只算"有出厂定义"会给锁定角色点亮一个必 409 的按钮。
"""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.api.school_rbac import RoleCreatePayload, RoleDeletionPreviewResponse
from app.auth.principal import Principal
from app.rbac import admin
from app.rbac.admin import (
    DB_ROLE_NAME_BLANK_CHARS,
    EDITABLE_ROLE_CODES,
    LOCKED_ROLE_REASONS,
    RESERVED_TENANT_ROLE_CODES,
    ROLE_CODE_PATTERN,
    SUPER_ADMIN_ROLE_CODE,
    RoleAdminError,
    _deletion_fingerprint,
    _lock_reason,
    _to_summary,
)
from app.rbac.seeds import TENANT_BUILTIN_ROLES, builtin_roles

MIGRATION_FILE = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "d7a1c3e5b9f4_m2_ui3_custom_role_invariants.py"
)

#: TD-198 的迁移（角色名非空白 CHECK）。
NAME_CHECK_MIGRATION_FILE = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "a1c3e5f7b9d2_m2_ui3_role_name_not_blank.py"
)


def _principal() -> Principal:
    return Principal(
        user_id=uuid4(),
        realm="tenant",
        tenant_id=uuid4(),
        jti=uuid4(),
        session_id=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        must_change_password=False,
        roles=frozenset({"school_super_admin"}),
        permissions=frozenset({"school.role.manage", "school.role.delete"}),
    )


def _row(code: str, *, is_system: bool, resettable: bool | None = None) -> dict[str, object]:
    return {
        "id": uuid4(),
        "code": code,
        "name": f"{code} 名称",
        "is_system": is_system,
        "defaults_applied": is_system,
        "version": 3,
        "user_count": 2,
        "permission_codes": ["school.kb.view"],
        # `resettable` 由服务端从出厂默认表派生（TD-200），与 `is_system` 不是一回事：
        # 默认按"内置 ⇒ 有出厂定义"给值，需要单独验派生时显式传。
        "resettable": is_system if resettable is None else resettable,
    }


def test_reserved_codes_derive_from_seed_definition() -> None:
    """保留名单 = `seeds.py` 的租户内置角色码，且不混入平台 realm。"""

    assert frozenset(
        str(role.code) for role in builtin_roles("tenant")
    ) == RESERVED_TENANT_ROLE_CODES
    assert {str(role.code) for role in TENANT_BUILTIN_ROLES} <= RESERVED_TENANT_ROLE_CODES
    # 平台角色码不是租户 roles 表的内置码：把它们列为保留会让租户建不出同名的自建角色
    platform_codes = {str(role.code) for role in builtin_roles("platform")}
    assert platform_codes and platform_codes.isdisjoint(RESERVED_TENANT_ROLE_CODES)


def test_interface_code_pattern_matches_database_check() -> None:
    """接口正则与迁移里的 CHECK 逐字符一致；长度上限 63（列宽 64 会穿过接口再撞库）。"""

    assert MIGRATION_FILE.exists(), f"迁移文件路径变了：{MIGRATION_FILE}"
    migration_sql = MIGRATION_FILE.read_text(encoding="utf-8")
    # 迁移用 f-string 把 `_CODE_FORMAT` 插进 CHECK，所以对照点是那个常量声明本身
    embedded = re.search(r'^_CODE_FORMAT = r"([^"]+)"', migration_sql, re.MULTILINE)
    assert embedded is not None, "迁移里找不到 code 格式正则：本用例的对照前提不成立"
    assert embedded.group(1) == ROLE_CODE_PATTERN, (
        f"接口正则 {ROLE_CODE_PATTERN!r} 与库层 {embedded.group(1)!r} 漂移"
    )

    compiled = re.compile(ROLE_CODE_PATTERN)
    for good in ("abc", "a_1", "library_keeper", "x" * 63):
        assert compiled.fullmatch(good), good
    for bad in ("ab", "1digit", "Bad-Code", "含中文", "x" * 64, "_leading"):
        assert not compiled.fullmatch(bad), bad


def test_db_blank_set_is_a_subset_of_the_interface_blank_set() -> None:
    """TD-198：库层 CHECK 的空白字符集是接口层那一份的**子集**（接口更严）。

    两条方向都要钉住：
    - **库层判为空 ⇒ 接口层必拒**（ASCII 集 ⊆ Python `strip()` 集）：库里算"没名字"的，
      接口不会放行，两边不会对同一个名字给出相反结论；
    - **接口层比库层更严**（Python `strip()` 还去 U+3000 等 Unicode 空白）：界面里不能出现
      "看不见名字"的角色——这正是 TD-198 要治的病。
    已知窄缝：绕过应用直接写 SQL 时，一个只含 U+3000 的名字库层会放行。补齐全套 Unicode
    空白集要在 SQL 里维护一份易腐的字面量，代价大于收益（写入口只有接口层）；
    该窄缝登记在 `docs/tech-debt.md` TD-239。
    """

    assert NAME_CHECK_MIGRATION_FILE.exists(), (
        f"迁移文件路径变了：{NAME_CHECK_MIGRATION_FILE}"
    )
    migration_sql = NAME_CHECK_MIGRATION_FILE.read_text(encoding="utf-8")
    # 迁移用 f-string 把字符集插进 CHECK，所以对照点是那个常量声明本身
    # （与 `ck_roles_code_format_valid` 的同款回归网）。
    embedded = re.search(r'^_BLANK_CHARS = r"([^"]+)"', migration_sql, re.MULTILINE)
    assert embedded is not None, "迁移里找不到角色名的空白字符集：本用例的对照前提不成立"
    # 迁移里是 PG 的 E'' 转义写法（\t \n ...），解成真实字符再比对
    assert embedded.group(1).encode().decode("unicode_escape") == DB_ROLE_NAME_BLANK_CHARS, (
        f"库层空白字符集 {embedded.group(1)!r} 与应用层快照漂移"
    )
    # 约束表达式确实用上了这份字符集（不是"常量声明在那儿、CHECK 用别的"）
    assert "btrim(name, E'{_BLANK_CHARS}')" in migration_sql, (
        "CHECK 表达式没有引用字符集常量：常量与约束会各说各话"
    )
    # 库里这份字符集必须全在 Python 的空白集内：库拒的名字，接口也拒。
    for char in DB_ROLE_NAME_BLANK_CHARS:
        assert char.isspace(), f"库层字符集里的 {char!r} 不是空白字符，两边语义对不上"
    assert "".join(DB_ROLE_NAME_BLANK_CHARS).strip() == "", "字符集本身全是空白，判据自相矛盾"

    # 接口层：全空白名一律拒，且落库前先 strip。
    for blank in ("", "   ", "\t", "\n", " \t\r\n\v\f ", "　"):
        with pytest.raises(ValidationError):
            RoleCreatePayload(code="library_keeper", name=blank)
    assert RoleCreatePayload(code="library_keeper", name="  资料管理员  ").name == "资料管理员"


def test_resettable_is_derived_by_the_server_not_by_is_system() -> None:
    """TD-200：`resettable` 与 `is_system` 是两件事，前端只认 `resettable`。

    今天"内置 ⇒ 有出厂定义"让两者恰好等价，但那是巧合：给某个内置角色撤掉定义行、
    或给自定义角色加"按模板初始化"，按 `is_system` 点亮的按钮就会亮着恒 409。

    **还要连"角色可编辑"这一道门一起算**：重置实际要过 `_assert_editable` → `ROLE_DEFAULTS_UNAVAILABLE`
    两道门。校方超管/学生/教师这三个内置锁定角色**都有**出厂定义，只算"有定义"会给它们
    点亮一个永远 409 的按钮——那正是 TD-200 要治的病换了个方向复发。
    """

    # 内置但没有出厂定义 → 不可重置（按钮必须不亮）
    builtin_without_defaults = _to_summary(_row("school_admin", is_system=True, resettable=False))
    assert builtin_without_defaults.resettable is False
    # 自定义但有出厂定义 → 可重置（按钮该亮）
    custom_with_defaults = _to_summary(_row("library_keeper", is_system=False, resettable=True))
    assert custom_with_defaults.resettable is True
    # 同一行上 `is_system` 与 `resettable` 可以各说各话——这正是要下发 `resettable` 的理由
    assert custom_with_defaults.is_system is False
    assert builtin_without_defaults.is_system is True

    # 内置锁定角色：有出厂定义也**不可**重置（`_assert_editable` 先拦）
    for locked_code in LOCKED_ROLE_REASONS:
        locked = _to_summary(_row(locked_code, is_system=True, resettable=True))
        assert locked.editable is False, locked_code
        assert locked.resettable is False, (
            f"{locked_code} 是锁定角色，重置永远 409：按钮不能亮"
        )
    # 内置可编辑角色（`school_admin`/`teacher_b_end`）有出厂定义时可重置
    for editable_code in EDITABLE_ROLE_CODES:
        editable = _to_summary(_row(editable_code, is_system=True, resettable=True))
        assert editable.resettable is True, editable_code


def test_custom_roles_are_editable_and_builtin_still_gated() -> None:
    """`is_system=false` 一律可编辑；内置角色按锁定名单与可编辑名单划分。"""

    assert _lock_reason("library_keeper", is_system=False) is None
    assert _lock_reason("school_admin", is_system=False) is None, (
        "内置码的锁定判断只能看 is_system：同码的自定义行不该被内置语义连坐"
    )
    for code, reason in LOCKED_ROLE_REASONS.items():
        assert _lock_reason(code, is_system=True) == reason
    for code in EDITABLE_ROLE_CODES:
        assert _lock_reason(code, is_system=True) is None
    # 既不在锁定名单也不在可编辑名单的内置角色（兜底分支）
    assert _lock_reason("future_builtin_role", is_system=True) == "该内置角色不在可编辑范围"
    assert _lock_reason("future_builtin_role", is_system=False) is None

    custom = _to_summary(_row("library_keeper", is_system=False))
    assert (custom.editable, custom.lock_reason, custom.is_system) == (True, None, False)
    builtin_editable = _to_summary(_row("school_admin", is_system=True))
    assert (builtin_editable.editable, builtin_editable.lock_reason) == (True, None)
    builtin_locked = _to_summary(_row("student", is_system=True))
    assert (builtin_locked.editable, builtin_locked.lock_reason) == (False, LOCKED_ROLE_REASONS["student"])


async def test_service_layer_rejects_blank_role_name() -> None:
    """直调 `create_role` 也要拒全空白名（TD-198）。

    接口层 Pydantic 已经拒了，但服务层不校验就等于"只靠调用方守规矩"：直调会一路走到 INSERT
    撞新 CHECK → `IntegrityError` → 500。判据与服务层其他入参错误同族（422、不落安全审计）。
    这条用例不碰库：名字判据排在任何写路径之前。
    """

    with pytest.raises(RoleAdminError) as caught:
        await admin.create_role(
            _principal(),
            code="library_keeper",
            name="   ",
            permission_codes=[],
            copy_from_role_id=None,
            reason=None,
            request_id=None,
        )
    assert (caught.value.code, caught.value.http_status) == ("ROLE_NAME_NOT_BLANK", 422)


async def test_copy_from_and_explicit_codes_are_mutually_exclusive() -> None:
    """双输入必须在服务层入口就 422，不能"任选一个静默执行"。"""

    with pytest.raises(RoleAdminError) as caught:
        await admin.create_role(
            _principal(),
            code="ambiguous_role",
            name="歧义角色",
            permission_codes=["school.kb.view"],
            copy_from_role_id=UUID(str(uuid4())),
            reason=None,
            request_id=None,
        )
    assert (caught.value.code, caught.value.http_status) == (
        "ROLE_INITIAL_SOURCE_AMBIGUOUS",
        422,
    )


async def test_platform_principal_cannot_create_or_delete_roles() -> None:
    """realm 守卫先于一切：平台主体既不建也不删租户角色，且不碰库。"""

    platform = Principal(
        user_id=uuid4(),
        realm="platform",
        tenant_id=None,
        jti=uuid4(),
        session_id=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        must_change_password=False,
        roles=frozenset({"platform_super_admin"}),
        permissions=frozenset({"school.role.manage"}),
    )
    for call in (
        admin.create_role(
            platform,
            code="cross_realm",
            name="跨 realm",
            permission_codes=[],
            copy_from_role_id=None,
            reason=None,
            request_id=None,
        ),
        admin.preview_role_deletion(platform, uuid4()),
        admin.delete_role(
            platform,
            uuid4(),
            confirm_role_code="cross_realm",
            reason="平台主体试图删租户角色",
            version=0,
            fingerprint="0" * 64,
            request_id=None,
        ),
    ):
        with pytest.raises(RoleAdminError) as caught:
            await call
        assert (caught.value.code, caught.value.http_status) == ("REALM_MISMATCH", 403)


def test_deletion_response_fields_mirror_the_service_preview() -> None:
    """服务层影响面与 API 响应模型字段一一对上。

    `_deletion_response` 是手写字段映射：服务层加一个字段而响应模型没跟上，前端就静默看不到
    （删除影响面里"少一个数字"等于少一项风险评估）。这条比"接口返回 200"更省事。
    """

    assert set(RoleDeletionPreviewResponse.model_fields) == set(
        admin.RoleDeletionPreview.__dataclass_fields__
    )


def test_role_create_payload_bounds_match_the_check_constraint() -> None:
    """接口按 3..63 收：按列宽 64 写会让 64 字符穿过接口、在 INSERT 撞库 CHECK 变成 500。"""

    def build(code: str) -> Any:
        return RoleCreatePayload(code=code, name="角色")

    for good in ("abc", "a_1", "library_keeper", "x" * 63):
        assert build(good).code == good, good
    for bad in ("ab", "a", "1digit", "Bad-Code", "含中文", "x" * 64, "-leading"):
        with pytest.raises(ValidationError):
            build(bad)


def test_role_create_payload_rejects_blank_name() -> None:
    """`min_length=1` 挡得住空串、挡不住全空格：服务层还会再 strip，于是落库成一行没有名字的角色。"""

    assert RoleCreatePayload(code="library_keeper", name="  图书馆管理员  ").name == "图书馆管理员"
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(ValidationError):
            RoleCreatePayload(code="library_keeper", name=blank)


def test_deletion_fingerprint_is_a_set_hash_scoped_to_the_role() -> None:
    """影响面指纹只由「角色 + 持有人集合」决定（TD-199）。

    三条性质各自挡住一类误判：顺序无关（数据库返回顺序不是契约）、人员搬走后集合相同即
    同指纹（"改绑又解回原集合"必须能正常删除）、`role_id` 入哈希（把 A 的所有人搬给 B 后
    两个角色的指纹不能撞——否则拿着 A 的预览能删 B）。
    """

    role_a = uuid4()
    role_b = uuid4()
    first = uuid4()
    second = uuid4()

    baseline = _deletion_fingerprint(role_a, [first, second])
    assert baseline == _deletion_fingerprint(role_a, [second, first])
    assert baseline == _deletion_fingerprint(role_a, [first, second])
    assert len(baseline) == 64 and set(baseline) <= set("0123456789abcdef")

    assert _deletion_fingerprint(role_a, [first]) != baseline
    assert _deletion_fingerprint(role_a, []) != baseline
    assert _deletion_fingerprint(role_b, [first, second]) != baseline
    # 空集合也带 role_id：两个都没有持有人的角色不能共用同一个指纹
    assert _deletion_fingerprint(role_a, []) != _deletion_fingerprint(role_b, [])


def test_role_summary_bindable_flags_only_the_super_admin_role() -> None:
    """`bindable` 是派生字段而不是库列：只由角色码决定，自定义角色即使与内置码同名也照常可绑。"""

    assert _to_summary(_row(SUPER_ADMIN_ROLE_CODE, is_system=True)).bindable is False
    assert _to_summary(_row("school_admin", is_system=True)).bindable is True
    assert _to_summary(_row("library_keeper", is_system=False)).bindable is True
