"""支払通知書の月次発行 — 業務委託先の証憑を受け付け、支払通知書を作り、承認を得て交付する。

対象は、請求書を受け取らず当社から支払通知書（契約書でいう支払明細書）を交付する業務委託契約
（指示 2026-11-20: 宮﨑大樹さんと株式会社ヤマトライジングの業務委託契約）。

毎月の流れ:
  1. 受付 … 委託先が専用ルームに領収書などの画像を送る。読み取って、読めた内容を返信で見せる。
            どの月の分かは**提出日**で決まる（契約7条2項・4項。月末を過ぎた分は翌月分）
  2. 作成 … 翌月の第1営業日に、スプレッドシートのひな形タブを複製して当月分を作り、PDFにする。
            1行目は定例の業務委託費（発行日=作成日、日付=締日）、その下に証憑ごとに1行
  3. 承認 … 承認ルームで承認者にPDFを見せる。**承認者のうち1名の「承認」**で先へ進む。
            直しの指示（「②を外して」「業務委託費を50,000円に」）は作り直してもう一度見せる
  4. 交付 … 承認された版のPDFをそのまま、委託先のルームへ添付する
            （期限は締日の翌日から起算して5営業日。契約6条2項）
  5. 引継ぎ … 承認ルームに、交付した旨と支払予定日（翌月15日。休業日なら前営業日）を置く。振込はしない

方針:
- **承認なしに交付しない**。交付するのは承認された版そのもの（保存したPDFのバイト列）
- 金額は領収書の画像から読むので、承認依頼に1行ずつ並べ、元の画像へのリンクを付ける。
  金額や日付を読めなかった証憑は載せず、承認者に渡す（決めつけない）
- 合計はコードで計算し、スプレッドシートの合計と突き合わせる。合わなければ出さない
- 自分で作ったタブ以外は消さない（同じ名前のタブが既にあれば止めて知らせる）
- 委託先のルームは許可ルーム（Q&Aの対象）に入れない。委託先からの連絡は答えずに承認者へ取り次ぐ
- 本番（`live`）にするまでは、リハーサル（承認依頼も交付も管理者ルーム）だけが動く

手で動かす:
  python -m raizuinu.paynotice --rehearse 2026-10 --receipt 領収書.jpg   （管理者ルームで通し稽古）
  python -m raizuinu.paynotice --read 領収書.jpg                         （読み取りだけ試す）
  python -m raizuinu.paynotice --status
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import traceback
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from .faxwatch import JST, WEEKDAYS, _account_of, _parse_iso, at_clock, circled, message_url, parse_clock, \
    reply_targets, strip_own_mention, strip_tags

LIVE, REHEARSAL = "live", "rehearsal"
AWAITING, ISSUED = "awaiting", "issued"
OK, REVIEW, EXCLUDED = "ok", "review", "excluded"

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
               ".webp": "image/webp"}
MAX_IMAGE_BYTES = 4_500_000   # これを超える画像は縮めてから読む（縮められなければ人に渡す）
MAX_IMAGE_SIDE = 2000
MAX_AMOUNT = 10_000_000       # 読み違いの歯止め（これを超える金額は人が確かめる）
MAX_RELAY_CHARS = 300
MAX_OWNED = 300               # 承認ルームでの自分の投稿を覚えておく数（返信の見分け用）

_FILE_RE = re.compile(r"\[download:(\d+)\](.*?)\[/download\]", re.S)
_SIZE_TAIL_RE = re.compile(r"\s*\([\d.,]+\s*[KMG]?B\)\s*$", re.I)


class NoticeError(RuntimeError):
    """支払通知書を作れなかった（人に知らせて止める）。"""


class ModelUnavailable(Exception):
    """モデルに読ませられなかった。次の巡回でやり直す。"""


# --- 月と営業日 ---


def period_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def period_bounds(period: str) -> tuple[date, date]:
    """"2026-11" → (11/1, 11/30)。"""
    year, month = (int(part) for part in period.split("-"))
    first = date(year, month, 1)
    return first, (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def previous_period(day: date) -> str:
    return period_of(day.replace(day=1) - timedelta(days=1))


def period_label(period: str) -> str:
    year, month = (int(part) for part in period.split("-"))
    return f"{year}年{month}月"


def jp_date(day: date) -> str:
    return f"{day.month}月{day.day}日（{WEEKDAYS[day.weekday()]}）"


def yen(amount: int) -> str:
    return f"{int(amount):,}円"


class Calendar:
    """営業日（平日。祝日と会社の休業日を除く）。"""

    def __init__(self, holidays: Iterable[str] = (), closed: Iterable[str] = ()) -> None:
        self._off = set(holidays)
        for entry in closed or ():
            start, _, end = str(entry).partition("/")
            try:
                day, last = date.fromisoformat(start), date.fromisoformat(end or start)
            except ValueError:
                continue
            while day <= last:
                self._off.add(day.isoformat())
                day += timedelta(days=1)

    def is_business_day(self, day: date) -> bool:
        return day.weekday() < 5 and day.isoformat() not in self._off

    def nth_after(self, base: date, n: int) -> date:
        """base の翌日から起算して n 営業日目（11/30 の翌日から5営業日 → 12/7）。"""
        day, count = base, 0
        while count < n:
            day += timedelta(days=1)
            if self.is_business_day(day):
                count += 1
        return day

    def days_left(self, today: date, deadline: date) -> int:
        """今日から期限までの営業日数（今日を含む。期限を過ぎていれば0以下）。"""
        if today > deadline:
            return -sum(1 for d in _days(deadline + timedelta(days=1), today) if self.is_business_day(d))
        return sum(1 for d in _days(today, deadline) if self.is_business_day(d))

    def on_or_before(self, day: date) -> date:
        """その日が休業日なら前の営業日（支払日が金融機関の休業日のとき。契約6条3項）。"""
        while day.weekday() >= 5 or day.isoformat() in self._off:
            day -= timedelta(days=1)
        return day


def _days(start: date, end: date) -> Iterable[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


# --- 行と合計 ---


MAX_LABEL_CHARS = 30  # 「内容」欄に収まる長さ（超えると枠で切れる）
_CORP_RE = re.compile(r"株式会社|有限会社|合同会社|\(株\)|（株）|㈱")
_PAREN_RE = re.compile(r"[（(][^（）()]*[）)]")


def receipt_label(receipt: dict) -> str:
    """支払通知書の「内容」欄。「立替：交通費（近鉄奈良〜大阪難波） 近畿日本鉄道」

    欄に収まるよう、支払先の「株式会社」と括弧書きは省き、長ければ支払先を落とし、それでも長ければ切る。
    """
    purpose = str(receipt.get("purpose") or "").strip() or "経費"
    payee = _PAREN_RE.sub("", _CORP_RE.sub("", str(receipt.get("payee") or ""))).strip()
    base = f"立替：{purpose}"
    if payee:
        full = f"{base} {payee}" if _PAREN_RE.search(purpose) else f"{base}（{payee}）"
        if len(full) <= MAX_LABEL_CHARS:
            return full
    return base if len(base) <= MAX_LABEL_CHARS else base[: MAX_LABEL_CHARS - 1] + "…"


def build_lines(contract: dict, period: str, receipts: list[dict], overrides: dict | None = None) -> list[dict]:
    """支払通知書の行（1行目が定例の業務委託費、その下に証憑ごとに1行、最後に手で足した行）。"""
    overrides = overrides or {}
    fee = contract.get("fee") or {}
    expense = contract.get("expense") or {}
    _, closing = period_bounds(period)
    lines = [{
        "kind": "fee",
        "date": closing.isoformat(),
        "label": str(overrides.get("fee_label") or fee.get("label") or "業務委託費"),
        "exempt": False,
        "quantity": fee.get("quantity", 1),
        "unit": str(fee.get("unit") or "式"),
        "price": int(overrides["fee_amount"] if overrides.get("fee_amount") is not None else fee.get("amount", 0)),
    }]
    exempt = bool(expense.get("tax_exempt", True))
    for receipt in sorted(receipts, key=lambda r: (str(r.get("date") or ""), str(r.get("id")))):
        if receipt.get("period") != period or receipt.get("status") != OK:
            continue
        lines.append({
            "kind": "receipt",
            "receipt_id": receipt["id"],
            "date": receipt["date"],
            "label": str(receipt.get("label") or receipt_label(receipt)),
            "exempt": exempt,
            "quantity": 1,
            "unit": str(expense.get("unit") or "式"),
            "price": int(receipt["amount"]),
        })
    for extra in overrides.get("added") or []:
        lines.append({"kind": "added", "quantity": 1, "unit": str(expense.get("unit") or "式"), **extra})
    return lines


def line_key(line: dict) -> str:
    """行を見分ける鍵（番号を版をまたいで保つため）。定例の行／証憑／手で足した行。"""
    if line.get("kind") == "fee":
        return "fee"
    if line.get("receipt_id"):
        return f"r:{line['receipt_id']}"
    return f"a:{line.get('uid') or line.get('label')}"


def total_of(lines: list[dict]) -> int:
    return sum(int(line["quantity"]) * int(line["price"]) for line in lines)


def check_receipt(receipt: dict, submitted: date, term_start: date | None, others: list[dict]) -> list[str]:
    """読み取った証憑の点検。→ 気になる点（空なら問題なし）。金額・日付が無ければ載せない。"""
    issues = []
    amount = receipt.get("amount")
    if not isinstance(amount, int) or amount <= 0:
        issues.append("金額を読み取れませんでした")
    elif amount > MAX_AMOUNT:
        issues.append(f"金額が大きい（{yen(amount)}）ため、読み違いでないかご確認ください")
    day = None
    try:
        day = date.fromisoformat(str(receipt.get("date") or ""))
    except ValueError:
        issues.append("日付を読み取れませんでした")
    if day:
        if day > submitted:
            issues.append(f"日付（{day.isoformat()}）が提出日より後です")
        if term_start and day < term_start:
            issues.append(f"日付（{day.isoformat()}）が契約の開始日より前です")
    if day and isinstance(amount, int) and amount > 0:
        for other in others:
            if other.get("status") == EXCLUDED:
                continue
            if other.get("date") == receipt.get("date") and other.get("amount") == amount \
                    and _loose(other.get("payee")) == _loose(receipt.get("payee")):
                issues.append("同じ日付・金額・支払先の証憑が既にあります（重複の疑い）")
                break
    return issues


_REGISTRATION_RE = re.compile(r"^T\d{13}$")


def clean_registration(text: Any) -> tuple[str, bool]:
    """適格請求書発行事業者の登録番号 → (整えた番号, 形が正しいか)。正しい形は T＋数字13桁。

    実例（2026-11-20 の読み取りテスト）: 駐車場の領収書の番号を「T41200001210276」（数字14桁）と読んだ。
    桁数が合わない番号は読み違いの疑いがあるので、そのまま信じない。
    """
    value = re.sub(r"[\s\-‐－ー]", "", unicodedata.normalize("NFKC", str(text or ""))).upper()
    return value, bool(_REGISTRATION_RE.match(value))


def blocks_listing(issues: list[str]) -> bool:
    """載せられない問題か（金額・日付が読めない、金額が大きすぎる）。"""
    return any("読み取れません" in i or "金額が大きい" in i for i in issues)


def _loose(text: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")))


# --- モデルへの指示 ---

RECEIPT_SYSTEM = """あなたは経理担当の補助として、領収書・レシート・利用明細の画像を読み取ります。
画像に写っている証憑を1枚ずつ、次の項目で書き出してください。1つの画像に複数枚写っていれば、枚数分を書きます。
証憑でない画像（風景、書類の一部で金額が無いものなど）なら、receipts は空の配列にして note に理由を書きます。

