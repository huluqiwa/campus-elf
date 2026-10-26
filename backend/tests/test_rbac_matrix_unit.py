"""M1-E1 单元：权限矩阵文档与代码权限目录/出厂默认的一致性。

为什么拿文档当断言源：AGENTS.md §1.2 规定 Spec 是唯一事实来源，
`docs/rbac-matrix.md` §3/§4 就是权限码与角色映射的口径。把"文档说了什么"
写成可执行的测试，才能在后续里程碑加接口时挡住目录漂移（新增码没进矩阵、
矩阵改了码没进库这类问题只能靠机器发现）。

纯单元测试：不连数据库，只读文档、Python 常量与 `alembic/versions` 的迁移源码文本。
"""

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

from app.rbac import build_sync_statements
from app.rbac.catalog import PERMISSION_CATALOG, catalog_codes, code_realm, codes_with_module
from app.rbac.seeds import (
    PLATFORM_BUILTIN_ROLES,
    TENANT_BUILTIN_ROLES,
    BuiltinRole,
)

MATRIX_DOC = Path(__file__).resolve().parents[2] / "docs" / "rbac-matrix.md"
VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"
E1_REVISION = "e5f7a9c1b3d5"
E1_FILE = "e5f7a9c1b3d5_m1e1_rbac_matrix.py"

# §3 各表的码都写在第一列的反引号里：| `platform.tenant.view` | 说明 |
_FIRST_COL_CODE = re.compile(r"^\|\s*`([^`]+)`\s*\|")
# 与 permissions.code 的 String(64)/description 的 String(200) 对齐
_CODE_SHAPE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+){1,3}")


def _section(heading: str) -> str:
    """取矩阵文档某个 `## ` 小节正文（到下一个 `## ` 为止）。"""

    text = MATRIX_DOC.read_text(encoding="utf-8")
    start = text.index(heading)
    tail = text[start:]
    nxt = tail.find("\n## ", len(heading))
    return tail if nxt == -1 else tail[:nxt]


def _expand(raw: str) -> tuple[str, ...]:
    """展开矩阵里用斜线合并的同前缀权限点。

    `school.personnel.create/edit/delete` → 三条完整码；
    首段决定前缀，其余段共用该前缀。
    """

    parts = raw.split("/")
    head = parts[0]
    prefix = head[: head.rindex(".") + 1]
    return (head, *(prefix + rest for rest in parts[1:]))


def matrix_permission_codes() -> set[str]:
    """矩阵 §3 声明的全部权限码（已展开斜线合并项）。"""

    codes: set[str] = set()
    for line in _section("## 3. 权限点").splitlines():
        match = _FIRST_COL_CODE.match(line.strip())
        if match:
            codes.update(_expand(match.group(1)))
    return codes


