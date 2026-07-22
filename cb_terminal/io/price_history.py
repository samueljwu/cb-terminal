"""Raw CB quote/price-history ingestion.

This module is intentionally separate from ``market_history``.  ``market_history``
feeds the valuation engine and must include stock price / FX / assumptions.  This
module captures dealer/Bloomberg-style CB quote history as an auditable source
that can later be joined to stock/FX histories by the market-data pipeline.

Only stdlib readers are used.  XLSX support is a conservative OpenXML reader for
plain worksheet exports; it does not evaluate formulas or macros.
"""

from __future__ import annotations

import csv
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time
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
class PriceQuoteRow:
    reference_security: str
    as_of_date: date
    as_of_time: time | None = None
    dealer: str = ""
    source_type: str = ""
    security: str = ""
    bid_price: float | None = None
    ask_price: float | None = None
    mid_price: float | None = None
    stock_price: float | None = None
    price_currency: str = ""
    sender_name: str = ""
    subject: str = ""
    keyword: str = ""
    instrument_id: str = ""
    contract_id: str = ""
    source_file: str = ""
    source_sheet: str = ""
    source_row: int = 0


ALIASES: dict[str, tuple[str, ...]] = {
    "instrument_id": ("instrument id", "instrument_id", "canonical id", "canonical_id", "isin"),
    "contract_id": ("contract id", "contract_id"),
    "reference_security": ("reference security", "reference_security", "security id", "bbg id", "figi"),
    "date": ("date", "as_of_date", "as of date", "pricing date"),
    "time": ("time", "quote time", "as_of_time"),
    "dealer": ("dealer", "broker", "contributor"),
    "source_type": ("source", "source type", "quote source"),
    "security": ("security", "description", "name"),
    "bid_price": ("bid price", "bid", "px bid", "bid_px", "bid px"),
    "ask_price": ("ask price", "ask", "offer", "px ask", "ask_px", "ask px"),
    "mid_price": ("mid price", "mid", "market price", "market_price", "mkt prc", "mkt price", "px last", "last price", "bval price"),
    "stock_price": ("stock price", "stock_price", "equity price", "underlying price", "underlying_price", "stock px", "equity px"),
    "price_currency": ("currency", "price currency", "bond price currency", "price_currency", "bond_price_currency"),
    "sender_name": ("sender name", "sender", "salesperson"),
    "subject": ("subject", "message subject"),
    "keyword": ("keyword", "keywords"),
}


def load_price_history_file(
    path: str | Path,
    *,
    instrument_id: str = "",
    contract_id: str = "",
) -> list[PriceQuoteRow]:
    """Load a raw CB quote-history export from CSV or XLSX."""

    source = Path(path)
    _reject_oversized_file(source)
    suffix = source.suffix.lower()
    if suffix == ".xlsx":
        return load_price_history_xlsx(source, instrument_id=instrument_id, contract_id=contract_id)
    if suffix == ".csv":
        with source.open("r", newline="", encoding="utf-8-sig") as handle:
            return parse_price_history_csv(
                handle,
                source_file=str(source),
                instrument_id=instrument_id,
                contract_id=contract_id,
            )
    raise ValueError(f"unsupported price-history file type: {source.suffix}")


def parse_price_history_csv(
    handle: Iterable[str],
    *,
    source_file: str = "",
    instrument_id: str = "",
    contract_id: str = "",
) -> list[PriceQuoteRow]:
    reader = csv.DictReader(handle)
    if not reader.fieldnames:
        raise ValueError("price history CSV must include a header row")
    rows = []
    for source_row, raw in enumerate(reader, start=2):
        if source_row - 1 > MAX_CSV_ROWS:
            raise ValueError(f"price history CSV exceeds {MAX_CSV_ROWS} data rows")
        rows.append([raw.get(header, "") for header in reader.fieldnames])
    return parse_price_history_table(
        reader.fieldnames,
        rows,
        source_file=source_file,
        source_sheet="csv",
        first_source_row=2,
        instrument_id=instrument_id,
        contract_id=contract_id,
    )


def parse_price_history_csv_text(
    text: str,
    *,
    source_file: str = "<text>",
    instrument_id: str = "",
    contract_id: str = "",
) -> list[PriceQuoteRow]:
    return parse_price_history_csv(
        StringIO(text),
        source_file=source_file,
        instrument_id=instrument_id,
        contract_id=contract_id,
    )


