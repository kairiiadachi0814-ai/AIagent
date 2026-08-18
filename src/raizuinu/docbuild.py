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

# ひな形の定義。追加するときはここに1件足せばよい（判定・生成は共通）
TEMPLATES: dict[str, dict[str, Any]] = {
    "書類送付状_ライズ": {
        "label": "書類送付状（株式会社ライズクリエイション）",
        "file": "書類送付状_ライズ.docx",
        "kind": "docx",
        "box_url": "https://app.box.com/file/1529686391255",
        "sender": "株式会社ライズクリエイション",
        "hint": "差出人が株式会社ライズクリエイションのとき。既定はこれ",
    },
    "書類送付状_ヤマトライジング": {
        "label": "書類送付状（株式会社ヤマトライジング）",
        "file": "書類送付状_ヤマトライジング.docx",
        "kind": "docx",
        "box_url": "https://app.box.com/file/2361204879735",
        "sender": "株式会社ヤマトライジング",
        "hint": "差出人がヤマトライジングのとき",
    },
    "書類送付状_楽天軒": {
        "label": "書類送付状（ＲＡＫＵＴＥＮＫＥＮ株式会社）",
        "file": "書類送付状_楽天軒.docx",
        "kind": "docx",
        "box_url": "https://app.box.com/file/1681180574076",
        "sender": "ＲＡＫＵＴＥＮＫＥＮ株式会社",
        "hint": "差出人が楽天軒（RAKUTENKEN）のとき",
    },
    "FAX送付状": {
        "label": "FAX送付状",
        "file": "FAX送付状.xlsx",
        "kind": "xlsx",
        "sheet": "汎用",
        "box_url": "https://app.box.com/file/1529688805714",
        "sender": "株式会社ライズクリエイション",
        "hint": "郵送ではなくFAXで送るとき",
    },
}

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

宛先の書き方（to_lines）は1行ずつ配列にする。例:
  ["株式会社大塚商会", "大阪南CADグループ", "販売２課　濵野 康一　様"]

