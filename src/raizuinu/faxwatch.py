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


_TO_RE = re.compile(r"\[To:(\d+)\]")
SHORT_REPORT_CHARS = 20


def is_short_report(body: str, me: int = 0) -> bool:
    """返信でもファイル名指定でもない文を、完了の報告として受けてよいか。

    「2件とも対応完了です」のような短い一言だけ。誰か（自分以外）へのメンションが
    付いた文や長い文は、周知や別の話なので受けない。
    """
    others = [int(a) for a in _TO_RE.findall(str(body or "")) if int(a) != int(me or 0)]
    if others:
        return False
    return len(strip_tags(body).strip()) <= SHORT_REPORT_CHARS


# --- 通知の番号（①②…）---
# 朝のまとめ通知は1通に複数のFAXを載せるので、どれが済んだかを番号で返してもらう。
# 番号はその日の通知の通し番号（まとめ通知の中は①から、日中の単独通知はその続き）
_CIRCLED_RANGES = ((1, 20, 0x2460), (21, 35, 0x3251), (36, 50, 0x32B1))


def circled(n: int) -> str:
    """1→①、21→㉑、36→㊱。51以上は (51)。"""
    for lo, hi, base in _CIRCLED_RANGES:
        if lo <= n <= hi:
            return chr(base + n - lo)
    return f"({n})"


def _circled_value(ch: str) -> int:
    code = ord(ch)
    for lo, hi, base in _CIRCLED_RANGES:
        if base <= code <= base + (hi - lo):
            return lo + code - base
    return 0


_NUM_FORMS_RE = re.compile(r"(?:No\.?\s*|[#＃])(\d{1,2})|(\d{1,2})\s*番|[(（](\d{1,2})[)）]", re.IGNORECASE)
# 日付・時刻・件数・ファイル名の一部（9/12、10時、2件、4950_001）は番号と読まない
_BARE_NUM_RE = re.compile(r"(?<![\d/:.\-_A-Za-z])(\d{1,2})(?![\d/:.\-_月日円件時分個枚人%])")
_ALL_RE = re.compile(r"(全て|すべて|全部|全件|一式|まとめて|両方|どちらも|いずれも|件とも|つとも|とも(対応|完了|済|確認))")
_EXCEPT_RE = re.compile(r"(以外|除い|除き|のぞい|のぞき)")


def report_numbers(body: str) -> list[int]:
    """返事に書かれた通知の番号（①③、1と3、No.2、(4)）。順番どおり、重複なし。"""
    text = strip_tags(body)
    found: list[int] = []
    kept: list[str] = []
    for ch in text:
        value = _circled_value(ch)
        if value:
            found.append(value)
            kept.append(" ")
        else:
            kept.append(ch)
    plain = unicodedata.normalize("NFKC", "".join(kept))
    for match in _NUM_FORMS_RE.finditer(plain):
        found.append(int(next(g for g in match.groups() if g)))
    plain = _NUM_FORMS_RE.sub(" ", plain)
    found += [int(m.group(1)) for m in _BARE_NUM_RE.finditer(plain)]
    seen: list[int] = []
    for n in found:
        if n and n not in seen:
            seen.append(n)
    return seen


def mentions_all(body: str) -> bool:
    """「全て対応完了」「2件とも済み」のように、載っている分をまとめて済んだと言っているか。"""
    return bool(_ALL_RE.search(unicodedata.normalize("NFKC", strip_tags(body))))


def names_exceptions(body: str) -> bool:
    """「①以外は対応完了」のように除外を含む返事（番号で閉じると逆になる）。"""
    return bool(_EXCEPT_RE.search(unicodedata.normalize("NFKC", strip_tags(body))))


# 「以外」の後ろにこれがあれば、済んでいない分の話が混ざっているので受けない
_PENDING_RE = re.compile(r"(確認中|対応中|処理中|作業中|保留|後で|あとで|後ほど|明日|来週|未|まだ|待ち)")
# 「以外」の前に番号以外の語が無いことを見る（番号・区切りを除いた残り）
_NUMBER_SEPARATORS_RE = re.compile(r"^[\s、,，・/／とや及びおよび番No.()（）#＃]*$", re.IGNORECASE)


def exclusion_numbers(body: str) -> list[int] | None:
    """「①③以外は対応完了」の①③。形が崩れていれば None（聞き返す）。

    「以外」は挙げなかった分を全部閉じるので、読み違いが複数件の取りこぼしになる。
    受けるのは、「以外」の前が番号だけで、後ろに番号や保留の語（確認中・明日など）が
    無いときだけ。「光パックスの①以外」「①以外は完了、③は確認中」は受けない。
    """
    text = strip_tags(body).strip()
    match = _EXCEPT_RE.search(unicodedata.normalize("NFKC", text))
    if not match:
        return None
    # NFKC で長さが変わることがあるので、元の文でも同じ語を探して切る
    raw = re.search("|".join(re.escape(w) for w in ("以外", "除い", "除き", "のぞい", "のぞき")), text)
    if not raw:
        return None
    before, after = text[: raw.start()], text[raw.end():]
    numbers = report_numbers(before)
    if not numbers or report_numbers(after):
        return None
    rest = "".join(" " if _circled_value(ch) else ch for ch in before)
    rest = re.sub(r"\d", " ", unicodedata.normalize("NFKC", rest))
    rest = re.sub(r"No\.?", " ", rest, flags=re.IGNORECASE)
    if not _NUMBER_SEPARATORS_RE.match(rest):
        return None
    if _PENDING_RE.search(unicodedata.normalize("NFKC", after)):
        return None
    return numbers


def pick_by_number(threads: list[dict], numbers: list[int]) -> list[dict]:
    """番号に当たる控え。同じ番号が別の日の通知にもあれば、新しい通知の方を採る。"""
    chosen: list[dict] = []
    for n in numbers:
        hits = [t for t in threads if int(t.get("number") or 0) == n]
        if hits:
            chosen.append(max(hits, key=lambda t: _as_int(t.get("posted_id"))))
    return chosen


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
            "description": (
                "差出人＝このFAXを送ってきた相手側の会社名（団体名）。文書に書かれているとおり。"
                "宛先（こちらのグループ会社）を入れてはならない。分からなければ空文字"
            ),
        },
        "addressee": {
            "type": "string",
            "description": (
                "宛先の社名（「御中」「様」の付いた側。取引先からの発注書なら宛先はこちらの"
                "グループ会社）。読み取れなければ空文字。sender と同じ名前を入れてはならない"
            ),
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
        "urgent": {
            "type": "boolean",
            "description": (
                "「急ぎ」「至急」「早急」「大至急」のように、急いでほしいと文書に明記されていれば true。"
                "納期が近いだけ、定型の「最短納品でお願いします」だけなら false"
            ),
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
        "sender", "addressee", "kind", "category", "is_order", "urgent", "summary", "items", "due", "notes",
        "rotation", "readable",
    ],
    "additionalProperties": False,
}

