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
- 知らせるのは月〜金の 8:30〜19:30 だけ。夜間・土日に届いた分は取っておき、
  次の時間帯の頭にまとめて知らせる。祝日は平日と同じに扱う（業務があるため）
- 発注書・注文書は「対応完了」の返事をもらうまで見ておき、翌営業日の朝に
  済んだかを聞き、それにも返事が無ければ昼にもう一度だけ聞く
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

JST = timezone(timedelta(hours=9))
WEEKDAYS = "月火水木金土日"

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

# 返信タグと、本文の判定前に落とすChatworkのタグ
_RP_RE = re.compile(r"\[rp\s+aid=\d+\s+to=(\d+)-(\d+)\]")
_TAG_RE = re.compile(r"\[/?[A-Za-z]+[^\]]*\]")

# 「対応完了」と読める言い回し。人は「完了しました」「対応済です」などと書く
_DONE_RE = re.compile(
    r"(完了|済み|済です|済でした|済んで|対応済|確認済|処理済|終わ"
    r"|対応しました|確認しました|処理しました|発注しました|入力しました|登録しました|手配しました)"
)
# 打ち消し。「未完了」「まだ対応できていません」「対応中です」は完了ではない
_NOT_DONE_RE = re.compile(
    r"(未完了|未対応|未確認|未処理|まだ|ていません|てません|てない|していない|中です|これから|できてません)"
)


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


def strip_tags(body: str) -> str:
    return _TAG_RE.sub("", str(body or ""))


def rotate_pdf(data: bytes, degrees: int) -> bytes | None:
    """全ページを時計回りに degrees 回したPDF。回せなければ None。

    FAXは紙を逆さまに入れて送られてくることがあり、そのままではモデルが
    読めない（実例 2026-09-05: 珍味屋の発注書が「その他」になった）。
    """
    try:
        import pypdf
    except ImportError:
        print("[warn] pypdf が無いため、逆さまのFAXを回せません", flush=True)
        return None
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        writer = pypdf.PdfWriter()
        for page in reader.pages:
            page.rotate(int(degrees))
            writer.add_page(page)
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception:
        print("[warn] PDFを回せませんでした: " + traceback.format_exc(), flush=True)
        return None


def _merge_usage(first: dict, second: dict) -> dict:
    merged = dict(first or {})
    for key, value in (second or {}).items():
        merged[key] = merged.get(key, 0) + value
    return merged


def is_completion(body: str) -> bool:
    """「対応完了」「確認済です」など、済んだと読める返事か。"""
    text = unicodedata.normalize("NFKC", strip_tags(body))
    return bool(_DONE_RE.search(text)) and not _NOT_DONE_RE.search(text)


def reply_targets(body: str, room_id: int) -> set[str]:
    """本文の返信タグが指しているメッセージID（同じルームのものだけ）。"""
    return {mid for room, mid in _RP_RE.findall(str(body or "")) if int(room) == int(room_id)}


# --- 時間の扱い ---


def parse_clock(text: Any, default: tuple[int, int]) -> tuple[int, int]:
    """"08:30" → (8, 30)。読めなければ既定値。"""
    try:
        hour, minute = str(text).split(":")
        return int(hour), int(minute)
    except (ValueError, AttributeError):
        return default


def is_business_day(day: date) -> bool:
    """月〜金。祝日も平日と同じに数える（祝日も業務があるため。休むのは土日だけ）。"""
    return day.weekday() < 5


def next_business_day(day: date) -> date:
    day += timedelta(days=1)
    while not is_business_day(day):
        day += timedelta(days=1)
    return day


def in_notify_window(now: datetime, window: dict) -> bool:
    """知らせてよい時間帯か（月〜金の start〜end）。"""
    if not is_business_day(now.date()):
        return False
    start = parse_clock(window.get("start"), (8, 30))
    end = parse_clock(window.get("end"), (19, 30))
    minutes = now.hour * 60 + now.minute
    return start[0] * 60 + start[1] <= minutes < end[0] * 60 + end[1]


def at_clock(day: date, clock: tuple[int, int]) -> datetime:
    return datetime(day.year, day.month, day.day, clock[0], clock[1], tzinfo=JST)


def _parse_iso(text: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)


def _parse_received(text: Any) -> date | None:
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(text).strip(), fmt).date()
        except ValueError:
            continue
    return None


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
            "description": (
                "文書の表題をそのまま。例: 発注書／注文書／直送依頼書／サンプル依頼書／FAX申込書／"
                "商品注文伝票／注文表／【発注・入荷】表／発注伝票／請求書／見積書／納品書。"
                "表題が無ければ内容を表す短い語（注文／直送／案内／広告 など）"
            ),
        },
        "category": {
            "type": "string",
            "enum": ["注文", "請求", "見積", "納品", "案内", "広告", "その他"],
            "description": (
                "文書の性質。こちらに商品の発注・注文・直送・サンプル送付などを依頼してきている"
                "文書は、表題が何であれ「注文」"
            ),
        },
        "is_order": {
            "type": "boolean",
            "description": "category が「注文」なら true（こちらに何かを発注・注文・依頼してきている文書）",
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
        "rotation": {
            "type": "integer",
            "enum": [0, 90, 180, 270],
            "description": "文書を正しい向きで読むために時計回りに回す角度。上下逆さまなら180。正しい向きなら0",
        },
        "readable": {
            "type": "boolean",
            "description": "差出人・種類・内容の主要な項目が判読できたら true。逆さま・不鮮明・鏡像などでほとんど読めなければ false",
        },
    },
    "required": [
        "sender", "kind", "category", "is_order", "summary", "items", "due", "notes", "rotation", "readable",
    ],
    "additionalProperties": False,
}

