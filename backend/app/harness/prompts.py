"""版本化 Prompt 注册表。

生产事实来源是 Git 中的模板文件；本注册表只做进程内登记/渲染。
"""

import hashlib
from typing import Any

from jinja2 import StrictUndefined, meta
from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, ConfigDict, Field


def _normalize_template(template: str) -> str:
    """统一换行为 LF，保证 Windows/Linux hash 一致。"""

    return template.replace("\r\n", "\n").replace("\r", "\n")


class PromptSpec(BaseModel):
    """一个 Prompt 版本。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    template: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    hash_scheme_version: str = "1"
    source_path: str | None = None


class PromptRegistry:
    """Prompt 注册与渲染。"""

    def __init__(self) -> None:
        """初始化空注册表与沙箱 Jinja2 环境。"""

        self._specs: dict[tuple[str, str], PromptSpec] = {}
        self._env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)

    def register(
        self,
        prompt_name: str,
        version: str,
        template: str,
        source_path: str | None = None,
    ) -> PromptSpec:
        """注册 Prompt 版本；重复 key 抛 ValueError。"""

        key = (prompt_name, version)
        if key in self._specs:
            raise ValueError(f"prompt already registered: {prompt_name}/{version}")
        normalized = _normalize_template(template)
        spec = PromptSpec(
            name=prompt_name,
            version=version,
            template=normalized,
            sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            source_path=source_path,
        )
        self._specs[key] = spec
        return spec.model_copy(deep=True)

    def get(self, prompt_name: str, version: str) -> PromptSpec:
        """取指定版本深拷贝；不存在抛 KeyError。"""

        return self._specs[(prompt_name, version)].model_copy(deep=True)

    def versions(self, prompt_name: str) -> tuple[str, ...]:
        """返回某 Prompt 已注册版本。"""

        return tuple(
            sorted(
                version
                for (registered_name, version) in self._specs
                if registered_name == prompt_name
            )
        )

    def render(self, prompt_name: str, version: str, **variables: Any) -> str:
        """严格渲染模板。

        缺变量抛 jinja2.UndefinedError；多余变量抛 ValueError。
        """

        spec = self.get(prompt_name, version)
        ast = self._env.parse(spec.template)
        allowed = meta.find_undeclared_variables(ast)
        extra = set(variables) - allowed
        if extra:
            raise ValueError(f"unknown prompt variables: {sorted(extra)}")
        template = self._env.from_string(spec.template)
        return template.render(**variables)
