"""営業メールの見張り — 取引先の担当者から届いたメールを担当者へ知らせ、期日と返信を追う。

「楽天軒　営業メール確認」ルームには、通知ボット（通知管理くん）が営業あてのメールを
1通ずつ流す（差出人・受信日時・件名・添付の名前・本文の先頭約400字）。広告もシステムの
自動通知も、当社の担当者が送ったメールも同じ形で流れる。

やること（指示 2026-11-20）:
- 対象は「取引先の担当者から」届いたメールだけ。広告・自動通知は知らせない
- 届いたら内容を読み、メールの宛名に書かれた当社の担当者（森さん・藤田さん）へ、
  届いたことと簡単な内容を知らせる。期日はとくに確かめる
- 返信が要る内容なら、当社からの返信が流れてくるまで追う。返信は中身まで読み、
  求めに答えているかを確かめる。済んでいなければ毎朝の一覧と期日当日の確認で知らせる

方針:
- メールは送らない。扱うのは仕分け・通知・進捗だけ（社外への送付は実装しない）
- ルームは巡回で見る。許可ルーム（Q&Aの対象）には入れない
- 期日の日付はコードで確かめる。モデルが読んだ日付は、原文の抜き書きに同じ日が
  書かれているときだけ採る。合わなければ「日付を確定できなかった」と人に渡す
- モデルの読みで「返信が足りない」と見えても、決めつけずに担当者へ尋ねる
- 番号は返信・対応を追うメールにだけ振る（共有だけのメールは「・」）
- 知らせるのは月〜金の 8:30〜19:30 だけ（FAX確認と同じ。祝日は平日と同じ扱い）
- 監査ログにも状態にもメールの本文は残さない（状態に持つのは件名と要約まで）

メールの読み方は2通り（指示 2026-11-24: メールサーバーを直接確認する）:
- メールサーバーの受信箱・送信済みを直接読む（`mailbox.py`。IMAP・読み取り専用）。全文・宛先（To／CC）・
  返信関係のIDが分かる。`mail_watch.mailboxes` のうち、パスワードの環境変数が入っている箱だけ読む
- 通知ルームの「新着メール」通知（先頭約400字）。直接読めない箱のメールと、サーバーに
  つながらない間の代わり。同じメールを両方で見たら、先に読んだ方だけを使う

すぐ試す: python -m raizuinu.mailwatch --dry-run（投稿せず、直近のメールでの動きを表示）
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .faxwatch import (
    JST,
    UNNUMBERED_MARK,
    WEEKDAYS,
    _NOT_DONE_RE,
    _account_of,
    _merge_usage,
    _parse_iso,
    at_clock,
    circled,
    in_notify_window,
    is_reopen_request,
    is_undo_request,
    mentions_all,
    message_url,
    next_business_day,
    parse_clock,
    reply_targets,
    report_numbers,
    strip_own_mention,
    strip_tags,
)

MAIL_TITLE = "新着メール"
OMITTED_MARK = "…(以下省略)"

PARTNER = "取引先の担当者"
CATEGORIES = [PARTNER, "広告・メルマガ", "システムの自動通知", "その他"]
ANSWERED, PARTIAL, ACK_ONLY, UNRELATED = "回答済み", "一部のみ", "受領・予告のみ", "別件"
VERDICTS = [ANSWERED, PARTIAL, ACK_ONLY, UNRELATED]

SHORT_REPORT_CHARS = 30
MAX_CLOSED_LOG = 300
MAX_RECENT = 60  # 再送の見分けに覚えておくメールの数
MAX_SEEN_IDS = 1500  # 同じメールが複数の受信箱に届いたときの見分け（Message-ID）
CROSS_SOURCE_SECONDS = 15 * 60  # サーバーで読んだメールと通知ルームの通知を同じメールと見る時間差
CHATWORK, IMAP = "chatwork", "imap"

_TITLE_RE = re.compile(r"\[title\](.*?)\[/title\]", re.S)
_RULE_RE = re.compile(r"^[ \t]*[─━―]{4,}[ \t]*$", re.M)
_FIELD_RE = re.compile(r"^(差出人|受信|件名|添付)\s*[:：][ \t]?(.*)$")
_ADDRESS_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")


class ModelUnavailable(Exception):
    """モデルに読ませられなかった（障害・混雑・応答が読めない）。次の巡回でやり直す。"""


# --- 通知の読み取り ---


def parse_mail_notice(body: str) -> dict[str, Any] | None:
    """通知管理くんの「新着メール」通知 → 差出人・受信日時・件名・添付・本文。違う形なら None。"""
    text = str(body or "")
    title = _TITLE_RE.search(text)
    if not title or MAIL_TITLE not in title.group(1):
        return None
    rest = re.sub(r"\[/info\]\s*$", "", text[title.end():].strip())
    rule = _RULE_RE.search(rest)
    head, content = (rest[: rule.start()], rest[rule.end():]) if rule else (rest, "")
    fields: dict[str, str] = {}
    for line in head.splitlines():
        match = _FIELD_RE.match(line.strip())
        if match and match.group(1) not in fields:
            fields[match.group(1)] = match.group(2).strip()
    address = _ADDRESS_RE.search(fields.get("差出人", ""))
    if not address:
        return None
    content = content.strip()
    truncated = content.endswith(OMITTED_MARK)
    if truncated:
        content = content[: -len(OMITTED_MARK)].rstrip()
    attached = fields.get("添付", "")
    received = None
    try:
        received = datetime.strptime(fields.get("受信", ""), "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    except ValueError:
        pass
    return {
        "sender": address.group(0).lower(),
        "received": received,
        "subject": fields.get("件名", ""),
        "attachments": [] if attached in ("", "なし") else [a.strip() for a in attached.split(",") if a.strip()],
        "text": content,
        "truncated": truncated,
    }


def matches_sender(address: str, patterns: Any) -> bool:
    """差出人が規則に当たるか。「example.co.jp」はそのドメインと下位ドメイン、「a@b.jp」はその宛先だけ。"""
    address = str(address or "").lower()
    domain = address.rpartition("@")[2]
    for raw in patterns or []:
        pattern = str(raw or "").strip().lower().lstrip("@")
        if not pattern:
            continue
        if "@" in pattern:
            if address == pattern:
                return True
        elif domain == pattern or domain.endswith("." + pattern):
            return True
    return False


_REPLY_MARK_RE = re.compile(r"(?<![a-z])(?:re|fwd?|fw)\s*:\s*", re.I)
_FORWARD_HEAD_RE = re.compile(r"^\s*(?:re\s*:\s*)*(?:fwd?|fw)\s*:", re.I)
MIN_CONTAINED_KEY = 8


def thread_key(subject: str) -> str:
    """件名から返信・転送の印（Re:／RE:／Fwd:）と空白を除いたもの。同じやり取りかの照合に使う。"""
    text = unicodedata.normalize("NFKC", str(subject or ""))
    return re.sub(r"\s+", "", _REPLY_MARK_RE.sub("", text)).lower()


def same_thread(a: str, b: str) -> bool:
    """同じやり取りの件名か。先方が頭に「【ご対応依頼】⇒RE:」のような語を足すことがあるので、
    片方がもう片方を含んでいても同じと見る（短い件名は一致だけ）。"""
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return len(short) >= MIN_CONTAINED_KEY and short in long


def is_forward(subject: str) -> bool:
    """転送（Fwd:）か。社内へ回しただけで、先方への返信ではない。"""
    return bool(_FORWARD_HEAD_RE.match(unicodedata.normalize("NFKC", str(subject or ""))))


def _loose(text: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")))


_HONORIFIC = r"(?:様|さま|さん|殿)"


def addressed_to(name: str, text: str) -> bool:
    """本文に「森様」「森 美明　様」のような宛名があるか（モデルが挙げた担当者の裏付け）。"""
    name = unicodedata.normalize("NFKC", str(name or "")).strip()
    if not name:
        return False
    plain = unicodedata.normalize("NFKC", str(text or ""))
    return bool(re.search(rf"{re.escape(name)}[ \t]*[^\s様殿、,。]{{0,5}}?[ \t]*{_HONORIFIC}", plain))


_CORP_RE = re.compile(r"株式会社|有限会社|合同会社|\(株\)|\(有\)|㈱|㈲")
_NAME_SPLIT_RE = re.compile(r"[\s/・,、()]+")
_VARIANTS = str.maketrans("髙﨑濵", "高崎浜")
MIN_NAME_TOKEN = 2
NAME_HEAD_CHARS = 300


def name_tokens(partner: str) -> list[str]:
    """「株式会社髙島屋 安田様」→ ["高島屋", "安田"]。当社の返信が誰あてかを見るための語。"""
    text = _CORP_RE.sub(" ", unicodedata.normalize("NFKC", str(partner or "")).translate(_VARIANTS))
    tokens = []
    for raw in _NAME_SPLIT_RE.split(text):
        token = re.sub(rf"{_HONORIFIC}$", "", raw.strip())
        if len(token) >= MIN_NAME_TOKEN and token not in tokens:
            tokens.append(token)
    return tokens


def written_to(partner: str, text: str) -> bool:
    """当社のメールの冒頭（宛名のあたり）に、その取引先の社名か担当者名が書かれているか。"""
    head = _loose(str(text or "")[:NAME_HEAD_CHARS]).translate(_VARIANTS)
    return any(token in head for token in name_tokens(partner))


# --- 期日 ---


def find_date_quote(day: date, text: str) -> str:
    """メールの中から、その日付が書かれている箇所を抜き出す（「11/18までに」）。無ければ空。

    モデルの抜き書きが原文どおりでなかったときの補い。日付そのものが書かれていなければ採らない。
    """
    plain = unicodedata.normalize("NFKC", str(text or ""))
    pattern = rf"(?<!\d){day.month}\s*[/月]\s*{day.day}(?!\d)\s*日?(?:\s*\([月火水木金土日]\))?[^\n。、]{{0,10}}"
    match = re.search(pattern, plain)
    return match.group(0).strip() if match else ""


def jp_date(day: date) -> str:
    return f"{day.month}月{day.day}日（{WEEKDAYS[day.weekday()]}）"


def short_date(day: date) -> str:
    return f"{day.month}/{day.day}（{WEEKDAYS[day.weekday()]}）"


def relative_deadline(quote: str, received: date) -> date | None:
    """「本日中」「明日まで」「今週中」「今月中」のような、日付の無い期日 → 受信日から数えた日。"""
    text = unicodedata.normalize("NFKC", str(quote or ""))
    if "明後日" in text:
        return received + timedelta(days=2)
    if "明日" in text:
        return received + timedelta(days=1)
    if re.search(r"本日|今日", text):
        return received
    if "来週" in text:
        return received + timedelta(days=4 - received.weekday() + 7)
    if "今週" in text:
        return received + timedelta(days=max(0, 4 - received.weekday()))
    if re.search(r"今月|月内|月末", text) and "来月" not in text:
        first_of_next = (received.replace(day=1) + timedelta(days=32)).replace(day=1)
        return first_of_next - timedelta(days=1)
    return None


def check_deadline(date_text: Any, quote: Any, received: date) -> date | None:
    """モデルが読んだ期日を、原文の抜き書きで確かめる。裏付けが取れた日付だけ返す。

    採る条件: 抜き書きにその日の数字があり、月日の形（11/27・11月27日）があれば月も合い、
    曜日が書かれていれば曜日も合うこと。日付の無い言い方（本日中・今週中）は受信日から
    数えた日と合うこと。受信日から離れすぎた日付（年の取り違え）は採らない。
    """
    try:
        day = date.fromisoformat(str(date_text or "").strip())
    except ValueError:
        return None
    text = unicodedata.normalize("NFKC", str(quote or "")).strip()
    if not text:
        return None
    if not (received - timedelta(days=45) <= day <= received + timedelta(days=200)):
        return None
    if day.day in {int(n) for n in re.findall(r"\d+", text)}:
        pairs = {(int(m), int(d)) for m, d in re.findall(r"(\d{1,2})\s*[/月]\s*(\d{1,2})", text)}
        if pairs and (day.month, day.day) not in pairs:
            return None
        weekday = re.search(rf"(?<!\d){day.day}\s*日?\s*\(\s*([月火水木金土日])", text)
        if weekday and weekday.group(1) != WEEKDAYS[day.weekday()]:
            return None
        return day
    return day if relative_deadline(text, received) == day else None


# --- モデルへの指示 ---

READ_SYSTEM = """あなたは{company}の営業担当あてに届いたメールを仕分ける担当です。
本文は、長いときや通知経由のときに後ろが省略されています（その場合は「本文はここで省略されている」と書かれています）。
「引用されている過去のやり取り」が付いているときは参考にし、依頼や期日がそこにしか書かれていない場合（転送されたメールなど）はそこから読みます。
書かれていることだけで判断し、推測で埋めないでください。

