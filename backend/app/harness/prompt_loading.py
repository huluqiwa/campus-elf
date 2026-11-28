"""Git 版本化 Prompt 文件加载（M4-AI01b 步 3；ADR-008：Git 是生产 prompt 唯一事实源）。

模板文件在 `app/harness/prompt_files/<name>_<version>.md`；加载即注册进
``PromptRegistry``（sha256 由 ``PromptSpec`` 校验，磁盘与注册不一致即启动失败）。
Langfuse 只做草稿/实验，生产渲染一律走本注册表。
"""

from pathlib import Path

from app.harness.prompts import PromptRegistry, PromptSpec

PROMPT_FILES_DIR = Path(__file__).parent / "prompt_files"


def load_prompt(registry: PromptRegistry, name: str, version: str) -> PromptSpec:
    """从磁盘加载模板并注册；文件缺失响亮失败（FileNotFoundError）。"""

    path = PROMPT_FILES_DIR / f"{name}_{version}.md"
    template = path.read_text(encoding="utf-8")
    source_path = path.relative_to(PROMPT_FILES_DIR.parents[2]).as_posix()
    return registry.register(name, version, template, source_path=source_path)


def build_default_registry() -> PromptRegistry:
    """生产 Prompt 注册表（新 prompt 在此登记；缺文件即启动失败）。"""

    registry = PromptRegistry()
    load_prompt(registry, "kb_qa", "v1")
    load_prompt(registry, "route", "v1")
    load_prompt(registry, "route", "v2")
    load_prompt(registry, "route", "v3")
    load_prompt(registry, "route", "v4")
    load_prompt(registry, "react_think", "v1")
    load_prompt(registry, "react", "v1")
    return registry
