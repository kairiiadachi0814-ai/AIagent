"""卸の出荷管理表（Googleスプレッドシート「タスク管理」）の読み取り。

卸の営業担当が納品チームへ出す依頼（商品発送・在庫確保・在庫確認）を、月ごとのタブに
1行1件で控えた表。発行請求書の確認（invoicecheck）で、注文書と請求書の内容を出荷の実績と
突き合わせるために使う（指示 2026-11-05: 卸の請求書確認の際に使い、発注書と請求書の内容の
確認に使うこと）。

ここでは表を行に直し、取引先と期間で絞るところまで（判定は invoicematch、コメント欄の
読み取りは invoiceread）。表の形（2026-11-05 時点）:
- タブは月ごと（「2026/10」「2026-11」。xlsx に書き出すと「202610」になる）。「テンプレート」など
  月でないタブは読まない
- 見出しは3行目あたり。列の並びは月によって違うので、見出しの名前で列を決める
  （7月は担当者・対応者の列が無い）
- 依頼日は、同じ日の2件目から空欄（上の行と同じ日）
- 完了はチェックボックス。**完了でも出荷していないことがある**（納品チームコメントに
  「全商品在庫ありません」）。出荷の裏付けは伝票番号と納品チームコメントで見る

取得は xlsx 書き出し（認証なし。「リンクを知っている全員」の共有が前提。linksync と同じ）。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from typing import Any, Callable

from .linksync import spreadsheet_export_url
from .sheettext import read_sheets

TASK_SHIP = "商品発送"

# 見出しの名前 → 行の項目。見出しがこの語で始まる列を使う
_COLUMNS = (
    ("依頼日", "request_date"), ("番号", "number"), ("取引先", "vendor"), ("担当者", "owner"),
    ("期日", "due_date"), ("タスク内容", "task"), ("依頼内容チャットリンク", "link"),
    ("依頼者コメント", "comment"), ("対応者", "handler"), ("完了", "done"), ("在庫有無", "stock"),
    ("伝票番号", "tracking"), ("納品チームコメント", "team_comment"),
)
_HEADER_ROWS = 10
_TAB_RE = re.compile(r"^(20\d{2})\D?(\d{1,2})$")
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MONTH_DAY_RE = re.compile(r"(\d{1,2})\s*[月/]\s*(\d{1,2})")
_TRUE_WORDS = {"1", "TRUE", "✓", "✔", "済", "完了", "○", "〇"}
_COMPANY_WORDS = ("株式会社", "有限会社", "合同会社", "(株)", "㈱", "(有)", "㈲", "御中", "様")
_NAME_SPLIT_RE = re.compile(r"[・/、,&＆]")
MIN_NAME_CHARS = 2


def tab_month(name: str) -> tuple[int, int] | None:
    """タブ名から年月。「2026/10」「2026-11」「202610」→ (2026, 10)。月のタブでなければ None。"""
    match = _TAB_RE.match(unicodedata.normalize("NFKC", str(name or "")).strip())
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    return (year, month) if 1 <= month <= 12 else None


def _cell_date(text: str, year: int, month: int) -> str:
    """セルの日付を YYYY-MM-DD に。「10月1日」「10/1」は、タブの年月から年を補う。"""
    text = unicodedata.normalize("NFKC", str(text or "")).strip()
    match = _ISO_RE.match(text)
    if match:
        return text
    match = _MONTH_DAY_RE.search(text)
    if not match:
        return ""
    cell_month, day = int(match.group(1)), int(match.group(2))
    if cell_month - month > 6:  # 1月のタブに 12月の日付
        year -= 1
    elif month - cell_month > 6:  # 12月のタブに 1月の日付
        year += 1
    try:
        return date(year, cell_month, day).isoformat()
    except ValueError:
        return ""


def _header(rows: list[dict[int, str]]) -> tuple[int, dict[int, str]] | None:
    """見出しの行を探す。→ (行の位置, {列番号: 項目名})"""
    for index, cells in enumerate(rows[:_HEADER_ROWS]):
        columns: dict[int, str] = {}
        for col, text in cells.items():
            head = re.sub(r"\s+", "", text)
            field = next((name for label, name in _COLUMNS if head.startswith(label)), "")
            if field and field not in columns.values():
                columns[col] = field
        if {"vendor", "task"} <= set(columns.values()):
            return index, columns
    return None


def parse_workbook(data: bytes) -> list[dict]:
    """出荷管理表のブックを、行（依頼1件）の並びにする。月のタブだけを古い順に読む。"""
    sheets = [(tab_month(name), rows) for name, rows in read_sheets(data)]
    out: list[dict] = []
    for found, rows in sorted((s for s in sheets if s[0] is not None), key=lambda s: s[0]):
        year, month = found
        header = _header(rows)
        if header is None:
            continue
        start, columns = header
        request_date = ""
        for cells in rows[start + 1:]:
            row = {name: "" for _, name in _COLUMNS}
            for col, name in columns.items():
                row[name] = cells.get(col, "")
            if row["request_date"]:
                request_date = _cell_date(row["request_date"], year, month) or request_date
            if not row["vendor"].strip() or not row["task"].strip():
                continue  # 先に作ってある空の行（番号とチェックボックスだけ）
            row["request_date"] = request_date
            row["due_date"] = _cell_date(row["due_date"], year, month)
            row["done"] = unicodedata.normalize("NFKC", row["done"]).strip().upper() in _TRUE_WORDS
            row["month"] = f"{year}-{month:02d}"
            row["id"] = f"{row['month']}#{row['number']}"
            row["label"] = f"{month}月 No.{row['number']}"
            out.append(row)
    return out


def fetch_rows(spreadsheet_id: str, http_get: Callable[[str], tuple[int, str, bytes]] | None = None) -> list[dict]:
    """出荷管理表を取得して行にする。取得できなければ例外。"""
    if http_get is None:
        import requests

        def http_get(url: str) -> tuple[int, str, bytes]:
            resp = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
            return resp.status_code, resp.url, resp.content

    status, final_url, body = http_get(spreadsheet_export_url(spreadsheet_id))
    if status != 200 or not body.startswith(b"PK"):
        # 共有が「リンクを知っている全員」でないと、ログイン画面（HTML）が返る
        raise RuntimeError(f"出荷管理表を取得できません（HTTP {status}。共有設定を確認してください）")
    return parse_workbook(body)


def _name_key(text: Any) -> str:
    out = unicodedata.normalize("NFKC", str(text or ""))
    out = re.sub(r"\([^)]*\)", "", out)  # 「スーパーデリバリー（8.）」の添え書き
    for word in _COMPANY_WORDS:
        out = out.replace(word, "")
    return re.sub(r"\s+", "", out)


def vendor_matches(cell: str, names: list[str]) -> bool:
    """取引先の欄が、その取引先を指しているか。

    欄は略称のことがある（「旅サラダ」と「旅サラダストア」、「ナガセ」と「ナガセテクノサービス」）
    ので、どちらかがもう一方を含めば同じとみる。「リマーク・サクラマチ」は両方に当たる。
    """
    keys = [k for k in (_name_key(n) for n in names) if len(k) >= MIN_NAME_CHARS]
    for part in _NAME_SPLIT_RE.split(unicodedata.normalize("NFKC", str(cell or ""))):
        part = _name_key(part)
        if len(part) >= MIN_NAME_CHARS and any(part in key or key in part for key in keys):
            return True
    return False


def rows_for(rows: list[dict], names: list[str], start: str = "", end: str = "") -> list[dict]:
    """その取引先の行のうち、依頼日か期日が期間（YYYY-MM-DD。両端を含む）に入るもの。"""
    out = []
    for row in rows:
        if not vendor_matches(row.get("vendor", ""), names):
            continue
        dates = [d for d in (row.get("request_date"), row.get("due_date")) if d]
        if (start or end) and not any((not start or d >= start) and (not end or d <= end) for d in dates):
            continue
        out.append(row)
    return out


def shift(day: str, days: int) -> str:
    """YYYY-MM-DD を days 日ずらす。日付でなければ空。"""
    try:
        return (date.fromisoformat(day) + timedelta(days=days)).isoformat()
    except (TypeError, ValueError):
        return ""


def one_line(text: Any, limit: int = 0) -> str:
    """セルの文を1行にする（改行は「／」）。limit を超えたら「…」で切る。"""
    out = "／".join(part.strip() for part in re.split(r"[\r\n]+", str(text or "")) if part.strip())
    out = re.sub(r"[ \t　]+", " ", out)
    return out if not limit or len(out) <= limit else out[:limit] + "…"
