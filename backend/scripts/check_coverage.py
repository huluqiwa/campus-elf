"""按包校验分支覆盖率（pytest-cov 的 `--cov-fail-under` 只看 TOTAL 汇总）。

用法（在 backend/ 下）：

    uv run pytest --cov=app.rbac --cov=app.auth --cov=app.tenant --cov=app.audit \
        --cov-branch --cov-report=json:coverage.json
    uv run python scripts/check_coverage.py

只对租户隔离、认证、RBAC、审计四个关键包设门槛：全仓门槛会被外围代码稀释，
为数字写废测试没有意义。阈值是**棘轮**——等于基线向下取整；要调低必须先补测，
确需下调须在 `docs/tech-debt.md` 登记理由与复查时点。
"""

import json
import sys
from pathlib import Path
from typing import Any

# 基线：2026-10-28 全量后端套件实测（真实测试栈，755 passed / 1573s，分支覆盖率）。
# 数值为向下取整的棘轮；{包: (基线百分比, 覆盖分支/总分支)}。
THRESHOLDS: dict[str, float] = {
    "app/rbac": 73,  # 73.87%（164/222）
    "app/auth": 81,  # 81.15%（409/504）
    "app/tenant": 81,  # 81.25%（13/16）
    "app/audit": 66,  # 66.67%（4/6；分支总数少，掉一条即 -16.7 个点）
}


def _branch_totals(files: dict[str, Any], package: str) -> tuple[int, int]:
    """汇总某包下所有文件的分支覆盖数（包前缀按路径段匹配）。"""

    prefix = f"{package}/"
    covered = total = 0
    for name, entry in files.items():
        normalized = name.replace("\\", "/")
        if not (normalized == f"{package}.py" or normalized.startswith(prefix)):
            continue
        summary = entry["summary"]
        covered += int(summary["covered_branches"])
        total += int(summary["num_branches"])
    return covered, total


def main() -> int:
    """读取 coverage.json，逐包比较阈值；任一包低于阈值或缺失即失败。"""

    report_path = Path("coverage.json")
    if not report_path.exists():
        print(f"未找到 {report_path}；先用 --cov-report=json:coverage.json 跑测试", file=sys.stderr)
        return 2
    files: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))["files"]

    failures = 0
    for package, floor in THRESHOLDS.items():
        covered, total = _branch_totals(files, package)
        if total == 0:
            print(
                f"[coverage] FAIL {package}：没有分支数据（--cov 漏了该包或包路径写错）",
                file=sys.stderr,
            )
            failures += 1
            continue
        percent = covered / total * 100
        status = "PASS" if percent >= floor else "FAIL"
        stream = sys.stdout if status == "PASS" else sys.stderr
        print(
            f"[coverage] {status} {package}：{percent:.2f}%（{covered}/{total} 分支）"
            f" 门槛 {floor:.0f}%",
            file=stream,
        )
        if status == "FAIL":
            failures += 1

    if failures:
        print(f"[coverage] 结果：{failures} 个包低于门槛", file=sys.stderr)
        return 1
    print("[coverage] 结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
