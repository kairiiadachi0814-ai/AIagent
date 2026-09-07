"""空き時間の提案と、複数人の予定調整。

「9日に金融機関との打合せ1時間取れる？」なら本人の予定から空きを返し、
「篠田さんと来週1時間打合せしたい。いつがいい？」なら関係者それぞれの予定と
勤務日・勤務時間を突き合わせ、全員が空いている時間帯を候補として返す。
管理者なら「1番で登録して」でそのまま登録できる。
「来週火曜14時から篠田さんと打合せを入れて」のように日時を決めて頼まれた
ときは、登録の前に相手の予定を確かめ、重なっていれば止めて代わりを出す。

方針:
- 他の人の予定は、その人のトヨクモ iCal（環境変数 SCHEDULE_ICS_URL_<account_id>）
  か、サービスアカウントに共有されたGoogleカレンダー（members[].google_calendar_id）
  から読む。読めない人は勤務日・勤務時間だけで見て、その旨を返信に書く
- 候補の日時はコードで決める。モデルに任せるのは依頼文の読み取りだけで、
  時刻を作文させない
- 見つからなかったときは「無かった」で終わらせず、その日の空きの実態
  （どこが何分空いているか）と、近い日の代わりを返す
- 終日の予定は、休暇・出張のような不在だけをその日の塞ぎとみなす。
  「[予定入力NG]」のような覚え書きは塞がず、注記として添える
- 既存の予定を動かす提案はしない（人の予定を勝手に動かさない）
- 登録は既存の登録フローと同じく管理者だけ。登録先は管理者のGoogleカレンダー
"""

from __future__ import annotations

import json
import re
import traceback
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

from .schedule import JST, Event, GoogleCalendarSource, IcsSource, build_sources, collect

WEEKDAYS = "月火水木金土日"

# 「打合せ」「会議」などの言葉と、「いつ」「候補」「調整」などの言葉が両方あれば提案の依頼
_MEETING_RE = re.compile(
    r"(打合せ|打ち合わせ|打合わせ|会議|ミーティング|MTG|面談|相談|予定|スケジュール|アポ|時間)", re.I
)
_PROPOSE_CUE_RE = re.compile(
    r"(候補|調整|提案|空き|空いて|いつ|都合|日程|合わせ|組ん|取れ|作れ|探し|良い日|いい日|"
    r"良い時間|いい時間|ベスト|おすすめ|お勧め|何日|何時が)"
)
_HOWTO_RE = re.compile(r"(手順|やり方|方法|書き方|どうやって|使い方)")
_CIRCLED = {"①": "1", "②": "2", "③": "3", "④": "4", "⑤": "5", "⑥": "6"}
_PICK_VERB_RE = re.compile(
    r"(?<!\d)([1-9])\s*(?:番目|番|つ目)?\s*(?:で|に|を|が|の)?\s*"
    r"(?:登録|お願い|入れ|よろしく|決定|確定|決め|いき|行き|行こ)"
)
_PICK_BARE_RE = re.compile(r"\s*([1-9])\s*(?:番目|番|つ目)?\s*(?:で|でお願いします|で登録して)?\s*[。！!]?\s*")

# 終日予定のうち、その日を丸ごと塞ぐとみなす言葉（休暇・出張など本人が居ない予定）
DEFAULT_BLOCK_WORDS = ("休", "出張", "外出", "不在", "旅行", "研修", "欠勤", "有給", "年休", "帰省")


def looks_like_proposal_request(question: str) -> bool:
    """空き時間の提案・日程調整の依頼か。"""
    if _HOWTO_RE.search(question):
        return False
    return bool(_MEETING_RE.search(question) and _PROPOSE_CUE_RE.search(question))


# 依頼文に日付・時刻が書かれているか（書かれていなければ直前の候補から補う）
_DATE_RE = re.compile(
    r"(\d{1,2}\s*[/月]\s*\d{1,2}|\d{1,2}日|今日|本日|明日|あした|明後日|あさって|来週|再来週|今週|[月火水木金土日]曜)"
)
_TIME_RE = re.compile(r"(\d{1,2})\s*[:：時]\s*(\d{2})?\s*(?:分|半)?")


def has_date(text: str) -> bool:
    return bool(_DATE_RE.search(unicodedata.normalize("NFKC", str(text or ""))))


def time_in(text: str) -> tuple[int, int] | None:
    """文中の最初の時刻（時, 分）。「14時半」「14:30」「14時」。無ければ None。"""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    match = _TIME_RE.search(normalized)
    if not match:
        return None
    hour = int(match.group(1))
    if hour > 23:
        return None
    minute = int(match.group(2) or 0)
    if "半" in normalized[match.end() - 1: match.end() + 1] and not match.group(2):
        minute = 30
    return hour, minute


_PICK_HEAD_RE = re.compile(r"^\s*[1-9]\s*(?:番目|番|つ目)?\s*(?:で|に|を|が|の)?")
_PICK_TAIL_RE = re.compile(
    r"(登録|お願い|入れ|よろしく|決定|確定|決め|いき|行き|行こ)"
    r"(しといて|しておいて|して|します|しました|ください|ましょう|ね|で|う)?"
)
_TIME_SPAN_RE = re.compile(
    r"\d{1,2}\s*[:時]\s*(?:\d{2})?\s*(?:分|半)?\s*(?:[〜~\-]|から)?\s*(?:\d{1,2}\s*[:時]\s*(?:\d{2})?\s*(?:分|半)?)?\s*(?:まで)?\s*(?:で|に|から)?"
)
_NOISE_RE = re.compile(
    r"(?:その|この|上記の|さっきの|先ほどの|提案の)?(?:時間|日時|候補|枠|時刻)(?:で|に|を)?|お願いします|ください|ですね|です"
)


