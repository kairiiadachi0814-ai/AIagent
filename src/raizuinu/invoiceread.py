"""発行請求書の確認で、モデルに任せる部分（読み取り・対応付け・所見）。

役割の分担（要件定義書 FR-15）:
- 読み取り: 請求書と作成元資料を、照合しやすい決まった形に書き出す（read_invoice / read_source）
- 対応付け: コードの突合で残った行だけを見て、行をまとめた請求・付随費用・請求しない理由の
  説明を読む（interpret）。結果はコード側が数量を検算してから受ける
- 所見: コードで決められない点（契約条件どおりか、注記の内容、指示の反映）だけを書く（review）

金額の計算と判定はここでは行わない（invoicematch）。同じファイルを読み直さないよう、
読み取り結果はファイルの内容の指紋で控える（cache_dir）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import traceback
from pathlib import Path
from typing import Any

from .invoicematch import BACKORDER_STATUSES, DOC_TYPES, LINE_STATUSES, SEVERITIES

PROMPT_VERSION = "1"

FEE_OK, FEE_MISMATCH, FEE_UNKNOWN = "根拠どおり", "資料と不一致", "根拠が見当たらない"

_PDF_TYPES = (".pdf",)
_SHEET_TYPES = (".xlsx", ".xlsm")
_TEXT_TYPES = (".txt", ".csv", ".tsv", ".md")
_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def _obj(properties: dict, description: str = "") -> dict:
    schema = {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    if description:
        schema["description"] = description
    return schema


def _arr(items: dict, description: str = "") -> dict:
    schema = {"type": "array", "items": items}
    if description:
        schema["description"] = description
    return schema


def _s(description: str = "") -> dict:
    return {"type": "string", "description": description} if description else {"type": "string"}


def _n(description: str = "") -> dict:
    return {"type": "number", "description": description} if description else {"type": "number"}


def _b(description: str = "") -> dict:
    return {"type": "boolean", "description": description} if description else {"type": "boolean"}


_INVOICE_LINE = _obj({
    "date": _s("取引年月日 YYYY-MM-DD。無ければ空"),
    "code": _s("品番。無ければ空"),
    "jan": _s("JANコード。無ければ空"),
    "description": _s("品名・内容（品番とJANを除く）"),
    "quantity": _n(),
    "unit": _s(),
    "unit_price": _n("単価（税抜）。円"),
    "tax_rate": _n("税率。10 や 8"),
    "amount": _n("金額（税抜）。円。マイナスは負の数"),
    "order_ref": _s("行に書かれた注文番号・管理番号。無ければ空"),
    "note": _s("行のすぐ下に書かれた補足（納品先・送料負担など）。無ければ空"),
})

INVOICE_SCHEMA: dict[str, Any] = _obj({
    "readable": _b("内容を読み取れたか"),
    "issuer_name": _s("発行者の社名"),
    "registration_number": _s("登録番号（T＋13桁）"),
    "recipient_name": _s("宛名（部署まで。御中・様は除く）"),
    "issue_date": _s("発行日 YYYY-MM-DD"),
    "invoice_number": _s("請求書番号"),
    "due_date": _s("振込期日 YYYY-MM-DD。無ければ空"),
    "has_detail_sheet": _b("別紙の請求明細書が付いているか"),
    "detail_invoice_number": _s("請求明細書に書かれた請求書番号。別紙が無ければ空"),
    "detail_registration_number": _s("請求明細書に書かれた登録番号。別紙が無ければ空"),
    "lines": _arr(_INVOICE_LINE, "請求の明細行。別紙の請求明細書があればその行を全部、無ければ請求書本体の行"),
    "cover_lines": _arr(
        _obj({"description": _s(), "quantity": _n(), "unit_price": _n(), "tax_rate": _n(), "amount": _n()}),
        "別紙明細があるときの、請求書本体側の行（商品代金 一式 など）。別紙が無ければ空",
    ),
    "subtotal": _n("小計（税抜）"),
    "tax_total": _n("消費税の合計"),
    "total": _n("合計（税込）"),
    "tax_breakdown": _arr(_obj({"rate": _n("税率"), "base": _n("対象額（税抜）"), "tax": _n("消費税額")}), "税率ごとの欄"),
    "reduced_rate_note": _b("軽減税率対象を示す記載（※は軽減税率対象 など）があるか"),
    "remarks": _arr(_s(), "備考欄と行の注記の文"),
    "stated_figures": _arr(
        _obj({"label": _s("その金額が何か"), "amount": _n()}),
        "備考や注記の文中に書かれた金額（残高・相殺額・過請求額など）。明細の金額欄・合計欄の金額は入れない",
    ),
})

SOURCE_SCHEMA: dict[str, Any] = _obj({
    "readable": _b(),
    "doc_type": {"type": "string", "enum": DOC_TYPES},
    "title": _s("文書の表題"),
    "counterparty": _s("文書の発行者（取引先）"),
    "order_number": _s("注文番号・管理No.。無ければ空"),
    "order_date": _s("注文日（文書の日付）YYYY-MM-DD"),
    "ship_date": _s("出荷日の指定、または出荷した日 YYYY-MM-DD。無ければ空"),
    "delivery_date": _s("納品期日・着日指定 YYYY-MM-DD。無ければ空"),
    "ship_to": _s("納品先・送付先。無ければ空"),
    "shipping_evidence": _s("送り状番号など、実際に出荷したことが分かる記載。無ければ空"),
    "is_shipment_copy": _b("出荷した行だけを残して他を消した「出荷分の控え」か"),
    "lines": _arr(_obj({
        "code": _s(), "jan": _s(), "description": _s(),
        "quantity": _n(), "unit_price": _n("書かれていなければ 0"), "amount": _n("書かれていなければ 0"),
        "status": {"type": "string", "enum": LINE_STATUSES},
        "note": _s(),
    }), "注文書の行。売上報告書・管理表・契約書では空"),
    "subtotal": _n(), "tax": _n(), "total": _n(),
    "terms": _arr(_s(), "請求額の根拠になる条件（単価・名入れ代・送料の条件など）を1条件ずつ"),
    "figures": _arr(_obj({"label": _s(), "amount": _n()}), "合計・残高・相殺額など、行以外の金額"),
    "backorders": _arr(_obj({
        "order_number": _s(), "code": _s(), "jan": _s(), "description": _s(),
        "quantity": _n(), "amount": _n(),
        "status": {"type": "string", "enum": BACKORDER_STATUSES},
        "ship_date": _s(),
    }), "欠品・入荷待ち管理表の行。それ以外の文書では空"),
    "notes": _s("書き込み・注意書き・ファイル名から分かること"),
})

INTERPRET_SCHEMA: dict[str, Any] = _obj({
    "pairs": _arr(_obj({
        "invoice_line_id": _s(),
        "source_line_ids": _arr(_s()),
        "reason": _s(),
    })),
    "non_order_lines": _arr(_obj({
        "invoice_line_id": _s(),
        "kind": _s("送料・名入代・手数料 など"),
        "basis": _s("根拠の記載。無ければ空"),
    })),
    "exemptions": _arr(_obj({
        "source_line_id": _s(),
        "reason": _s("請求しない理由。根拠の文言を引用"),
    })),
})

REVIEW_SCHEMA: dict[str, Any] = _obj({
    "fee_checks": _arr(_obj({
        "invoice_line_id": _s(),
        "verdict": {"type": "string", "enum": [FEE_OK, FEE_MISMATCH, FEE_UNKNOWN]},
        "basis": _s("根拠の資料名と記載"),
    })),
    "findings": _arr(_obj({
        "severity": {"type": "string", "enum": SEVERITIES},
        "invoice": _s("対象の請求書のファイル名。取引先全体なら空"),
        "title": _s(),
        "detail": _s(),
        "evidence": _s("根拠の資料名と該当の記載"),
    })),
})

_COMMON = """あなたは株式会社ライズクリエイション経理財務部のアシスタントです。
当社が取引先へ発行する請求書を、発行前に作成元の資料と照合する作業の一部を担当します。
- 文書に書かれていることだけを使う。書かれていないことを推測で補わない
- 金額は円の整数（カンマや¥を付けない）。日付は YYYY-MM-DD（和暦は西暦に。令和8年は2026年）
- 読み取れない値は空文字か 0 にする。それらしい値を作らない
"""

INVOICE_SYSTEM = _COMMON + """
今回は、当社が発行する請求書のPDFを読み、内容を決まった形に書き出します。判断や要約はせず、
書かれているとおりに写します。
- lines は請求の明細行。別紙の「請求明細書」があればその行を全部（1行も省かず、同じ内容の行が
  続いていてもまとめない）、無ければ請求書本体の行