送付書類（items）は依頼文にある物だけを並べる。部数の指定がない物は qty を空文字にする
（こちらで「1部」と仮置きし、その旨を利用者に伝える）。
"""

_FIELDS_SCHEMA = {
    "type": "object",
    "properties": {
        "template_id": {
            "type": "string",
            "enum": [*TEMPLATES.keys(), ""],
            "description": "使うひな形。判断できないときは空文字",
        },
        "date": {"type": "string", "description": "書類の日付。例: 2026年8月17日"},
        "to_lines": {
            "type": "array",
            "items": {"type": "string"},
            "description": "宛先。会社名・部署・担当者を1行ずつ",
        },
        "staff": {"type": "string", "description": "差出人の担当者名（姓のみ）。不明なら空"},
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
    "required": ["template_id", "date", "to_lines", "staff", "items", "missing", "opening"],
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
        # 差出人の担当者は、依頼文に指定がなければ依頼者の苗字にする。
        # モデルがフルネームを返した場合もここで苗字へ落とす
        fields["staff"] = surname(fields.get("staff") or requester_name, self._roster())
        meta["fields"] = {k: v for k, v in fields.items() if k != "opening"}

        template_id = str(fields.get("template_id") or "")
        allowed = self._allowed_templates()
        if template_id not in allowed:
            meta["error"] = "template_not_found"
            return (
                self._choose_guidance(
                    allowed, fields.get("opening", ""), fields.get("missing") or []
                ),
                meta,
                usage,
            )

        template = TEMPLATES[template_id]
        meta["template"] = template_id
        missing = self._missing(template, fields)
        if missing:
            meta["error"] = "missing_fields"
            return self._ask_for(fields.get("opening", ""), missing), meta, usage

        try:
            filename, data = self._render(template_id, template, fields)
        except TemplateError as exc:
            raise DocumentBuildError(
                "すみません、ひな形への差し込みでつまずいてしまいました。"
                f"（{exc}）お手数ですが、もう一度ご依頼いただけますか。",
                usage,
            ) from exc
        meta["artifact"] = (filename, data)
        meta["output_filename"] = filename
        return self._reply(fields, template), meta, usage

    # --- 内部 ---

    def _allowed_templates(self) -> list[str]:
        configured = self._config.doc_build.get("allowed_templates") or []
        return [t for t in configured if t in TEMPLATES]

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

        catalog = "\n".join(
            f"- {tid}: {TEMPLATES[tid]['label']}（差出人 {TEMPLATES[tid]['sender']}。{TEMPLATES[tid]['hint']}）"
            for tid in self._allowed_templates()
        )
        parts = [
            f"今日の日付: {today}",
            f"使えるひな形:\n{catalog}",
        ]
        if requester_name:
            parts.append(
                f"依頼者の姓: {surname(requester_name, self._roster())}"
                "（差出人の担当者名は、依頼文に別の指定がなければこの姓にする。"
                "staffには姓だけを入れ、フルネームや敬称は付けない）"
            )
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
    def _missing(template: dict[str, Any], fields: dict[str, Any]) -> list[str]:
        missing = [str(m) for m in fields.get("missing") or []]
        if not fields.get("to_lines"):
            missing.append("宛先")
        if template["kind"] == "docx" and not fields.get("items"):
            missing.append("送付する書類")
        if template["kind"] == "xlsx" and not str(fields.get("subject") or "").strip():
            missing.append("件名")
        seen: dict[str, None] = {}
        for item in missing:
            seen.setdefault(item.strip(), None)
        return [m for m in seen if m]

    def _render(
        self, template_id: str, template: dict[str, Any], fields: dict[str, Any]
    ) -> tuple[str, bytes]:
        data = (self._templates_dir() / template["file"]).read_bytes()
        to_lines = [str(line) for line in fields.get("to_lines") or [] if str(line).strip()]
        date = str(fields.get("date") or "")

        if template["kind"] == "docx":
            max_items = int(self._config.doc_build.get("max_items", 20))
            items = (fields.get("items") or [])[:max_items]
            out = render_docx(
                data,
                {"date": date, "staff": str(fields.get("staff") or "")},
                {
                    "to_line": [{"to_line": line} for line in to_lines],
                    "item_name": [
                        {
                            "item_name": str(item.get("name", "")),
                            "item_qty": str(item.get("qty", "") or "1部"),
                        }
                        for item in items
                    ],
                },
            )
        else:
            # 「汎用」シートは会社名と敬称が別の欄。敬称込みで会社名を入れると
            # 「○○ 御中 御中」になるため、末尾の敬称を切り離す
            company, honorific = _split_honorific(to_lines[0] if to_lines else "")
            cells: dict[str, Any] = {
                FAX_CELLS["to_company"]: company,
                FAX_CELLS["honorific"]: honorific,
                FAX_CELLS["to_person"]: "　".join(to_lines[1:]),
                FAX_CELLS["to_fax"]: str(fields.get("to_fax") or ""),
                FAX_CELLS["to_tel"]: str(fields.get("to_tel") or ""),
                FAX_CELLS["subject"]: str(fields.get("subject") or ""),
                FAX_CELLS["sender_staff"]: str(fields.get("staff") or ""),
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
            out = render_xlsx(data, template["sheet"], cells)

        suffix = ".docx" if template["kind"] == "docx" else ".xlsx"
        stem = template_id if template["kind"] == "xlsx" else template_id.split("_")[0]
        label = _split_honorific(to_lines[0])[0] if to_lines else "宛先未定"
        name = f"{stem}_{_safe_name(label)}_{_ymd(date)}{suffix}"
        return name, out

    def _reply(self, fields: dict[str, Any], template: dict[str, Any]) -> str:
        opening = _delivering_opening(fields.get("opening"))
        assumed = [
            item["name"] for item in fields.get("items") or [] if not item.get("qty")
        ]
        note = ""
        if assumed:
            # 推測で埋めた箇所は黙って通さず、必ず伝える
            note = "部数の指定がなかった「" + "」「".join(assumed) + "」は1部としています。\n"
        return (
            f"{opening}\n\n"
            f"{note}"
            f"ひな形: {template['label']}\n"
            f"出典: {template['box_url']}\n"
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
    def _choose_guidance(allowed: list[str], opening: str, missing: list[str]) -> str:
        lead = _asking_opening(opening, "送付状ですね、お作りします。")
        names = "\n".join(f"・{TEMPLATES[t]['label']}" for t in allowed)
        text = (
            f"{lead}\n\n"
            "どのひな形で作るか決めきれませんでした。次のどれかを指定していただけますか。\n"
            f"{names}"
        )
        others = [str(m) for m in missing if str(m).strip()]
        if others:
            text += "\n\nあわせて、これも教えていただけると一度で作れます。\n" + "\n".join(
                f"・{m}" for m in others
            )
        return text


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
    fields["to_lines"] = [
        str(line).strip() for line in fields.get("to_lines") or [] if str(line).strip()
    ]
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