def strip_pick(text: str) -> str:
    """「1番で登録して」「その時間で」「14:30で」を除いた残り（件名や場所の追加指定）。

    残りが無ければ空文字。空なら読み取りを掛けずに候補どおり登録できる。
    """
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    for mark, digit in _CIRCLED.items():
        normalized = normalized.replace(mark, digit)
    rest = _PICK_HEAD_RE.sub("", normalized, count=1)
    rest = _TIME_SPAN_RE.sub("", rest)
    rest = _PICK_TAIL_RE.sub("", rest)
    rest = _NOISE_RE.sub("", rest)
    rest = re.sub(r"^[\s、。・でにをがの]+|[\s、。・]+$", "", rest).strip()
    return rest if len(rest) >= 2 else ""


def pick_number(text: str) -> int | None:
    """「1番で登録して」「②で」のように候補の番号を選んでいれば、その番号。"""
    normalized = unicodedata.normalize("NFKC", str(text or "")).strip()
    for mark, digit in _CIRCLED.items():
        normalized = normalized.replace(mark, digit)
    match = _PICK_VERB_RE.search(normalized) or _PICK_BARE_RE.fullmatch(normalized)
    return int(match.group(1)) if match else None


# --- メンバーと勤務条件 ---


@dataclass
class Member:
    """予定を見に行ける社内メンバー。"""

    name: str
    account_id: int = 0
    is_owner: bool = False
    work_days: tuple[int, ...] = (0, 1, 2, 3, 4)
    work_start: int | None = None  # 分（9:00 → 540）
    work_end: int | None = None
    skip_holidays: bool = False
    ics_env: str = ""
    google_calendar_id: str = ""
    aliases: tuple[str, ...] = ()

    def env_name(self) -> str:
        return self.ics_env or f"SCHEDULE_ICS_URL_{self.account_id}"

    def works_on(self, day: date, holidays: set[str]) -> bool:
        if day.weekday() not in self.work_days:
            return False
        return not (self.skip_holidays and day.isoformat() in holidays)

    def hours_label(self) -> str:
        days = "・".join(WEEKDAYS[d] for d in sorted(self.work_days))
        if self.work_start is None and self.work_end is None:
            return f"{days}出勤"
        return f"{days}の{_fmt(self.work_start or 0)}〜{_fmt(self.work_end or 1440)}出勤"


def _hm(text: Any, default: int) -> int:
    try:
        hour, minute = str(text).split(":")
        return int(hour) * 60 + int(minute)
    except (ValueError, AttributeError):
        return default


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def jp_date(day: date) -> str:
    return f"{day.month}月{day.day}日（{WEEKDAYS[day.weekday()]}）"


def names_of(members: list[Member]) -> str:
    return "・".join(m.name for m in members) + "さん"


def load_members(config: Any) -> list[Member]:
    """config.schedule.members → Member。owner_name の人は既存の予定ソース（トヨクモ＋Google）を使う。"""
    cfg = config.schedule
    owner_name = str(cfg.get("owner_name", "") or "")
    members: list[Member] = []
    for raw in cfg.get("members") or []:
        name = str(raw.get("name", "")).strip()
        if not name:
            continue
        hours = raw.get("work_hours") or {}
        members.append(
            Member(
                name=name,
                account_id=int(raw.get("account_id", 0) or 0),
                is_owner=bool(raw.get("owner")) or (bool(owner_name) and name == owner_name),
                work_days=tuple(int(d) for d in (raw.get("work_days") or (0, 1, 2, 3, 4))),
                work_start=_hm(hours.get("start"), 0) if hours.get("start") else None,
                work_end=_hm(hours.get("end"), 0) if hours.get("end") else None,
                skip_holidays=bool(raw.get("skip_holidays")),
                ics_env=str(raw.get("ics_env", "") or ""),
                google_calendar_id=str(raw.get("google_calendar_id", "") or ""),
                aliases=tuple(str(a) for a in (raw.get("aliases") or [])),
            )
        )
    if owner_name and not any(m.is_owner for m in members):
        members.insert(0, Member(name=owner_name, is_owner=True))
    return members


def find_members(text: str, members: list[Member]) -> list[Member]:
    """文中に名前が出てくるメンバー（出てきた順）。"""
    flat = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")))
    found: list[tuple[int, Member]] = []
    for member in members:
        for name in (member.name, *member.aliases):
            position = flat.find(re.sub(r"\s+", "", name))
            if position >= 0:
                found.append((position, member))
                break
    return [m for _, m in sorted(found, key=lambda pair: pair[0])]


def member_by_account(account_id: int, members: list[Member]) -> Member | None:
    return next((m for m in members if m.account_id and int(m.account_id) == int(account_id)), None)


def sources_for(member: Member, config: Any) -> list[Any]:
    """そのメンバーの予定を読むソース。設定が無ければ空（勤務日だけで見る）。"""
    import os

    if member.is_owner:
        return build_sources(config)
    sources: list[Any] = []
    url = os.environ.get(member.env_name())
    if url:
        sources.append(IcsSource(url, f"{member.name}さんのカレンダー"))
    credentials = os.environ.get("GOOGLE_CALENDAR_CREDENTIALS_PATH")
    if member.google_calendar_id and credentials:
        sources.append(
            GoogleCalendarSource(credentials, member.google_calendar_id, label=f"{member.name}さんのGoogleカレンダー")
        )
    return sources


# --- 空き時間の計算 ---


@dataclass
class Settings:
    work_start: int = 9 * 60
    work_end: int = 18 * 60
    lunch: tuple[int, int] | None = (12 * 60, 13 * 60)
    buffer: int = 15
    default_minutes: int = 60
    horizon_days: int = 14
    max_candidates: int = 3
    per_day: int = 2
    skip_holidays: bool = True
    all_day_block_words: tuple[str, ...] = DEFAULT_BLOCK_WORDS
    lead_minutes: int = 60
    grid: int = 30
    remember_minutes: int = 1440
    # 空きが見つからなかったとき、日ごとの空きの実態を書く期間の上限（日）
    explain_days: int = 3
    # 直前のやり取りの条件（日付・相手・所要時間）を次の依頼に引き継ぐ時間（分）。
    # 「午後で」「30分でいい」のような続きを、同じ日程の話として扱う
    context_minutes: int = 120


