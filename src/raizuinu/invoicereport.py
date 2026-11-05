"""発行請求書の確認結果を、報告書と返信案の文面にする。

判定表・金額・指摘の並びはすべて照合の結果（invoicematch）から組み立てる。
モデルが書くのは所見の文（detail）だけで、金額や判定をここで作り直さない。
"""

from __future__ import annotations

from typing import Any

from .invoicematch import CONFIRM, FIX, KIND_ADJUST, NOTE, VERDICT_OK, short_date, to_int, yen

_ORDER = {FIX: 0, CONFIRM: 1, NOTE: 2}
MAX_TABLE_LINES = 14


def _sorted(findings: list[dict]) -> list[dict]:
    return sorted(findings, key=lambda f: _ORDER.get(f.get("severity"), 9))


def _own(group: dict, file: str) -> list[dict]:
    """その請求書に関わる指摘（取引先全体の指摘を含む）。"""
    return [f for f in group.get("findings") or [] if f.get("invoice") in (file, "")]


def verdict_label(group: dict, invoice: dict) -> str:
    verdict = str(invoice.get("verdict") or VERDICT_OK)
    if verdict == VERDICT_OK and any(f.get("severity") == NOTE for f in _own(group, invoice["file"])):
        return f"{VERDICT_OK}（留意あり）"
    return verdict


def points(group: dict, invoice: dict) -> str:
    """判定表の「要点」。要修正・要確認の題を並べる。無ければ資料と一致。"""
    titles: list[str] = []
    for item in _sorted(_own(group, invoice["file"])):
        if item.get("severity") in (FIX, CONFIRM) and item.get("title") not in titles:
            titles.append(str(item.get("title")))
    return "、".join(titles) if titles else "資料と一致"


def summary_rows(result: dict) -> list[dict]:
    rows = []
    for group in result.get("groups") or []:
        for invoice in group.get("invoices") or []:
            rows.append({
                "group": group.get("name"), "file": invoice.get("file"),
                "invoice_number": invoice.get("invoice_number"), "total": invoice.get("total"),
                "verdict": invoice.get("verdict"), "label": verdict_label(group, invoice),
                "points": points(group, invoice),
            })
    return rows


def _cell(text: Any) -> str:
    return str(text if text is not None else "").replace("|", "／").replace("\n", " ")


def _line_table(lines: list[dict]) -> list[str]:
    shown = lines
    hidden = 0
    if len(lines) > MAX_TABLE_LINES:
        shown = [l for l in lines if not str(l.get("result", "")).startswith("一致") or "（" in str(l.get("result", ""))]
        hidden = len(lines) - len(shown)
    out = ["| 日付 | 内容 | 数量×単価 | 金額（税抜） | 資料 | 結果 |", "|---|---|---|---|---|---|"]
    for line in shown:
        what = " ".join(x for x in (line.get("code"), line.get("description")) if x)
        size = f"{line['quantity']:g}×{yen(line['unit_price'])}" if line.get("unit_price") else f"{line['quantity']:g}"
        out.append(
            f"| {_cell(short_date(line.get('date')))} | {_cell(what)} | {size} | {yen(line.get('amount'))} "
            f"| {_cell(line.get('source'))} | {_cell(line.get('result'))} |"
        )
    if hidden:
        out.append(f"\nほか {hidden} 行は資料と一致。")
    return out


def legal_notes(result: dict) -> list[str]:
    """税法・法的リスクの欄。結果に応じて足す。"""
    notes: list[str] = []
    findings = [f for g in result.get("groups") or [] for f in g.get("findings") or []]
    if any(f.get("code") in ("duplicate", "not_billable", "not_shipped", "shipping_evidence") and f.get("severity") in (FIX, CONFIRM) for f in findings):
        notes.append("過大請求・未出荷分の請求: そのまま発行すると、先方への過大請求と課税売上の早すぎる計上になります。発行前に修正・確認が必要です。")
    returned = 0
    for group in result.get("groups") or []:
        for line in group.get("lines") or []:
            if line.get("kind") == KIND_ADJUST and to_int(line.get("amount")) < 0:
                returned += abs(to_int(line.get("amount")))
    if returned:
        notes.append(
            f"対価の返還（相殺・値引き {yen(returned)}）: 適格返還請求書の扱いになります。請求書と1枚にまとめて相殺後の金額で表示する方法は、"
            f"継続して適用することが条件です。返還分の消費税額（10%なら ▲{yen(returned // 10)}）を併記すると先方の処理で迷いません。"
        )
    notes.append("印紙税: PDFでの発行のため不要です。")
    notes.append("電子帳簿保存法: 発行したPDFは電子取引データとして保存します（改ざん防止の措置と、取引年月日・金額・取引先で検索できる状態）。")
    notes.append("下請法・源泉徴収: 物品の販売で当社が受注側のため該当しません。")
    return notes