# §4 表格的列顺序（第 3 列起）：文档加了列、改了顺序都必须先在这里同步，否则测试报"列数不齐"
_SECTION4_COLUMNS: tuple[tuple[str, str], ...] = (
    ("平台超管", "platform_super_admin"),
    ("平台运营", "platform_operator"),
    ("平台审计", "platform_auditor"),
    ("校方超管", "school_super_admin"),
    ("校方管理员", "school_admin"),
    ("教师 B 端", "teacher_b_end"),
    ("学生 C 端", "student"),
    ("教师 C 端", "teacher"),
)
_ALLOW = "√"
_CONDITIONAL = "△"
_NONE = "—"
_ROW_CODE = re.compile(r"`([^`]+)`")
_SEPARATOR_CELL = re.compile(r":?-{2,}:?")


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _section4_table() -> list[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    """§4 矩阵表 → (权限组, 该行展开后的权限码, 八个角色列的标记)。"""

    table_lines = [
        line
        for line in _section("## 4. 角色-权限矩阵").splitlines()
        if line.strip().startswith("|")
    ]
    assert table_lines, "矩阵 §4 没有解析到表格"
    header = _cells(table_lines[0])
    assert header[0] == "权限组", f"§4 表格第一列应是权限组，实际：{header[:3]}"
    assert tuple(name for name, _ in _SECTION4_COLUMNS) == tuple(header[2:]), (
        f"§4 角色列与测试的列口径不一致（文档改了列名/顺序？）：{header[2:]}"
    )
    rows: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    for line in table_lines[1:]:
        cells = _cells(line)
        assert len(cells) == len(header), f"§4 表格列数不齐（应为 {len(header)} 列）：{line}"
        if all(_SEPARATOR_CELL.fullmatch(cell) for cell in cells):
            continue
        codes: list[str] = []
        for raw in _ROW_CODE.findall(cells[1]):
            codes.extend(_expand(raw))
        rows.append((cells[0], tuple(codes), tuple(cells[2:])))
    assert rows, "§4 表格只有表头"
    return rows


def matrix_section4_grants() -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """§4 每个角色列的 (√ 码集合, △ 码集合)。"""

    allow: dict[str, set[str]] = {cell: set() for _, cell in _SECTION4_COLUMNS}
    conditional: dict[str, set[str]] = {cell: set() for _, cell in _SECTION4_COLUMNS}
    for group, codes, marks in _section4_table():
        for (column, role_code), mark in zip(_SECTION4_COLUMNS, marks, strict=True):
            if mark not in {_ALLOW, _CONDITIONAL, _NONE}:
                raise AssertionError(f"§4「{group}」的「{column}」列出现未知标记 {mark!r}")
            for code in codes:
                if mark == _ALLOW:
                    allow[role_code].add(code)
                elif mark == _CONDITIONAL:
                    conditional[role_code].add(code)
    return {
        role_code: (frozenset(allow[role_code]), frozenset(conditional[role_code]))
        for role_code in allow
    }


def _builtin_role(code: str) -> BuiltinRole:
    """按码取内置角色（平台/租户两个清单合起来找）。"""

    return _role((*PLATFORM_BUILTIN_ROLES, *TENANT_BUILTIN_ROLES), code)


def _section4_expected(role_code: str) -> frozenset[str]:
    """角色列应当打 √ 的码集合。

    默认取出厂默认（`seeds.py`）；只有平台超管例外：矩阵的列回答的是"这个身份用得上吗"，
    而它的默认按 §5-11 是目录全量。租户域码对它不可用（平台主体走校方接口会被 realm 守卫
    拒绝），所以列口径取平台运维域全量，"持有码"由 `test_platform_super_admin_holds_every_code` 单独钉。
    """

    if role_code == "platform_super_admin":
        return frozenset(code for code in catalog_codes() if code_realm(code) == "platform")
    return _builtin_role(role_code).permissions


def _role(roles: tuple[BuiltinRole, ...], code: str) -> BuiltinRole:
    found = [role for role in roles if role.code == code]
    assert found, [role.code for role in roles]
    return found[0]


_REVISION_DECL = re.compile(r"^revision: str = ['\"]([^'\"]+)['\"]", re.M)
_DOWN_REVISION_DECL = re.compile(r'^down_revision: str \| None = (["\'])([^"\']+)\1', re.M)
# 开通路径的两个 SECURITY DEFINER 函数：谁重写它们，就必须继续补齐内置角色
_CREATE_PROVISIONING_FUNCTION = re.compile(
    r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+(?:public\.)?(provision_tenant|review_registration_request)\b",
    re.IGNORECASE,
)
_SEED_CALL = re.compile(r"PERFORM\s+(?:public\.)?seed_builtin_roles\s*\(", re.IGNORECASE)


def _migration_sources() -> dict[str, tuple[str, str]]:
    """revision → (文件名, 源码文本)；只读文件，不导入模块。"""

    found: dict[str, tuple[str, str]] = {}
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        match = _REVISION_DECL.search(source)
        assert match, f"{path.name} 没有 `revision: str = ...` 声明"
        found[match.group(1)] = (path.name, source)
    return found


def _chain_order() -> list[str]:
    """按 down_revision 串出的线性迁移链（本仓库约定单头无分支）。"""

    sources = _migration_sources()
    parents: dict[str, str | None] = {}
    for revision, (_, source) in sources.items():
        match = _DOWN_REVISION_DECL.search(source)
        parents[revision] = match.group(2) if match else None
    roots = [revision for revision, parent in parents.items() if parent is None]
    assert len(roots) == 1, f"迁移链应有且只有一个根，实际：{roots}"
    children: dict[str, list[str]] = {}
    for revision, parent in parents.items():
        if parent is not None:
            assert parent in sources, f"{revision} 的 down_revision {parent} 不在链上"
            children.setdefault(parent, []).append(revision)
    order: list[str] = [roots[0]]
    while True:
        nxt = children.get(order[-1], [])
        assert len(nxt) <= 1, f"{order[-1]} 之后出现分支：{nxt}（清单测试假设线性链）"
        if not nxt:
            return order
        order.append(nxt[0])


def _load_migration_module(filename: str) -> ModuleType:
    """按文件名加载迁移模块（用于取其构造 SQL 的纯函数）。"""

    path = VERSIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(f"migration_source_{path.stem}", path)
    assert spec is not None and spec.loader is not None, f"无法加载迁移：{path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_matrix_parser_expands_merged_rows() -> None:
    """解析器把斜线合并项展开成完整码，且归一后的历史别名不再出现。"""

    codes = matrix_permission_codes()

    assert len(codes) >= 55, f"矩阵 §3 解析出的码过少，先确认解析器还能对上表格：{len(codes)}"
    assert {"school.personnel.create", "school.personnel.edit", "school.personnel.delete"} <= codes
    assert {"campus.leave.create", "campus.leave.withdraw"} <= codes
    # 口径 ⑦：`campus.chat` 已归一为 `campus.chat.use`；
    # `audit.view`/`audit.export` 与 realm 专用码重复，已从矩阵删除
    assert "campus.chat" not in codes
    assert "audit.view" not in codes and "audit.export" not in codes


def test_catalog_covers_every_matrix_code() -> None:
    """矩阵里声明的每个权限点都必须存在于代码目录（缺失=接口无对应校验点或目录漂移）。"""

    missing = sorted(matrix_permission_codes() - set(catalog_codes()))

    assert missing == [], f"权限目录缺少矩阵已声明的权限点：{missing}"


def test_catalog_has_no_undocumented_code() -> None:
    """目录里不得出现矩阵没写的码：矩阵是唯一口径（口径 ⑧）。"""

    extra = sorted(set(catalog_codes()) - matrix_permission_codes())

    assert extra == [], f"权限目录存在矩阵未声明的码，请补矩阵或删除：{extra}"


def test_catalog_codes_unique_and_fit_columns() -> None:
    """码唯一、形状符合 `模块.动作`，且长度不超库字段。"""

    codes = [entry.code for entry in PERMISSION_CATALOG]

    assert len(codes) == len(set(codes))
    assert all(_CODE_SHAPE.fullmatch(code) for code in codes), [
        code for code in codes if not _CODE_SHAPE.fullmatch(code)
    ]
    assert all(len(code) <= 64 for code in codes)
    assert all(entry.description and len(entry.description) <= 200 for entry in PERMISSION_CATALOG)


def test_catalog_module_and_action_derive_from_code() -> None:
    """module/action 由码本身派生（不另设第二份标注，避免与码漂移）。"""

    for entry in PERMISSION_CATALOG:
        assert entry.module == entry.code.split(".", 1)[0]
        assert entry.action == entry.code.rsplit(".", 1)[1]

    assert {entry.module for entry in PERMISSION_CATALOG} == {
        "platform",
        "school",
        "campus",
        "observability",
        "eval",
    }


def test_builtin_roles_reference_only_catalog_codes() -> None:
    """角色映射不得引用幽灵权限（界面能勾、后端没有对应逻辑=假开关）。"""

    known = set(catalog_codes())
    offenders = {
        f"{role.realm}/{role.code}: {sorted(role.permissions - known)}"
        for role in (*PLATFORM_BUILTIN_ROLES, *TENANT_BUILTIN_ROLES)
        if role.permissions - known
    }

    assert offenders == set(), sorted(offenders)


def test_tenant_roles_never_grant_platform_module() -> None:
    """租户角色不得引用 `platform.*`（与 DB 触发器 + 启动自检同一口径，纯函数层提前拦截）。"""

    for role in TENANT_BUILTIN_ROLES:
        leaked = sorted(code for code in role.permissions if code.split(".", 1)[0] == "platform")
        assert role.realm == "tenant"
        assert leaked == [], f"{role.code} 引用了平台权限：{leaked}"

    for role in PLATFORM_BUILTIN_ROLES:
        assert role.realm == "platform"


def test_school_super_admin_equals_school_and_campus_modules() -> None:
    """超管默认=本校全量（与 provision_tenant 的 `module IN ('school','campus')` 同一语义）。"""

    super_admin = _role(TENANT_BUILTIN_ROLES, "school_super_admin")

    assert super_admin.permissions == codes_with_module("school") | codes_with_module("campus")


def test_platform_super_admin_holds_every_code() -> None:
    """平台超管持有目录全量（矩阵 §5-11：平台是超集运营角色）。"""

    assert _role(PLATFORM_BUILTIN_ROLES, "platform_super_admin").permissions == catalog_codes()


def test_matrix_open_questions_pinned_in_defaults() -> None:
    """矩阵 §6 定稿的 7 条口径逐条钉住（改默认权限集会立刻撞到这条）。"""

    admin = _role(TENANT_BUILTIN_ROLES, "school_admin")
    # 1：授权管理员默认不含 KB 审批/发布（△=需超管显式授予）
    assert "school.kb.approval" not in admin.permissions
    assert "school.kb.publish" not in admin.permissions
    # 管理员默认只有矩阵"校方管理员"列打 √ 的项
    assert admin.permissions == {
        "school.dashboard.view",
        "school.personnel.view",
        "school.kb.view",
        "school.message.view",
        "school.message.send",
        "school.message.read",
    }

    teacher_b = _role(TENANT_BUILTIN_ROLES, "teacher_b_end")
    # 2：班主任/辅导员不自动获得请假审批
    assert "school.approval.leave" not in teacher_b.permissions
    # 3（改后口径 ⑤）：持后台账号即可上传知识库
    assert "school.kb.upload" in teacher_b.permissions

    operator = _role(PLATFORM_BUILTIN_ROLES, "platform_operator")
    # 4：运营不能改模型密钥/配置
    assert "platform.config.edit" not in operator.permissions
    # 5：审计导出不给运营
    assert "platform.audit.export" not in operator.permissions

    auditor = _role(PLATFORM_BUILTIN_ROLES, "platform_auditor")
    assert "platform.audit.export" in auditor.permissions


def test_campus_roles_match_matrix_columns() -> None:
    """C 端角色：学生=全部 campus 码；教师不含请假（矩阵 C 端请假列学生 √、教师 —）。"""

    campus = codes_with_module("campus")

    assert _role(TENANT_BUILTIN_ROLES, "student").permissions == campus
    assert _role(TENANT_BUILTIN_ROLES, "teacher").permissions == campus - {
        "campus.leave.create",
        "campus.leave.withdraw",
    }


def test_matrix_section4_rows_partition_the_catalog() -> None:
    """§4 每行的权限码合起来必须**不重不漏**地等于目录 60 码（第三轮审查 M1 的机器口径）。

    只比对"矩阵声明的码 ⊆ 目录"不够：§3 有码、§4 没有对应行时，该权限的授予口径无人声明
    （本次就漏了 `school.kb.delete`、`school.account_termination.*`、`campus.conversation.clear`、
    `campus.nav`）。分区断言把这类"文档写了码但没进矩阵"直接变成失败。
    """

    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for group, codes, _ in _section4_table():
        assert codes, f"§4「{group}」没有声明对应权限码"
        for code in codes:
            if code in seen:
                duplicates.append(f"{code}：「{seen[code]}」与「{group}」")
            seen[code] = group

    assert duplicates == [], f"§4 同一权限码出现在多行：{duplicates}"
    assert set(seen) == set(catalog_codes()), (
        f"§4 行并集与目录不相等 只在矩阵：{sorted(set(seen) - set(catalog_codes()))} "
        f"只在目录：{sorted(set(catalog_codes()) - set(seen))}"
    )


@pytest.mark.parametrize("role_code", [cell for _, cell in _SECTION4_COLUMNS])
def test_matrix_section4_allow_marks_equal_the_role_permission_set(role_code: str) -> None:
    """每个角色列的 √ 集合 = 该角色的权限口径（5 个常规角色 = 出厂默认，两个全量角色见 helper）。

    这条是"文档说的和库里种的一样"的唯一防线：改了 `seeds.py` 的默认没改矩阵、或改了矩阵
    没改默认，都会在这里失败（第三轮审查 M1：§4 此前只是人读的表格）。
    """

    allow, _ = matrix_section4_grants()[role_code]
    expected = _section4_expected(role_code)

    assert allow == expected, (
        f"§4「{role_code}」列 √ 与权限口径不一致 "
        f"只在矩阵 √：{sorted(allow - expected)} 只在口径：{sorted(expected - allow)}"
    )


@pytest.mark.parametrize("role_code", [cell for _, cell in _SECTION4_COLUMNS])
def test_matrix_section4_conditional_marks_are_not_granted_by_default(role_code: str) -> None:
    """△ = 可授予但**默认不给**（§4 说明）；默认已含的码不允许再标 △。

    否则同一格既被读成"出厂即有"又被读成"需显式授予"，M2 配置界面照文档实现时会二义。
    两个全量角色列按此规则也不得出现 △：它们的范围定义里没有"待授权"这一档。
    """

    _, conditional = matrix_section4_grants()[role_code]

    assert conditional - _builtin_role(role_code).permissions == conditional, (
        f"§4「{role_code}」列把默认已授予的码标成了 △：{sorted(conditional)}"
    )


def test_matrix_section4_tenant_columns_only_use_tenant_realm_codes() -> None:
    """租户五个角色列的 √/△ 只能是 `school`/`campus` 域码（2026-10-23 用户定稿）。

    `observability.*`/`eval.*` 属平台运维域，此前矩阵给"校方管理员"打了 △，与
    `catalog.code_realm()`、种子自检和同步守卫冲突；本条把"不下放租户角色"钉成断言。
    """

    grants = matrix_section4_grants()
    offenders: list[str] = []
    for _, role_code in _SECTION4_COLUMNS[3:]:
        allow, conditional = grants[role_code]
        for code in sorted(allow | conditional):
            if code_realm(code) != "tenant":
                offenders.append(f"{role_code}: {code}")

    assert offenders == [], f"租户角色列引用了平台运维域权限：{offenders}"


def test_builtin_role_codes_are_the_matrix_role_set() -> None:
    """内置角色集合与矩阵 §1 一致：平台 3 + 租户 5（含 C 端 student/teacher）。"""

    assert {role.code for role in PLATFORM_BUILTIN_ROLES} == {
        "platform_super_admin",
        "platform_operator",
        "platform_auditor",
    }
    assert {role.code for role in TENANT_BUILTIN_ROLES} == {
        "school_super_admin",
        "school_admin",
        "teacher_b_end",
        "student",
        "teacher",
    }
    assert {role.realm for role in TENANT_BUILTIN_ROLES} == {"tenant"}


def test_catalog_module_index_matches_entries() -> None:
    """`codes_with_module` 与目录条目一致（provision_tenant 的 SQL 按 module 过滤，靠它对齐）。"""

    for module in ("platform", "school", "campus", "observability", "eval"):
        assert codes_with_module(module) == frozenset(
            entry.code for entry in PERMISSION_CATALOG if entry.module == module
        )

    # 目录里没有 audit 模块（口径 ⑦：与 realm 专用码重复的两条已删）
    assert codes_with_module("audit") == frozenset()


def test_e1_provisioning_bodies_delegate_builtin_role_seeding() -> None:
    """E1 重写的两个开通函数只调用 `seed_builtin_roles`，不再自己抄一份授权 SQL。

    生产开通走审核函数、DEV 开通走 provision：两条路径必须同源，否则新租户的权限
    因入口不同而不同（简报 §3.1-B 的选型理由）。
    """

    module = _load_migration_module(E1_FILE)
    # 模块动态加载，两个构造函数只能按名字从命名空间取（避免 getattr 常量属性的 lint 噪声）
    bodies = {
        "provision_tenant": str(module.__dict__["_provision_body"]()),
        "review_registration_request": str(module.__dict__["_review_body"]()),
    }
    for name, sql in bodies.items():
        assert _CREATE_PROVISIONING_FUNCTION.search(
            sql
        ), f"{name} 的新版函数体不再 CREATE OR REPLACE 自身"
        assert _SEED_CALL.search(sql), f"{name} 没有补齐内置角色（seed_builtin_roles 调用丢失）"
        assert "INSERT INTO public.roles" not in sql, f"{name} 又抄了一份建角色 SQL（会与种子函数漂移）"


def test_migrations_after_e1_keep_provisioning_functions_seeded() -> None:
    """E1 之后任何重写开通函数的迁移，必须继续调用 `seed_builtin_roles`。

    这类回归是"复制旧迁移改改"产生的：新函数体退回自己 INSERT roles 的老写法，
    迁移照样绿，但新开通的租户从此只有 3 个角色、缺 C 端 `student`/`teacher`。

    **本期它是空转的**（E1 就是链头，循环体一次都不执行）：这是一条前置防线，
    价值在 M1-E2 之后有人动这两个函数时才显现；`test_e1_provisioning_bodies_delegate_builtin_role_seeding`
    才是本步对现状的断言。
    """

    order = _chain_order()
    assert E1_REVISION in order, "M1-E1 迁移不在链上（改名或回滚后请同步本清单）"
    sources = _migration_sources()
    for revision in order[order.index(E1_REVISION) + 1 :]:
        filename, source = sources[revision]
        if _CREATE_PROVISIONING_FUNCTION.search(source):
            assert _SEED_CALL.search(source), (
                f"{filename} 重写了 provision_tenant/review_registration_request，"
                "但没有调用 seed_builtin_roles：新租户将缺内置角色与出厂默认映射"
            )


def test_build_sync_statements_orders_legacy_deletes_first() -> None:
    """语句顺序：先删旧码映射，再删码/补目录（反过来会撞 `role_permissions` 的外键）。"""

    statements = build_sync_statements()

    assert statements, "同步语句不应为空"
    assert "delete from public.role_permissions" in statements[0][0]


@pytest.mark.parametrize(
    ("bad_code", "expected"),
    [
        ("school.nosuch.permission", "目录外的权限码"),
        ("platform.tenant.view", "引用了租户域"),
        ("observability.trace.view", "引用了租户域"),
    ],
)
def test_build_sync_statements_actually_runs_the_seed_self_check(
    monkeypatch: pytest.MonkeyPatch, bad_code: str, expected: str
) -> None:
    """接线断言：坏种子必须在**构建语句**阶段就被挡住（第三轮审查 M2）。

    只测 `_self_check_seeds` 里的常量不够 —— 把 `build_sync_statements()` 里那一行调用删掉，
    迁移和 M2 运维入口就会带着幽灵码/越域码一路写进库。`observability.*` 是改造时真实
    踩过的形态：它不带 `platform` 前缀，只按前缀判分的实现会放它进租户角色。
    """

    bad_role = BuiltinRole(
        code="broken_builtin",
        name="坏种子角色",
        realm="tenant",
        permissions=frozenset({bad_code}),
    )
    monkeypatch.setattr("app.rbac.TENANT_BUILTIN_ROLES", (*TENANT_BUILTIN_ROLES, bad_role))

    with pytest.raises(RuntimeError, match=expected):
        build_sync_statements()