def load_settings(config: Any) -> Settings:
    raw = config.schedule.get("proposal") or {}
    hours = raw.get("work_hours") or {}
    lunch_raw = raw.get("lunch")
    lunch = None
    if lunch_raw and lunch_raw.get("start") and lunch_raw.get("end"):
        lunch = (_hm(lunch_raw["start"], 12 * 60), _hm(lunch_raw["end"], 13 * 60))
    elif lunch_raw is None:
        lunch = (12 * 60, 13 * 60)
    if "all_day_block_words" in raw:
        block_words = tuple(str(w) for w in (raw.get("all_day_block_words") or []))
    else:
        block_words = DEFAULT_BLOCK_WORDS if raw.get("all_day_blocks", True) else ()
    return Settings(
        work_start=_hm(hours.get("start"), 9 * 60),
        work_end=_hm(hours.get("end"), 18 * 60),
        lunch=lunch,
        buffer=int(raw.get("buffer_minutes", 15)),
        default_minutes=int(raw.get("default_minutes", 60)),
        horizon_days=int(raw.get("horizon_days", 14)),
        max_candidates=int(raw.get("max_candidates", 3)),
        per_day=int(raw.get("per_day", 2)),
        skip_holidays=bool(raw.get("skip_holidays", True)),
        all_day_block_words=block_words,
        lead_minutes=int(raw.get("lead_minutes", 60)),
        grid=int(raw.get("grid_minutes", 30)),
        remember_minutes=int(raw.get("remember_minutes", 1440)),
        explain_days=int(raw.get("explain_days", 3)),
        context_minutes=int(raw.get("context_minutes", 120)),
    )


def blocks_day(event: Event, words: tuple[str, ...]) -> bool:
    """終日予定がその日を塞ぐか（休暇・出張などの不在だけ）。"""
    return event.all_day and any(w and w in event.summary for w in words)


def day_reason(day: date, members: list[Member], settings: Settings, holidays: set[str]) -> str:
    """その日に枠を探せない理由（土日・祝日・誰かの休み）。探せるなら空文字。"""
    if day.weekday() >= 5:
        return "土日"
    if settings.skip_holidays and day.isoformat() in holidays:
        return "祝日"
    off = [m.name for m in members if not m.works_on(day, holidays)]
    if off:
        return "・".join(off) + "さんの出勤日ではありません"
    return ""


def common_window(day: date, members: list[Member], settings: Settings, holidays: set[str]) -> tuple[int, int] | None:
    """その日に全員が揃える時間帯（分）。誰かの休みなら None。"""
    if day_reason(day, members, settings, holidays):
        return None
    start, end = settings.work_start, settings.work_end
    for member in members:
        if member.work_start is not None:
            start = max(start, member.work_start)
        if member.work_end is not None:
            end = min(end, member.work_end)
    return (start, end) if end > start else None


