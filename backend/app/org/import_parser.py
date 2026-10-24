"""导入文件解析器（M2 ORG-02，纯函数无 I/O）。

职责：把上传的 CSV/XLSX 字节流解析成**规范化的行记录**（canonical 列名 + 数据行号），
不做任何数据库校验（重复/班级存在性属于任务层）。

口径（`docs/design-briefs/M2-ORG02-import.md`，已确认）：
- CSV：charset-normalizer 编码探测（含 utf-8 BOM）、csv.Sniffer 分隔符探测（失败回退逗号）；
- XLSX：openpyxl 只读模式取**第一个工作表**；
- 表头严格校验：必填列缺失 / 出现未知列（含中文别名归一后仍未知）都拒绝——
  拼错列名被静默忽略是数据质量陷阱；
- 行数上限 MAX_ROWS、文件上限 10 MiB；
- 空数据行**占行号**但不产出记录（错误报告行号 = 用户电子表格数据行号，重试按行号过滤稳定）；
- 未命名列表头：该列值整列丢弃；重复列名：拒绝（都不允许静默错位/覆盖）。
"""

import csv
import io
from dataclasses import dataclass
from typing import Any

from charset_normalizer import from_bytes

MAX_ROWS = 5000
MAX_FILE_SIZE = 10 * 1024 * 1024

ALLOWED_EXTENSIONS = {".csv", ".xlsx"}
XLSX_MAGIC = b"PK\x03\x04"

# 中文表头别名 → canonical 列名（英文 canonical 亦直接可用）
HEADER_ALIASES: dict[str, str] = {
    "login_id": "login_id",
    "登录名": "login_id",
    "工号": "employee_no",
    "employee_no": "employee_no",
    "学号": "student_no",
    "student_no": "student_no",
    "姓名": "name",
    "name": "name",
    "年龄": "age",
    "age": "age",
    "性别": "gender",
    "gender": "gender",
    "职位": "position",
    "position": "position",
    "邮箱": "email",
    "email": "email",
    "年级": "class_grade",
    "class_grade": "class_grade",
    "班级": "class_name",
    "class_name": "class_name",
    "部门": "department",
    "department": "department",
    "部门路径": "department",
}

REQUIRED_HEADERS: dict[str, set[str]] = {
    "teachers": {"employee_no", "name"},
    # 年级/班级为可选列（未分班学生合法，spec ORG-05 验收口径）；提供时须成对
    "students": {"student_no", "name"},
}


class ImportParseError(ValueError):
    """解析期致命错误（上传接口映射 422；整个 job 不创建）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ImportRow:
    """规范化后的数据行（值全部为 str，空可选列为空串）。"""

    row_no: int
    fields: dict[str, str]


def _normalize_headers(
    raw_headers: list[Any], import_type: str
) -> list[tuple[int, str]]:
    """表头归一（中文别名/大小写/空白）并做严格校验。"""

    column_map: list[tuple[int, str]] = []  # (原始列下标, canonical)
    unknown: list[str] = []
    seen: set[str] = set()
    for raw_index, raw in enumerate(raw_headers):
        name = str(raw or "").strip()
        if not name:
            continue  # 未命名列：该列的值整列丢弃，绝不挤占后续列位
        canonical = HEADER_ALIASES.get(name) or HEADER_ALIASES.get(name.lower())
        if canonical is None:
            unknown.append(name)
            continue
        if canonical in seen:
            raise ImportParseError(
                "HEADER_DUPLICATE", f"存在重复列：{name}（归一为 {canonical}）"
            )
        seen.add(canonical)
        column_map.append((raw_index, canonical))
    required = REQUIRED_HEADERS[import_type]
    missing = sorted(required - seen)
    if missing:
        raise ImportParseError(
            "HEADER_INVALID", f"缺少必填列：{', '.join(missing)}（允许的列见导入说明）"
        )
    if unknown:
        raise ImportParseError(
            "HEADER_UNKNOWN", f"存在无法识别的列：{', '.join(unknown)}"
        )
    return column_map


def _decode_csv(data: bytes) -> str:
    """CSV 解码：utf-8（含 BOM）→ gb18030（中文 CSV 常见，GBK 的超集）→ 探测器兜底。

    不直接信探测器：ASCII 占比高的短文件会被误判成单字节编码（实测）。
    gb18030 对本产品目标用户（中文学校导出的 CSV）是确定性覆盖。
    """

    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    best = from_bytes(data).best()
    if best is None:
        raise ImportParseError("ENCODING_UNKNOWN", "无法识别文件编码，请另存为 UTF-8 CSV")
    return str(best)


def parse_import_file(data: bytes, filename: str, import_type: str) -> list[dict[str, Any]]:
    """解析上传文件为规范化行记录列表。

    参数:
        data: 文件字节。
        filename: 原始文件名（决定解析方式：.csv / .xlsx，且校验魔数）。
        import_type: 'teachers' | 'students'。

    返回:
        行记录列表，每行含 `row_no`（数据行号，从 1 起）+ canonical 字段（值 str）。

    异常:
        ImportParseError: 类型/大小/魔数/编码/表头/行数任一不满足（code 见各抛出点）。
    """

    if import_type not in REQUIRED_HEADERS:
        raise ImportParseError("TYPE_INVALID", f"不支持的导入类型：{import_type}")
    if len(data) == 0:
        raise ImportParseError("FILE_EMPTY", "上传文件为空")
    if len(data) > MAX_FILE_SIZE:
        raise ImportParseError("FILE_TOO_LARGE", "文件超过 10MB 上限")

    lower = filename.lower()
    if lower.endswith(".xlsx"):
        if not data.startswith(XLSX_MAGIC):
            raise ImportParseError("FILE_TYPE_INVALID", "文件内容与 .xlsx 扩展名不符")
        rows = _parse_xlsx(data)
    elif lower.endswith(".csv"):
        rows = _parse_csv(data)
    else:
        raise ImportParseError("FILE_TYPE_INVALID", "仅支持 .csv 与 .xlsx 文件")

    if not rows:
        raise ImportParseError("HEADER_INVALID", "文件没有表头行")
    column_map = _normalize_headers(rows[0], import_type)

    parsed: list[dict[str, Any]] = []
    row_no = 0
    for raw_row in rows[1:]:
        values = ["" if value is None else str(value).strip() for value in raw_row]
        # 空数据行**占行号**（与用户电子表格行号对齐，错误报告不错位），不产出记录
        row_no += 1
        if not any(values):
            continue
        if row_no > MAX_ROWS:
            raise ImportParseError("TOO_MANY_ROWS", f"数据行超过 {MAX_ROWS} 行上限")
        record: dict[str, Any] = {"row_no": row_no}
        for raw_index, canonical in column_map:
            record[canonical] = values[raw_index] if raw_index < len(values) else ""
        parsed.append(record)
    return parsed


def _parse_xlsx(data: bytes) -> list[list[Any]]:
    """XLSX → 行列表（第一个工作表；只读 + 取值模式）。"""

    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ImportParseError("FILE_TYPE_INVALID", "文件不是有效的 XLSX") from exc
    try:
        sheet = workbook.worksheets[0]
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()


def _parse_csv(data: bytes) -> list[list[Any]]:
    """CSV → 行列表（分隔符探测失败回退逗号）。"""

    text = _decode_csv(data)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel  # 回退：标准逗号
    reader = csv.reader(io.StringIO(text), dialect)
    return [list(row) for row in reader]