- 行のすぐ下に納品先・送料負担などの補足だけが書かれた行は、独立した行にせず、直前の行の note に入れる
- cover_lines は、別紙明細があるときの請求書本体側の行（「商品代金 一式」など）。別紙が無ければ空
- code は品番（R0560-A など）、jan は13桁のJANコード、description は品名。品番が無ければ空
- 年の無い日付は、発行日から見て自然な年を補う
- stated_figures は、備考や行の注記の文中に書かれた金額だけ（「残高10,069円」など）。
  数量・単価・金額の欄や小計・合計の欄の数字は入れない
"""

SOURCE_SYSTEM = _COMMON + """
今回は、請求書の作成元資料（注文書・発注書、契約書、売上報告書、管理表など）を読み、内容を
決まった形に書き出します。ファイル名にも意味がある（①②の番号、「発送分」「請求無し」「返信」など）。
- doc_type:
  注文書 … 相手が当社に商品を注文している文書（発注書・直送依頼書・注文伝票を含む）
  契約書・取引条件 … 商品契約書、単価表、送料の条件
  売上報告書／相殺・残高の管理表／欠品・入荷待ちの管理表／出荷記録
  請求書 … 当社が発行した請求書（旧版や控えを含む）
- 注文書は lines に全行を書く。単価・金額が書かれていなければ 0
- 行に手書きや赤い線で消し線が引かれていれば status は「消し線」、枠や丸で囲まれていれば「囲み」、
  それ以外は「通常」。大きな×や斜線で複数の行がまとめて消されていれば、その範囲の行はすべて「消し線」
  （枠で囲まれた行は消されていない）
