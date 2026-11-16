"""発行請求書の確認（FR-15）のテスト。

実例を元にした形で、照合の規則を固定する（金額・品番は実物を簡略にしたもの）:
- 2026-11-04 ナガセ 10月分: 同じ注文を、注文全体と出荷分の控え2通の両方で計上（二重計上）
- 2026-11-04 旅サラダ: 相殺後の残高の誤記（11,069円を10,069円）
- 2026-11-04 新日本カレンダー: 訂正前の納期の日付が明細に残っていた
- 2026-10-02 ナガセ 9月分: 入荷待ちの2行を請求（承認後に営業の連絡で削除）
- 2026-11-05 サクラマチ: 問題なし（出荷の記録が無い注文は留意どまり）
- 2026-11-05 出荷管理表（卸の発送依頼の一覧）との突き合わせ: ナガセ 9月分の「全商品在庫ありません」、
  10月分の「上記4品番以外発送済」と後日の発送、11/2 期日の出荷、どの注文とも結べない出荷
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from raizuinu.config import Config
from raizuinu.invoicecheck import InvoiceChecker, load_case
from raizuinu.invoicematch import (
    BACKORDER_WAITING, CONFIRM, DOC_BACKORDER, DOC_CONTRACT, DOC_INVOICE, DOC_LEDGER, DOC_ORDER, DOC_SALES,
    FIX, LINE_BOXED, LINE_NORMAL, LINE_STRUCK, NOTE, PURPOSE_CLAIM, PURPOSE_NORMAL, PURPOSE_SAMPLE, VERDICT_OK,
    GroupCheck, build_order_book, check_arithmetic, check_requirements, code_tokens, codes_related, line_kind,
    month_end, norm_code, ship_tasks, task_ships, to_int, weekday_gap,
)
from raizuinu.invoiceread import FEE_MISMATCH, FEE_OK, FEE_UNKNOWN, REVIEW_SYSTEM, InvoiceReader, file_blocks
from raizuinu.invoicereport import legal_notes, render_reply, render_report, summary_rows

JST = timezone(timedelta(hours=9))
ISSUER = {"name": "株式会社ライズクリエイション", "registration_number": "T8150001018250"}
SETTINGS = {"issuers": [ISSUER], "shipping_evidence": "risk"}


def line(date, code, quantity, price, description="", jan="", **extra):
    return {"date": date, "code": code, "jan": jan, "description": description or code, "quantity": quantity,
            "unit": "個", "unit_price": price, "tax_rate": 10, "amount": int(quantity * price),
            "order_ref": "", "note": "", **extra}


def invoice(file, lines, **over):
    subtotal = sum(l["amount"] for l in lines)
    tax = subtotal * 10 // 100
    found = {
        "file": file, "readable": True, "issuer_name": "株式会社ライズクリエイション",
        "registration_number": "T8150001018250", "recipient_name": "テスト商事株式会社",
        "issue_date": "2026-11-05", "invoice_number": "T202610", "due_date": "2026-11-30",
        "has_detail_sheet": False, "detail_invoice_number": "", "detail_registration_number": "",
        "lines": lines, "cover_lines": [], "subtotal": subtotal, "tax_total": tax, "total": subtotal + tax,
        "tax_breakdown": [{"rate": 10, "base": subtotal, "tax": tax}], "reduced_rate_note": True,
        "remarks": [], "stated_figures": [],
    }
    found.update(over)
    return found


def oline(code, quantity, price, status=LINE_NORMAL, jan="", description=""):
    return {"code": code, "jan": jan, "description": description or code, "quantity": quantity,
            "unit_price": price, "amount": int(quantity * price), "status": status, "note": ""}


def order(file, number, order_date, lines, **over):
    found = {
        "file": file, "readable": True, "doc_type": DOC_ORDER, "title": "注文書", "counterparty": "テスト商事株式会社",
        "order_number": number, "order_date": order_date, "ship_date": "", "delivery_date": "", "ship_to": "",
        "shipping_evidence": "", "is_shipment_copy": False, "lines": lines, "subtotal": 0, "tax": 0, "total": 0,
        "terms": [], "figures": [], "backorders": [], "notes": "",
    }
    found.update(over)
    return found


def other(file, doc_type, **over):
    return order(file, "", "", [], doc_type=doc_type, **over)


def run(invoices, sources, mapping=None, review=None, settings=None, unread=None, shipments=None):
    check = GroupCheck("テスト商事", invoices, sources, settings or SETTINGS, unread, shipments)
    check.prepare()
    if mapping is not None:
        check.apply_interpretation(mapping)
    result = check.finish()
    check.apply_review(result, review or {}, FEE_OK, FEE_MISMATCH)
    return check, result


def found(result, code, severity=None):
    return [f for f in result["findings"] if f["code"] == code and (severity is None or f["severity"] == severity)]


# --- ナガセ 10月分の形: 同じ注文の二重計上 ---

ORDER_1001 = [oline("R0560-A", 100, 177), oline("R0406-BK", 4, 1789), oline("R0400-GRAY", 10, 665),
              oline("R0400-GRAY", 10, 665), oline("R0528-A", 8, 1820)]


def shipment_copy(file, ship_date, shipped):
    """出荷分の控え。shipped（行の位置）以外を消し線にする。"""
    lines = []
    for index, base in enumerate(ORDER_1001):
        lines.append({**base, "status": (LINE_BOXED if len(shipped) == 1 else LINE_NORMAL) if index in shipped else LINE_STRUCK})
    return order(file, "ORD-1001", "2026-10-01", lines, is_shipment_copy=True, ship_date=ship_date)


def split_shipment_case():
    sources = [
        order("①注文書.pdf", "ORD-1001", "2026-10-01", ORDER_1001),
        shipment_copy("⑧_261009発送分.pdf", "2026-10-09", {1, 2, 3}),
        shipment_copy("⑨_261013発送分.pdf", "2026-10-13", {0}),
        order("⑤注文書.pdf", "ORD-1016", "2026-10-16", [oline("R0830-A", 1, 6170)]),
    ]
    lines = (
        [line("2026-10-01", o["code"], o["quantity"], o["unit_price"]) for o in ORDER_1001]
        + [line("2026-10-09", "R0406-BK", 4, 1789), line("2026-10-09", "R0400-GRAY", 10, 665), line("2026-10-09", "R0400-GRAY", 10, 665)]
        + [line("2026-10-13", "R0560-A", 100, 177), line("2026-10-16", "R0830-A", 1, 6170)]
    )
    return [invoice("請求書.pdf", lines)], sources


class TestDuplicateOrders:
    def test_copies_of_one_order_are_one_order(self):
        _, sources = split_shipment_case()
        orders, lines = build_order_book(sources)
        assert [(o["order_number"], o["copies"], o["has_shipment_copies"]) for o in orders] == [("ORD-1001", 3, True), ("ORD-1016", 1, False)]
        assert len(lines) == 6  # 控えの行を重ねて数えない
        states = [(l["code"], l["state"], l["ship_date"]) for l in lines]
        assert states[0] == ("R0560-A", "出荷済", "2026-10-13")
        assert states[1] == ("R0406-BK", "出荷済", "2026-10-09")
        assert states[4] == ("R0528-A", "未出荷", "")  # どの控えでも消し線
        assert states[5] == ("R0830-A", "出荷不明", "")

    def test_the_same_order_billed_twice_is_found(self):
        _, result = run(*split_shipment_case())
        dup = found(result, "duplicate")
        assert len(dup) == 1 and dup[0]["severity"] == FIX
        assert dup[0]["amount"] == 17700 + 7156 + 6650 + 6650  # 出荷分で請求済みの4行
        assert "ORD-1001" in dup[0]["detail"] and "10/1付でも 4 行・38,156円" in dup[0]["detail"]
        assert "①注文書.pdf・⑧_261009発送分.pdf・⑨_261013発送分.pdf" in dup[0]["detail"]
        assert result["invoices"][0]["verdict"] == FIX

    def test_a_line_struck_on_every_shipment_copy_is_not_shipped(self):
        _, result = run(*split_shipment_case())
        unshipped = found(result, "not_shipped")
        assert len(unshipped) == 1 and unshipped[0]["severity"] == CONFIRM
        assert unshipped[0]["amount"] == 14560 and "R0528-A" in unshipped[0]["detail"]

    def test_the_shipped_lines_are_matched_by_their_ship_date(self):
        _, result = run(*split_shipment_case())
        by_date = {}
        for row in result["lines"]:
            by_date.setdefault(row["date"], []).append(row["result"])
        assert by_date["2026-10-09"] == ["一致", "一致", "一致"] and by_date["2026-10-13"] == ["一致"]
        assert sorted(by_date["2026-10-01"]) == ["一致（未出荷）", "二重計上", "二重計上", "二重計上", "二重計上"]
        assert result["date_basis"] == {"出荷日": 4, "注文日": 2}

    def test_an_order_without_trouble_only_gets_a_note_about_shipping(self):
        _, result = run(*split_shipment_case())
        notes = found(result, "shipping_evidence")
        assert [n["severity"] for n in notes] == [NOTE] and "ORD-1016" in notes[0]["detail"]


# --- サクラマチの形: 問題なし ---


def clean_case():
    sources = [
        order("①K12073_返信.pdf", "K12073", "2026-10-01", [oline("S0384-B-S", 10, 1312), oline("S0384-H-M", 60, 1312)],
              delivery_date="2026-10-06", shipping_evidence="ヤマト 391399216272"),
        order("②K12079.pdf", "K12079", "2026-10-06", [oline("S0384-H-M", 15, 1312)], delivery_date="2026-10-10"),
        order("③J33.pdf", "J33", "2026-10-08", [oline("S0384-H-M", 5, 1312)], delivery_date="2026-10-10"),
    ]
    lines = [
        line("2026-10-06", "S0384-B-S", 10, 1312, order_ref="No.K12073"),
        line("2026-10-06", "S0384-H-M", 60, 1312, order_ref="No.K12073"),
        line("2026-10-10", "S0384-H-M", 15, 1312, order_ref="No.K12079"),
        line("2026-10-10", "S0384-H-M", 5, 1312, order_ref="No.J33"),
    ]
    return [invoice("請求書.pdf", lines)], sources


class TestCleanInvoice:
    def test_everything_matches_and_nothing_needs_fixing(self):
        check, result = run(*clean_case())
        assert result["invoices"][0]["verdict"] == VERDICT_OK
        assert [f for f in result["findings"] if f["severity"] in (FIX, CONFIRM)] == []
        assert all(row["result"] == "一致" for row in result["lines"])
        assert result["unbilled"] == [] and not check.needs_interpretation()
        assert result["date_basis"] == {"納品日": 4}

    def test_the_order_reference_decides_between_same_items(self):
        _, result = run(*clean_case())
        assert [row["source"] for row in result["lines"]] == ["K12073", "K12073", "K12079", "J33"]

    def test_orders_without_a_shipping_record_are_only_noted(self):
        _, result = run(*clean_case())
        notes = found(result, "shipping_evidence")
        assert len(notes) == 1 and notes[0]["severity"] == NOTE
        assert "K12079" in notes[0]["detail"] and "J33" in notes[0]["detail"] and "K12073" not in notes[0]["detail"]

    def test_always_mode_asks_for_shipping_evidence_every_time(self):
        _, result = run(*clean_case(), settings={**SETTINGS, "shipping_evidence": "always"})
        assert [f["severity"] for f in found(result, "shipping_evidence")] == [CONFIRM]
        assert result["invoices"][0]["verdict"] == CONFIRM


# --- ナガセ 9月分の形: 品切と入荷待ち ---


def backorder_case():
    sources = [
        order("①9.8_請求無し_WHITEMARBLE・R0528-A.pdf", "NTS260908-A1", "2026-09-08",
              [oline("R0528-A", 4, 1820), oline("R0528-B", 4, 1820), oline("R0560-A", 60, 177), oline("R0400-WHITEMARBLE", 8, 665)]),
        order("②9.9.pdf", "NTS260909-A", "2026-09-09", [oline("R0588-C", 4, 1400)]),
        order("③9.18.pdf", "NTS260918-A", "2026-09-18",
              [oline("R0528-A", 4, 1820), oline("R0406-green", 6, 1789), oline("R0400-IVORY", 8, 665), oline("R0400-WHITEMARBLE", 4, 665)]),
    ]
    lines = [
        line("2026-09-08", "R0528-B", 4, 1820), line("2026-09-08", "R0560-A", 60, 177),
        line("2026-09-09", "R0588-C", 4, 1400),
        line("2026-09-18", "R0406-green", 6, 1789), line("2026-09-18", "R0400-IVORY", 8, 665),
    ]
    return [invoice("請求書.pdf", lines)], sources


def out_of_stock(check):
    """依頼文の「品切商品（請求なし）」を読んだモデルの答えに当たるもの。"""
    return {"pairs": [], "non_order_lines": [], "exemptions": [
        {"source_line_id": s["id"], "reason": "依頼文「品切商品（請求なし）」"}
        for s in check.left_source_lines() if s["code"] in ("R0528-A", "R0400-WHITEMARBLE")
    ]}


class TestOutOfStockAndBackorders:
    def test_unbilled_lines_without_an_explanation_are_asked_about(self):
        check, result = run(*backorder_case())
        assert check.needs_interpretation()
        missing = found(result, "unbilled")
        assert len(missing) == 1 and missing[0]["severity"] == CONFIRM and "4 行" in missing[0]["detail"]

    def test_lines_explained_as_out_of_stock_are_not_a_missed_billing(self):
        invoices, sources = backorder_case()
        probe = GroupCheck("テスト商事", invoices, sources, SETTINGS)
        probe.prepare()
        _, result = run(invoices, sources, mapping=out_of_stock(probe))
        assert found(result, "unbilled") == []
        assert {u["reason"] for u in result["unbilled"]} == {"依頼文「品切商品（請求なし）」"}

    def test_orders_with_out_of_stock_lines_need_shipping_confirmation(self):
        # 実例（2026-10-02）: 伝票③の4行中2行が品切。残り2行も入荷待ちで未発送だった
        invoices, sources = backorder_case()
        probe = GroupCheck("テスト商事", invoices, sources, SETTINGS)
        probe.prepare()
        _, result = run(invoices, sources, mapping=out_of_stock(probe))
        evidence = found(result, "shipping_evidence")
        asked = next(f for f in evidence if f["severity"] == CONFIRM)
        assert "NTS260918-A（2 行・16,054円）" in asked["detail"] and "NTS260908-A1（2 行・17,900円）" in asked["detail"]
        noted = next(f for f in evidence if f["severity"] == NOTE)
        assert "NTS260909-A" in noted["detail"]
        assert result["invoices"][0]["verdict"] == CONFIRM

    def test_a_line_on_the_backorder_table_must_not_be_billed(self):
        invoices, sources = backorder_case()
        sources.append(other("取引先まとめ.xlsx", DOC_BACKORDER, backorders=[
            {"order_number": "", "code": "R0406-green", "jan": "", "description": "", "quantity": 6, "amount": 10734, "status": BACKORDER_WAITING, "ship_date": ""},
            {"order_number": "", "code": "R0400-IVORY", "jan": "", "description": "", "quantity": 8, "amount": 5320, "status": BACKORDER_WAITING, "ship_date": ""},
        ]))
        _, result = run(invoices, sources)
        waiting = found(result, "not_billable")
        assert len(waiting) == 1 and waiting[0]["severity"] == FIX and waiting[0]["amount"] == 16054
        assert "入荷待ち管理表（取引先まとめ.xlsx）" in waiting[0]["detail"]
        assert result["invoices"][0]["verdict"] == FIX

    def test_a_struck_line_is_cancelled_and_not_a_missed_billing(self):
        sources = [order("⑦.pdf", "NTS261029-A", "2026-10-29",
                         [oline("R0534-D", 10, 400), oline("R0528-B", 12, 1820, status=LINE_STRUCK)])]
        _, result = run([invoice("請求書.pdf", [line("2026-10-29", "R0534-D", 10, 400)])], sources)
        assert found(result, "unbilled") == []
        assert [(u["code"], u["state"], u["reason"]) for u in result["unbilled"]] == [("R0528-B", "取消", "注文書で消し線")]
        # 消し線の行を請求していれば要修正
        billed = [line("2026-10-29", "R0534-D", 10, 400), line("2026-10-29", "R0528-B", 12, 1820)]
        _, result = run([invoice("請求書.pdf", billed)], sources)
        assert [f["title"] for f in found(result, "not_billable")] == ["取り消された行を請求している"]


# --- 旅サラダの形: 注文書の無い請求と、注記の金額 ---


def offset_case(balance=10069):
    sources = [
        other("売上報告書.pdf", DOC_SALES, figures=[{"label": "下代売上合計額（税抜）", "amount": 79771}]),
        other("相殺管理表.pdf", DOC_LEDGER, figures=[
            {"label": "7月分 過払い額", "amount": 217707}, {"label": "2026年10月 相殺前残額", "amount": 90840},
            {"label": "2026年10月 相殺額", "amount": 79771}, {"label": "2026年10月 相殺後残額", "amount": 11069},
        ]),
    ]
    lines = [
        {**line("2026-10-31", "", 1, 79771, "10月度販売分"), "unit": "式"},
        {**line("2026-10-31", "", 1, -79771, "7月度過請求分のうち今回相殺分"), "unit": "式"},
    ]
    stated = [{"label": "7月度過請求分", "amount": 217707}, {"label": "今回相殺分", "amount": 79771},
              {"label": "7月度過請求残高", "amount": balance}]
    return [invoice("請求書.pdf", lines, stated_figures=stated)], sources


class TestFiguresAndRemarks:
    def test_a_mistyped_balance_in_the_remarks_is_found(self):
        _, result = run(*offset_case())
        wrong = found(result, "stated_figure")
        assert len(wrong) == 1 and wrong[0]["severity"] == FIX
        assert "10,069円" in wrong[0]["detail"] and "相殺後残額」11,069円" in wrong[0]["detail"]

    def test_the_right_balance_passes(self):
        _, result = run(*offset_case(balance=11069))
        assert result["invoices"][0]["verdict"] == VERDICT_OK and result["findings"] == []
        assert [row["result"] for row in result["lines"]] == ["根拠あり", "根拠あり"]
        assert "売上報告書.pdf の「下代売上合計額（税抜）」79,771円" == result["lines"][0]["source"]

    def test_an_amount_with_no_figure_behind_it_is_asked_about(self):
        invoices, sources = offset_case(balance=11069)
        invoices[0]["lines"][0] = {**line("2026-10-31", "", 1, 80000, "10月度販売分"), "unit": "式"}
        invoices[0] = invoice("請求書.pdf", invoices[0]["lines"], stated_figures=[])
        _, result = run(invoices, sources)
        assert [f["title"] for f in found(result, "no_figure")] == ["資料に同じ金額が見当たらない行"]

    def test_a_zero_total_with_an_offset_still_adds_up(self):
        invoices, _ = offset_case()
        assert invoices[0]["total"] == 0 and check_arithmetic(invoices[0]) == []
        assert line_kind(invoices[0]["lines"][1]) == "調整"


# --- 新日本カレンダーの形: 行をまとめた請求・付随費用・日付 ---


def calendar_case(date_small="2026-10-28"):
    colors = [oline(f"R0469-{c}-M", 60, 0) for c in "PQKJM"]
    sources = [
        order("②発注書.pdf", "112941", "2026-10-05", colors, ship_date="2026-10-26", delivery_date="2026-10-27"),
        order("①発注書.pdf", "112799", "2026-10-02", [oline("R0469-J-S", 240, 0)], ship_date="2026-10-23", delivery_date="2026-10-26"),
        other("契約書M.pdf", DOC_CONTRACT, terms=["油汚れクロスM 単価91円", "名入代 300枚 1,500円", "下代15,000円以上は元払い"]),
    ]
    lines = [
        {**line("2026-10-26", "", 300, 91, "油汚れクロス（M）5色各60枚（R0469-P/Q/K/J/M-M）"), "unit": "枚"},
        {**line("2026-10-26", "", 1, 1500, "名入代（フルカラー・オンデマンド・一式）"), "unit": "式"},
        {**line(date_small, "", 240, 114, "油汚れクロス（S）台紙1色名入れ（台紙制作・封入作業込み）"), "unit": "枚"},
    ]
    return [invoice("請求書-01.pdf", lines)], sources


def calendar_mapping():
    return {
        "pairs": [
            {"invoice_line_id": "I1", "source_line_ids": ["S1", "S2", "S3", "S4", "S5"], "reason": "色違い5行をまとめた請求"},
            {"invoice_line_id": "I3", "source_line_ids": ["S6"], "reason": "S 240枚"},
        ],
        "non_order_lines": [], "exemptions": [],
    }


class TestGroupedLinesFeesAndDates:
    def test_a_fee_line_is_told_apart_from_an_item_that_mentions_the_word(self):
        invoices, _ = calendar_case()
        assert [line_kind(l) for l in invoices[0]["lines"]] == ["商品", "付随費用", "商品"]

    def test_lines_the_code_cannot_pair_are_handed_to_the_model(self):
        check, _ = run(*calendar_case())
        payload = check.interpretation_payload()
        assert [l["id"] for l in payload["invoice_lines"]] == ["I1", "I3"]  # 名入代は付随費用なので入らない
        assert len(payload["source_lines"]) == 6 and "契約書M.pdf" in payload["files"]

    def test_a_grouped_line_is_accepted_when_the_quantities_add_up(self):
        _, result = run(*calendar_case(), mapping=calendar_mapping(),
                        review={"fee_checks": [{"invoice_line_id": "I2", "verdict": FEE_OK, "basis": "契約書M.pdf「名入代 300枚 1,500円」"}], "findings": []})
        assert result["lines"][0]["result"] == "一致・行をまとめて請求" and result["lines"][0]["source"] == "112941（5 行をまとめた請求）"
        assert result["lines"][1]["result"] == "根拠あり" and found(result, "fee") == []
        assert found(result, "no_source") == [] and result["unbilled"] == []

    def test_a_pairing_whose_quantities_do_not_add_up_is_flagged(self):
        mapping = calendar_mapping()
        mapping["pairs"][0]["source_line_ids"] = ["S1", "S2", "S3", "S4"]  # 240枚ぶんしか無い
        _, result = run(*calendar_case(), mapping=mapping)
        assert [f["title"] for f in found(result, "quantity")] == ["数量が資料と違う"]

    def test_a_pairing_that_reuses_a_source_line_is_ignored(self):
        mapping = calendar_mapping()
        mapping["pairs"][1]["source_line_ids"] = ["S1"]  # 既に I1 が使っている
        _, result = run(*calendar_case(), mapping=mapping)
        assert [f["lines"] for f in found(result, "no_source")] == [["I3"]]

    def test_a_date_that_matches_no_date_on_the_order_is_asked_about(self):
        # 実例（2026-11-04）: 発注書（再FAX）は出荷10/23・着10/26。明細は10/28のままだった
        _, result = run(*calendar_case(), mapping=calendar_mapping())
        odd = found(result, "line_date_basis")
        assert len(odd) == 1 and odd[0]["severity"] == CONFIRM and odd[0]["lines"] == ["I3"]
        assert "10/28付" in odd[0]["detail"] and "注文日 10/2、出荷日 10/23、納品日 10/26" in odd[0]["detail"]
        _, result = run(*calendar_case(date_small="2026-10-23"), mapping=calendar_mapping())
        assert found(result, "line_date_basis") == [] and result["date_basis"] == {"出荷日": 2}

    def test_a_fee_nobody_could_ground_is_asked_about(self):
        _, result = run(*calendar_case(), mapping=calendar_mapping())
        fee = found(result, "fee")
        assert len(fee) == 1 and fee[0]["severity"] == CONFIRM and "名入代" in fee[0]["detail"]

    def remark_case(self):
        """リマークの形: 注文ごとに送料780円。条件は、中身を読んでいない資料の名前に書かれている。"""
        sources = [order(f"発注書/10.{day} 発注書.xlsx", "", f"2026-10-{day:02d}", [oline("C017-A", 5, 1380)]) for day in (5, 8)]
        lines = []
        for day in (5, 8):
            lines += [line(f"2026-10-{day:02d}", "C017-A", 5, 1380), line(f"2026-10-{day:02d}", "SOU01", 1, 780, "送料")]
        return [invoice("請求書.pdf", lines)], sources

    def test_a_fee_written_in_a_file_name_is_grounded_by_code(self):
        # 実例（2026-11-05）: モデルに任せると、同じ資料名を根拠と認めたり「中身が空で確認できない」と
        # 4行とも要確認にしたりした。費目の語と金額が資料名にあれば、コードで根拠にする
        unread = [{"file": "株式会社リマーク様商品提案書←原価の金額で請求、送料は1pdfごと税抜き780円.xlsx", "reason": "中身が空"}]
        unknown = {"fee_checks": [{"invoice_line_id": i, "verdict": FEE_UNKNOWN, "basis": "中身が空で確認できない"} for i in ("I2", "I4")], "findings": []}
        check, result = run(*self.remark_case(), review=unknown, unread=unread)
        assert found(result, "fee") == [] and result["invoices"][0]["verdict"] == VERDICT_OK
        fees = [row for row in result["lines"] if row["kind"] == "付随費用"]
        assert [(row["result"], row["source"]) for row in fees] == [("根拠あり", f"資料名「{unread[0]['file']}」")] * 2
        # 金額が違う・費目の語が無い名前は根拠にしない。同じ内容の行は1件にまとめて聞く
        for name in ("提案書←送料は1pdfごと税抜き1,780円.xlsx", "提案書←名入代780円.xlsx", "提案書.xlsx"):
            _, result = run(*self.remark_case(), review=unknown, unread=[{"file": name, "reason": "中身が空"}])
            fee = found(result, "fee")
            assert len(fee) == 1 and fee[0]["severity"] == CONFIRM and fee[0]["amount"] == 1560 and len(fee[0]["lines"]) == 2
            assert "「送料」780円（2 行） の根拠（契約条件・指示）を資料から確かめられませんでした（中身が空で確認できない）" in fee[0]["detail"]
        # 桁区切りのある金額、資料（読めたもの）の名前でも受ける
        sources = self.remark_case()[1] + [other("送料 1件 ７８０円（税抜）の覚書.pdf", DOC_CONTRACT)]
        _, result = run(self.remark_case()[0], sources, review=unknown)
        assert found(result, "fee") == []

    def test_a_fee_the_model_says_contradicts_the_terms_is_asked_about(self):
        review = {"fee_checks": [{"invoice_line_id": "I2", "verdict": FEE_MISMATCH, "basis": "契約書M.pdf では300枚の名入代は1,500円ではなく2,000円"}], "findings": []}
        _, result = run(*calendar_case(), mapping=calendar_mapping(), review=review)
        assert [(f["severity"], f["by"], f["title"]) for f in found(result, "fee")] == [(CONFIRM, "所見", "付随費用が資料の条件と違う可能性")]

    def test_model_findings_need_evidence(self):
        review = {"fee_checks": [], "findings": [
            {"severity": CONFIRM, "invoice": "請求書-01.pdf", "title": "単価114円は契約書に無い", "detail": "見積による\n単価", "evidence": "契約書M.pdf の\n単価表"},
            {"severity": FIX, "invoice": "", "title": "根拠の無い所見", "detail": "…", "evidence": ""},
            {"severity": "重大", "invoice": "", "title": "知らない重さ", "detail": "…", "evidence": "x"},
        ]}
        _, result = run(*calendar_case(), mapping=calendar_mapping(), review=review)
        kept = found(result, "review")
        assert [f["title"] for f in kept] == ["単価114円は契約書に無い"]
        assert kept[0]["by"] == "所見" and kept[0]["detail"] == "見積による 単価（根拠: 契約書M.pdf の 単価表）"  # 1行にそろえる

    def test_the_model_can_ask_but_never_orders_a_fix(self):
        # 実例（2026-11-05）: 営業担当の「①送料弊社負担の間違いです。修正お願いしたいです」を逆の意味に読み、
        # 契約どおり直っている請求書に「要修正」を出した。要修正はコードで裏付けの取れた指摘だけ
        review = {"fee_checks": [], "findings": [
            {"severity": FIX, "invoice": "請求書-01.pdf", "title": "送料負担の誤りが未修正", "detail": "指示①が反映されていない", "evidence": "依頼文「①送料弊社負担の間違いです」"},
        ]}
        _, result = run(*calendar_case(date_small="2026-10-23"), mapping=calendar_mapping(), review={**review, "fee_checks": [
            {"invoice_line_id": "I2", "verdict": FEE_OK, "basis": "契約書M.pdf「名入代 300枚 1,500円」"}]})
        assert [f["severity"] for f in found(result, "review")] == [CONFIRM]
        assert result["invoices"][0]["verdict"] == CONFIRM and not [f for f in result["findings"] if f["severity"] == FIX]


class TestNearMatches:
    def test_a_different_unit_price_is_a_fix(self):
        sources = [order("注文書.pdf", "A1", "2026-10-01", [oline("C017-A", 5, 1380)])]
        _, result = run([invoice("請求書.pdf", [line("2026-10-01", "C017-A", 5, 1400)])], sources)
        wrong = found(result, "unit_price")
        assert len(wrong) == 1 and "請求書 1,400円、資料（注文書.pdf）1,380円" in wrong[0]["detail"]

    def test_a_different_quantity_is_a_fix(self):
        sources = [order("注文書.pdf", "A1", "2026-10-01", [oline("C017-A", 5, 1380)])]
        _, result = run([invoice("請求書.pdf", [line("2026-10-01", "C017-A", 6, 1380)])], sources)
        assert [f["title"] for f in found(result, "quantity")] == ["数量が資料と違う"]

    def test_the_jan_code_pairs_lines_whose_codes_are_written_differently(self):
        sources = [order("注文書.pdf", "A1", "2026-10-01", [oline("R0528-A", 4, 1820, jan="4582676213329")])]
        billed = [line("2026-10-01", "R0528-A-A", 4, 1820, jan="4582676213329")]
        _, result = run([invoice("請求書.pdf", billed)], sources)
        assert result["lines"][0]["result"] == "一致"

    def test_a_jan_mismatch_under_the_same_code_is_noted(self):
        sources = [order("注文書.pdf", "A1", "2026-10-01", [oline("C017-A", 5, 1380, jan="4582676227456")])]
        billed = [line("2026-10-01", "C017-A", 5, 1380, jan="4582676227470")]
        _, result = run([invoice("請求書.pdf", billed)], sources)
        assert [f["severity"] for f in found(result, "jan")] == [NOTE] and result["invoices"][0]["verdict"] == VERDICT_OK

    def test_a_line_with_nothing_behind_it_is_asked_about(self):
        sources = [order("注文書.pdf", "A1", "2026-10-01", [oline("C017-A", 5, 1380)])]
        billed = [line("2026-10-01", "C017-A", 5, 1380), line("2026-10-02", "X999", 3, 500)]
        _, result = run([invoice("請求書.pdf", billed)], sources)
        assert [f["lines"] for f in found(result, "no_source")] == [["I2"]]

    def test_two_invoices_of_one_customer_are_checked_together(self):
        # -01 と -02 に分かれた請求。片方ずつ見ると、もう片方の注文が請求漏れに見える
        sources = [order("①.pdf", "A1", "2026-10-01", [oline("C017-A", 5, 1380)]),
                   order("②.pdf", "A2", "2026-10-02", [oline("C017-B", 1, 1380)])]
        first = invoice("請求書-01.pdf", [line("2026-10-01", "C017-A", 5, 1380)], invoice_number="T-01")
        second = invoice("請求書-02.pdf", [line("2026-10-02", "C017-B", 1, 1380)], invoice_number="T-02")
        _, result = run([first, second], sources)
        assert found(result, "unbilled") == [] and [i["verdict"] for i in result["invoices"]] == [VERDICT_OK, VERDICT_OK]


class TestArithmeticAndRequirements:
    def base(self, **over):
        return invoice("請求書.pdf", [line("2026-10-01", "C017-A", 5, 1380), line("2026-10-02", "C017-B", 3, 463)], **over)

    def test_a_correct_invoice_passes(self):
        assert check_arithmetic(self.base()) == []
        rows, problems = check_requirements(self.base(), [ISSUER])
        assert problems == [] and all(r["ok"] for r in rows) and len(rows) == 6

    def test_tax_is_floored_once_per_rate(self):
        ok = self.base()
        assert ok["subtotal"] == 8289 and ok["tax_total"] == 828  # 828.9 の切り捨て
        rounded = self.base(tax_total=829, total=9118, tax_breakdown=[{"rate": 10, "base": 8289, "tax": 829}])
        assert [f["severity"] for f in check_arithmetic(rounded)] == [NOTE]
        wrong = self.base(tax_total=900, total=9189, tax_breakdown=[{"rate": 10, "base": 8289, "tax": 900}])
        assert [f["code"] for f in check_arithmetic(wrong)] == ["tax"]

    def test_wrong_sums_are_fixes(self):
        assert [f["code"] for f in check_arithmetic(self.base(subtotal=9000))] == ["subtotal", "total"]
        assert [f["code"] for f in check_arithmetic(self.base(total=1))] == ["total"]
        bad_line = self.base()
        bad_line["lines"][0]["amount"] = 7000
        assert "line_amount" in [f["code"] for f in check_arithmetic(bad_line)]

    def test_the_cover_page_must_agree_with_the_detail_sheet(self):
        covered = self.base(cover_lines=[{"description": "商品代金", "quantity": 1, "unit_price": 8000, "tax_rate": 10, "amount": 8000}])
        assert [f["code"] for f in check_arithmetic(covered)] == ["cover_total"]

    def test_the_registration_number_must_be_ours(self):
        rows, problems = check_requirements(self.base(registration_number="T1234567890123"), [ISSUER])
        assert [p["code"] for p in problems] == ["registration"] and problems[0]["severity"] == FIX
        assert not rows[0]["ok"]

    def test_an_unknown_issuer_is_asked_about(self):
        _, problems = check_requirements(self.base(issuer_name="別の会社株式会社"), [ISSUER])
        assert [(p["code"], p["severity"]) for p in problems] == [("issuer", CONFIRM)]

    def test_missing_recipient_and_dates_are_fixes(self):
        missing = self.base(recipient_name="")
        missing["lines"][1]["date"] = ""
        _, problems = check_requirements(missing, [ISSUER])
        assert sorted(p["code"] for p in problems) == ["line_date", "recipient"]

    def test_the_detail_sheet_should_carry_the_invoice_number(self):
        linked = self.base(has_detail_sheet=True, detail_invoice_number="T202610", detail_registration_number="T8150001018250")
        rows, problems = check_requirements(linked, [ISSUER])
        assert problems == [] and rows[-1]["requirement"] == "請求書と別紙明細の紐付け" and rows[-1]["ok"]
        _, problems = check_requirements(self.base(has_detail_sheet=True), [ISSUER])
        assert [p["code"] for p in problems] == ["detail_link"]

    @pytest.mark.parametrize("raw, expected", [("¥17,700", 17700), ("17700.0", 17700), (17700, 17700), ("▲79,771", -79771), ("", 0), (None, 0), ("x", 0)])
    def test_reading_amounts(self, raw, expected):
        assert to_int(raw) == expected

    def test_codes_are_compared_loosely(self):
        assert norm_code("ｒ０４０６－black") == norm_code("R0406-BLACK") == "R0406-BLACK"


# --- 出荷管理表との突き合わせ ---


def ship_row(number, requested, due, comment="", tracking="", team="", done=True, task="商品発送", vendor="テスト商事"):
    """出荷管理表の1行（shiplog.parse_workbook が返す形）。"""
    month = requested[:7]
    return {
        "id": f"{month}#{number}", "label": f"{int(month[5:])}月 No.{number}", "number": str(number), "month": month,
        "request_date": requested, "vendor": vendor, "owner": "山田", "due_date": due, "task": task, "link": "",
        "comment": comment, "handler": "竹内", "done": done, "stock": "", "tracking": tracking, "team_comment": team,
    }


def fact(row, **over):
    """コメント欄の読み取り（モデルの答えに当たるもの）。"""
    return {"id": row["id"], "items": [], "unshipped": [], "nothing_shipped": False, "partial": False, "quote": "",
            "shipped_on": "", "arrival_date": "", "purpose": PURPOSE_NORMAL, **over}


def simple_case(order_date="2026-10-16", line_date=None, **over):
    """注文1件・請求1行。"""
    sources = [order("⑤注文書.pdf", "ORD-1016", order_date, [oline("R0830-A", 1, 6170)], **over)]
    return [invoice("請求書.pdf", [line(line_date or order_date, "R0830-A", 1, 6170)])], sources


HELD = "R0560-A\nR0528-A\n上記2品番以外発送済　※R0560-Aは在庫補充され次第発送予定※"


def restock_case():
    """ナガセ 10月分の形（出荷分の控えは無く、出荷管理表だけで出荷を見る）。"""
    sources = [order("①注文書.pdf", "ORD-1001", "2026-10-01", ORDER_1001)]
    lines = [line("2026-10-01", o["code"], o["quantity"], o["unit_price"]) for o in ORDER_1001[1:]]
    lines.append(line("2026-10-13", "R0560-A", 100, 177))
    first = ship_row(7, "2026-10-02", "2026-10-06", "10/7着依頼です。", "675-9154-5711", HELD)
    later = ship_row(38, "2026-10-09", "2026-10-13", "R0560-A　サニタリーボックス キヅカイ 5枚　×　100", "福山通運\n67592444684")
    facts = {"rows": [
        fact(first, arrival_date="2026-10-07", unshipped=[
            {"code": "R0560-A", "description": "", "quote": "上記2品番以外発送済"},
            {"code": "R0528-A", "description": "", "quote": "上記2品番以外発送済"},
        ]),
        fact(later, items=[{"code": "R0560-A", "description": "サニタリーボックス キヅカイ 5枚", "quantity": 100}]),
    ]}
    return [invoice("請求書.pdf", lines)], sources, ship_tasks([first, later], facts)


class TestShipmentLog:
    def test_a_row_with_a_tracking_number_proves_the_shipment(self):
        # 実例: ナガセ NTS261016-A。依頼日が注文日と同じ行（10月 No.62）に伝票番号
        row = ship_row(62, "2026-10-16", "2026-10-20", "19→20発送先方了承済み", "福山通運\n67595104452\n67595104463", "10/16ピック済（本田）")
        check, result = run(*simple_case(), shipments=ship_tasks([row], {"rows": [fact(row)]}))
        assert found(result, "shipping_evidence") == [] and result["invoices"][0]["verdict"] == VERDICT_OK
        assert check.order_lines[0]["state"] == "出荷済"
        assert check.order_lines[0]["state_reason"] == "出荷管理表 10月 No.62（期日 10/20、福山通運 67595104452 67595104463）"
        assert result["shipments"]["orders"][0]["tasks"][0]["label"] == "10月 No.62"

    def test_without_the_table_the_old_rule_stays(self):
        _, result = run(*simple_case())
        assert [f["severity"] for f in found(result, "shipping_evidence")] == [NOTE]
        assert result["shipments"] == {"used": False, "rows": 0}

    def test_a_customer_missing_from_the_table_is_noted_and_the_old_rule_stays(self):
        _, result = run(*simple_case(), shipments=[])
        assert [f["severity"] for f in found(result, "no_ship_rows")] == [NOTE]
        assert [f["title"] for f in found(result, "shipping_evidence")] == ["出荷の記録が資料に無い注文"]

    def test_done_without_a_number_or_a_remark_still_counts(self):
        # 実例: ナガセの1個だけの発送（10月 No.9・18・21）は伝票番号の欄が空
        row = ship_row(9, "2026-10-16", "2026-10-19", "R0830-A\n豆乳メーカー　1個")
        check, result = run(*simple_case(), shipments=ship_tasks([row]))
        assert found(result, "shipping_evidence") == []
        assert check.order_lines[0]["state_reason"] == "出荷管理表 10月 No.9（期日 10/19、完了・伝票番号の記載なし）"

    def test_done_is_not_shipped_when_the_warehouse_says_nothing_was_in_stock(self):
        # 実例（2026-09-18 ナガセ NTS260918-A）: 完了・伝票番号なし・「全商品在庫ありません。」。
        # この注文の2行を請求し、承認後に営業の連絡で削除した
        row = ship_row(75, "2026-09-18", "2026-09-22", "商品発送依頼\n期日　9月22日", "", "全商品在庫ありません。")
        facts = {"rows": [fact(row, nothing_shipped=True, quote="全商品在庫ありません。")]}
        check, result = run(*simple_case("2026-09-18"), shipments=ship_tasks([row], facts))
        asked = found(result, "not_shipped")
        assert len(asked) == 1 and asked[0]["severity"] == CONFIRM and asked[0]["amount"] == 6170
        assert "出荷した記録がありません（出荷管理表 9月 No.75「全商品在庫ありません。」）" in asked[0]["detail"]
        assert result["lines"][0]["result"] == "一致（未出荷）" and result["invoices"][0]["verdict"] == CONFIRM

    def test_the_same_row_is_still_caught_when_the_remarks_could_not_be_read(self):
        row = ship_row(75, "2026-09-18", "2026-09-22", "", "", "全商品在庫ありません。")
        _, result = run(*simple_case("2026-09-18"), shipments=ship_tasks([row]))
        asked = found(result, "shipping_evidence")
        assert [f["severity"] for f in asked] == [CONFIRM] and asked[0]["title"].startswith("出荷管理表で出荷を確かめられない注文")
        assert "出荷管理表 9月 No.75（期日 9/22）は伝票番号が無く、納品チームコメントに「全商品在庫ありません。」とあります" in asked[0]["detail"]
        assert [f["severity"] for f in found(result, "ship_comment")] == [CONFIRM]

    def test_a_row_that_is_not_done_proves_nothing(self):
        row = ship_row(3, "2026-10-16", "2026-10-20", done=False)
        _, result = run(*simple_case(), shipments=ship_tasks([row]))
        assert "出荷管理表 10月 No.3（期日 10/20）は完了になっていません" in found(result, "shipping_evidence", CONFIRM)[0]["detail"]

    def test_an_order_with_no_row_is_asked_about(self):
        other_order = ship_row(88, "2026-10-22", "2026-10-26", tracking="福山通運\n67597763973")
        _, result = run(*simple_case(), shipments=ship_tasks([other_order]))
        assert "ORD-1016（1 行・6,170円）: 出荷管理表に、この注文の出荷の行が見当たりません" in found(result, "shipping_evidence", CONFIRM)[0]["detail"]

    def test_goods_held_back_are_unshipped_until_a_later_row_ships_them(self):
        # 実例: ナガセ NTS261001-A。10月 No.7「上記4品番以外発送済 ※R0560-Aは在庫補充され次第発送予定」、
        # No.38 で R0560-A を発送
        invoices, sources, tasks = restock_case()
        check, result = run(invoices, sources, shipments=tasks)
        states = {l["code"]: (l["state"], l["state_reason"]) for l in check.order_lines}
        assert states["R0528-A"] == ("未出荷", "出荷管理表 10月 No.7「上記2品番以外発送済」")
        assert states["R0560-A"] == ("出荷済", "出荷管理表 10月 No.38（期日 10/13、福山通運 67592444684）。10月 No.7 では未発送")
        assert states["R0406-BK"][0] == "出荷済" and "10月 No.7（期日 10/6、675-9154-5711）" in states["R0406-BK"][1]
        asked = found(result, "not_shipped")
        assert len(asked) == 1 and asked[0]["amount"] == 14560 and "R0528-A 8" in asked[0]["detail"]
        assert found(result, "line_date_basis") == []  # 10/13付の行は、出荷管理表の期日が根拠
        assert result["date_basis"] == {"注文日": 4, "出荷日": 1}
        assert [t["label"] for t in result["shipments"]["orders"][0]["tasks"]] == ["10月 No.7", "10月 No.38"]

    def test_the_table_adds_to_what_the_shipment_copies_already_say(self):
        invoices, sources = split_shipment_case()
        _, _, tasks = restock_case()
        check, result = run(invoices, sources, shipments=tasks)
        held = next(l for l in check.order_lines if l["code"] == "R0528-A")
        assert held["state_reason"] == "出荷分の控えで消し線。出荷管理表 10月 No.7「上記2品番以外発送済」"
        assert found(result, "ship_conflict") == [] and len(found(result, "duplicate")) == 1

    def test_a_copy_that_says_shipped_against_a_table_that_says_not_is_asked_about(self):
        invoices, sources = split_shipment_case()
        _, _, tasks = restock_case()
        _, result = run(invoices, sources, shipments=tasks[:1])  # 後日の発送の行が無い
        conflict = found(result, "ship_conflict")
        assert [f["severity"] for f in conflict] == [CONFIRM] and "ORD-1001 R0560-A" in conflict[0]["detail"]

    def test_a_shipment_due_next_month_is_asked_about(self):
        # 実例: ナガセ NTS261029-A。10月 No.96「在庫ある分のみ発送」、期日 11/2
        row = ship_row(96, "2026-10-29", "2026-11-02", "在庫ある分のみ発送お願い致します。", "福山通運\n67599333815")
        facts = {"rows": [fact(row, partial=True, quote="在庫ある分のみ発送")]}
        _, result = run(*simple_case("2026-10-29"), shipments=ship_tasks([row], facts))
        late = found(result, "cutoff")
        assert [f["severity"] for f in late] == [CONFIRM] and late[0]["amount"] == 6170
        assert "注文 ORD-1016（1 行・6,170円）は、出荷管理表 10月 No.96（期日 11/2、福山通運 67599333815）の出荷です" in late[0]["detail"]
        assert "依頼者コメント「在庫ある分のみ発送お願い致します。」" in late[0]["detail"] and "10月分の請求から外してください" in late[0]["detail"]
        assert legal_notes(result_of([result]))[0].startswith("過大請求・未出荷分の請求")

    def test_a_shipment_within_the_month_is_not_a_cutoff_problem(self):
        row = ship_row(90, "2026-10-29", "2026-10-30", tracking="67597763984")
        _, result = run(*simple_case("2026-10-29"), shipments=ship_tasks([row]))
        assert found(result, "cutoff") == [] and result["invoices"][0]["verdict"] == VERDICT_OK

    def test_a_shipment_no_order_explains_may_be_a_missed_billing(self):
        # 実例: ナガセ 10月 No.92（依頼 10/26・期日 10/28）。資料の注文は 10/22・10/23・10/29
        rows = [ship_row(62, "2026-10-16", "2026-10-20", tracking="67595104452"), ship_row(92, "2026-10-26", "2026-10-28", tracking="福山通運\n67598350890")]
        _, result = run(*simple_case(), shipments=ship_tasks(rows))
        loose = found(result, "unattributed_shipment")
        assert [f["severity"] for f in loose] == [CONFIRM] and loose[0]["invoice"] == ""
        assert "1 件: 10月 No.92（依頼 10/26・期日 10/28、福山通運 67598350890）" in loose[0]["detail"]
        assert "請求漏れ" in loose[0]["detail"] and result["invoices"][0]["verdict"] == CONFIRM
        assert [t["label"] for t in result["shipments"]["unattributed"]] == ["10月 No.92"]
        for mode, expected in (("note", [NOTE]), ("off", [])):
            _, result = run(*simple_case(), shipments=ship_tasks(rows), settings={**SETTINGS, "shipment_log": {"unattributed": mode}})
            assert [f["severity"] for f in found(result, "unattributed_shipment")] == expected

    def test_questions_are_put_to_the_person_who_made_the_invoice(self):
        # 指示（2026-11-05）: 営業担当者ではなく坂田さん（請求書を作った担当者）に確認する流れとする
        rows = [ship_row(3, "2026-10-16", "2026-10-20", done=False), ship_row(92, "2026-10-26", "2026-10-28", tracking="67598350890")]
        _, loose = run(*simple_case(), shipments=ship_tasks(rows))
        assert found(loose, "unattributed_shipment")[0]["detail"].endswith("請求漏れ）の可能性があります。どの注文の出荷かをご確認ください")
        assert found(loose, "shipping_evidence")[0]["detail"].endswith("出荷済みかをご確認のうえ、未出荷なら外してください")
        invoices, sources, tasks = restock_case()
        _, held = run(invoices, sources, shipments=tasks)
        assert found(held, "not_shipped")[0]["detail"].endswith("当月中に出荷したかをご確認のうえ、未出荷なら外してください")
        invoices, sources = split_shipment_case()
        _, conflict = run(invoices, sources, shipments=tasks[:1])
        assert found(conflict, "ship_conflict")[0]["detail"].endswith("どちらが正しいかをご確認ください")
        invoices, sources = backorder_case()
        probe = GroupCheck("テスト商事", invoices, sources, SETTINGS)
        probe.prepare()
        _, risky = run(invoices, sources, mapping=out_of_stock(probe))
        for result in (loose, held, conflict, risky, run(*split_shipment_case())[1]):
            assert all("営業担当に確認" not in f["detail"] for f in result["findings"])
            assert "営業担当に確認" not in render_reply(result_of([result]))
        assert "別の人への確認を求める書き方はしない" in REVIEW_SYSTEM

    def test_rows_that_straddle_the_month_are_left_to_the_other_month(self):
        rows = [
            ship_row(62, "2026-10-16", "2026-10-20", tracking="67595104452"),
            ship_row(87, "2026-09-29", "2026-10-01", tracking="67582044121"),  # 9月の注文の出荷
            ship_row(98, "2026-10-30", "2026-11-03", tracking="391553065606"),  # 11月の出荷
        ]
        _, result = run(*simple_case(), shipments=ship_tasks(rows))
        assert found(result, "unattributed_shipment") == [] and result["shipments"]["unattributed"] == []

    def test_samples_and_claim_shipments_are_not_billing(self):
        rows = [
            ship_row(62, "2026-10-16", "2026-10-20", tracking="67595104452"),
            ship_row(68, "2026-10-17", "2026-10-20", "サンプル納品依頼です。", "福山通運\n67595104522"),
            ship_row(5, "2026-10-20", "2026-10-21", "クレーム不足分72個対応分"),
        ]
        facts = {"rows": [fact(rows[0]), fact(rows[1], purpose=PURPOSE_SAMPLE), fact(rows[2], purpose=PURPOSE_CLAIM)]}
        for tasks in (ship_tasks(rows, facts), ship_tasks(rows)):  # 読み取りが無くても語で見分ける
            _, result = run(*simple_case(), shipments=tasks)
            assert found(result, "unattributed_shipment") == []
            assert [(t["label"], t["purpose"]) for t in result["shipments"]["others"]] == [("10月 No.68", PURPOSE_SAMPLE), ("10月 No.5", PURPOSE_CLAIM)]

    def test_an_order_shipped_with_samples_is_still_an_ordinary_shipment(self):
        # 実例: リマーク 9月 No.3「あと併せてサンプルで1つづつ発送お願いいたします」
        row = ship_row(3, "2026-10-16", "2026-10-19", "在庫確保分から発送お願いします。\nあと併せてサンプルで1つづつ発送", "67569307234")
        assert ship_tasks([row], {"rows": [fact(row)]})[0]["purpose"] == PURPOSE_NORMAL
        assert ship_tasks([row], {"rows": [fact(row, purpose=PURPOSE_CLAIM)]})[0]["purpose"] == PURPOSE_NORMAL  # 語が無い

    def test_rows_naming_the_goods_go_to_the_order_that_has_them(self):
        # 実例: サクラマチ K12079（15個）と J33（5個）。どちらも S0384-H-M、依頼日は 10/6 と 10/8
        invoices, sources = clean_case()
        rows = [
            ship_row(6, "2026-10-01", "2026-10-05", "10/6AM必着です。", "391399216272\n391399200695"),
            ship_row(27, "2026-10-06", "2026-10-08", "キャミソールエプロン チャコールグレー M\n【品番:S0384-H-M】15個\n10月10日AM必着", "391422171965"),
            ship_row(29, "2026-10-08", "2026-10-09", "S0384-H-M\n注文数：5\n10/10（土）AM着希望", "ヤマト運輸\n3914-2220-6991"),
        ]
        facts = {"rows": [
            fact(rows[0], arrival_date="2026-10-06"),
            fact(rows[1], arrival_date="2026-10-10", items=[{"code": "S0384-H-M", "description": "キャミソールエプロン", "quantity": 15}]),
            fact(rows[2], arrival_date="2026-10-10", items=[{"code": "S0384-H-M", "description": "", "quantity": 5}]),
        ]}
        _, result = run(invoices, sources, shipments=ship_tasks(rows, facts))
        linked = {o["order"]: [t["label"] for t in o["tasks"]] for o in result["shipments"]["orders"]}
        assert linked == {"K12073": ["10月 No.6"], "K12079": ["10月 No.27"], "J33": ["10月 No.29"]}
        assert result["findings"] == [] and result["invoices"][0]["verdict"] == VERDICT_OK  # 留意も無くなる

    def test_the_due_date_links_orders_shipped_weeks_after_they_were_placed(self):
        # 実例: 新日本カレンダー。注文 10/2・出荷 10/23・納品 10/26 に、依頼 10/20・期日 10/23 の行
        invoices, sources = simple_case("2026-10-02", "2026-10-23", ship_date="2026-10-23", delivery_date="2026-10-26")
        row = ship_row(75, "2026-10-20", "2026-10-23", "第35回ペタンク大会※着日指定・出荷主指定あり", "ヤマト：391494558453")
        _, result = run(invoices, sources, shipments=ship_tasks([row]))
        assert result["shipments"]["orders"][0]["tasks"][0]["label"] == "10月 No.75" and result["findings"] == []

    def test_rows_asked_for_on_the_same_day_are_one_order_shipped_in_parts(self):
        # 実例: ナガセ 10月 No.62（福山通運）と No.64（ヤマト運輸）。どちらも 10/16 の依頼
        rows = [ship_row(62, "2026-10-16", "2026-10-20", tracking="福山通運\n67595104452"), ship_row(64, "2026-10-16", "2026-10-20", tracking="ヤマト運輸\n391482755344")]
        _, result = run(*simple_case(), shipments=ship_tasks(rows))
        assert [t["label"] for t in result["shipments"]["orders"][0]["tasks"]] == ["10月 No.62", "10月 No.64"]
        assert found(result, "unattributed_shipment") == []

    def test_the_same_goods_in_a_later_month_do_not_link_a_row_to_an_old_order(self):
        # 実例（2026-11-05）: 9月分の確認で、10月 No.7（10/1 の注文の出荷。R0528-A を含む）を、同じ品番のある
        # 9/18 の注文に結び、「翌月の出荷」と誤って指摘した。同じ商品の注文は毎週のように来る
        sources = [order("③9.18.pdf", "NTS260918-A", "2026-09-18", [oline("R0528-A", 4, 1820), oline("R0406-green", 6, 1789)])]
        invoices = [invoice("請求書.pdf", [line("2026-09-18", "R0406-green", 6, 1789)])]
        rows = [
            ship_row(75, "2026-09-18", "2026-09-22", "商品発送依頼", "", "全商品在庫ありません。"),
            ship_row(7, "2026-10-02", "2026-10-06", "10/7着依頼です。", "675-9154-5711", HELD),
        ]
        facts = {"rows": [
            fact(rows[0], nothing_shipped=True, quote="全商品在庫ありません。"),
            fact(rows[1], unshipped=[{"code": "R0528-A", "description": "", "quote": "上記2品番以外発送済"}]),
        ]}
        _, result = run(invoices, sources, shipments=ship_tasks(rows, facts))
        assert [t["label"] for t in result["shipments"]["orders"][0]["tasks"]] == ["9月 No.75"]
        assert found(result, "cutoff") == [] and found(result, "unattributed_shipment") == []
        assert "9月 No.75「全商品在庫ありません。」" in found(result, "not_shipped", CONFIRM)[0]["detail"]

    def test_a_same_day_row_for_other_goods_is_not_a_part_of_the_order(self):
        # 資料に無い注文の出荷かもしれない行を、同じ日というだけで別の注文の分割出荷にしない
        rows = [ship_row(62, "2026-10-16", "2026-10-20", tracking="67595104452"), ship_row(63, "2026-10-16", "2026-10-20", "Z9999-A 10個", "67595104463")]
        _, result = run(*simple_case(), shipments=ship_tasks(rows))
        assert [t["label"] for t in result["shipments"]["orders"][0]["tasks"]] == ["10月 No.62"]
        assert "10月 No.63" in found(result, "unattributed_shipment", CONFIRM)[0]["detail"]
        # 他に行が無ければ、品番の書き方が違うだけとみて結ぶ（注文書が相手の品番のことがある）
        _, result = run(*simple_case(), shipments=ship_tasks(rows[1:]))
        assert [t["label"] for t in result["shipments"]["orders"][0]["tasks"]] == ["10月 No.63"]

    def test_two_orders_of_one_day_can_share_one_shipment(self):
        sources = [order("④.pdf", "ORD-A", "2026-10-05", [oline("Y0731-B", 1, 4016)]), order("⑤.pdf", "ORD-B", "2026-10-05", [oline("R0830-A", 1, 6170)])]
        invoices = [invoice("請求書.pdf", [line("2026-10-05", "Y0731-B", 1, 4016), line("2026-10-05", "R0830-A", 1, 6170)])]
        row = ship_row(18, "2026-10-05", "2026-10-07", tracking="67587511283")
        check, result = run(invoices, sources, shipments=ship_tasks([row]))
        assert [o["shared"] for o in result["shipments"]["orders"]] == [False, True]
        assert check.order_lines[1]["state_reason"].endswith("（他の注文と同じ出荷）") and found(result, "shipping_evidence") == []

    def test_what_the_model_read_must_be_written_in_the_row(self):
        row = ship_row(7, "2026-10-02", "2026-10-06", "10/7着依頼です。", "675-9154-5711", "R0528-A は在庫補充され次第発送予定")
        made_up = fact(
            row, nothing_shipped=True, partial=True, quote="全商品在庫ありません", shipped_on="2026-12-01", arrival_date="2026-10-07",
            purpose=PURPOSE_SAMPLE,
            items=[{"code": "X9999-Z", "description": "", "quantity": 3}, {"code": "", "description": "在庫補充され次第", "quantity": 0}],
            unshipped=[{"code": "r0528-a", "description": "", "quote": "在庫補充され次第発送予定"}, {"code": "R0560-A", "description": "", "quote": ""}],
        )
        task = ship_tasks([row], {"rows": [made_up]})[0]
        assert [u["code"] for u in task["unshipped"]] == ["r0528-a"]  # 行に無い品番は受けない
        assert [i["description"] for i in task["items"]] == ["在庫補充され次第"]  # 品番が無ければ品名が行にあること
        assert (task["nothing_shipped"], task["partial"], task["quote"]) == (False, False, "")  # 引用が行に無い
        assert (task["shipped_on"], task["arrival_date"], task["purpose"]) == ("", "2026-10-07", PURPOSE_NORMAL)
        assert task["date"] == "2026-10-06" and task["tokens"] == {"R0528-A"} and task_ships(task)
        assert ship_tasks([ship_row(10, "2026-10-02", "2026-10-05", task="在庫確保（在庫ロボット・エアロジ共に減算）")]) == []  # 商品発送だけ

    def test_small_helpers(self):
        assert code_tokens("C017-Aベージュ　15枚\nＲ０４６９－Ｒ－Ｍ 油汚れクロス", "y0726-white 6個、伝票 67595104452") == {"C017-A", "R0469-R-M", "Y0726-WHITE"}
        assert codes_related("R0528-A", "r0528-a-a") and not codes_related("R0400-GRAY", "R0400-GRAYMARBLE") and not codes_related("", "")
        assert weekday_gap("2026-10-23", "2026-10-26") == 1 and weekday_gap("2026-10-16", "2026-10-16") == 0  # 金曜→月曜
        assert weekday_gap("2026-10-14", "2026-10-17") == 2 and weekday_gap("2026-10-08", "2026-10-05") == -3
        assert weekday_gap("", "2026-10-05") is None
        assert month_end("2026-10-09") == "2026-10-31" and month_end("2026-12-01") == "2026-12-31" and month_end("") == ""


# --- 報告書と返信案 ---


def result_of(groups):
    return {"checked_at": "2026-11-05T10:00:00+09:00", "case": "テスト", "request_note": "", "groups": groups,
            "calls": 3, "cache_hits": 0, "usage": {}, "cost_jpy": 42.0}


class TestReport:
    def test_a_clean_result_says_nothing_needs_fixing(self):
        _, group = run(*clean_case())
        result = result_of([group])
        assert [(r["label"], r["points"]) for r in summary_rows(result)] == [("問題なし（留意あり）", "資料と一致")]
        report = render_report(result)
        assert "**修正の必要はありません。**" in report and "| テスト商事 | T202610 | 129,888円 | 問題なし（留意あり） | 資料と一致 |" in report
        assert "適格請求書の記載要件: " in report and "はすべて具備。" in report
        reply = render_reply(result)
        assert reply.startswith("坂田さん\n請求書 1 通（テスト商事）を、作成元の資料と照合して確認しました。足立さんの確認を経てお戻しします。")
        assert "修正の必要はありません。このままで結構です。" in reply and "■参考（修正不要）" in reply
        assert "修正版をいただければ" not in reply

    def test_findings_are_listed_worst_first_with_a_reply_draft(self):
        _, group = run(*split_shipment_case())
        result = result_of([group])
        report = render_report(result)
        assert "**1 通に修正・確認が必要です。**" in report
        assert report.index("【要修正】同じ注文の二重計上") < report.index("【要確認】出荷した記録の無い行を請求している") < report.index("【留意】")
        assert "| 10/1 | R0560-A R0560-A | 100×177円 | 17,700円 | ORD-1001（請求済みの行と同じ） | 二重計上 |" in report
        reply = render_reply(result, reply_tag="[rp aid=1 to=2-3]")
        assert reply.startswith("[rp aid=1 to=2-3]坂田さん\n")
        assert "1 通に修正・確認をお願いします。" in reply and "■テスト商事（要修正）" in reply
        assert "・同じ注文の二重計上: " in reply and reply.rstrip().endswith("修正版をいただければ再確認します。")

    def test_long_invoices_show_only_the_rows_that_matter(self):
        lines = [line("2026-10-01", f"C{n:03d}", 1, 100) for n in range(20)] + [line("2026-10-02", "X999", 1, 100)]
        sources = [order("注文書.pdf", "A1", "2026-10-01", [oline(f"C{n:03d}", 1, 100) for n in range(20)])]
        _, group = run([invoice("請求書.pdf", lines)], sources)
        report = render_report(result_of([group]))
        assert "ほか 20 行は資料と一致。" in report and "X999" in report and "C005" not in report

    def test_the_shipment_table_is_shown_order_by_order(self):
        invoices, sources, tasks = restock_case()
        sample = ship_row(8, "2026-10-02", "2026-10-05", "サンプルの送り状の宛名を水島宛て", "675-8204-4261")
        loose = ship_row(92, "2026-10-26", "2026-10-28", tracking="福山通運\n67598350890")
        _, group = run(invoices, sources, shipments=tasks + ship_tasks([sample, loose]))
        report = render_report({**result_of([group]), "shipment_log": {"used": True, "rows": 4, "note": ""}})
        assert "**出荷管理表との照合**" in report
        assert "| ORD-1001 | 10月 No.7 | 10/2 | 10/6 | 675-9154-5711 | 納品チーム「R0560-A R0528-A 上記2品番以外発送済 ※R0560-Aは在庫補充され次第発送予定※」／依頼者「10/7着依頼です。」 |" in report
        assert "| ORD-1001 | 10月 No.38 | 10/9 | 10/13 | 福山通運 67592444684 | 依頼者「R0560-A サニタリーボックス キヅカイ 5枚 × 100」 |" in report
        assert "どの注文とも結べなかった出荷の行: 10月 No.92（依頼 10/26・期日 10/28）。" in report
        assert "請求の対象外とみた行: 10月 No.8（10/5 サンプル）。" in report
        assert "【要確認】出荷管理表にあるのに、対応する注文書が資料に無い出荷: 1 件: 10月 No.92" in report
        reply = render_reply(result_of([group]))
        assert "・出荷した記録の無い行を請求している: 1 行・14,560円（R0528-A 8）は、出荷した記録がありません（出荷管理表 10月 No.7「上記2品番以外発送済」）" in reply
        # 出荷の行が無い注文、表を使っていない結果
        _, group = run(*simple_case(), shipments=ship_tasks([loose]))
        assert "| ORD-1016 | 出荷の行なし | | | | |" in render_report(result_of([group]))
        _, group = run(*simple_case())
        assert "出荷管理表" not in render_report(result_of([group]))

    def test_the_legal_section_follows_the_result(self):
        _, group = run(*offset_case())
        notes = legal_notes(result_of([group]))
        assert any("対価の返還（相殺・値引き 79,771円）" in n and "▲7,977円" in n for n in notes)
        assert any(n.startswith("印紙税") for n in notes)
        _, group = run(*split_shipment_case())
        assert legal_notes(result_of([group]))[0].startswith("過大請求・未出荷分の請求")


# --- 本体（読み取りは差し替え） ---


class FakeReader:
    """決まった読み取り結果を返す。何を頼まれたかを控える。"""

    def __init__(self, invoices, sources, mapping=None, review=None):
        self._invoices, self._sources = invoices, sources
        self._mapping, self._review = mapping, review or {}
        self.asked: list[tuple] = []
        self.usage = {"input_tokens": 1000, "output_tokens": 200}
        self.calls, self.cache_hits = 0, 0

    def read_invoice(self, name, data):
        self.calls += 1
        return {**self._invoices[name], "file": name}

    def read_source(self, name, data):
        if not data:
            return {"readable": False, "unread_reason": "中身が空（大きすぎて取得していない資料など）", "file": name}
        self.calls += 1
        return {**self._sources[name], "file": name}

    def read_shiplog(self, name, rows):
        self.asked.append(("shiplog", name, [r["id"] for r in rows]))
        return {"rows": [fact(r) for r in rows]}

    def interpret(self, note, payload):
        self.asked.append(("interpret", note, payload))
        return self._mapping(payload) if callable(self._mapping) else (self._mapping or {})

    def review(self, payload):
        self.asked.append(("review", payload))
        return self._review


def make_config(tmp_path):
    config = Config.load(tmp_path / "no-config.json")
    config.data["invoice_check"] = {**config.data["invoice_check"], "issuers": [ISSUER], "checklist": ["資料と合っているか"]}
    config.data["state_dir"] = str(tmp_path / "state")
    config.data["audit_log_dir"] = str(tmp_path / "logs")
    config.base_dir = tmp_path
    return config


class TestChecker:
    def checker(self, tmp_path, reader):
        return InvoiceChecker(make_config(tmp_path), reader=reader, now=lambda: datetime(2026, 11, 5, 10, 0, tzinfo=JST))

    def test_a_request_is_checked_end_to_end(self, tmp_path):
        invoices, sources = clean_case()
        reader = FakeReader({"請求書.pdf": invoices[0]}, {s["file"]: s for s in sources})
        groups = [{"name": "サクラマチ", "invoices": [("請求書.pdf", b"%PDF")], "sources": [(s["file"], b"%PDF") for s in sources]}]
        result = self.checker(tmp_path, reader).check("ご確認ください", groups, case="2026-10_サクラマチ")
        assert [r["verdict"] for r in summary_rows(result)] == [VERDICT_OK]
        assert result["calls"] == 4 and result["checked_at"].startswith("2026-11-05")
        assert [kind for kind, *_ in reader.asked] == ["review"]  # 対応付けは要らない
        payload = reader.asked[0][1]
        assert payload["checklist"] == ["資料と合っているか"] and payload["request_note"] == "ご確認ください"
        assert [s["file"] for s in payload["sources"]] == [s["file"] for s in sources]
        assert payload["code_findings"][0]["title"] == "出荷の記録が資料に無い注文"
        audit = (tmp_path / "logs").glob("*.jsonl")
        record = json.loads(next(audit).read_text(encoding="utf-8").splitlines()[-1])
        assert record["type"] == "invoice_check" and record["verdicts"] == {"T202610": VERDICT_OK}
        assert "lines" not in json.dumps(record, ensure_ascii=False)  # 本文は残さない

    def test_copies_of_the_invoice_old_invoices_and_empty_files_are_set_aside(self, tmp_path):
        invoices, sources = clean_case()
        old = other("×旧_請求書.xlsx", DOC_INVOICE, notes="見積No. RC-1 パターン②")
        table = {s["file"]: s for s in sources} | {"×旧_請求書.xlsx": old}
        reader = FakeReader({"請求書.pdf": invoices[0]}, table)
        files = [(s["file"], b"%PDF") for s in sources] + [("×旧_請求書.xlsx", b"PK"), ("請求書.pdf", b"%PDF"), ("注文書/大きい提案書←送料は780円.xlsx", b"")]
        result = self.checker(tmp_path, reader).check("", [{"name": "サクラマチ", "invoices": [("請求書.pdf", b"%PDF")], "sources": files}])
        group = result["groups"][0]
        assert [u["file"] for u in group["unread"]] == ["注文書/大きい提案書←送料は780円.xlsx"]
        assert "×旧_請求書.xlsx" in [s["file"] for s in group["sources"]]
        payload = reader.asked[-1][1]
        assert payload["reference_invoices"] == [{"file": "×旧_請求書.xlsx", "notes": "見積No. RC-1 パターン②", "terms": []}]
        assert payload["unread_files"] == ["注文書/大きい提案書←送料は780円.xlsx（中身が空（大きすぎて取得していない資料など））"]
        assert all(row["result"] == "一致" for row in group["lines"])  # 請求書の控えを資料として読まない

    def test_the_model_is_asked_only_about_what_the_code_could_not_pair(self, tmp_path):
        invoices, sources = backorder_case()

        def mapping(payload):
            return {"pairs": [], "non_order_lines": [], "exemptions": [
                {"source_line_id": s["id"], "reason": "依頼文「品切商品（請求なし）」"} for s in payload["source_lines"]
            ]}

        reader = FakeReader({"請求書.pdf": invoices[0]}, {s["file"]: s for s in sources}, mapping=mapping)
        groups = [{"name": "ナガセ", "invoices": [("請求書.pdf", b"%PDF")], "sources": [(s["file"], b"%PDF") for s in sources]}]
        result = self.checker(tmp_path, reader).check("品切商品（請求なし） R0400-WHITEMARBLE R0528-A-A", groups)
        kind, note, payload = reader.asked[0]
        assert kind == "interpret" and "品切商品" in note
        assert payload["invoice_lines"] == [] and len(payload["source_lines"]) == 4
        assert summary_rows(result)[0]["verdict"] == CONFIRM
        assert summary_rows(result)[0]["points"] == "出荷の裏付けが無い行（発行前に出荷確認）"

    def test_skipping_the_review_still_grounds_fees_by_code(self, tmp_path):
        invoices, sources = offset_case(balance=11069)
        reader = FakeReader({"請求書.pdf": invoices[0]}, {s["file"]: s for s in sources})
        groups = [{"name": "旅サラダ", "invoices": [("請求書.pdf", b"%PDF")], "sources": [(s["file"], b"%PDF") for s in sources]}]
        result = self.checker(tmp_path, reader).check("", groups, review=False)
        assert reader.asked == [] and summary_rows(result)[0]["verdict"] == VERDICT_OK

    def shipment_rows(self):
        return [
            ship_row(6, "2026-10-01", "2026-10-05", "10/6AM必着です。", "391399216272", vendor="サクラマチ"),
            ship_row(27, "2026-10-06", "2026-10-08", "【品番:S0384-H-M】15個", "391422171965", vendor="リマーク・サクラマチ"),
            ship_row(29, "2026-10-08", "2026-10-09", "S0384-H-M\n注文数：5", "3914-2220-6991", vendor="サクラマチ"),
            ship_row(84, "2026-10-21", "2026-10-30", "在庫確保、減算よろしくお願い致します。", task="在庫確保（在庫ロボット・エアロジ共に減算）", vendor="サクラマチ"),
            ship_row(22, "2026-10-05", "2026-10-09", "C017-A 5個", "67587511283", vendor="リマーク"),
            ship_row(9, "2026-08-03", "2026-08-05", "", "67500000000", vendor="サクラマチ"),  # 期間の外
        ]

    def test_the_shipment_table_is_cut_to_the_customer_and_read_once(self, tmp_path):
        invoices, sources = clean_case()
        reader = FakeReader({"請求書.pdf": invoices[0]}, {s["file"]: s for s in sources})
        groups = [{"name": "サクラマチ", "invoices": [("請求書.pdf", b"%PDF")], "sources": [(s["file"], b"%PDF") for s in sources]}]
        result = self.checker(tmp_path, reader).check("", groups, shiplog_rows=self.shipment_rows())
        kind, name, ids = reader.asked[0]
        assert (kind, name) == ("shiplog", "サクラマチ")
        assert ids == ["2026-10#6", "2026-10#27", "2026-10#29"]  # その取引先の、期間内の、商品発送の行だけ
        group = result["groups"][0]
        assert [[t["label"] for t in o["tasks"]] for o in group["shipments"]["orders"]] == [["10月 No.6"], ["10月 No.27"], ["10月 No.29"]]
        assert group["findings"] == [] and result["shipment_log"] == {"used": True, "rows": 6, "note": ""}
        report = render_report(result)
        assert "- 出荷管理表: 注文ごとに出荷の行（期日・伝票番号・納品チームコメント）と突き合わせ" in report
        assert "| K12079 | 10月 No.27 | 10/6 | 10/8 | 391422171965 | 依頼者「【品番:S0384-H-M】15個」 |" in report

    def test_the_customer_is_also_found_by_the_name_on_the_invoice_and_by_aliases(self, tmp_path):
        invoices, sources = clean_case()
        invoices[0]["recipient_name"] = "株式会社サクラマチ"
        reader = FakeReader({"請求書.pdf": invoices[0]}, {s["file"]: s for s in sources})
        groups = [{"name": "ユーコープ分", "invoices": [("請求書.pdf", b"%PDF")], "sources": [(s["file"], b"%PDF") for s in sources]}]
        self.checker(tmp_path, reader).check("", groups, shiplog_rows=self.shipment_rows())
        assert reader.asked[0][2] == ["2026-10#6", "2026-10#27", "2026-10#29"]
        invoices[0]["recipient_name"] = "生活協同組合ユーコープ"
        checker = self.checker(tmp_path, reader)
        checker._config.data["invoice_check"]["shipment_log"]["aliases"] = {"ユーコープ分": ["サクラマチ"]}
        reader.asked.clear()
        checker.check("", groups, shiplog_rows=self.shipment_rows())
        assert reader.asked[0][2] == ["2026-10#6", "2026-10#27", "2026-10#29"]

    def test_invoices_not_based_on_orders_are_not_compared_with_shipments(self, tmp_path):
        invoices, sources = offset_case(balance=11069)
        reader = FakeReader({"請求書.pdf": invoices[0]}, {s["file"]: s for s in sources})
        groups = [{"name": "サクラマチ", "invoices": [("請求書.pdf", b"%PDF")], "sources": [(s["file"], b"%PDF") for s in sources]}]
        result = self.checker(tmp_path, reader).check("", groups, review=False, shiplog_rows=self.shipment_rows())
        assert reader.asked == [] and result["groups"][0]["shipments"]["used"] is False
        assert found(result["groups"][0], "no_ship_rows") == []

    def test_a_copy_of_the_table_in_the_case_folder_is_used_before_fetching(self, tmp_path):
        from raizuinu.invoicecheck import SHIPLOG_FILE, load_shiplog
        from test_shiplog import book

        config = make_config(tmp_path)
        config.data["invoice_check"]["shipment_log"]["spreadsheet_id"] = "SHEET-ID"
        fetched = []

        def fetch(spreadsheet_id):
            fetched.append(spreadsheet_id)
            return [ship_row(1, "2026-10-01", "2026-10-02")]

        rows, why = load_shiplog(config, tmp_path, fetch=fetch)
        assert fetched == ["SHEET-ID"] and len(rows) == 1 and why == ""
        (tmp_path / SHIPLOG_FILE).write_bytes(book())
        rows, why = load_shiplog(config, tmp_path, fetch=fetch)
        assert fetched == ["SHEET-ID"] and len(rows) == 8 and why == ""  # 写しがあれば取りに行かない
        assert load_shiplog(config, tmp_path, skip=True, fetch=fetch) == (None, "")

    def test_a_table_that_cannot_be_fetched_is_reported_and_the_check_goes_on(self, tmp_path):
        from raizuinu.invoicecheck import load_shiplog

        config = make_config(tmp_path)
        assert load_shiplog(config, tmp_path) == (None, "")  # スプレッドシートが設定されていない
        config.data["invoice_check"]["shipment_log"]["spreadsheet_id"] = "SHEET-ID"

        def fetch(spreadsheet_id):
            raise RuntimeError("出荷管理表を取得できません（HTTP 401。共有設定を確認してください）")

        rows, why = load_shiplog(config, tmp_path, fetch=fetch)
        assert rows is None and why.startswith("出荷管理表を読めなかったため、出荷の実績との突き合わせをしていません（出荷管理表を取得できません")
        invoices, sources = clean_case()
        reader = FakeReader({"請求書.pdf": invoices[0]}, {s["file"]: s for s in sources})
        groups = [{"name": "サクラマチ", "invoices": [("請求書.pdf", b"%PDF")], "sources": [(s["file"], b"%PDF") for s in sources]}]
        result = self.checker(tmp_path, reader).check("", groups, shiplog_rows=rows, shiplog_note=why)
        assert [f["title"] for f in found(result["groups"][0], "shipping_evidence")] == ["出荷の記録が資料に無い注文"]
        assert f"- 出荷管理表: {why}" in render_report(result)
        config.data["invoice_check"]["shipment_log"]["enabled"] = False
        assert load_shiplog(config, tmp_path, fetch=fetch) == (None, "")

    def test_a_case_folder_is_read_per_customer(self, tmp_path):
        case = tmp_path / "2026-10_卸"
        (case / "ナガセ" / "invoices").mkdir(parents=True)
        (case / "ナガセ" / "sources" / "注文書").mkdir(parents=True)
        (case / "_result").mkdir()
        (case / "空の取引先").mkdir()
        (case / "request.txt").write_text("ご確認ください", encoding="utf-8")
        (case / "ナガセ" / "invoices" / "請求書.pdf").write_bytes(b"%PDF-1")
        (case / "ナガセ" / "sources" / "注文書" / "①.pdf").write_bytes(b"%PDF-2")
        (case / "ナガセ" / "sources" / "契約書.pdf").write_bytes(b"%PDF-3")
        (case / "出荷管理表.xlsx").write_bytes(b"PK")  # 出荷管理表の写しは取引先の資料に数えない
        note, groups = load_case(case)
        assert note == "ご確認ください" and [g["name"] for g in groups] == ["ナガセ"]
        assert groups[0]["invoices"] == [("請求書.pdf", b"%PDF-1")]
        assert {name for name, _ in groups[0]["sources"]} == {"注文書/①.pdf", "契約書.pdf"}  # 下の階層も読む


# --- 読み取り ---


def fake_client(payload):
    """payload は1つの答えか、呼び出し順に返す答えのリスト（最後を繰り返す）。"""
    payloads = payload if isinstance(payload, list) else [payload]
    client = SimpleNamespace(calls=0, kwargs=None, content=None)

    def create(messages, **kwargs):
        answer = payloads[min(client.calls, len(payloads) - 1)]
        client.calls += 1
        client.kwargs, client.content = kwargs, messages[0]["content"]
        text = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=3000, output_tokens=500, cache_creation_input_tokens=0, cache_read_input_tokens=0),
        )

    client.messages = SimpleNamespace(create=create)
    return client


class TestReader:
    def test_files_become_the_right_kind_of_block(self):
        settings = {"max_file_mb": 1}
        blocks, reason = file_blocks("注文書.pdf", b"%PDF-1.4 x", settings)
        assert reason == "" and blocks[0]["type"] == "document" and blocks[0]["title"] == "注文書.pdf"
        blocks, _ = file_blocks("管理表.csv", "品番,数量\nA,1".encode("utf-8"), settings)
        assert blocks[0]["type"] == "text" and "ファイル「管理表.csv」の内容:\n品番,数量" in blocks[0]["text"]
        assert file_blocks("写真.png", b"\x89PNG", settings)[0][0]["type"] == "image"
        assert file_blocks("注文書.pdf", b"not a pdf", settings) == (None, "PDFとして読めない")
        assert file_blocks("提案書.xlsx", b"", settings)[1].startswith("中身が空")
        assert file_blocks("大きい.pdf", b"%PDF" + b"0" * (1024 * 1024 + 1), settings)[1] == "1MB を超えている"
        assert file_blocks("資料.docx", b"PK", settings)[1] == "対応していない形式（.docx）"
        assert file_blocks("壊れた.xlsx", b"not a zip", settings)[1] == "Excelとして読めない"

    def test_a_reading_is_kept_and_not_paid_for_twice(self, tmp_path):
        config = make_config(tmp_path)
        client = fake_client({"readable": True, "invoice_number": "T202610"})
        cost = SimpleNamespace(added=[], add_usage=lambda usage: cost.added.append(usage))
        reader = InvoiceReader(config, client=client, cost=cost, cache_dir=tmp_path / "cache")
        first = reader.read_invoice("請求書.pdf", b"%PDF-1.4 abc")
        assert first["invoice_number"] == "T202610" and first["file"] == "請求書.pdf"
        assert client.kwargs["output_config"]["format"]["type"] == "json_schema" and client.kwargs["output_config"]["effort"] == "medium"
        assert client.content[0]["type"] == "document" and "請求書.pdf" in client.content[1]["text"]
        again = InvoiceReader(config, client=client, cost=cost, cache_dir=tmp_path / "cache")
        assert again.read_invoice("請求書.pdf", b"%PDF-1.4 abc")["invoice_number"] == "T202610"
        assert client.calls == 1 and again.cache_hits == 1 and len(cost.added) == 1
        assert reader.usage == {"input_tokens": 3000, "output_tokens": 500, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        # 中身が違えば読み直す
        again.read_invoice("請求書.pdf", b"%PDF-1.4 other")
        assert client.calls == 2

    def test_an_order_misread_as_another_kind_is_read_again(self, tmp_path):
        # 実例（2026-11-05）: 表題「注文書（直送依頼書）」を「契約書・取引条件」と読み、行を書き出さなかった
        wrong = {"readable": True, "doc_type": DOC_CONTRACT, "title": "注文書（直送依頼書）", "lines": [], "subtotal": 0, "total": 0}
        right = {"readable": True, "doc_type": DOC_CONTRACT, "title": "注文書（直送依頼書）",
                 "lines": [{"code": "S0384-H-M", "quantity": 15, "unit_price": 1312, "amount": 19680}], "subtotal": 19680, "total": 21648}
        client = fake_client([wrong, right])
        reader = InvoiceReader(make_config(tmp_path), client=client)
        result = reader.read_source("②K12079.pdf", b"%PDF-1.4")
        assert client.calls == 2 and result["doc_type"] == DOC_ORDER and len(result["lines"]) == 1
        assert "この文書は表題から注文書です" in client.content[-1]["text"]
        # 契約書の表題なら読み直さない
        client = fake_client({"readable": True, "doc_type": DOC_CONTRACT, "title": "商品契約書", "lines": [], "subtotal": 0, "total": 0})
        InvoiceReader(make_config(tmp_path), client=client).read_source("契約書.pdf", b"%PDF-1.4")
        assert client.calls == 1

    def test_a_spaced_title_or_the_file_name_also_triggers_the_second_reading(self, tmp_path):
        # 実例（2026-11-05）: 表題が「注　文　書（直送依頼書）」と字間の空いた文書を見逃した
        line_15 = [{"code": "S0384-H-M", "quantity": 5, "unit_price": 1312, "amount": 6560}]
        wrong = {"readable": True, "doc_type": DOC_CONTRACT, "title": "注　文　書 （直送依頼書）", "lines": [], "subtotal": 0, "total": 0}
        client = fake_client([wrong, {**wrong, "lines": line_15}])
        result = InvoiceReader(make_config(tmp_path), client=client).read_source("③J33.pdf", b"%PDF-1.4")
        assert client.calls == 2 and result["doc_type"] == DOC_ORDER
        # 表題に出ていなくても、フォルダ名・ファイル名が注文書なら確かめ直す（注文書と答えたときだけ受ける）
        untitled = {"readable": True, "doc_type": DOC_CONTRACT, "title": "ユーコープ10-2", "lines": [], "subtotal": 0, "total": 0}
        client = fake_client([untitled, {**untitled, "doc_type": DOC_ORDER, "lines": line_15}])
        result = InvoiceReader(make_config(tmp_path), client=client).read_source("注文書/③J33.pdf", b"%PDF-1.4")
        assert client.calls == 2 and result["doc_type"] == DOC_ORDER and "注文書の可能性があります" in client.content[-1]["text"]
        client = fake_client([untitled, {**untitled, "lines": line_15}])  # 2回目も注文書と言わなければそのまま
        result = InvoiceReader(make_config(tmp_path), client=client).read_source("注文書/単価表.pdf", b"%PDF-1.4")
        assert client.calls == 2 and result["doc_type"] == DOC_CONTRACT and result["lines"] == []
        # 当社の請求書（旧版）は、フォルダ名が注文書でも読み直さない
        client = fake_client({"readable": True, "doc_type": DOC_INVOICE, "title": "御請求書", "lines": [], "subtotal": 0, "total": 0})
        InvoiceReader(make_config(tmp_path), client=client).read_source("発注書/×旧請求書.pdf", b"%PDF-1.4")
        assert client.calls == 1

    def test_a_reading_whose_lines_do_not_add_up_is_read_again(self, tmp_path):
        short = {"readable": True, "lines": [{"amount": 100}], "subtotal": 300, "total": 330}
        full = {"readable": True, "lines": [{"amount": 100}, {"amount": 200}], "subtotal": 300, "total": 330}
        client = fake_client([short, full])
        result = InvoiceReader(make_config(tmp_path), client=client).read_invoice("請求書.pdf", b"%PDF-1.4")
        assert client.calls == 2 and len(result["lines"]) == 2
        assert "行の金額の合計が文書の小計と合いませんでした" in client.content[-1]["text"]
        # 読み直しても合わなければ最初の読み取りのまま（請求書そのものの誤りとして検算で出る）
        client = fake_client([short, short])
        result = InvoiceReader(make_config(tmp_path), client=client).read_invoice("請求書.pdf", b"%PDF-1.4")
        assert client.calls == 2 and len(result["lines"]) == 1
        # 合っていれば読み直さない。相殺で小計が0円の請求書も読み直さない
        client = fake_client({"readable": True, "lines": [{"amount": 79771}, {"amount": -79771}], "subtotal": 0, "total": 0})
        InvoiceReader(make_config(tmp_path), client=client).read_invoice("請求書.pdf", b"%PDF-1.4")
        assert client.calls == 1

    def test_an_answer_that_is_not_json_is_unreadable(self, tmp_path):
        reader = InvoiceReader(make_config(tmp_path), client=fake_client("読めませんでした"))
        result = reader.read_source("注文書.pdf", b"%PDF-1.4")
        assert result["readable"] is False and result["unread_reason"] == "読み取りに失敗" and result["file"] == "注文書.pdf"

    def test_an_unsupported_file_is_not_sent_to_the_model(self, tmp_path):
        client = fake_client({})
        reader = InvoiceReader(make_config(tmp_path), client=client)
        assert reader.read_source("資料.docx", b"PK")["unread_reason"] == "対応していない形式（.docx）"
        assert client.calls == 0

    def test_the_request_note_and_leftovers_go_to_the_interpretation(self, tmp_path):
        client = fake_client({"pairs": [], "non_order_lines": [], "exemptions": []})
        reader = InvoiceReader(make_config(tmp_path), client=client)
        assert reader.interpret("品切商品（請求なし）", {"invoice_lines": [], "source_lines": [{"id": "S1"}], "files": []}) == {"pairs": [], "non_order_lines": [], "exemptions": []}
        text = client.content[0]["text"]
        assert text.startswith("依頼文:\n品切商品（請求なし）") and '"id": "S1"' in text
        assert "説明が見当たらない行は" in client.kwargs["system"]

    def test_the_remarks_of_the_shipment_table_are_read_once_per_content(self, tmp_path):
        rows = [ship_row(75, "2026-09-18", "2026-09-22", "商品発送依頼", "", "全商品在庫ありません。")]
        answer = {"rows": [fact(rows[0], nothing_shipped=True, quote="全商品在庫ありません。")]}
        client = fake_client(answer)
        reader = InvoiceReader(make_config(tmp_path), client=client, cache_dir=tmp_path / "cache")
        assert reader.read_shiplog("ナガセテクノサービス", rows) == answer
        text = client.content[0]["text"]
        assert text.startswith("取引先「ナガセテクノサービス」あての発送依頼の行:\n") and '"team_comment": "全商品在庫ありません。"' in text
        assert '"id": "2026-09#75"' in text and "handler" not in text and "竹内" not in text  # 読むのに要る欄だけ渡す
        assert "上記4品番以外発送済" in client.kwargs["system"]
        assert client.kwargs["output_config"]["format"]["schema"]["properties"]["rows"]["items"]["properties"]["purpose"]["enum"] == [PURPOSE_NORMAL, PURPOSE_SAMPLE, PURPOSE_CLAIM]
        reader.read_shiplog("ナガセテクノサービス", rows)
        assert client.calls == 1 and reader.cache_hits == 1  # 行が変わらなければ読み直さない
        reader.read_shiplog("ナガセテクノサービス", [{**rows[0], "tracking": "67572395391"}])
        assert client.calls == 2
        assert reader.read_shiplog("ナガセテクノサービス", []) == {} and client.calls == 2
        failed = InvoiceReader(make_config(tmp_path), client=fake_client("読めませんでした"))
        assert failed.read_shiplog("ナガセテクノサービス", rows) == {}

    def test_an_excel_order_is_read_as_text_with_real_dates(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        from datetime import date
        from io import BytesIO

        from raizuinu.sheettext import xlsx_to_text

        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "注文書"
        sheet["B2"] = "注文書"
        sheet["G3"] = date(2026, 10, 5)
        sheet["G3"].number_format = '[$-411]ggge"年"m"月"d"日"'
        sheet.append([])
        sheet.append([None, "No.", "品番", "数量", "単価", "金額"])
        sheet.append([None, 1, "C017-A", 5, 1380, "=D6*E6"])
        buffer = BytesIO()
        book.save(buffer)
        text = xlsx_to_text(buffer.getvalue())
        assert text.startswith("# シート: 注文書\n")
        assert "2026-10-05" in text and "\tNo.\t品番\t数量\t単価\t金額" in text and "\t1\tC017-A\t5\t1380" in text
        blocks, reason = file_blocks("10.5 発注書.xlsx", buffer.getvalue(), {})
        assert reason == "" and blocks[0]["text"].startswith("Excelファイル「10.5 発注書.xlsx」の内容（タブ区切り）:\n# シート: 注文書")