# 急ぎの注文を見分ける語（表題・要約・備考・納期・品名のどこかにあれば急ぎ）。
# 実例 2026-09-23: ジェットの発注看板に「セール急ぎ分」。楽天軒の森さん・藤田さんにも知らせる
DEFAULT_URGENT_WORDS = ("急ぎ", "至急", "早急", "大至急", "緊急", "特急", "急いで", "急ぐ", "ASAP")


def find_urgent_word(found: dict, words: tuple[str, ...] | list[str]) -> str:
    """読み取り結果のどこかにある急ぎの語（最初に見つかったもの）。無ければ空文字。"""
    parts = [str(found.get(k) or "") for k in ("kind", "summary", "notes", "due")]
    parts += [str(i.get("name") or "") for i in (found.get("items") or []) if isinstance(i, dict)]
    text = unicodedata.normalize("NFKC", " ".join(parts)).lower()
    for word in words:
        needle = unicodedata.normalize("NFKC", str(word or "")).lower()
        if needle and needle in text:
            return str(word)
    return ""

# こちら（受信側）のグループ会社。差出人にこれが書かれていたら宛先と取り違えている
# （実例 2026-09-07〜10: 47件中9件で「RAKUTENKEN株式会社から発注書が届きました」と
# 出た。取引先の発注書は宛先が当社なので、宛先を差出人と読んでいた）
DEFAULT_OWN_COMPANY_WORDS = (
    "ライズクリエイション", "ライズクリエーション", "RAKUTENKEN", "楽天軒", "樂天軒", "ラクテンケン",
    "ヤマトライジング", "イノベイト", "ohirome", "ライズホールディングス",
)

# 差出人を読み直させるときの一言。宛先を差出人と読んだ理由ごと伝える
REREAD_SENDER_HINT = (
    "前回の読み取りでは sender に「{sender}」と書かれていましたが、これはこちら（受信側）の"
    "会社名で、文書の宛先です。FAXを送ってきた相手側の社名を探し直してください"
    "（社判・住所・TEL/FAX・担当者欄・発注者欄。こちらの様式を相手が記入して送り返した場合は、"
    "その相手＝様式上の宛先が差出人）。見当たらなければ sender は空文字にしてください。"
)


def is_own_company(name: str, words: tuple[str, ...] | list[str]) -> bool:
    """差出人として読まれた社名が、こちらのグループ会社か。"""
    text = _loose(name)
    return bool(text) and any(_loose(w) and _loose(w) in text for w in words)