- is_shipment_copy は、出荷した行だけを残して他を線で消すなどして「その日に出荷した分」を示した控えなら
  true（ファイル名に「発送分」「出荷分」とあるもの）。そのとき ship_date は出荷した日
  （ファイル名の 261009 は 2026-10-09）。消し線が「取消・欠品」を表すだけの注文書は false
- shipping_evidence は、送り状番号・運送会社名の書き込みなど、実際に出荷したことが分かる記載。
  出荷日の指定や納期の希望は出荷の証拠ではない
- order_date は文書の日付（注文日）、ship_date は出荷日の指定、delivery_date は納品期日・着日指定
- 契約書・取引条件は terms に、請求額の根拠になる条件（商品名と単価、数量ごとの名入れ代、
  送料の条件、元払の条件など）を原文に近い短い文で1条件ずつ
- 売上報告書・管理表は行ごとの明細を書かず、figures に合計・残高・相殺額などの金額を、
  何の金額か分かるラベル（月や項目名）を付けて書く
- 欠品・入荷待ちの管理表は backorders に行ごと
- 請求書（旧版・控え）は lines を空にし、notes に請求書番号・発行日・合計と備考の要点
- notes には、書き込み・注意書き・ファイル名から分かること（「再FAX。納期の訂正」など）を短く
"""

INTERPRET_SYSTEM = _COMMON + """
請求書の行と注文書の行をコードで突き合わせた結果、対応が付かなかった行が渡されます。
依頼文（請求書を作った担当者の説明と、営業担当の指示の引用）と資料のファイル名も渡されます。
次の3つを書き出してください。
1. pairs: 請求書の1行が、注文書の複数の行をまとめたもの（色違いの5行を1行にした、など）や、
   品番の書き方が違うだけの同じ商品であれば、その対応。source_line_ids の数量の合計が請求書の
   数量と一致するものだけ。確信が持てない対応は書かない
