"""ひな形から書類を作る（書類送付状・FAX送付状）。

Boxの「⑨各種書類ひな形データ」にある書類を、依頼文の内容で埋めて
Chatworkへ添付で返す。ひな形の実体は templates/ に白紙化して同梱してある
（VPSからBoxは見えないため。作り方は tools/build_templates.py）。

方針:
- 埋めるのは依頼文に書かれた内容だけ。宛先・書類名を推測で作らない
  （足りなければ作らずに、何が足りないかを聞き返す）
- 出来上がりは「下書き」。送付・提出の実行は人が行う（要件定義書 版0.13）
- どのひな形を使ったかを返信に必ず書く（出典の明示）
- 契約書ひな形は対象外。ひな形リンク集のURL案内に倒す（LegalForceでの
  リーガルチェックが必須のため）
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .templatefill import TemplateError, render_docx, render_xlsx

JST = timezone(timedelta(hours=9))

# 書類の型。会社ごとではなく「書式の型」で持つ。差出人（会社名・住所・TEL・部署）は
# templates/companies.json から差し込むので、グループ会社が増えても増やさない
LAYOUTS: dict[str, str] = {
    "標準": "送付状_標準.docx",
    "楽天軒型": "送付状_楽天軒型.docx",
}
SOUFUJO_BOX_URL = "https://app.box.com/file/1529686391255"
FAX_TEMPLATE = "FAX送付状.xlsx"
FAX_SHEET = "汎用"
FAX_BOX_URL = "https://app.box.com/file/1529688805714"
# 返信でひな形名として見せる呼び方（Box上のファイル名に合わせる）
DOC_LABELS = {"送付状": "書類送付状", "FAX送付状": "FAX送付状"}

# FAX送付状「汎用」シートの差し込み先。敬称は会社名と別の欄（H10）にある
FAX_CELLS = {
    "to_company": "D10",
    "honorific": "H10",
    "to_person": "D12",
    "to_fax": "D14",
    "to_tel": "D16",
    "subject": "E19",
    "pages": "N6",
    "sender_staff": "O12",
}
FAX_BODY_ROWS = ("B23", "B24", "B25", "B26", "B27")
# FAX送付状の発信者欄。会社が変わればここも変わる（担当者名は O12）
FAX_SENDER_CELLS = {
    "company": "M10",
    "dept": "M12",
    "fax": "M14",
    "tel": "M16",
}
_HONORIFIC_RE = re.compile(r"[\s　]*(御中|様|殿)$")

# 契約書ひな形は生成しない（締結前のリーガルチェックが必須のため案内に倒す）
CONTRACT_GUIDANCE = (
    "契約書のひな形は、こちらで中身を埋めて作ることはしていません。"
    "条項の当てはめを誤ると締結後に効いてくるためです。\n"
    "ひな形リンク集の該当ファイルをBoxから開いてご利用ください。"
    "作成後は、締結前にLegalForceでのリーガルチェックをお願いします。"
)

# 「作る」を指す語だけを見る。「ください」「お願いします」は丁寧語であって
# 作成の指示ではない（それらを含めると送付状に触れた質問がすべて吸われる）
_BUILD_VERB_RE = re.compile(
    r"(作成|作って|作り|作れ|つくって|つくり|用意し|準備し|起こして|仕上げて|"
    r"発行し|出力し|埋めて)"
)
_DOC_NAME_RE = re.compile(
    r"(送付状|送信状|送り状|添え状|添付状|送付票|カバーレター|そえじょう|そうふじょう)"
)
# 「書き方」「どこにある」等はQ&Aで答える（作成依頼ではない）。
# 「手順」「ルール」のような、作成依頼の文中でも自然に出る語は入れない
_EXCLUDE_RE = re.compile(
    r"(書き方|作り方|やり方|どこにあ|どこです|どこ[？?]|場所は|保管|"
    r"とは何|の意味|どんな時|どういう時|違いは|必要ですか|誰が作)"
)
# 既にある文書を読ませる依頼（要約・議事録化など）。添付があるならそちらが優先
_READ_TASK_RE = re.compile(r"(要約|議事録|まとめて|整理して|抽出|読んで|内容を確認|チェックして)")
_ATTACHMENT_RE = re.compile(r"\[download:\d+\]")
# 「送付状お願いします」のような依頼語。ただし疑問形なら質問として扱う
_REQUEST_RE = re.compile(r"(お願い|ください|下さい|ほしい|欲しい|頼み)")
_QUESTION_RE = re.compile(r"(ですか|でしょうか|ますか|ますでしょ|[？?]\s*$)")
# 作った側が使ってはいけない書き出し（依頼する側の言い方）。
# 「よろしくお願いいたします。」だけを返すと、依頼を受けた返事として噛み合わない
_ASKING_OPENING_RE = re.compile(
    r"^(?:[^。\n]*(?:よろしく|宜しく)お願い|お願いいたします|お願いします|"
    r"ご対応(?:のほど)?|恐れ入りますが|お手数ですが)"
)
DEFAULT_OPENING = "承知しました。下書きを作成しました。"
_CONTRACT_RE = re.compile(r"(契約書|覚書|念書|誓約書|NDA|秘密保持)")


def _fold(text: str) -> str:
    """社名を突き合わせるための正規化（全角/半角・大小文字・空白のゆれを吸収）。

    「ＲＡＫＵＴＥＮＫＥＮ」と「RAKUTENKEN」、「ohirome」と「OHIROME」を
    同じものとして扱う。
    """
    return unicodedata.normalize("NFKC", text).casefold().replace(" ", "").replace("　", "")


def _box_url(kind: str, company: dict[str, Any]) -> str:
    """返信に載せる出典。送付状は会社ごとに原本が分かれているので台帳を優先する。"""
    if kind == "送付状":
        return str(company.get("box_url") or SOUFUJO_BOX_URL)
    return FAX_BOX_URL


def looks_like_document_build_request(question: str, body: str = "") -> bool:
    """「送付状を作って」のような書類作成依頼かどうか。

    書類名（送付状・FAX送付状 等）と「作る」を指す語が両方あり、
    「書き方を教えて」のような質問でないときだけ真。
    添付ファイルつきで「要約して」のように読み取りを頼まれた場合は、
    こちらではなく文書タスク（議事録・要約）で扱う。
    """
    if not _DOC_NAME_RE.search(question):
        return False
    if _EXCLUDE_RE.search(question):
        return False
    if _READ_TASK_RE.search(question) and _ATTACHMENT_RE.search(body or question):
        return False
    if _BUILD_VERB_RE.search(question):
        return True
    # 「送付状お願いします」は作成依頼。「送付状って要りますか？」は質問
    return bool(_REQUEST_RE.search(question)) and not _QUESTION_RE.search(question)


def wants_contract_template(question: str) -> bool:
    """契約書ひな形そのものの作成依頼か（送付状の同封物としての言及は除く）。"""
    if not _CONTRACT_RE.search(question):
        return False
    return not _DOC_NAME_RE.search(question)


class DocumentBuildError(Exception):
    """書類の作成に失敗した（利用者向けの文面をそのまま持つ）。

    失敗しても、すでに消費したトークンはコストに計上する必要があるため
    usage を持ち回る。
    """

    def __init__(self, message: str, usage: dict[str, int] | None = None) -> None:
        super().__init__(message)
        self.usage = usage or {}


SYSTEM_PROMPT = """あなたは株式会社ライズクリエイション経理財務部のアシスタント「{agent_name}」です。
社内のひな形を使って書類の下書きを作るため、依頼文から必要な項目を抜き出します。

