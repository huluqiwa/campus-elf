"""按包校验分支覆盖率（pytest-cov 的 `--cov-fail-under` 只看 TOTAL 汇总）。

用法（在 backend/ 下）：

    uv run pytest --cov=app.rbac --cov=app.auth --cov=app.tenant --cov=app.audit \
        --cov-branch --cov-report=json:coverage.json
    uv run python scripts/check_coverage.py

只对租户隔离、认证、RBAC、审计四个关键包设门槛：全仓门槛会被外围代码稀释，
为数字写废测试没有意义。阈值是**棘轮**——等于基线向下取整；要调低必须先补测，
确需下调须在 `docs/tech-debt.md` 登记理由与复查时点。基线不可复现时，可按 TD
以 CI 实测取整并限期抬回，不得无 TD 下调。
"""

import json
import sys
from pathlib import Path
from typing import Any

# 基线：以 CI（Linux）全量实测为准——2026-10-28 PR #1 同一 commit 两次 run 均 757 passed，
# app/auth 分别为 407/504=80.75%（run 40681833037）与 408/504=80.95%（run 40681807497），
# 本地 Windows 为 409/504=81.15%；读数在 407–409 间漂移，缺口集中在 service.py 的
# 1342–1343 与 2047–2050 两段异常路径（非确定性待 TD-221/TD-187f 定位）。
# 按最低观测向下取整，{包: (基线百分比, 覆盖分支/总分支)}。
THRESHOLDS: dict[str, float] = {
    "app/rbac": 73,  # 73.87%（164/222；CI 与本地一致）
    "app/auth": 80,  # 80.75%（407/504；CI 最低观测，见 TD-221）
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


def _print_breakdown(files: dict[str, Any], package: str) -> None:
    """失败时逐文件打印分支数与缺失条数，缩小定位范围（完整明细见 coverage 产物）。"""

    prefix = f"{package}/"
    for name, entry in sorted(files.items()):
        normalized = name.replace("\\", "/")
        if not (normalized == f"{package}.py" or normalized.startswith(prefix)):
            continue
        summary = entry["summary"]
        covered = int(summary["covered_branches"])
        total = int(summary["num_branches"])
        missing = len(entry.get("missing_branches") or [])
        print(f"[coverage]   {normalized}：{covered}/{total} 分支，缺口 {missing}", file=sys.stderr)


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
            _print_breakdown(files, package)
            failures += 1

    if failures:
        print(f"[coverage] 结果：{failures} 个包低于门槛", file=sys.stderr)
        return 1
    print("[coverage] 结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
