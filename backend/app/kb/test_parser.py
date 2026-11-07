"""测试集文件解析器（M3 KB-04A；`docs/design.md` §4.3 kb_test_cases）。

支持 CSV/XLSX/JSON 三格式（D2）。列名约定：
- `question`（必填）：测试问题；
- `expected_answer`（可选）：期望答案（M4 LLM 评分用，M3 只存储）；
- `expected_points`（可选）：期望要点列表（JSON 数组或分号分隔字符串）；
- `expected_chunk_ids`（可选）：期望引用的 chunk_id 列表（JSON 数组或分号分隔）；
- `category`（可选）：类别标签；
- `is_adversarial`（可选）：对抗标记（true/false/1/0）。

行级错误：缺 question / row_no 无效 / 不可解析 → `{row_no, code, message}`。
整批原子：任一行非法 → 全部拒绝（422 + 行级错误列表，KB-01A 同口径）。
"""

import csv
import io
import json
from dataclasses import dataclass, field
from typing import Any

from app.org.import_parser import _decode_csv

TEST_SET_FORMATS = ("csv", "xlsx", "json")
REQUIRED_COLUMN = "question"
OPTIONAL_COLUMNS = ("expected_answer", "expected_points", "expected_chunk_ids", "category", "is_adversarial")


@dataclass(frozen=True, slots=True)
class TestCaseRow:
    """一行合法的测试用例（写入 kb_test_cases 前的中间形态）。"""

    row_no: int
    question: str
    expected_answer: str | None = None
    expected_points: list[str] | None = None
    expected_chunk_ids: list[str] | None = None
    category: str | None = None
    is_adversarial: bool = False


@dataclass(frozen=True, slots=True)
class ParseResult:
    """解析结果：合法行列表 + 行级错误列表。"""

    cases: list[TestCaseRow] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0 and len(self.cases) > 0


class TestSetParseError(Exception):
    """文件级解析失败（非行级；如 JSON 根不是数组 / XLSX 无工作表）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


def _parse_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass
    return [item.strip() for item in text.split(";") if item.strip()]


def _build_case(row_no: int, row: dict[str, Any]) -> TestCaseRow | dict[str, Any]:
    question = str(row.get("question") or "").strip()
    if not question:
        return {
            "row_no": row_no,
            "code": "QUESTION_REQUIRED",
            "message": "question 列不能为空",
        }
    return TestCaseRow(
        row_no=row_no,
        question=question,
        expected_answer=str(row["expected_answer"]).strip() if row.get("expected_answer") else None,
        expected_points=_parse_list(row.get("expected_points")),
        expected_chunk_ids=_parse_list(row.get("expected_chunk_ids")),
        category=str(row["category"]).strip() if row.get("category") else None,
        is_adversarial=_parse_bool(row.get("is_adversarial", False)),
    )


def _finalize(result: ParseResult) -> ParseResult:
    if result.errors:
        return ParseResult(cases=[], errors=result.errors)
    return result


def parse_csv(data: bytes) -> ParseResult:
    """CSV → 测试用例列表（charset 探测 + csv.Sniffer 复用 import_parser 范式）。"""

    try:
        text = _decode_csv(data)
    except Exception as exc:
        raise TestSetParseError("CSV_DECODE_FAILED", f"CSV 编码解析失败：{exc}") from exc
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or REQUIRED_COLUMN not in reader.fieldnames:
        return ParseResult(errors=[{"row_no": 0, "code": "COLUMN_MISSING", "message": f"CSV 缺少必填列 {REQUIRED_COLUMN}"}])
    result = ParseResult()
    for row_no, row in enumerate(reader, start=2):  # 行 1 是表头
        item = _build_case(row_no, row)
        if isinstance(item, dict):
            result.errors.append(item)
        else:
            result.cases.append(item)
    return _finalize(result)


def parse_json(data: bytes) -> ParseResult:
    """JSON（数组 `[{...}]`）→ 测试用例列表。"""

    try:
        payload = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TestSetParseError("JSON_DECODE_FAILED", f"JSON 解析失败：{exc}") from exc
    if not isinstance(payload, list):
        raise TestSetParseError("JSON_NOT_ARRAY", "JSON 根必须是数组")
    result = ParseResult()
    for row_no, row in enumerate(payload, start=1):
        if not isinstance(row, dict):
            result.errors.append({"row_no": row_no, "code": "ROW_NOT_OBJECT", "message": "每条用例必须是对象"})
            continue
        item = _build_case(row_no, row)
        if isinstance(item, dict):
            result.errors.append(item)
        else:
            result.cases.append(item)
    return _finalize(result)


def parse_xlsx(data: bytes) -> ParseResult:
    """XLSX → 测试用例列表（openpyxl 只读模式取第一个工作表，复用 import_parser 范式）。"""

    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True)
    except Exception as exc:
        raise TestSetParseError("XLSX_DECODE_FAILED", f"XLSX 解析失败：{exc}") from exc
    sheet = workbook.worksheets[0]
    rows_iter = sheet.iter_rows(values_only=True)
    header = next(rows_iter, None)
    if header is None:
        raise TestSetParseError("XLSX_EMPTY", "XLSX 工作表为空")
    column_names = [str(cell or "").strip() for cell in header]
    if REQUIRED_COLUMN not in column_names:
        return ParseResult(errors=[{"row_no": 0, "code": "COLUMN_MISSING", "message": f"XLSX 缺少必填列 {REQUIRED_COLUMN}"}])
    result = ParseResult()
    for row_no, values in enumerate(rows_iter, start=2):
        row = dict(zip(column_names, values, strict=False))
        item = _build_case(row_no, row)
        if isinstance(item, dict):
            result.errors.append(item)
        else:
            result.cases.append(item)
    workbook.close()
    return _finalize(result)


def parse_test_set(data: bytes, file_format: str) -> ParseResult:
    """按格式分发解析（csv/xlsx/json）。"""

    if file_format == "csv":
        return parse_csv(data)
    if file_format == "xlsx":
        return parse_xlsx(data)
    if file_format == "json":
        return parse_json(data)
    raise TestSetParseError("UNSUPPORTED_FORMAT", f"不支持的测试集格式：{file_format}")


def detect_test_set_format(filename: str) -> str:
    """从文件名后缀推断格式（csv/xlsx/json）；不认识的后缀抛异常。"""

    lower = filename.lower()
    for fmt in TEST_SET_FORMATS:
        if lower.endswith(f".{fmt}"):
            return fmt
    raise TestSetParseError("UNSUPPORTED_FORMAT", f"不支持的测试集文件后缀：{filename}（支持 {', '.join(TEST_SET_FORMATS)}）")
