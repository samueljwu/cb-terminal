"""Daily equity and FX history ingestion for market-data joins.

The expected first source shape is a Bloomberg-style multi-security worksheet:

- row 4: security names, e.g. ``6669 TT Equity`` / ``USDTWD Curncy``;
- row 5/6: field labels, usually ``Last Price`` / ``PX_LAST``;
- column A from row 7 onward: Excel serial dates;
- each security column: daily numeric value.

The parser is intentionally stdlib-only and conservative.  It captures observed
price series; it does not decide how those observations should be used in a CB
valuation until the join step.
"""

from __future__ import annotations

import csv
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Iterable, Sequence
from xml.etree import ElementTree as ET

MAX_INPUT_FILE_BYTES = 50_000_000
MAX_CSV_ROWS = 200_000
MAX_XLSX_MEMBERS = 200
MAX_XLSX_MEMBER_BYTES = 20_000_000
MAX_XLSX_TOTAL_UNCOMPRESSED_BYTES = 100_000_000
MAX_XLSX_ROWS_PER_SHEET = 200_000
MAX_XLSX_CELLS_PER_SHEET = 2_000_000


@dataclass(frozen=True)
class MarketDataPoint:
    instrument_id: str
    instrument_type: str
    as_of_date: date
    field: str
    value: float
    source_file: str = ""
    source_sheet: str = ""
    source_row: int = 0
    source_column: str = ""


def load_market_data_file(path: str | Path) -> list[MarketDataPoint]:
    source = Path(path)
    _reject_oversized_file(source)
    suffix = source.suffix.lower()
    if suffix == ".xlsx":
        return load_market_data_xlsx(source)
    if suffix == ".csv":
        with source.open("r", newline="", encoding="utf-8-sig") as handle:
            return parse_market_data_csv(handle, source_file=str(source))
    raise ValueError(f"unsupported market-data file type: {source.suffix}")


def load_market_data_xlsx(path: str | Path) -> list[MarketDataPoint]:
    source = Path(path)
    return _parse_market_data_xlsx_tables(_read_xlsx_tables(source), source_file=str(source))


def _parse_market_data_xlsx_tables(
    tables: Sequence[tuple[str, Sequence[Sequence[object]]]],
    *,
    source_file: str,
) -> list[MarketDataPoint]:
    """Parse Bloomberg market-data sheets while ignoring notes/other layouts."""

    parsed: list[MarketDataPoint] = []
    recognized_sheets: list[str] = []
    for sheet_name, table in tables:
        if len(table) < 7 or _find_security_header_row(table) is None:
            continue
        recognized_sheets.append(sheet_name)
        parsed.extend(parse_bloomberg_market_data_table(table, source_file=source_file, source_sheet=sheet_name))
    if not recognized_sheets:
        raise ValueError("no XLSX worksheet contains a recognizable Bloomberg equity/FX history layout")
    return parsed


def parse_market_data_csv(handle: Iterable[str], *, source_file: str = "") -> list[MarketDataPoint]:
    reader = csv.DictReader(handle)
    if not reader.fieldnames:
        raise ValueError("market data CSV must include a header row")
    # Simple canonical long schema for future controlled exports.
    required = {"date", "instrument id", "value"}
    normalized_headers = {_normalize_header(name): name for name in reader.fieldnames}
    if not required.issubset(set(normalized_headers)):
        raise ValueError("market data CSV must include date, instrument_id, and value columns")
    rows: list[MarketDataPoint] = []
    for source_row, raw in enumerate(reader, start=2):
        if source_row - 1 > MAX_CSV_ROWS:
            raise ValueError(f"market data CSV exceeds {MAX_CSV_ROWS} data rows")
        if all((value or "").strip() == "" for value in raw.values()):
            continue
        instrument = (raw[normalized_headers["instrument id"]] or "").strip()
        rows.append(
            MarketDataPoint(
                instrument_id=instrument,
                instrument_type=_instrument_type(instrument),
                as_of_date=_parse_date(raw[normalized_headers["date"]]),
                field=(raw.get(normalized_headers.get("field", ""), "PX_LAST") or "PX_LAST").strip() or "PX_LAST",
                value=_parse_float(raw[normalized_headers["value"]], "value", source_row),
                source_file=source_file,
                source_sheet="csv",
                source_row=source_row,
            )
        )
    return rows


