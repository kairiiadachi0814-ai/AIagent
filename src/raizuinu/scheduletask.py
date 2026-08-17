"""チャットからの予定の照会と登録。

- 照会: 「足立さんの今日の予定は？」→ 部署の誰でも聞ける
- 登録: 「明日14時から南都銀行と面談」→ 管理者本人だけが行える
- 取り消し: 「さっきの予定取り消して」→ 直前に登録した予定を消す

登録は外部カレンダー（Googleカレンダー）への書き込みになるため、
要件定義書 Phase 2 の「実行前に必ず人間の承認を挟む」を次で担保する。
1. 登録できるのは設定で指定した管理者のアカウントだけ（ルーム制限とは別に効かせる）
2. 登録した内容を必ず復唱する（日時・件名・場所をそのまま返す）
3. 直前の1件は「取り消して」で消せる
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any

from .config import Config
from .schedule import (
    JST,
    Event,
    ScheduleError,
    build_sources,
    build_writer,
    collect,
    day_range,
    format_answer,
)

_SCHEDULE_NOUN_RE = re.compile(r"(予定|スケジュール|アポ|面談|来客|出張|空いて|在席|不在)")
_QUERY_RE = re.compile(
    r"(教えて|何[かしてすで]|どうな|入って|ある[？?]|ありま|でしょうか|ですか|"
    r"確認したい|知りたい|空いて|いつ|[？?])"
)
_REGISTER_RE = re.compile(r"(登録|入れて|入れといて|追加|押さえ|ブロック|控えて|予定して)")
_CANCEL_RE = re.compile(r"(取り消|取消|キャンセル|消して|削除|やっぱり無し|やっぱりなし)")
# 「予定」と言わずに様子を尋ねる聞き方（「足立さん今日は何してますか？」）。
# 本人の名前が入っているときだけ予定の照会として扱う
# 「予定の登録手順を教えて」のような、やり方を尋ねる質問はQ&Aで答える
_HOWTO_RE = re.compile(r"(手順|やり方|方法|書き方|どうやって|使い方|登録の仕方)")
_WHEREABOUTS_RE = re.compile(
    r"(今|いま|本日|今日|明日|午前|午後).{0,8}(何(を|か)?(して|してる|してます|されて|なさ)|"
    r"どこ|外出|社内|在席|席に|捕ま|つかま)"
)
# 「明日」「来週」などの相対表現。抽出はモデルに任せるが、判定の手がかりに使う
_WHEN_RE = re.compile(
    r"(今日|本日|明日|あした|明後日|あさって|来週|再来週|今週|\d{1,2}\s*[/月]\s*\d{1,2}|"
    r"月曜|火曜|水曜|木曜|金曜|土曜|日曜)"
)

NOT_ADMIN_MESSAGE = (
    "すみません、予定の登録は管理者の方からの依頼だけ受け付けています。"
    "予定の確認でしたらどなたでもお答えできますので、お気軽にどうぞ。"
)
NO_WRITER_MESSAGE = (
    "すみません、予定を登録する先のカレンダーがまだ設定されていません。"
    "管理者にご連絡いただけますか。"
)
NO_SOURCE_MESSAGE = (
    "すみません、予定を読みに行く先がまだ設定されていません。"
    "管理者にご連絡いただけますか。"
)


def looks_like_schedule_request(question: str, owner_name: str = "") -> bool:
    """予定に関する依頼・質問か（照会・登録・取り消しのいずれか）。

    「予定」という語が無くても、本人の名前を挙げて様子を尋ねる聞き方
    （「足立さん今日は何してますか？」）は予定の照会として扱う。
    """
    if _HOWTO_RE.search(question):
        return False
    if _SCHEDULE_NOUN_RE.search(question) and (
        _QUERY_RE.search(question) or _REGISTER_RE.search(question) or _CANCEL_RE.search(question)
    ):
        return True
    return bool(owner_name and owner_name in question and _WHEREABOUTS_RE.search(question))


def is_register_request(question: str) -> bool:
    """登録の依頼か（照会と区別する）。"""
    if _CANCEL_RE.search(question):
        return False
    if not _REGISTER_RE.search(question):
        return False
    # 「予定を登録する手順を教えて」のような質問は登録依頼ではない
    return not re.search(r"(手順|方法|やり方|書き方|どうやって)", question)


def is_cancel_request(question: str) -> bool:
    return bool(_CANCEL_RE.search(question)) and bool(_SCHEDULE_NOUN_RE.search(question))


_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "予定の件名。依頼文の言葉をそのまま使う"},
                    "date": {"type": "string", "description": "YYYY-MM-DD。todayを基準に解決する"},
                    "start_time": {"type": "string", "description": "HH:MM。終日なら空文字"},
                    "end_time": {"type": "string", "description": "HH:MM。指定が無ければ空文字"},
                    "all_day": {"type": "boolean"},
                    "location": {"type": "string", "description": "場所。書かれていなければ空文字"},
                },
                "required": ["summary", "date", "start_time", "end_time", "all_day", "location"],
                "additionalProperties": False,
            },
        },
        "missing": {
            "type": "array",
            "items": {"type": "string"},
            "description": "登録に足りない項目。例: 日付 / 件名",
        },
        "opening": {"type": "string", "description": "依頼を受けた側の一言。1文"},
    },
    "required": ["events", "missing", "opening"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """あなたは株式会社ライズクリエイション経理財務部のアシスタント「{agent_name}」です。
