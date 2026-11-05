"""卸の出荷管理表（Googleスプレッドシート）の読み取りのテスト。

表の形は実物（2026-11-05 時点）に合わせる: 月ごとのタブ、見出しは3行目、列の並びは月によって違う、
依頼日は同じ日の2件目から空欄、完了はチェックボックス。
"""

from __future__ import annotations

import zipfile
from io import BytesIO
from xml.sax.saxutils import escape

import pytest

from raizuinu import shiplog

HEADER = ["依頼日", "番号", "取引先", "担当者", "期日", "タスク内容", "依頼内容チャットリンク", "依頼者コメント",
          "対応者", "完了", "在庫有無", "伝票番号\n※グッズ、新日本カレンダーは\n配送業者も記載", "納品チームコメント"]
OLD_HEADER = ["依頼日", "番号", "取引先", "期日", "タスク内容", "依頼内容チャットリンク", "依頼者コメント", "完了",
              "在庫有無", "伝票番号", "納品チームコメント"]  # 7月: 担当者・対応者の列が無い
BANNER = [None, None, "本日期日 0件　／　期限超過(昨日以前) 0件　／　未完了 0件"]


def _column(index: int) -> str:
    out = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def make_xlsx(sheets: dict[str, list[list]]) -> bytes:
    """最小の xlsx。文字はセルに直に書き、True／False はチェックボックス（真偽値）にする。"""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        names = "".join(
            f'<sheet name="{escape(name)}" sheetId="{n}" r:id="rId{n}"/>' for n, name in enumerate(sheets, start=1)
        )
        z.writestr("xl/workbook.xml", (
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{names}</sheets></workbook>"
        ))
        z.writestr("xl/_rels/workbook.xml.rels", (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                f'Target="worksheets/sheet{n}.xml"/>' for n in range(1, len(sheets) + 1)
            ) + "</Relationships>"
        ))
        for n, rows in enumerate(sheets.values(), start=1):
            body = ""
            for r, row in enumerate(rows, start=1):
                cells = ""
                for c, value in enumerate(row, start=1):
                    ref = f"{_column(c)}{r}"
                    if value is None or value == "":
                        continue
                    if isinstance(value, bool):
                        cells += f'<c r="{ref}" t="b"><v>{int(value)}</v></c>'
                    else:
                        cells += f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
                body += f'<row r="{r}">{cells}</row>'
            z.writestr(f"xl/worksheets/sheet{n}.xml", (
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f"<sheetData>{body}</sheetData></worksheet>"
            ))
    return buffer.getvalue()


def october():
    return [
        BANNER, [],
        HEADER,
        ["2026-10-01", 1, "スーパーデリバリー", "梶本", "2026-10-02", "商品発送", "https://www.chatwork.com/#!rid1-2", "納品書添付いたします。", "", True],
        ["", 6, "サクラマチ", "山田", "2026-10-05", "商品発送", "", "10/6AM必着です。", "竹内", True, "", "391399216272\n391399200695"],
        ["10月2日", 7, "ナガセテクノサービス", "山田", "10月6日", "商品発送", "", "10/7着依頼です。", "", True, "下釜、本田", "675-9154-5711",
         "R0560-A\nR0528-A\n上記2品番以外発送済"],
        ["", 10, "新日本カレンダー", "山田", "2026-10-05", "在庫確保（在庫ロボット・エアロジ共に減算お願いいたします）", "", "R0469-J-S 240枚", "", True],
        ["", 17, "リマーク・サクラマチ", "山田", "2026-10-06", "在庫確保（SCM記載分済み）", "", "", "", True],
        ["2026-10-29", 96, "ナガセテクノサービス", "河村", "2026-11-02", "商品発送", "", "在庫ある分のみ発送お願い致します。", "竹内・寺坂", False],
        ["", 97, "", "", "", "", "", "", "", False],  # 先に作ってある空の行
        ["", 98, "", "", "", "", "", "", "", False],
    ]


def book():
    return make_xlsx({
        "202610": october(),  # xlsx に書き出すと「2026/10」の「/」が落ちる
        "20267": [BANNER, [], OLD_HEADER, ["2026-07-27", 1, "ヨドバシカメラ", "2026-07-28", "商品発送", "", "", True, "", "12345"]],
        "2026-11": [BANNER, [None] * 6 + ["使い方：このタブを複製→タブ名変更→4行目から入力"], HEADER,
                    ["2026-11-04", 3, "リマーク", "河村", "2026-11-06", "商品発送", "", "リマーク発注書です。", "", False]],
        "テンプレート": [BANNER, [], HEADER, ["", "", "", "", "", "", "", "", "", False]],
        "シート2": [],
    })