def parse_market_data_csv_text(text: str, *, source_file: str = "<text>") -> list[MarketDataPoint]:
    return parse_market_data_csv(StringIO(text), source_file=source_file)


def parse_bloomberg_market_data_table(
    table: Sequence[Sequence[object]],
    *,
    source_file: str = "",
    source_sheet: str = "",
) -> list[MarketDataPoint]:
    if len(table) < 7:
        raise ValueError("Bloomberg market-data worksheet is too short")
    # First non-empty row containing at least two instrument-looking cells is the
    # security header.  In the sample this is Excel row 4.
    header_index = _find_security_header_row(table)
    if header_index is None:
        raise ValueError("could not find security header row in market-data worksheet")
    field_index = header_index + 2 if header_index + 2 < len(table) else header_index + 1
    securities = table[header_index]
    fields = table[field_index] if field_index < len(table) else []
    points: list[MarketDataPoint] = []
    for row_index in range(field_index + 1, len(table)):
        row = table[row_index]
        if not row or _blank(row[0]):
            continue
        as_of_date = _parse_date(str(row[0]))
        for col_index in range(1, max(len(securities), len(row))):
            instrument = _cell(securities, col_index).strip()
            if not instrument:
                continue
            raw_value = _cell(row, col_index)
            if _blank(raw_value):
                continue
            value = _parse_float(raw_value, instrument, row_index + 1)
            field = _cell(fields, col_index).strip() or "PX_LAST"
            points.append(
                MarketDataPoint(
                    instrument_id=instrument,
                    instrument_type=_instrument_type(instrument),
                    as_of_date=as_of_date,
                    field=field,
                    value=value,
                    source_file=source_file,
                    source_sheet=source_sheet,
                    source_row=row_index + 1,
                    source_column=_column_name(col_index + 1),
                )
            )
    return points


def _find_security_header_row(table: Sequence[Sequence[object]]) -> int | None:
    for index, row in enumerate(table[:20]):
        non_empty = [str(cell).strip() for cell in row[1:] if not _blank(cell)]
        if len(non_empty) >= 1 and any(_instrument_type(cell) != "unknown" for cell in non_empty):
            return index
    return None


