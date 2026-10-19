"""能力与工具注册表。

注册表只负责声明与可用性过滤；实际执行策略在 ToolExecutor / Orchestrator。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import jsonschema

from app.harness.contracts import TenantContext, ToolCall, ToolResult, ToolSpec

ToolHandler = Callable[[ToolCall, TenantContext], Awaitable[ToolResult]]

_SCOPE_RANK: dict[str, int] = {
    "none": 0,
    "self": 1,
    "course": 2,
    "class": 3,
    "school": 4,
}


def scope_allows(required: str, ctx: TenantContext) -> bool:
    """判断用户数据范围是否覆盖工具要求；范围可向下兼容。"""

    if required == "none":
        return True
    required_rank = _SCOPE_RANK[required]
    return any(_SCOPE_RANK.get(scope, -1) >= required_rank for scope in ctx.data_scopes)


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """用户可感知的能力声明。"""

    name: str
    description: str
    permission: str | None = None
    feature_flag: str | None = None


class CapabilityRegistry:
    """能力注册表。"""

    def __init__(self) -> None:
        """初始化空注册表。"""

        self._items: dict[str, CapabilitySpec] = {}

    def register(self, spec: CapabilitySpec) -> None:
        """注册能力；重复名称抛 ValueError。"""

        if spec.name in self._items:
            raise ValueError(f"capability already registered: {spec.name}")
        self._items[spec.name] = spec

    def get(self, name: str) -> CapabilitySpec:
        """按名称取能力；不存在抛 KeyError。"""

        return self._items[name]

    def all(self) -> tuple[CapabilitySpec, ...]:
        """返回全部能力，按名称排序。"""

        return tuple(sorted(self._items.values(), key=lambda item: item.name))

    def available(self, ctx: TenantContext) -> tuple[CapabilitySpec, ...]:
        """按租户状态、权限与功能开关返回当前可用能力。"""

        if ctx.tenant_status != "active":
            return ()
        result: list[CapabilitySpec] = []
        for spec in self._items.values():
            if spec.permission is not None and spec.permission not in ctx.permissions:
                continue
            if spec.feature_flag is not None and not ctx.feature_flags.get(
                spec.feature_flag, False
            ):
                continue
            result.append(spec)
        return tuple(sorted(result, key=lambda item: item.name))


class ToolRegistry:
    """工具注册表。"""

    def __init__(self) -> None:
        """初始化空注册表。"""

        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(
        self,
        spec: ToolSpec,
        handler: ToolHandler,
        *,
        allow_write: bool = False,
    ) -> None:
        """注册工具与处理器。

        MVP 默认拒绝 side_effect=True；只有显式 ``allow_write=True`` 才允许注册，
        且执行层仍会再次拒绝。重复名称抛 ValueError。
        """

        if spec.side_effect and not allow_write:
            raise ValueError(f"write tool registration is disabled: {spec.name}")
        if spec.name in self._specs:
            raise ValueError(f"tool already registered: {spec.name}")
        for schema_name, schema in (
            ("input_schema", spec.input_schema),
            ("output_schema", spec.output_schema),
        ):
            try:
                jsonschema.Draft202012Validator.check_schema(schema)
            except jsonschema.SchemaError as exc:
                raise ValueError(
                    f"invalid {schema_name} for tool {spec.name}: {exc.message}"
                ) from exc
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def get_spec(self, name: str) -> ToolSpec:
        """取工具定义；不存在抛 KeyError。"""

        return self._specs[name]

    def get_handler(self, name: str) -> ToolHandler:
        """取工具处理器；不存在抛 KeyError。"""

        return self._handlers[name]

    def all(self) -> tuple[ToolSpec, ...]:
        """返回全部工具，按名称排序。"""

        return tuple(sorted(self._specs.values(), key=lambda item: item.name))

    def available(self, ctx: TenantContext) -> tuple[ToolSpec, ...]:
        """按租户状态、权限、数据范围、功能开关与只读策略返回当前可用工具。"""

        if ctx.tenant_status != "active":
            return ()
        result: list[ToolSpec] = []
        for spec in self._specs.values():
            if spec.side_effect:
                continue
            if spec.permission not in ctx.permissions:
                continue
            if not scope_allows(spec.data_scope, ctx):
                continue
            if spec.availability_flag is not None and not ctx.feature_flags.get(
                spec.availability_flag, False
            ):
                continue
            result.append(spec)
        return tuple(sorted(result, key=lambda item: item.name))
