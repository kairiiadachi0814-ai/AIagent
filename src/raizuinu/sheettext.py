"""xlsx を、モデルに渡せる文字（タブ区切りの行）にする。標準ライブラリのみ。

請求書の確認（invoicecheck）で、取引先から届く Excel の注文書や管理表を読むために使う。
書式は捨て、セルの値だけを行ごとに並べる。日付の書式が付いた数値は日付に直す
（「令和8年10月5日」と表示されるセルの中身は 46300 のような通し番号のため）。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, timedelta
from io import BytesIO

from .linksync import _cell_ref_parts, _col_index, _rels, _shared_strings

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

# 組み込みの日付書式（14〜22: 欧文、27〜36・50〜58: 和暦など、45〜47: 時刻）
_BUILTIN_DATE_FORMATS = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))
_EXCEL_EPOCH = date(1899, 12, 30)

DEFAULT_MAX_CHARS = 60000
DEFAULT_MAX_ROWS = 500


def _is_date_format(code: str) -> bool:
    """独自の表示形式が日付か（y・d・和暦の g/e を含む。引用符や [$-411] の中は見ない）。"""
    cleaned = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", str(code or ""))
    if re.search(r"0\.?0*E[+-]", cleaned, re.I):
        return False  # 指数表記
    return bool(re.search(r"[yd]|g{2,}|e", cleaned, re.I))


def _date_styles(z: zipfile.ZipFile) -> set[int]:
    """日付として表示するセル書式（cellXfs の番号）。"""
    if "xl/styles.xml" not in z.namelist():
        return set()
    root = ET.fromstring(z.read("xl/styles.xml"))
    custom = {
        int(fmt.get("numFmtId", "0")): fmt.get("formatCode", "")
        for fmt in root.iter(_NS + "numFmt")
    }
    styles: set[int] = set()
    xfs = root.find(_NS + "cellXfs")
    if xfs is None:
        return styles
    for index, xf in enumerate(xfs.findall(_NS + "xf")):
        fmt_id = int(xf.get("numFmtId", "0") or 0)
        if fmt_id in _BUILTIN_DATE_FORMATS or (fmt_id in custom and _is_date_format(custom[fmt_id])):
            styles.add(index)
    return styles


def _number_text(raw: str) -> str:
    """1380.0 → 1380。桁の多い小数はそのまま。"""
    try:
        value = float(raw)
    except ValueError:
        return raw
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return ("%.6f" % value).rstrip("0").rstrip(".")


def _cell_text(cell: ET.Element, strings: list[str], date_styles: set[int]) -> str:
    kind = cell.get("t", "")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(_NS + "t"))
    value = cell.find(_NS + "v")
    raw = (value.text or "") if value is not None else ""
    if not raw:
        return ""
    if kind == "s":
        try:
            return strings[int(raw)]
        except (ValueError, IndexError):
            return ""
    if kind in ("str", "b", "e"):
        return raw
    if int(cell.get("s", "0") or 0) in date_styles:
        try:
            serial = float(raw)
            if 1 <= serial < 2958466:  # 〜9999-12-31
                return (_EXCEL_EPOCH + timedelta(days=int(serial))).isoformat()
        except ValueError:
            pass
    return _number_text(raw)


def read_sheets(data: bytes) -> list[tuple[str, list[dict[int, str]]]]:
    """ブックを (シート名, 行) の並びにする。行は {列番号（A=1）: セルの文字}。空の行は入れない。

    セルの中の改行はそのまま残す（コメント欄のように、改行に意味がある表のため）。
    """
    out: list[tuple[str, list[dict[int, str]]]] = []
    with zipfile.ZipFile(BytesIO(data)) as z:
        strings = _shared_strings(z)
        date_styles = _date_styles(z)
        workbook = ET.fromstring(z.read("xl/workbook.xml"))
        rels = _rels(z, "xl/_rels/workbook.xml.rels")
        for sheet in workbook.iter(_NS + "sheet"):
            target = rels.get(sheet.get(_REL_ID, ""), "")
            path = target.lstrip("/")
            if path and not path.startswith("xl/"):
                path = "xl/" + path
            if not path or path not in z.namelist():
                continue
            rows: list[dict[int, str]] = []
            for row in ET.fromstring(z.read(path)).iter(_NS + "row"):
                cells: dict[int, str] = {}
                for cell in row.findall(_NS + "c"):
                    letters, _ = _cell_ref_parts(cell.get("r", ""))
                    text = _cell_text(cell, strings, date_styles).strip()
                    if text:
                        cells[_col_index(letters) if letters else len(cells) + 1] = text
                if cells:
                    rows.append(cells)
            out.append((sheet.get("name", ""), rows))
    return out


def xlsx_to_text(data: bytes, max_chars: int = DEFAULT_MAX_CHARS, max_rows: int = DEFAULT_MAX_ROWS) -> str:
    """ブック全体を「# シート: 名前」＋タブ区切りの行にする。空の行は飛ばす。"""
    out: list[str] = []
    for name, rows in read_sheets(data):
        out.append(f"# シート: {name}")
        for count, cells in enumerate(rows, start=1):
            flat = {col: re.sub(r"[ \t\r\n　]+", " ", text).strip() for col, text in cells.items()}
            out.append("\t".join(flat.get(col, "") for col in range(1, max(flat) + 1)))
            if count >= max_rows:
                out.append("（以下省略）")
                break
    text = "\n".join(out)
    return text if len(text) <= max_chars else text[:max_chars] + "\n（以下省略）"