2. non_order_lines: 注文書の行に対応しない請求（送料・名入代・手数料など）。basis には、
   依頼文や資料名に根拠の記載があればその文言
3. exemptions: 請求していない注文書の行のうち、依頼文やファイル名に「品切」「請求無し」「入荷待ち」
   「キャンセル」などの説明があるもの。reason にその根拠の文言を引用する。説明が見当たらない行は
   書かない（請求漏れとして人が確認する）
依頼文で「同じ商品」と説明されている品番の違い（枝番の有無など）は、その説明に従う。
"""

REVIEW_SYSTEM = _COMMON + """
コードによる照合（行の突合・検算・適格請求書の記載要件）は済んでいます。あなたは、コードでは
判断できない点だけを確認し、所見を書きます。
確認する点:
- 送料・名入代など注文書の行以外の請求（fee_lines）が、契約書・取引条件・依頼文・資料名の記載
  どおりか。行ごとに fee_checks へ（資料名に条件が書かれていることがある。読めなかった資料の
  名前も渡される）
- 注文書に単価が無い取引先で、単価が契約書・取引条件どおりか
- 備考・注記の内容が、資料や依頼文と食い違っていないか
- 依頼文にある営業担当の指示（修正の依頼など）が請求書に反映されているか
- 確認事項の一覧（checklist）にあって、上に無い点
守ること:
- コードが既に出した指摘（code_findings）と同じことは書かない
- evidence に、根拠の資料名と該当の記載を必ず書く。根拠を示せないことは書かない
- 金額を自分で計算し直して別の数字を作らない。資料と請求書に書かれた数字だけを使う
- severity: 資料の記載と請求書が明確に食い違う → 要修正。資料からは確かめられない・根拠が
  見当たらない → 要確認。問題は無いが知らせる価値がある → 留意
