"""导入文件解析器单元测试（M2-ORG02）：CSV/XLSX、编码探测、表头校验、上限。"""

import io
from uuid import uuid4

import pytest

from app.org.import_parser import (
    MAX_ROWS,
    ImportParseError,
    parse_import_file,
)

TEACHER_HEADERS = ["login_id", "employee_no", "name", "age", "gender", "position", "email"]


def _xlsx_bytes(rows: list[list[object]], sheet_name: str = "Sheet") -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _csv_bytes(rows: list[list[str]], encoding: str = "utf-8") -> bytes:
    import csv

    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue().encode(encoding)


def test_parse_csv_teachers_happy_path() -> None:
    data = _csv_bytes(
        [
            ["login_id", "employee_no", "name", "age", "gender", "position", "email"],
            ["T-1", "E-1", "张老师", "35", "male", "数学教师", "t1@school.edu"],
            ["T-2", "E-2", "李老师", "", "", "", ""],
        ]
    )
    rows = parse_import_file(data, "teachers.csv", "teachers")
    assert [row["row_no"] for row in rows] == [1, 2]
    assert rows[0] == {
        "row_no": 1,
        "login_id": "T-1",
        "employee_no": "E-1",
        "name": "张老师",
        "age": "35",
        "gender": "male",
        "position": "数学教师",
        "email": "t1@school.edu",
    }
    assert rows[1]["age"] == ""  # 可选列缺省为空串


def test_parse_xlsx_students_with_chinese_alias_headers() -> None:
    data = _xlsx_bytes(
        [
            ["学号", "姓名", "性别", "年级", "班级", "邮箱"],
            ["S-1", "学生一", "female", "2026级", "1班", ""],
            ["S-2", "学生二", "", "2026级", "1班", "s2@school.edu"],
        ]
    )
    rows = parse_import_file(data, "students.xlsx", "students")
    assert len(rows) == 2
    assert rows[0]["student_no"] == "S-1"
    assert rows[0]["class_grade"] == "2026级"
    assert rows[1]["class_name"] == "1班"


def test_parse_csv_gbk_encoding_detected() -> None:
    data = _csv_bytes(
        [
            ["employee_no", "name"],
            ["E-9", "王老师"],
        ],
        encoding="gbk",
    )
    rows = parse_import_file(data, "teachers.csv", "teachers")
    assert rows[0]["name"] == "王老师"


def test_parse_csv_utf8_bom_handled() -> None:
    body = _csv_bytes([["employee_no", "name"], ["E-8", "李四"]])
    rows = parse_import_file(b"\xef\xbb\xbf" + body, "teachers.csv", "teachers")
    assert rows[0]["employee_no"] == "E-8"


def test_missing_required_header_rejected() -> None:
    data = _csv_bytes([["employee_no", "nickname"], ["E-1", "张三"]])
    with pytest.raises(ImportParseError) as exc_info:
        parse_import_file(data, "teachers.csv", "teachers")
    assert exc_info.value.code == "HEADER_INVALID"
    assert "name" in exc_info.value.message


def test_unknown_header_rejected() -> None:
    data = _csv_bytes([["employee_no", "name", "工号2"], ["E-1", "张三", "x"]])
    with pytest.raises(ImportParseError) as exc_info:
        parse_import_file(data, "teachers.csv", "teachers")
    assert exc_info.value.code == "HEADER_UNKNOWN"


def test_row_limit_enforced() -> None:
    rows = [["employee_no", "name"]] + [
        [f"E-{i}", f"老师{i}"] for i in range(MAX_ROWS + 1)
    ]
    with pytest.raises(ImportParseError) as exc_info:
        parse_import_file(_csv_bytes(rows), "teachers.csv", "teachers")
    assert exc_info.value.code == "TOO_MANY_ROWS"


def test_oversized_file_rejected() -> None:
    with pytest.raises(ImportParseError) as exc_info:
        parse_import_file(b"x" * (10 * 1024 * 1024 + 1), "teachers.csv", "teachers")
    assert exc_info.value.code == "FILE_TOO_LARGE"


def test_wrong_extension_rejected() -> None:
    with pytest.raises(ImportParseError) as exc_info:
        parse_import_file(b"data", "teachers.docx", "teachers")
    assert exc_info.value.code == "FILE_TYPE_INVALID"


def test_fake_xlsx_with_csv_name_rejected() -> None:
    """扩展名与魔数不符（伪装）→ 拒绝。"""

    with pytest.raises(ImportParseError) as exc_info:
        parse_import_file(_csv_bytes([["employee_no", "name"]]), "fake.xlsx", "teachers")
    assert exc_info.value.code == "FILE_TYPE_INVALID"


def test_empty_file_rejected() -> None:
    with pytest.raises(ImportParseError) as exc_info:
        parse_import_file(b"", "teachers.csv", "teachers")
    assert exc_info.value.code in {"FILE_EMPTY", "HEADER_INVALID"}


def test_empty_rows_occupy_row_numbers_alias_check() -> None:
    data = _csv_bytes(
        [
            ["employee_no", "name"],
            ["E-1", "张三"],
            ["", ""],
            ["E-2", "李四"],
        ]
    )
    rows = parse_import_file(data, "teachers.csv", "teachers")
    assert [row["row_no"] for row in rows] == [1, 3]  # 空行占 2 号
    assert [row["employee_no"] for row in rows] == ["E-1", "E-2"]


def test_unknown_import_type_rejected() -> None:
    with pytest.raises(ImportParseError) as exc_info:
        parse_import_file(_csv_bytes([["employee_no", "name"]]), "x.csv", "courses")
    assert exc_info.value.code == "TYPE_INVALID"


def test_parse_is_deterministic_for_retry() -> None:
    """重试语义依赖：同一文件两次解析结果完全一致（行号与内容）。"""

    data = _csv_bytes(
        [
            ["employee_no", "name"],
            ["E-1", "张三"],
            ["E-2", "李四"],
        ]
    )
    assert parse_import_file(data, "a.csv", "teachers") == parse_import_file(
        data, "a.csv", "teachers"
    )
    assert uuid4()  # 占位引用（保持导入风格一致）


def test_unnamed_middle_column_does_not_shift_values() -> None:
    """Excel 导出常见的未命名中间列：该列值整列丢弃，后续列位不错移。"""

    data = _csv_bytes(
        [
            ["学号", "姓名", "", "年级", "班级"],
            ["S-1", "学生一", "垃圾值", "2026级", "1班"],
        ]
    )
    rows = parse_import_file(data, "students.csv", "students")
    assert rows[0]["name"] == "学生一"
    assert rows[0]["class_grade"] == "2026级"
    assert rows[0]["class_name"] == "1班"
    assert "垃圾值" not in str(rows[0])


def test_duplicate_canonical_header_rejected() -> None:
    """两个「姓名」列 → 拒绝（静默覆盖是数据质量陷阱）。"""

    data = _csv_bytes(
        [
            ["employee_no", "name", "姓名"],
            ["E-1", "张三", "李四"],
        ]
    )
    with pytest.raises(ImportParseError) as exc_info:
        parse_import_file(data, "teachers.csv", "teachers")
    assert exc_info.value.code == "HEADER_DUPLICATE"


def test_empty_rows_occupy_row_numbers() -> None:
    """空数据行占行号：错误报告行号与用户电子表格对齐。"""

    data = _csv_bytes(
        [
            ["employee_no", "name"],
            ["E-1", "张三"],
            ["", ""],
            ["E-2", "李四"],
        ]
    )
    rows = parse_import_file(data, "teachers.csv", "teachers")
    assert [row["row_no"] for row in rows] == [1, 3]
