"""Prompt 文件加载器校验（M4-AI01b 步 3；ADR-008：Git 是生产 prompt 唯一事实源）。"""

import hashlib

import pytest
from jinja2 import UndefinedError

from app.harness.prompt_loading import (
    PROMPT_FILES_DIR,
    build_default_registry,
    load_prompt,
)


def test_default_registry_has_kb_qa_v1() -> None:
    """默认注册表含 kb_qa/v1；注册 hash 与磁盘文件内容一致（防手改不同步）。"""

    registry = build_default_registry()
    assert registry.versions("kb_qa") == ("v1",)
    spec = registry.get("kb_qa", "v1")
    disk = (PROMPT_FILES_DIR / "kb_qa_v1.md").read_text(encoding="utf-8").replace(
        "\r\n", "\n"
    )
    assert spec.sha256 == hashlib.sha256(disk.encode("utf-8")).hexdigest()
    assert spec.source_path == "app/harness/prompt_files/kb_qa_v1.md"


def test_render_kb_qa_includes_question_and_evidence() -> None:
    """渲染：问题与证据块进入模板；红线段落恒在（约束层随模板版本化）。"""

    registry = build_default_registry()
    rendered = registry.render(
        "kb_qa",
        "v1",
        question="请假流程是什么？",
        evidence="[1] 请假需班主任审批。",
    )
    assert "请假流程是什么？" in rendered
    assert "[1] 请假需班主任审批。" in rendered
    assert "红线" in rendered


def test_render_missing_variable_fails_loudly() -> None:
    """缺变量：显式失败（StrictUndefined），不静默渲染空值。"""

    registry = build_default_registry()
    with pytest.raises((UndefinedError, ValueError)):
        registry.render("kb_qa", "v1", question="只有问题")


def test_render_unknown_variable_fails_loudly() -> None:
    """多变量：显式失败（防调用点与模板漂移）。"""

    registry = build_default_registry()
    with pytest.raises(ValueError):
        registry.render(
            "kb_qa", "v1", question="q", evidence="e", extra="不存在的变量"
        )


def test_load_prompt_rejects_missing_file(tmp_path: object) -> None:
    """加载不存在的模板文件：响亮失败（不吞错）。"""

    registry = build_default_registry()
    with pytest.raises(FileNotFoundError):
        load_prompt(registry, "不存在", "v1")
