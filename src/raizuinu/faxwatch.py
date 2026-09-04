"""FAX受信の見張り — 届いたFAXを読んで、どこから何が来たかを知らせる。

複合機からのFAXは「通知管理くん」がChatworkのFAXルームへ2通で流す。
  1通目: 受信日時・送信元番号（無いことも多い）・ファイル名の本文
  2通目: PDFの添付
通知だけでは差出人が分からないため、PDFを読んで差出人と内容を知らせる。
発注書・注文書なら、発注内容（品名・数量・金額・納期）までまとめる。

方針:
- FAXルームは巡回で見る（通知ボットはこちらをメンションしないためwebhookが
  効かない）。許可ルーム（Q&Aの対象）には入れない
- 差出人は、送信元番号があれば取引先台帳（Googleスプレッドシート）で引き、
  無ければPDFの本文から読む。どちらで判断したかを必ず書く
- 発注内容の数量・金額・納期はPDFのとおり写す。読み取れない箇所は
  「（読み取れず）」とし、推測で埋めない。発注の可否や妥当性は判断しない
- 一度知らせたFAXは覚えておき、二度と流さない。1日の処理数に上限を持つ
"""

from __future__ import annotations

import base64
import csv
import io
import json
import re
import traceback
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

JST = timezone(timedelta(hours=9))

# 通知本文から拾う項目
_FILENAME_RE = re.compile(r"ファイル名[：:]\s*([^\s\[]+\.pdf)", re.I)
_RECEIVED_RE = re.compile(r"受信日時[：:]\s*([\d/]+\s+[\d:]+)")
# 送信元番号。「送信元番号：0745787390」「送信元：0745-78-7390」など。
# 「送信元番号なし」の行には番号が無いので、数字の並びが無ければ空になる
_SENDER_RE = re.compile(r"送信元(?:番号)?[：:]?\s*([\d\-－() ]{9,20})")
_DOWNLOAD_RE = re.compile(r"\[download:(\d+)\]([^\[]*?\.pdf)\s*(?:\([^)]*\))?\s*\[/download\]", re.I)

# 台帳のFAX番号は「0745787390」のように数字だけで書かれている。
# 通知側はハイフン付きのこともあるため、数字だけに揃えてから突き合わせる
_DIGITS_RE = re.compile(r"\D+")


def digits_only(text: str) -> str:
    return _DIGITS_RE.sub("", unicodedata.normalize("NFKC", str(text or "")))


def parse_notice(body: str) -> dict[str, str]:
    """通知本文から受信日時・送信元番号・ファイル名を取り出す。"""
    text = unicodedata.normalize("NFKC", str(body or ""))
    sender = ""
    if "番号なし" not in text:
        match = _SENDER_RE.search(text)
        if match:
            sender = digits_only(match.group(1))
    filename = _FILENAME_RE.search(text)
    received = _RECEIVED_RE.search(text)
    return {
        "sender_number": sender if len(sender) >= 9 else "",
        "filename": filename.group(1).strip() if filename else "",
        "received_at": received.group(1).strip() if received else "",
    }


def parse_attachment(body: str) -> dict[str, Any] | None:
    """PDF添付の通知から、ファイルIDとファイル名を取り出す。"""
    match = _DOWNLOAD_RE.search(str(body or ""))
    if not match:
        return None
    return {"file_id": int(match.group(1)), "filename": match.group(2).strip()}


