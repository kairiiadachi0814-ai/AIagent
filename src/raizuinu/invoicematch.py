"""発行請求書の照合（モデルを使わない部分）。

読み取り済みの請求書と作成元資料（注文書・契約書・管理表）を突き合わせ、指摘を作る。
ここでは行の突合・金額の検算・出荷状況の判定だけを行う。文書の読み取りと、コードでは
決められない所見（契約条件の解釈など）は invoiceread 側のモデルに任せる
（要件定義書 FR-15。金額や判定をモデルに作文させない）。

取引先ごとに1回（`GroupCheck`）。同じ取引先に請求書が複数あるとき（-01・-02）は、
まとめて資料と突き合わせる（片方の請求書だけ見ると、もう片方の注文が請求漏れに見えるため）。

実例から決めた規則:
- 同じ注文番号の注文書が複数あれば同じ注文（出荷分の控えなど）。注文の1行は1回しか
  請求できない（2026-11-04: ナガセ 10月分。10/1 の注文を、注文全体と出荷分の控え2通の
  両方で計上し 106,324円の二重計上）
- 出荷分の控えで消し線になっている行、入荷待ち管理表に載っている行は未出荷
  （2026-10-02: ナガセ 9月分。入荷待ちの2行を請求し、承認後に営業の連絡で削除）
- 備考・注記に書かれた金額は資料の金額と突き合わせる（2026-11-04: 旅サラダ。
  相殺後の残高 11,069円を 10,069円と記載）
- 明細の日付は、注文日・出荷日・納品日のどれかに一致するはず（2026-11-04: 新日本
  カレンダー。訂正前の納期の日付が残っていた）
- 卸の出荷は、出荷管理表（発送依頼の一覧。shiplog）の行と注文を結んで確かめる（指示 2026-11-05）。
  完了のチェックだけでは裏付けにしない（2026-09-18 ナガセ: 完了・伝票番号なし・「全商品在庫
  ありません。」の注文を請求した）。品目が合うだけでは行と注文を結ばない（同じ商品の注文が
  毎週来る）。どの注文とも結べない出荷は、資料に無い注文（請求漏れ）の可能性として聞く
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, timedelta
from typing import Any

from .shiplog import TASK_SHIP

# --- 語彙（invoiceread のスキーマと共有） ---

DOC_ORDER = "注文書"
DOC_CONTRACT = "契約書・取引条件"
DOC_SALES = "売上報告書"
DOC_LEDGER = "相殺・残高の管理表"
DOC_BACKORDER = "欠品・入荷待ちの管理表"
DOC_SHIPMENT = "出荷記録"
DOC_INVOICE = "請求書"
DOC_OTHER = "その他"
DOC_TYPES = [DOC_ORDER, DOC_CONTRACT, DOC_SALES, DOC_LEDGER, DOC_BACKORDER, DOC_SHIPMENT, DOC_INVOICE, DOC_OTHER]

LINE_NORMAL, LINE_STRUCK, LINE_BOXED = "通常", "消し線", "囲み"
LINE_STATUSES = [LINE_NORMAL, LINE_STRUCK, LINE_BOXED]

BACKORDER_WAITING, BACKORDER_SHIPPED, BACKORDER_OTHER = "入荷待ち", "発送済", "その他"
BACKORDER_STATUSES = [BACKORDER_WAITING, BACKORDER_SHIPPED, BACKORDER_OTHER]

FIX, CONFIRM, NOTE = "要修正", "要確認", "留意"
SEVERITIES = [FIX, CONFIRM, NOTE]
VERDICT_OK = "問題なし"

# 注文の行の状態
OPEN, SHIPPED, NOT_SHIPPED, CANCELLED, BACKORDERED = "出荷不明", "出荷済", "未出荷", "取消", "入荷待ち"
_NOT_BILLABLE = (NOT_SHIPPED, CANCELLED, BACKORDERED)

# 請求書の行の種類
KIND_ITEM, KIND_FEE, KIND_ADJUST = "商品", "付随費用", "調整"

_FEE_RE = re.compile(r"(送料|運賃|配送料|名入代|名入れ代|版代|手数料)")
_ADJUST_RE = re.compile(r"(相殺|値引|過請求|返金|返品|調整|割引|控除)")
_DASHES = "‐‑‒–—―−－ｰー"

# 出荷管理表の行（商品発送）の用途。サンプル・クレーム対応は請求の対象外なので、注文と結ばない
PURPOSE_NORMAL, PURPOSE_SAMPLE, PURPOSE_CLAIM = "通常の出荷", "サンプル", "クレーム・不良対応"
SHIP_PURPOSES = [PURPOSE_NORMAL, PURPOSE_SAMPLE, PURPOSE_CLAIM]
_PURPOSE_WORDS = {
    PURPOSE_SAMPLE: ("サンプル", "見本"),
    PURPOSE_CLAIM: ("クレーム", "不良", "交換", "再送", "破損", "不足分"),
}
_CODE_TOKEN_RE = re.compile(r"[A-Z]{1,2}\d{3,5}(?:-[A-Z0-9]+)*")
MIN_NAME_CHARS = 4  # 品名で同じ商品とみる最短の長さ
MAX_SHIP_DATE_SHIFT = 14  # コメントから読んだ発送日・着日を受ける、期日からのずれ（日）
MAX_TRACKING_CHARS, MAX_COMMENT_CHARS = 40, 80

# 出荷管理表の行と注文を結ぶ見込みの点数（_link_score）
_REQUEST_SCORES = (40, 30, 15, 5)  # 依頼日が注文日の当日・翌平日・2日後・3日後
_WINDOW_SCORE, _SHIP_DATE_SCORE, _ARRIVAL_SCORE = 30, 10, 20
_ITEM_SCORE, _QUANTITY_SCORE, _OTHER_ITEM_PENALTY = 100, 20, 20  # 減点は翌平日の点（30）より小さく
_WINDOW_BEFORE, _WINDOW_AFTER, _WINDOW_BEFORE_DELIVERY = 3, 3, 5


def to_int(value: Any) -> int:
    """「¥17,700」「17700.0」→ 17700。読めなければ 0。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(round(value))
    text = unicodedata.normalize("NFKC", str(value or "")).replace(",", "").replace("¥", "").replace("円", "").strip()
    text = text.replace("▲", "-").replace("△", "-")
    try:
        return int(round(float(text)))
    except ValueError:
        return 0


def to_number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = unicodedata.normalize("NFKC", str(value or "")).replace(",", "").strip()
    try:
        return float(text)
    except ValueError:
        return 0.0


def norm_code(text: Any) -> str:
    """品番を比べられる形に（全角→半角、大文字、ハイフンの異体字をそろえ、空白を除く）。"""
    out = unicodedata.normalize("NFKC", str(text or "")).upper()
    for dash in _DASHES:
        out = out.replace(dash, "-")
    return re.sub(r"\s+", "", out)


def norm_jan(text: Any) -> str:
    return re.sub(r"\D", "", unicodedata.normalize("NFKC", str(text or "")))


def norm_name(text: Any) -> str:
    """社名を比べられる形に（空白を除き、㈱・(株) を株式会社にそろえる）。"""
    out = unicodedata.normalize("NFKC", str(text or ""))
    out = out.replace("(株)", "株式会社").replace("㈱", "株式会社")
    return re.sub(r"[\s　]+", "", out)


def yen(amount: Any) -> str:
    return f"{to_int(amount):,}円"


def short_date(text: Any) -> str:
    """2026-10-09 → 10/9。日付でなければそのまま。"""
    match = re.match(r"^\d{4}-(\d{2})-(\d{2})$", str(text or ""))
    return f"{int(match.group(1))}/{int(match.group(2))}" if match else str(text or "")


def same_item(a: dict, b: dict) -> bool:
    """同じ商品か。品番が一致、または両方にJANがあって一致。"""
    code_a, code_b = norm_code(a.get("code")), norm_code(b.get("code"))
    if code_a and code_a == code_b:
        return True
    jan_a, jan_b = norm_jan(a.get("jan")), norm_jan(b.get("jan"))
    return bool(jan_a) and jan_a == jan_b


def same_quantity(a: dict, b: dict) -> bool:
    return abs(to_number(a.get("quantity")) - to_number(b.get("quantity"))) < 1e-6


def line_kind(line: dict) -> str:
    """請求書の行が商品か、付随費用（送料・名入代）か、調整（相殺・値引）か。

    「台紙1色名入れ 240枚」のように語を含むだけの商品行を巻き込まないよう、付随費用は
    数量が1の行に限る。
    """
    text = str(line.get("description") or "")
    if to_int(line.get("amount")) < 0 or _ADJUST_RE.search(text):
        return KIND_ADJUST
    if _FEE_RE.search(text) and to_number(line.get("quantity")) in (0.0, 1.0):
        return KIND_FEE
    return KIND_ITEM


def finding(
    severity: str, code: str, title: str, detail: str, invoice: str = "", amount: int = 0,
    lines: list[str] | None = None, brief: str = "",
) -> dict:
    """指摘1件。invoice は対象の請求書ファイル（取引先全体の指摘なら空）。brief は返信案に載せる短い言い方。"""
    return {
        "severity": severity, "code": code, "title": title, "detail": detail,
        "invoice": invoice, "amount": int(amount), "lines": list(lines or []), "by": "照合", "brief": brief,
    }


def worst(findings: list[dict]) -> str:
    for severity in (FIX, CONFIRM):
        if any(f.get("severity") == severity for f in findings):
            return severity
    return VERDICT_OK


# --- 注文の一覧（資料側） ---