def load_price_history_xlsx(
    path: str | Path,
    *,
    instrument_id: str = "",
    contract_id: str = "",
) -> list[PriceQuoteRow]:
    source = Path(path)
    tables = _read_xlsx_tables(source)
    return _parse_price_history_xlsx_tables(
        tables,
        source_file=str(source),
        instrument_id=instrument_id,
        contract_id=contract_id,
    )


def _parse_price_history_xlsx_tables(
    tables: Sequence[tuple[str, Sequence[Sequence[object]]]],
    *,
    source_file: str,
    instrument_id: str = "",
    contract_id: str = "",
) -> list[PriceQuoteRow]:
    """Parse recognized quote sheets and ignore README/other-format sheets.

    Once a sheet advertises the required quote columns, malformed rows still
    fail closed.  Only sheets whose headers clearly belong to another format
    are skipped.
    """

    all_rows: list[PriceQuoteRow] = []
    recognized_sheets: list[str] = []
    for sheet_name, table in tables:
        if not table:
            continue
        headers = table[0]
        header_map = _header_map([str(header or "") for header in headers])
        if not {"reference_security", "date"}.issubset(set(header_map.values())):
            continue
        recognized_sheets.append(sheet_name)
        data_rows = table[1:]
        all_rows.extend(
            parse_price_history_table(
                headers,
                data_rows,
                source_file=source_file,
                source_sheet=sheet_name,
                first_source_row=2,
                instrument_id=instrument_id,
                contract_id=contract_id,
            )
        )
    if not recognized_sheets:
        raise ValueError("no XLSX worksheet contains the required CB quote columns: reference_security and date")
    return all_rows


def parse_price_history_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    source_file: str = "",
    source_sheet: str = "",
    first_source_row: int = 2,
    instrument_id: str = "",
    contract_id: str = "",
) -> list[PriceQuoteRow]:
    header_map = _header_map([str(header or "") for header in headers])
    _require_columns(header_map, ("reference_security", "date"))
    parsed: list[PriceQuoteRow] = []
    for offset, raw_values in enumerate(rows):
        source_row = first_source_row + offset
        if _is_blank_row(raw_values):
            continue
        raw = {str(headers[index] or ""): _string_value(raw_values[index]) if index < len(raw_values) else "" for index in range(len(headers))}
        normalized = _normalize_row(raw, header_map)
        reference_security = _required_value(normalized, "reference_security", source_row)
        bid = _parse_optional_float(normalized.get("bid_price"), "bid_price", source_row)
        ask = _parse_optional_float(normalized.get("ask_price"), "ask_price", source_row)
        explicit_mid = _parse_optional_float(normalized.get("mid_price"), "mid_price", source_row)
        stock_price = _parse_optional_float(normalized.get("stock_price"), "stock_price", source_row)
        mid = explicit_mid if explicit_mid is not None else _mid_price(bid, ask)
        if bid is None and ask is None and mid is None:
            # Message-only rows are useful as raw provenance in Bloomberg, but
            # the price database is for numeric quote history.
            continue
        parsed.append(
            PriceQuoteRow(
                reference_security=reference_security,
                as_of_date=_parse_quote_date(_required_value(normalized, "date", source_row)),
                as_of_time=_parse_quote_time(normalized.get("time")),
                dealer=(normalized.get("dealer") or "").strip(),
                source_type=(normalized.get("source_type") or "").strip(),
                security=(normalized.get("security") or "").strip(),
                bid_price=bid,
                ask_price=ask,
                mid_price=mid,
                stock_price=stock_price,
                price_currency=(normalized.get("price_currency") or "").strip().upper(),
                sender_name=(normalized.get("sender_name") or "").strip(),
                subject=(normalized.get("subject") or "").strip(),
                keyword=(normalized.get("keyword") or "").strip(),
                instrument_id=(instrument_id or normalized.get("instrument_id") or reference_security).strip(),
                contract_id=(contract_id or normalized.get("contract_id") or "").strip(),
                source_file=source_file,
                source_sheet=source_sheet,
                source_row=source_row,
            )
        )
    return parsed


