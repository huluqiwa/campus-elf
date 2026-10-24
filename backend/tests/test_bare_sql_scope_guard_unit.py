"""裸 SQL / 裸 ORM 绕过仓储基类的守卫（简报 §2-E，用户口径 ④）。

数据范围过滤只在 `TenantScopedRepository` 里注入一次，因此**任何**直接查租户级表的模块
都必须被显式登记在这里：新增一处直连查询 = 本测试变红，逼作者要么走仓储（自带范围谓词），
要么写清「为什么这里可以不带范围」并让复审看到。豁免清单同时限制条目上限，
防止清单变成随手登记的通行证墙。
"""

import re
from pathlib import Path
from typing import Any

from app.db.base import Base, TenantScopedMixin

BACKEND_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND_ROOT / "app"

# 允许直连租户级表的模块 → (理由, 允许触碰的表)。
# 理由必须写清"为什么它不需要数据范围"，而不是"它就是不需要"。
SCOPE_EXEMPTIONS: dict[str, dict[str, str]] = {
    "app/db/repository.py": {"*": "仓储基类本身：范围谓词与租户过滤在这里生成"},
    "app/auth/principal.py": {
        "*": (
            "认证主体解析：读的是身份与授权判据本身（users/user_roles/roles/role_permissions），"
            "数据范围判据也从这里产出，不能反过来被范围限制"
        )
    },
    "app/auth/service.py": {
        "*": (
            "登录/改密/锁定/MFA：全部按 user_id 精确操作本人凭证行，"
            "属于 self 范围内唯一可见的那一行，无需角色级范围"
        )
    },
    "app/auth/sessions.py": {
        "refresh_tokens": "会话设备管理：只操作调用者自己的刷新令牌族（按 user_id 过滤）"
    },
    "app/db/selfcheck.py": {
        "*": "启动自检：迁移后一次性只读核对，无请求上下文，因此没有可绑定的范围"
    },
    "app/rbac/__init__.py": {
        "*": "RBAC 定义同步：管理连接上的幂等维护任务，运行时无租户上下文"
    },
    "app/workers/tasks.py": {
        "refresh_tokens": "后台清理任务：跨租户清理过期令牌，属运维写路径而非业务读路径"
    },
    "app/org/scopes_sync.py": {
        "*": (
            "class 范围行同步（M2-ORG01-03 D1）：按租户聚合组织关系推导期望范围行并差量增删，"
            "无单一请求主体的范围语义（与 rbac 定义同步同类）；写路径只在组织关系变更的同一事务内调用"
        )
    },
}

# 豁免条目上限：超过就说明"直连"正在变成常态，必须回头收敛到仓储。
MAX_EXEMPT_MODULES = 8

_TABLE_RE = re.compile(r"\b(?:from|into|update|join)\s+(?:public\.)?([a-z_]+)\b", re.IGNORECASE)
_ORM_CALL_TEMPLATE = r"\b(?:select|insert|update|delete|get|exists|scalar)\s*\(\s*(?:[A-Za-z_]+\.)?{name}\b"


def _tenant_scoped_tables() -> tuple[list[str], dict[str, str]]:
    """(租户级表名, 模型类名 → 表名)：从 `TenantScopedMixin` 推，避免手工清单与模型漂移。"""

    tables: list[str] = []
    class_to_table: dict[str, str] = {}
    for mapper in Base.registry.mappers:
        model: Any = mapper.class_
        if issubclass(model, TenantScopedMixin):
            table: str = model.__table__.name
            tables.append(table)
            class_to_table[model.__name__] = table
    return sorted(set(tables)), class_to_table


def _touching_tables(relative: str, class_to_table: dict[str, str], tables: list[str]) -> set[str]:
    """这个源文件引用了哪些租户级表（裸 SQL 表名 + ORM 实体两种形态）。"""

    source = (BACKEND_ROOT / relative).read_text(encoding="utf-8")
    found = {match.group(1).lower() for match in _TABLE_RE.finditer(source)} & set(tables)
    for class_name, table in class_to_table.items():
        if re.search(_ORM_CALL_TEMPLATE.format(name=class_name), source):
            found.add(table)
    return found


def test_exemption_list_only_names_existing_tenant_tables() -> None:
    """豁免清单里登记的表必须真实存在：防止模型改名/删表后清单还留着幽灵条目。"""

    tables, _ = _tenant_scoped_tables()
    known = {*tables, "*"}
    for module, allowed in SCOPE_EXEMPTIONS.items():
        for table in allowed:
            assert table in known, f"{module} 豁免了不存在的表 {table}"
        assert Path(BACKEND_ROOT / module).exists(), f"{module} 已不存在，请从豁免清单删除"


def test_exemption_list_size_is_bounded() -> None:
    """豁免模块数量有上限：新增直连点必须同时抬高上限，等于强迫它在复审里被讨论。"""

    assert len(SCOPE_EXEMPTIONS) <= MAX_EXEMPT_MODULES, (
        f"直连租户级表的模块已达 {len(SCOPE_EXEMPTIONS)} 个（上限 {MAX_EXEMPT_MODULES}）；"
        "请改为通过 TenantScopedRepository 访问，或在简报里说明为什么不能"
    )


def test_only_exempted_modules_touch_tenant_tables() -> None:
    """核心断言：除豁免清单外，`app/` 里不得有任何模块直接查租户级表。"""

    tables, class_to_table = _tenant_scoped_tables()
    offenders: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        relative = path.relative_to(BACKEND_ROOT).as_posix()
        found = _touching_tables(relative, class_to_table, tables)
        if not found:
            continue
        allowed = SCOPE_EXEMPTIONS.get(relative)
        if allowed is None:
            offenders.append(f"{relative} 直接访问 {sorted(found)}，但未登记豁免，也未走仓储")
            continue
        if "*" in allowed:
            continue
        unexpected = sorted(found - set(allowed))
        if unexpected:
            offenders.append(f"{relative} 访问了未豁免的表 {unexpected}")

    assert not offenders, "数据范围只能经仓储基类注入（简报 §2-E）：\n" + "\n".join(offenders)


def test_business_read_paths_are_not_exempted() -> None:
    """业务读接口（api/ 与业务服务）不得出现在豁免清单里：它们必须走仓储。"""

    for module in SCOPE_EXEMPTIONS:
        assert not module.startswith("app/api/"), f"接口层 {module} 不应直连租户表"