各項目の決め方:
- category: 差出人の種類。
  「取引先の担当者」= 取引先・仕入先・百貨店・卸・委託先などの担当者や担当部署が、当社あてに業務の連絡として送ったメール。BCCの一斉連絡でも、取引に関する業務連絡ならこれ。
  「広告・メルマガ」= 販促、セミナーの案内、メールマガジン、キャンペーン。
  「システムの自動通知」= 受注・出荷・在庫などのシステムが自動で送る定型の通知、送信専用アドレスからの通知。
  「その他」= どれにも当たらないもの。
- staff: 宛名に書かれている当社の担当者。候補は {staff} だけ。本文の宛名（「森様」「藤田 様」）に実際に書かれている人を挙げる。本文の途中や署名に名前が出るだけの人は含めない。宛名に無ければ空の配列。
- addressee: 宛名を原文のまま（「RAKUTENKEN株式会社 森様」「お取引先様 各位」）。無ければ空。
- partner: 差出人の会社名と担当者名（「株式会社髙島屋 山木様」）。名乗りや署名から読む。読めなければ空（メールアドレスから作らない）。
- summary: 何の連絡かを1〜2文で。事実だけを書き、日付・金額・数量は原文のとおりにする。
- needs_reply: 先方が当社からの返信・回答・提出・承諾・手配を求めているか。件名と、見えている本文に書かれていることだけで決める。
  true にするもの: 質問、「いかがでしょうか」、回答や返信の依頼、資料や書類の提出の依頼、承諾の依頼、手配や発送の依頼。
  false にするもの: お礼、情報の共有、了承の連絡、納期や出荷の連絡、資料を送っただけの連絡、「ご確認ください」「ご査収ください」で終わる連絡。
  「修正があればご連絡ください」「問題があればご返信ください」「該当する情報があればご連絡ください」のような条件つきの求めは false とし、ask にその条件を書く。
  本文が省略されていても、見えている範囲に求めや問いが無ければ false とし、cut_off を true にする。
- ask: 先方が当社に求めていること（何を、どうしてほしいか）を1文で。書かれていることだけを書き、「〜の可能性がある」のような推測は書かない。無ければ空。
- deadline_quote と deadline_date: 先方が当社に求めている返信・提出・対応の期日。件名と本文の両方を見る。deadline_quote は期日が書かれている箇所を原文のまま短く抜き書きする（「11月27日(金)まで」）。deadline_date はその日付を YYYY-MM-DD で書く（年が無ければ受信日から見て最も近い先の日付）。期日が複数あれば最も早いもの。催事の会期、納品日、先方の予定など、当社の返信や対応の締切ではない日付は期日にしない。無ければ両方とも空。
- cut_off: 本文が省略されていて、依頼の内容や期日が省略された部分に続いていそうなら true。

「先に届いて、まだ返信・対応が済んでいないメール」が添えられているときは、このメールはその続きです。needs_reply は、このメールが届いた後もなお当社の返信が要るかで決めてください。"""

CHECK_SYSTEM = """あなたは、取引先から届いたメールに{company}の担当者が返信したかを確かめる担当です。
[届いたメール] と [当社が送ったメール] を読み、次を決めてください。本文は後ろが省略されていることがあります（その場合は「本文はここで省略されている」と書かれています）。「引用されている過去のやり取り」は当社が今回書いた部分ではないので、返信の中身としては数えません。

- verdict:
  「回答済み」= 先方の求めに答えている、または求められた資料を送っている。
  「一部のみ」= 求めの一部にしか答えていない。
  「受領・予告のみ」= 受け取った旨や「確認して改めてご連絡します」のような後日の回答の予告だけで、求めにはまだ答えていない。
  「別件」= 届いたメールへの返信ではない（相手や話題が違う）。
- reply_summary: 当社が送ったメールの要点を1文で。事実だけを書く。
- remaining: まだ答えていない点、または省略のため確かめられなかった点を1文で。無ければ空。