def _order_key(source: dict) -> str:
    number = norm_code(source.get("order_number"))
    return number if number else "FILE:" + str(source.get("file") or "")


def _signature(source: dict) -> tuple:
    """注文番号の無い注文書が同じ内容かを見るための形（日付と行）。"""
    lines = sorted(
        (norm_code(l.get("code")) or norm_jan(l.get("jan")), to_number(l.get("quantity")), to_int(l.get("unit_price")))
        for l in source.get("lines") or []
    )
    return (str(source.get("order_date") or ""), tuple(lines))


def build_order_book(sources: list[dict]) -> tuple[list[dict], list[dict]]:
    """注文書から、注文とその行の一覧を作る。→ (注文, 注文の行)

    同じ注文番号の注文書は同じ注文の控えとしてまとめ、行は1組だけ持つ。出荷分の控え
    （出荷した行だけ残して他を線で消したもの）があれば、行ごとの出荷状況をそこから決める。
    """
    order_docs = [s for s in sources if s.get("readable", True) and s.get("doc_type") == DOC_ORDER and s.get("lines")]
    grouped: dict[str, list[dict]] = {}
    signatures: dict[tuple, str] = {}
    for source in order_docs:
        key = _order_key(source)
        if key.startswith("FILE:"):
            # 注文番号が無くても、日付と行が全く同じなら同じ注文の控え
            key = signatures.setdefault(_signature(source), key)
        grouped.setdefault(key, []).append(source)

    orders: list[dict] = []
    lines: list[dict] = []
    for key, copies in grouped.items():
        base = sorted(
            copies,
            key=lambda s: (
                bool(s.get("is_shipment_copy")),
                -len(s.get("lines") or []),
                sum(1 for l in s.get("lines") or [] if l.get("status") == LINE_STRUCK),
            ),
        )[0]
        shipment_copies = [c for c in copies if c.get("is_shipment_copy")]
        evidence = next((str(c.get("shipping_evidence")) for c in copies if c.get("shipping_evidence")), "")
        order = {
            "key": key,
            "order_number": str(base.get("order_number") or ""),
            "files": [str(c.get("file") or "") for c in copies],
            "order_date": str(base.get("order_date") or ""),
            "ship_date": str(base.get("ship_date") or ""),
            "delivery_date": str(base.get("delivery_date") or ""),
            "ship_to": str(base.get("ship_to") or ""),
            "evidence": evidence,
            "copies": len(copies),
            "has_shipment_copies": bool(shipment_copies),
        }
        orders.append(order)
        own: list[dict] = []
        for raw in base.get("lines") or []:
            line = {
                "id": f"S{len(lines) + len(own) + 1}",
                "order": key,
                "order_number": order["order_number"],
                "file": str(base.get("file") or ""),
                "code": str(raw.get("code") or ""),
                "jan": str(raw.get("jan") or ""),
                "description": str(raw.get("description") or ""),
                "quantity": to_number(raw.get("quantity")),
                "unit_price": to_int(raw.get("unit_price")),
                "amount": to_int(raw.get("amount")),
                "order_date": order["order_date"],
                "ship_date": order["ship_date"],
                "delivery_date": order["delivery_date"],
                "requested_ship_date": order["ship_date"],
                "state": OPEN,
                "state_reason": "",
                "note": str(raw.get("note") or ""),
            }
            if shipment_copies:
                line["state"], line["ship_date"] = NOT_SHIPPED, ""
                line["state_reason"] = "出荷分の控えで消し線"
            elif raw.get("status") == LINE_STRUCK:
                line["state"], line["state_reason"] = CANCELLED, "注文書で消し線"
            elif evidence:
                line["state"], line["state_reason"] = SHIPPED, evidence
            own.append(line)
        for copy in shipment_copies:
            for raw in copy.get("lines") or []:
                if raw.get("status") == LINE_STRUCK:
                    continue
                hit = next(
                    (l for l in own if l["state"] == NOT_SHIPPED and same_item(l, raw) and same_quantity(l, raw)),
                    None,
                )
                if hit is not None:
                    hit["state"] = SHIPPED
                    hit["ship_date"] = str(copy.get("ship_date") or "")
                    hit["state_reason"] = f"出荷分の控え（{copy.get('file')}）"
        lines += own

    _apply_backorders(sources, lines)
    return orders, lines


def _apply_backorders(sources: list[dict], lines: list[dict]) -> None:
    """欠品・入荷待ち管理表の内容を、注文の行の状態に反映する。"""
    for source in sources:
        for entry in source.get("backorders") or []:
            status = str(entry.get("status") or "")
            if status not in (BACKORDER_WAITING, BACKORDER_SHIPPED):
                continue
            number = norm_code(entry.get("order_number"))
            for line in lines:
                if number and norm_code(line.get("order_number")) != number:
                    continue
                if not same_item(line, entry):
                    continue
                if to_number(entry.get("quantity")) and not same_quantity(line, entry):
                    continue
                if status == BACKORDER_WAITING and line["state"] != BACKORDERED:
                    line["state"], line["ship_date"] = BACKORDERED, ""
                    line["state_reason"] = f"入荷待ち管理表（{source.get('file')}）"
                    break
                if status == BACKORDER_SHIPPED and line["state"] in (OPEN, NOT_SHIPPED):
                    line["state"] = SHIPPED
                    line["ship_date"] = str(entry.get("ship_date") or "") or line["ship_date"]
                    line["state_reason"] = f"入荷待ち管理表で発送済（{source.get('file')}）"
                    break


# --- 出荷管理表（卸の出荷の実績。shiplog が行に直したもの） ---


def _norm_text(text: Any) -> str:
    """文中の品番を拾える形に（全角→半角、大文字、ハイフンの異体字をそろえる。空白は残す）。"""
    out = unicodedata.normalize("NFKC", str(text or "")).upper()
    for dash in _DASHES:
        out = out.replace(dash, "-")
    return out


def code_tokens(*texts: Any) -> set[str]:
    """文中に書かれた品番（R0560-A、S0384-H-M など）。"""
    out: set[str] = set()
    for text in texts:
        out |= set(_CODE_TOKEN_RE.findall(_norm_text(text)))
    return out


def codes_related(a: Any, b: Any) -> bool:
    """同じ品番か、枝番違い（R0528-A と R0528-A-A）。"""
    a, b = norm_code(a), norm_code(b)
    return bool(a) and bool(b) and (a == b or a.startswith(b + "-") or b.startswith(a + "-"))


def _to_date(text: Any) -> date | None:
    try:
        return date.fromisoformat(str(text or ""))
    except ValueError:
        return None


def days_between(start: Any, end: Any) -> int | None:
    a, b = _to_date(start), _to_date(end)
    return (b - a).days if a and b else None


def weekday_gap(start: Any, end: Any) -> int | None:
    """start の後、end までの平日の日数（同じ日は 0、金曜→月曜は 1）。end が前なら負の数。"""
    a, b = _to_date(start), _to_date(end)
    if not a or not b:
        return None
    if b < a:
        return (b - a).days
    return sum(1 for n in range(1, (b - a).days + 1) if (a + timedelta(days=n)).weekday() < 5)


def _shift(day: Any, days: int) -> str:
    found = _to_date(day)
    return (found + timedelta(days=days)).isoformat() if found else ""


def month_end(day: Any) -> str:
    found = _to_date(day)
    if not found:
        return ""
    first_of_next = date(found.year + (found.month == 12), found.month % 12 + 1, 1)
    return (first_of_next - timedelta(days=1)).isoformat()


def _short(text: Any, limit: int) -> str:
    out = _one_line(text)
    return out if len(out) <= limit else out[:limit] + "…"


def ship_tasks(rows: list[dict], facts: dict | None = None) -> list[dict]:
    """出荷管理表の「商品発送」の行に、コメント欄の読み取り（モデル）を足す。

    読み取りは、行の文面で確かめられるものだけを受ける（品番・品名がその行に書かれている、
    根拠の引用がその行にある、サンプル等の語がある）。読み取りが無くても、依頼日・期日・完了・
    伝票番号だけで出荷の裏付けには使える。
    """
    by_id = {str(f.get("id") or ""): f for f in (facts or {}).get("rows") or []}
    out: list[dict] = []
    for row in rows:
        if not str(row.get("task") or "").startswith(TASK_SHIP):
            continue
        text = "\n".join(str(row.get(k) or "") for k in ("comment", "team_comment", "tracking"))
        flat, tokens = norm_code(text), code_tokens(text)
        fact = by_id.get(str(row.get("id") or ""), {})

        def written(entry: dict) -> bool:
            code, name = norm_code(entry.get("code")), norm_code(entry.get("description"))
            return code in tokens if code else (len(name) >= MIN_NAME_CHARS and name in flat)

        def entry(raw: dict) -> dict:
            return {"code": str(raw.get("code") or ""), "description": str(raw.get("description") or ""),
                    "quantity": to_number(raw.get("quantity")), "quote": _one_line(raw.get("quote"))}

        quote = _one_line(fact.get("quote"))
        quoted = bool(quote) and norm_code(quote) in flat
        base = row.get("due_date") or row.get("request_date")
        shipped_on = str(fact.get("shipped_on") or "")
        shift = days_between(base, shipped_on)
        arrival = str(fact.get("arrival_date") or "")
        ahead = days_between(base, arrival)
        purpose = str(fact.get("purpose") or "")
        if not any(word in text for word in _PURPOSE_WORDS.get(purpose, ())):
            purpose = PURPOSE_NORMAL
        if not fact:  # 読み取りが無いときは、語だけで見分ける
            purpose = next((p for p, words in _PURPOSE_WORDS.items() if any(w in text for w in words)), PURPOSE_NORMAL)
        task = {
            **row, "tokens": tokens, "read": bool(fact),
            "items": [entry(i) for i in fact.get("items") or [] if written(i)],
            "unshipped": [entry(i) for i in fact.get("unshipped") or [] if written(i)],
            "nothing_shipped": bool(fact.get("nothing_shipped")) and quoted,
            "partial": bool(fact.get("partial")) and quoted,
            "quote": quote if quoted else "",
            "shipped_on": shipped_on if shift is not None and abs(shift) <= MAX_SHIP_DATE_SHIFT else "",
            "arrival_date": arrival if ahead is not None and 0 <= ahead <= MAX_SHIP_DATE_SHIFT else "",
            "purpose": purpose,
        }
        task["date"] = task["shipped_on"] or str(row.get("due_date") or "") or str(row.get("request_date") or "")
        out.append(task)
    return out