def _read_xlsx_tables(path: Path) -> list[tuple[str, list[list[str]]]]:
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        _validate_xlsx_archive(archive)
        shared_strings = _read_shared_strings(archive, ns)
        sheet_names = _workbook_sheet_names(archive, ns)
        sheet_paths = sorted(name for name in archive.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"))
        tables: list[tuple[str, list[list[str]]]] = []
        for index, sheet_path in enumerate(sheet_paths, start=1):
            xml = _safe_archive_read(archive, sheet_path)
            root = ET.fromstring(xml)
            rows: list[list[str]] = []
            cell_count = 0
            for row in root.findall(".//main:sheetData/main:row", ns):
                if len(rows) >= MAX_XLSX_ROWS_PER_SHEET:
                    raise ValueError(f"XLSX sheet {sheet_path} exceeds {MAX_XLSX_ROWS_PER_SHEET} rows")
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
            sheet_name = sheet_names[index - 1] if index - 1 < len(sheet_names) else f"sheet{index}"
            tables.append((sheet_name, rows))
        return tables


def _read_shared_strings(archive: zipfile.ZipFile, ns: dict[str, str]) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(_safe_archive_read(archive, "xl/sharedStrings.xml"))
    strings: list[str] = []
    for item in root.findall("main:si", ns):
        parts = [node.text or "" for node in item.findall(".//main:t", ns)]
        strings.append("".join(parts))
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
    value = cell.findtext("main:v", default="", namespaces=ns)
    return value or ""


def _column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    index = 0
    for letter in letters:
        index = index * 26 + (ord(letter) - ord("A") + 1)
    return index or 1


def _header_map(fieldnames: list[str]) -> dict[str, str]:
    canonical_by_alias = {}
    for canonical, aliases in ALIASES.items():
        for alias in aliases:
            canonical_by_alias[_normalize_header(alias)] = canonical
    return {field: canonical_by_alias.get(_normalize_header(field), _normalize_header(field)) for field in fieldnames}


def _normalize_row(raw: dict[str, str], header_map: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for header, value in raw.items():
        canonical = header_map.get(header, _normalize_header(header))
        if canonical in normalized and not _is_blank(normalized[canonical]) and not _is_blank(value):
            if str(normalized[canonical]).strip() != str(value).strip():
                raise ValueError(f"conflicting values supplied for canonical column {canonical!r}")
        if canonical not in normalized or _is_blank(normalized[canonical]):
            normalized[canonical] = value
    return normalized


def _normalize_header(value: str) -> str:
    return " ".join((value or "").strip().lower().replace("_", " ").split())


def _require_columns(header_map: dict[str, str], required: tuple[str, ...]) -> None:
    present = set(header_map.values())
    missing = [name for name in required if name not in present]
    if missing:
        raise ValueError(f"required column(s) missing from price history: {', '.join(missing)}")


def _required_value(row: dict[str, str], key: str, source_row: int) -> str:
    value = row.get(key)
    if _is_blank(value):
        raise ValueError(f"required column {key!r} is blank on source row {source_row}")
    return str(value).strip()


def _string_value(value: object) -> str:
    return "" if value is None else str(value)


def _is_blank(value: object) -> bool:
    return value is None or str(value).strip() == ""


def _is_blank_row(values: Iterable[object]) -> bool:
    return all(_is_blank(value) for value in values)


def _parse_optional_float(value: object, field: str, source_row: int) -> float | None:
    if _is_blank(value):
        return None
    text = str(value).strip().replace(",", "")
    if text.upper().startswith("#N/A"):
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"invalid numeric value for {field!r} on source row {source_row}: {value!r}") from exc


def _mid_price(bid: float | None, ask: float | None) -> float | None:
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return bid if ask is None else ask


def _parse_quote_date(value: str) -> date:
    text = value.strip()
    # Bloomberg-style quote exports commonly use explicit US mm/dd/yy dates.
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d", "%Y%m%d", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    # Excel serial dates are possible in bare OpenXML cells if no shared string
    # was written.  Use Excel's 1899-12-30 convention.
    if re.fullmatch(r"\d+(\.\d+)?", text):
        serial = float(text)
        return datetime.fromordinal(datetime(1899, 12, 30).toordinal() + int(serial)).date()
    return date.fromisoformat(text)


def _parse_quote_time(value: object) -> time | None:
    if _is_blank(value):
        return None
    text = str(value).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    # Excel fractional day.
    try:
        fraction = float(text)
    except ValueError:
        return None
    if 0 <= fraction < 1:
        seconds = round(fraction * 24 * 60 * 60)
        return time(seconds // 3600, (seconds % 3600) // 60, seconds % 60)
    return None