厳守すること:
- 依頼文（と会話の流れ）に書かれていることだけを使う。宛先・会社名・書類名・
  部数・FAX番号を推測で作らない
- 宛先や送付書類が分からないときは、その項目を空にして missing に何が足りないかを書く
- 敬称は依頼文の書き方に従う。会社あてなら「御中」、個人あてなら「様」を付ける
- 日付の指定がなければ today をそのまま使う
- 数字・固有名詞は依頼文のまま写す（丸めたり言い換えたりしない）

返信の書き出し（opening）は、**依頼を受けて書類を作り終えた側の言葉**にすること。
依頼を短く受け止め、作ったことを伝える1〜2文にする。例:
  「エムズステップ　南様あての送付状ですね。倉庫寄託契約書1通で作成しました。」
  「承知しました。大塚商会あての送付状、下記のとおり作成しました。」
**「よろしくお願いいたします」「お願いします」「ご対応ください」「ご確認をお願いします」の
ような、依頼する側・お願いする側の言い方で書き出してはならない**（依頼したのは相手であり、
作ったのはこちらのため、会話として噛み合わなくなる）。

宛先は「会社名」「支店名」「部署名」「担当者名」に分けて抜き出す。
組み立てはこちらで行うので、敬称（御中・様）は付けず、余計な語も足さないこと。例:
  「大塚商会 大阪南支店 販売２課の濵野康一様あて」
  → to_company="大塚商会" / to_branch="大阪南支店" / to_department="販売２課" / to_person="濵野 康一"
会社名は㈱・㈲・(株)などの略記を正式名称（株式会社・有限会社）に直す。
ただし依頼文に法人格が書かれていなければ足さない（推測で法人格を決めない）。

