"""発行請求書の確認（要件定義書 FR-15。段階1: 手動実行）。

請求書を作った担当者から管理者あてに来る「発行請求書の確認依頼」を、管理者の代わりに点検する。
確認事項は3本柱（2026-11-04 に確定）:
1. 請求内容が作成元資料（注文書・契約書・売上報告書・相殺管理表）と合っているか
2. 適格請求書の記載要件を満たしているか
3. 請求書・請求側に税法等の法的リスクが無いか

流れ: 読み取り（モデル。invoiceread）→ 突合と検算（コード。invoicematch）→ 所見（モデル）→
報告書と返信案（コード。invoicereport）。金額や判定をモデルに作文させない。
**承認はしない**（「問題なし」は確認の結果であり、発行の判断は管理者。ガードレール4）。

段階1は、資料を置いたフォルダを指定して手で回す:
    python -m raizuinu.invoicecheck --case <フォルダ>
フォルダの形:
    <フォルダ>/request.txt            依頼文（担当者の説明・営業の指示の引用）。無くてもよい
    <フォルダ>/<取引先>/invoices/…    確認する請求書（PDF）
    <フォルダ>/<取引先>/sources/…     作成元資料（下の階層も読む）
結果は <フォルダ>/_result/ に報告書（report.md）・返信案（reply.txt）・照合結果（result.json）。
Chatworkへの投稿、Box・Googleドライブからの取得、自動検知は段階2以降。
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .invoicematch import DOC_INVOICE, GroupCheck, VERDICT_OK
from .invoiceread import FEE_MISMATCH, FEE_OK, InvoiceReader
from .invoicereport import render_reply, render_report, summary_rows

JST = timezone(timedelta(hours=9))
RESULT_DIR = "_result"


def load_case(case_dir: Path) -> tuple[str, list[dict]]:
    """フォルダから、依頼文と取引先ごとのファイルを読む。→ (依頼文, 取引先)"""
    note_path = case_dir / "request.txt"
    note = note_path.read_text(encoding="utf-8-sig") if note_path.exists() else ""
    groups: list[dict] = []
    for folder in sorted(p for p in case_dir.iterdir() if p.is_dir() and not p.name.startswith("_")):
        invoices_dir, sources_dir = folder / "invoices", folder / "sources"
        if not invoices_dir.is_dir():
            continue
        invoices = [(p.name, p.read_bytes()) for p in sorted(invoices_dir.iterdir()) if p.is_file()]
        sources = []
        if sources_dir.is_dir():
            for path in sorted(p for p in sources_dir.rglob("*") if p.is_file()):
                sources.append((path.relative_to(sources_dir).as_posix(), path.read_bytes()))
        if invoices:
            groups.append({"name": folder.name, "invoices": invoices, "sources": sources})
    return note, groups


class InvoiceChecker:
    """依頼1件（取引先1社以上・請求書1通以上）の確認。"""

    def __init__(
        self,
        config: Any,
        reader: InvoiceReader | None = None,
        now: Callable[[], datetime] | None = None,
        cost: Any | None = None,
    ) -> None:
        self._config = config
        self._cost = cost
        self._reader = reader or InvoiceReader(config, cost=cost)
        self._now = now or (lambda: datetime.now(JST))

    @property
    def settings(self) -> dict:
        return dict(self._config.data.get("invoice_check") or {})

    def check(self, request_note: str, groups: list[dict], case: str = "", review: bool = True) -> dict:
        results = [self._check_group(request_note, group, review) for group in groups]
        usage = dict(self._reader.usage)
        cost_jpy = None
        if self._cost is not None:
            try:
                cost_jpy = float(self._cost.estimate_cost_jpy(usage))
            except Exception:
                cost_jpy = None
        result = {
            "checked_at": self._now().isoformat(),
            "case": case,
            "request_note": request_note,
            "groups": results,
            "calls": self._reader.calls,
            "cache_hits": self._reader.cache_hits,
            "usage": usage,
            "cost_jpy": cost_jpy,
        }
        self._audit(result)
        return result

    # --- 内部 ---

    def _check_group(self, note: str, group: dict, review: bool) -> dict:
        settings = self.settings
        invoice_names = {name for name, _ in group.get("invoices") or []}
        invoices = [self._reader.read_invoice(name, data) for name, data in group.get("invoices") or []]
        sources: list[dict] = []
        references: list[dict] = []
        unread: list[dict] = []
        limit = int(settings.get("max_source_files", 40))
        for index, (name, data) in enumerate(group.get("sources") or []):
            if Path(name).name in invoice_names:
                continue  # 確認している請求書そのものの控え
            if index >= limit:
                unread.append({"file": name, "reason": f"資料が {limit} 件を超えたため未読"})
                continue
            found = self._reader.read_source(name, data)
            if found.get("unread_reason"):
                unread.append({"file": name, "reason": str(found.get("unread_reason"))})
            elif found.get("doc_type") == DOC_INVOICE:
                references.append(found)  # 旧版の請求書など。突合には使わず、所見の参考にする
            else:
                sources.append(found)

        check = GroupCheck(str(group.get("name") or ""), invoices, sources, settings, unread)
        check.prepare()
        if check.needs_interpretation():
            payload = check.interpretation_payload()
            payload["files"] += [str(r.get("file") or "") for r in references]
            check.apply_interpretation(self._reader.interpret(note, payload))
        result = check.finish()
        result["sources"] = [
            {"file": s.get("file"), "doc_type": s.get("doc_type"), "title": s.get("title")}
            for s in sources + references
        ]
        if review:
            found = self._reader.review(self._review_payload(note, check, result, invoices, sources, references))
            check.apply_review(result, found, FEE_OK, FEE_MISMATCH)
        else:
            check.apply_review(result, {}, FEE_OK, FEE_MISMATCH)
        return result

    def _review_payload(self, note: str, check: GroupCheck, result: dict, invoices: list[dict], sources: list[dict], references: list[dict]) -> dict:
        lines_by_invoice: dict[str, list[dict]] = {}
        for line in result["lines"]:
            lines_by_invoice.setdefault(line["invoice"], []).append({
                k: line[k] for k in ("id", "date", "code", "description", "quantity", "unit_price", "amount", "result", "source")
            })
        notes = {l["id"]: str(l.get("note") or "") for l in check.lines}
        for lines in lines_by_invoice.values():
            for line in lines:
                if notes.get(line["id"]):
                    line["note"] = notes[line["id"]]
        return {
            "request_note": note.strip(),
            "checklist": list(self.settings.get("checklist") or []),
            "invoices": [
                {
                    "file": inv.get("file"), "invoice_number": inv.get("invoice_number"),
                    "issue_date": inv.get("issue_date"), "recipient_name": inv.get("recipient_name"),
                    "due_date": inv.get("due_date"), "total": inv.get("total"),
                    "remarks": inv.get("remarks") or [], "stated_figures": inv.get("stated_figures") or [],
                    "lines": lines_by_invoice.get(str(inv.get("file") or ""), []),
                }
                for inv in invoices
            ],
            "fee_lines": [
                {
                    "id": l["id"], "invoice": l["invoice"], "date": l.get("date"), "description": l.get("description"),
                    "quantity": l["quantity"], "unit_price": l["unit_price"], "amount": l["amount"],
                    "note": l.get("note") or "", "basis_found_by_code": check.fee_basis.get(l["id"], ""),
                }
                for l in check.fee_lines()
            ],
            "sources": [
                {
                    "file": s.get("file"), "doc_type": s.get("doc_type"), "title": s.get("title"),
                    "order_number": s.get("order_number"), "order_date": s.get("order_date"),
                    "ship_date": s.get("ship_date"), "delivery_date": s.get("delivery_date"),
                    "ship_to": s.get("ship_to"), "line_count": len(s.get("lines") or []),
                    "lines": [
                        {k: l.get(k) for k in ("code", "description", "quantity", "unit_price", "status") if l.get(k) not in (None, "", 0)}
                        for l in (s.get("lines") or [])[:30]
                    ],
                    "terms": s.get("terms") or [], "figures": (s.get("figures") or [])[:40], "notes": s.get("notes") or "",
                }
                for s in sources
            ],
            "reference_invoices": [{"file": r.get("file"), "notes": r.get("notes") or "", "terms": r.get("terms") or []} for r in references],
            "unread_files": [f"{u.get('file')}（{u.get('reason')}）" for u in check.unread],
            "unbilled": result["unbilled"],
            "code_findings": [
                {"severity": f["severity"], "title": f["title"], "detail": f["detail"], "invoice": f["invoice"]}
                for f in result["findings"]
            ],
        }

    def _audit(self, result: dict) -> None:
        """監査ログには件数と判定だけを残す（請求書・資料の本文は残さない）。"""
        try:
            from .audit import AuditLogger

            cfg = self._config
            rows = summary_rows(result)
            counts: dict[str, int] = {}
            for group in result["groups"]:
                for item in group.get("findings") or []:
                    counts[item["severity"]] = counts.get(item["severity"], 0) + 1
            AuditLogger(log_dir=cfg.resolve_path(cfg.audit_log_dir), retention_days=cfg.audit_log_retention_days).log({
                "type": "invoice_check",
                "case": result.get("case"),
                "invoices": len(rows),
                "verdicts": {str(r["invoice_number"] or r["file"]): r["verdict"] for r in rows},
                "findings": counts,
                "calls": result.get("calls"),
                "usage": result.get("usage"),
            })
        except Exception:
            print("[warn] 監査ログの記録に失敗: " + traceback.format_exc(), flush=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .config import Config
    from .cost import CostTracker

    parser = argparse.ArgumentParser(description="発行請求書の確認（段階1: フォルダを指定して手動で回す）")
    parser.add_argument("--case", required=True, help="依頼1件ぶんのフォルダ（取引先/invoices・sources）")
    parser.add_argument("--out", default="", help="結果の出力先（既定: <case>/_result）")
    parser.add_argument("--no-cache", action="store_true", help="読み取り結果の控えを使わず、読み直す")
    parser.add_argument("--no-review", action="store_true", help="モデルの所見を省く（突合と検算だけ）")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    config = Config.load()
    case_dir = Path(args.case)
    if not case_dir.is_dir():
        print(f"フォルダがありません: {case_dir}")
        return 2
    note, groups = load_case(case_dir)
    if not groups:
        print("確認する請求書がありません（<取引先>/invoices/ にPDFを置いてください）")
        return 2
    state_dir = config.resolve_path(config.state_dir)
    cost = CostTracker(
        state_dir=state_dir,
        limit_jpy=config.monthly_cost_limit_jpy,
        alert_threshold=config.cost_alert_threshold,
        usd_jpy_rate=config.usd_jpy_rate,
        pricing_usd_per_mtok=config.pricing_usd_per_mtok,
        prompt_cache_ttl=config.prompt_cache_ttl,
    )
    if cost.status().over_limit:
        print("今月の費用が上限に達しているため、実行しません")
        return 3
    cache_dir = None if args.no_cache else state_dir / "invoice_check" / "cache"
    reader = InvoiceReader(config, cost=cost, cache_dir=cache_dir)
    result = InvoiceChecker(config, reader=reader, cost=cost).check(note, groups, case=case_dir.name, review=not args.no_review)

    out_dir = Path(args.out) if args.out else case_dir / RESULT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(render_report(result), encoding="utf-8")
    (out_dir / "reply.txt").write_text(render_reply(result) + "\n", encoding="utf-8")
    (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    for row in summary_rows(result):
        print(f"{row['label']}\t{row['group']}\t{row['invoice_number']}\t{row['total']:,}円\t{row['points']}")
    need = sum(1 for r in summary_rows(result) if r["verdict"] != VERDICT_OK)
    cost_text = f"、費用の概算 {result['cost_jpy']:,.0f}円" if result.get("cost_jpy") is not None else ""
    print(f"修正・確認が必要: {need} 通／モデル呼び出し {result['calls']} 回（控えの再利用 {result['cache_hits']} 件）{cost_text}")
    print(f"結果: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
