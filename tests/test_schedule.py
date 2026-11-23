import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from raizuinu.config import Config
from raizuinu.schedule import (
    JST,
    Event,
    IcsSource,
    Schedule,
    ScheduleError,
    collect,
    day_range,
    format_answer,
    format_day,
    parse_ics,
)
from raizuinu.scheduletask import (
    NOT_ADMIN_MESSAGE,
    ScheduleRunner,
    is_cancel_request,
    is_register_request,
    looks_like_schedule_request,
)

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//JP
BEGIN:VEVENT
UID:1
SUMMARY:南都銀行 訪問
DTSTART;TZID=Asia/Tokyo:20260818T140000
DTEND;TZID=Asia/Tokyo:20260818T150000
LOCATION:本店営業部
END:VEVENT
BEGIN:VEVENT
UID:2
SUMMARY:月次締め
DTSTART;VALUE=DATE:20260818
DTEND;VALUE=DATE:20260819
END:VEVENT
BEGIN:VEVENT
UID:3
SUMMARY:朝礼
DTSTART;TZID=Asia/Tokyo:20260817T090000
DTEND;TZID=Asia/Tokyo:20260817T091500
RRULE:FREQ=DAILY;COUNT=10
END:VEVENT
END:VCALENDAR
""".encode()


class TestIcsParsing:
    def test_events_of_the_day(self):
        start, end = day_range(date(2026, 8, 18))
        events = parse_ics(ICS, start, end, source="トヨクモ")
        assert {e.summary for e in events} == {"南都銀行 訪問", "月次締め", "朝礼"}

    def test_recurring_event_is_expanded(self):
        # 繰り返し予定は個別の回に展開されて出る
        start, end = day_range(date(2026, 8, 20))
        events = parse_ics(ICS, start, end)
        assert [e.summary for e in events] == ["朝礼"]

    def test_all_day_and_timed_are_distinguished(self):
        start, end = day_range(date(2026, 8, 18))
        events = {e.summary: e for e in parse_ics(ICS, start, end)}
        assert events["月次締め"].all_day is True
        assert events["月次締め"].time_label() == "終日"
        assert events["南都銀行 訪問"].all_day is False
        assert events["南都銀行 訪問"].time_label() == "14:00〜15:00"
        assert events["南都銀行 訪問"].location == "本店営業部"

    def test_broken_ics_is_an_error(self):
        start, end = day_range(date(2026, 8, 18))
        with pytest.raises(ScheduleError):
            parse_ics(b"<html>not a calendar</html>", start, end)


class FakeSource:
    def __init__(self, events, label="テスト", fail=False):
        self._events = events
        self.label = label
        self._fail = fail

    def list_events(self, start, end):
        if self._fail:
            raise ScheduleError("取得できません")
        return self._events


def ev(summary, hour=None, day=18, location="", source=""):
    start = datetime(2026, 8, day, hour or 0, 0, tzinfo=JST)
    return Event(
        summary=summary,
        start=start,
        end=start + timedelta(hours=1 if hour is not None else 24),
        all_day=hour is None,
        location=location,
        source=source,
    )


class TestCollect:
    def test_duplicates_across_calendars_are_merged(self):
        # トヨクモがGoogleカレンダーを取り込んでいると同じ予定が両方から返る
        a = FakeSource([ev("南都銀行 訪問", 14)], "トヨクモ")
        b = FakeSource([ev("南都銀行　訪問", 14)], "Googleカレンダー")
        result = collect([a, b], *day_range(date(2026, 8, 18)))
        assert len(result.events) == 1

    def test_sorted_all_day_first(self):
        source = FakeSource([ev("面談", 14), ev("月次締め"), ev("朝礼", 9)])
        result = collect([source], *day_range(date(2026, 8, 18)))
        assert [e.summary for e in result.events] == ["月次締め", "朝礼", "面談"]

    def test_one_broken_calendar_does_not_hide_the_rest(self):
        ok = FakeSource([ev("面談", 14)], "Googleカレンダー")
        broken = FakeSource([], "トヨクモ スケジューラー", fail=True)
        result = collect([broken, ok], *day_range(date(2026, 8, 18)))
        assert [e.summary for e in result.events] == ["面談"]
        assert result.failed_sources == ["トヨクモ スケジューラー"]
        assert result.ok is False


class TestFormatting:
    def test_morning_message(self):
        schedule = collect(
            [FakeSource([ev("南都銀行 訪問", 14, location="本店営業部"), ev("月次締め")])],
            *day_range(date(2026, 8, 18)),
        )
        body = format_day(schedule, date(2026, 8, 18), "足立")
        assert body.startswith("[info][title]足立さん 今日の予定　2026年8月18日（火）[/title]")
        assert "・終日　月次締め" in body
        assert "・14:00〜15:00　南都銀行 訪問　＠本店営業部" in body
        assert body.endswith("[/info]")

    def test_empty_day(self):
        body = format_day(Schedule(), date(2026, 8, 18), "足立")
        assert "予定は入っていません。" in body

    def test_failed_source_is_disclosed(self):
        schedule = Schedule(events=[], failed_sources=["トヨクモ スケジューラー"])
        body = format_day(schedule, date(2026, 8, 18), "足立")
        # 読めなかったことを黙らない（予定が無いのと区別できるように）
        assert "読み取れませんでした" in body

    def test_answer_is_conversational_not_boxed(self):
        schedule = collect([FakeSource([ev("面談", 14)])], *day_range(date(2026, 8, 18)))
        text = format_answer(schedule, date(2026, 8, 18), "足立")
        assert "[info]" not in text
        assert text.startswith("足立さんの8月18日（火）の予定です。")


class TestRequestDetection:
    @pytest.mark.parametrize(
        "question",
        [
            "足立さんの今日の予定教えて",
            "足立さん今日は何してますか？",
            "明日って空いてますか",
            "来週の予定どうなってる？",
        ],
    )
    def test_queries(self, question):
        assert looks_like_schedule_request(question, "足立") is True
        assert is_register_request(question) is False

    @pytest.mark.parametrize(
        "question",
        [
            "明日14時から南都銀行と面談、予定に入れといて",
            "8/20の10時に月次会議で予定を登録して",
            "来週火曜終日、出張で予定押さえて",
            "月曜10時から篠田さんと打合せを入れて",  # 「予定」と言わない頼み方
        ],
    )
    def test_registrations(self, question):
        assert looks_like_schedule_request(question, "足立") is True
        assert is_register_request(question) is True

    @pytest.mark.parametrize(
        "question",
        ["会議の議事録を作って", "打合せの資料を追加して", "来週の会議の資料はどこ？"],
    )
    def test_meeting_words_alone_are_not_schedule(self, question):
        assert looks_like_schedule_request(question, "足立") is False

    def test_cancel(self):
        assert is_cancel_request("さっきの予定を取り消して") is True
        assert is_register_request("さっきの予定を取り消して") is False

    @pytest.mark.parametrize(
        "question",
        ["経費精算の締め日は？", "予定を登録する手順を教えて", "送付状を作って"],
    )
    def test_not_schedule(self, question):
        assert looks_like_schedule_request(question, "足立") is False

    def test_whereabouts_needs_the_owner_name(self):
        # 名前が入っていれば「予定」と言わなくても照会として扱う
        assert looks_like_schedule_request("足立さん今日は何してますか？", "足立") is True
        # 名前が無ければ一般の質問として扱う（Q&Aへ流す）
        assert looks_like_schedule_request("今日は何しますか？", "足立") is False
        assert looks_like_schedule_request("足立さんに今日何を渡せばいい？", "足立") is False


def make_config(tmp_path):
    config = Config.load(tmp_path / "no-config.json")
    config.base_dir = tmp_path
    config.data["state_dir"] = str(tmp_path / "state")
    config.data["schedule"] = {
        "enabled": True,
        "owner_name": "足立",
        "notify_room_id": 384793683,
        "notify_account_id": 6945415,
        "notify_weekdays_only": True,
        "ics_labels": ["トヨクモ スケジューラー"],
    }
    return config


def fake_client(fields):
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=json.dumps(fields, ensure_ascii=False))],
        usage=SimpleNamespace(
            input_tokens=800, output_tokens=120,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        ),
    )
    client = SimpleNamespace(kwargs=None)

    def create(**kwargs):
        client.kwargs = kwargs
        return response

    client.messages = SimpleNamespace(create=create)
    return client


class FakeWriter:
    label = "Googleカレンダー"

    def __init__(self):
        self.inserted = []
        self.deleted = []

    def list_events(self, start, end):
        return []

    def insert_event(self, event):
        self.inserted.append(event)
        return f"id{len(self.inserted)}"

    def delete_event(self, event_id):
        self.deleted.append(event_id)


class TestRegister:
    def test_registers_and_echoes_back(self, tmp_path, monkeypatch):
        writer = FakeWriter()
        monkeypatch.setattr("raizuinu.scheduletask.build_writer", lambda cfg: writer)
        client = fake_client(
            {
                "events": [
                    {"summary": "南都銀行 訪問", "date": "2026-08-18", "start_time": "14:00",
                     "end_time": "15:00", "all_day": False, "location": "本店営業部"}
                ],
                "missing": [], "opening": "承知しました。登録しました。",
            }
        )
        runner = ScheduleRunner(make_config(tmp_path), client=client)
        reply, meta, usage = runner.register("明日14時から南都銀行訪問、予定入れて")

        assert len(writer.inserted) == 1
        event = writer.inserted[0]
        assert (event.summary, event.location) == ("南都銀行 訪問", "本店営業部")
        assert event.start == datetime(2026, 8, 18, 14, 0, tzinfo=JST)
        # 何を登録したかを必ず復唱する（誤りがその場で分かるように）
        assert "8月18日（火） 14:00〜15:00　南都銀行 訪問　＠本店営業部" in reply
        assert "取り消して" in reply
        assert meta["registered"][0]["id"] == "id1"
        assert usage["input_tokens"] == 800

    def test_end_time_defaults_to_one_hour(self, tmp_path, monkeypatch):
        writer = FakeWriter()
        monkeypatch.setattr("raizuinu.scheduletask.build_writer", lambda cfg: writer)
        client = fake_client(
            {
                "events": [{"summary": "打ち合わせ", "date": "2026-08-18", "start_time": "10:00",
                            "end_time": "", "all_day": False, "location": ""}],
                "missing": [], "opening": "",
            }
        )
        runner = ScheduleRunner(make_config(tmp_path), client=client)
        runner.register("明日10時から打ち合わせ入れて")
        event = writer.inserted[0]
        assert event.end - event.start == timedelta(hours=1)

    def test_missing_fields_are_asked_not_guessed(self, tmp_path, monkeypatch):
        writer = FakeWriter()
        monkeypatch.setattr("raizuinu.scheduletask.build_writer", lambda cfg: writer)
        client = fake_client({"events": [], "missing": ["日付"], "opening": "予定ですね。"})
        runner = ScheduleRunner(make_config(tmp_path), client=client)
        reply, meta, _ = runner.register("南都銀行と面談の予定入れといて")
        assert not writer.inserted  # 日付を推測で埋めない
        assert "日付" in reply
        assert meta["error"] == "missing_fields"

    def test_no_writer_configured(self, tmp_path, monkeypatch):
        monkeypatch.setattr("raizuinu.scheduletask.build_writer", lambda cfg: None)
        runner = ScheduleRunner(make_config(tmp_path), client=fake_client({}))
        reply, meta, usage = runner.register("明日10時に会議入れて")
        assert meta["error"] == "no_writer"
        assert usage == {}

    def test_cancel_deletes_the_last_one(self, tmp_path, monkeypatch):
        writer = FakeWriter()
        monkeypatch.setattr("raizuinu.scheduletask.build_writer", lambda cfg: writer)
        runner = ScheduleRunner(make_config(tmp_path), client=fake_client({}))
        reply, meta, _ = runner.cancel([{"id": "id1", "summary": "南都銀行 訪問"}])
        assert writer.deleted == ["id1"]
        assert "南都銀行 訪問" in reply


class TestAnswer:
    def test_answers_todays_schedule(self, tmp_path, monkeypatch):
        source = FakeSource([ev("面談", 14)], "トヨクモ スケジューラー")
        monkeypatch.setattr("raizuinu.scheduletask.build_sources", lambda cfg: [source])
        runner = ScheduleRunner(make_config(tmp_path), client=fake_client({}))
        reply, meta, usage = runner.answer("足立さんの今日の予定教えて")
        assert "足立さんの" in reply and "面談" in reply
        assert usage == {}  # 照会はAPIを使わない
        assert meta["events"] == 1

    def test_no_source_configured(self, tmp_path, monkeypatch):
        monkeypatch.setattr("raizuinu.scheduletask.build_sources", lambda cfg: [])
        runner = ScheduleRunner(make_config(tmp_path), client=fake_client({}))
        reply, meta, _ = runner.answer("今日の予定は？")
        assert meta["error"] == "no_source"

    @pytest.mark.parametrize(
        "question,offset,span",
        [("今日の予定", 0, 1), ("明日の予定は？", 1, 1), ("明後日の予定", 2, 1)],
    )
    def test_relative_days(self, tmp_path, monkeypatch, question, offset, span):
        seen = {}

        class Recorder:
            label = "テスト"

            def list_events(self, start, end):
                seen["start"] = start
                seen["span"] = (end - start).days
                return []

        monkeypatch.setattr("raizuinu.scheduletask.build_sources", lambda cfg: [Recorder()])
        runner = ScheduleRunner(make_config(tmp_path), client=fake_client({}))
        runner.answer(question)
        today = datetime.now(JST).date()
        assert seen["start"].date() == today + timedelta(days=offset)
        assert seen["span"] == span


class TestMorning:
    def _notifier(self, tmp_path, sources, chatwork):
        from raizuinu.morning import MorningNotifier

        return MorningNotifier(make_config(tmp_path), chatwork=chatwork, sources=sources)

    def test_posts_once_per_day(self, tmp_path):
        sent = []
        chatwork = SimpleNamespace(send_message=lambda room, body: sent.append((room, body)) or "1")
        notifier = self._notifier(tmp_path, [FakeSource([ev("面談", 14)])], chatwork)

        assert notifier.run_once(today=date(2026, 8, 18)) is True
        assert notifier.run_once(today=date(2026, 8, 18)) is False  # 二重投稿しない
        assert len(sent) == 1
        room, body = sent[0]
        assert room == 384793683
        assert body.startswith("[To:6945415]\n[info][title]足立さん 今日の予定")

    def test_skips_weekend(self, tmp_path):
        sent = []
        chatwork = SimpleNamespace(send_message=lambda room, body: sent.append(body) or "1")
        notifier = self._notifier(tmp_path, [FakeSource([])], chatwork)
        assert notifier.run_once(today=date(2026, 8, 22)) is False  # 土曜
        assert not sent

    def test_posts_even_when_empty(self, tmp_path):
        sent = []
        chatwork = SimpleNamespace(send_message=lambda room, body: sent.append(body) or "1")
        notifier = self._notifier(tmp_path, [FakeSource([])], chatwork)
        assert notifier.run_once(today=date(2026, 8, 18)) is True
        assert "予定は入っていません。" in sent[0]

    def test_disabled_does_nothing(self, tmp_path):
        sent = []
        chatwork = SimpleNamespace(send_message=lambda room, body: sent.append(body) or "1")
        notifier = self._notifier(tmp_path, [FakeSource([])], chatwork)
        notifier._config.data["schedule"]["enabled"] = False
        assert notifier.run_once(today=date(2026, 8, 18)) is False
        assert not sent


class TestIcsSourceSecrecy:
    def test_url_is_not_leaked_in_errors(self):
        secret = "https://example.com/ical/SECRETTOKEN12345"

        def http_get(url, timeout=30):
            return 403, b""

        source = IcsSource(secret, "トヨクモ スケジューラー", http_get=http_get)
        with pytest.raises(ScheduleError) as exc:
            source.list_events(*day_range(date(2026, 8, 18)))
        # URL自体が認証情報なので、メッセージにも監査ログにも出さない
        assert "SECRETTOKEN12345" not in str(exc.value)
        assert "トヨクモ スケジューラー" in str(exc.value)


class TestRegisteredReplyTense:
    """登録済みなのに「これから登録します」と読める返信にしない。"""

    @pytest.mark.parametrize(
        "opening",
        [
            "承知しました。以下の予定を登録します。",
            "了解です。予定に入れておきます。",
            "追加しときますね。",
            "よろしくお願いいたします。",
            "",
        ],
    )
    def test_future_or_asking_opening_is_replaced(self, tmp_path, monkeypatch, opening):
        writer = FakeWriter()
        monkeypatch.setattr("raizuinu.scheduletask.build_writer", lambda cfg: writer)
        client = fake_client(
            {
                "events": [{"summary": "面談", "date": "2026-08-26", "start_time": "15:00",
                            "end_time": "16:30", "all_day": False, "location": ""}],
                "missing": [], "opening": opening,
            }
        )
        runner = ScheduleRunner(make_config(tmp_path), client=client)
        reply, _, _ = runner.register("来週水曜15時から面談を入れて")
        # 言い回しは毎回選び直すが、意味は「登録し終えた」で固定
        from raizuinu.phrasing import BANKS

        assert reply.splitlines()[0] in BANKS["schedule_done"]

    def test_past_tense_opening_is_kept(self, tmp_path, monkeypatch):
        writer = FakeWriter()
        monkeypatch.setattr("raizuinu.scheduletask.build_writer", lambda cfg: writer)
        client = fake_client(
            {
                "events": [{"summary": "面談", "date": "2026-08-26", "start_time": "15:00",
                            "end_time": "16:30", "all_day": False, "location": ""}],
                "missing": [], "opening": "南都銀行との面談ですね。登録しました。",
            }
        )
        runner = ScheduleRunner(make_config(tmp_path), client=client)
        reply, _, _ = runner.register("来週水曜15時から面談を入れて")
        assert reply.startswith("南都銀行との面談ですね。登録しました。")

    def test_completion_is_stated_by_code_not_the_model(self, tmp_path, monkeypatch):
        writer = FakeWriter()
        monkeypatch.setattr("raizuinu.scheduletask.build_writer", lambda cfg: writer)
        client = fake_client(
            {
                "events": [{"summary": "面談", "date": "2026-08-26", "start_time": "15:00",
                            "end_time": "16:30", "all_day": False, "location": ""}],
                "missing": [], "opening": "南都銀行との面談ですね。",
            }
        )
        runner = ScheduleRunner(make_config(tmp_path), client=client)
        reply, _, _ = runner.register("来週水曜15時から面談を入れて")
        # 「登録した」という事実は文面任せにしない
        assert "上記で登録しました。" in reply


class TestUnsyncedMarking:
    """トヨクモに反映されない予定を黙って混ぜない。"""

    def _schedule(self):
        toyokumo = ev("朝礼", 9, source="トヨクモ スケジューラー")
        google = ev("南都銀行 訪問", 14, location="本店営業部", source="Googleカレンダー")
        return Schedule(events=[toyokumo, google])

    def test_morning_marks_google_only_events(self):
        body = format_day(
            self._schedule(), date(2026, 8, 18), "足立",
            mark_source="Googleカレンダー", mark_note="（トヨクモ未反映）",
        )
        assert "・09:00〜10:00　朝礼" in body
        assert "（トヨクモ未反映）" in body
        # トヨクモ由来には印を付けない
        assert "朝礼（トヨクモ未反映）" not in body

    def test_answer_marks_too(self):
        text = format_answer(
            self._schedule(), date(2026, 8, 18), "足立",
            mark_source="Googleカレンダー", mark_note="（トヨクモ未反映）",
        )
        assert "南都銀行 訪問　＠本店営業部（トヨクモ未反映）" in text

    def test_no_marking_when_not_configured(self):
        body = format_day(self._schedule(), date(2026, 8, 18), "足立")
        assert "（トヨクモ未反映）" not in body

    def test_register_reply_warns_about_toyokumo(self, tmp_path, monkeypatch):
        writer = FakeWriter()
        monkeypatch.setattr("raizuinu.scheduletask.build_writer", lambda cfg: writer)
        client = fake_client(
            {
                "events": [{"summary": "面談", "date": "2026-08-26", "start_time": "15:00",
                            "end_time": "16:30", "all_day": False, "location": ""}],
                "missing": [], "opening": "面談ですね。登録しました。",
            }
        )
        config = make_config(tmp_path)
        config.data["schedule"]["register_note"] = "※トヨクモへは自動反映されません。"
        runner = ScheduleRunner(config, client=client)
        reply, _, _ = runner.register("来週水曜15時から面談を入れて")
        assert "※トヨクモへは自動反映されません。" in reply


class TestMorningGreeting:
    """朝の通知を、掲示ではなく人からの声かけらしくする。"""

    def _sched(self, *events):
        return Schedule(events=list(events))

    def test_monday_and_friday_get_a_weekly_touch(self):
        from raizuinu.schedule import morning_lead

        monday = morning_lead(date(2026, 8, 17), self._sched(ev("朝礼", 9)))
        friday = morning_lead(date(2026, 8, 21), self._sched(ev("朝礼", 9)))
        wednesday = morning_lead(date(2026, 8, 19), self._sched(ev("朝礼", 9)))
        assert monday.startswith("おはようございます。今週もよろしくお願いします。")
        assert friday.startswith("おはようございます。今週も残り1日です。")
        assert wednesday.startswith("おはようございます。\n")  # 平日中日は挨拶だけ

    def test_mentions_the_first_appointment(self):
        from raizuinu.schedule import morning_lead

        text = morning_lead(date(2026, 8, 19), self._sched(ev("朝礼", 9), ev("面談", 14)))
        assert "今日の予定は2件です。最初は9時からの「朝礼」です。" in text

    def test_busy_day_is_called_out(self):
        from raizuinu.schedule import morning_lead

        text = morning_lead(
            date(2026, 8, 19),
            self._sched(ev("A", 9), ev("B", 11), ev("C", 14), ev("D", 16)),
        )
        assert "今日の予定は4件、少し立て込んでいます。" in text

    def test_single_and_all_day(self):
        from raizuinu.schedule import morning_lead

        one = morning_lead(date(2026, 8, 19), self._sched(ev("面談", 14)))
        assert one.endswith("今日の予定は、14時からの「面談」1件です。")
        allday = morning_lead(date(2026, 8, 19), self._sched(ev("月次締め")))
        assert allday.endswith("今日の予定は、終日の「月次締め」1件です。")

    def test_empty_day(self):
        from raizuinu.schedule import morning_lead

        assert morning_lead(date(2026, 8, 19), self._sched()).endswith("今日は予定が入っていません。")

    def test_greeting_appears_before_the_box(self):
        body = format_day(
            self._sched(ev("朝礼", 9)), date(2026, 8, 19), "足立",
            greeting="おはようございます。",
        )
        assert body.startswith("おはようございます。")
        assert "[info][title]足立さん 今日の予定" in body

    def test_no_greeting_when_not_configured(self):
        body = format_day(self._sched(ev("朝礼", 9)), date(2026, 8, 19), "足立")
        assert body.startswith("[info][title]")  # 照会の返答には挨拶を付けない


class TestAllDayNormalization:
    """カレンダーに「00:00〜23:45」と入れられた終日相当の予定の扱い。"""

    def test_zero_oclock_long_event_becomes_all_day(self):
        e = Event(
            summary="[休み]",
            start=datetime(2026, 8, 17, 0, 0, tzinfo=JST),
            end=datetime(2026, 8, 17, 23, 45, tzinfo=JST),
        )
        assert e.all_day is True
        assert e.time_label() == "終日"

    def test_short_morning_event_stays_timed(self):
        # 0時開始でも短いものは終日にしない
        e = Event(
            summary="夜間バッチ",
            start=datetime(2026, 8, 17, 0, 0, tzinfo=JST),
            end=datetime(2026, 8, 17, 2, 0, tzinfo=JST),
        )
        assert e.all_day is False
        assert e.time_label() == "00:00〜02:00"

    def test_long_event_not_starting_at_zero_stays_timed(self):
        e = Event(
            summary="終日研修",
            start=datetime(2026, 8, 17, 9, 0, tzinfo=JST),
            end=datetime(2026, 8, 18, 9, 0, tzinfo=JST),
        )
        assert e.all_day is False

    def test_lead_reads_naturally(self):
        from raizuinu.schedule import morning_lead

        rest = Event(
            summary="[休み]",
            start=datetime(2026, 8, 17, 0, 0, tzinfo=JST),
            end=datetime(2026, 8, 17, 23, 45, tzinfo=JST),
        )
        text = morning_lead(date(2026, 8, 17), Schedule(events=[rest, ev("朝礼", 9)]))
        assert "最初は終日の「[休み]」です。" in text
        assert "0時からの" not in text