class TestSheet:
    def test_only_the_month_tabs_are_read_oldest_first(self):
        rows = shiplog.parse_workbook(book())
        assert [r["id"] for r in rows] == ["2026-07#1", "2026-10#1", "2026-10#6", "2026-10#7", "2026-10#10", "2026-10#17", "2026-10#96", "2026-11#3"]
        assert rows[3]["label"] == "10月 No.7" and rows[3]["month"] == "2026-10"

    @pytest.mark.parametrize("name, expected", [
        ("2026/10", (2026, 10)), ("202610", (2026, 10)), ("20269", (2026, 9)), ("2026-11", (2026, 11)),
        ("２０２６／７", (2026, 7)), ("テンプレート", None), ("シート2", None), ("202613", None), ("", None),
    ])
    def test_tab_names(self, name, expected):
        assert shiplog.tab_month(name) == expected

    def test_columns_are_found_by_their_headings(self):
        rows = {r["id"]: r for r in shiplog.parse_workbook(book())}
        old = rows["2026-07#1"]  # 担当者・対応者の列が無い月
        assert (old["vendor"], old["due_date"], old["task"], old["done"], old["tracking"], old["owner"]) == ("ヨドバシカメラ", "2026-07-28", "商品発送", True, "12345", "")
        new = rows["2026-10#7"]
        assert (new["owner"], new["stock"], new["tracking"]) == ("山田", "下釜、本田", "675-9154-5711")
        assert new["team_comment"] == "R0560-A\nR0528-A\n上記2品番以外発送済"  # 改行は残す

    def test_the_request_date_carries_down_and_text_dates_take_the_year_of_the_tab(self):
        rows = {r["id"]: r for r in shiplog.parse_workbook(book())}
        assert rows["2026-10#6"]["request_date"] == "2026-10-01"  # 空欄は上の行と同じ日
        assert (rows["2026-10#7"]["request_date"], rows["2026-10#7"]["due_date"]) == ("2026-10-02", "2026-10-06")  # 「10月2日」
        assert rows["2026-10#17"]["request_date"] == "2026-10-02"
        assert (rows["2026-10#96"]["request_date"], rows["2026-10#96"]["due_date"], rows["2026-10#96"]["done"]) == ("2026-10-29", "2026-11-02", False)

    def test_a_december_date_on_a_january_tab_belongs_to_the_year_before(self):
        data = make_xlsx({"2027/1": [HEADER, ["12月28日", 1, "リマーク", "山田", "1/5", "商品発送", "", "", "", True]]})
        row = shiplog.parse_workbook(data)[0]
        assert (row["request_date"], row["due_date"]) == ("2026-12-28", "2027-01-05")

    def test_a_tab_without_the_headings_is_skipped(self):
        assert shiplog.parse_workbook(make_xlsx({"2026/10": [["メモ"], ["取引先だけ", "取引先"]]})) == []


class TestRowsForACustomer:
    @pytest.mark.parametrize("cell, names, expected", [
        ("ナガセテクノサービス", ["ナガセテクノサービス"], True),
        ("ナガセテクノサービス", ["ナガセ", "株式会社ナガセテクノサービス 御中"], True),
        ("旅サラダストア", ["旅サラダ"], True),  # 略称
        ("リマーク・サクラマチ", ["サクラマチ"], True),  # 2社ぶんの行
        ("リマーク・サクラマチ", ["株式会社リマーク"], True),
        ("スーパーデリバリー（8.）", ["スーパーデリバリー"], True),
        ("カワタキ", ["カワタキコーポレーション"], True),
        ("新日本カレンダー", ["サクラマチ", "株式会社サクラマチ"], False),
        ("グッズ", ["株式会社"], False),  # 会社の種類だけでは当たらない
        ("", ["リマーク"], False),
    ])
    def test_the_customer_cell_is_matched_loosely(self, cell, names, expected):
        assert shiplog.vendor_matches(cell, names) is expected

    def test_rows_are_cut_to_the_customer_and_the_period(self):
        rows = shiplog.parse_workbook(book())
        nagase = shiplog.rows_for(rows, ["ナガセテクノサービス"], "2026-09-24", "2026-11-14")
        assert [r["number"] for r in nagase] == ["7", "96"]
        assert [r["number"] for r in shiplog.rows_for(rows, ["ナガセ"], "2026-10-25", "2026-10-31")] == ["96"]  # 依頼日が期間内
        assert [r["number"] for r in shiplog.rows_for(rows, ["ナガセ"], "2026-11-01", "2026-11-05")] == ["96"]  # 期日が期間内
        assert shiplog.rows_for(rows, ["ナガセ"], "2026-11-03", "2026-11-30") == []
        assert [r["id"] for r in shiplog.rows_for(rows, ["リマーク"])] == ["2026-10#17", "2026-11#3"]  # 期間なしは全部


class TestFetch:
    def test_the_sheet_is_fetched_as_xlsx(self):
        asked = []

        def http_get(url):
            asked.append(url)
            return 200, url, book()

        rows = shiplog.fetch_rows("SHEET-ID", http_get)
        assert asked == ["https://docs.google.com/spreadsheets/d/SHEET-ID/export?format=xlsx"] and len(rows) == 8

    def test_a_login_page_instead_of_the_sheet_is_an_error(self):
        with pytest.raises(RuntimeError, match="共有設定"):
            shiplog.fetch_rows("SHEET-ID", lambda url: (200, url, b"<html>Sign in</html>"))
        with pytest.raises(RuntimeError, match="HTTP 404"):
            shiplog.fetch_rows("SHEET-ID", lambda url: (404, url, b""))


class TestText:
    def test_cell_text_on_one_line(self):
        assert shiplog.one_line("福山通運\n67592444684\r\n67592444695") == "福山通運／67592444684／67592444695"
        assert shiplog.one_line("全商品　在庫ありません。", 5) == "全商品 在…"
        assert shiplog.one_line(None) == ""

    def test_shifting_a_date(self):
        assert shiplog.shift("2026-10-01", -7) == "2026-09-24" and shiplog.shift("2026-10-29", 14) == "2026-11-12"
        assert shiplog.shift("10月1日", 1) == "" and shiplog.shift("", 0) == ""
