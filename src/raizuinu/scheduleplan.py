"""空き時間の提案と、複数人の予定調整。

「篠田さんと来週1時間打合せしたい。いつがいい？」のように聞かれたら、
関係者それぞれの予定と勤務日・勤務時間を突き合わせ、全員が空いている
時間帯を候補として返す。管理者なら「1番で登録して」でそのまま登録できる。
「来週火曜14時から篠田さんと打合せを入れて」のように日時を決めて頼まれた
ときは、登録の前に相手の予定を確かめ、重なっていれば止めて代わりを出す。

方針:
- 他の人の予定は、その人のトヨクモ iCal（環境変数 SCHEDULE_ICS_URL_<account_id>）
  か、サービスアカウントに共有されたGoogleカレンダー（members[].google_calendar_id）
  から読む。読めない人は勤務日・勤務時間だけで見て、その旨を返信に書く
- 候補の日時はコードで決める。モデルに任せるのは依頼文の読み取りだけで、
  時刻を作文させない
- 既存の予定を動かす提案はしない（人の予定を勝手に動かさない）
- 登録は既存の登録フローと同じく管理者だけ。登録先は管理者のGoogleカレンダー
"""

from __future__ import annotations

import json
import re
import traceback
import unicodedata
from dataclasses import dataclass, field
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


def looks_like_proposal_request(question: str) -> bool:
    """空き時間の提案・日程調整の依頼か。"""
    if _HOWTO_RE.search(question):
        return False
    return bool(_MEETING_RE.search(question) and _PROPOSE_CUE_RE.search(question))


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
    all_day_blocks: bool = True
    lead_minutes: int = 60
    grid: int = 30
    remember_minutes: int = 1440


def load_settings(config: Any) -> Settings:
    raw = config.schedule.get("proposal") or {}
    hours = raw.get("work_hours") or {}
    lunch_raw = raw.get("lunch")
    lunch = None
    if lunch_raw and lunch_raw.get("start") and lunch_raw.get("end"):
        lunch = (_hm(lunch_raw["start"], 12 * 60), _hm(lunch_raw["end"], 13 * 60))
    elif lunch_raw is None:
        lunch = (12 * 60, 13 * 60)
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
        all_day_blocks=bool(raw.get("all_day_blocks", True)),
        lead_minutes=int(raw.get("lead_minutes", 60)),
        grid=int(raw.get("grid_minutes", 30)),
        remember_minutes=int(raw.get("remember_minutes", 1440)),
    )


def common_window(day: date, members: list[Member], settings: Settings, holidays: set[str]) -> tuple[int, int] | None:
    """その日に全員が揃える時間帯（分）。誰かの休みなら None。"""
    if day.weekday() >= 5 or (settings.skip_holidays and day.isoformat() in holidays):
        return None
    start, end = settings.work_start, settings.work_end
    for member in members:
        if not member.works_on(day, holidays):
            return None
        if member.work_start is not None:
            start = max(start, member.work_start)
        if member.work_end is not None:
            end = min(end, member.work_end)
    return (start, end) if end > start else None