送付書類（items）は依頼文にある物だけを並べる。部数の指定がない物は qty を空文字にする
（こちらで「1部」と仮置きし、その旨を利用者に伝える）。
"""

_FIELDS_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["送付状", "FAX送付状", ""],
            "description": "郵送なら送付状、FAXならFAX送付状。判断できないときは空文字",
        },
        "company_id": {
            "type": "string",
            "description": "差出人の会社ID（渡した一覧から選ぶ）。判断できないときは空文字",
        },
        "sender_hint": {
            "type": "string",
            "description": (
                "依頼文で差出人として名指しされた会社名をそのまま写す"
                "（例:「ヤマトライジング名で」→「ヤマトライジング」）。"
                "差出人の指定がなければ空文字。宛先の会社名は入れない"
            ),
        },
        "date": {"type": "string", "description": "書類の日付。例: 2026年8月17日"},
        "to_company": {
            "type": "string",
            "description": (
                "宛先の会社名（団体名）だけ。㈱・㈲・(株)などの略記は正式名称に直す。"
                "依頼文に法人格が書かれていなければ足さない。支店名・部署名・敬称は含めない。"
                "個人あてで会社名が無ければ空"
            ),
        },
        "to_branch": {
            "type": "string",
            "description": "支店・営業所・工場・センター名。無ければ空",
        },
        "to_department": {
            "type": "string",
            "description": "部署名・課名。無ければ空",
        },
        "to_person": {
            "type": "string",
            "description": "担当者名。敬称（様・殿）は付けない。書かれていなければ空",
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "qty": {"type": "string", "description": "例: 1部 / 2枚 / 一式"},
                },
                "required": ["name", "qty"],
                "additionalProperties": False,
            },
            "description": "送付する書類。書類送付状のときに使う",
        },
        "subject": {"type": "string", "description": "FAX送付状の件名"},
        "pages": {"type": "integer", "description": "FAXの送信枚数（送付状を含む）"},
        "to_fax": {"type": "string"},
        "to_tel": {"type": "string"},
        "body_lines": {
            "type": "array",
            "items": {"type": "string"},
            "description": "FAX送付状の本文（1行ずつ）",
        },
        "missing": {
            "type": "array",
            "items": {"type": "string"},
            "description": "作成に足りない項目。例: 宛先の会社名 / 送付する書類",
        },
        "opening": {
            "type": "string",
            "description": (
                "作り終えたことを伝える書き出し。1〜2文。"
                "「よろしくお願いいたします」等の依頼する側の言い方は禁止"
            ),
        },
    },
    "required": [
        "kind",
        "company_id",
        "sender_hint",
        "date",
        "to_company",
        "to_branch",
        "to_department",
        "to_person",
        "items",
        "missing",
        "opening",
    ],
    "additionalProperties": False,
}


class DocBuildRunner:
    """依頼文からひな形を選び、項目を埋めた書類のバイト列を作る。"""

    def __init__(self, config: Config, client: Any | None = None) -> None:
        self._config = config
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client

    # --- 公開API ---

    def run(
        self,
        instruction: str,
        context: str = "",
        requester_name: str = "",
    ) -> tuple[str, dict[str, Any], dict[str, int]]:
        """→ (返信本文, 監査用メタ, usage)。

        メタに artifact（(ファイル名, バイト列)）が入っていれば添付して返す。
        """
        meta: dict[str, Any] = {}
        if wants_contract_template(instruction):
            meta["error"] = "contract_template"
            return CONTRACT_GUIDANCE, meta, {}

        fields, usage = self._extract(instruction, context, requester_name)
        _normalize(fields)  # 空白だけの宛先・品名を捨ててから不足判定にかける
        # 書類の担当者は、原則として依頼してきた人の苗字。代理で作るときのために
        # 「担当は岩永で」のような名指しがあればそちらを使う。名指しの判定は
        # コードで行う（モデルに拾わせると宛先側の担当者名が差出人欄へ回るため）
        roster = self._roster()
        mine = surname(requester_name, roster)
        named = _staff_from_text(instruction, roster)
        if named and any(named in str(line) for line in fields.get("to_lines") or []):
            named = ""  # 宛先に出てくる名前は先方の担当者。差出人にはしない
        fields["staff"] = named or mine
        # 依頼者本人以外の名前で作るときは、黙って通さず返信で伝える
        meta["staff_override"] = named if named and named != mine else ""
        meta["fields"] = {k: v for k, v in fields.items() if k != "opening"}

        kind = str(fields.get("kind") or "")
        hint = str(fields.get("sender_hint") or "").strip()
        if any(_fold(hint) == _fold(member) for member in roster):
            # 「岩永名義で」は担当者の名指し。会社の名指しと同じ言い方になるため、
            # 社名として扱うと未登録の会社を聞き返してしまう
            hint = ""
        company = self._find_company(str(fields.get("company_id") or ""), hint)
        if company is None and not hint:
            # 差出人の指定がない依頼は、既定の会社（ライズクリエイション）で作る
            company = next((c for c in self._companies() if c.get("default")), None)
        if company is None:
            # 名指しされた会社を知らないまま既定の会社で作ると、差出人が別会社の
            # 書類が出来上がってしまう。作らずに聞き返す
            meta["error"] = "unknown_company"
            return self._unknown_company(fields.get("opening", ""), hint), meta, usage
        if kind not in ("送付状", "FAX送付状"):
            meta["error"] = "template_not_found"
            return (
                self._choose_guidance(fields.get("opening", ""), fields.get("missing") or []),
                meta,
                usage,
            )

        meta["template"] = f"{kind}（{company['name']}）"
        meta["company"] = company["id"]
        meta["company_name"] = company["name"]
        missing = self._missing(kind, fields)
        if missing:
            meta["error"] = "missing_fields"
            return self._ask_for(fields.get("opening", ""), missing), meta, usage

        try:
            filename, data = self._render(kind, company, fields)
        except TemplateError as exc:
            raise DocumentBuildError(
                "すみません、ひな形への差し込みでつまずいてしまいました。"
                f"（{exc}）お手数ですが、もう一度ご依頼いただけますか。",
                usage,
            ) from exc
        meta["artifact"] = (filename, data)
        meta["output_filename"] = filename
        reply = self._reply(fields, kind, company, meta.get("staff_override", ""))
        return reply, meta, usage

    # --- 内部 ---

    def _companies(self) -> list[dict[str, Any]]:
        """差出人になれるグループ会社の一覧（templates/companies.json）。"""
        path = self._templates_dir() / "companies.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [c for c in data.get("companies", []) if c.get("id") and c.get("name")]

    def _find_company(self, company_id: str, hint: str) -> dict[str, Any] | None:
        """差出人として名指しされた社名から会社を引く。

        依頼文に書かれていた社名（hint）を先に見る。company_id はモデルの
        解釈であり、「ライズ名義で」をライズホールディングスと取り違えても
        こちらからは分からないため、依頼文に根拠がある側を優先する。

        「ライズ」が「ライズホールディングス」にも含まれるように、社名は互いに
        重なる。先に見つかったものではなく、いちばん強く一致したものを採る
        （完全一致 > 長く一致したほう）。
        """
        companies = self._companies()
        target = _fold(hint)
        if not target:
            # 差出人の名指しがない依頼。文脈から拾った company_id があれば使う
            for company in companies:
                if company_id and company_id == company["id"]:
                    return company
            return None
        best: dict[str, Any] | None = None
        best_score = 0
        for company in companies:
            for name in (company["name"], company["id"], *(company.get("aliases") or [])):
                folded = _fold(str(name))
                if not folded:
                    continue
                if folded == target:
                    score = 1000 + len(folded)
                elif folded in target:
                    score = len(folded)  # 依頼文のうち社名で説明できた長さ
                elif target in folded:
                    score = len(target)
                else:
                    continue
                if score > best_score:
                    best, best_score = company, score
        return best

    def _read_template(self, filename: str) -> bytes:
        """ひな形を読む。無いときは黙って落ちずに、作成失敗として扱う。"""
        try:
            return (self._templates_dir() / filename).read_bytes()
        except OSError as exc:
            raise TemplateError(f"ひな形 {filename} を読み込めませんでした") from exc

    def _templates_dir(self) -> Path:
        return self._config.resolve_path(
            self._config.doc_build.get("templates_dir", "templates")
        )

    def _extract(
        self, instruction: str, context: str, requester_name: str
    ) -> tuple[dict[str, Any], dict[str, int]]:
        from .answer import _call_with_continuation

        cfg = self._config
        now = datetime.now(JST)
        today = f"{now.year}年{now.month}月{now.day}日"

        companies = self._companies()
        catalog = "\n".join(
            "- {id}: {name}{alias}{mark}{note}".format(
                id=c["id"],
                name=c["name"],
                alias=(
                    "（略称: " + "・".join(c.get("aliases") or []) + "）"
                    if c.get("aliases")
                    else ""
                ),
                mark="　※指定がないときはこの会社" if c.get("default") else "",
                note=f"　※{c['note']}" if c.get("note") else "",
            )
            for c in companies
        )
        parts = [
            f"今日の日付: {today}",
            "書類の種類（kind）: 郵送に添えるなら「送付状」、FAXで送るなら「FAX送付状」",
            "差出人になれる会社（company_id はこの一覧のIDから選ぶ。"
            "依頼文に「○○名義で」「○○として」とあればその会社。"
            "宛先の会社名と取り違えないこと）:\n" + catalog,
        ]
        if requester_name:
            # 担当者名はこちらで確定させるので抜き出させない。ただし依頼文の
            # 読み取り（「私あてに」等）に効くので、誰からの依頼かは伝える
            parts.append(f"依頼者の姓: {surname(requester_name, self._roster())}")
        directory = self._fax_directory()
        if directory:
            parts.append(
                "FAX送付状の宛先台帳（会社名が一致するときはFAX番号・TEL・担当者を使ってよい。"
                "一致しないときは使わない）:\n" + directory
            )
        if context:
            parts.append(f"直近の会話:\n{context[:3000]}")
        parts.append(f"===依頼ここから===\n{instruction}\n===依頼ここまで===")

        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": int(cfg.max_tokens),
            "output_config": {
                "effort": cfg.effort,
                "format": {"type": "json_schema", "schema": _FIELDS_SCHEMA},
            },
            "system": SYSTEM_PROMPT.format(agent_name=cfg.agent_name),
        }
        messages = [{"role": "user", "content": "\n\n".join(parts)}]
        response, usage, _ = _call_with_continuation(
            self._client.messages.create, kwargs, messages
        )
        text = next(
            (b.text for b in getattr(response, "content", []) if getattr(b, "type", "") == "text"),
            "",
        )
        try:
            fields = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise DocumentBuildError(
                "すみません、依頼の内容をうまく読み取れませんでした。"
                "宛先と送付する書類を書き添えて、もう一度お願いできますか。",
                usage,
            ) from exc
        if not fields.get("date"):
            fields["date"] = today
        return fields, usage

    def _roster(self) -> tuple[str, ...]:
        """社内の担当者名簿（FAX送付状ひな形のプルダウンから作ったもの）。"""
        path = self._templates_dir() / "staff_roster.json"
        if not path.exists():
            return ()
        try:
            return tuple(json.loads(path.read_text(encoding="utf-8")).get("staff") or [])
        except (OSError, json.JSONDecodeError):
            return ()

    def _fax_directory(self) -> str:
        path = self._templates_dir() / "fax_destinations.json"
        if not path.exists():
            return ""
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        lines = []
        for entry in entries:
            bits = [entry.get("company", "")]
            for key in ("person", "fax", "tel"):
                if entry.get(key):
                    bits.append(f"{key}={entry[key]}")
            lines.append("- " + " / ".join(b for b in bits if b))
        return "\n".join(lines)

    @staticmethod
    def _missing(kind: str, fields: dict[str, Any]) -> list[str]:
        missing = [str(m) for m in fields.get("missing") or []]
        if not fields.get("to_lines"):
            missing.append("宛先")
        if not str(fields.get("staff") or "").strip():
            # 依頼者の表示名が拾えなかったとき。担当者欄が空の書類を黙って
            # 出さずに聞き返す（Chatworkの会話履歴の取得に失敗した場合など）
            missing.append("差出人の担当者名（依頼者の苗字）")
        if kind == "送付状" and not fields.get("items"):
            missing.append("送付する書類")
        if kind == "FAX送付状" and not str(fields.get("subject") or "").strip():
            missing.append("件名")
        seen: dict[str, None] = {}
        for item in missing:
            seen.setdefault(item.strip(), None)
        return [m for m in seen if m]

    def _render(
        self, kind: str, company: dict[str, Any], fields: dict[str, Any]
    ) -> tuple[str, bytes]:
        to_lines = [str(line) for line in fields.get("to_lines") or [] if str(line).strip()]
        date = str(fields.get("date") or "")

        if kind == "送付状":
            layout = str(company.get("layout") or "標準")
            if layout not in LAYOUTS:
                raise TemplateError(f"未対応の書式です（{layout}）")
            data = self._read_template(LAYOUTS[layout])
            max_items = int(self._config.doc_build.get("max_items", 20))
            items = (fields.get("items") or [])[:max_items]
            address2 = str(company.get("address2") or "")
            tel = str(company.get("tel") or "")
            out = render_docx(
                data,
                {
                    "date": date,
                    "staff": str(fields.get("staff") or ""),
                    "sender_company": str(company.get("name") or ""),
                    "sender_address1": str(company.get("address1") or ""),
                    "sender_dept": str(company.get("dept") or ""),
                },
                {
                    # 2行目（支店・部署・担当者）は1字下げて会社名にぶら下げる
                    "to_line": [
                        {"to_line": line if index == 0 else "　" + line}
                        for index, line in enumerate(to_lines)
                    ],
                    "item_name": [
                        {
                            "item_name": str(item.get("name", "")),
                            "item_qty": str(item.get("qty", "") or "1部"),
                        }
                        for item in items
                    ],
                    # 住所が1行で足りる会社・電話番号を持たない会社では、その行ごと消す
                    # （空欄のまま残すと「TEL：」だけの行が印字されてしまう）
                    "sender_address2": [{"sender_address2": address2}] if address2 else [],
                    "sender_tel": [{"sender_tel": tel}] if tel else [],
                },
            )
        else:
            data = self._read_template(FAX_TEMPLATE)
            # 「汎用」シートは会社名と敬称が別の欄。敬称込みで会社名を入れると
            # 「○○ 御中 御中」になるため、末尾の敬称を切り離す
            to_company, honorific = _split_honorific(to_lines[0] if to_lines else "")
            cells: dict[str, Any] = {
                FAX_CELLS["to_company"]: to_company,
                FAX_CELLS["honorific"]: honorific,
                FAX_CELLS["to_person"]: "　".join(to_lines[1:]),
                FAX_CELLS["to_fax"]: str(fields.get("to_fax") or ""),
                FAX_CELLS["to_tel"]: str(fields.get("to_tel") or ""),
                FAX_CELLS["subject"]: str(fields.get("subject") or ""),
                FAX_CELLS["sender_staff"]: str(fields.get("staff") or ""),
                # 発信者欄は差出人の会社ごとに入れ替える（部署名の末尾空白は落とす）
                FAX_SENDER_CELLS["company"]: str(company.get("name") or ""),
                FAX_SENDER_CELLS["dept"]: str(company.get("dept") or "").strip("　 "),
                FAX_SENDER_CELLS["fax"]: str(company.get("fax") or ""),
                FAX_SENDER_CELLS["tel"]: str(company.get("tel") or ""),
            }
            pages = fields.get("pages")
            if isinstance(pages, int) and pages > 0:
                cells[FAX_CELLS["pages"]] = pages
            for ref, line in zip(FAX_BODY_ROWS, fields.get("body_lines") or []):
                cells[ref] = str(line)
            if date and _ymd(date) != datetime.now(JST).strftime("%Y%m%d"):
                # 今日以外の日付を指定されたときだけ =TODAY() を上書きする
                # （「2026/8/17」のような表記ゆれで無用に潰さないよう年月日で比べる）
                cells["E6"] = date
            out = render_xlsx(data, FAX_SHEET, cells)

        suffix = ".docx" if kind == "送付状" else ".xlsx"
        label = _split_honorific(to_lines[0])[0] if to_lines else "宛先未定"
        name = f"{DOC_LABELS[kind]}_{_safe_name(label)}_{_ymd(date)}{suffix}"
        return name, out

    def _reply(
        self,
        fields: dict[str, Any],
        kind: str,
        company: dict[str, Any],
        staff_override: str = "",
    ) -> str:
        opening = _delivering_opening(fields.get("opening"))
        assumed = [
            item["name"] for item in fields.get("items") or [] if not item.get("qty")
        ]
        note = ""
        if staff_override:
            # 依頼者本人以外の名前で作った箇所は、黙って通さず必ず伝える
            note = f"担当者は「{staff_override}」で作成しています。\n"
        if assumed:
            # 推測で埋めた箇所は黙って通さず、必ず伝える
            note += "部数の指定がなかった「" + "」「".join(assumed) + "」は1部としています。\n"
        return (
            f"{opening}\n\n"
            f"{note}"
            f"差出人: {company['name']}\n"
            f"ひな形: {DOC_LABELS[kind]}\n"
            f"出典: {_box_url(kind, company)}\n"
            "※下書きです。日付・宛名・部数をご確認のうえ、印刷や送付はお手元でお願いします。"
        )

    @staticmethod
    def _ask_for(opening: str, missing: list[str]) -> str:
        lead = _asking_opening(opening, "書類の下書き、お作りしますね。")
        items = "\n".join(f"・{m}" for m in missing)
        return (
            f"{lead}\n\n"
            "作るのにこれだけ足りませんでした。教えていただけますか。\n"
            f"{items}"
        )

    @staticmethod
    def _choose_guidance(opening: str, missing: list[str]) -> str:
        lead = _asking_opening(opening, "送付状ですね、お作りします。")
        text = (
            f"{lead}\n\n"
            "郵送に添える送付状と、FAX送付状のどちらでしょうか。教えていただければ作ります。"
        )
        others = [str(m) for m in missing if str(m).strip()]
        if others:
            text += "\n\nあわせて、これも教えていただけると一度で作れます。\n" + "\n".join(
                f"・{m}" for m in others
            )
        return text

    def _unknown_company(self, opening: str, hint: str) -> str:
        lead = _asking_opening(opening, "送付状ですね、お作りします。")
        names = "\n".join(f"・{c['name']}" for c in self._companies())
        return (
            f"{lead}\n\n"
            f"ただ、差出人の「{hint}」は住所・電話番号を登録していないので、"
            "そのままだと差出人欄を埋められません。\n"
            "いま差出人に使えるのはこの会社です。\n"
            f"{names}\n\n"
            f"「{hint}」でお作りするなら、会社名（正式名称）・住所・電話番号・FAX番号を"
            "教えてください。登録すれば次回からは会社名を言うだけで作れます。"
        )


# 表示名に書き足されがちな装飾（「足立 海里　資料作成集中（急ぎ案件のみ対応可）※土日休」）
_NAME_NOISE_RE = re.compile(r"[（(【\[<].*?[）)】\]>]|[※≪＜].*$|[／/|｜].*$")
_NAME_SUFFIX_RE = re.compile(r"(さん|様|氏|くん|ちゃん)$")
# 「経理財務部 足立」のように部署が先に来る表示名では、次の語を苗字とみなす。
# 「阿部」「服部」「渡部」を部署と誤らないよう、3文字以上のときだけ部署扱いにする
_ORG_WORD_RE = re.compile(r"..(部|課|係|室|支店|チーム|グループ)$|(株式会社|有限会社|合同会社)")


def surname(display_name: str, roster: tuple[str, ...] = ()) -> str:
    """Chatworkの表示名から苗字を取り出す。

    表示名には勤務状況などが書き足されていることがあるため、装飾を落として
    から先頭の語を取る。名簿（roster）があれば、まずそこと突き合わせる。
    区切りが無い表示名（「足立海里」等）は名簿がないと切りようがないため、
    誤った位置で切るより名前全体を残す。
    """
    name = unicodedata.normalize("NFKC", str(display_name or ""))
    name = _NAME_NOISE_RE.sub("", name).strip()
    if not name:
        return ""
    for member in roster:  # 名簿にある姓で始まっていればそれが苗字
        if member and name.startswith(member):
            return member
    parts = [p for p in re.split(r"[\s,、･・]+", name) if p]
    if not parts:
        return ""
    head = parts[0]
    if len(parts) > 1 and _ORG_WORD_RE.search(head):
        head = parts[1]
    return _NAME_SUFFIX_RE.sub("", head)


# 「担当は足立です」のように、担当者を名指ししている書き方
_STAFF_MARKER_RE = re.compile(r"(担当(?:者)?(?:名)?|差出人|発信者|私)[はをのが：:\s]*$")
# 「足立です」「足立さん」のような、名前だけの返事に付く言い回し
_POLITE_TAIL_RE = re.compile(r"(?:さん|様|氏|くん|ちゃん)?(?:です|でお願いします|でお願いします)?[。．.]?$")
# 名前のすぐ後ろの敬称。自社の担当者名には付かないので、先方の担当者の目印になる
_HONORIFIC_AFTER_RE = re.compile(r"[\s　]*(?:様|さま|サマ|殿|どの|さん)")
# 「岩永名義で」のように、名前の後ろに付く名指しの言い方。会社にも同じ言い方を
# 使う（「ヤマトライジング名義で」）が、こちらは名簿にある人だけを見るので混ざらない
_STAFF_SUFFIX_RE = re.compile(r"^(?:さん|氏)?[\s　]*(?:名義|名で|の名前|の名義)")


def _staff_from_text(instruction: str, roster: tuple[str, ...]) -> str:
    """依頼文から社内の担当者名を拾う（表示名が取れなかったときの受け皿）。

    宛先の会社名に名簿と同じ字が入っていることがあるため（「坂田商事」等）、
    担当者を名指ししている箇所か、名前だけを答えた返事のときだけ採る。
    """
    text = unicodedata.normalize("NFKC", str(instruction or "")).strip()
    if not text:
        return ""
    for member in roster:
        if text == member or _POLITE_TAIL_RE.sub("", text, count=1) == member:
            return member  # 聞き返しへの「足立」「足立です」という答え
    for member in roster:
        for match in re.finditer(re.escape(member), text):
            tail = text[match.end() :]
            if _STAFF_SUFFIX_RE.match(tail):
                return member  # 「岩永名義で」「岩永さん名義で」
            if not _STAFF_MARKER_RE.search(text[: match.start()]):
                continue
            if _HONORIFIC_AFTER_RE.match(tail):
                continue  # 「担当は伊藤様」は先方の担当者。自社の担当者に敬称は付かない
            return member
    return ""


def _delivering_opening(text: Any, default: str = DEFAULT_OPENING) -> str:
    """依頼を受けた側の書き出しにする。

    「よろしくお願いいたします」のような依頼する側の言い方で始まっていたら、
    受領・完了を伝える言い方へ置き換える（依頼したのは相手のため噛み合わない）。
    """
    opening = str(text or "").strip()
    if not opening or _ASKING_OPENING_RE.match(opening):
        return default
    return opening


# 聞き返しの時点では何も作っていない。「作成しました」「空欄にしています」と
# 書かれるとファイルが出来たと誤解されるため、作った旨の文面は使わせない
_MADE_IT_RE = re.compile(r"(作成しました|作りました|できました|空欄|用意しました|仕上げました)")


def _asking_opening(text: Any, default: str) -> str:
    """まだ作っていない段階の書き出し。完成したと読める文面は使わない。"""
    opening = str(text or "").strip()
    if not opening or _MADE_IT_RE.search(opening) or _ASKING_OPENING_RE.match(opening):
        return default
    return opening


# 法人格の略記。㈱のような合字はNFKCで「(株)」になるため、両方を見る
_LEGAL_FORMS = {
    "株": "株式会社",
    "有": "有限会社",
    "合": "合同会社",
    "同": "合同会社",
    "資": "合資会社",
    "名": "合名会社",
    "社": "社団法人",
    "財": "財団法人",
    "医": "医療法人",
    "学": "学校法人",
    "税": "税理士法人",
    "宗": "宗教法人",
    "独": "独立行政法人",
}
_ABBREV_RE = re.compile(r"[（(]\s*(" + "|".join(_LEGAL_FORMS) + r")\s*[）)]")
_LIGATURES = {
    "㈱": "株式会社", "㈲": "有限会社", "㈳": "社団法人", "㈶": "財団法人",
    "㈵": "企業組合", "㈻": "学校法人", "㈼": "監督", "㈺": "協同組合",
}


def expand_legal_form(name: str) -> str:
    """「㈱ライズ」「(株)ライズ」→「株式会社ライズ」。

    書類の宛名に略記は使わない。合字（㈱）と括弧書き（(株)）の両方を直す。
    書かれていない法人格を足すことはしない（推測で決めない）。
    """
    text = str(name or "").strip()
    for ligature, full in _LIGATURES.items():
        text = text.replace(ligature, full)
    text = _ABBREV_RE.sub(lambda m: _LEGAL_FORMS[m.group(1)], text)
    return re.sub(r"[\s　]+", " ", text).strip()


def build_recipient(parts: dict[str, str]) -> list[str]:
    """宛先を2行に組む。

    1行目に会社名（正式名称）、2行目に支店名・部署名・担当者名を置く。例:
      ["南都銀行", "奈良支店　営業課　田中様"]
      ["株式会社大塚商会", "大阪南支店　販売２課　ご担当者様"]
    2行目に置くものが無ければ、会社名に御中を付けた1行だけにする。
    会社名が無い個人あては、担当者の行だけを返す。
    """
    company = expand_legal_form(parts.get("company", ""))
    tail = [
        expand_legal_form(parts.get("branch", "")),
        expand_legal_form(parts.get("department", "")),
    ]
    person = re.sub(r"[\s　]+", " ", str(parts.get("person") or "")).strip()
    person = _HONORIFIC_RE.sub("", person).strip()  # 敬称はこちらで付け直す
    if person:
        tail.append(f"{person}様")
    elif any(tail):
        # 部署までしか分からないときは、個人名を作らずに「ご担当者様」とする
        tail.append("ご担当者様")
    tail = [t for t in tail if t]

    if not company:
        return ["　".join(tail)] if tail else []
    if not tail:
        return [f"{company}　御中"]
    return [company, "　".join(tail)]


def _split_honorific(line: str) -> tuple[str, str]:
    """「株式会社○○ 御中」→ ("株式会社○○", "御中")。敬称が無ければ既定の「御中」。"""
    text = str(line).strip()
    match = _HONORIFIC_RE.search(text)
    if match:
        return text[: match.start()].strip(), match.group(1)
    return text, "御中"


def _normalize(fields: dict[str, Any]) -> None:
    """空白だけの要素を落とす。

    不足判定（_missing）と差し込み（_render）で見え方が違うと、
    「宛先あり」と判定したのに宛先の無い書類ができてしまう。
    """
    fields["to_parts"] = {
        "company": str(fields.get("to_company") or "").strip(),
        "branch": str(fields.get("to_branch") or "").strip(),
        "department": str(fields.get("to_department") or "").strip(),
        "person": str(fields.get("to_person") or "").strip(),
    }
    # 差し込み・不足判定・ファイル名は組み上げた行を見る（見え方を1か所に揃える）
    fields["to_lines"] = build_recipient(fields["to_parts"])
    items = []
    for item in fields.get("items") or []:
        name = str((item or {}).get("name", "")).strip()
        if name:
            items.append({"name": name, "qty": str((item or {}).get("qty", "")).strip()})
    fields["items"] = items
    for key in ("subject", "staff", "to_fax", "to_tel", "date"):
        if key in fields:
            fields[key] = str(fields[key] or "").strip()


def _safe_name(text: str) -> str:
    """ファイル名に使える文字だけにする。"""
    cleaned = unicodedata.normalize("NFKC", text).strip()
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "", cleaned)
    return cleaned[:40] or "宛先未定"


def _ymd(date_text: str) -> str:
    """「2026年8月17日」→「20260817」。読み取れなければ今日の日付。"""
    m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", date_text)
    if m:
        return f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    return datetime.now(JST).strftime("%Y%m%d")