def task_ships(task: dict) -> bool:
    """その行が出荷の裏付けになるか。

    完了でも出荷していないことがある（実例 2026-09-18 ナガセ: 完了・伝票番号なし・納品チーム
    コメント「全商品在庫ありません。」）。伝票番号が無く納品チームのコメントがある行は、裏付けにしない。
    """
    if not task.get("done") or task.get("nothing_shipped"):
        return False
    return bool(str(task.get("tracking") or "").strip()) or not str(task.get("team_comment") or "").strip()


def task_evidence(task: dict) -> str:
    """出荷の裏付けとして報告に書く形。"""
    tracking = _short(task.get("tracking"), MAX_TRACKING_CHARS) or "完了・伝票番号の記載なし"
    return f"出荷管理表 {task.get('label')}（期日 {short_date(task.get('due_date'))}、{tracking}）"


def task_doubt(task: dict) -> str:
    """出荷の裏付けにならない行の、理由の言い方。"""
    label = f"出荷管理表 {task.get('label')}（期日 {short_date(task.get('due_date'))}）"
    comment = _short(task.get("team_comment"), MAX_COMMENT_CHARS)
    if not task.get("done"):
        return f"{label}は完了になっていません"
    if task.get("nothing_shipped"):
        return f"{label}に「{task.get('quote')}」とあります"
    return f"{label}は伝票番号が無く、納品チームコメントに「{comment}」とあります"


def task_view(task: dict) -> dict:
    """報告書に出す、行の要点（コメントは短く切る。住所などを長々と写さない）。"""
    return {
        "label": str(task.get("label") or ""), "request_date": str(task.get("request_date") or ""),
        "due_date": str(task.get("due_date") or ""), "date": str(task.get("date") or ""),
        "done": bool(task.get("done")), "ships": task_ships(task), "purpose": str(task.get("purpose") or ""),
        "tracking": _short(task.get("tracking"), MAX_TRACKING_CHARS),
        "comment": _short(task.get("comment"), MAX_COMMENT_CHARS),
        "team_comment": _short(task.get("team_comment"), MAX_COMMENT_CHARS),
        "link": _one_line(task.get("link")),
    }


# --- 請求書ごとの検算と記載要件 ---


def check_arithmetic(invoice: dict) -> list[dict]:
    """行の金額、小計、消費税、合計を計算し直す。"""
    name = str(invoice.get("file") or "")
    out: list[dict] = []
    lines = invoice.get("lines") or []
    for line in lines:
        quantity, price, amount = to_number(line.get("quantity")), to_int(line.get("unit_price")), to_int(line.get("amount"))
        if quantity and price and int(round(quantity * price)) != amount:
            out.append(finding(
                FIX, "line_amount", "行の金額が数量×単価と合わない",
                f"{short_date(line.get('date'))} {line.get('description')}: {quantity:g}×{yen(price)}＝{yen(round(quantity * price))} のはずが {yen(amount)}",
                name,
            ))
    line_total = sum(to_int(l.get("amount")) for l in lines)
    subtotal, tax_total, total = to_int(invoice.get("subtotal")), to_int(invoice.get("tax_total")), to_int(invoice.get("total"))
    if lines and line_total != subtotal:
        out.append(finding(FIX, "subtotal", "明細の合計と小計が合わない", f"明細の合計 {yen(line_total)}、小計 {yen(subtotal)}", name))
    cover = invoice.get("cover_lines") or []
    if cover and lines:
        cover_total = sum(to_int(l.get("amount")) for l in cover)
        if cover_total != line_total:
            out.append(finding(
                FIX, "cover_total", "請求書本体と別紙明細の合計が合わない",
                f"請求書本体 {yen(cover_total)}、別紙明細 {yen(line_total)}", name,
            ))
    breakdown = [b for b in invoice.get("tax_breakdown") or [] if to_number(b.get("rate"))]
    expected_tax = 0
    for part in breakdown:
        rate, base, tax = to_number(part.get("rate")), to_int(part.get("base")), to_int(part.get("tax"))
        rated = [l for l in lines if to_number(l.get("tax_rate")) == rate]
        if rated and sum(to_int(l.get("amount")) for l in rated) != base:
            out.append(finding(
                FIX, "tax_base", f"{rate:g}%対象の金額が明細と合わない",
                f"明細の {rate:g}% の行の合計 {yen(sum(to_int(l.get('amount')) for l in rated))}、税率別の欄 {yen(base)}", name,
            ))
        exact = base * rate / 100
        floor, nearest, ceil = math.floor(exact + 1e-9), int(round(exact)), math.ceil(exact - 1e-9)
        expected_tax += floor
        if tax == floor:
            continue
        if tax in (nearest, ceil):
            how = "四捨五入" if tax == nearest else "切り上げ"
            out.append(finding(NOTE, "tax_rounding", "消費税の端数処理が切り捨てではない", f"{rate:g}%対象 {yen(base)} に対し消費税 {yen(tax)}（{how}）。当社の他の請求書は切り捨て", name))
        else:
            out.append(finding(FIX, "tax", "消費税額が合わない", f"{rate:g}%対象 {yen(base)} の消費税は {yen(floor)}（切り捨て）。請求書は {yen(tax)}", name))
    if breakdown and sum(to_int(b.get("tax")) for b in breakdown) != tax_total:
        out.append(finding(FIX, "tax_total", "税率別の消費税の合計と消費税欄が合わない", f"税率別の合計 {yen(sum(to_int(b.get('tax')) for b in breakdown))}、消費税欄 {yen(tax_total)}", name))
    if subtotal + tax_total != total:
        out.append(finding(FIX, "total", "合計が小計＋消費税と合わない", f"{yen(subtotal)}＋{yen(tax_total)}＝{yen(subtotal + tax_total)} のはずが {yen(total)}", name))
    return out


def check_requirements(invoice: dict, issuers: list[dict]) -> tuple[list[dict], list[dict]]:
    """適格請求書の記載要件。→ (要件ごとの確認結果, 指摘)"""
    name = str(invoice.get("file") or "")
    rows: list[dict] = []
    out: list[dict] = []

    def row(requirement: str, ok: bool, text: str) -> None:
        rows.append({"requirement": requirement, "ok": bool(ok), "text": text})

    issuer_name = norm_name(invoice.get("issuer_name"))
    number = norm_code(invoice.get("registration_number")).replace("-", "")
    known = next((i for i in issuers if norm_name(i.get("name")) == issuer_name), None)
    expected = norm_code((known or {}).get("registration_number")).replace("-", "")
    valid_form = bool(re.match(r"^T\d{13}$", number))
    if known is None:
        row("発行事業者の名称・登録番号", valid_form and bool(issuer_name), f"{invoice.get('issuer_name')} {invoice.get('registration_number')}（設定に無い発行者）")
        out.append(finding(CONFIRM, "issuer", "発行者が設定に登録されていない", f"発行者「{invoice.get('issuer_name')}」の登録番号を照合できません", name))
    elif number != expected:
        row("発行事業者の名称・登録番号", False, f"{invoice.get('registration_number')}（登録は {known.get('registration_number')}）")
        out.append(finding(FIX, "registration", "登録番号が当社の登録と違う", f"請求書 {invoice.get('registration_number') or '（記載なし）'}、登録 {known.get('registration_number')}", name))
    else:
        row("発行事業者の名称・登録番号", True, f"{invoice.get('issuer_name')} {invoice.get('registration_number')}")

    lines = invoice.get("lines") or []
    undated = [l for l in lines if not str(l.get("date") or "").strip()]
    row("取引年月日", not undated and bool(lines), "各行に記載" if not undated else f"日付の無い行が {len(undated)} 行")
    if undated:
        out.append(finding(FIX, "line_date", "取引年月日の無い行がある", f"{len(undated)} 行に日付がありません", name))

    reduced = any(to_number(l.get("tax_rate")) == 8 for l in lines)
    row("取引内容（軽減税率対象の旨）", bool(lines) and (not reduced or bool(invoice.get("reduced_rate_note"))),
        "記載あり。軽減税率対象なし" if not reduced else "軽減税率対象の行あり")
    if reduced and not invoice.get("reduced_rate_note"):
        out.append(finding(FIX, "reduced_rate", "軽減税率対象である旨の記載が無い", "8%の行がありますが、軽減税率対象を示す記載が読み取れません", name))

    breakdown = [b for b in invoice.get("tax_breakdown") or [] if to_number(b.get("rate"))]
    row("税率ごとの合計額と適用税率", bool(breakdown), "、".join(f"{to_number(b.get('rate')):g}%対象 {yen(b.get('base'))}" for b in breakdown) or "記載なし")
    row("税率ごとの消費税額", bool(breakdown), "、".join(f"{to_number(b.get('rate')):g}% {yen(b.get('tax'))}" for b in breakdown) or "記載なし")
    if not breakdown:
        out.append(finding(FIX, "tax_breakdown", "税率ごとの金額と消費税額の記載が無い", "税率別の欄が読み取れません", name))

    recipient = str(invoice.get("recipient_name") or "").strip()
    row("交付を受ける事業者の名称", bool(recipient), recipient or "記載なし")
    if not recipient:
        out.append(finding(FIX, "recipient", "宛名が無い", "交付を受ける事業者の名称が読み取れません", name))

    if invoice.get("has_detail_sheet"):
        linked = (
            norm_code(invoice.get("detail_invoice_number")) == norm_code(invoice.get("invoice_number"))
            and norm_code(invoice.get("detail_registration_number")).replace("-", "") == number
        )
        row("請求書と別紙明細の紐付け", linked, "明細書に同じ請求書番号と登録番号" if linked else "明細書の請求書番号・登録番号が請求書と一致しない")
        if not linked:
            out.append(finding(CONFIRM, "detail_link", "別紙明細と請求書の紐付けが弱い", "明細書に請求書番号と登録番号を入れると、2つの書類で要件を満たす形になります", name))
    return rows, out