- date: 支払った日（発行日・利用日）。YYYY-MM-DD。和暦は西暦に直す。読めなければ空。
- payee: 支払先（店名・会社名・交通機関名）。書かれているとおりに。読めなければ空。
- amount: 支払った金額の合計（税込）。整数の円。お預かり額やお釣りではなく、合計・領収金額を書く。読めなければ 0。
- purpose: 何の支払いかを短く（「交通費（近鉄奈良〜大阪難波）」「駐車料金」「文具」「会議飲食代」）。品目や但し書きから読み取れる範囲で。分からなければ空。
- kind: 「領収書」「レシート」「利用明細」「その他」のいずれか。
- tax_note: 税率の記載（「10%」「8%（軽減）」「10%と8%」「記載なし」）。
- registration: 適格請求書発行事業者の登録番号（T＋13桁）。記載が無ければ空。

書かれていることだけを書き、推測で埋めないでください。数字が不鮮明で確信が持てないときは、amount を 0 にして note にその旨を書きます。"""

RECEIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "receipts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "payee": {"type": "string"},
                    "amount": {"type": "integer"},
                    "purpose": {"type": "string"},
                    "kind": {"type": "string", "enum": ["領収書", "レシート", "利用明細", "その他"]},
                    "tax_note": {"type": "string"},
                    "registration": {"type": "string"},
                },
                "required": ["date", "payee", "amount", "purpose", "kind", "tax_note", "registration"],
                "additionalProperties": False,
            },
        },
        "note": {"type": "string"},
    },
    "required": ["receipts", "note"],
    "additionalProperties": False,
}

REVISE_SYSTEM = """あなたは、支払通知書の承認依頼に付いた返信を読み取る担当です。
承認者の返信と、支払通知書の行の一覧（番号つき）が渡されます。返信が何を求めているかを決めてください。

- intent:
  「approve」= このまま承認する（直しの指示が無い）。
  「revise」= 直しの指示がある（承認の語が一緒に書かれていても、直しがあれば revise）。
  「other」= 質問や連絡など、承認でも直しでもない。
- ops: 直しの内容。revise のときだけ。番号は一覧の番号（①なら 1）。
  set_fee_amount（定例の業務委託費の単価を amount 円に）／set_fee_label（定例の行の内容を label に）／
  exclude_receipt（number の証憑を載せない）／include_receipt（number の証憑を載せる）／
  set_receipt_amount（number の証憑の金額を amount 円に）／set_receipt_date（number の証憑の日付を date に）／
  set_receipt_label（number の証憑の内容を label に）／
  add_line（証憑の無い行を足す。date・label・amount、課税対象外なら exempt=true）／remove_line（number の、手で足した行を消す）／
  set_issue_date（発行日を date に）