# 表題にこれらの語があれば、モデルの判定に関わらず注文として扱う（見落としを防ぐ保険）。
# 実例（2026-09-07）: 直送依頼書・サンプル依頼書・FAX申込書・商品注文伝票・注文表・
# 【発注・入荷】表・発注伝票、それに「注文」「直送」とだけ書かれた伝票で注文が来ていた
DEFAULT_ORDER_WORDS = ("注文", "発注", "直送", "申込", "サンプル", "入荷")


def looks_like_order(title: str, words: tuple[str, ...] | list[str]) -> bool:
    text = unicodedata.normalize("NFKC", str(title or ""))
    return any(w and w in text for w in words)

READ_SYSTEM = """あなたは株式会社ライズクリエイション経理財務部のアシスタントです。
届いたFAXのPDFを読み、差出人と内容を部内へ知らせるために整理します。

厳守すること:
- 発注・注文の見落としは許されない。表題が「発注書」「注文書」でなくても、
  直送依頼書・サンプル依頼書・FAX申込書・商品注文伝票・注文表・【発注・入荷】表・
  発注伝票のように、こちらへ商品の注文・発注・直送・サンプル送付を頼んでいる文書は
  category を「注文」、is_order を true にする。「注文」「直送」とだけ書かれた
  手書きの伝票も同じ。kind には表題をそのまま写す
- 文書に書かれていることだけを使う。書かれていないことを推測で補わない
- 品名・数量・金額・納期・日付は文書のとおり一字一句正確に写す。丸めない
- 読み取れない箇所は空文字にする。それらしい値を作らない。様式に金額欄が
  無ければ amount は空文字のままでよい
- 納品日が行ごとに違う様式（納品日と数量の表）なら、items は行ごとに分け、
  name の先頭に納品日を付ける（例: 「6/5(金) 天津栗 焼冷凍10KG/CS」）。
  due には納品日の範囲や一覧を入れる
- 発注してよいか・金額が妥当かなどの判断はしない。内容の整理だけを行う
- FAXは画質が粗いことがある。自信の無い読み取りは summary で
  「（判読しづらい）」と添える
- FAXは上下逆さまに送られてくることがある。逆さまなら rotation に 180 を入れ、
  readable は false にしてよい（無理に読まなくてよい。こちらで回してから読み直す）
"""

# 発注書の通知の末尾に添える一言。翌朝の確認を減らすため、返し方を示しておく
ASK_DONE = "確認・対応が済みましたら、このメッセージへの返信で「対応完了」とお知らせください。"

# 自分の投稿にだけ現れる言い回し。通知も催促も「対応完了」の返し方を書いており、
# その本文自体が「完了」の返事と誤読されないようにする
_OWN_MARK = "「対応完了」と"