# --- 取引先ごとの照合 ---


class GroupCheck:
    """1つの取引先の請求書（1通以上）と資料の照合。

    使い方: prepare() → （必要なら）interpretation_payload() をモデルへ渡して
    apply_interpretation() → finish()。
    """

    def __init__(
        self, name: str, invoices: list[dict], sources: list[dict], settings: dict | None = None,
        unread: list[dict] | None = None, shipments: list[dict] | None = None,
    ) -> None:
        self.name = name
        self.invoices = invoices
        self.sources = sources
        self.settings = dict(settings or {})
        self.unread = list(unread or [])
        # 出荷管理表の、この取引先の「商品発送」の行（ship_tasks の結果）。None は表を使わない
        self.shipments = shipments
        self.ship_links: dict[str, list[dict]] = {}  # 注文 → 結び付いた行
        self.ship_shared: set[str] = set()  # 他の注文の行を借りた注文（同梱とみた）
        self.ship_doubts: dict[str, list[str]] = {}  # 注文 → 出荷を確かめられない理由
        self.unattributed: list[dict] = []  # どの注文とも結べなかった行
        self.orders: list[dict] = []
        self.order_lines: list[dict] = []
        self.lines: list[dict] = []  # 請求書の行（全請求書ぶん）
        self.pairs: dict[str, list[str]] = {}  # 請求の行 → 資料の行
        self.used: dict[str, str] = {}  # 資料の行 → 請求の行
        self.duplicates: dict[str, str] = {}  # 請求の行 → 既に使われた資料の行
        self.mismatches: dict[str, dict] = {}  # 請求の行 → {"source", "what"}
        self.exemptions: dict[str, str] = {}  # 資料の行 → 請求しない理由
        self.fee_basis: dict[str, str] = {}  # 請求の行 → 根拠（モデルの対応付けから）
        self.mapped_by_model: set[str] = set()
        self.findings: list[dict] = []

    # --- 準備と完全一致 ---

    def prepare(self) -> None:
        self.orders, self.order_lines = build_order_book(self.sources)
        self.lines = []
        for invoice in self.invoices:
            for raw in invoice.get("lines") or []:
                line = dict(raw)
                line["id"] = f"I{len(self.lines) + 1}"
                line["invoice"] = str(invoice.get("file") or "")
                line["kind"] = line_kind(line)
                line["quantity"] = to_number(line.get("quantity"))
                line["unit_price"] = to_int(line.get("unit_price"))
                line["amount"] = to_int(line.get("amount"))
                self.lines.append(line)
        self._match_exact()
        self._match_near()
        self._find_duplicates()

    @property
    def has_orders(self) -> bool:
        return bool(self.order_lines)

    def _items(self) -> list[dict]:
        return [l for l in self.lines if l["kind"] == KIND_ITEM]

    def _source(self, source_id: str) -> dict:
        return next(l for l in self.order_lines if l["id"] == source_id)

    def _line(self, line_id: str) -> dict:
        return next(l for l in self.lines if l["id"] == line_id)

    @staticmethod
    def _date_score(line: dict, source: dict) -> int:
        date = str(line.get("date") or "")
        if not date:
            return 0
        if date == source.get("ship_date"):
            return 3
        if date == source.get("delivery_date"):
            return 2
        if date == source.get("order_date"):
            return 1
        return 0

    def _match_exact(self) -> None:
        """品番（またはJAN）・数量・単価が同じ行どうしを、日付の合うものから結ぶ。"""
        if not self.has_orders:
            return
        candidates: list[tuple] = []
        for i, line in enumerate(self._items()):
            ref = norm_code(line.get("order_ref"))
            for j, source in enumerate(self.order_lines):
                if not (same_item(line, source) and same_quantity(line, source)):
                    continue
                if source["unit_price"] and source["unit_price"] != line["unit_price"]:
                    continue
                score = self._date_score(line, source) * 10
                number = norm_code(source.get("order_number"))
                if ref and number and (ref in number or number in ref):
                    score += 100
                if source["state"] not in _NOT_BILLABLE:
                    score += 1  # 同点なら請求できる行を先に
                candidates.append((-score, i, j, line["id"], source["id"]))
        for _, _, _, line_id, source_id in sorted(candidates):
            if line_id in self.pairs or source_id in self.used:
                continue
            self.pairs[line_id] = [source_id]
            self.used[source_id] = line_id

    def _match_near(self) -> None:
        """同じ商品で、数量か単価だけが違う行（相手が1つに決まるときだけ）。"""
        for line in self._items():
            if line["id"] in self.pairs:
                continue
            free = [s for s in self.order_lines if s["id"] not in self.used and same_item(line, s)]
            if len(free) != 1:
                continue
            source = free[0]
            if same_quantity(line, source):
                what = "単価"
            elif not source["unit_price"] or source["unit_price"] == line["unit_price"]:
                what = "数量"
            else:
                continue
            self.pairs[line["id"]] = [source["id"]]
            self.used[source["id"]] = line["id"]
            self.mismatches[line["id"]] = {"source": source["id"], "what": what}

    def _find_duplicates(self) -> None:
        """資料の同じ行が既に別の請求行に使われているなら、二重計上。"""
        for line in self._items():
            if line["id"] in self.pairs:
                continue
            taken = next(
                (
                    s for s in self.order_lines
                    if s["id"] in self.used and same_item(line, s) and same_quantity(line, s)
                    and (not s["unit_price"] or s["unit_price"] == line["unit_price"])
                ),
                None,
            )
            if taken is not None:
                self.duplicates[line["id"]] = taken["id"]

    # --- モデルの対応付け（行をまとめた請求・請求しない理由の説明） ---

    def left_invoice_lines(self) -> list[dict]:
        return [l for l in self._items() if l["id"] not in self.pairs and l["id"] not in self.duplicates]

    def left_source_lines(self) -> list[dict]:
        return [s for s in self.order_lines if s["id"] not in self.used]

    def unexplained_source_lines(self) -> list[dict]:
        """請求していない資料の行のうち、消し線・入荷待ちなどコードで理由が分かるもの以外。"""
        return [s for s in self.left_source_lines() if s["state"] not in _NOT_BILLABLE]

    def needs_interpretation(self) -> bool:
        if not self.has_orders:
            return False
        return bool(self.left_invoice_lines() or self.unexplained_source_lines())

    def interpretation_payload(self) -> dict:
        def view(line: dict, keys: tuple[str, ...]) -> dict:
            return {k: line.get(k) for k in keys if line.get(k) not in (None, "")}

        return {
            "invoice_lines": [
                view(l, ("id", "invoice", "date", "code", "jan", "description", "quantity", "unit", "unit_price", "amount", "order_ref", "note"))
                for l in self.left_invoice_lines()
            ],
            "source_lines": [
                view(s, ("id", "file", "order_number", "order_date", "ship_date", "delivery_date", "code", "jan", "description", "quantity", "unit_price", "amount", "note"))
                for s in self.unexplained_source_lines()
            ],
            "files": [str(s.get("file") or "") for s in self.sources] + [str(u.get("file") or "") for u in self.unread],
        }

    def apply_interpretation(self, mapping: dict) -> None:
        """モデルの対応付けを、検算してから受ける（数量の合計が合わないものは受けない）。"""
        known_lines = {l["id"] for l in self.left_invoice_lines()}
        known_sources = {s["id"] for s in self.left_source_lines()}
        for pair in mapping.get("pairs") or []:
            line_id = str(pair.get("invoice_line_id") or "")
            source_ids = [str(s) for s in pair.get("source_line_ids") or []]
            if line_id not in known_lines or not source_ids:
                continue
            if any(s not in known_sources or s in self.used for s in source_ids) or len(set(source_ids)) != len(source_ids):
                continue
            line = self._line(line_id)
            total = sum(self._source(s)["quantity"] for s in source_ids)
            self.pairs[line_id] = source_ids
            for source_id in source_ids:
                self.used[source_id] = line_id
            self.mapped_by_model.add(line_id)
            if abs(total - line["quantity"]) > 1e-6:
                self.mismatches[line_id] = {"source": source_ids[0], "what": "数量", "total": total}
            else:
                priced = {self._source(s)["unit_price"] for s in source_ids if self._source(s)["unit_price"]}
                if priced and priced != {line["unit_price"]}:
                    self.mismatches[line_id] = {"source": source_ids[0], "what": "単価"}
        for item in mapping.get("non_order_lines") or []:
            line_id = str(item.get("invoice_line_id") or "")
            if line_id in known_lines and line_id not in self.pairs:
                self._line(line_id)["kind"] = KIND_FEE
                self.fee_basis[line_id] = str(item.get("basis") or "")
        for item in mapping.get("exemptions") or []:
            source_id = str(item.get("source_line_id") or "")
            reason = str(item.get("reason") or "").strip()
            if source_id in known_sources and source_id not in self.used and reason:
                self.exemptions[source_id] = reason

    # --- 規則 ---

    def finish(self) -> dict:
        """規則を当てて、取引先ぶんの結果を作る。"""
        issuers = list(self.settings.get("issuers") or [])
        per_invoice: list[dict] = []
        for invoice in self.invoices:
            name = str(invoice.get("file") or "")
            if not invoice.get("readable", True):
                self.findings.append(finding(CONFIRM, "unreadable", "請求書を読み取れなかった", "PDFの内容を読み取れませんでした。人の目で確認してください", name))
                per_invoice.append({"file": name, "requirements": [], "invoice_number": "", "total": 0})
                continue
            self.findings += check_arithmetic(invoice)
            rows, found = check_requirements(invoice, issuers)
            self.findings += found
            per_invoice.append({
                "file": name, "requirements": rows,
                "invoice_number": str(invoice.get("invoice_number") or ""),
                "issue_date": str(invoice.get("issue_date") or ""),
                "subtotal": to_int(invoice.get("subtotal")), "tax_total": to_int(invoice.get("tax_total")),
                "total": to_int(invoice.get("total")),
            })
        self._apply_shipments()
        self._check_duplicates()
        self._check_mismatches()
        self._check_unmatched()
        self._check_not_billable()
        self._check_shipping_evidence()
        self._check_cutoff()
        self._check_unattributed()
        self._check_dates()
        self._check_unbilled()
        self._check_figures()
        self._check_identity()
        self._check_sources()
        for item in per_invoice:
            own = [f for f in self.findings if f.get("invoice") in (item["file"], "")]
            item["verdict"] = worst(own)
        return {
            "name": self.name,
            "invoices": per_invoice,
            "lines": [self._line_result(l) for l in self.lines],
            "unbilled": [self._unbilled_result(s) for s in self.left_source_lines()],
            "orders": self.orders,
            "findings": self.findings,
            "unread": self.unread,
            "date_basis": self._date_basis(),
            "shipments": self._shipments_result(),
        }

    # --- 出荷管理表との突き合わせ ---

    def _table_in_use(self) -> bool:
        """出荷管理表に、この取引先の商品発送の行があるか（無ければ資料の記録だけで見る）。"""
        return bool(self.shipments) and self.has_orders

    def _line_keys(self, source: dict) -> tuple[set[str], list[str]]:
        """注文の行の品番と品名。対になった請求書の行の書き方も含める（注文書は相手の品番のことがある）。"""
        items = [source]
        if source["id"] in self.used:
            items.append(self._line(self.used[source["id"]]))
        codes: set[str] = set()
        names: list[str] = []
        for item in items:
            code = norm_code(item.get("code"))
            if code:
                codes.add(code)
            codes |= code_tokens(item.get("description"), item.get("note"))
            name = norm_code(item.get("description"))
            if len(name) >= MIN_NAME_CHARS:
                names.append(name)
        return codes, names

    @staticmethod
    def _same_goods(entry: dict, codes: set[str], names: list[str]) -> bool:
        """出荷管理表に書かれた商品（品番か品名）が、注文のその行のものか。"""
        code, name = norm_code(entry.get("code")), norm_code(entry.get("description"))
        if code and any(codes_related(code, c) for c in codes):
            return True
        return len(name) >= MIN_NAME_CHARS and any(name in n or n in name for n in names)

    def _task_lists(self, task: dict, source: dict) -> tuple[bool, bool]:
        """その行に、注文のこの行の商品が書かれているか。→ (書かれている, 数量も同じ)"""
        codes, names = self._line_keys(source)
        hit = any(codes_related(token, code) for token in task["tokens"] for code in codes)
        same_quantity_too = False
        for item in task["items"]:
            if self._same_goods(item, codes, names):
                hit = True
                same_quantity_too = same_quantity_too or (item["quantity"] and abs(item["quantity"] - source["quantity"]) < 1e-6)
        return hit, bool(same_quantity_too)

    @staticmethod
    def _ship_window(order: dict) -> tuple[str, str]:
        """注文書の出荷日・納品日の指定から、出荷の期日が入るはずの範囲。指定が無ければ空。"""
        ship, delivery = order.get("ship_date") or "", order.get("delivery_date") or ""
        if ship and delivery:
            return _shift(ship, -_WINDOW_BEFORE), delivery
        if ship:
            return _shift(ship, -_WINDOW_BEFORE), _shift(ship, _WINDOW_AFTER)
        if delivery:
            return _shift(delivery, -_WINDOW_BEFORE_DELIVERY), delivery
        return "", ""

    def _link_score(self, task: dict, order: dict) -> tuple[int, bool, bool]:
        """出荷管理表の1行が、その注文の出荷である見込み。→ (点数, 期日が注文書の指定に合う, 別の商品の行)

        日付の手がかり（依頼日が注文日の当日〜数日後／期日が注文書の出荷日・納品日の指定に合う）が
        無ければ 0。**品目が合うだけでは結ばない**——同じ商品の注文が毎週のように来るため
        （実例 2026-11-05: 9月分の確認で、10月の注文の出荷行を、同じ品番のある 9/18 の注文に結んだ）。
        品目は、日付の合う行どうしの順位に使う（合えば加点、別の商品が書かれていれば減点）。
        """
        score = 0
        gap = weekday_gap(order.get("order_date"), task.get("request_date"))
        if gap is not None and 0 <= gap < len(_REQUEST_SCORES):
            score += _REQUEST_SCORES[gap]
        low, high = self._ship_window(order)
        in_window = bool(low and task["date"] and low <= task["date"] <= high)
        if in_window:
            score += _WINDOW_SCORE + (_SHIP_DATE_SCORE if task["date"] == order.get("ship_date") else 0)
        if task["arrival_date"] and task["arrival_date"] == order.get("delivery_date"):
            score += _ARRIVAL_SCORE
        if not score:
            return 0, False, False
        listed = [self._task_lists(task, s) for s in self.order_lines if s["order"] == order["key"]]
        other_goods = bool(task["tokens"] or task["items"]) and not any(hit for hit, _ in listed)
        if other_goods:
            score -= _OTHER_ITEM_PENALTY
        elif any(hit for hit, _ in listed):
            score += _ITEM_SCORE + (_QUANTITY_SCORE if any(same for _, same in listed) else 0)
        return max(score, 0), in_window, other_goods

    def _ships_held_goods(self, task: dict, key: str, linked: list[dict]) -> bool:
        """その行が、注文に結んだ行で未発送とされた商品を発送しているか（在庫補充の後の発送）。"""
        for source in (s for s in self.order_lines if s["order"] == key):
            codes, names = self._line_keys(source)
            held = any(self._same_goods(u, codes, names) for t in linked for u in t["unshipped"])
            if held and self._task_lists(task, source)[0]:
                return True
        return False

    def _link_shipments(self) -> None:
        """出荷管理表の行（通常の出荷）を、注文に結ぶ。"""
        tasks = [t for t in self.shipments or [] if t["purpose"] == PURPOSE_NORMAL]
        scores: dict[tuple[int, str], tuple[int, bool, bool]] = {}
        for index, task in enumerate(tasks):
            for order in self.orders:
                found = self._link_score(task, order)
                if found[0] > 0:
                    scores[(index, order["key"])] = found
        links: dict[str, list[dict]] = {o["key"]: [] for o in self.orders}
        placed: set[int] = set()
        # 1. 見込みの高い順に、注文と行を1対1で結ぶ
        for (index, key), _ in sorted(scores.items(), key=lambda kv: (-kv[1][0], kv[0][0])):
            if index not in placed and not links[key]:
                links[key].append(tasks[index])
                placed.add(index)
        # 2. 残った行は、同じ注文を分けて出荷したと分かるものだけ足す: 先に結んだ行と同じ日の依頼／
        #    期日が注文書の指定に合う（どちらも、別の商品の行でないこと）／結んだ行で未発送とされた
        #    商品を発送している。依頼日が近いだけの行は足さない（資料に無い注文の出荷かもしれない）
        for index, task in enumerate(tasks):
            if index in placed:
                continue
            best: tuple[int, str] | None = None
            for order in self.orders:
                key = order["key"]
                score, in_window, other_goods = scores.get((index, key), (0, False, False))
                same_day = bool(score) and bool(links[key]) and links[key][0]["request_date"] == task["request_date"]
                split = (same_day or in_window) and not other_goods
                if (split or self._ships_held_goods(task, key, links[key])) and (best is None or score > best[0]):
                    best = (score, key)
            if best is not None:
                links[best[1]].append(task)
                placed.add(index)
        # 3. 行の付かなかった注文は、他の注文と一緒に出荷した見込みの行を借りる
        for order in self.orders:
            if links[order["key"]]:
                continue
            found = [
                (scores[(i, order["key"])][0], -i) for i in range(len(tasks))
                if (i, order["key"]) in scores and not scores[(i, order["key"])][2]
            ]
            if found:
                links[order["key"]].append(tasks[-max(found)[1]])
                self.ship_shared.add(order["key"])
        self.ship_links = links
        self.unattributed = [t for i, t in enumerate(tasks) if i not in placed]

    def _shipped_later(self, after: dict, source: dict, linked: list[dict]) -> dict | None:
        """その注文に結んだ後の行で、その商品を出荷しているもの（在庫補充の後の発送）。"""
        later = linked[linked.index(after) + 1:] if after in linked else []
        return next((t for t in later if task_ships(t) and self._task_lists(t, source)[0]), None)

    def _apply_shipments(self) -> None:
        """出荷管理表の記載を、注文の行の出荷状況に反映する。

        資料（出荷分の控え・消し線・入荷待ち管理表）で決まっている行は変えず、出荷不明の行だけを決める:
        未発送と書かれた商品 → 未出荷（後日の行で出荷していれば出荷済）／裏付けになる行がある → 出荷済／
        行はあるが裏付けにならない（未完了・何も発送していない・伝票番号なしでコメントあり）→ 出荷不明のまま。
        """
        if self.shipments is None or not self.has_orders:
            return
        if not self.shipments:
            self.findings.append(finding(
                NOTE, "no_ship_rows", "出荷管理表にこの取引先の行が無い",
                "出荷管理表の対象の期間に、この取引先の「商品発送」の行がありません。取引先名の書き方が違うか、"
                "表を通さない出荷の可能性があります。出荷の確認は、資料の記録だけで行いました", "",
                brief="出荷管理表にこの取引先の行が無く、出荷の確認は資料の記録だけで行いました",
            ))
            return
        self._link_shipments()
        conflicts: list[str] = []
        for order in self.orders:
            linked = self.ship_links.get(order["key"]) or []
            ships = [t for t in linked if task_ships(t)]
            doubts = [t for t in linked if not task_ships(t)]
            for source in (s for s in self.order_lines if s["order"] == order["key"]):
                codes, names = self._line_keys(source)
                held = next((t for t in linked if any(self._same_goods(u, codes, names) for u in t["unshipped"])), None)
                later = self._shipped_later(held, source, linked) if held is not None else None
                if held is not None and later is None:
                    quote = next(u["quote"] for u in held["unshipped"] if self._same_goods(u, codes, names))
                    said = f"出荷管理表 {held['label']}" + (f"「{_short(quote, MAX_COMMENT_CHARS)}」" if quote else "で未発送")
                    if source["state"] == OPEN:
                        source["state"], source["ship_date"], source["state_reason"] = NOT_SHIPPED, "", said
                    elif source["state"] == NOT_SHIPPED:
                        source["state_reason"] += f"。{said}"
                    elif source["state"] == SHIPPED:
                        conflicts.append(f"{order['order_number'] or order['files'][0]} {source['code'] or source['description']}（資料: {source['state_reason']}、{said}）")
                    continue
                if source["state"] != OPEN:
                    continue
                if later is not None:
                    source["state"], source["state_reason"] = SHIPPED, f"{task_evidence(later)}。{held['label']} では未発送"
                    source["table_ship_dates"], source["table_task"] = [later["date"]], later
                elif ships:
                    source["state"] = SHIPPED
                    source["state_reason"] = "、".join(task_evidence(t) for t in ships[:2]) + ("（他の注文と同じ出荷）" if order["key"] in self.ship_shared else "")
                    source["table_ship_dates"] = sorted(t["date"] for t in ships if t["date"])
                    source["table_task"] = min(ships, key=lambda t: t["date"] or "9999")
                elif any(t["nothing_shipped"] for t in doubts):
                    task = next(t for t in doubts if t["nothing_shipped"])
                    source["state"], source["ship_date"] = NOT_SHIPPED, ""
                    source["state_reason"] = f"出荷管理表 {task['label']}「{task['quote']}」"
            if doubts and not ships:
                self.ship_doubts[order["key"]] = [task_doubt(t) for t in doubts]
        if conflicts:
            self.findings.append(finding(
                CONFIRM, "ship_conflict", "資料では出荷済み、出荷管理表では未発送の記載",
                "、".join(conflicts[:6]) + "。どちらが正しいかを営業担当に確認してください", "",
            ))
        unread = [t for links in self.ship_links.values() for t in links if not t["read"] and str(t.get("team_comment") or "").strip()]
        if unread:
            listing = "、".join(f"{t['label']}「{_short(t.get('team_comment'), MAX_COMMENT_CHARS)}」" for t in unread[:6])
            self.findings.append(finding(
                CONFIRM, "ship_comment", "出荷管理表のコメント欄を読み取れなかった",
                f"納品チームコメントのある行: {listing}。未発送の商品が書かれていないか、人の目で確認してください", "",
            ))

    def _period(self, invoice: dict | None = None) -> tuple[str, str]:
        """請求の対象の期間（明細の日付の月の初日〜末日）。日付が無ければ空。"""
        lines = [l for l in self.lines if invoice is None or l["invoice"] == str(invoice.get("file") or "")]
        dates = sorted(str(l.get("date")) for l in lines if _to_date(l.get("date")))
        if not dates:
            return "", ""
        return dates[0][:8] + "01", month_end(dates[-1])

    def _check_cutoff(self) -> None:
        """請求した注文の出荷が、出荷管理表では請求の月より後になっていないか。"""
        for invoice in self.invoices:
            name = str(invoice.get("file") or "")
            _, end = self._period(invoice)
            if not end:
                continue
            late: dict[str, list[dict]] = {}
            evidence: dict[str, dict] = {}
            for line in (l for l in self.lines if l["invoice"] == name):
                for source in self._billed_sources(line):
                    dates = source.get("table_ship_dates") or []
                    if source["state"] == SHIPPED and dates and min(dates) > end:
                        late.setdefault(source["order"], []).append(line)
                        evidence.setdefault(source["order"], source["table_task"])
                        break
            parts = []
            for key, lines in late.items():
                order = next(o for o in self.orders if o["key"] == key)
                task = evidence[key]
                asked = f"。依頼者コメント「{_short(task.get('comment'), MAX_COMMENT_CHARS)}」" if task.get("partial") else ""
                parts.append(
                    f"注文 {order['order_number'] or order['files'][0]}（{len(lines)} 行・{yen(sum(l['amount'] for l in lines))}）は、"
                    f"{task_evidence(task)}の出荷です{asked}"
                )
            if parts:
                month = int(end[5:7])
                self.findings.append(finding(
                    CONFIRM, "cutoff", "翌月の出荷を当月分で請求している可能性",
                    "。".join(parts) + f"。{month}月中に出荷していなければ、{month}月分の請求から外してください（出荷した月の売上になります）",
                    name, sum(l["amount"] for lines in late.values() for l in lines),
                    [l["id"] for lines in late.values() for l in lines],
                ))

    def _loose_shipments(self) -> list[dict]:
        """どの注文とも結べなかった行のうち、依頼日も期日も請求の月に入っているもの。"""
        start, end = self._period()
        if not start:
            return []
        return [
            t for t in self.unattributed
            if start <= str(t.get("request_date") or "") <= end and start <= t["date"] <= end
        ]

    def _check_unattributed(self) -> None:
        """出荷管理表にあるのに、資料のどの注文とも結べない出荷（資料に無い注文＝請求漏れの可能性）。

        依頼日も期日も請求の月に入っている行だけを見る（月をまたぐ行は前後の月の請求のことがある）。
        """
        mode = str((self.settings.get("shipment_log") or {}).get("unattributed") or "confirm")
        found = self._loose_shipments()
        if mode == "off" or not found:
            return
        listing = "、".join(
            f"{t['label']}（依頼 {short_date(t.get('request_date'))}・期日 {short_date(t.get('due_date'))}、"
            f"{_short(t.get('tracking'), MAX_TRACKING_CHARS) or ('完了' if t.get('done') else '未完了')}）"
            for t in found[:8]
        )
        self.findings.append(finding(
            NOTE if mode == "note" else CONFIRM, "unattributed_shipment", "出荷管理表にあるのに、対応する注文書が資料に無い出荷",
            f"{len(found)} 件: {listing}。資料のどの注文の出荷か特定できません。入荷待ち分の後日の出荷か、"
            "資料に無い注文（請求漏れ）の可能性があります。営業担当に確認してください", "",
            brief=f"出荷管理表に、対応する注文書が資料に無い出荷が {len(found)} 件あります（{'、'.join(t['label'] for t in found[:8])}）",
        ))

    def _shipments_result(self) -> dict:
        """報告書に出す、出荷管理表との照合の結果。"""
        if self.shipments is None:
            return {"used": False, "rows": 0}
        tasks = self.shipments
        placed = {id(t) for links in self.ship_links.values() for t in links}
        return {
            "used": self._table_in_use(),
            "rows": len(tasks),
            "orders": [
                {
                    "order": o["order_number"] or o["files"][0], "shared": o["key"] in self.ship_shared,
                    "tasks": [task_view(t) for t in self.ship_links.get(o["key"]) or []],
                }
                for o in self.orders
            ],
            "unattributed": [task_view(t) for t in self._loose_shipments()],
            "others": [task_view(t) for t in tasks if t["purpose"] != PURPOSE_NORMAL and id(t) not in placed],
        }

    def _check_duplicates(self) -> None:
        by_invoice: dict[str, list[dict]] = {}
        for line in self.lines:
            if line["id"] in self.duplicates:
                by_invoice.setdefault(line["invoice"], []).append(line)
        for name, lines in by_invoice.items():
            total = sum(l["amount"] for l in lines)
            first_by_order: dict[str, list[dict]] = {}
            for line in lines:
                first_by_order.setdefault(self._source(self.duplicates[line["id"]])["order"], []).append(line)
            parts = []
            for key, group in first_by_order.items():
                order = next(o for o in self.orders if o["key"] == key)
                label = order["order_number"] or order["files"][0]
                files = "・".join(order["files"]) if order["copies"] > 1 else order["files"][0]
                dates = _dates(l.get("date") for l in group)
                firsts = _dates(self._line(self.used[self.duplicates[l["id"]]]).get("date") for l in group)
                parts.append(
                    f"注文 {label}（資料: {files}）の行が {firsts}付で請求済みなのに、{dates}付でも {len(group)} 行・{yen(sum(l['amount'] for l in group))} 計上されています"
                )
            self.findings.append(finding(
                FIX, "duplicate", "同じ注文の二重計上",
                "。".join(parts) + f"。重複は計 {len(lines)} 行・{yen(total)}（税抜）。同じ注文番号の注文書は同じ注文の控えです",
                name, total, [l["id"] for l in lines],
            ))

    def _check_mismatches(self) -> None:
        for line_id, info in self.mismatches.items():
            line, source = self._line(line_id), self._source(info["source"])
            if info["what"] == "単価":
                detail = f"{line.get('code') or line.get('description')}: 請求書 {yen(line['unit_price'])}、資料（{source['file']}）{yen(source['unit_price'])}"
                self.findings.append(finding(FIX, "unit_price", "単価が資料と違う", detail, line["invoice"], 0, [line_id]))
            else:
                quantity = info.get("total", source["quantity"])
                detail = f"{line.get('code') or line.get('description')}: 請求書 {line['quantity']:g}、資料（{source['file']}）{quantity:g}"
                self.findings.append(finding(FIX, "quantity", "数量が資料と違う", detail, line["invoice"], 0, [line_id]))

    def _check_unmatched(self) -> None:
        if not self.has_orders:
            return
        by_invoice: dict[str, list[dict]] = {}
        for line in self.left_invoice_lines():
            by_invoice.setdefault(line["invoice"], []).append(line)
        for name, lines in by_invoice.items():
            listing = "、".join(f"{short_date(l.get('date'))} {l.get('code') or l.get('description')} {l['quantity']:g}×{yen(l['unit_price'])}" for l in lines[:8])
            self.findings.append(finding(
                CONFIRM, "no_source", "資料に対応する注文が見当たらない行",
                f"{len(lines)} 行・{yen(sum(l['amount'] for l in lines))}: {listing}", name,
                sum(l["amount"] for l in lines), [l["id"] for l in lines],
            ))

    def _billed_sources(self, line: dict) -> list[dict]:
        return [self._source(s) for s in self.pairs.get(line["id"], [])]

    def _check_not_billable(self) -> None:
        """取消・入荷待ち・未出荷の行を請求していないか。"""
        groups: dict[tuple[str, str], list[dict]] = {}
        for line in self.lines:
            for source in self._billed_sources(line):
                if source["state"] in _NOT_BILLABLE:
                    groups.setdefault((line["invoice"], source["state"]), []).append(line)
                    break
        for (name, state), lines in groups.items():
            total = sum(l["amount"] for l in lines)
            listing = "、".join(f"{l.get('code') or l.get('description')} {l['quantity']:g}" for l in lines[:8])
            reasons = "・".join(sorted({self._billed_sources(l)[0]["state_reason"] for l in lines}))
            if state == NOT_SHIPPED:
                self.findings.append(finding(
                    CONFIRM, "not_shipped", "出荷した記録の無い行を請求している",
                    f"{len(lines)} 行・{yen(total)}（{listing}）は、出荷した記録がありません（{reasons}）。当月中に出荷したかを営業担当に確認し、未出荷なら外してください",
                    name, total, [l["id"] for l in lines],
                ))
            else:
                title = "入荷待ちの行を請求している" if state == BACKORDERED else "取り消された行を請求している"
                self.findings.append(finding(
                    FIX, "not_billable", title,
                    f"{len(lines)} 行・{yen(total)}（{listing}）は {reasons} のため、出荷するまで請求できません",
                    name, total, [l["id"] for l in lines],
                ))

    def _order_has_trouble(self, key: str) -> bool:
        """その注文に、取消・入荷待ち・未出荷・請求しない行があるか（在庫の影響が出ている注文）。"""
        return any(
            s["order"] == key and (s["state"] in _NOT_BILLABLE or s["id"] in self.exemptions)
            for s in self.order_lines
        )

    def _check_shipping_evidence(self) -> None:
        """請求した行に出荷の裏付けがあるか。"""
        mode = str(self.settings.get("shipping_evidence") or "risk")
        if mode == "off":
            return
        by_invoice: dict[str, dict[str, list[dict]]] = {}
        for line in self.lines:
            for source in self._billed_sources(line):
                if source["state"] == OPEN:
                    by_invoice.setdefault(line["invoice"], {}).setdefault(source["order"], []).append(line)
                    break
        for name, by_order in by_invoice.items():
            risky, plain, unproven = [], [], []
            for key, lines in by_order.items():
                order = next(o for o in self.orders if o["key"] == key)
                label = f"{order['order_number'] or order['files'][0]}（{len(lines)} 行・{yen(sum(l['amount'] for l in lines))}）"
                if self._table_in_use():
                    why = "、".join(self.ship_doubts.get(key) or []) or "出荷管理表に、この注文の出荷の行が見当たりません"
                    unproven.append(f"注文 {label}: {why}")
                else:
                    (risky if mode == "always" or self._order_has_trouble(key) else plain).append(label)
            if unproven:
                # 出荷管理表があれば、どの注文も出荷を確かめられる。確かめられない注文は発行前に聞く
                self.findings.append(finding(
                    CONFIRM, "shipping_evidence", "出荷管理表で出荷を確かめられない注文（発行前に出荷確認）",
                    "。".join(unproven) + "。出荷済みかを営業担当に確認し、未出荷なら外してください",
                    name, sum(l["amount"] for lines in by_order.values() for l in lines),
                    [l["id"] for lines in by_order.values() for l in lines],
                ))
            if risky:
                why = "同じ注文に品切・入荷待ち・取消の行があり、残りの行も在庫の影響を受けている可能性があります。" if mode != "always" else ""
                self.findings.append(finding(
                    CONFIRM, "shipping_evidence", "出荷の裏付けが無い行（発行前に出荷確認）",
                    f"注文 {'、'.join(risky)} は、資料が注文書だけで出荷の記録がありません。{why}出荷済みかを営業担当に確認してください",
                    name,
                ))
            if plain:
                self.findings.append(finding(
                    NOTE, "shipping_evidence", "出荷の記録が資料に無い注文",
                    f"注文 {'、'.join(plain)} は注文書だけで、送り状などの出荷記録がありません。予定どおり出荷済みなら問題ありません",
                    name, brief=f"出荷の記録が資料に無い注文が {len(plain)} 件あります。予定どおり出荷済みなら問題ありません",
                ))

    def _basis_of(self, line: dict) -> str:
        sources = self._billed_sources(line)
        if not sources or not line.get("date"):
            return ""
        source = sources[0]
        date = str(line.get("date"))
        if date == source.get("ship_date") and source.get("ship_date"):
            return "出荷日"
        if date == source.get("delivery_date") and source.get("delivery_date"):
            return "納品日"
        if date == source.get("order_date") and source.get("order_date"):
            return "注文日"
        if date == source.get("requested_ship_date") and source.get("requested_ship_date"):
            return "出荷日"
        if date in (source.get("table_ship_dates") or []):
            return "出荷日"  # 出荷管理表の期日（または実際の発送日）
        return "根拠なし" if any(source.get(k) for k in ("order_date", "ship_date", "delivery_date")) else ""

    def _check_dates(self) -> None:
        for line in self.lines:
            if self._basis_of(line) != "根拠なし" or line["id"] in self.duplicates:
                continue
            source = self._billed_sources(line)[0]
            known = "、".join(
                f"{label} {short_date(source.get(key))}"
                for label, key in (("注文日", "order_date"), ("出荷日", "ship_date"), ("出荷日", "requested_ship_date"), ("納品日", "delivery_date"))
                if source.get(key) and not (key == "requested_ship_date" and source.get("ship_date"))
            )
            if source.get("table_ship_dates"):
                known += "、出荷管理表の期日 " + "・".join(short_date(d) for d in source["table_ship_dates"])
            self.findings.append(finding(
                CONFIRM, "line_date_basis", "明細の日付の根拠が資料に無い",
                f"{short_date(line.get('date'))}付の「{line.get('code') or line.get('description')}」は、資料（{source['file']}）では {known} です。出荷日か納品日に直してください",
                line["invoice"], 0, [line["id"]],
            ))

    def _date_basis(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for line in self.lines:
            basis = self._basis_of(line)
            if basis and basis != "根拠なし" and line["id"] not in self.duplicates:
                counts[basis] = counts.get(basis, 0) + 1
        return counts

    def _check_unbilled(self) -> None:
        missing = [s for s in self.unexplained_source_lines() if s["id"] not in self.exemptions]
        if not missing:
            return
        listing = "、".join(
            f"{s['order_number'] or s['file']} {s.get('code') or s.get('description')} {s['quantity']:g}"
            + (f"×{yen(s['unit_price'])}" if s["unit_price"] else "")
            for s in missing[:10]
        )
        self.findings.append(finding(
            CONFIRM, "unbilled", "資料にあるのに請求していない行（請求漏れの可能性）",
            f"{len(missing)} 行: {listing}。品切・取消で請求しないのであれば、その旨を確認してください",
            "", sum(s["amount"] for s in missing), [s["id"] for s in missing],
        ))

    def _figures(self) -> list[dict]:
        out = []
        for source in self.sources:
            for figure in source.get("figures") or []:
                amount = to_int(figure.get("amount"))
                if amount:
                    out.append({"label": str(figure.get("label") or ""), "amount": amount, "file": str(source.get("file") or "")})
        return out

    def _check_figures(self) -> None:
        """注文書に基づかない行（販売分・相殺など）と、注記の金額を資料の金額と突き合わせる。"""
        figures = self._figures()
        if not figures:
            return
        amounts = {abs(f["amount"]) for f in figures}
        for line in self.lines:
            if line["id"] in self.pairs or line["id"] in self.duplicates or not line["amount"]:
                continue
            if self.has_orders and line["kind"] == KIND_ITEM:
                continue  # 注文書と突き合わせる行（対応なしは別の指摘で出す）
            if abs(line["amount"]) in amounts:
                hit = next(f for f in figures if abs(f["amount"]) == abs(line["amount"]))
                self.fee_basis.setdefault(line["id"], f"{hit['file']} の「{hit['label']}」{yen(hit['amount'])}")
            elif not self.has_orders:
                self.findings.append(finding(
                    CONFIRM, "no_figure", "資料に同じ金額が見当たらない行",
                    f"「{line.get('description')}」{yen(line['amount'])} の根拠になる金額が資料にありません",
                    line["invoice"], line["amount"], [line["id"]],
                ))
        known = amounts | {abs(l["amount"]) for l in self.lines} | {abs(s["amount"]) for s in self.order_lines}
        for invoice in self.invoices:
            known |= {abs(to_int(invoice.get(k))) for k in ("subtotal", "tax_total", "total")}
            for stated in invoice.get("stated_figures") or []:
                amount = abs(to_int(stated.get("amount")))
                if not amount or amount in known:
                    continue
                near = _nearest_figure(amount, figures)
                if near is not None:
                    self.findings.append(finding(
                        FIX, "stated_figure", "注記の金額が資料と違う",
                        f"「{stated.get('label')}」{yen(amount)} は、資料（{near['file']}）では「{near['label']}」{yen(near['amount'])} です",
                        str(invoice.get("file") or ""), near["amount"],
                    ))
                else:
                    self.findings.append(finding(
                        CONFIRM, "stated_figure", "注記の金額の根拠が資料に無い",
                        f"「{stated.get('label')}」{yen(amount)} と同じ金額が資料に見当たりません",
                        str(invoice.get("file") or ""),
                    ))

    def _check_identity(self) -> None:
        """品番は合うがJANが違う行（どちらかの誤記）。"""
        odd = []
        for line in self.lines:
            for source in self._billed_sources(line):
                jan_a, jan_b = norm_jan(line.get("jan")), norm_jan(source.get("jan"))
                if jan_a and jan_b and jan_a != jan_b and norm_code(line.get("code")) == norm_code(source.get("code")):
                    odd.append(f"{line.get('code')}（請求書 {jan_a}、{source['file']} {jan_b}）")
        if odd:
            self.findings.append(finding(NOTE, "jan", "品番は同じでJANコードが違う行", "、".join(odd[:6]) + "。どちらかの誤記の可能性があります", ""))

    def _check_sources(self) -> None:
        readable = [s for s in self.sources if s.get("readable", True)]
        if not readable:
            self.findings.append(finding(CONFIRM, "no_sources", "照合できる資料が無い", "作成元の資料を読めなかったため、請求内容の照合をしていません", ""))
        failed = [str(s.get("file") or "") for s in self.sources if not s.get("readable", True)]
        if failed:
            self.findings.append(finding(CONFIRM, "unreadable_source", "読み取れなかった資料がある", "、".join(failed) + " は内容を読み取れず、照合に使っていません", ""))

    # --- モデルの所見の取り込み ---

    def fee_lines(self) -> list[dict]:
        """注文書の行に基づかない請求（送料・名入代・相殺など）。根拠の確認が要る行。"""
        return [
            l for l in self.lines
            if l["id"] not in self.pairs and l["id"] not in self.duplicates
            and (l["kind"] != KIND_ITEM or not self.has_orders)
        ]

    def apply_review(self, result: dict, review: dict, fee_ok: str, fee_mismatch: str) -> None:
        """モデルの所見（付随費用の根拠・その他）を結果へ足し、判定を付け直す。

        根拠（evidence）の無い所見は受けない。付随費用は、コードでもモデルでも根拠が
        確かめられなかった行を「要確認」にする。
        **モデルの所見は「要確認」まで**（「要修正」はコードで裏付けが取れた指摘だけ）。
        実例（2026-11-05）: 営業担当の「①送料弊社負担の間違いです。修正お願いしたいです」を
        逆の意味に読み、契約どおり直っている請求書に「要修正」を出した。
        """
        files = {str(i.get("file") or "") for i in self.invoices}
        checks = {str(c.get("invoice_line_id") or ""): c for c in review.get("fee_checks") or []}
        for line in self.fee_lines():
            check = checks.get(line["id"])
            verdict, basis = (str(check.get("verdict") or ""), _one_line(check.get("basis"))) if check else ("", "")
            label = f"「{line.get('description')}」{yen(line['amount'])}"
            if verdict == fee_mismatch and basis:
                self.findings.append({**finding(CONFIRM, "fee", "付随費用が資料の条件と違う可能性", f"{label}: {basis}", line["invoice"], line["amount"], [line["id"]]), "by": "所見"})
            elif verdict == fee_ok and basis:
                self.fee_basis[line["id"]] = basis
            elif not self.fee_basis.get(line["id"]):
                self.findings.append(finding(
                    CONFIRM, "fee", "付随費用の根拠が資料に見当たらない",
                    f"{label} の根拠（契約条件・指示）を資料から確かめられませんでした" + (f"（{basis}）" if basis else ""),
                    line["invoice"], line["amount"], [line["id"]],
                ))
        for item in review.get("findings") or []:
            severity, evidence = str(item.get("severity") or ""), _one_line(item.get("evidence"))
            title, detail = _one_line(item.get("title")), _one_line(item.get("detail"))
            if severity not in SEVERITIES or not evidence or not title:
                continue
            if severity == FIX:
                severity = CONFIRM
            invoice = str(item.get("invoice") or "")
            self.findings.append({
                "severity": severity, "code": "review", "title": title,
                "detail": f"{detail}（根拠: {evidence}）", "invoice": invoice if invoice in files else "",
                "amount": 0, "lines": [], "by": "所見",
            })
        result["findings"] = self.findings
        result["lines"] = [self._line_result(l) for l in self.lines]
        for item in result["invoices"]:
            item["verdict"] = worst([f for f in self.findings if f.get("invoice") in (item["file"], "")])

    # --- 結果の行 ---

    def _line_result(self, line: dict) -> dict:
        line_id = line["id"]
        sources = self._billed_sources(line)
        if line_id in self.duplicates:
            source = self._source(self.duplicates[line_id])
            result, where = "二重計上", f"{source['order_number'] or source['file']}（請求済みの行と同じ）"
        elif line_id in self.mismatches:
            result, where = f"{self.mismatches[line_id]['what']}違い", self._where(sources)
        elif sources:
            state = sources[0]["state"]
            result = "一致" if state in (OPEN, SHIPPED) else f"一致（{state}）"
            if line_id in self.mapped_by_model:
                result += "・行をまとめて請求" if len(sources) > 1 else "・品名で対応付け"
            where = self._where(sources)
        elif line["kind"] != KIND_ITEM or not self.has_orders:
            result, where = ("根拠あり" if self.fee_basis.get(line_id) else "注文書以外"), self.fee_basis.get(line_id, "")
        else:
            result, where = "資料に対応なし", ""
        return {
            "id": line_id, "invoice": line["invoice"], "date": str(line.get("date") or ""),
            "code": str(line.get("code") or ""), "description": str(line.get("description") or line.get("note") or ""),
            "quantity": line["quantity"], "unit_price": line["unit_price"], "amount": line["amount"],
            "kind": line["kind"], "result": result, "source": where, "basis": self._basis_of(line),
        }

    def _where(self, sources: list[dict]) -> str:
        if not sources:
            return ""
        first = sources[0]
        label = first["order_number"] or first["file"]
        return f"{label}（{len(sources)} 行をまとめた請求）" if len(sources) > 1 else label

    def _unbilled_result(self, source: dict) -> dict:
        reason = self.exemptions.get(source["id"]) or (source["state_reason"] if source["state"] in _NOT_BILLABLE else "")
        return {
            "id": source["id"], "file": source["file"], "order_number": source["order_number"],
            "code": source["code"], "description": source["description"], "quantity": source["quantity"],
            "unit_price": source["unit_price"], "amount": source["amount"], "state": source["state"], "reason": reason,
        }


def _one_line(text: Any) -> str:
    """モデルの文を1行にする（報告書の箇条書きが崩れないように）。"""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _dates(values: Any) -> str:
    """日付を古い順に「10/9・10/13」の形で並べる。"""
    return "・".join(short_date(d) for d in sorted({str(v) for v in values if v}))


def _nearest_figure(amount: int, figures: list[dict]) -> dict | None:
    """打ち間違いと見られる近い金額（桁数が同じで1桁だけ違う、または差が2%以内）。"""
    text = str(amount)
    best = None
    for figure in figures:
        other = abs(figure["amount"])
        other_text = str(other)
        one_digit = len(text) == len(other_text) and sum(1 for a, b in zip(text, other_text) if a != b) == 1
        close = abs(other - amount) / max(amount, 1) <= 0.02
        if (one_digit or close) and (best is None or abs(other - amount) < abs(abs(best["amount"]) - amount)):
            best = figure
    return best