- 使わない項目は、number と amount は 0、date と label は空、exempt は false にします。
- 金額は整数の円（「5万円」は 50000）。日付は YYYY-MM-DD（年が無ければ一覧の日付と同じ年）。
- 読み取れない指示・一覧に無い番号は ops に入れず、question に何が分からないかを書きます。"""

REVISE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["approve", "revise", "other"]},
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": [
                        "set_fee_amount", "set_fee_label", "exclude_receipt", "include_receipt",
                        "set_receipt_amount", "set_receipt_date", "set_receipt_label", "add_line",
                        "remove_line", "set_issue_date"]},
                    "number": {"type": "integer"},
                    "amount": {"type": "integer"},
                    "date": {"type": "string"},
                    "label": {"type": "string"},
                    "exempt": {"type": "boolean"},
                },
                "required": ["op", "number", "amount", "date", "label", "exempt"],
                "additionalProperties": False,
            },
        },
        "question": {"type": "string"},
    },
    "required": ["intent", "ops", "question"],
    "additionalProperties": False,
}

_APPROVE_ONLY_RE = re.compile(r"^(?:承認(?:します|いたします|致します|です|しました)?|OK|ＯＫ|承認OK)[。．！!\s]*$", re.I)
_NEGATIVE_RE = re.compile(r"(承認しない|承認でき|承認不可|未承認|否認|保留|待って|まだ)")
_THANKS_RE = re.compile(r"(ありがとう|了解|承知|よろしく|お願いします|助かります)")


def is_plain_approval(text: str) -> bool:
    """「承認」「承認します」だけの返信か（直しの指示が混ざっていない）。"""
    plain = unicodedata.normalize("NFKC", strip_tags(text)).strip()
    return bool(_APPROVE_ONLY_RE.match(plain)) and not _NEGATIVE_RE.search(plain)


def attachments_of(body: str) -> list[tuple[int, str]]:
    """本文の添付 → [(file_id, ファイル名)]。"""
    found = []
    for file_id, name in _FILE_RE.findall(str(body or "")):
        found.append((int(file_id), _SIZE_TAIL_RE.sub("", name.strip())))
    return found


@dataclass
class Route:
    """どこで承認を取り、どこへ交付するか（本番とリハーサルで変わる）。"""

    name: str
    approval_room: int
    approvers: list[dict]
    state_file: str
    tab_prefix: str = ""
    banner: str = ""
    delivery: dict | None = None  # リハーサルでは全契約の交付先をここへ差し替える
    intake: bool = True
    extra_alert_room: int = 0

    def approver_ids(self) -> list[int]:
        return [int(a["account_id"]) for a in self.approvers if a.get("account_id")]


class PaymentNoticeRunner:
    """支払通知書の月次発行（巡回のタイマーに相乗り）。"""

    def __init__(
        self,
        config: Any,
        chatwork: Any,
        client: Any | None = None,
        cost: Any | None = None,
        now: Callable[[], datetime] | None = None,
        sheets: Callable[[str], Any] | None = None,
        download: Callable[[str], tuple[int, bytes]] | None = None,
        calendar: Calendar | None = None,
    ) -> None:
        self._config = config
        self._chatwork = chatwork
        self._client = client
        self._cost = cost
        self._now = now or (lambda: datetime.now(JST))
        self._sheets = sheets or self._default_sheets
        self._download = download or _http_download
        if calendar is None:
            from .letterpack import load_holidays

            holidays, _ = load_holidays(config)
            calendar = Calendar(holidays, self._settings().get("closed_days") or [])
        self._calendar = calendar
        self._root = config.resolve_path(config.state_dir)
        self._posts = 0
        self._owned: list[str] = []   # 承認ルームでの自分の投稿ID（状態の owned と同じリスト）
        self._owned_room = 0

    # --- 公開API ---

    def run_once(self) -> int:
        """本番（live のとき）とリハーサルの両方を進める。→ 投稿した数。"""
        settings = self._settings()
        if not settings.get("enabled"):
            return 0
        posted = 0
        # 5分ごとの巡回と、承認の返信を合図にした即時処理（webhook側）の両方から呼ばれる。
        # 同時に走ると二重に作り直し・交付するため、状態ファイルの隣のロックで直列にする
        with self._locked():
            try:
                posted += self._reading_tests()
            except Exception:
                print("[error] 証憑の読み取りテストに失敗: " + traceback.format_exc(), flush=True)
            for route in self.routes():
                try:
                    posted += self._run_route(route)
                except Exception:
                    print(f"[error] 支払通知書（{route.name}）の処理に失敗: " + traceback.format_exc(), flush=True)
        return posted

    def owns_reply(self, room_id: int, target_id: str) -> bool:
        """その返信先は、支払通知書の承認ルームでの自分の投稿か（承認依頼・PDF・催促・直した旨の返事）。

        webhook 側が、これへの返信を通常の質問応答へ回さないために使う。実例（2026-11-20 19:32・19:36）:
        承認依頼への「②を外して」「①を15万円に変更して」に、質問応答が「承知しました。作り直しますね」と
        返した（こちらの処理とは無関係の返事で、後者は実際には誰も作り直していなかった）。
        """
        if not target_id or not self._settings().get("enabled"):
            return False
        for route in self.routes():
            if int(route.approval_room or 0) != int(room_id):
                continue
            state = self._load(route)
            if str(target_id) in set(state.get("owned") or []):
                return True
            for cstate in (state.get("contracts") or {}).values():
                for notice in (cstate.get("notices") or {}).values():
                    if str(target_id) in set(notice.get("request_ids") or []) | set(notice.get("old_request_ids") or []):
                        return True
        return False

    def owns_room(self, room_id: int) -> bool:
        """委託先の専用ルームか（webhook 側が、メンションを合図にその場で受付を回すために使う）。"""
        if not self._settings().get("enabled"):
            return False
        return any(int(c.get("room_id", 0) or 0) == int(room_id) for c in self.contracts())

    def _locked(self):
        """状態ファイルのロック（Linuxの flock。無い環境では何もしない）。"""
        import contextlib

        try:
            import fcntl
        except ImportError:  # Windows（テスト環境）
            return contextlib.nullcontext()

        @contextlib.contextmanager
        def lock():
            path = self._root / "paynotice.lock"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

        return lock()

    def routes(self) -> list[Route]:
        settings = self._settings()
        routes = []
        if settings.get("live"):
            routes.append(self.route(LIVE))
        routes.append(self.route(REHEARSAL))
        return routes

    def route(self, name: str) -> Route:
        settings = self._settings()
        admin_room = int(self._config.admin_room_id or 0)
        if name == LIVE:
            return Route(
                name=LIVE,
                approval_room=int(settings.get("approval_room_id", 0) or 0),
                approvers=list(settings.get("approvers") or []),
                state_file="paynotice.json",
                extra_alert_room=admin_room,
            )
        admins = [int(a) for a in (self._config.admin_account_ids or [])]
        return Route(
            name=REHEARSAL,
            approval_room=admin_room,
            approvers=[{"account_id": a, "name": ""} for a in admins],
            state_file="paynotice-rehearsal.json",
            tab_prefix="テスト_",
            banner="【リハーサル】",
            delivery={"room_id": admin_room, "account_id": admins[0] if admins else 0, "name": ""},
            intake=False,
        )

    def contracts(self) -> list[dict]:
        return [c for c in (self._settings().get("contracts") or []) if c.get("id")]

    def contract(self, contract_id: str | None = None) -> dict:
        contracts = self.contracts()
        for contract in contracts:
            if contract_id in (None, "", contract["id"]):
                return contract
        raise NoticeError(f"契約「{contract_id}」が設定にありません")

    def rehearse(self, period: str, receipt_files: list[Path], contract_id: str | None = None,
                 from_room: bool = False) -> dict:
        """管理者ルームでの通し稽古を始める（日付の条件は見ない）。

        証憑はファイルから読むか、`from_room` なら委託先のルームに上がっている添付（委託先本人とテスト役の分）を
        読む。後者は、読み取りテストで使った実物の領収書を支払通知書に載せて確かめるためのもの（指示 2026-11-20）。
        控えはリハーサル用で、本番の支払通知書には載らない。
        """
        route = self.route(REHEARSAL)
        contract = self.contract(contract_id)
        state = self._load(route)
        cstate = self._cstate(state, contract)
        _, closing = period_bounds(period)
        submitted = datetime(closing.year, closing.month, closing.day, 12, 0, tzinfo=JST)
        cstate["receipts"] = [r for r in cstate["receipts"] if r.get("period") != period]
        cstate["notices"].pop(period, None)
        for index, path in enumerate(receipt_files, 1):
            found = self.read_receipt(path.read_bytes(), path.name)
            self._add_receipts(contract, cstate, found, file_id=f"file{index}", message_id="", filename=path.name,
                               submitted=submitted, period=period)
        if from_room:
            room_id = int(contract["room_id"])
            people = {int(a) for a in [contract.get("account_id"), *(contract.get("submitter_account_ids") or []),
                                       *(contract.get("test_submitter_account_ids") or [])] if a}
            for message in self._chatwork.get_recent_messages(room_id, limit=100):
                if _account_of(message) not in people:
                    continue
                sent = datetime.fromtimestamp(int(message.get("send_time", 0)), JST)
                for file_id, name in attachments_of(str(message.get("body") or "")):
                    suffix = Path(name).suffix.lower()
                    if suffix not in IMAGE_TYPES and suffix != ".pdf":
                        continue
                    info = self._chatwork.get_file_info(room_id, file_id)
                    status, data = self._download(str(info.get("download_url", "")))
                    if status != 200 or not data:
                        raise NoticeError(f"{name} を取得できませんでした（HTTP {status}）")
                    found = self.read_receipt(data, name)
                    self._add_receipts(contract, cstate, found, file_id=str(file_id),
                                       message_id=str(message.get("message_id")), filename=name,
                                       submitted=sent, period=period)
        self._owned, self._owned_room = state.setdefault("owned", []), route.approval_room
        notice = self._create(route, contract, cstate, period, self._now())
        self._save(route, state)
        return notice

    def create(self, period: str, contract_id: str | None = None) -> dict:
        """本番の経路で、指定した月の支払通知書を作って承認を求める（日付の条件は見ない）。"""
        route = self.route(LIVE)
        contract = self.contract(contract_id)
        state = self._load(route)
        cstate = self._cstate(state, contract)
        if period in cstate["notices"]:
            raise NoticeError(f"{period_label(period)}分は既に作成済みです")
        self._owned, self._owned_room = state.setdefault("owned", []), route.approval_room
        notice = self._create(route, contract, cstate, period, self._now())
        self._save(route, state)
        return notice

    def cleanup_rehearsal(self) -> list[str]:
        """リハーサルで作ったテスト用のタブを消し、リハーサルの控えを白紙に戻す。→ 消したタブの名前"""
        route = self.route(REHEARSAL)
        state = self._load(route)
        removed = []
        for contract in self.contracts():
            cstate = self._cstate(state, contract)
            client = self._sheets(str(contract.get("spreadsheet_id") or ""))
            for title, sheet_id in list((cstate.get("tabs") or {}).items()):
                existing = client.find(title)
                if existing is not None and int(existing["id"]) == int(sheet_id) and title.startswith(route.tab_prefix):
                    client.delete_sheet(int(sheet_id))
                    removed.append(title)
            state["contracts"][str(contract["id"])] = {}
        self._save(route, state)
        return removed

    def status(self) -> dict:
        return {route.name: self._load(route) for route in (self.route(LIVE), self.route(REHEARSAL))}

    # --- 巡回の本体 ---

    def _run_route(self, route: Route) -> int:
        if not route.approval_room or not route.approvers:
            return 0
        state = self._load(route)
        now = self._now()
        before = json.dumps(state, sort_keys=True, ensure_ascii=False)
        self._posts = 0
        self._owned = state.setdefault("owned", [])
        self._owned_room = route.approval_room
        for contract in self.contracts():
            cstate = self._cstate(state, contract)
            if route.intake:
                self._intake(route, contract, cstate, now)
                self._save(route, state)
                self._maybe_create(route, contract, cstate, now)
                self._save(route, state)
        self._approvals(route, state, now)
        self._retry_delivery(route, state, now)
        self._remind(route, state, now)
        del self._owned[:-MAX_OWNED]
        if json.dumps(state, sort_keys=True, ensure_ascii=False) != before:
            self._save(route, state)
        return self._posts

    # --- 1. 証憑の受付 ---

    def _intake(self, route: Route, contract: dict, cstate: dict, now: datetime) -> None:
        room_id = int(contract.get("room_id", 0) or 0)
        submitters = {int(a) for a in (contract.get("submitter_account_ids") or [contract.get("account_id")]) if a}
        if not room_id or not submitters:
            return
        try:
            messages = self._chatwork.get_recent_messages(room_id, limit=100)
        except Exception:
            print("[warn] 委託先のルームの取得に失敗: " + traceback.format_exc(), flush=True)
            return
        me = self._me()
        last_seen = int(cstate.get("last_seen") or 0)
        for message in sorted(messages, key=lambda m: int(m.get("message_id", 0))):
            mid = int(message.get("message_id", 0))
            if mid <= last_seen:
                continue
            if _account_of(message) in submitters:
                try:
                    self._from_payee(route, contract, cstate, message, me)
                except ModelUnavailable:
                    retries = cstate.setdefault("retries", {})
                    retries[str(mid)] = int(retries.get(str(mid), 0)) + 1
                    if retries[str(mid)] < int(self._settings().get("max_read_attempts", 3)):
                        return  # 次の巡回でやり直す（順番は崩さない）
                    self._hand_over_unread(route, contract, message)
                except Exception:
                    print(f"[error] 証憑 {mid} の受付に失敗: " + traceback.format_exc(), flush=True)
                    self._hand_over_unread(route, contract, message)
            cstate.get("retries", {}).pop(str(mid), None)
            cstate["last_seen"] = mid

    def _from_payee(self, route: Route, contract: dict, cstate: dict, message: dict, me: int) -> None:
        room_id = int(contract["room_id"])
        body = str(message.get("body") or "")
        files = attachments_of(body)
        submitted = datetime.fromtimestamp(int(message.get("send_time", 0)), JST)
        if not files:
            self._relay(route, contract, cstate, message, me)
            return
        period = period_of(submitted.date())
        accepted: list[dict] = []
        problems: list[str] = []
        for file_id, name in files:
            suffix = Path(name).suffix.lower()
            if suffix not in IMAGE_TYPES and suffix != ".pdf":
                problems.append(f"「{name}」は画像・PDFではないため、読み取っていません。")
                continue
            info = self._chatwork.get_file_info(room_id, file_id)
            status, data = self._download(str(info.get("download_url", "")))
            if status != 200 or not data:
                raise RuntimeError(f"{name} を取得できませんでした（HTTP {status}）")
            found = self.read_receipt(data, name)
            if not found.get("receipts"):
                problems.append(f"「{name}」は、領収書として読み取れませんでした。写りのよい画像を送り直していただけますか。")
                continue
            accepted += self._add_receipts(contract, cstate, found, file_id=str(file_id),
                                           message_id=str(message.get("message_id")), filename=name,
                                           submitted=submitted, period=period)
        self._acknowledge(route, contract, message, period, accepted, problems, submitted.date())
        self._audit({"type": "paynotice_receipt", "route": route.name, "contract": contract["id"],
                     "message_id": str(message.get("message_id")), "period": period,
                     "accepted": len([r for r in accepted if r["status"] == OK]),
                     "review": len([r for r in accepted if r["status"] == REVIEW])})

    def _add_receipts(self, contract: dict, cstate: dict, found: dict, file_id: str, message_id: str,
                      filename: str, submitted: datetime, period: str) -> list[dict]:
        term_start = _parse_date((contract.get("term") or {}).get("start"))
        added = []
        for index, raw in enumerate(found.get("receipts") or [], 1):
            receipt = {
                "id": f"{file_id}-{index}",
                "file_id": file_id,
                "message_id": message_id,
                "filename": filename,
                "submitted_at": submitted.isoformat(),
                "period": period,
                "date": str(raw.get("date") or "").strip(),
                "payee": str(raw.get("payee") or "").strip(),
                "amount": raw.get("amount") if isinstance(raw.get("amount"), int) else 0,
                "purpose": str(raw.get("purpose") or "").strip(),
                "kind": str(raw.get("kind") or ""),
                "tax_note": str(raw.get("tax_note") or ""),
            }
            # 登録番号は、形（T＋13桁）が合うものだけ控える（合わない番号は読み違いの疑い）
            number, valid = clean_registration(raw.get("registration"))
            receipt["registration"] = number if valid else ""
            issues = check_receipt(receipt, submitted.date(), term_start, cstate["receipts"])
            receipt["issues"] = issues
            receipt["status"] = REVIEW if blocks_listing(issues) else OK
            cstate["receipts"].append(receipt)
            added.append(receipt)
        return added

    def _acknowledge(self, route: Route, contract: dict, message: dict, period: str, accepted: list[dict],
                     problems: list[str], submitted: date) -> None:
        lines = []
        listed = [r for r in accepted if r["status"] == OK]
        unread = [r for r in accepted if r["status"] == REVIEW]
        if listed:
            lines.append(f"領収書を受け付けました（{period_label(period)}分）。")
            for receipt in listed:
                lines.append(f"・{_short(receipt['date'])} {receipt['payee'] or '支払先不明'} "
                             f"{receipt['purpose'] or ''} {yen(receipt['amount'])}".replace("  ", " "))
            _, closing = period_bounds(period)
            issue_month = (closing + timedelta(days=1)).month
            lines.append(f"{issue_month}月初めにお渡しする支払通知書に載せます。"
                         "読み取りに誤りがあれば、この投稿への返信でお知らせください。")
        for receipt in unread:
            lines.append(f"・「{receipt['filename']}」は{'、'.join(i for i in receipt['issues'] if '読み取れません' in i or '金額が大きい' in i)}。"
                         "経理担当が確認します。")
        lines += problems
        term_end = _parse_date((contract.get("term") or {}).get("end"))
        if term_end and submitted > term_end:
            lines.append("契約期間の終了後のご提出のため、精算の可否を経理担当が確認します。")
        if lines:
            self._reply(int(contract["room_id"]), message, "\n".join(lines))

    def _relay(self, route: Route, contract: dict, cstate: dict, message: dict, me: int) -> None:
        """委託先からアシスタント宛の連絡は、答えずに承認者へ取り次ぐ（誤りの申出・質問など）。"""
        body = str(message.get("body") or "")
        if not me or not (f"[To:{me}]" in body or f"[rp aid={me} " in body):
            return  # アシスタント宛でない発言には関わらない
        text = strip_tags(strip_own_mention(body, me)).strip()
        if not text or (len(text) <= 20 and _THANKS_RE.search(text)):
            return  # お礼や了解だけの返事は取り次がない
        room_id = int(contract["room_id"])
        quoted = text[:MAX_RELAY_CHARS] + ("…" if len(text) > MAX_RELAY_CHARS else "")
        self._send(route.approval_room, self._heads(route) + "\n" + "\n".join([
            f"{route.banner}{contract.get('payee', '')}様から、次のご連絡がありました。ご対応をお願いします。",
            f"「{quoted}」",
            message_url(room_id, message.get("message_id")),
        ]))
        self._reply(room_id, message, "ご連絡ありがとうございます。経理担当へお伝えしました。担当から折り返しご連絡します。")
        self._audit({"type": "paynotice_relay", "route": route.name, "contract": contract["id"],
                     "message_id": str(message.get("message_id"))})

    def _hand_over_unread(self, route: Route, contract: dict, message: dict) -> None:
        room_id = int(contract["room_id"])
        self._reply(room_id, message, "お送りいただいたファイルを、こちらで読み取れませんでした。経理担当が確認します。")
        self._send(route.approval_room, self._heads(route) + "\n" + "\n".join([
            f"{route.banner}{contract.get('payee', '')}様から届いた証憑を読み取れませんでした。内容のご確認をお願いします。",
            message_url(room_id, message.get("message_id")),
        ]))

    # --- 読み取りテスト（本番の受付とは別） ---

    def _reading_tests(self) -> int:
        """委託先のルームで、テスト役（`test_submitter_account_ids`）が上げた証憑を読んで結果を返す。

        実物の領収書で読み取りを確かめるためのもの（指示 2026-11-20: 宮﨑さんではなく足立さんが写真を上げて試す）。
        **読むだけで、控えには残さない**（支払通知書には載らない）。本番（live）かどうかに関係なく動く。
        テストが済んだら設定からテスト役を外す（外せば、その人の添付には関わらない）。
        """
        self._owned_room = 0
        before = self._posts
        path = self._root / "paynotice-test.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        changed = False
        for contract in self.contracts():
            testers = {int(a) for a in contract.get("test_submitter_account_ids") or [] if a}
            room_id = int(contract.get("room_id", 0) or 0)
            if not testers or not room_id:
                continue
            try:
                messages = self._chatwork.get_recent_messages(room_id, limit=100)
            except Exception:
                print("[warn] 委託先のルームの取得に失敗: " + traceback.format_exc(), flush=True)
                continue
            cstate = state.setdefault(str(contract["id"]), {"last_seen": 0, "retries": {}})
            for message in sorted(messages, key=lambda m: int(m.get("message_id", 0))):
                mid = int(message.get("message_id", 0))
                if mid <= int(cstate.get("last_seen") or 0):
                    continue
                if _account_of(message) in testers and attachments_of(str(message.get("body") or "")):
                    try:
                        self._test_read(contract, message)
                    except ModelUnavailable:
                        retries = cstate.setdefault("retries", {})
                        retries[str(mid)] = int(retries.get(str(mid), 0)) + 1
                        changed = True
                        if retries[str(mid)] < int(self._settings().get("max_read_attempts", 3)):
                            break  # 次の巡回でやり直す
                        self._reply(room_id, message, "【読み取りテスト】ファイルを読み取れませんでした（読み取りの処理が失敗しました）。")
                    except Exception as exc:
                        print(f"[error] 読み取りテスト {mid} に失敗: " + traceback.format_exc(), flush=True)
                        self._reply(room_id, message, f"【読み取りテスト】ファイルを読み取れませんでした（{exc}）。")
                cstate.get("retries", {}).pop(str(mid), None)
                cstate["last_seen"] = mid
                changed = True
        if changed:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            except OSError:
                print("[warn] 読み取りテストの状態を保存できませんでした", flush=True)
        return self._posts - before

    def _test_read(self, contract: dict, message: dict) -> None:
        room_id = int(contract["room_id"])
        submitted = datetime.fromtimestamp(int(message.get("send_time", 0)), JST)
        term_start = _parse_date((contract.get("term") or {}).get("start"))
        lines = ["【読み取りテスト】次のように読み取りました。テストのため控えには残さず、支払通知書にも載せません。"]
        count = 0
        seen: list[dict] = []
        for file_id, name in attachments_of(str(message.get("body") or "")):
            suffix = Path(name).suffix.lower()
            if suffix not in IMAGE_TYPES and suffix != ".pdf":
                lines.append(f"■ {name}\n　画像（JPEG・PNG・GIF・WebP）とPDF以外は読み取れません。")
                continue
            info = self._chatwork.get_file_info(room_id, file_id)
            status, data = self._download(str(info.get("download_url", "")))
            if status != 200 or not data:
                raise RuntimeError(f"{name} を取得できませんでした（HTTP {status}）")
            found = self.read_receipt(data, name)
            receipts = found.get("receipts") or []
            lines.append(f"■ {name}" + (f"（{len(receipts)}件）" if len(receipts) > 1 else ""))
            if not receipts:
                note = str(found.get("note") or "").strip()
                lines.append("　領収書として読み取れませんでした。" + (f"（{note}）" if note else ""))
                continue
            for raw in receipts:
                receipt = {
                    "date": str(raw.get("date") or "").strip(),
                    "payee": str(raw.get("payee") or "").strip(),
                    "amount": raw.get("amount") if isinstance(raw.get("amount"), int) else 0,
                    "purpose": str(raw.get("purpose") or "").strip(),
                    "status": OK,
                }
                issues = check_receipt(receipt, submitted.date(), term_start, seen)
                seen.append(receipt)
                count += 1
                day = _parse_date(receipt["date"])
                lines.append(
                    f"　日付: {jp_date(day) if day else '読み取れず'}／支払先: {receipt['payee'] or '読み取れず'}／"
                    f"金額: {yen(receipt['amount']) if receipt['amount'] else '読み取れず'}"
                )
                number, valid = clean_registration(raw.get("registration"))
                shown = "記載なし" if not number else (number if valid else f"{number}（桁数が合わず、読み違いの疑いがあります）")
                lines.append(f"　用途: {receipt['purpose'] or '読み取れず'}／種類: {raw.get('kind') or '-'}／"
                             f"税率の記載: {raw.get('tax_note') or '-'}／登録番号: {shown}")
                lines.append(f"　支払通知書の内容欄: {receipt_label(receipt)}")
                for issue in issues:
                    lines.append(f"　【要確認】{issue}")
        lines.append("読み違いがあれば、どの項目が違うかを教えてください。")
        self._reply(room_id, message, "\n".join(lines))
        self._audit({"type": "paynotice_test_read", "contract": contract["id"],
                     "message_id": str(message.get("message_id")), "receipts": count})

    # --- 2. 作成 ---

    def _maybe_create(self, route: Route, contract: dict, cstate: dict, now: datetime) -> None:
        today = now.date()
        if not self._calendar.is_business_day(today):
            return
        if now < at_clock(today, parse_clock(self._settings().get("create_time"), (9, 0))):
            return
        period = previous_period(today)
        if period in cstate["notices"]:
            return
        first = str(contract.get("first_period") or "")
        if first and period < first:
            return
        start, closing = period_bounds(period)
        term = contract.get("term") or {}
        term_start, term_end = _parse_date(term.get("start")), _parse_date(term.get("end"))
        if (term_start and closing < term_start) or (term_end and start > term_end):
            return
        try:
            self._create(route, contract, cstate, period, now)
        except Exception as exc:
            # 作れなかったら、その日は一度だけ知らせる（毎巡回で鳴らさない）
            marks = cstate.setdefault("create_errors", {})
            if marks.get(period) != today.isoformat():
                marks[period] = today.isoformat()
                print("[error] 支払通知書の作成に失敗: " + traceback.format_exc(), flush=True)
                self._alert(route, f"{contract.get('payee', '')}様の{period_label(period)}分の支払通知書を作成できませんでした。"
                                   f"\n理由: {exc}\n次の巡回でもう一度試します。")

    def _create(self, route: Route, contract: dict, cstate: dict, period: str, now: datetime,
                previous: dict | None = None) -> dict:
        """支払通知書を作り（作り直し）、承認を求める。"""
        _, closing = period_bounds(period)
        overrides = dict((previous or {}).get("overrides") or {})
        issue_date = _parse_date(overrides.get("issue_date")) or now.date()
        lines = build_lines(contract, period, cstate["receipts"], overrides)
        total = total_of(lines)
        version = int((previous or {}).get("version", 0)) + 1
        notice = {
            "period": period,
            "status": AWAITING,
            "version": version,
            "issue_date": issue_date.isoformat(),
            "deadline": self._calendar.nth_after(closing, int(self._settings().get("issue_within_business_days", 5))).isoformat(),
            "payment_date": self._payment_date(contract, closing).isoformat(),
            "lines": lines,
            "total": total,
            "overrides": overrides,
            "created_at": (previous or {}).get("created_at") or now.isoformat(),
            "built_at": now.isoformat(),
            "request_ids": [],
            "old_request_ids": list((previous or {}).get("old_request_ids") or []) + list((previous or {}).get("request_ids") or []),
            "reminded": dict((previous or {}).get("reminded") or {}),
            # 行の番号は一度振ったら変えない（外した行も、その番号のまま「載せていない証憑」に残す）
            "numbers": self._numbers_of(previous),
        }
        # 作れなかったときは控えを前の版のままにする（中途半端な版を承認待ちにしない）
        pdf = self._render(route, contract, cstate, notice)
        path = self._pdf_path(route, contract, period, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pdf)
        notice["pdf"] = str(path)
        notice["pdf_sha256"] = hashlib.sha256(pdf).hexdigest()
        notice["filename"] = self._filename(contract, period, issue_date)
        cstate["notices"][period] = notice
        self._request_approval(route, contract, cstate, notice, pdf)
        self._audit({"type": "paynotice_request", "route": route.name, "contract": contract["id"], "period": period,
                     "version": version, "lines": len(lines), "sha256": notice["pdf_sha256"][:16]})
        return notice

    def _render(self, route: Route, contract: dict, cstate: dict, notice: dict) -> bytes:
        """ひな形のタブを複製して値を書き、合計を突き合わせてPDFにする。

        消してよいのは、自分で作ったと控えてあるタブ（`tabs`）だけ。同じ名前のタブを人が作っていたら止める。
        """
        layout = {"issue_date": "Q4", "total": "B15", "first_row": 20, "last_row": 34, "last_column": "R",
                  "date": "B", "label": "D", "exempt": "K", "quantity": "L", "unit": "M", "price": "N",
                  "exempt_mark": "※", **(contract.get("layout") or {})}
        client = self._sheets(str(contract.get("spreadsheet_id") or ""))
        year, month = (int(part) for part in notice["period"].split("-"))
        title = route.tab_prefix + str(contract.get("sheet_title") or "{year}年{month}月").format(year=year, month=month)
        tabs = cstate.setdefault("tabs", {})
        existing = client.find(title)
        if existing is not None:
            if tabs.get(title) is None or int(existing["id"]) != int(tabs[title]):
                raise NoticeError(f"スプレッドシートに「{title}」というタブが既にあります（手で作られたタブは上書きしません）")
            client.delete_sheet(int(existing["id"]))  # 自分で作った前の版
            tabs.pop(title, None)
        sheet_id = client.duplicate(str(contract.get("template_sheet") or ""), title)
        tabs[title] = sheet_id
        notice["sheet_id"], notice["sheet_title"] = sheet_id, title
        lines = notice["lines"]
        first, last = int(layout["first_row"]), int(layout["last_row"])
        for _ in range(max(0, len(lines) - (last - first + 1))):
            client.add_row_like(sheet_id, last, str(layout["last_column"]))
        # ひな形は2行目（21行目）までしか書式が整っていない（3行目から文字が小さく、配置も違う。
        # 2026-11-20 のリハーサルで確認）。証憑の行は2行目の書式にそろえる
        format_row = int(layout.get("format_row") or first + 1)
        client.copy_row_format(sheet_id, format_row, format_row + 1, first + len(lines) - 1, str(layout["last_column"]))
        values: dict[str, Any] = {str(layout["issue_date"]): date.fromisoformat(notice["issue_date"])}
        for offset, line in enumerate(lines):
            row = first + offset
            values[f"{layout['date']}{row}"] = date.fromisoformat(line["date"])
            values[f"{layout['label']}{row}"] = line["label"]
            values[f"{layout['exempt']}{row}"] = layout["exempt_mark"] if line.get("exempt") else None
            values[f"{layout['quantity']}{row}"] = line["quantity"]
            values[f"{layout['unit']}{row}"] = line["unit"]
            values[f"{layout['price']}{row}"] = int(line["price"])
        client.write(title, values)
        shown = client.read_cell(title, str(layout["total"]))
        try:
            shown_total = int(round(float(shown)))
        except (TypeError, ValueError):
            shown_total = None
        if shown_total != int(notice["total"]):
            raise NoticeError(f"スプレッドシートの合計（{shown}）が計算した合計（{notice['total']:,}）と合いません")
        return client.export_pdf(sheet_id)

    def _payment_date(self, contract: dict, closing: date) -> date:
        """締日の翌月の支払日（休業日なら前営業日。契約6条3項）。"""
        next_month = closing + timedelta(days=1)
        return self._calendar.on_or_before(next_month.replace(day=int(contract.get("payment_day", 15))))

    def _filename(self, contract: dict, period: str, issue_date: date) -> str:
        """PDFの名前。先頭に発行日を「月.日」（半角・ゼロ埋めなし）で付ける（指示 2026-11-20。
        作成手順の「〇.10-宮﨑大樹様-…」の「〇.10」は発行日）。発行日を直せば名前も変わる。"""
        year, month = (int(part) for part in period.split("-"))
        pattern = str(contract.get("file_name") or "{issue_month}.{issue_day}-支払通知書{year}年{month}月分.pdf")
        return pattern.format(year=year, month=month, issue_month=issue_date.month, issue_day=issue_date.day)

    def _pdf_path(self, route: Route, contract: dict, period: str, version: int) -> Path:
        return self._root / "paynotice" / route.name / str(contract["id"]) / f"{period}-v{version}.pdf"

    # --- 3. 承認 ---

    def _request_approval(self, route: Route, contract: dict, cstate: dict, notice: dict, pdf: bytes) -> None:
        text = self._heads(route) + "\n" + "\n".join(self._request_lines(route, contract, cstate, notice))
        mid = self._send(route.approval_room, text)
        ids = [mid] if mid else []
        try:
            file_id = self._chatwork.upload_file(route.approval_room, notice["filename"], pdf,
                                                 message=f"{route.banner}支払通知書（確認用・第{notice['version']}版）です。")
            self._posts += 1
            # 添付の投稿IDは、APIがファイルIDしか返さないので後から拾う（`_link_uploads`。
            # 投稿の直後は一覧にまだ出ないことがある。2026-11-20 のリハーサルで拾えなかった）
            notice["request_file_ids"] = [str(file_id)]
        except Exception:
            print("[warn] 支払通知書の添付に失敗: " + traceback.format_exc(), flush=True)
            self._send(route.approval_room, "PDFを添付できませんでした。スプレッドシートの"
                                            f"「{notice.get('sheet_title', '')}」タブでご確認ください。")
        notice["request_ids"] = [i for i in ids if i]

    def _request_lines(self, route: Route, contract: dict, cstate: dict, notice: dict) -> list[str]:
        period = notice["period"]
        payee = contract.get("payee", "")
        deadline, payment = date.fromisoformat(notice["deadline"]), date.fromisoformat(notice["payment_date"])
        lines = [
            f"{route.banner}【承認のお願い】{payee}様 {period_label(period)}分 支払通知書（第{notice['version']}版）",
            f"発行日: {jp_date(date.fromisoformat(notice['issue_date']))}／交付期限: {jp_date(deadline)}／"
            f"支払予定日: {jp_date(payment)}",
            f"合計: {yen(notice['total'])}",
        ]
        room_id = int(contract.get("room_id", 0) or 0)
        # 番号は一度振ったら、その行が外されても版が替わっても変えない。
        # 実例（2026-11-20 のリハーサル）: 第1版の「②近鉄・③駐車料金」から②を外すと、第2版で駐車料金が②、
        # 外した近鉄が③に振り直され、どの行の話か追いにくくなった
        numbers = notice.setdefault("numbers", {})

        def number_of(key: str) -> int:
            if key not in numbers:
                numbers[key] = max(numbers.values(), default=0) + 1
            return int(numbers[key])

        display, shown, kept_out = [], [], []
        for line in notice["lines"]:
            number = number_of(line_key(line))
            display.append({"number": number, "kind": line["kind"], "receipt_id": line.get("receipt_id"),
                            "uid": line.get("uid"), "label": line["label"]})
            amount = yen(int(line["quantity"]) * int(line["price"]))
            mark = " ※課税対象外" if line.get("exempt") else ""
            text = f"{circled(number)} {_short(line['date'])} {line['label']} {amount}{mark}"
            receipt = self._receipt(cstate, line.get("receipt_id"))
            if receipt:
                text += self._receipt_link(room_id, receipt)
                for issue in receipt.get("issues") or []:
                    text += f"\n　【要確認】{issue}"
            shown.append((number, text))
        listed = {line.get("receipt_id") for line in notice["lines"]}
        for receipt in [r for r in cstate["receipts"] if r.get("period") == period and r["id"] not in listed]:
            number = number_of(f"r:{receipt['id']}")
            display.append({"number": number, "kind": "receipt", "receipt_id": receipt["id"], "uid": None,
                            "label": receipt.get("label") or receipt_label(receipt)})
            why = "外す指示あり" if receipt["status"] == EXCLUDED else "、".join(receipt.get("issues") or [])
            # 載っている行と同じ書き方にする（日付・内容・金額）。実例（2026-11-20 のリハーサル）: 外した行だけ
            # 「train.jpg 10/20 近畿日本鉄道株式会社 680円」と、先頭にファイル名・支払先は正式名という別の書き方で、
            # 同じ行に見えなかった。読めなかった項目は「日付不明」「金額不明」と書く
            amount = yen(receipt["amount"]) if receipt.get("amount") else "金額不明"
            label = receipt.get("label") or receipt_label(receipt)
            link = self._receipt_link(room_id, receipt)
            if receipt.get("message_id") and receipt.get("filename") and blocks_listing(receipt.get("issues") or []):
                link += f"（{receipt['filename']}）"  # 読めなかった証憑は、1通に複数あってもどの画像か分かるように
            kept_out.append((number, f"{circled(number)} {_short(receipt.get('date'))} {label} {amount}（{why}）{link}"))
        lines += [text for _, text in sorted(shown)]
        if kept_out:
            lines.append("載せていない証憑（番号はそのままです）:")
            lines += [text for _, text in sorted(kept_out)]
        notice["display"] = sorted(display, key=lambda entry: entry["number"])
        term_end = _parse_date((contract.get("term") or {}).get("end"))
        start, closing = period_bounds(period)
        if term_end and start <= term_end < closing:
            lines.append(f"※契約期間が{jp_date(term_end)}で終わります。業務委託費の日割り（契約5条2項）の要否をご確認ください。")
        if any(line["kind"] == "receipt" for line in notice["lines"]):
            lines.append("経費は、事前に承認されたものかどうかも合わせてご確認ください。")
        target = f"{payee}様のルーム" if route.name == LIVE else "このルーム（リハーサルのため）"
        lines.append(f"この投稿への返信で「承認」といただければ、{target}へPDFを交付します（どなたか1名で結構です）。")
        lines.append("直すときは、この投稿への返信で「②を外して」「業務委託費を50,000円に」のようにお知らせください。"
                     "作り直して、もう一度お見せします。")
        return lines

    @staticmethod
    def _numbers_of(previous: dict | None) -> dict:
        """前の版で振った番号（行の鍵 → 番号）。番号の控えが無い古い版は、見せた並びから起こす。"""
        if not previous:
            return {}
        if previous.get("numbers"):
            return dict(previous["numbers"])
        numbers = {}
        for index, entry in enumerate(previous.get("display") or [], 1):
            numbers[line_key(entry)] = int(entry.get("number") or index)
        return numbers

    @staticmethod
    def _receipt(cstate: dict, receipt_id: Any) -> dict | None:
        return next((r for r in cstate["receipts"] if r["id"] == receipt_id), None) if receipt_id else None

    @staticmethod
    def _receipt_link(room_id: int, receipt: dict) -> str:
        if room_id and receipt.get("message_id"):
            return "　証憑: " + message_url(room_id, receipt["message_id"])
        return f"　証憑: {receipt.get('filename', '')}"

    def _approvals(self, route: Route, state: dict, now: datetime) -> None:
        """承認ルームの返信を読む（承認・直しの指示）。"""
        def waiting() -> list[tuple[dict, dict, dict]]:
            # 毎回引き直す（直しの指示で版が替わると、控えの中身も入れ替わるため）
            return [(c, cs, n) for c in self.contracts() for cs in [self._cstate(state, c)]
                    for n in cs["notices"].values() if n.get("status") == AWAITING and not n.get("approved_by")]

        try:
            messages = self._chatwork.get_recent_messages(route.approval_room, limit=100)
        except Exception:
            print("[warn] 承認ルームの取得に失敗: " + traceback.format_exc(), flush=True)
            return
        newest = max((int(m.get("message_id", 0)) for m in messages), default=0)
        self._link_uploads(waiting(), messages)
        last_seen = state.get("approval_last_seen")
        if not waiting():
            state["approval_last_seen"] = max(newest, int(last_seen or 0))
            return
        if last_seen is None:
            # 初めての承認待ち: 承認依頼の投稿より後を読む（依頼の直後に付いた返信を取りこぼさない）
            ids = [int(i) for _, _, n in waiting() for i in n.get("request_ids") or [] if str(i).isdigit()]
            last_seen = (min(ids) - 1) if ids else newest
        approvers = set(route.approver_ids())
        me = self._me()
        for message in sorted(messages, key=lambda m: int(m.get("message_id", 0))):
            mid = int(message.get("message_id", 0))
            if mid <= int(last_seen):
                continue
            state["approval_last_seen"] = mid
            if _account_of(message) not in approvers:
                continue
            targets = reply_targets(str(message.get("body") or ""), route.approval_room)
            if not targets:
                continue
            for contract, cstate, notice in waiting():
                if targets & set(notice.get("request_ids") or []):
                    self._on_reply(route, contract, cstate, notice, message, me, now)
                    break
                if targets & set(notice.get("old_request_ids") or []):
                    self._reply(route.approval_room, message,
                                f"その版は作り直しています。最新は第{notice['version']}版ですので、そちらの投稿への返信でお願いします。")
                    break
            else:
                # 支払通知書についての自分の投稿（交付の知らせ・古い版の案内など）への返信で、
                # どの版の話か決められないもの。黙らずに、返信先を案内する
                if targets & set(state.get("owned") or []):
                    current = waiting()
                    if current:
                        _, _, notice = current[0]
                        link = message_url(route.approval_room, notice["request_ids"][0]) if notice.get("request_ids") else ""
                        self._reply(route.approval_room, message,
                                    f"承認や直しのご指示は、第{notice['version']}版の承認依頼の投稿への返信でお願いします。\n{link}")

    def _on_reply(self, route: Route, contract: dict, cstate: dict, notice: dict, message: dict, me: int,
                  now: datetime) -> None:
        body = strip_own_mention(str(message.get("body") or ""), me)
        text = strip_tags(body).strip()
        def answer(reply_text: str, to: dict | None = None) -> None:
            # この返事への返信も、同じ版への返信として読めるように結び付ける
            mid = self._reply(route.approval_room, message, reply_text)
            if mid:
                (to or notice).setdefault("request_ids", []).append(mid)

        if is_plain_approval(text):
            self._approve(route, contract, cstate, notice, message, now)
            return
        try:
            found = self._read_reply(text, notice)
        except ModelUnavailable:
            answer("ご返信を読み取れませんでした。承認の場合は「承認」とだけ、もう一度ご返信ください。")
            return
        if found.get("intent") == "approve" and not found.get("ops"):
            self._approve(route, contract, cstate, notice, message, now)
            return
        if found.get("intent") != "revise" or not found.get("ops"):
            hint = f"（{found['question']}）" if found.get("question") else ""
            answer(f"承認か直しのご指示かを読み取れませんでした{hint}。承認の場合は「承認」、"
                   "直す場合は「②を外して」「業務委託費を50,000円に」のようにお知らせください。")
            return
        try:
            done = self._apply(cstate, notice, found["ops"])
        except NoticeError as exc:
            answer(f"ご指示を反映できませんでした（{exc}）。もう一度お知らせください。")
            return
        try:
            rebuilt = self._create(route, contract, cstate, notice["period"], now, previous=notice)
        except Exception as exc:
            print("[error] 支払通知書の作り直しに失敗: " + traceback.format_exc(), flush=True)
            answer(f"作り直しに失敗しました（{exc}）。")
            return
        # 実例（2026-11-20 19:35）: この「直しました」への返信で次の直し（「①を15万円に変更して」）が来た。
        # 承認依頼そのものへの返信でなかったため読めず、誰も作り直さなかった
        answer("直しました（" + "、".join(done) + f"）。第{rebuilt['version']}版を投稿しています。"
               "ご確認のうえ「承認」とご返信ください（続けて直すときも、返信でお知らせください）。", to=rebuilt)
        self._audit({"type": "paynotice_revised", "route": route.name, "contract": contract["id"],
                     "period": notice["period"], "version": rebuilt["version"], "by": _account_of(message)})

    def _apply(self, cstate: dict, notice: dict, ops: list[dict]) -> list[str]:
        """直しの指示を控えに反映する。→ 何を直したかの一言の一覧。おかしな指示は NoticeError。"""
        overrides = notice.setdefault("overrides", {})
        display = notice.get("display") or []
        done = []

        def target(number: int, kind: str) -> dict:
            entry = next((e for i, e in enumerate(display, 1) if int(e.get("number") or i) == number), None)
            if entry is None or entry["kind"] != kind:
                raise NoticeError(f"{circled(number) if number else '番号'}に当たる行が見当たりません")
            return entry

        def amount_of(op: dict) -> int:
            amount = op.get("amount")
            if not isinstance(amount, int) or not 0 < amount <= MAX_AMOUNT:
                raise NoticeError("金額を読み取れませんでした")
            return amount

        def date_of(op: dict) -> str:
            day = _parse_date(op.get("date"))
            if day is None:
                raise NoticeError("日付を読み取れませんでした")
            return day.isoformat()

        for op in ops:
            name, number = op.get("op"), int(op.get("number") or 0)
            if name == "set_fee_amount":
                overrides["fee_amount"] = amount_of(op)
                done.append(f"業務委託費を{yen(overrides['fee_amount'])}に")
            elif name == "set_fee_label":
                if not str(op.get("label") or "").strip():
                    raise NoticeError("内容を読み取れませんでした")
                overrides["fee_label"] = str(op["label"]).strip()
                done.append("業務委託費の行の内容を変更")
            elif name == "set_issue_date":
                overrides["issue_date"] = date_of(op)
                done.append(f"発行日を{overrides['issue_date']}に")
            elif name == "add_line":
                label = str(op.get("label") or "").strip()
                if not label:
                    raise NoticeError("足す行の内容を読み取れませんでした")
                overrides["added_seq"] = int(overrides.get("added_seq", 0)) + 1
                overrides.setdefault("added", []).append(
                    {"uid": f"a{overrides['added_seq']}", "date": date_of(op), "label": label,
                     "price": amount_of(op), "exempt": bool(op.get("exempt"))})
                done.append(f"「{label}」{yen(op['amount'])}の行を追加")
            elif name == "remove_line":
                entry = target(number, "added")
                added = overrides.get("added") or []
                match = next((a for a in added if (a.get("uid") and a.get("uid") == entry.get("uid"))
                              or (not entry.get("uid") and a["label"] == entry["label"])), None)
                if match is None:
                    raise NoticeError(f"{circled(number)}は手で足した行ではありません")
                added.remove(match)
                done.append(f"{circled(number)}の行を削除")
            elif name in ("exclude_receipt", "include_receipt", "set_receipt_amount", "set_receipt_date",
                          "set_receipt_label"):
                receipt = self._receipt(cstate, target(number, "receipt").get("receipt_id"))
                if receipt is None:
                    raise NoticeError(f"{circled(number)}は証憑の行ではありません")
                if name == "exclude_receipt":
                    receipt["status"] = EXCLUDED
                    done.append(f"{circled(number)}を外す")
                    continue
                if name == "set_receipt_amount":
                    receipt["amount"] = amount_of(op)
                    done.append(f"{circled(number)}の金額を{yen(receipt['amount'])}に")
                elif name == "set_receipt_date":
                    receipt["date"] = date_of(op)
                    done.append(f"{circled(number)}の日付を{receipt['date']}に")
                elif name == "set_receipt_label":
                    if not str(op.get("label") or "").strip():
                        raise NoticeError("内容を読み取れませんでした")
                    receipt["label"] = str(op["label"]).strip()
                    done.append(f"{circled(number)}の内容を変更")
                else:
                    done.append(f"{circled(number)}を載せる")
                # 人が直した項目は点検をやり直す（金額と日付がそろえば載せる）
                receipt["issues"] = [i for i in receipt.get("issues") or []
                                     if "読み取れません" not in i and "金額が大きい" not in i]
                if not receipt.get("amount"):
                    receipt["issues"].append("金額を読み取れませんでした")
                if _parse_date(receipt.get("date")) is None:
                    receipt["issues"].append("日付を読み取れませんでした")
                if blocks_listing(receipt["issues"]):
                    if name == "include_receipt":
                        raise NoticeError(f"{circled(number)}は金額か日付が分からないため載せられません。金額・日付をお知らせください")
                    receipt["status"] = REVIEW
                else:
                    receipt["status"] = OK
            else:
                raise NoticeError("ご指示を読み取れませんでした")
        if not done:
            raise NoticeError("直す内容を読み取れませんでした")
        return done

    def _approve(self, route: Route, contract: dict, cstate: dict, notice: dict, message: dict, now: datetime) -> None:
        notice["approved_by"] = _account_of(message)
        notice["approved_at"] = now.isoformat()
        notice["approval_message_id"] = str(message.get("message_id"))
        self._audit({"type": "paynotice_approved", "route": route.name, "contract": contract["id"],
                     "period": notice["period"], "version": notice["version"], "by": notice["approved_by"]})
        self._deliver(route, contract, notice, now, reply_to=message)

    # --- 4. 交付 ---

    def _deliver(self, route: Route, contract: dict, notice: dict, now: datetime, reply_to: dict | None = None) -> bool:
        """承認された版のPDFを、そのまま交付する。承認が無ければ何もしない。"""
        if not notice.get("approved_by") or notice.get("status") == ISSUED:
            return False
        target = route.delivery or {"room_id": contract.get("room_id"), "account_id": contract.get("account_id"),
                                    "name": contract.get("to_name", "")}
        room_id = int(target.get("room_id") or 0)
        try:
            pdf = Path(notice["pdf"]).read_bytes()
            if hashlib.sha256(pdf).hexdigest() != notice.get("pdf_sha256"):
                raise NoticeError("保存したPDFが承認時のものと一致しません")
            file_id = self._chatwork.upload_file(room_id, notice["filename"], pdf,
                                                 message=self._delivery_text(route, contract, notice, target))
            self._posts += 1
        except Exception as exc:
            print("[error] 支払通知書の交付に失敗: " + traceback.format_exc(), flush=True)
            if not notice.get("delivery_failed_at"):
                notice["delivery_failed_at"] = now.isoformat()
                self._alert(route, f"{contract.get('payee', '')}様の{period_label(notice['period'])}分の支払通知書を"
                                   f"交付できませんでした（{exc}）。次の巡回でもう一度試します。")
            return False
        notice["status"] = ISSUED
        notice["issued_at"] = now.isoformat()
        notice["delivery_file_id"] = str(file_id)
        notice.pop("delivery_failed_at", None)
        payment = date.fromisoformat(notice["payment_date"])
        text = (f"{route.banner}{contract.get('payee', '')}様のルームへ、{period_label(notice['period'])}分の支払通知書"
                f"（第{notice['version']}版・合計{yen(notice['total'])}）を交付しました。\n"
                f"支払予定日は{jp_date(payment)}です。振込の手配をお願いします。")
        if reply_to is not None:
            self._reply(route.approval_room, reply_to, "承認ありがとうございます。" + text)
        else:
            self._send(route.approval_room, text)
        self._audit({"type": "paynotice_issued", "route": route.name, "contract": contract["id"],
                     "period": notice["period"], "version": notice["version"], "sha256": notice["pdf_sha256"][:16]})
        return True

    def _delivery_text(self, route: Route, contract: dict, notice: dict, target: dict) -> str:
        account = int(target.get("account_id") or 0)
        name = str(target.get("name") or "")
        head = f"[To:{account}]{name + 'さん' if name else ''}\n" if account else ""
        fee = sum(int(line["quantity"]) * int(line["price"]) for line in notice["lines"] if line["kind"] == "fee")
        others = [line for line in notice["lines"] if line["kind"] != "fee"]
        breakdown = f"業務委託費{yen(fee)}"
        if others:
            breakdown += f"、立替経費など{len(others)}件 {yen(notice['total'] - fee)}"
        payment = date.fromisoformat(notice["payment_date"])
        return head + "\n".join([
            f"{route.banner}{period_label(notice['period'])}分の支払通知書をお送りします。",
            f"お支払金額の合計は{yen(notice['total'])}（{breakdown}）、お支払予定日は{jp_date(payment)}です。",
            "内容に誤りがありましたら、受領後7日以内にこのチャットでお知らせください。"
            "お申し出がない場合は、ご確認いただいたものとさせていただきます。",
        ])

    def _retry_delivery(self, route: Route, state: dict, now: datetime) -> None:
        for contract in self.contracts():
            for notice in self._cstate(state, contract)["notices"].values():
                if notice.get("status") == AWAITING and notice.get("approved_by"):
                    self._deliver(route, contract, notice, now)

    # --- 承認待ちの催促 ---

    def _remind(self, route: Route, state: dict, now: datetime) -> None:
        today = now.date()
        if not self._calendar.is_business_day(today):
            return
        if now < at_clock(today, parse_clock(self._settings().get("remind_time"), (9, 0))):
            return
        for contract in self.contracts():
            for notice in self._cstate(state, contract)["notices"].values():
                if notice.get("status") != AWAITING or notice.get("approved_by"):
                    continue
                built = _parse_iso(notice.get("built_at"))
                if built and built.date() >= today:
                    continue  # 今日見せた版は、今日は催促しない
                reminded = notice.setdefault("reminded", {})
                if reminded.get(today.isoformat()):
                    continue
                reminded[today.isoformat()] = True
                deadline = date.fromisoformat(notice["deadline"])
                left = self._calendar.days_left(today, deadline)
                if today > deadline:
                    urgency = f"交付期限（{jp_date(deadline)}）を過ぎています。至急ご確認ください。"
                elif today == deadline:
                    urgency = "本日が交付期限です。"
                else:
                    urgency = f"交付期限は{jp_date(deadline)}で、今日を含めてあと{left}営業日です。"
                link = message_url(route.approval_room, notice["request_ids"][0]) if notice.get("request_ids") else ""
                text = "\n".join(x for x in [
                    self._heads(route),
                    f"{route.banner}【承認待ち】{contract.get('payee', '')}様 {period_label(notice['period'])}分の支払通知書"
                    f"（第{notice['version']}版）の承認をお願いします。",
                    urgency,
                    link,
                ] if x)
                mid = self._send(route.approval_room, text)
                if mid:
                    notice.setdefault("request_ids", []).append(mid)
                if today >= deadline and route.extra_alert_room and route.extra_alert_room != route.approval_room:
                    self._send(route.extra_alert_room, f"{contract.get('payee', '')}様 {period_label(notice['period'])}分の"
                                                       f"支払通知書が未承認です。{urgency}\n{link}")

    # --- モデル ---

    def read_receipt(self, data: bytes, filename: str) -> dict:
        """証憑の画像・PDFを読む。→ {"receipts": [...], "note": ""}"""
        suffix = Path(filename).suffix.lower()
        if suffix == ".pdf":
            block = {"type": "document", "title": filename,
                     "source": {"type": "base64", "media_type": "application/pdf",
                                "data": base64.standard_b64encode(data).decode("ascii")}}
        else:
            media_type = IMAGE_TYPES.get(suffix)
            if media_type is None:
                return {"receipts": [], "note": "対応していない形式です"}
            data, media_type = shrink_image(data, media_type)
            block = {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                 "data": base64.standard_b64encode(data).decode("ascii")}}
        content = [block, {"type": "text", "text": f"この画像（{filename}）に写っている証憑を書き出してください。"}]
        return self._ask_model(RECEIPT_SYSTEM, RECEIPT_SCHEMA, content)

    def _read_reply(self, text: str, notice: dict) -> dict:
        rows = []
        for index, entry in enumerate(notice.get("display") or [], 1):
            number = int(entry.get("number") or index)
            kind = {"fee": "定例の業務委託費", "receipt": "証憑", "added": "手で足した行"}[entry["kind"]]
            rows.append(f"{number}（{circled(number)}）: {kind}「{entry['label']}」")
        ask = "[支払通知書の行]\n" + "\n".join(rows) + f"\n\n[承認者の返信]\n{text}"
        return self._ask_model(REVISE_SYSTEM, REVISE_SCHEMA, ask)

    def _ask_model(self, system: str, schema: dict, content: Any) -> dict:
        from .answer import _call_with_continuation

        cfg = self._config
        try:
            if self._client is None:
                import anthropic

                self._client = anthropic.Anthropic()
            kwargs = {
                "model": cfg.model,
                "max_tokens": int(self._settings().get("max_tokens", 4000)),
                "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                "system": system,
            }
            response, usage, _ = _call_with_continuation(
                self._client.messages.create, kwargs, [{"role": "user", "content": content}]
            )
        except Exception as exc:
            print("[warn] 支払通知書の読み取りに失敗: " + traceback.format_exc(), flush=True)
            raise ModelUnavailable(str(exc)) from exc
        if self._cost is not None:
            try:
                self._cost.add_usage(usage)
            except Exception:
                print("[warn] コスト計上に失敗: " + traceback.format_exc(), flush=True)
        reply = next((b.text for b in getattr(response, "content", []) if getattr(b, "type", "") == "text"), "")
        try:
            found = json.loads(reply)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ModelUnavailable("応答を読めませんでした") from exc
        if not isinstance(found, dict):
            raise ModelUnavailable("応答を読めませんでした")
        return found

    # --- 共通の部品 ---

    def _settings(self) -> dict:
        return self._config.payment_notice or {}

    def _default_sheets(self, spreadsheet_id: str) -> Any:
        from .gsheets import SheetsClient

        return SheetsClient(spreadsheet_id)

    def _heads(self, route: Route) -> str:
        return " ".join(f"[To:{a['account_id']}]{a['name'] + 'さん' if a.get('name') else ''}"
                        for a in route.approvers if a.get("account_id"))

    def _me(self) -> int:
        try:
            return int(self._chatwork.get_me())
        except Exception:
            print("[warn] 自アカウントIDの取得に失敗: " + traceback.format_exc(), flush=True)
            return 0

    def _send(self, room_id: int, text: str) -> str:
        try:
            mid = str(self._chatwork.send_message(int(room_id), text) or "")
        except Exception:
            print("[warn] 支払通知書の投稿に失敗: " + traceback.format_exc(), flush=True)
            return ""
        self._posts += 1
        if mid and int(room_id) == int(self._owned_room or 0):
            self._owned.append(mid)
        return mid

    def _reply(self, room_id: int, message: dict, text: str) -> str:
        return self._send(room_id, f"[rp aid={_account_of(message)} to={int(room_id)}-{message.get('message_id')}]\n{text}")

    def _alert(self, route: Route, text: str) -> None:
        self._send(route.approval_room, self._heads(route) + "\n" + route.banner + text)

    @staticmethod
    def _link_uploads(waiting: list[tuple[dict, dict, dict]], messages: list[dict]) -> None:
        """PDFを添付した投稿を承認依頼に結び付ける（その投稿への返信も、承認として読めるように）。"""
        for _, _, notice in waiting:
            for file_id in notice.get("request_file_ids") or []:
                marker = f"[download:{file_id}]"
                for message in messages:
                    mid = str(message.get("message_id"))
                    if marker in str(message.get("body") or "") and mid not in notice.setdefault("request_ids", []):
                        notice["request_ids"].append(mid)

    def _audit(self, record: dict) -> None:
        try:
            from .audit import AuditLogger

            cfg = self._config
            AuditLogger(log_dir=cfg.resolve_path(cfg.audit_log_dir),
                        retention_days=cfg.audit_log_retention_days).log(record)
        except Exception:
            print("[warn] 監査ログの記録に失敗: " + traceback.format_exc(), flush=True)

    # --- 状態 ---

    @staticmethod
    def _cstate(state: dict, contract: dict) -> dict:
        cstate = state.setdefault("contracts", {}).setdefault(str(contract["id"]), {})
        cstate.setdefault("last_seen", None)
        cstate.setdefault("receipts", [])
        cstate.setdefault("notices", {})
        return cstate

    def _load(self, route: Route) -> dict:
        try:
            state = json.loads((self._root / route.state_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        state.setdefault("contracts", {})
        state.setdefault("approval_last_seen", None)
        return state

    def _save(self, route: Route, state: dict) -> None:
        try:
            path = self._root / route.state_file
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            temp.replace(path)
        except OSError:
            print("[warn] 支払通知書の状態を保存できませんでした", flush=True)


def shrink_image(data: bytes, media_type: str) -> tuple[bytes, str]:
    """大きすぎる画像を縮める（スマートフォンの写真は数MBある）。縮められなければ読ませない。"""
    if len(data) <= MAX_IMAGE_BYTES:
        return data, media_type
    try:
        import io

        from PIL import Image, ImageOps

        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=85)
        return out.getvalue(), "image/jpeg"
    except Exception as exc:
        raise ModelUnavailable(f"画像が大きく、縮められませんでした（{len(data):,}バイト）") from exc


def _http_download(url: str) -> tuple[int, bytes]:
    import requests

    response = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    return response.status_code, response.content


def _parse_date(text: Any) -> date | None:
    try:
        return date.fromisoformat(str(text or "").strip())
    except ValueError:
        return None


def _short(text: Any) -> str:
    day = _parse_date(text)
    return f"{day.month}/{day.day}" if day else "日付不明"


# --- 手で動かす ---


def main(argv: list[str] | None = None) -> None:
    import argparse

    from .chatwork import ChatworkClient
    from .config import Config

    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="支払通知書の月次発行")
    parser.add_argument("--rehearse", metavar="YYYY-MM", help="管理者ルームで通し稽古を始める（対象の月）")
    parser.add_argument("--receipt", action="append", default=[], help="--rehearse で使う証憑のファイル（複数可）")
    parser.add_argument("--from-room", action="store_true",
                        help="--rehearse で、委託先のルームに上がっている添付（本人とテスト役の分）を証憑として使う")
    parser.add_argument("--create", metavar="YYYY-MM", help="本番の経路で、その月の支払通知書を作って承認を求める")
    parser.add_argument("--read", metavar="FILE", help="証憑の読み取りだけを試す")
    parser.add_argument("--contract", default=None, help="契約のID（1件だけなら省略可）")
    parser.add_argument("--status", action="store_true", help="控えの状態を表示する")
    parser.add_argument("--cleanup-rehearsal", action="store_true", help="リハーサルで作ったテスト用のタブと控えを消す")
    args = parser.parse_args(argv)
    config = Config.load()
    runner = PaymentNoticeRunner(config, ChatworkClient(config.chatwork_api_token or ""))
    if args.read:
        path = Path(args.read)
        print(json.dumps(runner.read_receipt(path.read_bytes(), path.name), ensure_ascii=False, indent=2))
    elif args.rehearse:
        notice = runner.rehearse(args.rehearse, [Path(p) for p in args.receipt], args.contract,
                                 from_room=args.from_room)
        print(f"リハーサルを始めました: {period_label(args.rehearse)}分 第{notice['version']}版 合計{yen(notice['total'])}")
    elif args.create:
        notice = runner.create(args.create, args.contract)
        print(f"承認を求めました: {period_label(args.create)}分 第{notice['version']}版 合計{yen(notice['total'])}")
    elif args.cleanup_rehearsal:
        print("消したタブ:", runner.cleanup_rehearsal() or "なし")
    elif args.status:
        for name, state in runner.status().items():
            for cid, cstate in (state.get("contracts") or {}).items():
                print(f"[{name}] {cid}: 証憑{len(cstate.get('receipts') or [])}件")
                for period, notice in sorted((cstate.get("notices") or {}).items()):
                    print(f"   {period} 第{notice.get('version')}版 {notice.get('status')} 合計{yen(notice.get('total', 0))} "
                          f"期限{notice.get('deadline')} 承認{notice.get('approved_by') or '-'}")
    else:
        print(f"投稿: {runner.run_once()}件")


if __name__ == "__main__":
    main()