def busy_of(events: list[Event], day: date, buffer: int, all_day_blocks: bool) -> list[tuple[int, int]]:
    """その日の埋まっている時間帯（分）。前後に余白を足す。終日予定は1日ふさぐ。"""
    day_start = datetime.combine(day, time.min, tzinfo=JST)
    day_end = day_start + timedelta(days=1)
    out: list[tuple[int, int]] = []
    for event in events:
        start, end = event.start.astimezone(JST), event.end.astimezone(JST)
        if end <= day_start or start >= day_end:
            continue
        if event.all_day:
            if all_day_blocks:
                out.append((0, 24 * 60))
            continue
        begin = max(0, int((start - day_start).total_seconds() // 60) - buffer)
        finish = min(24 * 60, int((end - day_start).total_seconds() // 60) + buffer)
        out.append((begin, finish))
    return out


def free_slots(
    window: tuple[int, int],
    busy: list[tuple[int, int]],
    duration: int,
    lunch: tuple[int, int] | None,
    grid: int,
    not_before: int = 0,
) -> list[tuple[int, int]]:
    """時間帯の中で、埋まっていない duration 分の枠（grid 分刻み）。"""
    blocks = list(busy) + ([lunch] if lunch else [])
    slots: list[tuple[int, int]] = []
    at = ((window[0] + grid - 1) // grid) * grid
    while at + duration <= window[1]:
        if at >= not_before and not any(s < at + duration and e > at for s, e in blocks):
            slots.append((at, at + duration))
        at += grid
    return slots


def pick_for_day(slots: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """1日の枠から、勧めやすい順に最大2つ。正時始まりを優先し、2つ目は離れた時間帯。"""
    if not slots:
        return []
    on_hour = [s for s in slots if s[0] % 60 == 0]
    first = (on_hour or slots)[0]
    picks = [first]
    later = [s for s in (on_hour or slots) if s[0] >= first[1] + 120]
    if later:
        picks.append(later[0])
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
        window = common_window(day, members, settings, holidays)
        if window is not None:
            start, end = window
            if constraints.time_of_day == "am" and settings.lunch:
                end = min(end, settings.lunch[0])
            elif constraints.time_of_day == "pm" and settings.lunch:
                start = max(start, settings.lunch[1])
            if constraints.earliest is not None:
                start = max(start, constraints.earliest)
            if constraints.latest is not None:
                end = min(end, constraints.latest)
            busy: list[tuple[int, int]] = []
            for member in members:
                busy += busy_of(events_by_member.get(member.name, []), day, settings.buffer, settings.all_day_blocks)
            not_before = 0
            if day == now.date():
                lead = now + timedelta(minutes=settings.lead_minutes)
                not_before = lead.hour * 60 + lead.minute
            elif day < now.date():
                not_before = 24 * 60
            if end > start:
                slots = free_slots((start, end), busy, constraints.duration, settings.lunch, settings.grid, not_before)
                picks = pick_for_day(slots)[: max(1, settings.per_day)]
                for index, (s, e) in enumerate(picks):
                    candidate = Candidate(
                        start=datetime.combine(day, time(s // 60, s % 60), tzinfo=JST),
                        end=datetime.combine(day, time(e // 60, e % 60), tzinfo=JST),
                    )
                    (firsts if index == 0 else seconds).append(candidate)
        day += timedelta(days=1)
    chosen = (firsts + seconds)[: settings.max_candidates]
    return sorted(chosen, key=lambda c: c.start)


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
- 「来週」「今週中」「明日以降」は today と曜日を基準に YYYY-MM-DD へ直す
- 社内メンバーの名前は次の一覧にある苗字で書く: {members}
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

    def propose(self, question: str, requester_id: int = 0, is_admin: bool = False) -> tuple[str, dict, dict]:
        """候補を出す。日時が決め打ちなら、その時間の空きを確かめる。"""
        members = load_members(self._config)
        fields, usage = self._extract(question, members)
        participants = self._participants(question, fields, members, requester_id)
        constraints = self._constraints(fields)
        events, notes, failed = self._gather(participants, constraints)
        summary = str(fields.get("summary") or "").strip() or self._default_summary(participants, requester_id)

        fixed = _parse_fixed(fields.get("fixed_start"))
        if fixed is not None:
            end = fixed + timedelta(minutes=constraints.duration)
            found = conflicts_at(fixed, end, participants, events, self._settings, self._holidays)
            if not found:
                text = self._free_reply(fields, participants, fixed, end, notes, is_admin)
                candidates = [Candidate(fixed, end)]
            else:
                candidates = find_candidates(participants, events, constraints, self._settings, self._holidays, self._now())
                text = self._conflict_reply(fields, participants, fixed, end, found, candidates, notes, is_admin)
        else:
            candidates = find_candidates(participants, events, constraints, self._settings, self._holidays, self._now())
            text = self._proposal_reply(fields, participants, constraints, candidates, notes, is_admin)

        meta = {
            "participants": [m.name for m in participants],
            "candidates": [c.as_dict() for c in candidates],
            "summary": summary,
            "duration": constraints.duration,
            "fixed": fixed.isoformat() if fixed else "",
            "conflicts": found if fixed is not None else [],
            "failed_sources": failed,
        }
        return text, meta, usage

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
                    fields = {"opening": "", "summary": event.summary}
                    text = self._conflict_reply(fields, participants, event.start, event.end, found, candidates, notes, True)
                    self._last_check = {"candidates": candidates, "summary": event.summary, "participants": participants}
                    return text
            return None

        self._last_check = {}
        return check

    def last_check(self) -> dict:
        return getattr(self, "_last_check", {}) or {}

    def register_pick(self, pending: dict, number: int, schedule_runner: Any) -> tuple[str, dict, dict]:
        """覚えている候補から番号で1つ選び、管理者のカレンダーへ登録する。"""
        candidates = pending.get("candidates") or []
        if not 1 <= number <= len(candidates):
            return (
                f"すみません、候補は{len(candidates)}件までです。番号をもう一度お知らせください。",
                {"error": "bad_number"},
                {},
            )
        chosen = candidates[number - 1]
        event = Event(
            summary=str(pending.get("summary") or "打合せ"),
            start=datetime.fromisoformat(chosen["start"]),
            end=datetime.fromisoformat(chosen["end"]),
        )
        return schedule_runner.register_events([event])

    # --- 候補を覚えておく（「1番で登録して」のため） ---

    def remember(self, room_id: int, account_id: int, ts: int, meta: dict) -> None:
        if not meta.get("candidates"):
            return
        data = self._load()
        data[f"{room_id}:{account_id}"] = {
            "ts": int(ts),
            "candidates": meta["candidates"],
            "summary": meta.get("summary", ""),
            "participants": meta.get("participants", []),
        }
        self._save(data)

    def pending(self, room_id: int, account_id: int, now_ts: int) -> dict | None:
        entry = self._load().get(f"{room_id}:{account_id}")
        if not entry:
            return None
        if now_ts - int(entry.get("ts", 0)) > self._settings.remember_minutes * 60:
            return None
        return entry

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
            date_to=date_to,
            time_of_day=str(fields.get("time_of_day") or "any"),
            earliest=_hm(fields["earliest"], 0) if fields.get("earliest") else None,
            latest=_hm(fields["latest"], 0) if fields.get("latest") else None,
        )

    def _gather(self, participants: list[Member], constraints: Constraints) -> tuple[dict[str, list[Event]], list[str], list[str]]:
        """各自の予定を読む。→ (名前→予定, 注記, 読めなかったソース)"""
        start = datetime.combine(constraints.date_from, time.min, tzinfo=JST)
        end = datetime.combine(constraints.date_to, time.min, tzinfo=JST) + timedelta(days=1)
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
            names = "・".join(m.name for m in participants)
            opening = f"{names}さんの日程ですね。"
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

    def _proposal_reply(
        self, fields: dict, participants: list[Member], constraints: Constraints,
        candidates: list[Candidate], notes: list[str], is_admin: bool,
    ) -> str:
        names = "・".join(m.name for m in participants)
        span = f"{jp_date(constraints.date_from)}〜{jp_date(constraints.date_to)}"
        lines = [self._lead(fields, participants)]
        if not candidates:
            lines.append(f"{names}さんの空きを{span}で探しましたが、全員が揃う{constraints.duration}分の枠が見つかりませんでした。")
            lines.append("期間を広げるか、短い時間でよければもう一度お知らせください。")
        else:
            lines.append(f"{names}さんの空きを{span}で見ました（{self._conditions(constraints)}）。")
            lines.append("")
            for index, candidate in enumerate(candidates, 1):
                lines.append(f"{index}. {candidate.label()}")
            lines.append("")
            if is_admin:
                lines.append("登録するときは「1番で登録して」のように番号でお知らせください。")
            else:
                lines.append("この中で都合の良い時間を、参加される方と決めてください。")
        for note in notes:
            lines.append(f"※{note}。")
        return "\n".join(lines)

    def _free_reply(
        self, fields: dict, participants: list[Member], start: datetime, end: datetime,
        notes: list[str], is_admin: bool,
    ) -> str:
        names = "・".join(m.name for m in participants)
        lines = [self._lead(fields, participants)]
        lines.append(f"{jp_date(start.date())}{start:%H:%M}〜{end:%H:%M}は、{names}さんとも空いています。")
        if is_admin:
            lines.append("この時間で登録するなら「1番で登録して」とお知らせください。")
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
            lines.append("代わりに、全員が空いている時間はこちらです。")
            for index, candidate in enumerate(candidates, 1):
                lines.append(f"{index}. {candidate.label()}")
            lines.append("")
            if is_admin:
                lines.append("登録するときは「1番で登録して」のように番号でお知らせください。")
        else:
            lines.append("近い日で全員が揃う枠は見つかりませんでした。期間を広げてもう一度お知らせください。")
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
