"""评估用例集完整性检查（CI 门禁）。

期望类别与数量固定在此处（与 `evals/README.md` §3 口径一致）：
- 每个类别 ≥20 条；
- `prompt_injection` / `cross_tenant` 的**全部**用例视为对抗（≥20）；
- 其余类别对抗/幻觉诱导 ≥3 条。

另校验字段齐全且非空、id 全局唯一；缺文件、缺类别、数量不足均判失败。

用法：python scripts/check_eval_cases.py [--cases evals/cases]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FIELDS = ("id", "category", "input", "expected_points", "rubric")
MIN_CASES_PER_CATEGORY = 20
MIN_ADVERSARIAL = 3

# category -> 对抗最低数（None 表示"全部用例都必须是对抗"）
EXPECTED_CATEGORIES: dict[str, int | None] = {
    "kb_qa": MIN_ADVERSARIAL,
    "citation": MIN_ADVERSARIAL,
    "refusal": MIN_ADVERSARIAL,
    "prompt_injection": None,
    "cross_tenant": None,
    "sensitive_content": MIN_ADVERSARIAL,
    "capability_fallback": MIN_ADVERSARIAL,
    "multi_intent": MIN_ADVERSARIAL,
    "leave_navigation": MIN_ADVERSARIAL,
    # 检索直评估类（TD-294）：无对抗维度（确定性命中判定），对抗要求豁免（-1 哨兵）
    "chunk_grid": -1,
}


def _is_non_empty(value: object) -> bool:
    """字段值非空（字符串/列表/字典）。"""

    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return value is not None


def main() -> int:
    """校验用例集并输出统计；不通过返回 1。"""

    parser = argparse.ArgumentParser(description="评估用例集完整性检查")
    parser.add_argument("--cases", type=Path, default=REPO_ROOT / "evals" / "cases")
    args = parser.parse_args()

    problems: list[str] = []
    per_category: Counter[str] = Counter()
    adversarial: Counter[str] = Counter()
    seen_ids: set[str] = set()

    files = sorted(args.cases.glob("*.jsonl"))
    if not files:
        print(f"FAIL 未找到用例文件：{args.cases}", file=sys.stderr)
        return 1

    for path in files:
        for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not raw.strip():
                continue
            try:
                case = json.loads(raw)
            except json.JSONDecodeError as exc:
                problems.append(f"{path.name}:{line_number} JSON 解析失败：{exc}")
                continue
            if not isinstance(case, dict):
                problems.append(f"{path.name}:{line_number} 用例必须是 JSON 对象")
                continue

            missing = [field for field in REQUIRED_FIELDS if not _is_non_empty(case.get(field))]
            if missing:
                problems.append(
                    f"{path.name}:{line_number} 缺字段或字段为空：{', '.join(missing)}"
                )
                continue

            case_id = case["id"]
            if not isinstance(case_id, str):
                problems.append(f"{path.name}:{line_number} id 必须是字符串")
                continue
            if case_id in seen_ids:
                problems.append(f"{path.name}:{line_number} id 重复：{case_id}")
            seen_ids.add(case_id)

            category = case["category"]
            if not isinstance(category, str) or category not in EXPECTED_CATEGORIES:
                problems.append(f"{path.name}:{line_number} 未知类别：{category!r}")
                continue
            per_category[category] += 1
            if case.get("adversarial") or EXPECTED_CATEGORIES[category] is None:
                adversarial[category] += 1

    # 缺类别必须失败（整类被删不会因为"只校验出现过的类别"而漏过）。
    for category in EXPECTED_CATEGORIES:
        if per_category[category] == 0:
            problems.append(f"缺少类别：{category}（要求 ≥{MIN_CASES_PER_CATEGORY} 条）")

    for category, count in sorted(per_category.items()):
        if count < MIN_CASES_PER_CATEGORY:
            problems.append(f"类别 {category} 仅 {count} 条（要求 ≥{MIN_CASES_PER_CATEGORY}）")
        minimum = EXPECTED_CATEGORIES[category]
        if minimum == -1:
            continue  # 检索直评估类：无对抗要求（TD-294 简报 F3 口径）
        required_adversarial = minimum if minimum is not None else count
        if adversarial[category] < required_adversarial:
            problems.append(
                f"类别 {category} 对抗用例仅 {adversarial[category]} 条"
                f"（要求 ≥{required_adversarial}）"
            )

    print(f"用例总数：{len(seen_ids)}")
    for category, count in sorted(per_category.items()):
        print(f"  - {category}: {count}（对抗 {adversarial[category]}）")

    if problems:
        print("\nFAIL 发现问题：", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)
        return 1

    print("OK 用例集满足数量、类别与字段要求")
    return 0


if __name__ == "__main__":
    sys.exit(main())
