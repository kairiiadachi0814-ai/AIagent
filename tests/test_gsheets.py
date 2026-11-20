"""Googleスプレッドシートの窓口（支払通知書のタブの複製・書き込み・PDF書き出し）。"""

from datetime import date

import pytest

from raizuinu.gsheets import PDF_PARAMS, SheetsClient, SheetsError


class FakeHttp:
    def __init__(self):
        self.calls = []
        self.tabs = [{"sheetId": 1, "title": "2026年10月(源泉なし)", "index": 1}]
        self.pdf = b"%PDF-1.4 fake"
        self.status = 200

    def __call__(self, method, url, headers=None, params=None, json=None, raw=False):
        self.calls.append((method, url, params, json))
        assert headers == {"Authorization": "Bearer token-1"}
        if raw:
            return self.status, self.pdf
        if self.status != 200:
            return self.status, {"error": {"message": "The caller does not have permission"}}
        if method == "GET" and "/values/" not in url:
            return 200, {"sheets": [{"properties": t} for t in self.tabs]}
        if url.endswith(":batchUpdate") and "values" not in url:
            request = json["requests"][0]
            if "duplicateSheet" in request:
                new = {"sheetId": 77, "title": request["duplicateSheet"]["newSheetName"], "index": 1}
                self.tabs.append(new)
                return 200, {"replies": [{"duplicateSheet": {"properties": new}}]}
            return 200, {"replies": [{}]}
        if method == "GET":
            return 200, {"values": [[101240]]}
        return 200, {}


@pytest.fixture
def client():
    http = FakeHttp()
    return SheetsClient("SHEET", http=http, token=lambda: "token-1"), http


def test_a_tab_is_copied_next_to_the_template(client):
    sheets, http = client
    assert sheets.duplicate("2026年10月(源泉なし)", "2026年11月(源泉なし)") == 77
    request = http.calls[-1][3]["requests"][0]["duplicateSheet"]
    assert request == {"sourceSheetId": 1, "insertSheetIndex": 1, "newSheetName": "2026年11月(源泉なし)"}


def test_an_existing_tab_is_never_overwritten(client):
    sheets, http = client
    with pytest.raises(SheetsError, match="既にあります"):
        sheets.duplicate("2026年10月(源泉なし)", "2026年10月(源泉なし)")
    with pytest.raises(SheetsError, match="見当たりません"):
        sheets.duplicate("無いタブ", "2026年11月(源泉なし)")
    assert all("duplicateSheet" not in str(call[3]) for call in http.calls)


def test_dates_are_written_as_serial_numbers_and_none_clears(client):
    sheets, http = client
    sheets.write("2026年11月(源泉なし)", {"Q4": date(2026, 12, 1), "N20": 100000, "K20": None, "D20": "業務委託費"})
    body = http.calls[-1][3]
    assert body["valueInputOption"] == "RAW"
    assert {d["range"]: d["values"][0][0] for d in body["data"]} == {
        "'2026年11月(源泉なし)'!Q4": 46357, "'2026年11月(源泉なし)'!N20": 100000,
        "'2026年11月(源泉なし)'!K20": "", "'2026年11月(源泉なし)'!D20": "業務委託費",
    }
    assert sheets.read_cell("2026年11月(源泉なし)", "B15") == 101240


def test_a_row_is_added_inside_the_range_and_copied(client):
    sheets, http = client
    sheets.add_row_like(77, 34, "R")
    insert, paste = http.calls[-1][3]["requests"]
    assert insert["insertDimension"]["range"] == {"sheetId": 77, "dimension": "ROWS", "startIndex": 33, "endIndex": 34}
    assert paste["copyPaste"]["source"]["startRowIndex"] == 34 and paste["copyPaste"]["destination"]["startRowIndex"] == 33
    assert paste["copyPaste"]["source"]["endColumnIndex"] == 18


def test_a_row_format_is_copied_without_touching_values(client):
    sheets, http = client
    sheets.copy_row_format(77, 21, 22, 24, "R")
    paste = http.calls[-1][3]["requests"][0]["copyPaste"]
    assert paste["pasteType"] == "PASTE_FORMAT"
    assert (paste["source"]["startRowIndex"], paste["source"]["endRowIndex"]) == (20, 21)
    assert (paste["destination"]["startRowIndex"], paste["destination"]["endRowIndex"]) == (21, 24)
    calls = len(http.calls)
    sheets.copy_row_format(77, 21, 22, 21, "R")   # 写す先が無ければ何もしない
    assert len(http.calls) == calls


def test_the_pdf_leaves_out_cell_notes(client):
    sheets, http = client
    assert sheets.export_pdf(77) == b"%PDF-1.4 fake"
    params = http.calls[-1][2]
    assert params["gid"] == "77" and params["format"] == "pdf" and params["printnotes"] == "false"
    assert PDF_PARAMS["gridlines"] == "false"
    http.pdf = b"<html>sign in</html>"
    with pytest.raises(SheetsError, match="PDF"):
        sheets.export_pdf(77)


def test_a_refused_call_says_why(client):
    sheets, http = client
    http.status = 403
    with pytest.raises(SheetsError, match="403"):
        sheets.sheets()
