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

# FAX送付状「汎用」シートの差し込み先
FAX_CELLS = {
    "to_company": "D10",
    "to_person": "D12",
    "to_fax": "D14",
    "to_tel": "D16",
    "subject": "E19",
    "pages": "N6",
}
FAX_BODY_ROWS = ("B23", "B24", "B25", "B26", "B27")

# 契約書ひな形は生成しない（締結前のリーガルチェックが必須のため案内に倒す）
CONTRACT_GUIDANCE = (
    "契約書のひな形は、こちらで中身を埋めて作ることはしていません。"
    "条項の当てはめを誤ると締結後に効いてくるためです。\n"
    "ひな形リンク集の該当ファイルをBoxから開いてご利用ください。"
    "作成後は、締結前にLegalForceでのリーガルチェックをお願いします。"
)

_BUILD_VERB_RE = re.compile(
    r"(作成|作って|作りたい|つくって|つくりたい|用意し|準備し|起こして|出して|"
    r"仕上げて|お願いし|ください|下さい|ほしい|欲しい)"
)
_DOC_NAME_RE = re.compile(r"(送付状|送信状|添え状|添付状|そえじょう|そうふじょう)")
# 「書き方」「どこにある」等はQ&Aで答える（作成依頼ではない）
_EXCLUDE_RE = re.compile(
    r"(書き方|作り方|やり方|手順|どこにあ|どこです|どこ[？?]|場所は|保管|"
    r"とは何|の意味|注意点|ルール)"
)
_CONTRACT_RE = re.compile(r"(契約書|覚書|念書|誓約書|NDA|秘密保持)")


def looks_like_document_build_request(question: str) -> bool:
    """「送付状を作って」のような書類作成依頼かどうか。

    書類名（送付状・FAX送付状 等）と作成の語が両方あり、
    「書き方を教えて」のような質問でないときだけ真。
    """
    if not _DOC_NAME_RE.search(question):
        return False
    if _EXCLUDE_RE.search(question):
        return False
    return bool(_BUILD_VERB_RE.search(question))


def wants_contract_template(question: str) -> bool:
    """契約書ひな形そのものの作成依頼か（送付状の同封物としての言及は除く）。"""
    if not _CONTRACT_RE.search(question):
        return False
    return not _DOC_NAME_RE.search(question)


class DocumentBuildError(Exception):
    """書類の作成に失敗した（利用者向けの文面をそのまま持つ）。"""


SYSTEM_PROMPT = """あなたは株式会社ライズクリエイション経理財務部のアシスタント「{agent_name}」です。
社内のひな形を使って書類の下書きを作るため、依頼文から必要な項目を抜き出します。

厳守すること:
- 依頼文（と会話の流れ）に書かれていることだけを使う。宛先・会社名・書類名・
  部数・FAX番号を推測で作らない
- 宛先や送付書類が分からないときは、その項目を空にして missing に何が足りないかを書く
- 敬称は依頼文の書き方に従う。会社あてなら「御中」、個人あてなら「様」を付ける
- 日付の指定がなければ today をそのまま使う
- 数字・固有名詞は依頼文のまま写す（丸めたり言い換えたりしない）

宛先の書き方（to_lines）は1行ずつ配列にする。例:
  ["株式会社大塚商会", "大阪南CADグループ", "販売２課　濵野 康一　様"]

送付書類（items）は依頼文にある物だけを並べる。部数の指定がなければ qty は "1部" にする。
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
            "description": "依頼への一言。話し言葉で1〜2文。書類の中身は書かない",
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
        meta["fields"] = {k: v for k, v in fields.items() if k != "opening"}

        template_id = str(fields.get("template_id") or "")
        allowed = self._allowed_templates()
        if template_id not in allowed:
            meta["error"] = "template_not_found"
            return self._choose_guidance(allowed), meta, usage

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
                f"（{exc}）お手数ですが、もう一度ご依頼いただけますか。"
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
                f"依頼者: {requester_name}（差出人の担当者名が依頼文になければこの姓を使う）"
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
                "宛先と送付する書類を書き添えて、もう一度お願いできますか。"
            ) from exc
        if not fields.get("date"):
            fields["date"] = today
        return fields, usage

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
            cells: dict[str, Any] = {
                FAX_CELLS["to_company"]: to_lines[0] if to_lines else "",
                FAX_CELLS["to_person"]: "　".join(to_lines[1:]),
                FAX_CELLS["to_fax"]: str(fields.get("to_fax") or ""),
                FAX_CELLS["to_tel"]: str(fields.get("to_tel") or ""),
                FAX_CELLS["subject"]: str(fields.get("subject") or ""),
            }
            pages = fields.get("pages")
            if isinstance(pages, int) and pages > 0:
                cells[FAX_CELLS["pages"]] = pages
            for ref, line in zip(FAX_BODY_ROWS, fields.get("body_lines") or []):
                cells[ref] = str(line)
            if fields.get("date"):
                cells["E6"] = date  # 日付の指定があるときだけ =TODAY() を上書きする
            out = render_xlsx(data, template["sheet"], cells)

        suffix = ".docx" if template["kind"] == "docx" else ".xlsx"
        stem = template_id if template["kind"] == "xlsx" else template_id.split("_")[0]
        name = f"{stem}_{_safe_name(to_lines[0] if to_lines else '宛先未定')}_{_ymd(date)}{suffix}"
        return name, out

    def _reply(self, fields: dict[str, Any], template: dict[str, Any]) -> str:
        opening = str(fields.get("opening") or "").strip()
        if not opening:
            opening = "承知しました。下書きを作ってお送りしますね。"
        return (
            f"{opening}\n\n"
            f"ひな形: {template['label']}\n"
            f"出典: {template['box_url']}\n"
            "※下書きです。日付・宛名・部数をご確認のうえ、印刷や送付はお手元でお願いします。"
        )

    @staticmethod
    def _ask_for(opening: str, missing: list[str]) -> str:
        lead = opening.strip() or "書類の下書き、お作りしますね。"
        items = "\n".join(f"・{m}" for m in missing)
        return (
            f"{lead}\n\n"
            "作るのにこれだけ足りませんでした。教えていただけますか。\n"
            f"{items}"
        )

    @staticmethod
    def _choose_guidance(allowed: list[str]) -> str:
        names = "\n".join(f"・{TEMPLATES[t]['label']}" for t in allowed)
        return (
            "どのひな形で作るか決めきれませんでした。次のどれかを指定していただけますか。\n"
            f"{names}"
        )


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