def render_report(result: dict) -> str:
    rows = summary_rows(result)
    out: list[str] = ["# 発行請求書の確認結果", ""]
    out.append(f"- 確認日: {str(result.get('checked_at') or '')[:10]}")
    if result.get("case"):
        out.append(f"- 対象: {result.get('case')}")
    out.append(f"- 請求書: {len(rows)} 通（取引先 {len(result.get('groups') or [])} 社）")
    out.append("- 最終承認: 足立さん（この報告は確認の結果であり、発行の判断ではありません）")
    out += ["", "## 結論", ""]
    need = [r for r in rows if r["verdict"] != VERDICT_OK]
    out.append(f"**{len(need)} 通に修正・確認が必要です。**" if need else "**修正の必要はありません。**")
    out += ["", "| 取引先 | 請求書番号 | 税込 | 判定 | 要点 |", "|---|---|---|---|---|"]
    for row in rows:
        out.append(f"| {_cell(row['group'])} | {_cell(row['invoice_number'])} | {yen(row['total'])} | {row['label']} | {_cell(row['points'])} |")

    for group in result.get("groups") or []:
        out += ["", f"## {group.get('name')}", ""]
        findings = _sorted(group.get("findings") or [])
        if findings:
            out.append("**指摘**")
            out.append("")
            for item in findings:
                target = ""
                if len(group.get("invoices") or []) > 1 and item.get("invoice"):
                    number = next((i.get("invoice_number") for i in group["invoices"] if i.get("file") == item.get("invoice")), "")
                    target = f"（{number or item.get('invoice')}）"
                by = "・所見" if item.get("by") == "所見" else ""
                out.append(f"- 【{item.get('severity')}{by}】{item.get('title')}{target}: {item.get('detail')}")
            out.append("")
        else:
            out += ["指摘はありません。", ""]
        for invoice in group.get("invoices") or []:
            lines = [l for l in group.get("lines") or [] if l.get("invoice") == invoice.get("file")]
            out.append(f"**{invoice.get('invoice_number') or invoice.get('file')}** "
                       f"税抜 {yen(invoice.get('subtotal'))}、消費税 {yen(invoice.get('tax_total'))}、税込 {yen(invoice.get('total'))}（{verdict_label(group, invoice)}）")
            out.append("")
            if lines:
                out += _line_table(lines)
                out.append("")
            requirements = invoice.get("requirements") or []
            if requirements:
                failed = [r for r in requirements if not r.get("ok")]
                if failed:
                    out.append("適格請求書の記載要件: " + "、".join(f"{r['requirement']}（{r['text']}）" for r in failed) + " に不備。")
                else:
                    out.append("適格請求書の記載要件: " + "、".join(r["requirement"] for r in requirements) + " はすべて具備。")
                out.append("")
        unbilled = group.get("unbilled") or []
        if unbilled:
            out.append("**請求していない資料の行**")
            out.append("")
            for item in unbilled:
                what = " ".join(x for x in (item.get("code"), item.get("description")) if x)
                reason = item.get("reason") or "理由の記載なし"
                out.append(f"- {item.get('order_number') or item.get('file')} {what} {item['quantity']:g}（{reason}）")
            out.append("")
        basis = group.get("date_basis") or {}
        if basis:
            out.append("明細の日付の基準: " + "、".join(f"{k} {v} 行" for k, v in basis.items()) + "。")
            out.append("")
        read = group.get("sources") or []
        if read:
            out.append("読んだ資料: " + "、".join(f"{s.get('file')}（{s.get('doc_type')}）" for s in read) + "。")
        unread = group.get("unread") or []
        if unread:
            out.append("読んでいない資料: " + "、".join(f"{u.get('file')}（{u.get('reason')}）" for u in unread) + "。")

    out += ["", "## 税法・法的リスク", ""]
    out += [f"- {note}" for note in legal_notes(result)]
    out += ["", "## 返信案", "", "```", render_reply(result), "```", ""]
    cost = result.get("cost_jpy")
    if cost is not None:
        out.append(f"（モデルの呼び出し {result.get('calls', 0)} 回、費用の概算 {cost:,.0f}円）")
    return "\n".join(out) + "\n"


def render_reply(result: dict, addressee: str = "坂田さん", reply_tag: str = "") -> str:
    """請求書を作った担当者への返信案。管理者の「送信」を得てから投稿する文面。"""
    rows = summary_rows(result)
    groups = result.get("groups") or []
    need = [r for r in rows if r["verdict"] != VERDICT_OK]
    fine = [r for r in rows if r["verdict"] == VERDICT_OK]
    names = "、".join(dict.fromkeys(str(g.get("name")) for g in groups))
    head = f"{reply_tag}{addressee}\n" if reply_tag else f"{addressee}\n"
    out = [head + f"請求書 {len(rows)} 通（{names}）を、作成元の資料と照合して確認しました。足立さんの確認を経てお戻しします。"]
    if not need:
        out.append("修正の必要はありません。このままで結構です。")
    else:
        line = f"{len(need)} 通に修正・確認をお願いします。"
        if fine:
            line += "、".join(dict.fromkeys(str(r["invoice_number"] or r["group"]) for r in fine)) + " は資料と一致しており、このままで結構です。"
        out.append(line)
    for group in groups:
        for invoice in group.get("invoices") or []:
            own = [f for f in _sorted(_own(group, invoice["file"])) if f.get("severity") in (FIX, CONFIRM)]
            if not own:
                continue
            label = group.get("name") if len(group.get("invoices") or []) == 1 else f"{group.get('name')} {invoice.get('invoice_number')}"
            out.append("")
            out.append(f"■{label}（{invoice.get('verdict')}）")
            for item in own:
                out.append(f"・{item.get('title')}: {item.get('detail')}")
    notes = []
    for group in groups:
        for item in group.get("findings") or []:
            if item.get("severity") == NOTE:
                notes.append(f"・{group.get('name')}: {item.get('title')}。{item.get('detail')}")
    if notes:
        out.append("")
        out.append("■参考（修正不要）")
        out += notes[:4]
    if need:
        out.append("")
        out.append("修正版をいただければ再確認します。")
    return "\n".join(out)
