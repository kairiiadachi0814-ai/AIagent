"""予定の取得と登録（トヨクモ スケジューラー ＋ Googleカレンダー）。

トヨクモ スケジューラーには、外部プログラムから予定を登録する手段が無い。
公開APIは提供されておらず、kintone同期も予定についてはスケジューラー→kintoneの
単方向で、kintone側に書いてもスケジューラーには戻らない。読み取りだけは
iCalendar出力URLでできる。

そこで役割を分ける。

- 読む: トヨクモのiCal出力とGoogleカレンダーの両方を読み、重複を除いて束ねる
- 書く: Googleカレンダーへ登録する。トヨクモ側の「他のカレンダーから読込」で
  そのGoogleカレンダーを取り込んでおけば、スケジューラーの画面にも現れる

秘密情報の扱い:
- iCalのURLは認証が無く、URLを知る者は本人の全予定を読めてしまう。
  APIトークンと同格に扱い、環境変数のみで持ち、ログや返信に出さない
- Googleのサービスアカウント鍵はVPS上のファイルに置き、パスだけを環境変数で渡す
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable

JST = timezone(timedelta(hours=9))
GOOGLE_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar.events"


class ScheduleError(Exception):
    """予定の取得・登録に失敗した（利用者向けの説明文を持つ）。"""


@dataclass
class Event:
    """1件の予定。時刻はすべてJSTで扱う。"""

    summary: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str = ""
    source: str = ""

    def key(self) -> tuple[str, str, str]:
        """重複判定のキー。2つのカレンダーに同じ予定が出るため使う。"""
        return (
            re.sub(r"\s+", "", self.summary),
            self.start.astimezone(JST).isoformat(),
            self.end.astimezone(JST).isoformat(),
        )

    def time_label(self) -> str:
        if self.all_day:
            return "終日"
        start = self.start.astimezone(JST)
        end = self.end.astimezone(JST)
        return f"{start:%H:%M}〜{end:%H:%M}"


def day_range(target: date) -> tuple[datetime, datetime]:
    """その日の00:00〜翌日00:00（JST）。"""
    start = datetime.combine(target, time.min, tzinfo=JST)
    return start, start + timedelta(days=1)


# --- 読み取り: iCalendar（トヨクモ スケジューラー等） ---


def _as_jst(value: Any) -> tuple[datetime, bool]:
    """iCalendarの日時を (JSTのdatetime, 終日か) にそろえる。"""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=JST)
        return value.astimezone(JST), False
    # date型は終日予定
    return datetime.combine(value, time.min, tzinfo=JST), True


def parse_ics(data: bytes, start: datetime, end: datetime, source: str = "") -> list[Event]:
    """ICSを読み、期間内の予定を返す（繰り返し予定は個別の回に展開する）。"""
    import icalendar
    import recurring_ical_events

    try:
        calendar = icalendar.Calendar.from_ical(data)
    except Exception as exc:  # 壊れたICS・HTMLが返ってきた等
        raise ScheduleError("カレンダーの読み取りに失敗しました（形式が不正です）") from exc

    events: list[Event] = []
    for item in recurring_ical_events.of(calendar).between(start, end):
        started, all_day = _as_jst(item.get("DTSTART").dt)
        raw_end = item.get("DTEND")
        if raw_end is not None:
            ended, _ = _as_jst(raw_end.dt)
        else:
            ended = started + timedelta(days=1 if all_day else 1 / 24)
        events.append(
            Event(
                summary=str(item.get("SUMMARY", "")).strip() or "（件名なし）",
                start=started,
                end=ended,
                all_day=all_day,
                location=str(item.get("LOCATION", "")).strip(),
                source=source,
            )
        )
    return events


class IcsSource:
    """iCalendarのURLを読むだけの予定ソース（書き込みはできない）。"""

    def __init__(self, url: str, label: str, http_get: Callable[..., Any] | None = None) -> None:
        self._url = url
        self.label = label
        self._http_get = http_get or _default_http_get

    def list_events(self, start: datetime, end: datetime) -> list[Event]:
        status, data = self._http_get(self._url)
        if status != 200:
            # URLは秘密情報なのでメッセージに含めない
            raise ScheduleError(f"{self.label}のカレンダーを取得できませんでした（HTTP {status}）")
        return parse_ics(data, start, end, source=self.label)


def _default_http_get(url: str, timeout: int = 30) -> tuple[int, bytes]:
    import requests

    resp = requests.get(url, timeout=timeout)
    return resp.status_code, resp.content


# --- 読み書き: Googleカレンダー ---


class GoogleCalendarSource:
    """Googleカレンダー。読み取りと登録の両方ができる唯一の経路。"""

    def __init__(
        self,
        credentials_path: str,
        calendar_id: str,
        label: str = "Googleカレンダー",
        request: Callable[..., Any] | None = None,
    ) -> None:
        self._credentials_path = credentials_path
        self._calendar_id = calendar_id
        self.label = label
        self._request = request or self._authorized_request

    # -- 内部 --

    def _authorized_request(
        self, method: str, url: str, params: dict | None = None, body: dict | None = None
    ) -> tuple[int, dict]:
        import requests
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            self._credentials_path, scopes=[GOOGLE_SCOPE]
        )
        credentials.refresh(Request())
        resp = requests.request(
            method,
            url,
            params=params,
            json=body,
            headers={"Authorization": f"Bearer {credentials.token}"},
            timeout=30,
        )
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        return resp.status_code, payload

    def _events_url(self) -> str:
        from urllib.parse import quote

        return f"{GOOGLE_API}/calendars/{quote(self._calendar_id, safe='')}/events"

    # -- 公開API --

    def list_events(self, start: datetime, end: datetime) -> list[Event]:
        status, payload = self._request(
            "GET",
            self._events_url(),
            params={
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                # 繰り返し予定を個別の回に展開して返させる
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": 250,
            },
        )
        if status != 200:
            raise ScheduleError(
                f"Googleカレンダーを取得できませんでした（HTTP {status} {payload.get('error', {}).get('message', '')}）"
            )
        events = []
        for item in payload.get("items", []):
            if item.get("status") == "cancelled":
                continue
            started, all_day = _parse_google_time(item.get("start") or {})
            ended, _ = _parse_google_time(item.get("end") or {})
            events.append(
                Event(
                    summary=str(item.get("summary", "")).strip() or "（件名なし）",
                    start=started,
                    end=ended,
                    all_day=all_day,
                    location=str(item.get("location", "")).strip(),
                    source=self.label,
                )
            )
        return events

    def insert_event(self, event: Event) -> str:
        """予定を登録し、GoogleカレンダーのイベントIDを返す。"""
        if event.all_day:
            body = {
                "start": {"date": event.start.astimezone(JST).strftime("%Y-%m-%d")},
                "end": {"date": event.end.astimezone(JST).strftime("%Y-%m-%d")},
            }
        else:
            body = {
                "start": {"dateTime": event.start.astimezone(JST).isoformat(), "timeZone": "Asia/Tokyo"},
                "end": {"dateTime": event.end.astimezone(JST).isoformat(), "timeZone": "Asia/Tokyo"},
            }
        body["summary"] = event.summary
        if event.location:
            body["location"] = event.location
        # 参加者は付けない（サービスアカウントは招待を送れないため）
        status, payload = self._request(
            "POST", self._events_url(), params={"sendUpdates": "none"}, body=body
        )
        if status not in (200, 201):
            raise ScheduleError(
                f"予定を登録できませんでした（HTTP {status} {payload.get('error', {}).get('message', '')}）"
            )
        return str(payload.get("id", ""))

    def delete_event(self, event_id: str) -> None:
        """登録した予定を消す（取り消し用）。"""
        from urllib.parse import quote

        status, payload = self._request(
            "DELETE", f"{self._events_url()}/{quote(event_id, safe='')}"
        )
        # 既に消えている場合（410 Gone / 404）は成功として扱う
        if status not in (200, 204, 404, 410):
            raise ScheduleError(
                f"予定を取り消せませんでした（HTTP {status} {payload.get('error', {}).get('message', '')}）"
            )


def _parse_google_time(node: dict) -> tuple[datetime, bool]:
    if node.get("date"):  # 終日
        value = datetime.strptime(node["date"], "%Y-%m-%d")
        return value.replace(tzinfo=JST), True
    text = str(node.get("dateTime", ""))
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=JST)
    return value.astimezone(JST), False


# --- 束ねる ---


@dataclass
class Schedule:
    events: list[Event] = field(default_factory=list)
    failed_sources: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed_sources


def collect(sources: Iterable[Any], start: datetime, end: datetime) -> Schedule:
    """複数のカレンダーから集めて、重複を除いて時刻順に並べる。

    トヨクモがGoogleカレンダーを取り込んでいる場合、同じ予定が両方から
    返りうる。件名と時刻が一致するものは1件にまとめる。
    1つのカレンダーが落ちても、取れたぶんは返す（無言で欠けさせない）。
    """
    schedule = Schedule()
    seen: dict[tuple[str, str, str], Event] = {}
    for source in sources:
        try:
            found = source.list_events(start, end)
        except ScheduleError:
            schedule.failed_sources.append(getattr(source, "label", "カレンダー"))
            continue
        for event in found:
            seen.setdefault(event.key(), event)
    schedule.events = sorted(seen.values(), key=lambda e: (not e.all_day, e.start, e.summary))
    return schedule


# --- Chatwork向けの整形 ---

_WEEKDAYS = "月火水木金土日"


def _line(event: Event, mark_source: str = "", mark_note: str = "") -> str:
    """予定1件の表示行。指定した出どころのものには印を付ける。

    トヨクモ スケジューラーは外部から予定を登録できず、Googleカレンダーからの
    取り込みも一度きりで自動更新されない。そのためGoogleにしか無い予定は
    トヨクモの画面に出ない。黙って食い違わせないよう印で示す。
    """
    location = f"　＠{event.location}" if event.location else ""
    note = mark_note if (mark_source and event.source == mark_source) else ""
    return f"・{event.time_label()}　{event.summary}{location}{note}"


def format_day(
    schedule: Schedule,
    target: date,
    owner: str,
    mark_source: str = "",
    mark_note: str = "",
) -> str:
    """その日1日の予定をChatworkの本文にする。"""
    heading = f"{target.year}年{target.month}月{target.day}日（{_WEEKDAYS[target.weekday()]}）"
    lines = [f"[info][title]{owner}さん 本日の予定　{heading}[/title]"]
    if not schedule.events:
        lines.append("登録されている予定はありません。")
    for event in schedule.events:
        lines.append(_line(event, mark_source, mark_note))
    if schedule.failed_sources:
        lines.append("")
        lines.append(
            "※" + "、".join(schedule.failed_sources) + "を読み取れませんでした。"
            "抜けている予定があるかもしれません。"
        )
    lines.append("[/info]")
    return "\n".join(lines)


def format_answer(
    schedule: Schedule,
    target: date,
    owner: str,
    span_days: int = 1,
    mark_source: str = "",
    mark_note: str = "",
) -> str:
    """メンバーからの照会への返答（枠で囲まず会話として返す）。"""
    if span_days > 1:
        heading = f"{target.month}月{target.day}日からの{span_days}日間"
    else:
        heading = f"{target.month}月{target.day}日（{_WEEKDAYS[target.weekday()]}）"
    if not schedule.events:
        return f"{owner}さんの{heading}の予定は、いまのところ登録がありません。"

    lines = [f"{owner}さんの{heading}の予定です。"]
    current = None
    for event in schedule.events:
        day = event.start.astimezone(JST).date()
        if span_days > 1 and day != current:
            current = day
            lines.append(f"■{day.month}月{day.day}日（{_WEEKDAYS[day.weekday()]}）")
        lines.append(_line(event, mark_source, mark_note))
    if schedule.failed_sources:
        lines.append(
            "※" + "、".join(schedule.failed_sources) + "を読み取れなかったため、"
            "抜けている予定があるかもしれません。"
        )
    return "\n".join(lines)


# --- 設定からソースを組み立てる ---


def build_sources(config: Any, ics_urls: list[str] | None = None) -> list[Any]:
    """設定と環境変数から、読み取り対象のカレンダーを組み立てる。

    URLと鍵は秘密情報なので config.json ではなく環境変数から取る。
    """
    import os

    cfg = config.schedule
    sources: list[Any] = []
    urls = ics_urls if ics_urls is not None else _env_ics_urls()
    for index, url in enumerate(urls):
        label = cfg.get("ics_labels", ["トヨクモ スケジューラー"])[
            min(index, len(cfg.get("ics_labels", ["トヨクモ スケジューラー"])) - 1)
        ]
        sources.append(IcsSource(url, label))
    credentials = os.environ.get("GOOGLE_CALENDAR_CREDENTIALS_PATH")
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    if credentials and calendar_id:
        sources.append(GoogleCalendarSource(credentials, calendar_id))
    return sources


def build_writer(config: Any) -> GoogleCalendarSource | None:
    """予定を登録できるカレンダー（Googleカレンダーのみ）。"""
    import os

    credentials = os.environ.get("GOOGLE_CALENDAR_CREDENTIALS_PATH")
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    if not credentials or not calendar_id:
        return None
    return GoogleCalendarSource(credentials, calendar_id)


def _env_ics_urls() -> list[str]:
    """SCHEDULE_ICS_URLS はカンマ区切り（URL自体が秘密のため環境変数のみ）。"""
    import os

    raw = os.environ.get("SCHEDULE_ICS_URLS") or os.environ.get("SCHEDULE_ICS_URL") or ""
    return [u.strip() for u in raw.split(",") if u.strip()]
