"""Googleスプレッドシートの窓口 — ひな形のタブを複製し、値を書き、PDFに書き出す。

支払通知書（`paynotice.py`）のためのもの。サービスアカウントとして入り、
**共有されたスプレッドシートだけ**を扱う（共有されていないものは403で読めない）。

方針:
- できることを絞る: タブの一覧／複製／値の読み書き／行の追加／自分で作ったタブの削除／PDF書き出し。
  スプレッドシートそのものの作成・削除・共有の変更はしない
- 日付は通し番号（シリアル値）で書く。文字で書くとロケールによって解釈が変わるため
- 鍵はVPS上のファイルに置き、パスだけを環境変数で渡す（予定表と同じサービスアカウント）
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Callable
from urllib.parse import quote

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
EXPORT_URL = "https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"
CREDENTIAL_ENVS = ("GOOGLE_SHEETS_CREDENTIALS_PATH", "GOOGLE_CALENDAR_CREDENTIALS_PATH")
SERIAL_EPOCH = date(1899, 12, 30)

# PDFの書き出し方（A4縦・幅に合わせる・目盛線なし・タブ名やページ番号を付けない）。
# printnotes=false が無いと、セルのメモが「[1]」の印と2ページ目の脚注として出る（2026-11-20 に確認）
PDF_PARAMS = {
    "format": "pdf",
    "size": "A4",
    "portrait": "true",
    "fitw": "true",
    "gridlines": "false",
    "printtitle": "false",
    "sheetnames": "false",
    "pagenumbers": "false",
    "fzr": "false",
    "printnotes": "false",
}


class SheetsError(RuntimeError):
    """スプレッドシートの操作に失敗した。"""


def date_serial(day: date) -> int:
    """日付 → スプレッドシートの通し番号（2026-11-19 → 46345）。"""
    return (day - SERIAL_EPOCH).days


def column_index(letter: str) -> int:
    """列の文字 → 0始まりの番号（A→0、Q→16、AA→26）。"""
    index = 0
    for ch in letter.upper():
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


def credentials_path() -> str:
    for name in CREDENTIAL_ENVS:
        value = os.environ.get(name)
        if value:
            return value
    return ""


class SheetsClient:
    """1つのスプレッドシートを扱う。http は (method, url, headers, params, json) → (status, body)。"""

    def __init__(
        self,
        spreadsheet_id: str,
        credentials: str | None = None,
        http: Callable[..., tuple[int, Any]] | None = None,
        token: Callable[[], str] | None = None,
    ) -> None:
        self._id = spreadsheet_id
        self._credentials = credentials if credentials is not None else credentials_path()
        self._http = http or _requests_http
        self._token_source = token
        self._token = ""

    # --- 読む ---

    def sheets(self) -> list[dict[str, Any]]:
        """タブの一覧 → [{"id": 123, "title": "…", "index": 0, "hidden": False, "rows": 45}]"""
        body = self._call("GET", f"{SHEETS_API}/{self._id}",
                          params={"fields": "sheets.properties(sheetId,title,index,hidden,gridProperties.rowCount)"})
        result = []
        for sheet in body.get("sheets") or []:
            props = sheet.get("properties") or {}
            result.append({
                "id": int(props.get("sheetId", 0)),
                "title": str(props.get("title", "")),
                "index": int(props.get("index", 0)),
                "hidden": bool(props.get("hidden")),
                "rows": int((props.get("gridProperties") or {}).get("rowCount", 0)),
            })
        return result

    def find(self, title: str) -> dict[str, Any] | None:
        return next((s for s in self.sheets() if s["title"] == title), None)

    def read(self, title: str, cells: str, formulas: bool = False) -> list[list[Any]]:
        """値を読む（表示形式を外した値。formulas=True なら数式のまま）。"""
        body = self._call(
            "GET", f"{SHEETS_API}/{self._id}/values/{quote(_a1(title, cells), safe='')}",
            params={"valueRenderOption": "FORMULA" if formulas else "UNFORMATTED_VALUE"},
        )
        return body.get("values") or []

    def read_cell(self, title: str, cell: str) -> Any:
        values = self.read(title, cell)
        return values[0][0] if values and values[0] else ""

    # --- 書く ---

    def duplicate(self, source_title: str, new_title: str) -> int:
        """ひな形のタブを複製する。→ 新しいタブのID。同じ名前のタブがあれば失敗する（上書きしない）。"""
        tabs = self.sheets()
        if any(s["title"] == new_title for s in tabs):
            raise SheetsError(f"「{new_title}」というタブが既にあります")
        source = next((s for s in tabs if s["title"] == source_title), None)
        if source is None:
            raise SheetsError(f"ひな形のタブ「{source_title}」が見当たりません")
        body = self._batch([{
            "duplicateSheet": {
                "sourceSheetId": source["id"],
                "insertSheetIndex": source["index"],
                "newSheetName": new_title,
            }
        }])
        props = ((body.get("replies") or [{}])[0].get("duplicateSheet") or {}).get("properties") or {}
        if "sheetId" not in props:
            raise SheetsError("タブを複製できませんでした")
        return int(props["sheetId"])

    def write(self, title: str, values: dict[str, Any]) -> None:
        """セルごとの値を書く。日付は通し番号に直す。None は空にする。"""
        data = []
        for cell, value in values.items():
            if isinstance(value, date):
                value = date_serial(value)
            data.append({"range": _a1(title, cell), "values": [["" if value is None else value]]})
        if not data:
            return
        self._call("POST", f"{SHEETS_API}/{self._id}/values:batchUpdate",
                   json={"valueInputOption": "RAW", "data": data})

    def add_row_like(self, sheet_id: int, row: int, last_column: str) -> None:
        """row（1始まり）の上に1行足し、row の行の書式・数式・結合を新しい行へ写す。

        行は範囲の内側に足す（合計の数式の範囲が自動で広がるように）。足した後は、
        元の行が1つ下へずれ、新しい行が row の位置に入る。
        """
        end = column_index(last_column) + 1
        self._batch([
            {"insertDimension": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": row - 1, "endIndex": row},
                "inheritFromBefore": True,
            }},
            {"copyPaste": {
                "source": {"sheetId": sheet_id, "startRowIndex": row, "endRowIndex": row + 1,
                           "startColumnIndex": 0, "endColumnIndex": end},
                "destination": {"sheetId": sheet_id, "startRowIndex": row - 1, "endRowIndex": row,
                                "startColumnIndex": 0, "endColumnIndex": end},
                "pasteType": "PASTE_NORMAL",
            }},
        ])

    def copy_row_format(self, sheet_id: int, source_row: int, first_row: int, last_row: int, last_column: str) -> None:
        """source_row の書式（文字の大きさ・配置・表示形式）を first_row〜last_row へ写す。値と結合は変えない。"""
        if last_row < first_row:
            return
        end = column_index(last_column) + 1
        self._batch([{"copyPaste": {
            "source": {"sheetId": sheet_id, "startRowIndex": source_row - 1, "endRowIndex": source_row,
                       "startColumnIndex": 0, "endColumnIndex": end},
            "destination": {"sheetId": sheet_id, "startRowIndex": first_row - 1, "endRowIndex": last_row,
                            "startColumnIndex": 0, "endColumnIndex": end},
            "pasteType": "PASTE_FORMAT",
        }}])

    def delete_sheet(self, sheet_id: int) -> None:
        """タブを消す。呼ぶ側は、自分で作ったタブのIDだけを渡すこと。"""
        self._batch([{"deleteSheet": {"sheetId": int(sheet_id)}}])

    # --- PDF ---

    def export_pdf(self, sheet_id: int) -> bytes:
        """タブ1枚をPDFにする。"""
        status, body = self._http(
            "GET", EXPORT_URL.format(spreadsheet_id=self._id),
            headers=self._headers(), params={**PDF_PARAMS, "gid": str(int(sheet_id))}, raw=True,
        )
        if status != 200 or not isinstance(body, (bytes, bytearray)) or not bytes(body).startswith(b"%PDF"):
            raise SheetsError(f"PDFに書き出せませんでした（HTTP {status}）")
        return bytes(body)

    # --- 内部 ---

    def _batch(self, requests_: list[dict]) -> dict:
        return self._call("POST", f"{SHEETS_API}/{self._id}:batchUpdate", json={"requests": requests_})

    def _call(self, method: str, url: str, params: dict | None = None, json: dict | None = None) -> dict:
        status, body = self._http(method, url, headers=self._headers(), params=params, json=json)
        if status == 401 and self._token:
            self._token = ""  # 期限切れ。取り直してもう一度だけ
            status, body = self._http(method, url, headers=self._headers(), params=params, json=json)
        if status != 200 or not isinstance(body, dict):
            message = ""
            if isinstance(body, dict):
                message = str((body.get("error") or {}).get("message", ""))
            raise SheetsError(f"スプレッドシートの操作に失敗しました（HTTP {status} {message[:200]}）")
        return body

    def _headers(self) -> dict[str, str]:
        if not self._token:
            self._token = self._token_source() if self._token_source else self._service_account_token()
        return {"Authorization": f"Bearer {self._token}"}

    def _service_account_token(self) -> str:
        if not self._credentials:
            raise SheetsError("Googleの鍵の場所（GOOGLE_SHEETS_CREDENTIALS_PATH）が設定されていません")
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(self._credentials, scopes=[SCOPE])
        credentials.refresh(Request())
        return str(credentials.token)


def _a1(title: str, cells: str) -> str:
    return "'" + title.replace("'", "''") + "'!" + cells


def _requests_http(method: str, url: str, headers: dict | None = None, params: dict | None = None,
                   json: dict | None = None, raw: bool = False) -> tuple[int, Any]:
    import requests

    response = requests.request(method, url, headers=headers, params=params, json=json, timeout=60)
    if raw:
        return response.status_code, response.content
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, response.text
