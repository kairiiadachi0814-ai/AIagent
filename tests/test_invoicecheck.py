"""発行請求書の確認（FR-15）のテスト。

実例を元にした形で、照合の規則を固定する（金額・品番は実物を簡略にしたもの）:
- 2026-11-04 ナガセ 10月分: 同じ注文を、注文全体と出荷分の控え2通の両方で計上（二重計上）
- 2026-11-04 旅サラダ: 相殺後の残高の誤記（11,069円を10,069円）
- 2026-11-04 新日本カレンダー: 訂正前の納期の日付が明細に残っていた
- 2026-10-02 ナガセ 9月分: 入荷待ちの2行を請求（承認後に営業の連絡で削除）
- 2026-11-05 サクラマチ: 問題なし（出荷の記録が無い注文は留意どまり）
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
    FIX, LINE_BOXED, LINE_NORMAL, LINE_STRUCK, NOTE, VERDICT_OK,
    GroupCheck, build_order_book, check_arithmetic, check_requirements, line_kind, norm_code, to_int,
)
from raizuinu.invoiceread import FEE_MISMATCH, FEE_OK, FEE_UNKNOWN, InvoiceReader, file_blocks
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


def run(invoices, sources, mapping=None, review=None, settings=None, unread=None):
    check = GroupCheck("テスト商事", invoices, sources, settings or SETTINGS, unread)
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

    def test_a_fee_that_contradicts_the_terms_must_be_fixed(self):
        review = {"fee_checks": [{"invoice_line_id": "I2", "verdict": FEE_MISMATCH, "basis": "契約書M.pdf では300枚の名入代は1,500円ではなく2,000円"}], "findings": []}
        _, result = run(*calendar_case(), mapping=calendar_mapping(), review=review)
        assert [(f["severity"], f["by"]) for f in found(result, "fee")] == [(FIX, "所見")]

    def test_model_findings_need_evidence(self):
        review = {"fee_checks": [], "findings": [
            {"severity": CONFIRM, "invoice": "請求書-01.pdf", "title": "単価114円は契約書に無い", "detail": "見積による単価", "evidence": "契約書M.pdf の単価表"},
            {"severity": FIX, "invoice": "", "title": "根拠の無い所見", "detail": "…", "evidence": ""},
            {"severity": "重大", "invoice": "", "title": "知らない重さ", "detail": "…", "evidence": "x"},
        ]}
        _, result = run(*calendar_case(), mapping=calendar_mapping(), review=review)
        kept = found(result, "review")
        assert [f["title"] for f in kept] == ["単価114円は契約書に無い"]
        assert kept[0]["by"] == "所見" and "（根拠: 契約書M.pdf の単価表）" in kept[0]["detail"]


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
        note, groups = load_case(case)
        assert note == "ご確認ください" and [g["name"] for g in groups] == ["ナガセ"]
        assert groups[0]["invoices"] == [("請求書.pdf", b"%PDF-1")]
        assert {name for name, _ in groups[0]["sources"]} == {"注文書/①.pdf", "契約書.pdf"}  # 下の階層も読む


# --- 読み取り ---


def fake_client(payload):
    client = SimpleNamespace(calls=0, kwargs=None, content=None)

    def create(messages, **kwargs):
        client.calls += 1
        client.kwargs, client.content = kwargs, messages[0]["content"]
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
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