class PartnerDirectory:
    """FAX番号 → 取引先名 の台帳（Googleスプレッドシートの公開CSV）。

    毎回取りに行かず、しばらく手元に置く。取れなかったときは前回の表を使う
    （台帳が一時的に読めないだけで通知を止めない）。
    """

    def __init__(
        self,
        url: str,
        http_get: Callable[..., tuple[int, str, bytes]],
        ttl_seconds: int = 3600,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._url = url
        self._http_get = http_get
        self._ttl = int(ttl_seconds)
        self._now = now or (lambda: datetime.now(JST).timestamp())
        self._table: dict[str, str] = {}
        self._loaded_at = 0.0

    def lookup(self, fax_number: str) -> str:
        """番号から取引先名。無ければ空文字。"""
        key = digits_only(fax_number)
        if not key:
            return ""
        self._refresh()
        return self._table.get(key, "")

    def _refresh(self) -> None:
        if self._table and self._now() - self._loaded_at < self._ttl:
            return
        try:
            status, final_url, data = self._http_get(self._url)
        except Exception:
            print("[warn] 取引先台帳の取得に失敗: " + traceback.format_exc(), flush=True)
            return
        if status != 200 or "accounts.google.com" in str(final_url):
            print(
                "[warn] 取引先台帳を開けませんでした（共有設定が「リンクを知っている全員」か確認）",
                flush=True,
            )
            return
        table: dict[str, str] = {}
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"))))
        for row in rows[1:]:  # 1行目は見出し（FAX番号, 取引先名）
            if len(row) < 2:
                continue
            number, name = digits_only(row[0]), str(row[1]).strip()
            if number and name:
                table[number] = name
        if table:
            self._table = table
            self._loaded_at = self._now()


READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sender": {
            "type": "string",
            "description": "差出人の会社名（団体名）。文書に書かれているとおり。分からなければ空文字",
        },
        "kind": {
            "type": "string",
            "enum": ["発注書", "注文書", "請求書", "見積書", "納品書", "案内", "広告", "その他"],
            "description": "文書の種類",
        },
        "is_order": {
            "type": "boolean",
            "description": "発注書・注文書など、こちらに何かを発注してきている文書なら true",
        },
        "summary": {
            "type": "string",
            "description": "内容を1〜2文で。発注なら何を・いくつ・いくらで・いつまでに、を含める",
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "quantity": {"type": "string"},
                    "amount": {"type": "string"},
                },
                "required": ["name", "quantity", "amount"],
                "additionalProperties": False,
            },
            "description": "発注の明細。発注でなければ空配列。読み取れない項目は空文字",
        },
        "due": {"type": "string", "description": "納期・希望日。書かれていなければ空文字"},
        "notes": {"type": "string", "description": "担当者名・連絡先・備考など、伝えるべき一言。無ければ空文字"},
    },
    "required": ["sender", "kind", "is_order", "summary", "items", "due", "notes"],
    "additionalProperties": False,
}

READ_SYSTEM = """あなたは株式会社ライズクリエイション経理財務部のアシスタントです。
届いたFAXのPDFを読み、差出人と内容を部内へ知らせるために整理します。

厳守すること:
- 文書に書かれていることだけを使う。書かれていないことを推測で補わない
- 品名・数量・金額・納期・日付は文書のとおり一字一句正確に写す。丸めない
- 読み取れない箇所は空文字にする。それらしい値を作らない
- 発注してよいか・金額が妥当かなどの判断はしない。内容の整理だけを行う
- FAXは画質が粗いことがある。自信の無い読み取りは summary で
  「（判読しづらい）」と添える
"""