依頼文から、カレンダーに登録する予定を抜き出します。

厳守すること:
- 依頼文に書かれていることだけを使う。件名・場所・時刻を推測で作らない
- 日付が分からない、件名が分からないときは events を空にして missing に書く
- 「明日」「来週火曜」などは today と曜日を基準に YYYY-MM-DD へ直す
- 終了時刻の指定が無ければ end_time は空文字にする（こちらで1時間を仮置きする）
- 時刻の指定が無く「終日」「1日」の意味なら all_day を true にする
- 件名は依頼文の言い方をそのまま使う（言い換えない）
- opening は、依頼を受けて登録した側の一言にする。「よろしくお願いします」のような
  依頼する側の言い方は使わない
"""


class ScheduleRunner:
    """予定の照会・登録・取り消しを行う。"""

    def __init__(self, config: Config, client: Any | None = None) -> None:
        self._config = config
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client

    # --- 公開API ---

    def answer(self, question: str) -> tuple[str, dict[str, Any], dict[str, int]]:
        """予定の照会に答える（APIは使わない。日付の解釈だけコード側で行う）。"""
        cfg = self._config.schedule
        sources = build_sources(self._config)
        if not sources:
            return NO_SOURCE_MESSAGE, {"error": "no_source"}, {}

        target, span = self._target_days(question)
        start = datetime.combine(target, time.min, tzinfo=JST)
        end = start + timedelta(days=span)
        schedule = collect(sources, start, end)
        meta = {
            "target": target.isoformat(),
            "span_days": span,
            "events": len(schedule.events),
            "failed_sources": schedule.failed_sources,
        }
        owner = str(cfg.get("owner_name", "")) or "担当者"
        return format_answer(schedule, target, owner, span), meta, {}

    def register(self, question: str) -> tuple[str, dict[str, Any], dict[str, int]]:
        """予定を登録する（Googleカレンダーへ書き、内容を復唱する）。"""
        writer = build_writer(self._config)
        if writer is None:
            return NO_WRITER_MESSAGE, {"error": "no_writer"}, {}

        fields, usage = self._extract(question)
        events = self._to_events(fields)
        missing = [str(m) for m in fields.get("missing") or []]
        if not events:
            if not missing:
                missing = ["日付", "件名"]
            meta = {"error": "missing_fields", "missing": missing}
            lead = str(fields.get("opening") or "").strip() or "予定の登録ですね。"
            return (
                f"{lead}\n\nこれだけ教えていただけますか。\n"
                + "\n".join(f"・{m}" for m in missing),
                meta,
                usage,
            )

        registered: list[dict[str, str]] = []
        for event in events:
            try:
                event_id = writer.insert_event(event)
            except ScheduleError as exc:
                return (
                    f"すみません、予定の登録でつまずきました。（{exc}）",
                    {"error": "insert_failed", "detail": str(exc)},
                    usage,
                )
            registered.append({"id": event_id, "summary": event.summary})

        meta = {
            "registered": registered,
            "events": [self._describe(e) for e in events],
        }
        return self._registered_reply(fields, events), meta, usage

    def cancel(self, last: list[dict[str, str]]) -> tuple[str, dict[str, Any], dict[str, int]]:
        """直前に登録した予定を取り消す。"""
        writer = build_writer(self._config)
        if writer is None or not last:
            return (
                "すみません、取り消せる予定が見当たりませんでした。"
                "カレンダーから直接ご確認いただけますか。",
                {"error": "nothing_to_cancel"},
                {},
            )
        removed = []
        for entry in last:
            try:
                writer.delete_event(entry["id"])
                removed.append(entry.get("summary", ""))
            except ScheduleError as exc:
                return (
                    f"すみません、取り消しに失敗しました。（{exc}）",
                    {"error": "delete_failed"},
                    {},
                )
        names = "」「".join(n for n in removed if n)
        return f"「{names}」を取り消しました。", {"cancelled": removed}, {}

    # --- 内部 ---

    def _target_days(self, question: str) -> tuple[date, int]:
        """依頼文から対象の日と日数を決める（既定は今日1日）。"""
        today = datetime.now(JST).date()
        if re.search(r"(明日|あした)", question):
            return today + timedelta(days=1), 1
        if re.search(r"(明後日|あさって)", question):
            return today + timedelta(days=2), 1
        if re.search(r"今週", question):
            return today, 7 - today.weekday()
        if re.search(r"来週", question):
            monday = today + timedelta(days=7 - today.weekday())
            return monday, 7
        match = re.search(r"(\d{1,2})\s*[/月]\s*(\d{1,2})", question)
        if match:
            month, day = int(match.group(1)), int(match.group(2))
            year = today.year + (1 if month < today.month - 6 else 0)
            try:
                return date(year, month, day), 1
            except ValueError:
                pass
        return today, 1

    def _extract(self, question: str) -> tuple[dict[str, Any], dict[str, int]]:
        from .answer import _call_with_continuation

        cfg = self._config
        now = datetime.now(JST)
        weekday = "月火水木金土日"[now.weekday()]
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": int(cfg.max_tokens),
            "output_config": {
                "effort": cfg.effort,
                "format": {"type": "json_schema", "schema": _EXTRACT_SCHEMA},
            },
            "system": SYSTEM_PROMPT.format(agent_name=cfg.agent_name),
        }
        prompt = (
            f"today: {now:%Y-%m-%d}（{weekday}曜日）\n\n"
            f"===依頼ここから===\n{question}\n===依頼ここまで==="
        )
        response, usage, _ = _call_with_continuation(
            self._client.messages.create, kwargs, [{"role": "user", "content": prompt}]
        )
        text = next(
            (b.text for b in getattr(response, "content", []) if getattr(b, "type", "") == "text"),
            "",
        )
        try:
            return json.loads(text), usage
        except (json.JSONDecodeError, TypeError):
            return {"events": [], "missing": ["日付", "件名"], "opening": ""}, usage

    @staticmethod
    def _to_events(fields: dict[str, Any]) -> list[Event]:
        events: list[Event] = []
        for item in fields.get("events") or []:
            summary = str(item.get("summary", "")).strip()
            day_text = str(item.get("date", "")).strip()
            if not summary or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_text):
                continue
            day = date.fromisoformat(day_text)
            if item.get("all_day") or not str(item.get("start_time", "")).strip():
                start = datetime.combine(day, time.min, tzinfo=JST)
                events.append(
                    Event(summary=summary, start=start, end=start + timedelta(days=1),
                          all_day=True, location=str(item.get("location", "")).strip())
                )
                continue
            start = _combine(day, str(item["start_time"]))
            end_text = str(item.get("end_time", "")).strip()
            end = _combine(day, end_text) if end_text else start + timedelta(hours=1)
            if end <= start:  # 「22時から1時まで」のような日跨ぎ
                end += timedelta(days=1)
            events.append(
                Event(summary=summary, start=start, end=end,
                      location=str(item.get("location", "")).strip())
            )
        return events

    @staticmethod
    def _describe(event: Event) -> str:
        day = event.start.astimezone(JST)
        weekday = "月火水木金土日"[day.weekday()]
        location = f"　＠{event.location}" if event.location else ""
        return (
            f"{day.month}月{day.day}日（{weekday}） {event.time_label()}　"
            f"{event.summary}{location}"
        )

    def _registered_reply(self, fields: dict[str, Any], events: list[Event]) -> str:
        lead = str(fields.get("opening") or "").strip()
        if not lead or re.match(r"^([^。\n]*よろしくお願い|お願いし)", lead):
            lead = "承知しました。次の予定で登録しました。"
        lines = [lead, ""]
        for event in events:
            day = event.start.astimezone(JST)
            weekday = "月火水木金土日"[day.weekday()]
            location = f"　＠{event.location}" if event.location else ""
            lines.append(
                f"・{day.month}月{day.day}日（{weekday}） {event.time_label()}　"
                f"{event.summary}{location}"
            )
        lines.append("")
        lines.append("違っていたら「さっきの予定を取り消して」とお知らせください。")
        return "\n".join(lines)


def _combine(day: date, hhmm: str) -> datetime:
    match = re.match(r"(\d{1,2})\s*[:：]?\s*(\d{2})?", hhmm.strip())
    hour = int(match.group(1)) if match else 0
    minute = int(match.group(2) or 0) if match else 0
    return datetime.combine(day, time(hour % 24, minute), tzinfo=JST)
