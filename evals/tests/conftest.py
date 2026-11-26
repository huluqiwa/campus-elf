from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)

def _never_load_repository_dotenv_in_unit_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail before a unit test can print credentials from the real repository .env."""

    from campus_elf_evals import qa_runner

    def reject_repository_dotenv(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "unit tests must inject TEMP_* settings; repository .env reads are disabled"
        )

    monkeypatch.setattr(qa_runner, "load_temp_model_settings", reject_repository_dotenv)