判断の決まり:
- 添付ファイルは名前しか分かりません。求められた資料に当たる名前の添付があれば、送ったものとして扱います。
- 本文が省略されていて後半を確かめられないときは、見えている範囲で判断します。見えている範囲が求めに答えていれば「回答済み」。見えている範囲に答えが無く、後ろが省略されているなら「一部のみ」とし、remaining に確かめられなかった点を書きます。
- 推測で「回答済み」にしないでください。"""


def read_schema(staff_names: list[str]) -> dict[str, Any]:
    staff_items: dict[str, Any] = {"type": "string"}
    if staff_names:
        staff_items["enum"] = list(staff_names)
    properties = {
        "category": {"type": "string", "enum": CATEGORIES},
        "staff": {"type": "array", "items": staff_items},
        "addressee": {"type": "string"},
        "partner": {"type": "string"},
        "summary": {"type": "string"},
        "needs_reply": {"type": "boolean"},
        "ask": {"type": "string"},
        "deadline_quote": {"type": "string"},
        "deadline_date": {"type": "string"},
        "cut_off": {"type": "boolean"},
    }
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": VERDICTS},
        "reply_summary": {"type": "string"},
        "remaining": {"type": "string"},
    },
    "required": ["verdict", "reply_summary", "remaining"],
    "additionalProperties": False,
}


def mail_text(mail: dict) -> str:
    """モデルへ渡すメールの文面。"""
    received = mail.get("received")
    when = f"{received:%Y-%m-%d}（{WEEKDAYS[received.weekday()]}） {received:%H:%M}" if received else "不明"
    lines = [f"差出人: {mail.get('sender', '')}"]
    if mail.get("to"):
        lines.append(f"宛先（To）: {'、'.join(mail['to'])}")
    if mail.get("cc"):
        lines.append(f"CC: {'、'.join(mail['cc'])}")
    lines += [
        f"受信: {when}",
        f"件名: {mail.get('subject', '')}",
        f"添付: {'、'.join(mail.get('attachments') or []) or 'なし'}",
        "本文:",
        str(mail.get("text") or ""),
    ]
    if mail.get("truncated"):
        lines.append("（本文はここで省略されている）")
    if mail.get("quoted"):
        lines += ["", "[引用されている過去のやり取り（転送された文面を含む）]", str(mail["quoted"])]
    return "\n".join(lines)


# --- 返事の読み取り ---

# 済んだと読める言い回し。FAX確認と違い「確認しました」は含めない（メールを見たという意味で
# 書かれることがあり、返信が済んだとは限らない）
_MAIL_DONE_RE = re.compile(
    r"(完了|済み|済です|済でした|済んで|対応済|処理済|終わりました|対応しました"
    r"|返信済|返信しました|返信した|回答済|回答しました|送信済|送信しました|送りました|提出しました|提出済"
    r"|対応不要|返信不要|不要です|電話で(?:回答|連絡|対応|返答)|電話しました)"
)
_LIST_RE = re.compile(r"(一覧|未返信|未対応|残って|残り|状況|何件|どれ|リスト)")


def is_done_report(body: str) -> bool:
    """「③ 対応完了」「③ 返信しました」「③ 返信不要です」のように、済んだと読める返事か。"""
    text = unicodedata.normalize("NFKC", strip_tags(body))
    return bool(_MAIL_DONE_RE.search(text)) and not _NOT_DONE_RE.search(text)


def says_both(body: str) -> bool:
    """済んだ話と済んでいない話が1通に混ざっているか（「③はまだですが④は完了」）。"""
    text = unicodedata.normalize("NFKC", strip_tags(body))
    return bool(_MAIL_DONE_RE.search(text)) and bool(_NOT_DONE_RE.search(text))


class MailWatcher:
    """営業メールのルームを巡回し、取引先からのメールを知らせて、返信と期日を追う。"""

    def __init__(
        self,
        config: Any,
        chatwork: Any,
        client: Any | None = None,
        cost: Any | None = None,
        now: Callable[[], datetime] | None = None,
        state_path: Path | None = None,
        dry_run: bool = False,
        reader_factory: Callable[[dict], Any] | None = None,
    ) -> None:
        self._config = config
        self._chatwork = chatwork
        self._client = client
        self._cost = cost
        self._now = now or (lambda: datetime.now(JST))
        self._dry_run = dry_run
        self._state_path = state_path or config.resolve_path(config.state_dir) / "mailwatch.json"
        self._usage: dict[str, int] = {}
        self._posted: list[str] = []  # 自分の投稿ID（状態の post_log と同じリスト）
        self._reader_factory = reader_factory  # メールサーバーの読み取りの差し替え（テスト用）
        self._notices: list[dict] = []
        self._imap_new: dict[tuple[str, str], list[int]] = {}

    # --- 公開API ---

    def run_once(self) -> int:
        """新しいメールと返事を処理する。→ 知らせたメールの数。"""
        settings = self._settings()
        room_id = int(settings.get("room_id", 0) or 0)
        if not settings.get("enabled") or not room_id or not int(settings.get("notifier_account_id", 0) or 0):
            return 0
        try:
            messages = self._chatwork.get_recent_messages(room_id, limit=100)
        except Exception:
            print("[warn] 営業メールのルームの取得に失敗: " + traceback.format_exc(), flush=True)
            return 0
        state = self._load_state()
        # 初回は既読にするだけ。過去のメールをまとめて流さない
        if state.get("last_seen") is None:
            state["last_seen"] = max((int(m.get("message_id", 0)) for m in messages), default=0)
            self._save_state(state)
            return 0
        now = self._now()
        # 時間外は何もしない（読むのも知らせるのも次の時間帯の頭。1日20通ほどなので、
        # 週末をまたいでも直近100件から流れない）
        if not in_notify_window(now, settings.get("notify_window") or {}):
            return 0
        return self._run(state, messages, now)

    def replay(self, messages: list[dict], imap_last: int = 0, last: int | None = None) -> int:
        """渡された発言を「いま届いた」ものとして処理する（--dry-run 用。状態は白紙から）。

        last を渡すと、発言のうち直近 last 件だけを新着として扱う（0 なら通知ルームの分は処理しない。
        それより前の発言は、メールへのリンクを探すためだけに使う）。
        imap_last を渡すと、メールサーバーの受信箱・送信済みの直近 imap_last 通も同じように処理する。
        """
        state = self._blank_state()
        ids = sorted(int(m.get("message_id", 0)) for m in messages)
        if last is None:
            state["last_seen"] = (ids[0] if ids else 1) - 1
        else:
            state["last_seen"] = ids[-last - 1] if last < len(ids) else (ids[0] if ids else 1) - 1
        return self._run(state, messages, self._now(), imap_last=imap_last)

    def open_items(self) -> list[dict]:
        return list(self._load_state().get("open") or [])

    # --- 巡回の本体 ---

    def _run(self, state: dict, messages: list[dict], now: datetime, imap_last: int = 0) -> int:
        settings = self._settings()
        notifier = int(settings.get("notifier_account_id", 0) or 0)
        me = self._me()
        self._posted = state.setdefault("post_log", [])
        limit = int(settings.get("max_per_run", 15) or 0)
        max_attempts = int(settings.get("max_read_attempts", 3))
        self._notices = self._index_notices(messages, notifier)
        fresh = sorted(
            (m for m in messages if int(m.get("message_id", 0)) > int(state["last_seen"])),
            key=lambda m: int(m.get("message_id", 0)),
        )
        arrived: list[dict] = []
        reads = 0

        # 1) メールサーバーを直接読む。届いたメールを先に、当社が送ったメールを後に見る
        #    （同じ巡回で届いたメールとその返信を、正しい順で突き合わせるため）
        try:
            fetched = self._collect_imap(state, now, last=imap_last)
        except Exception:
            print("[error] メールサーバーの読み取りに失敗: " + traceback.format_exc(), flush=True)
            fetched = []
        by_time = lambda m: m["received"]  # noqa: E731
        ordered = sorted((m for m in fetched if not self._is_ours(m)), key=by_time)
        ordered += sorted((m for m in fetched if self._is_ours(m)), key=by_time)
        for mail in ordered:
            if limit and reads >= limit and not self._dry_run:
                break
            ref = mail["ref"]
            try:
                reads += self._handle_mail(state, mail, arrived, now)
            except ModelUnavailable:
                retries = state.setdefault("retries", {})
                retries[ref] = int(retries.get(ref, 0)) + 1
                if retries[ref] < max_attempts:
                    print(f"[warn] 営業メール {ref} を読めませんでした。次の巡回でやり直します", flush=True)
                    break
                self._report_unread(mail)
            except Exception:
                print(f"[error] 営業メール {ref} の処理に失敗: " + traceback.format_exc(), flush=True)
            state.get("retries", {}).pop(ref, None)
            self._imap_done(state, mail)
            self._save_state(state)
        self._imap_compact(state)
        self._save_state(state)

        # 2) 通知ルームの発言（直接読めない箱のメールの通知と、人からの返事）
        for message in fresh:
            mid = int(message.get("message_id", 0))
            account = _account_of(message)
            mail = None
            try:
                if account == notifier:
                    mail = parse_mail_notice(message.get("body", ""))
                    if mail is not None:
                        if limit and reads >= limit and not self._dry_run:
                            break  # 続きは次の巡回で（順番は崩さない）
                        if mail["received"] is None:
                            mail["received"] = datetime.fromtimestamp(int(message.get("send_time", 0)), JST)
                        mail.update(source=CHATWORK, ref=str(mid), link_id=str(mid))
                        reads += self._handle_mail(state, mail, arrived, now)
                elif account != me:
                    self._handle_human(state, message, me, now)
            except ModelUnavailable:
                retries = state.setdefault("retries", {})
                retries[str(mid)] = int(retries.get(str(mid), 0)) + 1
                if retries[str(mid)] < max_attempts:
                    print(f"[warn] 営業メール {mid} を読めませんでした。次の巡回でやり直します", flush=True)
                    self._save_state(state)
                    break
                if mail is not None:
                    self._report_unread(mail)
            except Exception:
                # 1通の失敗で全体を止めない（そのメールは飛ばして先へ進む）
                print(f"[error] 営業メール {mid} の処理に失敗: " + traceback.format_exc(), flush=True)
            state.get("retries", {}).pop(str(mid), None)
            state["last_seen"] = mid
            self._save_state(state)
        self._backfill_links(state)
        if arrived:
            self._announce(state, arrived, now)
        if self._morning_list(state, now):
            # その日の最初の巡回が午後だったとき、朝の一覧と当日の確認を続けて出さない
            state["lists"]["final"] = now.date().isoformat()
        self._final_check(state, now)
        self._save_state(state)
        return len(arrived)

    # --- 届いたメール ---

    def _handle_mail(self, state: dict, mail: dict, arrived: list[dict], now: datetime) -> int:
        """1通を仕分ける。→ モデルを呼んだ回数。"""
        settings = self._settings()
        mid = str(mail.get("ref"))
        if self._is_ours(mail):
            return self._own_mail(state, mail, now)
        if matches_sender(mail["sender"], settings.get("skip_senders")):
            self._audit({"type": "mail_skip", "message_id": mid, "reason": "rule"})
            return 0
        key = thread_key(mail["subject"])
        domain = mail["sender"].rpartition("@")[2]
        # 同じメールを二度知らせない（通知の再送、複数の受信箱への同報、サーバーと通知ルームの両方）
        if self._seen_before(state, mail, key):
            self._audit({"type": "mail_skip", "message_id": mid, "reason": "duplicate"})
            return 0
        references = set(mail.get("references") or [])
        earlier = [
            i for i in state["open"]
            if str(i.get("sender", "")).rpartition("@")[2] == domain
            and (same_thread(i.get("key", ""), key) or (i.get("message_id") and i["message_id"] in references))
        ]
        found = self._read(mail, earlier)
        self._remember(state, mail, key)  # 読めてから覚える（読めずにやり直す分を再送と取り違えない）
        if found.get("category") != PARTNER:
            self._audit({"type": "mail_skip", "message_id": mid, "reason": f"category:{found.get('category')}",
                         "usage": found.get("_usage")})
            return 1
        item = self._make_item(mid, mail, found, key)
        # 続きのメール: 先に届いた分の番号と期日を引き継ぎ、古い方は閉じる（同じ件を2件に数えない）
        for old in earlier:
            if not item["deadline"] and old.get("deadline"):
                item["deadline"], item["deadline_quote"] = old["deadline"], old.get("deadline_quote", "")
            item["follow_of"] = int(old.get("number") or 0) or item.get("follow_of", 0)
            self._close(state, old, "superseded", 0, now)
        if self._tracked(item):
            item["number"] = int(item.get("follow_of") or 0) or self._take_number(state, now)
            state["open"].append(item)
        arrived.append(item)
        self._audit({
            "type": "mail_notice", "message_id": mid, "number": item.get("number", 0),
            "sender_domain": domain, "needs_reply": item["needs_reply"], "deadline": item["deadline"],
            "recipients": item["recipients"], "follow_of": item.get("follow_of", 0), "usage": found.get("_usage"),
        })
        return 1

    def _make_item(self, mid: str, mail: dict, found: dict, key: str) -> dict:
        staff = self._staff()
        whole = f"{mail['subject']}\n{mail['text']}\n{mail.get('quoted', '')}"
        names = [
            s["name"] for s in staff
            if s["name"] in (found.get("staff") or []) and addressed_to(s["name"], mail["text"])
        ]
        via = "宛名" if names else ""
        if not names:
            # 本文に宛名が無いときは、メールの宛先（To）→ CC → 届いた受信箱の持ち主の順で決める
            names, via = self._staff_by_headers(mail)
        received: datetime = mail["received"]
        quote = str(found.get("deadline_quote") or "").strip()
        # 抜き書きは実際にメールに書かれている文でなければ採らない（作文された期日を通さない）。
        # 原文どおりでなかったときは、その日付が書かれている箇所をメールから探して補う
        if not quote or _loose(quote) not in _loose(whole):
            try:
                quote = find_date_quote(date.fromisoformat(str(found.get("deadline_date") or "").strip()), whole)
            except ValueError:
                quote = ""
        deadline = check_deadline(found.get("deadline_date"), quote, received.date()) if quote else None
        return {
            "id": mid,
            "number": 0,
            "key": key,
            "subject": mail["subject"],
            "sender": mail["sender"],
            "partner": str(found.get("partner") or "").strip(),
            "addressee": str(found.get("addressee") or "").strip(),
            "staff": names,
            "via": via,
            "source": mail.get("source", CHATWORK),
            "link_id": str(mail.get("link_id") or "") or self._find_notice(mail),
            "message_id": str(mail.get("message_id") or ""),
            "recipients": [s["account_id"] for s in staff if not names or s["name"] in names],
            "received_at": received.isoformat(),
            "summary": str(found.get("summary") or "").strip(),
            "ask": str(found.get("ask") or "").strip(),
            "needs_reply": bool(found.get("needs_reply")),
            "deadline": deadline.isoformat() if deadline else "",
            "deadline_quote": quote,
            "deadline_unsure": bool(quote) and deadline is None,
            "cut_off": bool(found.get("cut_off")) and bool(mail.get("truncated")),
            "attachments": len(mail.get("attachments") or []),
            "post_ids": [],
            "replies": [],
        }

    @staticmethod
    def _tracked(item: dict) -> bool:
        """返信・対応を追うメールか（返信が要る、または期日がある）。"""
        return bool(item.get("needs_reply") or item.get("deadline") or item.get("deadline_unsure"))

    # --- 当社が送ったメール（返信の確認）---

    def _own_mail(self, state: dict, mail: dict, now: datetime) -> int:
        mid = str(mail.get("ref"))
        if is_forward(mail["subject"]):
            self._audit({"type": "mail_skip", "message_id": mid, "reason": "own_forward"})
            return 0
        key = thread_key(mail["subject"])
        if self._seen_before(state, mail, key):
            self._audit({"type": "mail_skip", "message_id": mid, "reason": "duplicate"})
            return 0
        # 届いたメールより後に送ったものだけが、その返信になり得る
        sent_at = mail["received"]
        before = [i for i in state["open"] if (_parse_iso(i.get("received_at")) or sent_at) <= sent_at]
        references = set(mail.get("references") or [])
        # 返信関係のID（メールサーバーから読んだときだけ分かる）→ 件名 → 宛名の取引先名、の順で探す
        candidates = [i for i in before if i.get("message_id") and i["message_id"] in references]
        if not candidates:
            candidates = [i for i in before if same_thread(i.get("key", ""), key)]
        if not candidates:
            # 件名を変えて返信することがある（「産地証明書を提出してほしい」に「マロングラッセ産地証明」
            # という新しい件名で送る）。宛名にその取引先の名前があるメールは、中身を読んで確かめる
            candidates = [i for i in before if written_to(i.get("partner", ""), mail["text"])]
        if not candidates:
            self._remember(state, mail, key)
            self._audit({"type": "mail_skip", "message_id": mid, "reason": "own"})
            return 0
        link_id = str(mail.get("link_id") or "") or self._find_notice(mail)
        calls = 0
        for item in candidates:
            found = self._check_reply(item, mail)
            calls += 1
            verdict = found.get("verdict")
            self._audit({"type": "mail_reply_check", "item_id": item["id"], "number": item.get("number", 0),
                         "reply_id": mid, "verdict": verdict, "usage": found.get("_usage")})
            if verdict not in (ANSWERED, PARTIAL, ACK_ONLY):
                continue
            reply = {
                "id": mid,
                "link_id": link_id,
                "at": mail["received"].isoformat(),
                "by": self._staff_name(mail["sender"]),
                "verdict": verdict,
                "summary": str(found.get("reply_summary") or "").strip(),
                "remaining": str(found.get("remaining") or "").strip(),
                "truncated": bool(mail.get("truncated")),
            }
            item.setdefault("replies", []).append(reply)
            if verdict == ANSWERED:
                self._close(state, item, "reply", 0, now)
                self._post_reply_confirmed(state, item, reply)
            else:
                self._post_reply_short(item, reply)
        self._remember(state, mail, key)
        return calls

    def _post_reply_confirmed(self, state: dict, item: dict, reply: dict) -> None:
        """返信を確かめて閉じたことを、To 無しで一言置く（黙って閉じない）。"""
        lines = [
            f"{self._mark(item)} {self._who(item)} への返信を確認しました（{reply['by']}・{self._when(reply['at'])}）。",
            f"返信の内容: {reply['summary']}",
        ]
        lines += self._link_lines("返信のメール", self._reply_link(reply))
        if reply.get("truncated"):
            lines.append("※返信の本文は先頭の部分しか読めていません。見えている範囲で、求めに答えていると判断しました。")
        lines.append(f"{self._mark(item)} は完了にします。{self._remaining_phrase(state)}")
        self._send("\n".join(lines))

    def _post_reply_short(self, item: dict, reply: dict) -> None:
        """返信はあったが、求めに答えきっていないように読めるとき。決めつけず担当者に尋ねる。"""
        kind = "受け取りの連絡や後日の回答の予告" if reply["verdict"] == ACK_ONLY else "求めの一部への回答"
        lines = [
            f"{self._mark(item)} {self._who(item)} への返信を確認しました（{reply['by']}・{self._when(reply['at'])}）が、"
            f"{kind}のように読めました。",
            f"返信の内容: {reply['summary']}",
        ]
        if reply.get("remaining"):
            lines.append(f"残っていそうな点: {reply['remaining']}")
        if item.get("ask"):
            lines.append(f"先方の求め: {item['ask']}")
        lines += self._link_lines("返信のメール", self._reply_link(reply))
        if reply.get("truncated"):
            lines.append("※返信の本文は先頭の部分しか読めていないため、後半は確認できていません。")
        lines.append(
            f"これで済んでいましたら、この投稿への返信で「{self._mark(item)} 対応完了」とお知らせください。"
            "お知らせがあるまで、返信待ちとして残しておきます。"
        )
        mid = self._send(self._with_heads(item["recipients"], lines))
        if mid:
            item.setdefault("post_ids", []).append(mid)

    # --- 知らせる ---

    def _announce(self, state: dict, items: list[dict], now: datetime) -> None:
        today = now.date()
        limit = int(self._settings().get("max_chars", 6000))
        blocks = [(item, self._block(item, today)) for item in items]
        chunks: list[list[tuple[dict, list[str]]]] = [[]]
        for item, block in blocks:
            size = sum(len("\n".join(b)) for _, b in chunks[-1])
            if chunks[-1] and size + len("\n".join(block)) > limit:
                chunks.append([])
            chunks[-1].append((item, block))
        for index, chunk in enumerate(chunks):
            members = [item for item, _ in chunk]
            if len(items) == 1:
                opening = "取引先からメールが届いています。"
            elif index == 0:
                opening = f"取引先からのメールが{len(items)}件届いています。"
            else:
                opening = "（続き）"
            lines = [opening]
            for _, block in chunk:
                lines.append("")
                lines += block
            if any(i.get("number") for i in members):
                example = circled(next(int(i["number"]) for i in members if i.get("number")))
                lines.append("")
                lines.append(
                    "番号の付いたメールは、返信を送ると内容を確認して完了にします。返信以外の方法で済ませたとき、"
                    f"返信が要らないときは、この投稿への返信で「{example} 対応完了」のようにお知らせください。"
                )
            mid = self._send(self._with_heads(_unique(a for i in members for a in i["recipients"]), lines))
            for item in members:
                item["posted_at"] = now.isoformat()
                if mid:
                    item.setdefault("post_ids", []).append(mid)

    def _block(self, item: dict, today: date) -> list[str]:
        """届いたメール1通の知らせ。"""
        head = f"{self._mark(item)} {self._who(item)}"
        if item.get("follow_of"):
            head += f"（{circled(int(item['follow_of']))}の続き）"
        lines = [head + "　" + self._to_phrase(item), f"件名: {item['subject']}", f"内容: {item['summary']}"]
        ask = f"（{item['ask']}）" if item.get("ask") else ""
        lines.append(f"返信: 必要{ask}" if item["needs_reply"] else f"返信: 不要と読みました{ask}")
        lines.append("期日: " + self._deadline_phrase(item, today))
        attached = f"　添付{item['attachments']}件" if item.get("attachments") else ""
        lines.append(f"受信: {self._when(item['received_at'])}{attached}")
        lines += self._link_lines("メール", self._item_link(item))
        if item.get("cut_off") and item.get("source") == IMAP:
            lines.append("※本文が長いため、先頭の部分だけで判断しています。メール本体もご確認ください。")
        elif item.get("cut_off"):
            lines.append("※本文の後半が通知に表示されていないため、先頭の部分だけで判断しています。メール本体もご確認ください。")
        if item.get("follow_of") and not item.get("number"):
            earlier = circled(int(item["follow_of"]))
            lines.append(
                f"※先に届いた{earlier}は、この連絡で返信・対応が要らなくなったと読み、完了にしました。"
                f"違っていれば「{earlier} 戻して」とお知らせください。"
            )
        return lines

    def _to_phrase(self, item: dict) -> str:
        if item.get("staff"):
            via = f"（{item['via']}）" if item.get("via") and item["via"] != "宛名" else ""
            return "→ " + "・".join(f"{n}さん" for n in item["staff"]) + via
        everyone = "・".join(f"{s['name']}さん" for s in self._staff())
        addressee = f"宛名が「{item['addressee']}」の" if item.get("addressee") else "宛名の記載が無い"
        return f"→ {addressee}ため、{everyone}へ"

    def _deadline_phrase(self, item: dict, today: date) -> str:
        if item.get("deadline"):
            day = date.fromisoformat(item["deadline"])
            left = (day - today).days
            state = f"【{-left}日超過】" if left < 0 else ("【本日】" if left == 0 else f"・あと{left}日")
            return f"{jp_date(day)}{state}　原文「{item.get('deadline_quote', '')}」"
        if item.get("deadline_unsure"):
            return f"「{item.get('deadline_quote', '')}」とありますが、日付を確定できませんでした。メールでご確認ください。"
        return "記載なし"

    # --- 毎朝の一覧と、期日当日の確認 ---

    def _morning_list(self, state: dict, now: datetime) -> bool:
        """毎朝、返信・対応をまだ確認できていないメールを一覧で知らせる（1日1回）。→ 期日当日の確認も兼ねたか"""
        settings = self._settings()
        today = now.date()
        marks = state.setdefault("lists", {})
        if marks.get("morning") == today.isoformat():
            return False
        if now < at_clock(today, parse_clock(settings.get("morning_time"), (9, 0))):
            return False
        marks["morning"] = today.isoformat()
        expired = self._expire(state, now)
        days_before = int(settings.get("deadline_only_days_before", 3))
        due = []
        for item in state["open"]:
            posted = _parse_iso(item.get("posted_at"))
            if posted is None or posted.date() >= today:
                continue  # 今日知らせた分は、知らせ自体に書いてある
            if not item.get("needs_reply") and item.get("deadline"):
                # 返信は要らないが期日のあるもの: 期日が近づいてから載せる
                if (date.fromisoformat(item["deadline"]) - today).days > days_before:
                    continue
            due.append(item)
        if not due and not expired:
            return False
        lines = []
        if due:
            lines.append(f"おはようございます。返信・対応をまだ確認できていないメールが{len(due)}件あります。")
            lines += self._list_lines(due, today)
            lines.append(self._report_hint(due))
        if expired:
            lines.append(
                f"※{'・'.join(self._mark(i) for i in expired)} は、受信から{int(settings.get('max_open_days', 30))}日を過ぎたため"
                "一覧から外しました。"
            )
        mid = self._send(self._with_heads(_unique(a for i in due for a in i["recipients"]), lines))
        self._link(due, mid)
        self._audit({"type": "mail_list", "kind": "morning", "numbers": [int(i.get("number") or 0) for i in due]})
        return bool(due) and now >= at_clock(today, parse_clock(settings.get("final_time"), (15, 0)))

    def _final_check(self, state: dict, now: datetime) -> None:
        """期日当日の午後に、まだ済んでいない分をもう一度知らせる（1日1回）。

        期日が土日のものは、その前の最後の平日に知らせる。
        """
        settings = self._settings()
        today = now.date()
        marks = state.setdefault("lists", {})
        if marks.get("final") == today.isoformat():
            return
        if now < at_clock(today, parse_clock(settings.get("final_time"), (15, 0))):
            return
        marks["final"] = today.isoformat()
        upcoming = next_business_day(today)
        due = [
            i for i in state["open"]
            if i.get("deadline") and today <= date.fromisoformat(i["deadline"]) < upcoming
        ]
        if not due:
            return
        lines = [
            f"期日が今日（または次の平日より前）のメールで、返信・対応をまだ確認できていないものが{len(due)}件あります。",
        ]
        lines += self._list_lines(due, today)
        lines.append(self._report_hint(due))
        mid = self._send(self._with_heads(_unique(a for i in due for a in i["recipients"]), lines))
        self._link(due, mid)
        self._audit({"type": "mail_list", "kind": "final", "numbers": [int(i.get("number") or 0) for i in due]})

    def _expire(self, state: dict, now: datetime) -> list[dict]:
        """長く残ったものを一覧から外す（際限なく毎朝聞かない）。"""
        limit = int(self._settings().get("max_open_days", 30))
        old = [
            i for i in state["open"]
            if (now - (_parse_iso(i.get("received_at")) or now)).days > limit
            and not (i.get("deadline") and date.fromisoformat(i["deadline"]) >= now.date())
        ]
        for item in old:
            self._close(state, item, "expired", 0, now)
        return old

    def _list_lines(self, items: list[dict], today: date) -> list[str]:
        lines = []
        for item in sorted(items, key=lambda i: self._urgency(i, today)):
            lines.append(f"{self._mark(item)}{self._status_label(item, today)} {self._who(item)}　{self._to_phrase(item)}")
            lines.append(f"　件名: {item['subject']}")
            lines.append(f"　先方の求め: {item.get('ask') or item.get('summary', '')}")
            link = self._item_link(item)
            lines.append(f"　{self._when(item['received_at'])} 受信" + (f"　メール: {message_url(self._room_id(), link)}" if link else ""))
            for reply in (item.get("replies") or [])[-1:]:
                note = f"　※{self._when(reply['at'])}に{reply['by']}の返信を確認しましたが、まだ残っていそうな点があります"
                lines.append(note + (f"（{reply['remaining']}）" if reply.get("remaining") else ""))
        return lines

    @staticmethod
    def _urgency(item: dict, today: date) -> tuple:
        if item.get("deadline"):
            return (0, (date.fromisoformat(item["deadline"]) - today).days, int(item.get("number") or 0))
        return (1, str(item.get("received_at", "")), int(item.get("number") or 0))

    @staticmethod
    def _status_label(item: dict, today: date) -> str:
        if item.get("deadline"):
            day = date.fromisoformat(item["deadline"])
            left = (day - today).days
            if left < 0:
                return f"【期日超過・{short_date(day)}まで】"
            if left == 0:
                return "【本日期日】"
            return f"【期日 {short_date(day)}・あと{left}日】"
        received = _parse_iso(item.get("received_at"))
        age = (today - received.date()).days if received else 0
        if item.get("deadline_unsure"):
            return f"【期日の日付未確定・受信から{age}日】"
        return f"【期日の記載なし・受信から{age}日】"

    def _report_hint(self, items: list[dict]) -> str:
        example = self._mark(items[0]) if items else "①"
        return (
            "返信を送ると、内容を確認して完了にします。すでに済んでいる場合や返信が要らない場合は、"
            f"「{example} 対応完了」のように番号でお知らせください。"
        )

    # --- 人からの返事 ---

    def _handle_human(self, state: dict, message: dict, me: int, now: datetime) -> None:
        room_id = self._room_id()
        body = str(message.get("body") or "")
        mentions_me = bool(me) and (f"[To:{me}]" in body or f"[rp aid={me} " in body)
        targets = reply_targets(body, room_id)
        linked = [i for i in state["open"] if targets & set(i.get("post_ids") or [])]
        own_text = strip_own_mention(body, me)
        to_others = [a for a in re.findall(r"\[To:(\d+)\]", body) if int(a) != int(me or 0)]
        short = not to_others and len(strip_tags(own_text).strip()) <= SHORT_REPORT_CHARS
        ours = self._is_reply_to_ours(state, targets)
        if not (mentions_me or ours or short):
            return
        open_numbers = {int(i.get("number") or 0) for i in state["open"]}
        closed_numbers = {int(e.get("number") or 0) for e in state.get("closed_log") or []}
        numbers = report_numbers(own_text, known=open_numbers | closed_numbers)

        wants_back = is_undo_request(body) or (is_reopen_request(body) and not says_both(body) and not is_done_report(body))
        if wants_back and (numbers or mentions_me or ours):
            self._reopen(state, message, numbers, now)
            return
        if says_both(body) and numbers:
            self._reply(message, "済んだ分とまだの分が一緒に書かれているようです。取り違えないよう、"
                                 "「③ 対応完了」のように済んだ番号だけを挙げてお知らせください。")
            return
        if is_done_report(body):
            if numbers:
                picked = [i for i in state["open"] if int(i.get("number") or 0) in numbers]
                missing = [n for n in numbers if n not in {int(i.get("number") or 0) for i in picked}]
            elif len(linked) == 1 or (linked and mentions_all(body)):
                picked, missing = linked, []
            else:
                self._ask_which(state, message, linked or state["open"])
                return
            for item in picked:
                self._close(state, item, "manual", _account_of(message), now)
            self._thank(state, message, picked, missing)
            return
        if mentions_me and _LIST_RE.search(unicodedata.normalize("NFKC", strip_tags(own_text))):
            self._reply(message, self._status_text(state, now.date()))
            return
        if mentions_me:
            self._reply(
                message,
                "このルームでは、取引先からのメールの返信・対応の状況を控えています。\n"
                "・済んだとき: 「③ 対応完了」のように番号でお知らせください（返信メールを送った場合は自動で確認します）\n"
                "・戻すとき: 「③ 戻して」\n"
                "・いまの状況: 「一覧」",
            )

    def _is_reply_to_ours(self, state: dict, targets: set[str]) -> bool:
        if not targets:
            return False
        known = set(state.get("post_log") or [])
        return bool(targets & known)

    def _ask_which(self, state: dict, message: dict, candidates: list[dict]) -> None:
        if not candidates:
            self._reply(message, "返信・対応待ちのメールは、いまありません。")
            return
        today = self._now().date()
        lines = [
            f"ありがとうございます。返信・対応待ちが{len(candidates)}件あるので、どのメールが済んだか番号を添えてお知らせください"
            f"（例:「{self._mark(candidates[0])} 対応完了」）。"
        ]
        lines += self._list_lines(candidates, today)
        mid = self._reply(message, "\n".join(lines))
        self._link(candidates, mid)

    def _thank(self, state: dict, message: dict, closed: list[dict], missing: list[int]) -> None:
        lines = []
        if closed:
            lines.append(f"ありがとうございます。{'・'.join(self._mark(i) for i in closed)} を完了にしました。")
        if missing:
            lines.append(
                f"{'・'.join(circled(n) for n in missing)} に当たるメールは、返信・対応待ちの中に見当たりませんでした。"
            )
        lines.append(self._remaining_phrase(state))
        if state["open"] and (missing or not closed):
            lines += self._list_lines(state["open"], self._now().date())
        mid = self._reply(message, "\n".join(lines))
        if missing or not closed:
            self._link(state["open"], mid)

    def _reopen(self, state: dict, message: dict, numbers: list[int], now: datetime) -> None:
        """「③ 戻して」: 閉じたメールを返信・対応待ちへ戻す。番号が無ければ直前に閉じた分を尋ねる。"""
        open_numbers = {int(i.get("number") or 0) for i in state["open"]}
        log = state.get("closed_log") or []
        restored, still_open, unknown = [], [], []
        for n in numbers:
            if n in open_numbers:
                still_open.append(n)
                continue
            hits = [e for e in log if int(e.get("number") or 0) == n and e.get("how") in ("manual", "reply", "superseded")]
            if not hits:
                unknown.append(n)
                continue
            entry = max(hits, key=lambda e: str(e.get("closed_at", "")))
            log.remove(entry)
            item = {k: v for k, v in entry.items() if k not in ("closed_at", "how", "closed_by")}
            item["reopened_at"] = now.isoformat()
            state["open"].append(item)
            restored.append(item)
            self._audit({"type": "mail_reopen", "item_id": item["id"], "number": n, "by": _account_of(message)})
        lines = []
        if restored:
            lines.append(f"{'・'.join(self._mark(i) for i in restored)} を返信・対応待ちへ戻しました。")
        if still_open:
            lines.append(f"{'・'.join(circled(n) for n in still_open)} は、返信・対応待ちのままです。")
        if unknown:
            lines.append(f"{'・'.join(circled(n) for n in unknown)} に当たるメールは、戻せる履歴に見当たりませんでした。")
        if not numbers:
            lines.append("どのメールを戻すか、「③ 戻して」のように番号でお知らせください。")
        lines.append(self._remaining_phrase(state))
        mid = self._reply(message, "\n".join(lines))
        self._link(restored, mid)

    def _status_text(self, state: dict, today: date) -> str:
        items = state["open"]
        if not items:
            return "返信・対応待ちのメールは、いまありません。"
        return "\n".join([f"返信・対応待ちのメールは{len(items)}件です。"] + self._list_lines(items, today))

    def _remaining_phrase(self, state: dict) -> str:
        count = len(state["open"])
        return f"返信・対応待ちのメールは、あと{count}件です。" if count else "返信・対応待ちのメールは、これでありません。"

    # --- 共通の部品 ---

    def _close(self, state: dict, item: dict, how: str, by: int, now: datetime) -> None:
        if item in state["open"]:
            state["open"].remove(item)
        entry = {**item, "closed_at": now.isoformat(), "how": how, "closed_by": int(by or 0)}
        log = state.setdefault("closed_log", [])
        log.append(entry)
        del log[:-MAX_CLOSED_LOG]
        self._audit({"type": "mail_done", "item_id": item.get("id"), "number": int(item.get("number") or 0),
                     "how": how, "by": int(by or 0)})

    def _take_number(self, state: dict, now: datetime) -> int:
        """通し番号。返信・対応待ちが無くなっていて、番号が大きくなっていれば①から振り直す。"""
        numbering = state.setdefault("numbering", {})
        n = int(numbering.get("next", 1))
        restart = int((self._settings().get("numbering") or {}).get("restart_after", 30) or 0)
        if restart and n > restart and not state["open"]:
            n = 1
        numbering["next"] = n + 1
        return n

    def _link(self, items: list[dict], mid: str) -> None:
        """一覧や聞き返しの投稿を、そこに載せたメールに結び付ける（その投稿への返信で分かるように）。"""
        if not mid:
            return
        for item in items:
            ids = item.setdefault("post_ids", [])
            if mid not in ids:
                ids.append(mid)

    def _mark(self, item: dict) -> str:
        n = int(item.get("number") or 0)
        return circled(n) if n else UNNUMBERED_MARK

    @staticmethod
    def _who(item: dict) -> str:
        return str(item.get("partner") or "").strip() or str(item.get("sender") or "差出人不明")

    @staticmethod
    def _when(iso: Any) -> str:
        at = _parse_iso(iso)
        return f"{at.month}/{at.day}（{WEEKDAYS[at.weekday()]}）{at:%H:%M}" if at else ""

    def _settings(self) -> dict:
        return self._config.mail_watch or {}

    def _room_id(self) -> int:
        return int(self._settings().get("room_id", 0) or 0)

    def _staff(self) -> list[dict]:
        staff = []
        for raw in self._settings().get("staff") or []:
            if raw.get("name") and raw.get("account_id"):
                staff.append({
                    "name": str(raw["name"]),
                    "account_id": int(raw["account_id"]),
                    "addresses": [str(a).lower() for a in raw.get("addresses") or []],
                })
        return staff

    def _staff_name(self, address: str) -> str:
        """当社の送信者の呼び方。担当者（staff）と、名前だけ分かる社員（colleagues）から引く。"""
        people = self._staff() + [
            {"name": str(c.get("name", "")), "addresses": [str(a).lower() for a in c.get("addresses") or []]}
            for c in self._settings().get("colleagues") or []
        ]
        for person in people:
            if person["name"] and str(address).lower() in person["addresses"]:
                return f"{person['name']}さん"
        return str(address)

    def _heads(self, account_ids: list[int]) -> str:
        names = {s["account_id"]: s["name"] for s in self._staff()}
        return " ".join(f"[To:{a}]{names.get(a, '')}さん" if names.get(a) else f"[To:{a}]" for a in account_ids)

    def _with_heads(self, account_ids: list[int], lines: list[str]) -> str:
        heads = self._heads(account_ids)
        return "\n".join(([heads] if heads else []) + lines)

    def _me(self) -> int:
        try:
            return int(self._chatwork.get_me())
        except Exception:
            print("[warn] 自アカウントIDの取得に失敗: " + traceback.format_exc(), flush=True)
            return 0

    def _send(self, text: str) -> str:
        """ルームへ投稿する。→ 投稿ID（失敗したら空）。自分の投稿は控えておく（返信先の判定用）。"""
        try:
            mid = str(self._chatwork.send_message(self._room_id(), text) or "")
        except Exception:
            print("[warn] 営業メールのルームへの投稿に失敗: " + traceback.format_exc(), flush=True)
            return ""
        if mid:
            self._posted.append(mid)
        return mid

    def _reply(self, message: dict, text: str) -> str:
        tag = f"[rp aid={_account_of(message)} to={self._room_id()}-{message.get('message_id')}]"
        return self._send(f"{tag}\n{text}")

    def _report_unread(self, mail: dict) -> None:
        """何度やっても読めなかったメール。種類を決めつけず、To 無しで置く。"""
        if self._is_ours(mail):
            return  # 当社が送ったメールは知らせる対象ではない（返信の確認だけに使う）
        lines = [
            "次のメールは、こちらで内容を確認できませんでした。メールをご確認ください。",
            f"差出人: {mail.get('sender', '')}",
            f"件名: {mail.get('subject', '')}",
        ]
        lines += self._link_lines("メール", str(mail.get("link_id") or "") or self._find_notice(mail))
        self._send("\n".join(lines))
        self._audit({"type": "mail_unread", "message_id": str(mail.get("ref"))})

    # --- メールの出どころ（サーバー／通知ルーム）にまたがる部品 ---

    def _is_ours(self, mail: dict) -> bool:
        """当社が送ったメールか（送信済みフォルダのメール、または当社のドメインからのメール）。"""
        return mail.get("folder") == "sent" or matches_sender(mail.get("sender", ""), self._settings().get("own_domains"))

    def _seen_before(self, state: dict, mail: dict, key: str) -> bool:
        """同じメールをもう読んだか。

        メールサーバー同士は Message-ID で、通知ルーム同士は差出人・件名・受信日時の一致で、
        サーバーと通知ルームの間は差出人・件名が同じで受信が近いことで見分ける。
        """
        message_id = str(mail.get("message_id") or "")
        if message_id and message_id in (state.get("seen_ids") or []):
            return True
        source = mail.get("source", CHATWORK)
        for entry in state.get("recent") or []:
            if isinstance(entry, str):  # 前の版の控え（"差出人|件名|日時"）
                sender, _, rest = entry.partition("|")
                entry_key, _, when = rest.rpartition("|")
                entry = {"s": sender, "k": entry_key, "t": when, "src": CHATWORK}
            if entry.get("s") != mail["sender"] or entry.get("k") != key:
                continue
            at = _parse_iso(entry.get("t"))
            if at is None:
                continue
            gap = abs((mail["received"] - at).total_seconds())
            if entry.get("src") == source:
                if source == CHATWORK and gap == 0:
                    return True
            elif gap <= CROSS_SOURCE_SECONDS:
                return True
        return False

    def _remember(self, state: dict, mail: dict, key: str) -> None:
        recent = state.setdefault("recent", [])
        recent.append({"s": mail["sender"], "k": key, "t": mail["received"].isoformat(),
                       "src": mail.get("source", CHATWORK)})
        del recent[:-MAX_RECENT]
        if mail.get("message_id"):
            seen = state.setdefault("seen_ids", [])
            seen.append(str(mail["message_id"]))
            del seen[:-MAX_SEEN_IDS]

    def _staff_by_headers(self, mail: dict) -> tuple[list[str], str]:
        """メールの宛先（To）→ CC → 届いた受信箱の持ち主、の順で担当者を決める。"""
        staff = self._staff()
        for field, label in (("to", "メールの宛先"), ("cc", "CC")):
            addresses = set(mail.get(field) or [])
            names = [s["name"] for s in staff if addresses & set(s["addresses"])]
            if names:
                return names, label
        owner = str(mail.get("owner") or "")
        if owner and any(s["name"] == owner for s in staff):
            return [owner], "受信箱"
        return [], ""

    @staticmethod
    def _index_notices(messages: list[dict], notifier: int) -> list[dict]:
        """通知ルームに流れている「新着メール」通知の索引（サーバーで読んだメールに通知へのリンクを添えるため）。"""
        index = []
        for message in messages:
            if _account_of(message) != notifier:
                continue
            notice = parse_mail_notice(message.get("body", ""))
            if notice is None:
                continue
            at = notice["received"] or datetime.fromtimestamp(int(message.get("send_time", 0)), JST)
            index.append({"sender": notice["sender"], "key": thread_key(notice["subject"]), "at": at,
                          "id": str(message.get("message_id"))})
        return index

    def _find_notice(self, mail: dict) -> str:
        """このメールの通知が通知ルームにあれば、その投稿ID。無ければ空。"""
        key = thread_key(mail.get("subject", ""))
        for notice in getattr(self, "_notices", []):
            if notice["sender"] == mail.get("sender") and notice["key"] == key:
                if abs((notice["at"] - mail["received"]).total_seconds()) <= CROSS_SOURCE_SECONDS:
                    return notice["id"]
        return ""

    def _backfill_links(self, state: dict) -> None:
        """知らせた時点では通知がまだ流れていなかったメールに、後からリンクを結び付ける。"""
        for item in state["open"]:
            if item.get("source") == IMAP and not item.get("link_id"):
                at = _parse_iso(item.get("received_at"))
                if at is not None:
                    item["link_id"] = self._find_notice({"sender": item.get("sender"), "subject": item.get("subject", ""),
                                                         "received": at})

    @staticmethod
    def _item_link(item: dict) -> str:
        """メールの通知（通知ルームの投稿）のID。前の版の控えは id がそのまま投稿ID。"""
        if item.get("link_id"):
            return str(item["link_id"])
        return str(item["id"]) if "source" not in item and str(item.get("id", "")).isdigit() else ""

    @staticmethod
    def _reply_link(reply: dict) -> str:
        if reply.get("link_id"):
            return str(reply["link_id"])
        return str(reply["id"]) if "link_id" not in reply and str(reply.get("id", "")).isdigit() else ""

    def _link_lines(self, label: str, message_id: str) -> list[str]:
        return [f"{label}: {message_url(self._room_id(), message_id)}"] if message_id else []

    # --- メールサーバーを直接読む ---

    def _active_mailboxes(self) -> list[dict]:
        """読む受信箱。パスワードの環境変数が入っている箱だけ（入っていない箱は通知ルームの通知で見る）。"""
        settings = self._settings()
        if not (settings.get("imap") or {}).get("enabled", True):
            return []
        return [
            box for box in settings.get("mailboxes") or []
            if box.get("address") and box.get("host") and os.environ.get(str(box.get("password_env") or ""))
        ]

    def _open_reader(self, box: dict) -> Any:
        if self._reader_factory is not None:
            return self._reader_factory(box)
        from .mailbox import MailboxReader

        return MailboxReader(box)

    def _collect_imap(self, state: dict, now: datetime, last: int = 0) -> list[dict]:
        """各受信箱・送信済みの新しいメールを取ってくる（既読にしない）。

        読むのは、受信箱（INBOX）・送信済み・振り分け先のフォルダ（届いたメールがメールソフトの
        振り分けで取引先別のフォルダへ移ることがある。実例 2026-11-24: 森さんの箱で、一部の差出人の
        メールが届いた時刻のままサブフォルダに入っていた）。
        初めて開くフォルダは、いまあるメールを既読として控えるだけ（過去分を流さない）。
        古いメール（`max_age_days` より前に送られたもの）は、後からフォルダへ整理されただけなので読まない。
        """
        from .mailbox import FOLDER, MailboxError

        imap = self._settings().get("imap") or {}
        body_chars = int(imap.get("max_body_chars", 4000))
        quoted_chars = int(imap.get("max_quoted_chars", 2000))
        fetch_limit = int(imap.get("max_fetch_per_folder", 30))
        oldest = now - timedelta(days=int(imap.get("max_age_days", 7)))
        self._imap_new = {}
        mails: list[dict] = []
        for box in self._active_mailboxes():
            name = str(box.get("name") or box["address"])
            try:
                with self._open_reader(box) as reader:
                    targets = [("inbox", "inbox", reader.inbox())]
                    targets += [("sent", "sent:" + f["raw"], f) for f in reader.sent()]
                    targets += [("inbox", "folder:" + f["raw"], f) for f in reader.others()]
                    for kind, key, folder in targets:
                        try:
                            info = reader.open(folder)
                        except MailboxError as exc:
                            if exc.kind != FOLDER or key == "inbox":
                                raise
                            continue  # 開けないフォルダ（サーバーの設定用など）は飛ばす
                        place = state.setdefault("imap", {}).setdefault(name, {}).setdefault(key, {})
                        if last:
                            uids = reader.last_uids(last) if key == "inbox" or kind == "sent" else []
                        elif "last_uid" not in place or place.get("uidvalidity") != info["uidvalidity"]:
                            newest = (reader.last_uids(1) or [0])[-1]
                            place.clear()
                            place.update(uidvalidity=info["uidvalidity"], last_uid=newest, done=[])
                            continue
                        else:
                            done = set(place.get("done") or [])
                            uids = [u for u in reader.uids_after(int(place["last_uid"])) if u not in done]
                            self._imap_new[(name, key)] = uids
                        for uid in uids[:fetch_limit]:
                            mail = reader.fetch(uid)
                            stale = False
                            if mail is not None and mail.get("received") is not None and not last:
                                stale = min(mail["received"], mail.get("written") or mail["received"]) < oldest
                            if mail is None or not mail.get("sender") or mail.get("received") is None or stale:
                                if not last:
                                    place.setdefault("done", []).append(uid)
                                continue
                            text = str(mail.get("text") or "")
                            mail.update(
                                source=IMAP, box=name, folder=kind, place=key, owner=name,
                                ref=f"imap:{name}:{key}:{uid}",
                                text=text[:body_chars], quoted=str(mail.get("quoted") or "")[:quoted_chars],
                                truncated=bool(mail.get("truncated")) or len(text) > body_chars,
                            )
                            mails.append(mail)
                self._mailbox_ok(state, name)
            except MailboxError as exc:
                self._mailbox_failed(state, name, exc, now)
        return mails

    def _imap_done(self, state: dict, mail: dict) -> None:
        if self._dry_run:
            return
        place = state.setdefault("imap", {}).setdefault(mail["box"], {}).setdefault(mail["place"], {})
        done = place.setdefault("done", [])
        if mail["uid"] not in done:
            done.append(mail["uid"])

    def _imap_compact(self, state: dict) -> None:
        """済んだUIDを、続いている分だけ既読位置へ繰り入れる（順番どおりに済むとは限らないため）。"""
        for (name, kind), uids in getattr(self, "_imap_new", {}).items():
            place = state.get("imap", {}).get(name, {}).get(kind)
            if not place:
                continue
            done = set(place.get("done") or [])
            for uid in sorted(uids):
                if uid not in done:
                    break
                place["last_uid"] = uid
            place["done"] = sorted(u for u in done if u > int(place["last_uid"]))

    def _mailbox_failed(self, state: dict, name: str, exc: Exception, now: datetime) -> None:
        """読めなかった受信箱。続いたら管理者ルームへ1日1回知らせる（その間は通知ルームの通知で見張る）。"""
        print(f"[warn] {name} のメールサーバーを読めませんでした: {exc}", flush=True)
        if self._dry_run:
            return
        errors = state.setdefault("imap_errors", {})
        entry = errors.setdefault(name, {"since": now.isoformat(), "count": 0})
        entry["count"] = int(entry.get("count", 0)) + 1
        after = int((self._settings().get("imap") or {}).get("alert_after_failures", 3))
        if entry["count"] < after or entry.get("alerted") == now.date().isoformat():
            return
        if self._tell_admin(
            f"【営業メール】{name}さんのメールサーバーを読めていません（{exc}）。\n"
            "その間は、通知ルームに流れる通知（本文の先頭のみ）で見張りを続けます。"
        ):
            entry["alerted"] = now.date().isoformat()

    def _mailbox_ok(self, state: dict, name: str) -> None:
        entry = (state.get("imap_errors") or {}).pop(name, None)
        if entry and entry.get("alerted") and not self._dry_run:
            self._tell_admin(f"【営業メール】{name}さんのメールサーバーを、また読めるようになりました。")

    def _tell_admin(self, text: str) -> bool:
        room = getattr(self._config, "admin_room_id", None)
        if not room:
            return False
        heads = " ".join(f"[To:{int(a)}]" for a in (getattr(self._config, "admin_account_ids", None) or []))
        try:
            self._chatwork.send_message(int(room), (heads + "\n" if heads else "") + text)
            return True
        except Exception:
            print("[warn] 管理者ルームへの知らせに失敗: " + traceback.format_exc(), flush=True)
            return False

    # --- モデル ---

    def _read(self, mail: dict, earlier: list[dict]) -> dict:
        ask = "次のメールを仕分けてください。\n\n" + mail_text(mail)
        if earlier:
            ask += "\n\n[先に届いて、まだ返信・対応が済んでいないメール]"
            for old in earlier:
                due = f"／期日 {old['deadline']}" if old.get("deadline") else ""
                ask += f"\n- {self._when(old.get('received_at'))} 受信: {old.get('summary', '')}／求め: {old.get('ask', '') or 'なし'}{due}"
        names = [s["name"] for s in self._staff()]
        system = READ_SYSTEM.format(company=self._company(), staff="、".join(names) or "（なし）")
        return self._ask_model(system, read_schema(names), ask)

    def _check_reply(self, item: dict, mail: dict) -> dict:
        due = f"\n期日: {item['deadline']}" if item.get("deadline") else ""
        ask = (
            "[届いたメール]\n"
            f"差出人: {self._who(item)}（{item.get('sender', '')}）\n"
            f"受信: {self._when(item.get('received_at'))}\n"
            f"件名: {item.get('subject', '')}\n"
            f"内容: {item.get('summary', '')}\n"
            f"先方の求め: {item.get('ask', '') or '（とくに書かれていない）'}{due}\n\n"
            "[当社が送ったメール]\n" + mail_text(mail)
        )
        return self._ask_model(CHECK_SYSTEM.format(company=self._company()), CHECK_SCHEMA, ask)

    def _company(self) -> str:
        return str(self._settings().get("company_name") or "RAKUTENKEN株式会社（樂天軒本店）")

    def _ask_model(self, system: str, schema: dict, text: str) -> dict:
        from .answer import _call_with_continuation

        cfg = self._config
        try:
            if self._client is None:
                import anthropic

                self._client = anthropic.Anthropic()
            kwargs = {
                "model": cfg.model,
                "max_tokens": int(self._settings().get("max_tokens", 2000)),
                "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                "system": system,
            }
            response, usage, _ = _call_with_continuation(
                self._client.messages.create, kwargs, [{"role": "user", "content": text}]
            )
        except Exception as exc:
            print("[warn] 営業メールの読み取りに失敗: " + traceback.format_exc(), flush=True)
            raise ModelUnavailable(str(exc)) from exc
        self._usage = _merge_usage(self._usage, usage)
        if self._cost is not None:
            try:
                self._cost.add_usage(usage)
            except Exception:
                print("[warn] コスト計上に失敗: " + traceback.format_exc(), flush=True)
        reply = next(
            (b.text for b in getattr(response, "content", []) if getattr(b, "type", "") == "text"), ""
        )
        try:
            found = json.loads(reply)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ModelUnavailable("応答を読めませんでした") from exc
        if not isinstance(found, dict):
            raise ModelUnavailable("応答を読めませんでした")
        found["_usage"] = usage
        return found

    # --- 記録と状態 ---

    def _audit(self, record: dict) -> None:
        if self._dry_run:
            return
        try:
            from .audit import AuditLogger

            cfg = self._config
            AuditLogger(
                log_dir=cfg.resolve_path(cfg.audit_log_dir),
                retention_days=cfg.audit_log_retention_days,
            ).log({"room_id": self._room_id(), **{k: v for k, v in record.items() if v is not None}})
        except Exception:
            print("[warn] 監査ログの記録に失敗: " + traceback.format_exc(), flush=True)

    @staticmethod
    def _blank_state() -> dict:
        return {"last_seen": None, "open": [], "closed_log": [], "numbering": {"next": 1}, "lists": {},
                "retries": {}, "post_log": [], "recent": [], "seen_ids": [], "imap": {}, "imap_errors": {}}

    def _load_state(self) -> dict:
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        for key, empty in self._blank_state().items():
            state.setdefault(key, empty)
        return state

    def _save_state(self, state: dict) -> None:
        del state["post_log"][:-MAX_CLOSED_LOG]
        if self._dry_run:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._state_path.with_suffix(".tmp")
            temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            temp.replace(self._state_path)
        except OSError:
            print("[warn] 営業メールの状態を保存できませんでした", flush=True)


def _unique(values: Any) -> list[int]:
    seen: list[int] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


# --- 手で試す ---


class _Recorder:
    """--dry-run 用。読むのは本物、投稿は記録するだけ。"""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.sent: list[str] = []

    def get_me(self) -> int:
        return self._real.get_me()

    def get_recent_messages(self, room_id: int, limit: int = 100) -> list[dict]:
        return self._real.get_recent_messages(room_id, limit=limit)

    def send_message(self, room_id: int, body: str) -> str:
        self.sent.append(body)
        return f"dry{len(self.sent)}"


def main(argv: list[str] | None = None) -> None:
    import argparse

    from .chatwork import ChatworkClient
    from .config import Config

    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="営業メールの見張り")
    parser.add_argument("--dry-run", action="store_true", help="投稿も記録もせず、直近のメールでの動きを表示する")
    parser.add_argument("--last", type=int, default=30, help="--dry-run で見る直近の発言数")
    parser.add_argument("--imap-last", type=int, default=0,
                        help="--dry-run で、メールサーバーの各フォルダの直近この通数も読んで試す")
    args = parser.parse_args(argv)
    config = Config.load()
    chatwork = ChatworkClient(config.chatwork_api_token or "")
    if not args.dry_run:
        print(f"知らせたメール: {MailWatcher(config, chatwork).run_once()}件")
        return
    recorder = _Recorder(chatwork)
    watcher = MailWatcher(config, recorder, dry_run=True)
    messages = recorder.get_recent_messages(int(config.mail_watch.get("room_id", 0)), limit=100)
    count = watcher.replay(messages, imap_last=args.imap_last, last=args.last)
    for body in recorder.sent:
        print("=" * 60)
        print(body)
    print("=" * 60)
    print(f"通知ルームの発言 直近{args.last}件／メールサーバー 各フォルダ直近{args.imap_last}通／"
          f"知らせるメール {count}件／モデルの使用量 {watcher._usage}")


if __name__ == "__main__":
    main()