# 保留（時間外・上限待ち）の上限。迷惑FAXの洪水で状態ファイルが膨らまないように
MAX_PENDING = 200


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
        now: Callable[[], datetime] | None = None,
        phrasebook: Any | None = None,
    ) -> None:
        self._config = config
        self._chatwork = chatwork
        self._client = client
        self._cost = cost
        self._now = now or (lambda: datetime.now(JST))
        self._phrasebook = phrasebook
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

        # 新着は時間帯に関係なく拾って取っておく（APIは直近100件しか返さないため、
        # 週末をまたぐと取りこぼす）。知らせるのは時間帯の中だけ
        self._collect(state, messages, notifier)
        self._close_finished(state, messages, room_id, notifier)
        self._ack_evening_replies(state, messages, room_id, notifier)
        self._save_state(state)

        now = self._now()
        handled = 0
        if in_notify_window(now, settings.get("notify_window") or {}):
            handled = self._deliver(state, room_id, notifier, now)
            self._follow_up(state, room_id, notifier, now)
            self._evening_report(state, room_id, now)
            self._save_state(state)
        return handled

    # --- 内部 ---

    @staticmethod
    def _notices_by_filename(messages: list[dict], notifier: int) -> dict[str, dict[str, str]]:
        """通知本文（送信元番号など）を、添付とファイル名で結びつける。"""
        table: dict[str, dict[str, str]] = {}
        for message in messages:
            if _account_of(message) != notifier:
                continue
            body = str(message.get("body", ""))
            if "[download:" in body:
                continue
            parsed = parse_notice(body)
            if parsed["filename"]:
                table[parsed["filename"].lower()] = parsed
        return table

    def _collect(self, state: dict, messages: list[dict], notifier: int) -> None:
        """未読のPDF添付を保留リストへ入れ、既読位置を進める。"""
        notices = self._notices_by_filename(messages, notifier)
        last_seen = int(state.get("last_seen", 0))
        newest = last_seen
        pending = state.setdefault("pending", [])
        known = {str(p.get("message_id")) for p in pending} | set(state.get("done") or [])
        # 先に控えた分で通知本文が取れていなければ、今回見えた通知で埋める
        for item in pending:
            if not (item.get("notice") or {}).get("received_at"):
                found = notices.get(str(item["attachment"].get("filename", "")).lower())
                if found:
                    item["notice"] = found
        for message in messages:
            mid = int(message.get("message_id", 0))
            newest = max(newest, mid)
            if mid <= last_seen or _account_of(message) != notifier:
                continue
            body = str(message.get("body", ""))
            attachment = parse_attachment(body)
            if attachment is None or str(mid) in known:
                continue
            notice = notices.get(attachment["filename"].lower()) or {}
            if not notice.get("received_at"):
                # 通知本文と添付が1通にまとまっている形式（実例 2026-09-07 の 4971_001.pdf）
                own = parse_notice(body)
                if own.get("received_at") or own.get("filename"):
                    notice = own
            pending.append(
                {
                    "message_id": str(mid),
                    "account_id": notifier,
                    "attachment": attachment,
                    "notice": notice,
                }
            )
        if len(pending) > MAX_PENDING:
            print(f"[warn] 保留中のFAXが{len(pending)}件あり、古い分を切り捨てます", flush=True)
            del pending[:-MAX_PENDING]
        state["last_seen"] = newest

    def _deliver(self, state: dict, room_id: int, notifier: int, now: datetime) -> int:
        """保留中のFAXを読んで知らせる。"""
        settings = self._config.fax_watch
        today = now.strftime("%Y%m%d")
        if state.get("date") != today:
            state["date"], state["count"] = today, 0
        limit = int(settings.get("max_per_day", 50))
        follow = (settings.get("follow_up") or {}).get("enabled", True)

        handled = 0
        seen = state.setdefault("handled", {})  # ファイル名 → 直近の処理（同じFAXの再投稿を二度読まない）
        for item in sorted(list(state.get("pending") or []), key=lambda p: int(p["message_id"])):
            if state["count"] >= limit:
                print(f"[warn] FAXの1日の処理上限（{limit}件）に達しました。残りは明日知らせます", flush=True)
                break
            state["pending"] = [p for p in state["pending"] if p["message_id"] != item["message_id"]]
            state.setdefault("done", []).append(item["message_id"])
            state["done"] = state["done"][-500:]
            filename = str(item["attachment"].get("filename", "")).lower()
            prior = seen.get(filename) or {}
            if prior.get("readable") and now.timestamp() - float(prior.get("ts", 0)) < 24 * 3600:
                # 同じファイル名を読めた直後の再投稿。読み直さず、その旨だけ静かに置く
                # （読めなかった分の再送は読み直す）
                self._save_state(state)
                self._note_duplicate(room_id, item, prior)
                continue
            state["count"] += 1
            self._save_state(state)  # 呼ぶ前に数える（失敗しても消費は起きるため）
            try:
                result = self._handle(room_id, item, follow)
                handled += 1
            except Exception:
                print("[error] FAXの処理に失敗: " + traceback.format_exc(), flush=True)
                self._report_failure(room_id, item)
                continue
            seen[filename] = {
                "ts": now.timestamp(),
                "readable": bool(result.get("readable")),
                "message_id": item["message_id"],
                "at": now.strftime("%H:%M"),
            }
            if len(seen) > 300:
                for key in sorted(seen, key=lambda k: float(seen[k].get("ts", 0)))[: len(seen) - 300]:
                    seen.pop(key, None)
            if follow and result.get("is_order"):
                state.setdefault("open", []).append(
                    {
                        "posted_id": result["posted_id"],
                        "pdf_id": item["message_id"],
                        "filename": result["filename"],
                        "sender": result["sender"],
                        "kind": result["kind"],
                        "received_at": (item.get("notice") or {}).get("received_at", ""),
                        "posted_at": now.isoformat(),
                        "stage": 0,
                        "check_ids": [],
                    }
                )
                self._save_state(state)
        return handled

    def _handle(self, room_id: int, item: dict, ask_done: bool) -> dict[str, Any]:
        settings = self._config.fax_watch
        notice = item.get("notice") or {}
        data, filename = self._download(room_id, item["attachment"])
        found = self._read(data, filename)
        usage = found.pop("_usage", {})
        rotated = 0
        # 逆さま・判読不能なら、回してもう一度だけ読む（向きが分からなければ180）
        rotation = int(found.get("rotation") or 0)
        if rotation or not found.get("readable", True):
            turned = rotate_pdf(data, rotation or 180)
            if turned is not None:
                again = self._read(turned, filename)
                usage = _merge_usage(usage, again.pop("_usage", {}))
                if again.get("readable", True) or not found.get("readable", True):
                    found, rotated = again, rotation or 180
        readable = bool(found.get("readable", True))

        # 差出人: 台帳で引けたらそれを正とする。無ければ文書の記載
        number = notice.get("sender_number", "")
        partner = self._directory.lookup(number) if number else ""
        if partner:
            sender, basis = partner, f"FAX番号 {number} を台帳で照合"
        elif found.get("sender"):
            sender, basis = str(found["sender"]), "FAXの記載から"
        else:
            sender, basis = "", ""

        kind = str(found.get("kind") or "その他").strip() if readable else "不明"
        category = str(found.get("category") or "").strip()
        # 注文かどうかは、モデルの判定・性質・表題の語の3つのどれかで拾う（見落とし防止）
        order_words = tuple(settings.get("order_words") or DEFAULT_ORDER_WORDS)
        is_order = readable and (
            bool(found.get("is_order")) or category == "注文" or looks_like_order(kind, order_words)
        )
        if is_order:
            found = {**found, "is_order": True}
        if readable:
            text = self._compose(found, sender, basis, number, notice, filename, ask_done and is_order)
        else:
            text = self._compose_unreadable(found, notice, filename)
        from .answer import sanitize_for_chatwork

        reply_tag = f"[rp aid={int(item.get('account_id', 0))} to={room_id}-{item['message_id']}]"
        # 案内・広告などは呼び出さず、ルームに置くだけ（To を付けると通知が鳴る）。
        # 読めなかったものは発注書かもしれないので呼び出す
        mention = set(settings.get("mention_kinds") or [])
        heads = self._heads() if (is_order or not readable or category in mention or kind in mention) else ""
        parts = [reply_tag] + ([heads] if heads else []) + [sanitize_for_chatwork(text)]
        posted_id = self._chatwork.send_message(room_id, "\n".join(parts))
        self._audit(
            {
                "type": "fax_notice",
                "room_id": room_id,
                "message_id": str(item["message_id"]),
                "filename": filename,
                "sender": sender,
                "basis": basis,
                "kind": kind,
                "category": category,
                "is_order": is_order,
                "readable": readable,
                "rotated": rotated,
                "usage": usage,
            }
        )
        return {
            "posted_id": str(posted_id or ""),
            "is_order": is_order,
            "readable": readable,
            "sender": sender,
            "kind": kind,
            "filename": filename,
        }

    @staticmethod
    def _compose_unreadable(found: dict, notice: dict, filename: str) -> str:
        """回しても読めなかったとき。黙って「その他」にせず、人に見てもらう。"""
        lines = [
            f"FAX「{filename}」が届きましたが、こちらでは内容を読み取れませんでした（不鮮明などのため）。",
            "お手数ですがPDFを直接ご確認ください。",
        ]
        seen = [str(found.get(k) or "").strip() for k in ("summary", "notes")]
        seen = [s for s in seen if s]
        if seen:
            lines.append("")
            lines.append("読み取れた範囲: " + " ".join(seen))
        lines.append("")
        meta = [f"ファイル: {filename}"]
        received = notice.get("received_at", "")
        if received:
            meta.append(f"受信 {received}")
        lines.append("（" + "／".join(meta) + "）")
        return "\n".join(lines)

    # --- 発注書の見届け ---

    def _close_finished(self, state: dict, messages: list[dict], room_id: int, notifier: int) -> None:
        """「対応完了」の返事があった発注書を、見届けの対象から外す。"""
        threads = state.get("open") or []
        if not threads:
            return
        me = self._me()
        # 夕方の一覧への返信は、載っている発注書すべてへの返事として受ける
        evening_ids = {str(i) for i in state.get("evening_ids") or []}
        filenames = [str(t.get("filename") or "") for t in threads if t.get("filename")]
        remaining = []
        closers: dict[str, dict] = {}
        for thread in threads:
            ids = {str(thread.get("posted_id")), str(thread.get("pdf_id"))} | {
                str(c) for c in thread.get("check_ids") or []
            } | evening_ids
            since = _as_int(thread.get("posted_id"))
            filename = str(thread.get("filename") or "")
            closer = None
            for message in messages:
                mid = int(message.get("message_id", 0))
                if mid <= since or _account_of(message) in (notifier, me):
                    continue
                body = str(message.get("body", ""))
                if _OWN_MARK in body:
                    continue  # 自分の投稿（自IDが取れなかったときの保険）
                targets = reply_targets(body, room_id)
                if targets and not (targets & ids):
                    continue  # 別のFAXへの返事
                # ファイル名を挙げて書かれた報告は、そのFAXだけの話として読む
                named = [f for f in filenames if f and f in body]
                if named and filename not in named:
                    continue
                if is_completion(body):
                    closer = message
                    break
            if closer is None:
                remaining.append(thread)
                continue
            self._audit(
                {
                    "type": "fax_done",
                    "room_id": room_id,
                    "message_id": str(thread.get("pdf_id")),
                    "filename": thread.get("filename"),
                    "by": _account_of(closer),
                    "stage": int(thread.get("stage", 0)),
                }
            )
            closers.setdefault(str(closer.get("message_id")), closer)
        state["open"] = remaining
        # 報告には一言返す。1つの「完了」で複数の発注書が閉じても、お礼は1回。
        # そのとき、まだ対応待ちのFAXがあれば一緒に示す（月曜朝にまとめて届いた
        # 分の処理漏れを防ぐ）
        for closer in closers.values():
            self._thank(room_id, closer, remaining)

    def _thank(self, room_id: int, message: dict, remaining: list[dict]) -> None:
        """完了の報告に、相手のメッセージへの返信で礼を言い、残りを添える（黙って閉じない）。"""
        try:
            if self._phrasebook is None:
                from .phrasing import build

                self._phrasebook = build(self._config)
            tag = f"[rp aid={_account_of(message)} to={room_id}-{message.get('message_id')}]"
            lines = [self._phrasebook.pick("fax_done_thanks", scope=str(room_id))]
            lines += self._remaining_lines(remaining)
            self._chatwork.send_message(room_id, f"{tag}\n" + "\n".join(lines))
        except Exception:
            print("[warn] 完了報告への返事に失敗: " + traceback.format_exc(), flush=True)

    _thread_line = staticmethod(lambda thread: thread_line(thread))

    def _remaining_lines(self, remaining: list[dict]) -> list[str]:
        if not remaining:
            return ["対応待ちのFAXは、これでありません。"]
        lines = [f"対応待ちのFAXは、あと{len(remaining)}件です。"]
        lines += ["・" + self._thread_line(t) for t in remaining]
        return lines

    def _evening_report(self, state: dict, room_id: int, now: datetime) -> None:
        """業務終了の時刻に、対応完了の返信がないFAXの一覧を出して確認を求める（1日1回）。"""
        settings = self._config.fax_watch.get("follow_up") or {}
        clock_text = settings.get("evening_time")
        if not clock_text or not settings.get("enabled", True):
            return
        today = now.strftime("%Y%m%d")
        if state.get("evening_reported") == today or not is_business_day(now.date()):
            return
        if now < at_clock(now.date(), parse_clock(clock_text, (19, 0))):
            return
        state["evening_reported"] = today  # 残りが無い日も「今日は済み」にする
        open_threads = state.get("open") or []
        if not open_threads:
            return
        lines = [
            f"お疲れさまです。{now:%H:%M}時点で、対応完了の返信をいただいていないFAXが{len(open_threads)}件あります。"
        ]
        lines += ["・" + self._thread_line(t) for t in open_threads]
        lines.append(
            "対応済みでしたら「対応完了」とご返信ください（このメッセージへの返信で、まとめてで構いません）。"
        )
        lines.append("未対応のままで問題ないかもあわせてご確認ください。残っている分は翌営業日にもお知らせします。")
        try:
            mid = self._chatwork.send_message(room_id, f"{self._heads(now)}\n" + "\n".join(lines))
        except Exception:
            print("[warn] 夕方の一覧の投稿に失敗: " + traceback.format_exc(), flush=True)
            return
        ids = [str(i) for i in state.get("evening_ids") or []] + [str(mid or "")]
        state["evening_ids"] = [i for i in ids if i][-10:]
        self._audit(
            {
                "type": "fax_evening_report",
                "room_id": room_id,
                "count": len(open_threads),
                "filenames": [t.get("filename") for t in open_threads],
            }
        )

    def _ack_evening_replies(self, state: dict, messages: list[dict], room_id: int, notifier: int) -> None:
        """夕方の一覧への「問題なし」「明日対応します」のような返事に、一言返す（完了以外）。"""
        evening_ids = {str(i) for i in state.get("evening_ids") or []}
        if not evening_ids:
            return
        acked = [str(i) for i in state.get("acked") or []]
        me = self._me()
        for message in messages:
            mid = str(message.get("message_id", ""))
            if mid in acked or _account_of(message) in (notifier, me):
                continue
            body = str(message.get("body", ""))
            if _OWN_MARK in body or not (reply_targets(body, room_id) & evening_ids):
                continue
            if is_completion(body):
                continue  # 完了の報告は _close_finished がお礼を返す
            if me and f"[To:{me}]" in body:
                acked.append(mid)
                continue  # メンション付きなら webhook 側（FaxStatus）が会話として返している
            acked.append(mid)
            try:
                tag = f"[rp aid={_account_of(message)} to={room_id}-{mid}]"
                self._chatwork.send_message(
                    room_id,
                    f"{tag}\n承知しました。残っている分は、対応完了の返信をいただくまで翌営業日にもお知らせします。",
                )
            except Exception:
                print("[warn] 夕方の一覧への返事に失敗: " + traceback.format_exc(), flush=True)
        state["acked"] = acked[-50:]

    def _follow_up(self, state: dict, room_id: int, notifier: int, now: datetime) -> None:
        """返事の無い発注書を、翌営業日の朝と昼に一度ずつ聞く。"""
        settings = (self._config.fax_watch.get("follow_up") or {})
        if not settings.get("enabled", True):
            return
        check_clock = parse_clock(settings.get("check_time"), (9, 0))
        recheck_clock = parse_clock(settings.get("recheck_time"), (12, 0))
        gap = timedelta(
            minutes=(recheck_clock[0] * 60 + recheck_clock[1]) - (check_clock[0] * 60 + check_clock[1])
        )
        if gap <= timedelta(0):
            gap = timedelta(hours=3)

        max_open_days = int(settings.get("max_open_days", 14))
        remaining = []
        for thread in state.get("open") or []:
            stage = int(thread.get("stage", 0))
            posted_at = _parse_iso(thread.get("posted_at")) or now
            if now - posted_at >= timedelta(days=max_open_days):
                # いつまでも一覧に載せ続けない。外したことは記録に残す
                self._audit(
                    {"type": "fax_expired", "room_id": room_id, "filename": thread.get("filename")}
                )
                continue
            if stage == 0:
                due = at_clock(next_business_day(posted_at.date()), check_clock)
                if now >= due:
                    mid = self._post_check(room_id, notifier, thread, now, first=True)
                    thread["stage"] = 1
                    thread["checked_at"] = now.isoformat()
                    thread.setdefault("check_ids", []).append(mid)
                remaining.append(thread)
            elif stage == 1:
                checked_at = _parse_iso(thread.get("checked_at")) or posted_at
                due = at_clock(checked_at.date(), recheck_clock)
                if due <= checked_at:  # 朝の確認が遅れた日は、そこから同じ間隔を空ける
                    due = checked_at + gap
                if now >= due:
                    self._post_check(room_id, notifier, thread, now, first=False)
                    # 催促は2度で打ち切るが、対応待ちとしては残す（夕方の一覧と
                    # お礼の返信で示し続け、完了の返事で外れる）
                    thread["stage"] = 2
                    thread["rechecked_at"] = now.isoformat()
                remaining.append(thread)
            else:
                remaining.append(thread)
        state["open"] = remaining

    def _post_check(self, room_id: int, notifier: int, thread: dict, now: datetime, first: bool) -> str:
        from .answer import sanitize_for_chatwork

        subject = self._describe(thread)
        if first:
            greeting = "おはようございます。" if now.hour < 11 else "お疲れさまです。"
            text = (
                f"{greeting}\n{subject}ですが、確認と対応は完了していますでしょうか。\n"
                "済んでいましたら、このメッセージへの返信で「対応完了」とお知らせください。"
            )
        else:
            text = (
                f"たびたび失礼します。\n{subject}について、先ほどの確認にもまだお返事がないようです。"
                "確認漏れになっていないでしょうか。\n対応が済んでいましたら「対応完了」とご返信ください。"
            )
        reply_tag = f"[rp aid={notifier} to={room_id}-{thread.get('pdf_id')}]"
        mid = self._chatwork.send_message(
            room_id, f"{reply_tag}\n{self._heads(now)}\n" + sanitize_for_chatwork(text)
        )
        self._audit(
            {
                "type": "fax_check",
                "room_id": room_id,
                "message_id": str(thread.get("pdf_id")),
                "filename": thread.get("filename"),
                "stage": 1 if first else 2,
            }
        )
        return str(mid or "")

    @staticmethod
    def _describe(thread: dict) -> str:
        day = _parse_received(thread.get("received_at"))
        if day is None:
            posted = _parse_iso(thread.get("posted_at"))
            day = posted.date() if posted else None
        when = f"{day.month}月{day.day}日に" if day else ""
        sender = str(thread.get("sender") or "").strip()
        kind = str(thread.get("kind") or "FAX")
        filename = str(thread.get("filename") or "")
        if sender:
            return f"{when}{sender}から届いた{kind}（{filename}）"
        return f"{when}届いた{kind}（{filename}・差出人不明）"

    # --- 部品 ---

    def _recipients(self, now: datetime | None = None) -> list[int]:
        """その日に呼び出す相手。勤務日でない人には To を付けない（休みの日に鳴らさない）。

        notify_recipients（account_id・name・work_days 0=月）があればそれを使い、
        無ければ notify_account_ids を毎日の相手として使う。誰も勤務日でない日は全員。
        """
        settings = self._config.fax_watch
        detailed = settings.get("notify_recipients") or []
        if not detailed:
            return [int(a) for a in (settings.get("notify_account_ids") or [])]
        weekday = (now or self._now()).weekday()
        everyone = [int(r.get("account_id", 0)) for r in detailed if r.get("account_id")]
        working = [
            int(r["account_id"])
            for r in detailed
            if r.get("account_id") and weekday in {int(d) for d in (r.get("work_days") or range(5))}
        ]
        return working or everyone

    def _heads(self, now: datetime | None = None) -> str:
        return " ".join(f"[To:{a}]" for a in self._recipients(now))

    def _me(self) -> int:
        try:
            return int(self._chatwork.get_me())
        except Exception:
            print("[warn] 自アカウントIDの取得に失敗: " + traceback.format_exc(), flush=True)
            return 0

    def _download(self, room_id: int, attachment: dict) -> tuple[bytes, str]:
        info = self._chatwork.get_file_info(room_id, int(attachment["file_id"]))
        # 名前は通知と対にした添付欄のものを使う（受信通知の「ファイル名」と同じ表記）
        filename = str(attachment.get("filename") or info.get("filename") or "fax.pdf")
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
            found = {
                "sender": "", "kind": "その他", "category": "その他", "is_order": False, "summary": "",
                "items": [], "due": "", "notes": "", "rotation": 0, "readable": False,
            }
        found["_usage"] = usage
        return found

    @staticmethod
    def _compose(
        found: dict,
        sender: str,
        basis: str,
        number: str,
        notice: dict,
        filename: str,
        ask_done: bool = False,
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
            # 金額欄の無い様式（納品日と数量だけの注文票など）では、金額を
            # 「読み取れず」と書かない。1行でも値がある列だけ空欄を指摘する
            columns = [
                key for key in ("quantity", "amount")
                if any(str(i.get(key, "") or "").strip() for i in items)
            ]
            for item in items:
                bits = [str(item.get("name", "")).strip()]
                for key in columns:
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
        if ask_done:
            lines.append(ASK_DONE)
        return "\n".join(lines)

    def _note_duplicate(self, room_id: int, item: dict, prior: dict) -> None:
        """同じファイル名の再投稿に、読み直していないことだけ書く（To は付けない）。"""
        filename = (item.get("attachment") or {}).get("filename", "")
        try:
            tag = f"[rp aid={int(item.get('account_id', 0))} to={room_id}-{item['message_id']}]"
            self._chatwork.send_message(
                room_id,
                f"{tag}\nこのFAX（{filename}）は{prior.get('at', '先ほど')}に知らせた分と同じファイル名のため、改めては読んでいません。",
            )
        except Exception:
            print("[warn] 重複の知らせに失敗: " + traceback.format_exc(), flush=True)
        self._audit(
            {"type": "fax_duplicate", "room_id": room_id, "message_id": str(item["message_id"]), "filename": filename}
        )

    def _report_failure(self, room_id: int, item: dict) -> None:
        """読めなかったことは黙らずに知らせる（届いたのに気づかれないのを防ぐ）。"""
        try:
            filename = (item.get("attachment") or {}).get("filename", "")
            self._chatwork.send_message(
                room_id,
                f"{self._heads()}\nFAX「{filename}」が届いていますが、"
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
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {"last_seen": None}
        for key, empty in (("done", []), ("pending", []), ("open", [])):
            state.setdefault(key, empty)
        state.setdefault("date", "")
        state.setdefault("count", 0)
        return state

    def _save_state(self, state: dict) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError:
            print("[warn] FAX巡回の状態を保存できませんでした", flush=True)


def thread_line(thread: dict) -> str:
    """対応待ちのFAX1件の短い説明。「9/5 17:55 有限会社珍味屋 発注書（4955_001.pdf）」"""
    received = str(thread.get("received_at") or "")
    when = ""
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            at = datetime.strptime(received, fmt)
            when = f"{at.month}/{at.day} {at:%H:%M} "
            break
        except ValueError:
            continue
    sender = str(thread.get("sender") or "").strip() or "差出人不明"
    kind = str(thread.get("kind") or "FAX")
    return f"{when}{sender} {kind}（{thread.get('filename', '')}）"


# 「未処理の注文書残ってる？」のような、対応状況を尋ねる言い方
_STATUS_RE = re.compile(
    r"(未処理|未対応|残って|残り|対応待ち|溜まって|たまって|一覧|状況|何件|ある[？?]|あります|抱えて|どれ)"
)


class FaxStatus:
    """FAXルームでのメンションに、対応状況だけを答える（ハンドブックは使わない）。

    FAXルームは許可ルーム（Q&A）に入れていない（他部署のルームへ社内ナレッジが
    流れる経路を作らないため）。それでも「未処理の注文書残ってる？」と聞かれて
    無言なのは不親切なので、見張りの状態ファイルから対応待ちの一覧だけを返す。
    実例（2026-09-07 19:23）: 足立さんの問いに返事が無かった。
    """

    GUIDE = (
        "このルームではFAXの対応状況だけお答えしています。"
        "「未処理のFAXある？」のように聞いてください。ほかのご相談は経理財務部のルームでお願いします。"
    )

    CHAT_SYSTEM = """あなたは株式会社ライズクリエイション経理財務部のアシスタント「{agent_name}」です。
いまいるのはFAX受信通知のルームで、相手は経理財務部のメンバーです。
このルームでは、届いたFAXの通知と、その対応状況のやり取りだけをしています。

相手のメッセージに、同僚として自然に短く（1〜2文、です・ます調）返してください。
- 「了解です」「ありがとう」「お疲れさま」のような一言には、一言で返す。説明や案内を足さない
- 相手の言葉をそのまま返さない。「了解です」に「了解です」と返すのは会話になっていない。
  相手の一言を受けて、こちらからの一言（「はい、また届いたらお知らせします」
  「こちらこそ確認ありがとうございます」など）を返す
- 業務の手順・金額・社内ルールなど知識を求める質問には答えず、「経理財務部のルームで聞いてほしい」と
  一言添える（このルームでは答えられないため）
- FAXの対応状況の問い合わせはプログラム側が答えるので、ここでは扱わない
- 値・日付・件数を作らない。同じ言い回しを続けない。「お待たせしました」のような、
  待たせていないのに詫びる言い方はしない

例:
- こちら「いま対応待ちのFAXはありません。」→ 相手「了解です。」→「はい。また届いたらお知らせしますね。」
- 相手「ありがとう」→「こちらこそ、ご確認ありがとうございます。」
- 相手「お疲れさまです」→「お疲れさまです。今日も何かあればお知らせください。」
"""

    def __init__(
        self,
        config: Any,
        now: Callable[[], datetime] | None = None,
        client: Any | None = None,
    ) -> None:
        self._config = config
        self._now = now or (lambda: datetime.now(JST))
        self._client = client
        self._state_path = config.resolve_path(config.state_dir) / "faxwatch.json"

    def owns(self, room_id: int) -> bool:
        settings = self._config.fax_watch
        return bool(settings.get("enabled")) and int(room_id) == int(settings.get("room_id", 0) or 0)

    def reply(self, question: str, replied_to: str = "") -> tuple[str, dict]:
        """→ (返信, usage)。対応状況の問い合わせはコードで、それ以外の会話はモデルで返す。"""
        if _STATUS_RE.search(str(question or "")):
            return self._status(), {}
        return self._chat(question, replied_to)

    def _chat(self, question: str, replied_to: str) -> tuple[str, dict]:
        """「了解です」「ありがとう」のような一般の会話に、同僚として短く返す。

        実例（2026-09-07 20:07）: 「了解です。」に「このルームでは…」と案内を返して
        しまった。会話の範疇のものは会話で返す。
        """
        from .answer import _call_with_continuation

        try:
            if self._client is None:
                import anthropic

                self._client = anthropic.Anthropic()
            cfg = self._config
            kwargs = {
                "model": cfg.model,
                "max_tokens": int(cfg.max_tokens),
                "output_config": {
                    "effort": "low",
                    "format": {
                        "type": "json_schema",
                        "schema": {
                            "type": "object",
                            "properties": {"reply": {"type": "string", "description": "相手への返事。1〜2文"}},
                            "required": ["reply"],
                            "additionalProperties": False,
                        },
                    },
                },
                "system": self.CHAT_SYSTEM.format(agent_name=cfg.agent_name),
            }
            prompt = f"相手のメッセージ:\n{question}"
            if replied_to:
                prompt = f"こちらの直前の発言（相手はこれに返信している）:\n{replied_to}\n\n" + prompt
            response, usage, _ = _call_with_continuation(
                self._client.messages.create, kwargs, [{"role": "user", "content": prompt}]
            )
            text = next(
                (b.text for b in getattr(response, "content", []) if getattr(b, "type", "") == "text"), ""
            )
            reply = str(json.loads(text).get("reply") or "").strip()
            if not reply or is_parrot(question, reply):
                # 実例（2026-09-07 20:22）: 「了解です。」に「了解です。」と返した。
                # 繰り返しは会話になっていないので、こちらからの一言に差し替える
                reply = self._phrasebook().pick("fax_room_ack", scope=str(self._config.fax_watch.get("room_id", "")))
            return reply, usage
        except Exception:
            print("[warn] FAXルームでの会話の返答に失敗: " + traceback.format_exc(), flush=True)
            return self.GUIDE, {}

    def _phrasebook(self) -> Any:
        if getattr(self, "_book", None) is None:
            from .phrasing import build

            self._book = build(self._config)
        return self._book

    def _status(self) -> str:
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        open_threads = state.get("open") or []
        pending = state.get("pending") or []
        lines: list[str] = []
        if open_threads:
            lines.append(f"いま対応完了の返信をいただいていないのは{len(open_threads)}件です。")
            lines += ["・" + thread_line(t) for t in open_threads]
            lines.append("対応済みでしたら、それぞれの通知への返信で「対応完了」とお知らせください。")
        else:
            lines.append("いま対応待ちのFAXはありません。")
        if pending:
            when = delivery_phrase(self._now(), self._config.fax_watch.get("notify_window") or {})
            timing = "順次お知らせいたします" if when == "順次" else f"{when}にお知らせいたします"
            lines.append(f"ほかに、対応時間外に届いているFAXが{len(pending)}件あり、こちらは{timing}。")
        return "\n".join(lines)


_PUNCT_RE = re.compile(r"[\s、。．,.!！?？~〜ー…・「」『』()（）]+")


def is_parrot(question: str, reply: str) -> bool:
    """返事が相手の言葉の繰り返しか（「了解です」に「了解です」）。"""
    q = _PUNCT_RE.sub("", unicodedata.normalize("NFKC", str(question or ""))).lower()
    r = _PUNCT_RE.sub("", unicodedata.normalize("NFKC", str(reply or ""))).lower()
    if not q or not r:
        return False
    if q == r:
        return True
    shorter, longer = (q, r) if len(q) <= len(r) else (r, q)
    # 短い方が長い方にそのまま含まれていて、足されているのが数文字だけなら繰り返し
    return shorter in longer and len(longer) - len(shorter) <= 4


def next_delivery(now: datetime, window: dict) -> datetime:
    """控えているFAXを次に知らせる時刻（通知の時間帯の頭）。時間帯の中なら今。"""
    start = parse_clock(window.get("start"), (8, 30))
    day = now.date()
    if is_business_day(day):
        if now < at_clock(day, start):
            return at_clock(day, start)
        if in_notify_window(now, window):
            return now
    day += timedelta(days=1)
    while not is_business_day(day):
        day += timedelta(days=1)
    return at_clock(day, start)


def delivery_phrase(now: datetime, window: dict) -> str:
    """次に知らせるタイミングの言い方。「明日の朝一」「来週月曜日の朝一」「本日08:30」「順次」。

    金曜の夜に「明日」と言わない（人の会話として日付に則した言い方にする）。
    """
    target = next_delivery(now, window)
    if target <= now:
        return "順次"
    today, day = now.date(), target.date()
    if day == today:
        return f"本日{target:%H:%M}"
    if day == today + timedelta(days=1):
        return "明日の朝一"
    if day.weekday() == 0:
        return "来週月曜日の朝一"
    return f"{WEEKDAYS[day.weekday()]}曜日の朝一"


def _account_of(message: dict) -> int:
    return int((message.get("account") or {}).get("account_id", 0) or 0)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
