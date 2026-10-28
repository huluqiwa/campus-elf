"""覆盖率门槛脚本的单元测试：阈值边界、逐文件明细与缺失报告。

不依赖测试栈：用合成 `coverage.json` 钉住 80% 门槛的通过/失败边界与输出。
"""

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_coverage.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("check_coverage_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry(covered: int, total: int, missing: int = 0) -> dict[str, Any]:
    """构造单文件覆盖率条目；missing 只影响明细里的缺口条数。"""

    return {
        "summary": {"covered_branches": covered, "num_branches": total},
        "missing_branches": [[1, 2]] * missing,
    }


def _write_report(tmp_path: Path, files: dict[str, Any]) -> None:
    (tmp_path / "coverage.json").write_text(json.dumps({"files": files}), encoding="utf-8")


def test_production_auth_threshold_is_the_ci_floor() -> None:
    """生产阈值钉在 CI 最低观测（407/504）；抬升须连同 TD-221/187f 一起改。"""

    assert _load_script().THRESHOLDS["app/auth"] == 80


def test_guard_passes_at_the_ci_floor_and_fails_below_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    monkeypatch.setattr(script, "THRESHOLDS", {"app/auth": 80})
    monkeypatch.chdir(tmp_path)

    _write_report(tmp_path, {"app/auth/service.py": _entry(407, 504)})
    assert script.main() == 0

    # 404/504=80.16% 仍在门槛上；403/504=79.96% 才失败。
    _write_report(tmp_path, {"app/auth/service.py": _entry(404, 504)})
    assert script.main() == 0

    _write_report(tmp_path, {"app/auth/service.py": _entry(403, 504, missing=2)})
    assert script.main() == 1
    err = capsys.readouterr().err
    assert "app/auth/service.py" in err
    assert "403/504" in err
    assert "缺口 2" in err


def test_backslash_paths_are_counted_and_reported_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    monkeypatch.setattr(script, "THRESHOLDS", {"app/auth": 80})
    monkeypatch.chdir(tmp_path)
    _write_report(
        tmp_path,
        {"app\\auth\\service.py": _entry(4, 5), "app/other/x.py": _entry(0, 100)},
    )

    assert script.main() == 0

    # 失败明细把 Windows 反斜杠归一成正斜杠，便于和覆盖率报告对照。
    _write_report(tmp_path, {"app\\auth\\service.py": _entry(3, 5, missing=1)})
    assert script.main() == 1
    err = capsys.readouterr().err
    assert "app/auth/service.py" in err
    assert "3/5" in err


def test_missing_report_returns_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    monkeypatch.chdir(tmp_path)

    assert script.main() == 2
    assert "coverage.json" in capsys.readouterr().err