def _flex(word: str) -> str:
    """英数字の全角半角・大文字小文字の違いを許す正規表現の断片。"""
    out = []
    for ch in unicodedata.normalize("NFKC", word):
        if ch.isascii() and ch.isalnum():
            wide = chr(ord(ch) + 0xFEE0)
            variants = {ch.lower(), ch.upper(), wide.lower(), wide.upper()}
            out.append("[" + "".join(re.escape(v) for v in sorted(variants)) + "]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


_CORP = r"(?:株式会社|有限会社|\(株\)|（株）|㈱)?"


def strip_own_company(summary: str, words: tuple[str, ...] | list[str]) -> str:
    """要約から、当社を主語・宛先にした言い回しを外す。

    実例（2026-09-10）: 「RAKUTENKEN株式会社よりJR名古屋高島屋への天津甘栗の直送依頼」。
    注文は当社に来るものなので当社名は要らない（「JR名古屋高島屋への天津甘栗の直送依頼」）。
    商品名に含まれる「樂天軒本店〜」のような語はそのまま残す。
    """
    text = str(summary or "")
    names = [w for w in words if w]
    if not text.strip() or not names:
        return text
    body = "(?:" + "|".join(_flex(w) for w in names) + r")[^\s、。]{0,12}?" + _CORP
    # 文頭の「当社より／から／への」
    head = re.compile(r"^\s*" + _CORP + r"\s*" + body + r"\s*(?:よりの|より|からの|から|への|宛ての|宛の|あての)\s*")
    text = head.sub("", text, count=1)
    # 文中の「〜を当社へ発注」「当社宛に注文」
    mid = re.compile(body + r"\s*(?:へ|宛に|宛てに|あてに|宛|あて)(?=\s*(?:発注|注文|依頼|直送|ご注文|ご依頼))")
    return mid.sub("", text)

# 表題にこれらの語があれば、モデルの判定に関わらず注文として扱う（見落としを防ぐ保険）。
# 実例（2026-09-07）: 直送依頼書・サンプル依頼書・FAX申込書・商品注文伝票・注文表・
# 【発注・入荷】表・発注伝票、それに「注文」「直送」とだけ書かれた伝票で注文が来ていた
DEFAULT_ORDER_WORDS = ("注文", "発注", "直送", "申込", "サンプル", "入荷")


def looks_like_order(title: str, words: tuple[str, ...] | list[str]) -> bool:
    text = unicodedata.normalize("NFKC", str(title or ""))
    return any(w and w in text for w in words)


def _loose(text: Any) -> str:
    """差出人・表題の照合用。全角半角・空白・大文字小文字の違いを無視する。"""
    return "".join(unicodedata.normalize("NFKC", str(text or "")).split()).lower()


def quiet_rule_for(sender: str, title: str, rules: list[dict] | None) -> dict | None:
    """静かに置くだけにする規則（`fax_watch.quiet_rules`）に当たれば、その規則を返す。

    実例（2026-09-10）: G7ジャパンフードサービスの「発注書 情報確認表」は、既にFAXで
    届いた発注書の確認書なので、To と対応完了の確認は要らない（キヨスクの
    「棚卸残数日計表」と同じ扱い）。規則は差出人と表題の両方（書いてある方だけ）が
    含まれるときに当たる。表題だけの規則は差出人を問わない。
    """
    s, t = _loose(sender), _loose(title)
    for rule in rules or []:
        want_sender, want_title = _loose(rule.get("sender")), _loose(rule.get("title"))
        if not want_sender and not want_title:
            continue
        if want_sender and want_sender not in s:
            continue
        if want_title and want_title not in t:
            continue
        return rule
    return None

READ_SYSTEM = """あなたは株式会社ライズクリエイション経理財務部のアシスタントです。
届いたFAXのPDFを読み、差出人と内容を部内へ知らせるために整理します。

厳守すること:
- 差出人（sender）は「このFAXを送ってきた相手側」＝文書の発行元。宛先はこちらの
  グループ会社（{own_companies}）なので、これらを sender に書いてはならない。
  取引先の発注書・直送依頼書は宛先が当社で、差出人は取引先。「御中」「様」の付いた
  社名は宛先。発行元は社判・住所・TEL/FAX・担当者欄・発注者欄から拾う。当社の様式を
  相手が記入して送り返してきた場合も、差出人はその相手（様式上の宛先）。
  宛先は addressee に入れ、sender と同じ名前にしない
- summary に当社（宛先）の社名を書かない。「RAKUTENKEN株式会社より〜」「〜をRAKUTENKEN
  株式会社へ発注」のように書かず、「JR名古屋高島屋への天津甘栗の直送依頼」のように
  相手・納品先・品物だけで書く（注文が当社に来るのは当たり前なので）
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
- 「急ぎ」「至急」「早急」のように急いでほしいと書かれていれば urgent を true にし、
  その語を summary か notes にそのまま残す（例: 「セール急ぎ分」）
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
        # 直前の run_once で「対応完了」により閉じた発注書（webhook側が二重に返さないための目印）
        self.closed_last_run: list[dict] = []
        # 直前の run_once で「どの番号が済んだか」を聞き返した報告（同じく二重に返さないため）
        self.asked_last_run: list[dict] = []
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
        """新しいFAXを処理する。→ 知らせた件数。

        5分ごとの巡回と、通知管理くんのメンションを合図にした即時処理（webhook側）の
        両方から呼ばれる。別プロセス同士が同時に走ると二重に知らせるため、状態ファイル
        の隣のロックで直列にする。
        """
        settings = self._config.fax_watch
        if not settings.get("enabled"):
            return 0
        with self._locked():
            return self._run_once_locked(settings)

    def _locked(self):
        """状態ファイルのロック（Linuxの flock。無い環境では何もしない）。"""
        import contextlib

        try:
            import fcntl
        except ImportError:  # Windows（テスト環境）
            return contextlib.nullcontext()

        @contextlib.contextmanager
        def lock():
            path = self._state_path.with_suffix(".lock")
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

        return lock()

    def _run_once_locked(self, settings: dict) -> int:
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

    def has_pending(self) -> bool:
        """時間外などで控えている分があるか（メンション起点の処理で、見直すかの判断に使う）。"""
        return bool(self._load_state().get("pending"))

    def close_manually(
        self, room_id: int, message: dict, filenames: list[str] | None = None, everything: bool = False
    ) -> int:
        """アシスタント宛の文で「済んだ」と読めたものを閉じ、お礼と残りを1通で返す。→ 閉じた件数

        巡回の規則（返信・ファイル名・短い一言）に当たらない長めの報告でも、
        メンション付きで内容が済んだ報告なら、モデルの読み取りを信じて閉じる。
        """
        wanted = {str(f).strip().lower() for f in (filenames or []) if str(f).strip()}
        if not everything and not wanted:
            return 0
        with self._locked():
            state = self._load_state()
            closed = [
                t for t in state.get("open") or []
                if everything or str(t.get("filename", "")).lower() in wanted
            ]
            if not closed:
                return 0
            remaining = [t for t in state.get("open") or [] if t not in closed]
            state["open"] = remaining
            self._save_state(state)
        for thread in closed:
            self._audit(
                {
                    "type": "fax_done",
                    "room_id": room_id,
                    "message_id": str(thread.get("pdf_id")),
                    "filename": thread.get("filename"),
                    "by": _account_of(message),
                    "stage": int(thread.get("stage", 0)),
                    "via": "mention",
                }
            )
        self.closed_last_run = closed
        self._thank(room_id, message, remaining)
        return len(closed)

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

        seen = state.setdefault("handled", {})  # ファイル名 → 直近の処理（同じFAXの再投稿を二度読まない）
        prepared: list[dict] = []
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
                prepared.append(self._prepare(room_id, item, follow))
            except Exception:
                print("[error] FAXの処理に失敗: " + traceback.format_exc(), flush=True)
                self._report_failure(room_id, item)
                continue
        if not prepared:
            return 0

        # 一度に複数（時間外・土日に控えた分が朝にまとめて出るとき）は1通にまとめ、
        # 番号（①②…）を振って、どれが済んだか番号で返してもらう。1件なら従来どおり
        batch = settings.get("batch") or {}
        if batch.get("enabled", True) and len(prepared) >= 2:
            posted = self._post_batch(room_id, prepared, state, now)
        else:
            posted = [self._post_single(room_id, p, state, now) for p in prepared]

        for p in posted:
            item = p["item"]
            self._audit(
                {
                    "type": "fax_notice",
                    "room_id": room_id,
                    "message_id": str(item["message_id"]),
                    "posted_id": p.get("posted_id", ""),
                    "number": int(p.get("number") or 0),
                    "filename": p["filename"],
                    "sender": p["sender"],
                    "basis": p["basis"],
                    "kind": p["kind"],
                    "category": p["category"],
                    "is_order": p["is_order"],
                    "quiet": bool(p.get("quiet")),
                    "urgent": bool(p.get("urgent")),
                    "urgent_word": p.get("urgent_word", ""),
                    "readable": p["readable"],
                    "rotated": p["rotated"],
                    "usage": p["usage"],
                }
            )
            seen[p["filename"].lower()] = {
                "ts": now.timestamp(),
                "readable": bool(p["readable"]),
                "message_id": item["message_id"],
                "at": now.strftime("%H:%M"),
            }
            if follow and p["is_order"]:
                state.setdefault("open", []).append(
                    {
                        "posted_id": p.get("posted_id", ""),
                        "batch_id": p.get("posted_id", ""),
                        "number": int(p.get("number") or 0),
                        "pdf_id": item["message_id"],
                        "filename": p["filename"],
                        "sender": p["sender"],
                        "kind": p["kind"],
                        "received_at": (item.get("notice") or {}).get("received_at", ""),
                        "posted_at": now.isoformat(),
                        "stage": 0,
                        "check_ids": [],
                        "urgent": bool(p.get("urgent")),
                    }
                )
        if len(seen) > 300:
            for key in sorted(seen, key=lambda k: float(seen[k].get("ts", 0)))[: len(seen) - 300]:
                seen.pop(key, None)
        self._save_state(state)
        return len(posted)

    @staticmethod
    def _take_number(state: dict, now: datetime) -> int:
        """通知の通し番号を1つ取る。

        日付が変わったとき、対応待ちが残っていなければ①に戻し、残っていれば続きから振る
        （前日の①と当日の①が朝の一覧に並ぶと、どちらの話か決まらないため）。
        """
        today = now.strftime("%Y%m%d")
        numbering = state.get("numbering") or {}
        if numbering.get("date") != today:
            carry = bool(state.get("open"))
            numbering = {"date": today, "next": int(numbering.get("next", 1)) if carry else 1}
        n = int(numbering.get("next", 1))
        state["numbering"] = {"date": today, "next": n + 1}
        return n

    def _post_single(self, room_id: int, p: dict, state: dict, now: datetime) -> dict:
        """1件だけの通知（従来の形）。発注書には番号を付ける（夕方の一覧などで番号で指せるように）。"""
        from .answer import sanitize_for_chatwork

        item = p["item"]
        number = self._take_number(state, now) if (p["is_order"] and p["ask_done"]) else 0
        prefix = f"{circled(number)} " if number else ""
        text = self._render(p, prefix, trailer=True, ask_done=p["ask_done"] and p["is_order"])
        reply_tag = f"[rp aid={int(item.get('account_id', 0))} to={room_id}-{item['message_id']}]"
        heads = " ".join(h for h in (self._heads(now) if p["wants_heads"] else "", self._urgent_heads([p])) if h)
        parts = [reply_tag] + ([heads] if heads else []) + [sanitize_for_chatwork(text)]
        posted_id = self._chatwork.send_message(room_id, "\n".join(parts))
        return {**p, "posted_id": str(posted_id or ""), "number": number}

    def _post_batch(self, room_id: int, prepared: list[dict], state: dict, now: datetime) -> list[dict]:
        """複数件を1通にまとめて知らせる。長すぎれば分ける（番号は続き）。"""
        from .answer import sanitize_for_chatwork

        max_chars = int((self._config.fax_watch.get("batch") or {}).get("max_chars", 6000))
        numbered = [(p, self._take_number(state, now)) for p in prepared]
        # 1通にまとめると1件ずつの通知（PDFの投稿への返信）が無いので、各件にPDFのリンクを添える
        blocks = [
            (
                p,
                n,
                self._render(p, f"{circled(n)} ", trailer=False, ask_done=False)
                + f"\nPDF: {message_url(room_id, p['item']['message_id'])}",
            )
            for p, n in numbered
        ]
        chunks: list[list[tuple[dict, int, str]]] = [[]]
        size = 0
        for block in blocks:
            if chunks[-1] and size + len(block[2]) > max_chars:
                chunks.append([])
                size = 0
            chunks[-1].append(block)
            size += len(block[2])

        heads = self._heads(now) if any(p["wants_heads"] for p in prepared) else ""
        total = len(prepared)
        posted: list[dict] = []
        for index, chunk in enumerate(chunks):
            if index == 0:
                header = f"届いているFAXが{total}件あります。まとめてお知らせします。"
            else:
                header = f"（続き {index + 1}/{len(chunks)}）"
            lines = [header, ""]
            for _, _, text in chunk:
                lines.append(text)
                lines.append("")
            if any(p["readable"] for p, _, _ in chunk):
                lines.append("※PDFを読んで整理しています。数量・金額は原本でご確認ください。")
            need = [circled(n) for p, n, _ in chunk if p["is_order"] and p["ask_done"]]
            if need:
                lines.append(
                    f"対応完了の返事が要るのは {''.join(need)} です。"
                    "済みましたら、このメッセージへの返信で番号を添えて「対応完了」とお知らせください"
                    f"（例:「{need[0]} 対応完了」）。全件済みでしたら「全て対応完了」で結構です。"
                )
            body = sanitize_for_chatwork("\n".join(lines))
            top = " ".join(h for h in (heads, self._urgent_heads([p for p, _, _ in chunk])) if h)
            posted_id = str(self._chatwork.send_message(room_id, (top + "\n" if top else "") + body) or "")
            posted += [{**p, "posted_id": posted_id, "number": n} for p, n, _ in chunk]
        return posted

    def _read_document(
        self, data: bytes, filename: str, need_sender: bool = True
    ) -> tuple[dict[str, Any], dict, int, bool]:
        """PDFを読む。→ (読み取り結果, 使用量, 回した角度, 差出人が当社名しか取れなかったか)。

        逆さま・判読不能なら回してもう一度読む。差出人に当社（宛先）の社名が入っていたら、
        need_sender のときだけ理由を添えて一度読み直させ、それでも当社なら差出人を空にする。
        """
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
                    found, rotated, data = again, rotation or 180, turned
        own_only = False
        own_words = self._own_words()
        if need_sender and found.get("readable", True) and is_own_company(str(found.get("sender") or ""), own_words):
            # 宛先を差出人と読んでいる。理由を添えて読み直させる
            hint = REREAD_SENDER_HINT.format(sender=str(found.get("sender") or ""))
            again = self._read(data, filename, hint=hint)
            usage = _merge_usage(usage, again.pop("_usage", {}))
            if again.get("readable", True) and not is_own_company(str(again.get("sender") or ""), own_words):
                found = again
            else:
                found, own_only = {**found, "sender": ""}, True
        found = {**found, "summary": strip_own_company(str(found.get("summary") or ""), own_words)}
        return found, usage, rotated, own_only

    def _prepare(self, room_id: int, item: dict, ask_done: bool) -> dict[str, Any]:
        """PDFを取って読み、通知に要る材料をそろえる（投稿はしない）。"""
        settings = self._config.fax_watch
        notice = item.get("notice") or {}
        data, filename = self._download(room_id, item["attachment"])
        # 差出人: 台帳で引けたらそれを正とする。無ければ文書の記載
        number = notice.get("sender_number", "")
        partner = self._directory.lookup(number) if number else ""
        found, usage, rotated, own_only = self._read_document(data, filename, need_sender=not partner)
        readable = bool(found.get("readable", True))
        if partner:
            sender, basis = partner, f"FAX番号 {number} を台帳で照合"
        elif found.get("sender"):
            sender, basis = str(found["sender"]), "FAXの記載から"
        elif own_only:
            sender, basis = "", "文書には宛先の当社名しか見当たりません"
        else:
            sender, basis = "", ""

        kind = str(found.get("kind") or "その他").strip() if readable else "不明"
        category = str(found.get("category") or "").strip()
        # 注文かどうかは、モデルの判定・性質・表題の語の3つのどれかで拾う（見落とし防止）
        order_words = tuple(settings.get("order_words") or DEFAULT_ORDER_WORDS)
        is_order = readable and (
            bool(found.get("is_order")) or category == "注文" or looks_like_order(kind, order_words)
        )
        # 読めた文書で、差出人と表題が「静かに置くだけ」の規則に当たれば、注文扱いも To も外す
        # （既に届いた発注書の確認書・毎日の定例表など。読めなかった分は規則を当てない）
        quiet = quiet_rule_for(sender, kind, settings.get("quiet_rules")) if readable else None
        if quiet:
            is_order = False
        found = {**found, "is_order": is_order}
        # 案内・広告などは呼び出さず、ルームに置くだけ（To を付けると通知が鳴る）。
        # 読めなかったものは発注書かもしれないので呼び出す
        mention = set(settings.get("mention_kinds") or [])
        wants_heads = (is_order or not readable or category in mention or kind in mention) and not quiet
        # 急ぎの注文は、経理のほかに楽天軒側（urgent.extra_recipients）にも知らせる。
        # 語で見つけるのを主にし、モデルの urgent は取りこぼしの保険
        urgent_cfg = settings.get("urgent") or {}
        urgent_word = find_urgent_word(found, urgent_cfg.get("words") or DEFAULT_URGENT_WORDS) if readable else ""
        urgent = bool(is_order and not quiet and (urgent_word or found.get("urgent")))
        return {
            "item": item,
            "found": found,
            "notice": notice,
            "filename": filename,
            "sender": sender,
            "basis": basis,
            "fax_number": number,
            "kind": kind,
            "category": category,
            "is_order": is_order,
            "readable": readable,
            "rotated": rotated,
            "usage": usage,
            "quiet": quiet,
            "wants_heads": wants_heads,
            "ask_done": bool(ask_done),
            "urgent": urgent,
            "urgent_word": urgent_word if urgent else "",
        }

    def _urgent_recipients(self) -> list[dict]:
        return [
            r for r in ((self._config.fax_watch.get("urgent") or {}).get("extra_recipients") or [])
            if r.get("account_id")
        ]

    def _urgent_heads(self, prepared: list[dict]) -> str:
        """急ぎの注文が1件でもあれば、追加の宛先の To。"""
        if not any(p.get("urgent") for p in prepared):
            return ""
        return " ".join(f"[To:{int(r['account_id'])}]" for r in self._urgent_recipients())

    def _urgent_note(self, p: dict) -> str:
        names = "・".join(f"{r.get('name')}さん" for r in self._urgent_recipients() if r.get("name"))
        told = f"{names}にもお知らせしています。" if names else ""
        if p.get("urgent_word"):
            return f"※急ぎの指定があるため（「{p['urgent_word']}」）、{told}".rstrip("、")
        return f"※急ぎと読める記載があるため、{told}".rstrip("、")

    def _render(self, p: dict, prefix: str, trailer: bool, ask_done: bool) -> str:
        """通知1件分の本文。prefix は番号（「① 」）、trailer は末尾の注意書きと返し方。"""
        if not p["readable"]:
            return self._compose_unreadable(p["found"], p["notice"], p["filename"], prefix=prefix)
        lines = [
            self._compose(
                p["found"], p["sender"], p["basis"], p["fax_number"], p["notice"], p["filename"],
                prefix=prefix, trailer=False,
            )
        ]
        # 補足（静かに置く理由・急ぎの宛先）は本文の直後、注意書きと返し方の前に置く
        quiet = p.get("quiet") or {}
        if quiet and str(quiet.get("reason") or "").strip():
            lines.append(f"※{str(quiet['reason']).strip()}のため、呼び出し（To）と対応完了の確認は省いています。")
        if p.get("urgent"):
            lines.append(self._urgent_note(p))
        if trailer:
            lines.append("※PDFを読んで整理しています。数量・金額は原本でご確認ください。")
            if ask_done:
                lines.append(ASK_DONE)
        return "\n".join(lines)

    @staticmethod
    def _compose_unreadable(found: dict, notice: dict, filename: str, prefix: str = "") -> str:
        """回しても読めなかったとき。黙って「その他」にせず、人に見てもらう。"""
        lines = [
            f"{prefix}FAX「{filename}」が届きましたが、こちらでは内容を読み取れませんでした（不鮮明などのため）。",
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

    @staticmethod
    def _thread_ids(thread: dict, evening_ids: set[str]) -> set[str]:
        """この控えへの返事とみなすメッセージID（通知・PDF・催促・夕方の一覧）。"""
        return {str(thread.get("posted_id")), str(thread.get("pdf_id"))} | {
            str(c) for c in thread.get("check_ids") or []
        } | evening_ids

    def _close_finished(self, state: dict, messages: list[dict], room_id: int, notifier: int) -> None:
        """「対応完了」の返事があった発注書を、見届けの対象から外す。

        どの発注書の話かは、返信先 → ファイル名 → 番号（①③）→ 「全て」の順に決める。
        まとめ通知（複数件）への番号も「全て」も無い「対応完了」は、どれか分からないので
        閉じずに番号を聞き返す（黙って全部閉じると処理漏れになる）。
        """
        self.closed_last_run = []
        self.asked_last_run = []
        threads = state.get("open") or []
        if not threads:
            return
        me = self._me()
        # 夕方の一覧への返信は、載っている発注書すべてへの返事として受ける
        evening_ids = {str(i) for i in state.get("evening_ids") or []}
        asked = [str(i) for i in state.get("asked_ids") or []]
        filenames = [str(t.get("filename") or "") for t in threads if t.get("filename")]
        closed: list[dict] = []
        closers: dict[str, dict] = {}
        for message in sorted(messages, key=lambda m: int(m.get("message_id", 0))):
            mid = int(message.get("message_id", 0))
            if str(mid) in asked or _account_of(message) in (notifier, me):
                continue
            body = str(message.get("body", ""))
            if _OWN_MARK in body:
                continue  # 自分の投稿（自IDが取れなかったときの保険）
            if not is_completion(body):
                continue
            targets = reply_targets(body, room_id)
            candidates = [
                t for t in threads
                if t not in closed
                and mid > _as_int(t.get("posted_id"))
                and (not targets or targets & self._thread_ids(t, evening_ids))
            ]
            if not candidates:
                continue  # 別のFAXへの返事、または通知より前の発言
            # ファイル名を挙げて書かれた報告は、そのFAXだけの話として読む
            named = [f for f in filenames if f and f in body]
            note = ""
            via = "reply"
            if named:
                chosen = [t for t in candidates if str(t.get("filename") or "") in named]
                via = "filename"
            else:
                # 返信でもファイル名指定でもない文は、短い報告（「2件とも対応完了です」）だけを
                # 完了と読む。実例（2026-09-08）: メンバー宛の周知文に「対応完了に対する返信…」
                # とあり、開いていた発注書を閉じてお礼を返してしまった
                if not targets and not is_short_report(body, me):
                    continue
                numbers = report_numbers(body)
                if names_exceptions(body):
                    # 「①以外は対応完了」。挙げなかった分を全部閉じるので、条件を全部満たす
                    # ときだけ受け、外れたら番号を聞き返す（取り違えが複数件の取りこぼしになる）
                    kept = self._exclusion_targets(body, candidates, targets)
                    if kept is None:
                        asked.append(str(mid))
                        self._ask_which(room_id, message, candidates, exclusion=True)
                        self.asked_last_run.append(message)
                        continue
                    chosen = [t for t in candidates if t not in kept]
                    if chosen:
                        note = (
                            f"{self._numbers_of(kept)}を残して{self._numbers_of(chosen)}を閉じました。"
                            "違っていればお知らせください。"
                        )
                    via = "exclusion"
                elif numbers:
                    chosen = pick_by_number(candidates, numbers)
                    via = "number"
                elif (
                    mentions_all(body)
                    or len(candidates) == 1
                    or self._replied_to_each(candidates, threads, targets, evening_ids)
                ):
                    chosen = candidates
                    via = "all" if mentions_all(body) else "reply"
                else:
                    # まとめ通知に番号なしの「対応完了」。どれか分からないので聞き返す（一度だけ）
                    asked.append(str(mid))
                    self._ask_which(room_id, message, candidates)
                    self.asked_last_run.append(message)
                    continue
            if not chosen:
                continue
            for thread in chosen:
                self._audit(
                    {
                        "type": "fax_done",
                        "room_id": room_id,
                        "message_id": str(thread.get("pdf_id")),
                        "filename": thread.get("filename"),
                        "number": int(thread.get("number") or 0),
                        "by": _account_of(message),
                        "stage": int(thread.get("stage", 0)),
                        "via": via,
                    }
                )
                closed.append(thread)
            closers.setdefault(str(mid), (message, note))
        remaining = [t for t in threads if t not in closed]
        state["open"] = remaining
        state["asked_ids"] = asked[-50:]
        self.closed_last_run = closed
        # 報告には一言返す。1つの「完了」で複数の発注書が閉じても、お礼は1回。
        # そのとき、まだ対応待ちのFAXがあれば一緒に示す（月曜朝にまとめて届いた
        # 分の処理漏れを防ぐ）
        for closer, note in closers.values():
            self._thank(room_id, closer, remaining, note)

    def _replied_to_each(
        self, candidates: list[dict], threads: list[dict], targets: set[str], evening_ids: set[str]
    ) -> bool:
        """返信先が候補の1件ずつを個別に指しているか（複数の通知にまとめて返信した「対応完了」）。

        実例（2026-09-24 11:35）: 3通の通知それぞれに返信を付けた「対応完了」に、どの番号か
        聞き返してしまった。返信先がどれも1件だけの通知（まとめ通知や一覧ではない）なら、
        どのFAXの話かは明らかなので、その全部を済んだと読む。
        """
        if not targets:
            return False
        for candidate in candidates:
            own = self._thread_ids(candidate, evening_ids) & targets
            if not any(
                sum(1 for t in threads if mid in self._thread_ids(t, evening_ids)) == 1 for mid in own
            ):
                return False  # まとめ通知・催促・夕方の一覧など、複数件を指す投稿への返信
        return True

    @staticmethod
    def _numbers_of(threads: list[dict]) -> str:
        return "".join(circled(int(t.get("number") or 0)) for t in threads)

    def _exclusion_targets(self, body: str, candidates: list[dict], targets: set[str]) -> list[dict] | None:
        """「①以外は対応完了」で残す控え。条件に外れれば None（閉じずに聞き返す）。

        条件: (1) 返信先で範囲が決まっている（返信でない一言では受けない）
        (2) 候補に番号のない古い控えが無く、番号が一意（前日の①と今日の①が混ざらない）
        (3) 番号がすべて「以外」の前にあり、後ろに番号や保留の語が無い
        (4) 挙げた番号が候補に実在する（打ち間違いを受けない）
        """
        if not targets:
            return None
        numbers = [int(t.get("number") or 0) for t in candidates]
        if any(n <= 0 for n in numbers) or len(set(numbers)) != len(numbers):
            return None
        excluded = exclusion_numbers(body)
        if excluded is None or any(n not in numbers for n in excluded):
            return None
        return [t for t in candidates if int(t.get("number") or 0) in excluded]

    def _ask_which(self, room_id: int, message: dict, candidates: list[dict], exclusion: bool = False) -> None:
        """番号の無い「対応完了」に、どのFAXの話か番号で聞き返す。"""
        try:
            tag = f"[rp aid={_account_of(message)} to={room_id}-{message.get('message_id')}]"
            hint = self._reply_hint(candidates)
            if exclusion:
                opening = (
                    "ありがとうございます。「以外」の範囲を取り違えないよう、済んだFAXの番号を挙げる形で"
                    f"「対応完了」とお返事ください{hint}。全部済んでいれば「全て対応完了」で結構です。"
                )
            else:
                opening = (
                    f"ありがとうございます。対応待ちが{len(candidates)}件あるので、どのFAXが済んだか"
                    f"番号を添えて「対応完了」とお返事ください{hint}。全部済んでいれば「全て対応完了」で結構です。"
                )
            lines = [opening] + ["・" + self._thread_line(t) for t in candidates]
            self._chatwork.send_message(room_id, f"{tag}\n" + "\n".join(lines))
        except Exception:
            print("[warn] 番号の聞き返しに失敗: " + traceback.format_exc(), flush=True)

    @staticmethod
    def _reply_hint(threads: list[dict]) -> str:
        """「（例:「① 対応完了」）」。番号の付いた控えが無ければ空。"""
        numbers = [int(t.get("number") or 0) for t in threads if t.get("number")]
        return f"（例:「{circled(numbers[0])} 対応完了」）" if numbers else ""

    def _thank(self, room_id: int, message: dict, remaining: list[dict], note: str = "") -> None:
        """完了の報告に、相手のメッセージへの返信で礼を言い、残りを添える（黙って閉じない）。

        note は「①を残して②③を閉じました」のような、何を閉じたかの明記（除外の形のとき）。
        """
        try:
            if self._phrasebook is None:
                from .phrasing import build

                self._phrasebook = build(self._config)
            tag = f"[rp aid={_account_of(message)} to={room_id}-{message.get('message_id')}]"
            lines = [self._phrasebook.pick("fax_done_thanks", scope=str(room_id))]
            if note:
                lines.append(note)
            lines += self._remaining_lines(remaining)
            self._chatwork.send_message(room_id, f"{tag}\n" + "\n".join(lines))
        except Exception:
            print("[warn] 完了報告への返事に失敗: " + traceback.format_exc(), flush=True)

    def _thread_line(self, thread: dict) -> str:
        return thread_line(thread, int(self._config.fax_watch.get("room_id", 0) or 0))

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
            "対応済みでしたら、このメッセージへの返信で番号を添えて「対応完了」とお知らせください"
            f"{self._reply_hint(open_threads)}。全件済みでしたら「全て対応完了」で結構です。"
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
        due_first: list[dict] = []
        due_again: list[dict] = []
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
                    due_first.append(thread)
            elif stage == 1:
                checked_at = _parse_iso(thread.get("checked_at")) or posted_at
                due = at_clock(checked_at.date(), recheck_clock)
                if due <= checked_at:  # 朝の確認が遅れた日は、そこから同じ間隔を空ける
                    due = checked_at + gap
                if now >= due:
                    due_again.append(thread)
            remaining.append(thread)
        # 同じ時刻に催促する分は、夕方の一覧と同じ形で1通にまとめる（1件ずつ鳴らさない。
        # 指摘 2026-09-22: 朝9時の前日分も19時の一覧と同じまとめにしてほしい）
        if due_first:
            mid = self._post_check(room_id, due_first, now, first=True)
            for thread in due_first:
                thread["stage"] = 1
                thread["checked_at"] = now.isoformat()
                thread.setdefault("check_ids", []).append(mid)
        if due_again:
            mid = self._post_check(room_id, due_again, now, first=False)
            for thread in due_again:
                # 催促は2度で打ち切るが、対応待ちとしては残す（夕方の一覧と
                # お礼の返信で示し続け、完了の返事で外れる）
                thread["stage"] = 2
                thread["rechecked_at"] = now.isoformat()
                thread.setdefault("check_ids", []).append(mid)
        state["open"] = remaining

    def _post_check(self, room_id: int, threads: list[dict], now: datetime, first: bool) -> str:
        """催促を、対応待ちの一覧（番号・PDFのリンク付き）として1通で出す。"""
        from .answer import sanitize_for_chatwork

        listing = "\n".join("・" + self._thread_line(t) for t in threads)
        hint = self._reply_hint(threads)
        count = len(threads)
        if first:
            greeting = "おはようございます。" if now.hour < 11 else "お疲れさまです。"
            when = posted_phrase(threads, now)
            text = (
                f"{greeting}\n{when}お知らせした次の{count}件のFAXについて、まだ対応完了の返信をいただいていません。"
                f"確認と対応は完了していますでしょうか。\n{listing}\n"
                f"済んでいましたら、このメッセージへの返信で番号を添えて「対応完了」とお知らせください{hint}。"
                "全件済みでしたら「全て対応完了」で結構です。"
            )
        else:
            text = (
                f"たびたび失礼します。\n次の{count}件のFAXについて、先ほどの確認にもまだお返事がないようです。"
                f"確認漏れになっていないでしょうか。\n{listing}\n"
                f"対応が済んでいましたら番号を添えて「対応完了」とご返信ください{hint}。"
                "全件済みでしたら「全て対応完了」で結構です。"
            )
        mid = self._chatwork.send_message(room_id, f"{self._heads(now)}\n" + sanitize_for_chatwork(text))
        for thread in threads:
            self._audit(
                {
                    "type": "fax_check",
                    "room_id": room_id,
                    "message_id": str(thread.get("pdf_id")),
                    "filename": thread.get("filename"),
                    "number": int(thread.get("number") or 0),
                    "stage": 1 if first else 2,
                }
            )
        return str(mid or "")

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

    def _own_words(self) -> tuple[str, ...]:
        return tuple(self._config.fax_watch.get("own_company_words") or DEFAULT_OWN_COMPANY_WORDS)

    def _read(self, data: bytes, filename: str, hint: str = "") -> dict[str, Any]:
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
            "system": READ_SYSTEM.replace("{own_companies}", "／".join(self._own_words())),
        }
        ask = f"このFAX（{filename}）の差出人と内容を整理してください。"
        if hint:
            ask += "\n" + hint
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
            {"type": "text", "text": ask},
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
        prefix: str = "",
        trailer: bool = True,
    ) -> str:
        kind = str(found.get("kind") or "その他")
        received = notice.get("received_at", "")
        lines: list[str] = []
        if found.get("is_order"):
            lines.append(prefix + (f"{kind}が届きました。" if sender == "" else f"{sender}から{kind}が届きました。"))
        else:
            lines.append(
                prefix + (f"FAXが届きました（{kind}）。" if sender == "" else f"{sender}からFAXが届きました（{kind}）。")
            )
        if sender:
            lines.append(f"差出人の根拠: {basis}")
        elif basis:
            why = f"FAX番号 {number} は台帳に無く、{basis}" if number else f"送信元番号なし・{basis}"
            lines.append(f"差出人: 特定できませんでした（{why}）")
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
        if trailer:
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


def posted_phrase(threads: list[dict], now: datetime) -> str:
    """催促の冒頭で、いつ知らせた分かを言う語。「昨日」「先週金曜日に」「9月19日に」「これまでに」。

    指摘（2026-09-22）: 「先にお知らせした」は分かりにくい。人の会話として日に則した言い方にする。
    """
    days = {p.date() for p in (_parse_iso(t.get("posted_at")) for t in threads) if p}
    if len(days) != 1:
        return "これまでに"
    day = days.pop()
    delta = (now.date() - day).days
    if delta == 1:
        return "昨日"
    if 2 <= delta <= 6:
        prefix = "先週" if day.isocalendar()[1] != now.date().isocalendar()[1] else ""
        return f"{prefix}{WEEKDAYS[day.weekday()]}曜日に"
    return f"{day.month}月{day.day}日に"


def message_url(room_id: int, message_id: Any) -> str:
    """Chatworkのメッセージへのリンク（PDFの投稿を開ける）。"""
    return f"https://www.chatwork.com/#!rid{int(room_id)}-{message_id}"


def thread_line(thread: dict, room_id: int = 0) -> str:
    """対応待ちのFAX1件の短い説明。「① 9/5 17:55 有限会社珍味屋 発注書（4955_001.pdf） <PDFのURL>」

    まとめて示すと1件ずつの通知を開けないので、room_id があればPDFの投稿へのリンクを添える。
    """
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
    number = int(thread.get("number") or 0)
    label = f"{circled(number)} " if number else ""
    mark = "【急ぎ】" if thread.get("urgent") else ""
    line = f"{label}{mark}{when}{sender} {kind}（{thread.get('filename', '')}）"
    if room_id and thread.get("pdf_id"):
        line += " " + message_url(room_id, thread["pdf_id"])
    return line


# 「未処理の注文書残ってる？」のような、対応状況を尋ねる言い方
_STATUS_RE = re.compile(
    r"(未処理|未対応|残って|残り|対応待ち|溜まって|たまって|一覧|状況|何件|ある[？?]|あります|抱えて|どれ)"
)
# 尋ねている手がかり（長い文でこれが無ければ、連絡や説明として読む）
_ASKING_RE = re.compile(r"(\?|？|ますか|ある$|残ってる|残ってます|教えて|確認したい|知りたい|何件|どれ)")


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
- 相手が「このFAXの対応が済んだ」と報告していれば、done_all（全部済んだ）か
  done_filenames（済んだPDF名。下の一覧から）で示す。運用の連絡や説明
  （「〜の件、修正しました」「〜に変更しました」など）は報告ではないので示さない。
  報告のときの reply は短いお礼だけでよい（残りの一覧はプログラム側が添える）
- 対応待ちには番号（①②…）が付いている。「①と③が済みました」のように番号で
  報告されたら、その番号のPDF名を done_filenames に入れる。番号も「全て」も無い
  「対応完了」で対象が複数あるときは閉じず、どの番号か聞き返す
  （「全部でしたら『全て対応完了』とお返事ください」と添える）
- 「①以外は完了」のような除外の形はプログラム側が扱う。done_all・done_filenames は
  立てず、reply は「済んだ番号を挙げる形でお願いします」と一言添える

いま対応待ちのFAX（番号 / PDF名 / 差出人 / 種類）:
{open_list}

例:
- こちら「いま対応待ちのFAXはありません。」→ 相手「了解です。」→「はい。また届いたらお知らせしますね。」
- 相手「ありがとう」→「こちらこそ、ご確認ありがとうございます。」
- 相手「お疲れさまです」→「お疲れさまです。今日も何かあればお知らせください。」
- 相手「4987の件、対応完了しました。ありがとうございました。」→ done_filenames に 4987_001.pdf、reply「ご対応ありがとうございます。」
- 相手「②は済みました」→ 一覧で②のPDF名を done_filenames に、reply「ご対応ありがとうございます。」
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

    def is_status_question(self, question: str) -> bool:
        """対応状況を尋ねる文か。長い連絡文に「対応待ち」とあるだけでは尋ねていない。"""
        text = str(question or "")
        if not _STATUS_RE.search(text):
            return False
        return len(strip_tags(text).strip()) <= 30 or bool(_ASKING_RE.search(text))

    def open_threads(self) -> list[dict]:
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return list(state.get("open") or [])

    def reply(self, question: str, replied_to: str = "") -> tuple[str, dict]:
        """→ (返信, usage)。対応状況の問い合わせはコードで、それ以外の会話はモデルで返す。"""
        if self.is_status_question(question):
            return self._status(), {}
        text, usage, _ = self.converse(question, replied_to)
        return text, usage

    def converse(self, question: str, replied_to: str = "") -> tuple[str, dict, dict]:
        """アシスタント宛の文を読んで返す。→ (返信, usage, 済んだと読めた報告)

        済んだ報告は {"all": bool, "filenames": [...]}。呼び出し側がそれで発注書を閉じ、
        お礼と残りを1通で返す（そのとき返信文は使わない）。
        「了解です」「ありがとう」のような一般の会話には同僚として短く返す。
        実例（2026-09-07 20:07）: 「了解です。」に「このルームでは…」と案内を返してしまった。
        """
        from .answer import _call_with_continuation

        done = {"all": False, "filenames": []}
        try:
            if self._client is None:
                import anthropic

                self._client = anthropic.Anthropic()
            cfg = self._config
            open_list = "\n".join(
                f"- {circled(int(t.get('number') or 0)) if t.get('number') else '（番号なし）'} / "
                f"{t.get('filename', '')} / {t.get('sender') or '差出人不明'} / {t.get('kind', '')}"
                for t in self.open_threads()
            ) or "（なし）"
            kwargs = {
                "model": cfg.model,
                "max_tokens": int(cfg.max_tokens),
                "output_config": {
                    "effort": "low",
                    "format": {
                        "type": "json_schema",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "reply": {"type": "string", "description": "相手への返事。1〜2文"},
                                "done_all": {"type": "boolean", "description": "対応待ち全部が済んだという報告なら true"},
                                "done_filenames": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "済んだと報告されたPDF名（一覧にあるものだけ）。無ければ空",
                                },
                            },
                            "required": ["reply", "done_all", "done_filenames"],
                            "additionalProperties": False,
                        },
                    },
                },
                "system": self.CHAT_SYSTEM.format(agent_name=cfg.agent_name, open_list=open_list),
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
            found = json.loads(text)
            reply = str(found.get("reply") or "").strip()
            known = {str(t.get("filename", "")).lower() for t in self.open_threads()}
            done = {
                "all": bool(found.get("done_all")),
                "filenames": [str(f) for f in (found.get("done_filenames") or []) if str(f).lower() in known],
            }
            if not reply or is_parrot(question, reply):
                # 実例（2026-09-07 20:22）: 「了解です。」に「了解です。」と返した。
                # 繰り返しは会話になっていないので、こちらからの一言に差し替える
                reply = self._phrasebook().pick("fax_room_ack", scope=str(self._config.fax_watch.get("room_id", "")))
            return reply, usage, done
        except Exception:
            print("[warn] FAXルームでの会話の返答に失敗: " + traceback.format_exc(), flush=True)
            return self.GUIDE, {}, done

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
            room_id = int(self._config.fax_watch.get("room_id", 0) or 0)
            lines += ["・" + thread_line(t, room_id) for t in open_threads]
            hint = FaxWatcher._reply_hint(open_threads)
            lines.append(
                f"対応済みでしたら、番号を添えて「対応完了」とお知らせください{hint}。"
                "全件済みでしたら「全て対応完了」で結構です。"
            )
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