- 営業担当が「このままでよい」と答えている点は、問題にしない
- 問題が無ければ findings は空でよい。良い点の列挙や一般論は書かない
"""


def file_blocks(name: str, data: bytes, settings: dict) -> tuple[list[dict] | None, str]:
    """ファイルを、モデルに渡す形にする。→ (content の部品, 読めない理由)"""
    suffix = Path(name).suffix.lower()
    if not data:
        return None, "中身が空（大きすぎて取得していない資料など）"
    max_mb = float(settings.get("max_file_mb", 20))
    if len(data) > max_mb * 1024 * 1024:
        return None, f"{max_mb:.0f}MB を超えている"
    if suffix in _PDF_TYPES:
        if not data.startswith(b"%PDF"):
            return None, "PDFとして読めない"
        return [{
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(data).decode("ascii")},
            "title": name,
        }], ""
    if suffix in _SHEET_TYPES:
        try:
            from .sheettext import xlsx_to_text

            text = xlsx_to_text(data)
        except Exception:
            return None, "Excelとして読めない"
        return [{"type": "text", "text": f"Excelファイル「{name}」の内容（タブ区切り）:\n{text}"}], ""
    if suffix in _TEXT_TYPES:
        text = data.decode("utf-8-sig", errors="replace")[: int(settings.get("max_text_chars", 60000))]
        return [{"type": "text", "text": f"ファイル「{name}」の内容:\n{text}"}], ""
    if suffix in _IMAGE_TYPES:
        return [{
            "type": "image",
            "source": {"type": "base64", "media_type": _IMAGE_TYPES[suffix], "data": base64.standard_b64encode(data).decode("ascii")},
        }], ""
    return None, f"対応していない形式（{suffix or '拡張子なし'}）"


class InvoiceReader:
    """モデルの呼び出し（読み取り・対応付け・所見）。使った量は usage に積む。"""

    def __init__(self, config: Any, client: Any | None = None, cost: Any | None = None, cache_dir: Path | None = None) -> None:
        self._config = config
        self._client = client
        self._cost = cost
        self._cache_dir = cache_dir
        self.usage: dict[str, int] = {}
        self.calls = 0
        self.cache_hits = 0

    @property
    def settings(self) -> dict:
        return dict(self._config.data.get("invoice_check") or {})

    # --- 公開API ---

    def read_invoice(self, name: str, data: bytes) -> dict:
        found = self._read("invoice", name, data, INVOICE_SYSTEM, INVOICE_SCHEMA,
                           f"この請求書（{name}）の内容を書き出してください。")
        found.setdefault("readable", False)
        found["file"] = name
        return found

    def read_source(self, name: str, data: bytes) -> dict:
        found = self._read("source", name, data, SOURCE_SYSTEM, SOURCE_SCHEMA,
                           f"この資料（ファイル名: {name}）の内容を書き出してください。")
        found.setdefault("readable", False)
        found["file"] = name
        return found

    def interpret(self, request_note: str, payload: dict) -> dict:
        text = (
            "依頼文:\n" + (request_note.strip() or "（なし）")
            + "\n\n対応が付かなかった行と資料名:\n" + json.dumps(payload, ensure_ascii=False, indent=1)
        )
        return self._ask("interpret", INTERPRET_SYSTEM, INTERPRET_SCHEMA, [{"type": "text", "text": text}], text) or {}

    def review(self, payload: dict) -> dict:
        text = "照合の結果と資料:\n" + json.dumps(payload, ensure_ascii=False, indent=1)
        return self._ask("review", REVIEW_SYSTEM, REVIEW_SCHEMA, [{"type": "text", "text": text}], text) or {}

    # --- 内部 ---

    def _read(self, kind: str, name: str, data: bytes, system: str, schema: dict, ask: str) -> dict:
        blocks, reason = file_blocks(name, data, self.settings)
        if blocks is None:
            return {"readable": False, "unread_reason": reason}
        key = hashlib.sha1(data).hexdigest() + "|" + name
        found = self._ask(kind, system, schema, blocks + [{"type": "text", "text": ask}], key)
        return found if found is not None else {"readable": False, "unread_reason": "読み取りに失敗"}

    def _cache_path(self, kind: str, key: str) -> Path | None:
        if self._cache_dir is None:
            return None
        model = str(self.settings.get("model") or self._config.model)
        digest = hashlib.sha1(f"{PROMPT_VERSION}|{model}|{kind}|{key}".encode("utf-8")).hexdigest()
        return self._cache_dir / f"{kind}-{digest}.json"

    def _ask(self, kind: str, system: str, schema: dict, content: list[dict], cache_key: str) -> dict | None:
        path = self._cache_path(kind, cache_key)
        if path is not None and path.exists():
            try:
                self.cache_hits += 1
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        from .answer import _call_with_continuation

        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        cfg, settings = self._config, self.settings
        kwargs = {
            "model": str(settings.get("model") or cfg.model),
            "max_tokens": int(settings.get("max_tokens") or cfg.max_tokens),
            "output_config": {
                "effort": str(settings.get("effort") or "medium"),
                "format": {"type": "json_schema", "schema": schema},
            },
            "system": system,
        }
        try:
            response, usage, _ = _call_with_continuation(
                self._client.messages.create, kwargs, [{"role": "user", "content": content}]
            )
        except Exception:
            print(f"[warn] 請求書確認のモデル呼び出しに失敗（{kind}）: " + traceback.format_exc(), flush=True)
            return None
        self.calls += 1
        for name, value in (usage or {}).items():
            self.usage[name] = self.usage.get(name, 0) + int(value)
        if self._cost is not None and usage:
            try:
                self._cost.add_usage(usage)
            except Exception:
                print("[warn] コスト計上に失敗: " + traceback.format_exc(), flush=True)
        text = next((b.text for b in getattr(response, "content", []) if getattr(b, "type", "") == "text"), "")
        try:
            found = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            print(f"[warn] 請求書確認の読み取り結果を解釈できません（{kind}。stop={getattr(response, 'stop_reason', '')}）", flush=True)
            return None
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
            except OSError:
                print("[warn] 読み取り結果の控えを書けません: " + traceback.format_exc(), flush=True)
        return found