def _read_xlsx_tables(path: Path) -> list[tuple[str, list[list[str]]]]:
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        _validate_xlsx_archive(archive)
        shared_strings = _read_shared_strings(archive, ns)
        sheet_names = _workbook_sheet_names(archive, ns)
        sheet_paths = sorted(name for name in archive.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"))
        tables: list[tuple[str, list[list[str]]]] = []
        for index, sheet_path in enumerate(sheet_paths, start=1):
            root = ET.fromstring(_safe_archive_read(archive, sheet_path))
            rows: list[list[str]] = []
            cell_count = 0
            expected_row = 1
            for row in root.findall(".//main:sheetData/main:row", ns):
                if len(rows) >= MAX_XLSX_ROWS_PER_SHEET:
                    raise ValueError(f"XLSX sheet {sheet_path} exceeds {MAX_XLSX_ROWS_PER_SHEET} rows")
                row_number = int(row.attrib.get("r", str(expected_row)))
                while expected_row < row_number:
                    rows.append([])
                    expected_row += 1
                values_by_col: dict[int, str] = {}
                max_col = 0
                for cell in row.findall("main:c", ns):
                    cell_count += 1
                    if cell_count > MAX_XLSX_CELLS_PER_SHEET:
                        raise ValueError(f"XLSX sheet {sheet_path} exceeds {MAX_XLSX_CELLS_PER_SHEET} cells")
                    ref = cell.attrib.get("r", "")
                    col = _column_index(ref)
                    max_col = max(max_col, col)
                    values_by_col[col] = _cell_value(cell, shared_strings, ns)
                rows.append([values_by_col.get(col, "") for col in range(1, max_col + 1)])
                expected_row = row_number + 1
            sheet_name = sheet_names[index - 1] if index - 1 < len(sheet_names) else f"sheet{index}"
            tables.append((sheet_name, rows))
        return tables


def _read_shared_strings(archive: zipfile.ZipFile, ns: dict[str, str]) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(_safe_archive_read(archive, "xl/sharedStrings.xml"))
    strings: list[str] = []
    for item in root.findall("main:si", ns):
        strings.append("".join(node.text or "" for node in item.findall(".//main:t", ns)))
    return strings


def _workbook_sheet_names(archive: zipfile.ZipFile, ns: dict[str, str]) -> list[str]:
    if "xl/workbook.xml" not in archive.namelist():
        return []
    root = ET.fromstring(_safe_archive_read(archive, "xl/workbook.xml"))
    return [sheet.attrib.get("name", "") for sheet in root.findall(".//main:sheets/main:sheet", ns)]


def _reject_oversized_file(path: Path) -> None:
    if path.exists() and path.stat().st_size > MAX_INPUT_FILE_BYTES:
        raise ValueError(f"input file exceeds {MAX_INPUT_FILE_BYTES} bytes: {path}")


def _validate_xlsx_archive(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > MAX_XLSX_MEMBERS:
        raise ValueError(f"XLSX archive has too many members: {len(infos)}")
    total = 0
    for info in infos:
        total += info.file_size
        if info.file_size > MAX_XLSX_MEMBER_BYTES:
            raise ValueError(f"XLSX member too large: {info.filename}")
    if total > MAX_XLSX_TOTAL_UNCOMPRESSED_BYTES:
        raise ValueError(f"XLSX archive uncompressed size exceeds {MAX_XLSX_TOTAL_UNCOMPRESSED_BYTES} bytes")


def _safe_archive_read(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > MAX_XLSX_MEMBER_BYTES:
        raise ValueError(f"XLSX member too large: {name}")
    return archive.read(name)


def _cell_value(cell: ET.Element, shared_strings: list[str], ns: dict[str, str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "s":
        value = cell.findtext("main:v", default="", namespaces=ns)
        if value == "":
            return ""
        index = int(float(value))
        return shared_strings[index] if 0 <= index < len(shared_strings) else ""
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//main:t", ns))
    return cell.findtext("main:v", default="", namespaces=ns) or ""


def _cell(row: Sequence[object], index: int) -> str:
    return "" if index >= len(row) or row[index] is None else str(row[index])


def _column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    index = 0
    for letter in letters:
        index = index * 26 + (ord(letter) - ord("A") + 1)
    return index or 1


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, rem = divmod(index - 1, 26)
        result = chr(ord("A") + rem) + result
    return result


def _parse_date(value: object) -> date:
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return (datetime(1899, 12, 30) + timedelta(days=int(float(text)))).date()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%y", "%m/%d/%Y", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return date.fromisoformat(text)


def _parse_float(value: object, field: str, source_row: int) -> float:
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError as exc:
        raise ValueError(f"invalid numeric value for {field!r} on source row {source_row}: {value!r}") from exc


def _instrument_type(instrument_id: str) -> str:
    text = instrument_id.strip().upper()
    if text.endswith(" EQUITY"):
        return "equity"
    if text.endswith(" CURNCY") or text.endswith(" CURNCY"):
        return "fx"
    if len(text) == 6 and text.isalpha():
        return "fx"
    return "unknown"


def _normalize_header(value: str) -> str:
    return " ".join((value or "").strip().lower().replace("_", " ").split())


def _blank(value: object) -> bool:
    return value is None or str(value).strip() == ""
