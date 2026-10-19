"""Python 3.13 依赖兼容性冒烟测试。

分组断言：任一组失败只说明该组依赖不兼容，便于定位与替换。
"""

import importlib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

_DISTRIBUTIONS = {
    "sqlalchemy": "SQLAlchemy",
    "qdrant_client": "qdrant-client",
    "jinja2": "Jinja2",
    "langchain_core": "langchain-core",
    "jwt": "PyJWT",
}


def _version(module_name: str) -> str:
    """导入模块并通过发行版元数据返回版本号。"""

    importlib.import_module(module_name)
    distribution = _DISTRIBUTIONS.get(module_name, module_name)
    try:
        return package_version(distribution)
    except PackageNotFoundError:
        return ""


def test_web_db_dependencies() -> None:
    """Web/DB/队列依赖可导入。"""

    assert _version("sqlalchemy")
    assert _version("asyncpg")
    assert _version("alembic")
    assert _version("redis")
    assert _version("arq")
    assert _version("qdrant_client")


def test_rag_dependencies() -> None:
    """RAG/存储/输入防护依赖可导入。"""

    assert _version("aioboto3")
    assert _version("httpx")
    assert _version("jsonschema")
    assert _version("jinja2")
    assert _version("nh3")
    assert _version("jieba")


def test_agent_dependencies() -> None:
    """Agent/LLM 客户端依赖可导入。"""

    assert _version("langgraph")
    assert _version("langchain_core")
    assert _version("openai")


def test_auth_dependencies() -> None:
    """鉴权/加密/邮件依赖可导入。"""

    assert _version("jwt")
    assert _version("pwdlib")
    assert _version("pyotp")
    assert _version("cryptography")
    assert _version("aiosmtplib")