class FaxWatcher:
    """FAXルームを巡回し、新しいFAXを読んで知らせる。"""

    def __init__(
        self,
        config: Any,
        chatwork: Any,
        client: Any | None = None,
        http_get: Callable[..., tuple[int, str, bytes]] | None = None,
        cost: Any | None = None,
        directory: PartnerDirectory | None = None,
    ) -> None:
        self._config = config
        self._chatwork = chatwork
        self._client = client
        self._cost = cost
        if http_get is None:
            import requests

            def http_get(url: str, timeout: int = 60) -> tuple[int, str, bytes]:
                resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
                return resp.status_code, resp.url, resp.content

        self._http_get = http_get
        settings = config.fax_watch
        self._directory = directory or PartnerDirectory(
            str(settings.get("directory_csv_url") or ""), self._http_get
        )
        self._state_path = config.resolve_path(config.state_dir) / "faxwatch.json"

    # --- 公開API ---

    def run_once(self) -> int:
        """新しいFAXを処理する。→ 知らせた件数。"""
        settings = self._config.fax_watch
        if not settings.get("enabled"):
            return 0
        room_id = int(settings.get("room_id", 0))
        notifier = int(settings.get("notifier_account_id", 0))
        if not room_id or not notifier:
            return 0
        state = self._load_state()
        try:
            messages = self._chatwork.get_recent_messages(room_id, limit=100)
        except Exception:
            print("[warn] FAXルームの取得に失敗: " + traceback.format_exc(), flush=True)
            return 0

        # 初回は既読にするだけ。過去のFAXをまとめて流さない
        if state.get("last_seen") is None:
            state["last_seen"] = max((int(m.get("message_id", 0)) for m in messages), default=0)
            self._save_state(state)
            return 0

        today = datetime.now(JST).strftime("%Y%m%d")
        if state.get("date") != today:
            state["date"], state["count"] = today, 0
        limit = int(settings.get("max_per_day", 50))

        handled = 0
        notices = self._notices_by_filename(messages, notifier)
        newest = int(state.get("last_seen", 0))
        for message in messages:
            mid = int(message.get("message_id", 0))
            newest = max(newest, mid)
            if mid <= int(state.get("last_seen", 0)):
                continue
            if int((message.get("account") or {}).get("account_id", 0) or 0) != notifier:
                continue
            attachment = parse_attachment(message.get("body", ""))
            if attachment is None:
                continue
            if str(mid) in (state.get("done") or []):
                continue
            if state["count"] >= limit:
                print(f"[warn] FAXの1日の処理上限（{limit}件）に達しました", flush=True)
                break
            notice = notices.get(attachment["filename"].lower(), {})
            state["count"] += 1
            state.setdefault("done", []).append(str(mid))
            state["done"] = state["done"][-500:]
            self._save_state(state)  # 呼ぶ前に数える（失敗しても消費は起きるため）
            try:
                self._handle(room_id, message, attachment, notice)
                handled += 1
            except Exception:
                print("[error] FAXの処理に失敗: " + traceback.format_exc(), flush=True)
                self._report_failure(room_id, message, attachment)
        state["last_seen"] = newest
        self._save_state(state)
        return handled

    # --- 内部 ---

    @staticmethod
    def _notices_by_filename(messages: list[dict], notifier: int) -> dict[str, dict[str, str]]:
        """通知本文（送信元番号など）を、添付とファイル名で結びつける。"""
        table: dict[str, dict[str, str]] = {}
        for message in messages:
            if int((message.get("account") or {}).get("account_id", 0) or 0) != notifier:
                continue
            body = str(message.get("body", ""))
            if "[download:" in body:
                continue
            parsed = parse_notice(body)
            if parsed["filename"]:
                table[parsed["filename"].lower()] = parsed
        return table

    def _handle(self, room_id: int, message: dict, attachment: dict, notice: dict) -> None:
        settings = self._config.fax_watch
        data, filename = self._download(room_id, attachment)
        found = self._read(data, filename)
        usage = found.pop("_usage", {})

        # 差出人: 台帳で引けたらそれを正とする。無ければ文書の記載
        number = notice.get("sender_number", "")
        partner = self._directory.lookup(number) if number else ""
        if partner:
            sender, basis = partner, f"FAX番号 {number} を台帳で照合"
        elif found.get("sender"):
            sender, basis = str(found["sender"]), "FAXの記載から"
        else:
            sender, basis = "", ""

        text = self._compose(found, sender, basis, number, notice, filename)
        recipients = settings.get("notify_account_ids") or []
        heads = " ".join(f"[To:{int(a)}]" for a in recipients)
        from .answer import sanitize_for_chatwork

        reply_tag = f"[rp aid={int((message.get('account') or {}).get('account_id', 0))} to={room_id}-{message.get('message_id')}]"
        self._chatwork.send_message(
            room_id, f"{reply_tag}\n{heads}\n" + sanitize_for_chatwork(text)
        )
        self._audit(
            {
                "type": "fax_notice",
                "room_id": room_id,
                "message_id": str(message.get("message_id")),
                "filename": filename,
                "sender": sender,
                "basis": basis,
                "kind": found.get("kind"),
                "is_order": bool(found.get("is_order")),
                "usage": usage,
            }
        )

    def _download(self, room_id: int, attachment: dict) -> tuple[bytes, str]:
        info = self._chatwork.get_file_info(room_id, int(attachment["file_id"]))
        filename = str(info.get("filename") or attachment.get("filename") or "fax.pdf")
        max_mb = float(self._config.fax_watch.get("max_pdf_mb", 15))
        if float(info.get("filesize", 0)) > max_mb * 1024 * 1024:
            raise RuntimeError(f"{filename} が {max_mb:.0f}MB を超えています")
        status, _, data = self._http_get(str(info.get("download_url", "")))
        if status != 200 or not data.startswith(b"%PDF"):
            raise RuntimeError(f"{filename} をPDFとして取得できませんでした（HTTP {status}）")
        return data, filename

    def _read(self, data: bytes, filename: str) -> dict[str, Any]:
        from .answer import _call_with_continuation

        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        cfg = self._config
        kwargs = {
            "model": cfg.model,
            "max_tokens": int(cfg.max_tokens),
            "output_config": {
                "effort": "low",
                "format": {"type": "json_schema", "schema": READ_SCHEMA},
            },
            "system": READ_SYSTEM,
        }
        content = [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(data).decode("ascii"),
                },
                "title": filename,
            },
            {"type": "text", "text": f"このFAX（{filename}）の差出人と内容を整理してください。"},
        ]
        response, usage, _ = _call_with_continuation(
            self._client.messages.create, kwargs, [{"role": "user", "content": content}]
        )
        if self._cost is not None:
            try:
                self._cost.add_usage(usage)
            except Exception:
                print("[warn] コスト計上に失敗: " + traceback.format_exc(), flush=True)
        text = next(
            (b.text for b in getattr(response, "content", []) if getattr(b, "type", "") == "text"),
            "",
        )
        try:
            found = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            found = {"sender": "", "kind": "その他", "is_order": False, "summary": "", "items": [], "due": "", "notes": ""}
        found["_usage"] = usage
        return found

    @staticmethod
    def _compose(
        found: dict, sender: str, basis: str, number: str, notice: dict, filename: str
    ) -> str:
        kind = str(found.get("kind") or "その他")
        received = notice.get("received_at", "")
        lines: list[str] = []
        if found.get("is_order"):
            lines.append(f"{kind}が届きました。" if sender == "" else f"{sender}から{kind}が届きました。")
        else:
            lines.append(
                f"FAXが届きました（{kind}）。" if sender == "" else f"{sender}からFAXが届きました（{kind}）。"
            )
        if sender:
            lines.append(f"差出人の根拠: {basis}")
        elif number:
            lines.append(f"差出人: 特定できませんでした（FAX番号 {number} は台帳に無く、文書にも社名が見当たりません）")
        else:
            lines.append("差出人: 特定できませんでした（送信元番号なし・文書にも社名が見当たりません）")

        summary = str(found.get("summary") or "").strip()
        if summary:
            lines.append("")
            lines.append(summary)

        items = [i for i in (found.get("items") or []) if str(i.get("name", "")).strip()]
        if found.get("is_order") and items:
            lines.append("")
            lines.append("■発注内容")
            for item in items:
                bits = [str(item.get("name", "")).strip()]
                for key in ("quantity", "amount"):
                    value = str(item.get(key, "") or "").strip()
                    bits.append(value if value else "（読み取れず）")
                lines.append("・" + "　".join(bits))
            due = str(found.get("due") or "").strip()
            lines.append(f"納期: {due if due else '（記載なし）'}")
        notes = str(found.get("notes") or "").strip()
        if notes:
            lines.append(f"備考: {notes}")

        lines.append("")
        meta = [f"ファイル: {filename}"]
        if received:
            meta.append(f"受信 {received}")
        lines.append("（" + "／".join(meta) + "）")
        lines.append("※PDFを読んで整理しています。数量・金額は原本でご確認ください。")
        return "\n".join(lines)

    def _report_failure(self, room_id: int, message: dict, attachment: dict) -> None:
        """読めなかったことは黙らずに知らせる（届いたのに気づかれないのを防ぐ）。"""
        try:
            settings = self._config.fax_watch
            heads = " ".join(f"[To:{int(a)}]" for a in settings.get("notify_account_ids") or [])
            self._chatwork.send_message(
                room_id,
                f"{heads}\nFAX「{attachment.get('filename', '')}」が届いていますが、"
                "こちらでは内容を読み取れませんでした。お手数ですがPDFを直接ご確認ください。",
            )
        except Exception:
            print("[warn] 失敗の通知に失敗: " + traceback.format_exc(), flush=True)

    def _audit(self, record: dict) -> None:
        try:
            from .audit import AuditLogger

            cfg = self._config
            AuditLogger(
                log_dir=cfg.resolve_path(cfg.audit_log_dir),
                retention_days=cfg.audit_log_retention_days,
            ).log(record)
        except Exception:
            print("[warn] 監査ログの記録に失敗: " + traceback.format_exc(), flush=True)

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"last_seen": None, "done": [], "date": "", "count": 0}

    def _save_state(self, state: dict) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError:
            print("[warn] FAX巡回の状態を保存できませんでした", flush=True)
