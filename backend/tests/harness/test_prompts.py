"""Prompt 注册/渲染测试。"""

import hashlib

import pytest
from jinja2 import UndefinedError
from pydantic import ValidationError

from app.harness.prompts import PromptRegistry, PromptSpec


def test_prompt_register_and_render() -> None:
    """注册后可渲染，hash 稳定。"""

    registry = PromptRegistry()
    spec = registry.register("kb_answer", "v1", "你好 {{ name }}，请依据资料回答。")
    assert len(spec.sha256) == 64
    assert registry.render("kb_answer", "v1", name="同学") == "你好 同学，请依据资料回答。"
    assert registry.versions("kb_answer") == ("v1",)


def test_prompt_duplicate_rejected() -> None:
    """同名同版本重复注册失败。"""

    registry = PromptRegistry()
    registry.register("kb_answer", "v1", "x")
    with pytest.raises(ValueError):
        registry.register("kb_answer", "v1", "y")


def test_prompt_missing_variable_raises() -> None:
    """StrictUndefined 下缺变量直接失败。"""

    registry = PromptRegistry()
    registry.register("kb_answer", "v1", "你好 {{ name }}")
    with pytest.raises(UndefinedError):
        registry.render("kb_answer", "v1")


def test_prompt_extra_variable_raises() -> None:
    """多余变量被拒绝，避免拼写错误被静默忽略。"""

    registry = PromptRegistry()
    registry.register("kb_answer", "v1", "你好 {{ name }}")
    with pytest.raises(ValueError):
        registry.render("kb_answer", "v1", name="x", extra="y")


def test_prompt_normalizes_crlf_hash() -> None:
    """CRLF 归一化为 LF，Windows/Linux hash 一致。"""

    registry = PromptRegistry()
    spec = registry.register("kb_answer", "v1", "a\r\nb")
    assert spec.template == "a\nb"
    assert spec.sha256 == hashlib.sha256(b"a\nb").hexdigest()


def test_prompt_spec_rejects_hash_mismatch() -> None:
    """template 与 sha256 不一致时拒绝构造。"""

    with pytest.raises(ValidationError):
        PromptSpec(name="x", version="v1", template="a", sha256="0" * 64)


def test_prompt_missing_version_raises() -> None:
    """缺失版本抛 KeyError。"""

    registry = PromptRegistry()
    with pytest.raises(KeyError):
        registry.get("kb_answer", "v1")