def _minutes_of_day(events: list[Event], day: date) -> list[tuple[int, int, Event]]:
    day_start = datetime.combine(day, time.min, tzinfo=JST)
    day_end = day_start + timedelta(days=1)
    out = []
    for event in events:
        start, end = event.start.astimezone(JST), event.end.astimezone(JST)
        if end <= day_start or start >= day_end:
            continue
        begin = max(0, int((start - day_start).total_seconds() // 60))
        finish = min(24 * 60, int((end - day_start).total_seconds() // 60))
        out.append((begin, finish, event))
    return out


def busy_of(events: list[Event], day: date, buffer: int, block_words: tuple[str, ...]) -> list[tuple[int, int]]:
    """その日の埋まっている時間帯（分）。前後に余白を足す。不在の終日予定は1日ふさぐ。"""
    out: list[tuple[int, int]] = []
    for begin, finish, event in _minutes_of_day(events, day):
        if event.all_day:
            if blocks_day(event, block_words):
                out.append((0, 24 * 60))
            continue
        out.append((max(0, begin - buffer), min(24 * 60, finish + buffer)))
    return out


def all_day_notes(events: list[Event], day: date, block_words: tuple[str, ...]) -> list[str]:
    """その日の終日予定のうち、塞がずに注記だけするもの（「[予定入力NG]」など）。"""
    return [
        f"終日「{event.summary}」"
        for _, _, event in _minutes_of_day(events, day)
        if event.all_day and not blocks_day(event, block_words)
    ]


def free_gaps(window: tuple[int, int], busy: list[tuple[int, int]], lunch: tuple[int, int] | None) -> list[tuple[int, int]]:
    """時間帯の中で空いている区間（余白・昼休みを除いた実態）。"""
    blocks = sorted(list(busy) + ([lunch] if lunch else []))
    gaps: list[tuple[int, int]] = []
    cursor = window[0]
    for start, end in blocks:
        if end <= cursor:
            continue
        if start > cursor:
            gaps.append((cursor, min(start, window[1])))
        cursor = max(cursor, end)
        if cursor >= window[1]:
            break
    if cursor < window[1]:
        gaps.append((cursor, window[1]))
    return [(s, e) for s, e in gaps if e > s]


def free_slots(
    window: tuple[int, int],
    busy: list[tuple[int, int]],
    duration: int,
    lunch: tuple[int, int] | None,
    grid: int,
    not_before: int = 0,
) -> list[tuple[int, int]]:
    """時間帯の中で、埋まっていない duration 分の枠。

    grid 分刻みの切りの良い開始に加えて、空き区間の頭（14:15 など）も候補にする。
    切りの良い時刻だけだと、ちょうど収まる区間を取りこぼすため。
    """
    slots: set[tuple[int, int]] = set()
    for gap_start, gap_end in free_gaps(window, busy, lunch):
        if gap_end - gap_start < duration:
            continue
        starts = {gap_start}
        at = ((gap_start + grid - 1) // grid) * grid
        while at + duration <= gap_end:
            starts.add(at)
            at += grid
        for start in starts:
            if start >= not_before and start + duration <= gap_end:
                slots.add((start, start + duration))
    return sorted(slots)


def pick_for_day(slots: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """1日の枠から、勧めやすい順に最大2つ。正時→30分→その他の順に優先し、2つ目は離れた時間帯。"""
    if not slots:
        return []

    def tier(slot: tuple[int, int]) -> int:
        return 0 if slot[0] % 60 == 0 else (1 if slot[0] % 30 == 0 else 2)

    best_tier = min(tier(s) for s in slots)
    ranked = [s for s in slots if tier(s) == best_tier]
    first = ranked[0]
    picks = [first]
    later = [s for s in slots if s[0] >= first[1] + 120]
    if later:
        later_tier = min(tier(s) for s in later)
        picks.append([s for s in later if tier(s) == later_tier][0])
    return picks


@dataclass
class Candidate:
    start: datetime
    end: datetime

    def label(self) -> str:
        return f"{jp_date(self.start.date())}{self.start:%H:%M}〜{self.end:%H:%M}"

    def as_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass
class Constraints:
    duration: int
    date_from: date
    date_to: date
    time_of_day: str = "any"
    earliest: int | None = None
    latest: int | None = None

    def span_label(self) -> str:
        if self.date_from == self.date_to:
            return jp_date(self.date_from)
        return f"{jp_date(self.date_from)}〜{jp_date(self.date_to)}"


def _window_for(day: date, members: list[Member], constraints: Constraints, settings: Settings, holidays: set[str]) -> tuple[int, int] | None:
    window = common_window(day, members, settings, holidays)
    if window is None:
        return None
    start, end = window
    if constraints.time_of_day == "am" and settings.lunch:
        end = min(end, settings.lunch[0])
    elif constraints.time_of_day == "pm" and settings.lunch:
        start = max(start, settings.lunch[1])
    if constraints.earliest is not None:
        start = max(start, constraints.earliest)
    if constraints.latest is not None:
        end = min(end, constraints.latest)
    return (start, end) if end > start else None


def _busy_all(day: date, members: list[Member], events_by_member: dict[str, list[Event]], settings: Settings) -> list[tuple[int, int]]:
    busy: list[tuple[int, int]] = []
    for member in members:
        busy += busy_of(events_by_member.get(member.name, []), day, settings.buffer, settings.all_day_block_words)
    return busy


def _not_before(day: date, now: datetime, settings: Settings) -> int:
    if day < now.date():
        return 24 * 60
    if day == now.date():
        lead = now + timedelta(minutes=settings.lead_minutes)
        return lead.hour * 60 + lead.minute
    return 0


def find_candidates(
    members: list[Member],
    events_by_member: dict[str, list[Event]],
    constraints: Constraints,
    settings: Settings,
    holidays: set[str],
    now: datetime,
) -> list[Candidate]:
    """全員が空いている枠を、日を分散させて max_candidates 件まで。"""
    firsts: list[Candidate] = []
    seconds: list[Candidate] = []
    day = constraints.date_from
    while day <= constraints.date_to:
        window = _window_for(day, members, constraints, settings, holidays)
        if window is not None:
            busy = _busy_all(day, members, events_by_member, settings)
            slots = free_slots(window, busy, constraints.duration, settings.lunch, settings.grid, _not_before(day, now, settings))
            for index, (s, e) in enumerate(pick_for_day(slots)[: max(1, settings.per_day)]):
                candidate = Candidate(
                    start=datetime.combine(day, time(s // 60, s % 60), tzinfo=JST),
                    end=datetime.combine(day, time(e // 60, e % 60), tzinfo=JST),
                )
                (firsts if index == 0 else seconds).append(candidate)
        day += timedelta(days=1)
    chosen = (firsts + seconds)[: settings.max_candidates]
    return sorted(chosen, key=lambda c: c.start)


def explain_days(
    members: list[Member],
    events_by_member: dict[str, list[Event]],
    constraints: Constraints,
    settings: Settings,
    holidays: set[str],
    now: datetime,
) -> list[str]:
    """枠が無かった期間について、日ごとの実態を1行ずつ（短い期間だけ）。"""
    lines: list[str] = []
    day = constraints.date_from
    while day <= constraints.date_to:
        reason = day_reason(day, members, settings, holidays)
        if reason:
            lines.append(f"{jp_date(day)}: {reason}")
        else:
            window = _window_for(day, members, constraints, settings, holidays)
            busy = _busy_all(day, members, events_by_member, settings)
            gaps = [
                (max(s, _not_before(day, now, settings)), e)
                for s, e in (free_gaps(window, busy, settings.lunch) if window else [])
            ]
            gaps = [(s, e) for s, e in gaps if e - s >= 30]  # 数分の隙間は書かない
            if gaps:
                lines.append(
                    f"{jp_date(day)}の空き: " + "、".join(f"{_fmt(s)}〜{_fmt(e)}（{e - s}分）" for s, e in gaps)
                )
            else:
                lines.append(f"{jp_date(day)}: 予定で埋まっています")
        day += timedelta(days=1)
    return lines


def notes_for_days(
    days: list[date], members: list[Member], events_by_member: dict[str, list[Event]], settings: Settings
) -> list[str]:
    """候補に挙げた日の、塞がない終日予定の注記（誰の何か）。"""
    lines: list[str] = []
    for day in sorted(set(days)):
        for member in members:
            for note in all_day_notes(events_by_member.get(member.name, []), day, settings.all_day_block_words):
                who = f"{member.name}さんに" if len(members) > 1 else ""
                lines.append(f"{jp_date(day)}は{who}{note}が入っています")
    return lines


def conflicts_at(
    start: datetime, end: datetime, members: list[Member], events_by_member: dict[str, list[Event]],
    settings: Settings, holidays: set[str],
) -> list[str]:
    """その時間に誰が何で塞がっているか（人ごとに1行）。空なら全員空き。"""
    day = start.astimezone(JST).date()
    begin = start.astimezone(JST).hour * 60 + start.astimezone(JST).minute
    finish = end.astimezone(JST).hour * 60 + end.astimezone(JST).minute
    lines: list[str] = []
    for member in members:
        if not member.works_on(day, holidays):
            lines.append(f"{member.name}さんは{jp_date(day)}は出勤日ではありません")
            continue
        if (member.work_start is not None and begin < member.work_start) or (
            member.work_end is not None and finish > member.work_end
        ):
            lines.append(f"{member.name}さんの勤務時間（{_fmt(member.work_start or 0)}〜{_fmt(member.work_end or 1440)}）の外です")
            continue
        for event in events_by_member.get(member.name, []):
            if event.all_day and not blocks_day(event, settings.all_day_block_words):
                continue
            s, e = event.start.astimezone(JST), event.end.astimezone(JST)
            if s < end and e > start:
                lines.append(f"{member.name}さんに「{event.summary}」（{event.time_label()}）が入っています")
                break
    return lines


# --- 依頼文の読み取り ---

_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "participants": {
            "type": "array",
            "items": {"type": "string"},
            "description": "依頼文に出てくる社内メンバーの苗字（依頼者自身は含めない）。無ければ空配列",
        },
        "duration_minutes": {"type": "integer", "description": "所要時間（分）。書かれていなければ0"},
        "date_from": {"type": "string", "description": "探し始める日 YYYY-MM-DD。書かれていなければ空文字"},
        "date_to": {
            "type": "string",
            "description": "探し終える日 YYYY-MM-DD。「来週」なら来週の金曜、「今週中」なら今週の金曜。書かれていなければ空文字",
        },
        "time_of_day": {"type": "string", "enum": ["any", "am", "pm"], "description": "午前・午後の希望"},
        "earliest": {"type": "string", "description": "この時刻以降 HH:MM。無ければ空文字"},
        "latest": {"type": "string", "description": "この時刻までに終える HH:MM。無ければ空文字"},
        "fixed_start": {
            "type": "string",
            "description": "開始日時が具体的に指定されていれば YYYY-MM-DD HH:MM。候補を求めているなら空文字",
        },
        "summary": {"type": "string", "description": "登録するときの件名。例: 篠田さんと打合せ。依頼文の言葉を使う"},
        "opening": {
            "type": "string",
            "description": "依頼を受けた側の一言（1文）。候補を出す前の言葉で、登録した・決めたとは言わない",
        },
    },
    "required": [
        "participants", "duration_minutes", "date_from", "date_to", "time_of_day",
        "earliest", "latest", "fixed_start", "summary", "opening",
    ],
    "additionalProperties": False,
}

PLAN_SYSTEM = """あなたは株式会社ライズクリエイション経理財務部のアシスタント「{agent_name}」です。
日程調整の依頼文から、条件を読み取ります。候補の日時を決めるのはあなたではなく
プログラム側です。

厳守すること:
- 依頼文に書かれていることだけを使う。所要時間や日付を推測で作らない
- 「来週」「今週中」は today と曜日を基準に YYYY-MM-DD へ直す。
  「9日に」のように1日だけを指しているなら、today 以降で最も近いその日を
  date_from と date_to の両方に入れる。「来週以降」のように終わりが無ければ
  date_to は空文字にする
- 社内メンバーの名前は次の一覧にある苗字で書く: {members}。
  「金融機関」「先方」「○○様」のような社外の相手は participants に入れない
- opening は「篠田さんとの打合せの日程ですね。」のような受けの一言にする。
  「登録しました」「決めました」のように済んだ言い方はしない
"""


class PlanRunner:
    """空き時間の提案・日時の衝突確認・候補からの登録。"""

    def __init__(
        self,
        config: Any,
        client: Any | None = None,
        sources_for: Callable[[Member], list[Any]] | None = None,
        now: Callable[[], datetime] | None = None,
        holidays: set[str] | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._sources_for = sources_for or (lambda member: globals()["sources_for"](member, config))
        self._now = now or (lambda: datetime.now(JST))
        if holidays is None:
            from .letterpack import load_holidays

            holidays, _ = load_holidays(config)
        self._holidays = set(holidays)
        self._settings = load_settings(config)
        self._state_path = config.resolve_path(config.state_dir) / "schedule_plan.json"

    # --- 公開API ---

    def propose(
        self, question: str, requester_id: int = 0, is_admin: bool = False, context: dict | None = None
    ) -> tuple[str, dict, dict]:
        """候補を出す。日時が決め打ちなら、その時間の空きを確かめる。

        context は直前のやり取り（remember したもの）。依頼文に無い日付・相手・
        所要時間・件名はそこから補い、同じことを聞き返さない。
        """
        members = load_members(self._config)
        fields, usage = self._extract(question, members)
        fields = self._inherit(fields, context or {})
        participants = self._participants(question, fields, members, requester_id)
        constraints = self._constraints(fields)
        summary = str(fields.get("summary") or "").strip() or self._default_summary(participants, requester_id)
        now = self._now()

        fixed = _parse_fixed(fields.get("fixed_start"))
        found: list[str] = []
        if fixed is not None:
            end = fixed + timedelta(minutes=constraints.duration)
            events, notes, failed = self._gather(participants, constraints, fixed.date(), fixed.date())
            found = conflicts_at(fixed, end, participants, events, self._settings, self._holidays)
            if not found:
                candidates = [Candidate(fixed, end)]
                notes += notes_for_days([fixed.date()], participants, events, self._settings)
                text = self._free_reply(fields, participants, fixed, end, notes, is_admin)
            else:
                events, more_notes, more_failed = self._gather(participants, constraints)
                candidates = find_candidates(participants, events, constraints, self._settings, self._holidays, now)
                notes += [n for n in more_notes if n not in notes]
                failed += more_failed
                notes += notes_for_days([c.start.date() for c in candidates], participants, events, self._settings)
                text = self._conflict_reply(fields, participants, fixed, end, found, candidates, notes, is_admin)
        else:
            events, notes, failed = self._gather(participants, constraints)
            candidates = find_candidates(participants, events, constraints, self._settings, self._holidays, now)
            explanation: list[str] = []
            alternatives = False
            if not candidates:
                # 「無かった」で終わらせない。短い期間なら日ごとの実態を書き、近い日の代わりを探す
                span_days = (constraints.date_to - constraints.date_from).days + 1
                if span_days <= self._settings.explain_days:
                    explanation = explain_days(participants, events, constraints, self._settings, self._holidays, now)
                ahead = Constraints(
                    duration=constraints.duration,
                    date_from=constraints.date_to + timedelta(days=1),
                    date_to=constraints.date_to + timedelta(days=self._settings.horizon_days),
                    time_of_day=constraints.time_of_day,
                    earliest=constraints.earliest,
                    latest=constraints.latest,
                )
                events_ahead, _, more_failed = self._gather(participants, ahead)
                failed += [f for f in more_failed if f not in failed]
                candidates = find_candidates(participants, events_ahead, ahead, self._settings, self._holidays, now)
                alternatives = bool(candidates)
                events = {
                    name: events.get(name, []) + events_ahead.get(name, [])
                    for name in set(events) | set(events_ahead)
                }
            notes += notes_for_days([c.start.date() for c in candidates], participants, events, self._settings)
            text = self._proposal_reply(fields, participants, constraints, candidates, notes, is_admin, explanation, alternatives)

        meta = {
            "participants": [m.name for m in participants],
            "candidates": [c.as_dict() for c in candidates],
            "summary": summary,
            "duration": constraints.duration,
            "fixed": fixed.isoformat() if fixed else "",
            "conflicts": found,
            "failed_sources": failed,
            # 次の依頼（「午後で」「30分でいい」）に引き継ぐ条件
            "context": {
                "date_from": constraints.date_from.isoformat(),
                "date_to": constraints.date_to.isoformat(),
                "duration": constraints.duration,
                "participants": [m.name for m in participants],
                "summary": summary,
            },
        }
        return text, meta, usage

    @staticmethod
    def _inherit(fields: dict, context: dict) -> dict:
        """依頼文に無い条件を、直前のやり取りから補う（書いてあるものは上書きしない）。"""
        merged = dict(fields)
        if not merged.get("date_from") and not merged.get("date_to") and not merged.get("fixed_start"):
            if context.get("date_from"):
                merged["date_from"] = context["date_from"]
                merged["date_to"] = context.get("date_to") or context["date_from"]
        if not merged.get("duration_minutes") and context.get("duration"):
            merged["duration_minutes"] = int(context["duration"])
        if not merged.get("participants") and context.get("participants"):
            merged["participants"] = list(context["participants"])
        if not merged.get("summary") and context.get("summary"):
            merged["summary"] = context["summary"]
        return merged

    def conflict_check(self, question: str, requester_id: int = 0) -> Callable[[list[Event]], str | None] | None:
        """登録の前に、依頼文に名前の出た人の予定を確かめる関数。誰も出てこなければ None。"""
        members = load_members(self._config)
        others = [m for m in find_members(question, members) if not m.is_owner]
        if not others:
            return None
        owner = next((m for m in members if m.is_owner), None)
        participants = ([owner] if owner else []) + others

        def check(events: list[Event]) -> str | None:
            for event in events:
                if event.all_day:
                    continue
                span = Constraints(
                    duration=int((event.end - event.start).total_seconds() // 60) or self._settings.default_minutes,
                    date_from=event.start.astimezone(JST).date(),
                    date_to=event.start.astimezone(JST).date() + timedelta(days=self._settings.horizon_days),
                )
                gathered, notes, _ = self._gather(participants, span)
                found = conflicts_at(event.start, event.end, participants, gathered, self._settings, self._holidays)
                if found:
                    candidates = find_candidates(participants, gathered, span, self._settings, self._holidays, self._now())
                    notes += notes_for_days([c.start.date() for c in candidates], participants, gathered, self._settings)
                    fields = {"opening": "", "summary": event.summary}
                    text = self._conflict_reply(fields, participants, event.start, event.end, found, candidates, notes, True)
                    self._last_check = {"candidates": candidates, "summary": event.summary, "participants": participants}
                    return text
            return None

        self._last_check = {}
        return check

    def last_check(self) -> dict:
        return getattr(self, "_last_check", {}) or {}

    def register_pick(
        self, pending: dict, number: int, schedule_runner: Any, extra: str = ""
    ) -> tuple[str, dict, dict]:
        """覚えている候補から番号で1つ選び、管理者のカレンダーへ登録する。

        extra に件名や場所の追加指定（「件名は設備資金調達の件、場所は会議室②」）が
        あれば読み取って使う。日時は候補のものを正とし、読み取りで上書きしない。
        """
        candidates = pending.get("candidates") or []
        if not 1 <= number <= len(candidates):
            return (
                f"すみません、候補は{len(candidates)}件までです。番号をもう一度お知らせください。",
                {"error": "bad_number"},
                {},
            )
        chosen = candidates[number - 1]
        start, end = datetime.fromisoformat(chosen["start"]), datetime.fromisoformat(chosen["end"])
        summary = str(pending.get("summary") or "打合せ")
        location = ""
        usage: dict = {}
        opening = ""
        extra = extra.strip()
        if extra:
            text = (
                f"{jp_date(start.date())} {start:%H:%M}〜{end:%H:%M} に「{summary}」の予定を登録する。"
                f"次の指定があれば件名・場所に反映する: {extra}"
            )
            try:
                events, fields, usage = schedule_runner.extract_events(text)
            except Exception:
                print("[warn] 追加指定の読み取りに失敗: " + traceback.format_exc(), flush=True)
                events, fields = [], {}
            if events:
                summary = events[0].summary or summary
                location = events[0].location
                opening = str(fields.get("opening") or "")
        event = Event(summary=summary, start=start, end=end, location=location)
        reply, meta, _ = schedule_runner.register_events([event], opening)
        return reply, meta, usage

    def register_from_pending(
        self, pending: dict, question: str, schedule_runner: Any
    ) -> tuple[str, dict, dict] | None:
        """「登録して」「14:30で登録して」を、覚えている候補で解決する。

        日付が依頼文に無いときだけ使う（日付があれば普通の登録として扱う）。
        候補が1つならそれを登録し、時刻が書かれていれば合う候補を登録する。
        複数あって決められなければ番号を聞く（日付・件名は聞き返さない）。
        """
        candidates = pending.get("candidates") or []
        if not candidates or has_date(question):
            return None
        clock = time_in(question)
        if clock is not None:
            for index, candidate in enumerate(candidates, 1):
                start = datetime.fromisoformat(candidate["start"])
                if (start.hour, start.minute) == clock:
                    return self.register_pick(pending, index, schedule_runner, extra=strip_pick(question))
            return None  # 候補に無い時刻。日付が無いので普通の登録では聞き返しになる
        if len(candidates) == 1:
            return self.register_pick(pending, 1, schedule_runner, extra=strip_pick(question))
        lines = ["どの時間で登録しましょうか。番号でお知らせください。"]
        for index, candidate in enumerate(candidates, 1):
            start = datetime.fromisoformat(candidate["start"])
            end = datetime.fromisoformat(candidate["end"])
            lines.append(f"{index}. {jp_date(start.date())}{start:%H:%M}〜{end:%H:%M}")
        return "\n".join(lines), {"error": "which_candidate", "candidates": candidates}, {}

    # --- 候補と条件を覚えておく（「1番で登録して」「午後で」のため） ---

    def remember(self, room_id: int, account_id: int, ts: int, meta: dict) -> None:
        if not meta.get("candidates") and not meta.get("context"):
            return
        data = self._load()
        data[f"{room_id}:{account_id}"] = {
            "ts": int(ts),
            "candidates": meta.get("candidates") or [],
            "summary": meta.get("summary", ""),
            "participants": meta.get("participants", []),
            "context": meta.get("context") or {},
        }
        self._save(data)

    def pending(self, room_id: int, account_id: int, now_ts: int) -> dict | None:
        entry = self._load().get(f"{room_id}:{account_id}")
        if not entry:
            return None
        if now_ts - int(entry.get("ts", 0)) > self._settings.remember_minutes * 60:
            return None
        return entry

    def context_for(self, room_id: int, account_id: int, now_ts: int) -> dict:
        """直前のやり取りの条件（新しいうちだけ）。無ければ空。"""
        entry = self.pending(room_id, account_id, now_ts)
        if not entry or now_ts - int(entry.get("ts", 0)) > self._settings.context_minutes * 60:
            return {}
        context = dict(entry.get("context") or {})
        context.setdefault("summary", entry.get("summary", ""))
        context.setdefault("participants", entry.get("participants", []))
        return context

    def clear(self, room_id: int, account_id: int) -> None:
        data = self._load()
        if data.pop(f"{room_id}:{account_id}", None) is not None:
            self._save(data)

    # --- 内部 ---

    def _extract(self, question: str, members: list[Member]) -> tuple[dict, dict]:
        from .answer import _call_with_continuation

        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        cfg = self._config
        now = self._now()
        kwargs = {
            "model": cfg.model,
            "max_tokens": int(cfg.max_tokens),
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": _PLAN_SCHEMA}},
            "system": PLAN_SYSTEM.format(
                agent_name=cfg.agent_name, members="、".join(m.name for m in members) or "（未設定）"
            ),
        }
        prompt = (
            f"today: {now:%Y-%m-%d}（{WEEKDAYS[now.weekday()]}曜日）\n\n"
            f"===依頼ここから===\n{question}\n===依頼ここまで==="
        )
        try:
            response, usage, _ = _call_with_continuation(
                self._client.messages.create, kwargs, [{"role": "user", "content": prompt}]
            )
            text = next(
                (b.text for b in getattr(response, "content", []) if getattr(b, "type", "") == "text"), ""
            )
            return json.loads(text), usage
        except Exception:
            print("[warn] 日程調整の読み取りに失敗: " + traceback.format_exc(), flush=True)
            return {}, {}

    def _participants(self, question: str, fields: dict, members: list[Member], requester_id: int) -> list[Member]:
        named = find_members(question + " " + " ".join(str(p) for p in fields.get("participants") or []), members)
        chosen: list[Member] = []
        requester = member_by_account(requester_id, members)
        if requester is not None:
            chosen.append(requester)
        for member in named:
            if member not in chosen:
                chosen.append(member)
        if not chosen:
            owner = next((m for m in members if m.is_owner), None)
            if owner is not None:
                chosen.append(owner)
        return chosen

    def _constraints(self, fields: dict) -> Constraints:
        today = self._now().date()
        duration = int(fields.get("duration_minutes") or 0) or self._settings.default_minutes
        date_from = _parse_date(fields.get("date_from")) or today
        date_to = _parse_date(fields.get("date_to")) or (date_from + timedelta(days=self._settings.horizon_days))
        if date_to < date_from:
            date_to = date_from
        return Constraints(
            duration=duration,
            date_from=max(date_from, today),
            date_to=max(date_to, today),
            time_of_day=str(fields.get("time_of_day") or "any"),
            earliest=_hm(fields["earliest"], 0) if fields.get("earliest") else None,
            latest=_hm(fields["latest"], 0) if fields.get("latest") else None,
        )

    def _gather(
        self, participants: list[Member], constraints: Constraints,
        date_from: date | None = None, date_to: date | None = None,
    ) -> tuple[dict[str, list[Event]], list[str], list[str]]:
        """各自の予定を読む。→ (名前→予定, 注記, 読めなかったソース)"""
        start = datetime.combine(date_from or constraints.date_from, time.min, tzinfo=JST)
        end = datetime.combine(date_to or constraints.date_to, time.min, tzinfo=JST) + timedelta(days=1)
        events: dict[str, list[Event]] = {}
        notes: list[str] = []
        failed: list[str] = []
        for member in participants:
            sources = self._sources_for(member)
            if not sources:
                events[member.name] = []
                notes.append(f"{member.name}さんのカレンダーは未設定のため、{member.hours_label()}として見ています")
                continue
            schedule = collect(sources, start, end)
            events[member.name] = schedule.events
            if schedule.failed_sources:
                failed += schedule.failed_sources
                notes.append(f"{member.name}さんのカレンダーを読み取れなかったため、抜けている予定があるかもしれません")
            if member.work_start is not None or member.work_end is not None or member.work_days != (0, 1, 2, 3, 4):
                notes.append(f"{member.name}さんは{member.hours_label()}として見ています")
        return events, notes, failed

    @staticmethod
    def _default_summary(participants: list[Member], requester_id: int) -> str:
        others = [m.name for m in participants if not (m.account_id and int(m.account_id) == int(requester_id))]
        return ("・".join(others) + "さんと打合せ") if others else "打合せ"

    # --- 返信の組み立て（日時はコードで書く） ---

    def _lead(self, fields: dict, participants: list[Member]) -> str:
        opening = str(fields.get("opening") or "").strip()
        if not opening or re.search(r"(登録しました|決めました|入れました|確定しました)", opening):
            opening = f"{names_of(participants)}の日程ですね。"
        return opening

    def _conditions(self, constraints: Constraints) -> str:
        bits = [f"{constraints.duration}分"]
        if constraints.time_of_day == "am":
            bits.append("午前")
        elif constraints.time_of_day == "pm":
            bits.append("午後")
        if constraints.earliest is not None:
            bits.append(f"{_fmt(constraints.earliest)}以降")
        if constraints.latest is not None:
            bits.append(f"{_fmt(constraints.latest)}まで")
        bits.append(f"{_fmt(self._settings.work_start)}〜{_fmt(self._settings.work_end)}")
        if self._settings.lunch:
            bits.append("昼休みを除く")
        return "、".join(bits)

    @staticmethod
    def _register_hint(is_admin: bool, count: int = 2) -> str:
        if not is_admin:
            return "この中で都合の良い時間を、参加される方と決めてください。"
        if count <= 1:
            return "この時間でよければ「登録して」とお知らせください。件名や場所も一緒に書いていただければ、そのまま反映します。"
        return "登録するときは「1番で登録して」のように番号でお知らせください。件名や場所も一緒に書いていただければ、そのまま反映します。"

    def _proposal_reply(
        self, fields: dict, participants: list[Member], constraints: Constraints,
        candidates: list[Candidate], notes: list[str], is_admin: bool,
        explanation: list[str] | None = None, alternatives: bool = False,
    ) -> str:
        names = names_of(participants)
        together = "全員が揃う" if len(participants) > 1 else "続けて空く"
        lines = [self._lead(fields, participants)]
        if alternatives or not candidates:
            lines.append(
                f"{names}の{constraints.span_label()}は、{together}{constraints.duration}分の時間がありませんでした。"
            )
            for line in explanation or []:
                lines.append(f"・{line}")
            if explanation:
                lines.append(f"（予定の前後{self._settings.buffer}分を空けて見ています）")
        if candidates:
            if alternatives:
                lines.append("")
                lines.append("近い日でしたら、次が空いています。")
            else:
                lines.append(f"{names}の空きを{constraints.span_label()}で見ました（{self._conditions(constraints)}）。")
                lines.append("")
            for index, candidate in enumerate(candidates, 1):
                lines.append(f"{index}. {candidate.label()}")
            lines.append("")
            lines.append(self._register_hint(is_admin, len(candidates)))
        else:
            lines.append("期間を広げるか、短い時間でよければもう一度お知らせください。")
        for note in notes:
            lines.append(f"※{note}。")
        return "\n".join(lines)

    def _free_reply(
        self, fields: dict, participants: list[Member], start: datetime, end: datetime,
        notes: list[str], is_admin: bool,
    ) -> str:
        lines = [self._lead(fields, participants)]
        when = f"{jp_date(start.date())}{start:%H:%M}〜{end:%H:%M}"
        if len(participants) > 1:
            lines.append(f"{when}は、{names_of(participants)}とも空いています。")
        else:
            lines.append(f"{when}は空いています。")
        if is_admin:
            lines.append(self._register_hint(True, 1))
        for note in notes:
            lines.append(f"※{note}。")
        return "\n".join(lines)

    def _conflict_reply(
        self, fields: dict, participants: list[Member], start: datetime, end: datetime,
        found: list[str], candidates: list[Candidate], notes: list[str], is_admin: bool,
    ) -> str:
        lines = [self._lead(fields, participants)]
        lines.append(f"{jp_date(start.date())}{start:%H:%M}〜{end:%H:%M}は、次の理由で重なります。")
        for line in found:
            lines.append(f"・{line}")
        if candidates:
            lines.append("")
            together = "全員が空いている" if len(participants) > 1 else "空いている"
            lines.append(f"代わりに、{together}時間はこちらです。")
            for index, candidate in enumerate(candidates, 1):
                lines.append(f"{index}. {candidate.label()}")
            lines.append("")
            if is_admin:
                lines.append(self._register_hint(True, len(candidates)))
        else:
            lines.append("近い日で空いている枠は見つかりませんでした。期間を広げてもう一度お知らせください。")
        for note in notes:
            lines.append(f"※{note}。")
        return "\n".join(lines)

    def _load(self) -> dict:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            print("[warn] 日程候補を保存できませんでした", flush=True)


def _parse_date(text: Any) -> date | None:
    try:
        return date.fromisoformat(str(text).strip())
    except (TypeError, ValueError):
        return None


def _parse_fixed(text: Any) -> datetime | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=JST)
        except ValueError:
            continue
    return None
