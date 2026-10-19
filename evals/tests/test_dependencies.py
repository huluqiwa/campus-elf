"""评估作业依赖兼容性测试（独立于后端运行时）。"""

import importlib
from importlib.metadata import version as package_version
from pathlib import Path


def test_eval_observability_dependencies() -> None:
    """Ragas 与 Langfuse 可导入并报告版本。"""

    importlib.import_module("ragas")
    importlib.import_module("langfuse")
    assert package_version("ragas")
    assert package_version("langfuse")


def test_eval_package_is_importable() -> None:
    """评估作业包本身按 src-layout 可导入。"""

    package = importlib.import_module("campus_elf_evals")
    assert package.__file__ is not None
    package_path = Path(package.__file__)
    assert package_path.name == "__init__.py"
    assert package_path.parent.name == "campus_elf_evals"
