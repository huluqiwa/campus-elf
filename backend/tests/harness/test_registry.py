"""能力/工具注册表测试。"""

from uuid import uuid4

import pytest

from app.harness.contracts import TenantContext, ToolCall, ToolResult, ToolSpec
from app.harness.registry import CapabilityRegistry, CapabilitySpec, ToolRegistry


def tenant(**kwargs: object) -> TenantContext:
    """构造测试租户上下文。"""

    return TenantContext(tenant_id=uuid4(), user_id=uuid4(), **kwargs)


def tool_spec(name: str = "kb.search", **kwargs: object) -> ToolSpec:
    """构造测试工具定义。"""

    fields: dict[str, object] = {
        "version": "1",
        "description": "KB search",
        "side_effect": False,
        "permission": "kb.read",
        "data_scope": "self",
        "timeout_ms": 1000,
    }
    fields.update(kwargs)
    return ToolSpec(name=name, **fields)


def test_capability_registry_filters_permission_and_flag() -> None:
    """能力可用性由权限与功能开关共同决定。"""

    registry = CapabilityRegistry()
    registry.register(CapabilitySpec(name="kb_qa", description="KB"))
    registry.register(
        CapabilitySpec(
            name="personal_kb",
            description="personal",
            permission="campus.personal_kb",
            feature_flag="ENABLE_PERSONAL_KB",
        )
    )

    ctx = tenant(permissions=frozenset({"campus.personal_kb"}))
    assert [c.name for c in registry.available(ctx)] == ["kb_qa"]

    ctx_with_flag = tenant(
        permissions=frozenset({"campus.personal_kb"}),
        feature_flags={"ENABLE_PERSONAL_KB": True},
    )
    assert [c.name for c in registry.available(ctx_with_flag)] == ["kb_qa", "personal_kb"]


def test_capability_registry_rejects_duplicate() -> None:
    """重复能力注册必须失败。"""

    registry = CapabilityRegistry()
    spec = CapabilitySpec(name="kb_qa", description="KB")
    registry.register(spec)
    with pytest.raises(ValueError):
        registry.register(spec)


async def _handler(call: ToolCall, ctx: TenantContext) -> ToolResult:
    """测试工具处理器。"""

    return ToolResult(ok=True, data=call.args)


def test_tool_registry_rejects_duplicate_and_missing() -> None:
    """重复工具注册失败；缺失工具抛 KeyError。"""

    registry = ToolRegistry()
    spec = tool_spec()
    registry.register(spec, _handler)
    with pytest.raises(ValueError):
        registry.register(spec, _handler)
    with pytest.raises(KeyError):
        registry.get_spec("kb.missing")


def test_tool_registry_rejects_write_tool_by_default() -> None:
    """MVP 默认拒绝注册写工具。"""

    registry = ToolRegistry()
    with pytest.raises(ValueError):
        registry.register(tool_spec(side_effect=True), _handler)


def test_tool_registry_rejects_invalid_schema() -> None:
    """注册期校验 schema 合法性。"""

    registry = ToolRegistry()
    with pytest.raises(ValueError):
        registry.register(tool_spec(input_schema={"type": "not-a-type"}), _handler)


def test_tool_registry_available_filters_tenant_and_scope() -> None:
    """停用租户与范围不足时不返回工具。"""

    registry = ToolRegistry()
    registry.register(tool_spec(data_scope="school"), _handler)
    assert registry.available(tenant(tenant_status="suspended")) == ()
    assert registry.available(tenant(permissions=frozenset({"kb.read"}))) == ()
    assert [
        s.name
        for s in registry.available(
            tenant(permissions=frozenset({"kb.read"}), data_scopes=("school",))
        )
    ] == ["kb.search"]


def test_tool_registry_available_filters_permission_write_and_flag() -> None:
    """可用工具排除写工具，并按权限/开关过滤。"""

    registry = ToolRegistry()
    registry.register(tool_spec(), _handler)
    registry.register(tool_spec(name="kb.write", side_effect=True), _handler, allow_write=True)
    registry.register(
        tool_spec(name="kb.flagged", availability_flag="ENABLE_KB"),
        _handler,
    )
    assert registry.available(tenant()) == ()
    assert [
        s.name
        for s in registry.available(
            tenant(permissions=frozenset({"kb.read"}), data_scopes=("self",))
        )
    ] == ["kb.search"]
    assert [
        s.name
        for s in registry.available(
            tenant(
                permissions=frozenset({"kb.read"}),
                data_scopes=("self",),
                feature_flags={"ENABLE_KB": True},
            )
        )
    ] == ["kb.flagged", "kb.search"]
