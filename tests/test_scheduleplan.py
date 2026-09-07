"""空き時間の提案と、複数人の日程調整。

「篠田さんと来週打合せしたい。いつがいい？」→ 全員が空いている枠を候補で返し、
管理者は「1番で登録して」で登録できる。日時を決めて頼まれたら、登録の前に
相手の予定と重ならないか確かめる。候補の日時はコードで決める（作文させない）。
"""

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from raizuinu.config import Config
from raizuinu.schedule import JST, Event, ScheduleError
from raizuinu.scheduleplan import (
    Candidate,
    Constraints,
    Member,
    PlanRunner,
    Settings,
    busy_of,
    common_window,
    conflicts_at,
    find_candidates,
    find_members,
    free_slots,
    load_members,
    looks_like_proposal_request,
    pick_for_day,
    pick_number,
)
from raizuinu.scheduletask import ScheduleRunner

ADACHI, SHINODA, NAKAURA, GUEST = 6945415, 9228914, 10622368, 777
MON9 = datetime(2026, 9, 7, 9, 0, tzinfo=JST)  # 月曜 9:00
HOLIDAYS = {"2026-09-21", "2026-09-22", "2026-09-23"}


def make_config(tmp_path):
    config = Config.load(tmp_path / "no-config.json")
    config.base_dir = tmp_path
    config.data["state_dir"] = str(tmp_path / "state")
    config.data["schedule"] = {
        "enabled": True,
        "owner_name": "足立",
        "members": [
            {"name": "足立", "account_id": ADACHI, "owner": True},
            {"name": "篠田", "account_id": SHINODA, "work_days": [0, 3, 4]},
            {"name": "中浦", "account_id": NAKAURA, "work_days": [0, 1, 2],
             "work_hours": {"start": "10:00", "end": "16:30"}},
        ],
        "proposal": {"enabled": True},
    }
    config.data["admin_account_ids"] = [ADACHI]
    return config


def ev(day, start="10:00", end="11:00", summary="予定", all_day=False):
    def at(hm):
        hour, minute = map(int, hm.split(":"))
        return datetime(2026, 9, day, hour, minute, tzinfo=JST)

    if all_day:
        begin = datetime(2026, 9, day, 0, 0, tzinfo=JST)
        return Event(summary=summary, start=begin, end=begin + timedelta(days=1), all_day=True)
    return Event(summary=summary, start=at(start), end=at(end))


class FakeSource:
    def __init__(self, events, label="テスト", fail=False):
        self._events, self.label, self._fail = events, label, fail

    def list_events(self, start, end):
        if self._fail:
            raise ScheduleError("取得できません")
        return [e for e in self._events if e.start < end and e.end > start]


def fake_client(fields):
    base = {
        "participants": [], "duration_minutes": 0, "date_from": "", "date_to": "",
        "time_of_day": "any", "earliest": "", "latest": "", "fixed_start": "",
        "summary": "", "opening": "",
    }
    text = json.dumps({**base, **fields}, ensure_ascii=False)
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=500, output_tokens=80,
                              cache_creation_input_tokens=0, cache_read_input_tokens=0),
    )
    client = SimpleNamespace(kwargs=None)

    def create(**kwargs):
        client.kwargs = kwargs
        return response

    client.messages = SimpleNamespace(create=create)
    return client


def runner(tmp_path, fields, sources=None, now=MON9):
    sources = sources or {}
    return PlanRunner(
        make_config(tmp_path), client=fake_client(fields),
        sources_for=lambda m: sources.get(m.name, []), now=lambda: now, holidays=HOLIDAYS,
    )


WEEK = {"participants": ["篠田"], "duration_minutes": 60, "date_from": "2026-09-07",
        "date_to": "2026-09-11", "summary": "篠田さんと打合せ", "opening": "篠田さんとの打合せの日程ですね。"}


class TestDetection:
    @pytest.mark.parametrize("q", [
        "篠田さんと来週1時間打合せしたい。いつがいい？",
        "中浦さんと会議の日程を調整して",
        "来週、篠田さんと打ち合わせできる候補を出して",
        "打合せの時間、いつなら取れる？",
        "面談のおすすめの時間を提案して",
    ])
    def test_proposals(self, q):
        assert looks_like_proposal_request(q)

    @pytest.mark.parametrize("q", [
        "足立さんの今日の予定教えて",
        "来週の予定どうなってる？",
        "明日14時から南都銀行と面談、予定に入れといて",
        "予定を調整する手順を教えて",
        "経費精算の締め日は？",
    ])
    def test_not_proposals(self, q):
        assert not looks_like_proposal_request(q)

    @pytest.mark.parametrize("text, expected", [
        ("1番で登録して", 1), ("2で", 2), ("②でお願いします", 2), ("3番目で登録お願いします", 3),
        ("1つ目でいきましょう", 1), ("明日10時で登録して", None), ("1件目の予定は？", None),
        ("2026年の予定", None),
    ])
    def test_pick_number(self, text, expected):
        assert pick_number(text) == expected


class TestMembers:
    def test_loaded_from_config(self, tmp_path):
        members = load_members(make_config(tmp_path))
        assert [m.name for m in members] == ["足立", "篠田", "中浦"]
        assert members[0].is_owner and not members[1].is_owner
        assert members[2].work_start == 600 and members[2].work_end == 990
        assert members[1].env_name() == f"SCHEDULE_ICS_URL_{SHINODA}"

    def test_owner_is_added_when_missing(self, tmp_path):
        config = make_config(tmp_path)
        config.data["schedule"]["members"] = [{"name": "篠田", "account_id": SHINODA}]
        members = load_members(config)
        assert [m.name for m in members] == ["足立", "篠田"] and members[0].is_owner

    def test_names_are_found_in_order(self, tmp_path):
        members = load_members(make_config(tmp_path))
        found = find_members("中浦さんと篠田さんで打合せしたい", members)
        assert [m.name for m in found] == ["中浦", "篠田"]
        assert [m.name for m in find_members("篠田と", members)] == ["篠田"]
        assert find_members("経費精算の締め日は？", members) == []

    def test_hours_label(self):
        member = Member("中浦", work_days=(0, 1, 2), work_start=600, work_end=990)
        assert member.hours_label() == "月・火・水の10:00〜16:30出勤"
        assert Member("篠田", work_days=(0, 3, 4)).hours_label() == "月・木・金出勤"


class TestSlots:
    settings = Settings()

    def test_common_window_intersects_hours_and_days(self):
        adachi, nakaura, shinoda = Member("足立"), Member("中浦", work_days=(0, 1, 2), work_start=600, work_end=990), Member("篠田", work_days=(0, 3, 4))
        assert common_window(date(2026, 9, 7), [adachi, nakaura], self.settings, set()) == (600, 990)  # 月
        assert common_window(date(2026, 9, 8), [adachi, shinoda], self.settings, set()) is None  # 火は篠田が休み
        assert common_window(date(2026, 9, 12), [adachi], self.settings, set()) is None  # 土
        assert common_window(date(2026, 9, 21), [adachi], self.settings, {"2026-09-21"}) is None  # 祝日

    def test_a_member_may_work_on_holidays(self):
        adachi = Member("足立")
        settings = Settings(skip_holidays=False)
        assert common_window(date(2026, 9, 21), [adachi], settings, {"2026-09-21"}) == (540, 1080)
        fukumoto = Member("福本", skip_holidays=True)
        assert common_window(date(2026, 9, 21), [fukumoto], settings, {"2026-09-21"}) is None

    def test_busy_adds_buffer_and_blocks_absences_only(self):
        words = Settings().all_day_block_words
        busy = busy_of([ev(7, "10:00", "11:00"), ev(8, "14:00", "15:00")], date(2026, 9, 7), 15, words)
        assert busy == [(585, 675)]  # 前後15分
        assert busy_of([ev(7, all_day=True, summary="出張")], date(2026, 9, 7), 15, words) == [(0, 1440)]
        assert busy_of([ev(7, all_day=True, summary="夏季休暇")], date(2026, 9, 7), 15, words) == [(0, 1440)]
        # 覚え書きの終日予定は塞がない（実例: 「[予定入力NG]」が入った日に打合せを取りたい）
        assert busy_of([ev(7, all_day=True, summary="[予定入力NG]")], date(2026, 9, 7), 15, words) == []
        assert busy_of([ev(7, all_day=True, summary="出張")], date(2026, 9, 7), 15, ()) == []

    def test_free_gaps_show_the_real_picture(self):
        from raizuinu.scheduleplan import free_gaps

        gaps = free_gaps((540, 1080), [(585, 675), (765, 855), (945, 1035)], (720, 780))
        assert gaps == [(540, 585), (675, 720), (855, 945), (1035, 1080)]

    def test_a_gap_start_off_the_grid_is_still_a_slot(self):
        # 14:15〜15:45 の空きに60分を入れる: 14:30（切りの良い方）と14:15（区間の頭）
        slots = free_slots((540, 1080), [(0, 855), (945, 1440)], 60, None, 30)
        assert slots == [(855, 915), (870, 930)]
        assert pick_for_day(slots) == [(870, 930)]  # 30分刻みを優先

    def test_free_slots_skip_busy_and_lunch(self):
        slots = free_slots((540, 1080), [(585, 675)], 60, (720, 780), 30)
        assert slots[0] == (780, 840)  # 9:00は10時の予定の余白と、11:30は昼休みと重なる
        assert (1020, 1080) in slots and (1050, 1110) not in slots

    def test_pick_prefers_on_the_hour_and_spreads(self):
        slots = [(570, 630), (600, 660), (630, 690), (780, 840), (810, 870), (840, 900)]
        assert pick_for_day(slots) == [(600, 660), (780, 840)]
        assert pick_for_day([(570, 630)]) == [(570, 630)]
        assert pick_for_day([]) == []


class TestCandidates:
    def _members(self):
        return [Member("足立"), Member("篠田", work_days=(0, 3, 4))]

    def test_spread_over_days_and_sorted(self):
        events = {"足立": [ev(7, "10:00", "11:00")], "篠田": [ev(10, all_day=True, summary="出張")]}
        constraints = Constraints(60, date(2026, 9, 7), date(2026, 9, 11))
        found = find_candidates(self._members(), events, constraints, Settings(), set(), MON9)
        assert [c.label() for c in found] == [
            "9月7日（月）13:00〜14:00", "9月7日（月）16:00〜17:00", "9月11日（金）09:00〜10:00",
        ]

    def test_lead_time_today(self):
        constraints = Constraints(60, date(2026, 9, 7), date(2026, 9, 7))
        now = datetime(2026, 9, 7, 13, 10, tzinfo=JST)
        found = find_candidates([Member("足立")], {}, constraints, Settings(), set(), now)
        assert found[0].label() == "9月7日（月）15:00〜16:00"  # 1時間後以降で正時始まり

    def test_morning_or_afternoon_preference(self):
        constraints = Constraints(60, date(2026, 9, 7), date(2026, 9, 7), time_of_day="pm")
        found = find_candidates([Member("足立")], {}, constraints, Settings(), set(), MON9)
        assert all(c.start.hour >= 13 for c in found)
        constraints = Constraints(60, date(2026, 9, 8), date(2026, 9, 8), time_of_day="am")
        found = find_candidates([Member("足立")], {}, constraints, Settings(), set(), MON9)
        assert all(c.end.hour <= 12 for c in found)

    def test_nothing_when_nobody_lines_up(self):
        constraints = Constraints(60, date(2026, 9, 8), date(2026, 9, 9))  # 火水は篠田が休み
        assert find_candidates(self._members(), {}, constraints, Settings(), set(), MON9) == []


class TestConflicts:
    def test_reasons_per_person(self):
        members = [Member("足立"), Member("篠田", work_days=(0, 3, 4)), Member("中浦", work_days=(0, 1, 2), work_start=600, work_end=990)]
        events = {"足立": [ev(7, "14:00", "15:00", "南都銀行 訪問")]}
        start = datetime(2026, 9, 7, 14, 0, tzinfo=JST)
        found = conflicts_at(start, start + timedelta(hours=1), members, events, Settings(), set())
        assert found == ["足立さんに「南都銀行 訪問」（14:00〜15:00）が入っています"]
        start = datetime(2026, 9, 8, 17, 0, tzinfo=JST)  # 火 17:00
        found = conflicts_at(start, start + timedelta(hours=1), members, events, Settings(), set())
        assert "篠田さんは9月8日（火）は出勤日ではありません" in found
        assert "中浦さんの勤務時間（10:00〜16:30）の外です" in found


class TestPropose:
    def test_candidates_for_two_people(self, tmp_path):
        sources = {"足立": [FakeSource([ev(7, "10:00", "11:00")])],
                   "篠田": [FakeSource([ev(10, all_day=True, summary="出張")])]}
        reply, meta, usage = runner(tmp_path, WEEK, sources).propose(
            "篠田さんと来週1時間打合せしたい。いつがいい？", requester_id=ADACHI, is_admin=True
        )
        assert reply.startswith("篠田さんとの打合せの日程ですね。")
        assert "足立・篠田さんの9月7日（月）〜9月11日（金）の空きを確認しました（60分、09:00〜18:00、昼休みを除く）" in reply
        assert "1. 9月7日（月）13:00〜14:00" in reply
        assert "2. 9月7日（月）16:00〜17:00" in reply
        assert "3. 9月11日（金）09:00〜10:00" in reply
        assert "「1番で登録して」" in reply
        assert "※篠田さんは月・木・金出勤の前提で確認しています。" in reply
        assert meta["summary"] == "篠田さんと打合せ" and len(meta["candidates"]) == 3
        assert usage["input_tokens"] == 500

    def test_members_cannot_register_but_get_candidates(self, tmp_path):
        reply, _, _ = runner(tmp_path, WEEK, {"足立": [FakeSource([])], "篠田": [FakeSource([])]}).propose(
            "足立さんと打合せしたい。いつが空いてる？", requester_id=SHINODA, is_admin=False
        )
        assert "1番で登録して" not in reply
        assert "参加される方と決めてください" in reply

    def test_requester_is_included(self, tmp_path):
        _, meta, _ = runner(tmp_path, {**WEEK, "participants": ["中浦"]}, {}).propose(
            "中浦さんと打合せの候補を出して", requester_id=SHINODA, is_admin=False
        )
        assert meta["participants"] == ["篠田", "中浦"]

    def test_unknown_requester_falls_back_to_owner(self, tmp_path):
        _, meta, _ = runner(tmp_path, {**WEEK, "participants": []}, {}).propose(
            "打合せの候補を出して", requester_id=GUEST
        )
        assert meta["participants"] == ["足立"]

    def test_unset_calendar_is_disclosed(self, tmp_path):
        reply, _, _ = runner(tmp_path, WEEK, {"足立": [FakeSource([])]}).propose(
            "篠田さんと打合せしたい。いつがいい？", requester_id=ADACHI, is_admin=True
        )
        assert "※篠田さんのカレンダーは未設定のため、月・木・金出勤の前提で確認しています。" in reply

    def test_broken_calendar_is_disclosed(self, tmp_path):
        sources = {"足立": [FakeSource([], "トヨクモ", fail=True)], "篠田": [FakeSource([])]}
        reply, meta, _ = runner(tmp_path, WEEK, sources).propose("篠田さんと打合せしたい。いつがいい？", ADACHI, True)
        assert "足立さんのカレンダーを読み取れなかった" in reply
        assert meta["failed_sources"] == ["トヨクモ"]

    def test_no_slot_explains_why_and_looks_ahead(self, tmp_path):
        fields = {**WEEK, "date_from": "2026-09-08", "date_to": "2026-09-09"}  # 篠田の休みだけ
        reply, meta, _ = runner(tmp_path, fields, {"足立": [FakeSource([])], "篠田": [FakeSource([])]}).propose(
            "火水で篠田さんと打合せしたい。候補ある？", ADACHI, True
        )
        assert "足立・篠田さんの9月8日（火）〜9月9日（水）は、全員が揃う60分の時間がありませんでした。" in reply
        assert "・9月8日（火）: 篠田さんの出勤日ではありません" in reply
        assert "近い日でしたら、次が空いています。" in reply
        assert "1. 9月10日（木）09:00〜10:00" in reply
        assert len(meta["candidates"]) == 3


REAL_9TH = [  # 2026-09-09 の足立さんの実際の予定（トヨクモ）
    ev(9, all_day=True, summary="[予定入力NG]"),
    ev(9, "09:30", "10:00", "[外出]移動"), ev(9, "09:30", "10:00", "登録制アルバイト処理確認他"),
    ev(9, "10:00", "11:00", "[会議（リアル）]【仮】京都中央信用金庫"), ev(9, "11:00", "12:00", "[外出]三十三銀行訪問"),
    ev(9, "12:00", "12:30", "[外出]移動"), ev(9, "13:00", "14:00", "[会議（リアル）]京都中央信用金庫 澤田様・白樫様来社"),
    ev(9, "16:00", "17:00", "タスク実行時間調整"),
    Event(summary="㈱HEATtireservise 平岡様（時間調整中）", start=datetime(2026, 9, 9, 19, 15, tzinfo=JST),
          end=datetime(2026, 9, 10, 0, 15, tzinfo=JST)),
]
ONE_DAY = {"participants": [], "duration_minutes": 60, "date_from": "2026-09-09", "date_to": "2026-09-09",
           "summary": "金融機関との打合せ", "opening": "金融機関との打合せの日程ですね。"}


class TestOnePersonOneDay:
    """実例（2026-09-07）: 「9日に金融機関との打合せ予定1時間取れる？」。

    本人1人の空きなのに「全員が揃う枠が見つかりませんでした」と返した。
    終日の「[予定入力NG]」で日を塞いでいたのが原因。
    """

    def test_the_memo_does_not_block_and_the_gap_is_found(self, tmp_path):
        reply, meta, _ = runner(tmp_path, ONE_DAY, {"足立": [FakeSource(REAL_9TH)]}).propose(
            "9日に金融機関との打合せ予定1時間取れる？", ADACHI, True
        )
        assert reply.startswith("金融機関との打合せの日程ですね。")
        assert "足立さんの9月9日（水）の空きを確認しました" in reply  # 1人なら「全員」と言わない、1日なら範囲で書かない
        assert "1. 9月9日（水）14:30〜15:30" in reply  # 14:00の会議の後15分空け、16:00の前15分空けた中
        assert "※9月9日（水）は終日「[予定入力NG]」が入っています。" in reply
        assert "全員" not in reply
        assert meta["summary"] == "金融機関との打合せ" and len(meta["candidates"]) == 1

    def test_when_the_day_is_full_the_gaps_are_shown_and_next_days_offered(self, tmp_path):
        fields = {**ONE_DAY, "duration_minutes": 120}
        sources = {"足立": [FakeSource(REAL_9TH + [ev(10, "09:00", "09:30", "朝会")])]}
        reply, meta, _ = runner(tmp_path, fields, sources).propose("9日に2時間取れる？", ADACHI, True)
        assert "足立さんの9月9日（水）は、続けて空く120分の時間がありませんでした。" in reply
        assert "・9月9日（水）の空き: 14:15〜15:45（90分）、17:15〜18:00（45分）" in reply
        assert "（予定の前後15分を空けて確認しています）" in reply
        assert "近い日でしたら、次が空いています。" in reply
        assert "1. 9月10日（木）10:00〜12:00" in reply  # 9:30の朝会の後15分空けて
        assert "全員" not in reply

    def test_a_fixed_time_for_one_person(self, tmp_path):
        fields = {**ONE_DAY, "fixed_start": "2026-09-09 14:30"}
        reply, _, _ = runner(tmp_path, fields, {"足立": [FakeSource(REAL_9TH)]}).propose("9日14時半に1時間取れる？", ADACHI, True)
        assert "9月9日（水）14:30〜15:30は空いています。" in reply
        assert "とも空いています" not in reply

    def test_defaults_when_nothing_is_specified(self, tmp_path):
        fields = {"participants": ["篠田"], "opening": "はい、探しますね。"}
        _, meta, _ = runner(tmp_path, fields, {"足立": [FakeSource([])], "篠田": [FakeSource([])]}).propose(
            "篠田さんと打合せしたい。いつがいい？", ADACHI, True
        )
        assert meta["duration"] == 60 and len(meta["candidates"]) == 3

    def test_fixed_time_that_is_free(self, tmp_path):
        fields = {**WEEK, "fixed_start": "2026-09-07 14:00"}
        reply, meta, _ = runner(tmp_path, fields, {"足立": [FakeSource([])], "篠田": [FakeSource([])]}).propose(
            "月曜14時に篠田さんと打合せできる？", ADACHI, True
        )
        assert "9月7日（月）14:00〜15:00は、足立・篠田さんとも空いています。" in reply
        assert meta["candidates"] == [{"start": "2026-09-07T14:00:00+09:00", "end": "2026-09-07T15:00:00+09:00"}]

    def test_fixed_time_that_collides_gets_alternatives(self, tmp_path):
        fields = {**WEEK, "fixed_start": "2026-09-07 10:00"}
        sources = {"足立": [FakeSource([])], "篠田": [FakeSource([ev(7, "10:00", "11:00", "来客対応")])]}
        reply, meta, _ = runner(tmp_path, fields, sources).propose("月曜10時に篠田さんと打合せできる？", ADACHI, True)
        assert "9月7日（月）10:00〜11:00は、次の理由で重なります。" in reply
        assert "・篠田さんに「来客対応」（10:00〜11:00）が入っています" in reply
        assert "代わりに、全員が空いている時間はこちらです。" in reply
        assert "1. 9月7日（月）13:00〜14:00" in reply  # 10時の予定の余白で11時も外れる
        assert meta["conflicts"] and len(meta["candidates"]) == 3

    def test_an_opening_that_claims_registration_is_replaced(self, tmp_path):
        fields = {**WEEK, "opening": "登録しました。"}
        reply, _, _ = runner(tmp_path, fields, {"足立": [FakeSource([])], "篠田": [FakeSource([])]}).propose(
            "篠田さんと打合せしたい。いつがいい？", ADACHI, True
        )
        assert reply.startswith("足立・篠田さんの日程ですね。")

    def test_dates_are_written_by_code_not_the_model(self, tmp_path):
        fields = {**WEEK, "opening": "9月30日13時でどうでしょう。"}  # モデルの作文は候補にしない
        reply, meta, _ = runner(tmp_path, fields, {"足立": [FakeSource([])], "篠田": [FakeSource([])]}).propose(
            "篠田さんと打合せしたい。いつがいい？", ADACHI, True
        )
        assert all(c["start"] < "2026-09-12" for c in meta["candidates"])


class TestFollowUpParsing:
    """「登録して」「14:30で」を、直前の候補の続きとして読むための部品。"""

    @pytest.mark.parametrize("text, expected", [
        ("登録して", None), ("14:30で登録して", (14, 30)), ("14時半で", (14, 30)), ("14時で", (14, 0)),
        ("件名は設備資金調達の件、場所は本社事務所ミーティングルーム②", None), ("9/9 14:30〜15:30", (14, 30)),
    ])
    def test_time_in(self, text, expected):
        from raizuinu.scheduleplan import time_in

        assert time_in(text) == expected

    @pytest.mark.parametrize("text, expected", [
        ("登録して", False), ("9日で登録して", True), ("9/9 14:30で", True), ("来週火曜で", True),
        ("14:30で登録して", False), ("件名は設備資金調達の件", False),
    ])
    def test_has_date(self, text, expected):
        from raizuinu.scheduleplan import has_date

        assert has_date(text) is expected

    @pytest.mark.parametrize("text, expected", [
        ("1番で登録して", ""), ("登録して", ""), ("①でお願いします", ""),
        ("1番で。件名は設備資金調達の件、場所は本社事務所ミーティングルーム②", "件名は設備資金調達の件、場所は本社事務所ミーティングルーム2"),
        ("その時間で登録して。件名は設備資金調達の件", "件名は設備資金調達の件"),
        ("16時で登録して", ""), ("14:30〜15:30で登録しといて", ""), ("その時間でお願いします", ""),
    ])
    def test_strip_pick(self, text, expected):
        from raizuinu.scheduleplan import strip_pick

        assert strip_pick(text) == expected


class FakeWriter:
    label = "Googleカレンダー"

    def __init__(self):
        self.inserted = []

    def list_events(self, start, end):
        return []

    def insert_event(self, event):
        self.inserted.append(event)
        return f"id{len(self.inserted)}"


class TestPickAndRegister:
    def test_remember_pending_and_clear(self, tmp_path):
        plan = runner(tmp_path, WEEK)
        meta = {"candidates": [{"start": "2026-09-07T13:00:00+09:00", "end": "2026-09-07T14:00:00+09:00"}],
                "summary": "篠田さんと打合せ", "participants": ["足立", "篠田"]}
        plan.remember(1, ADACHI, 1_000, meta)
        assert plan.pending(1, ADACHI, 1_000 + 3600)["summary"] == "篠田さんと打合せ"
        assert plan.pending(1, ADACHI, 1_000 + 2 * 86400) is None  # 期限切れ
        assert plan.pending(1, SHINODA, 1_000) is None  # 別の人の候補は使わない
        plan.clear(1, ADACHI)
        assert plan.pending(1, ADACHI, 1_000) is None

    def test_register_the_chosen_candidate(self, tmp_path, monkeypatch):
        writer = FakeWriter()
        monkeypatch.setattr("raizuinu.scheduletask.build_writer", lambda cfg: writer)
        plan = runner(tmp_path, WEEK)
        pending = {"candidates": [
            {"start": "2026-09-07T13:00:00+09:00", "end": "2026-09-07T14:00:00+09:00"},
            {"start": "2026-09-07T16:00:00+09:00", "end": "2026-09-07T17:00:00+09:00"},
        ], "summary": "篠田さんと打合せ"}
        schedule = ScheduleRunner(make_config(tmp_path), client=fake_client({}))
        reply, meta, usage = plan.register_pick(pending, 2, schedule)
        assert len(writer.inserted) == 1
        event = writer.inserted[0]
        assert event.summary == "篠田さんと打合せ"
        assert event.start == datetime(2026, 9, 7, 16, 0, tzinfo=JST)
        assert "9月7日（月） 16:00〜17:00　篠田さんと打合せ" in reply
        assert "上記で登録しました。" in reply
        assert meta["registered"][0]["id"] == "id1" and usage == {}

    def test_a_bad_number_is_refused(self, tmp_path):
        plan = runner(tmp_path, WEEK)
        reply, meta, _ = plan.register_pick({"candidates": [{"start": "2026-09-07T13:00:00+09:00", "end": "2026-09-07T14:00:00+09:00"}]}, 5, None)
        assert meta["error"] == "bad_number" and "1件まで" in reply


ONE = {"candidates": [{"start": "2026-09-09T14:30:00+09:00", "end": "2026-09-09T15:30:00+09:00"}],
       "summary": "金融機関との打合せ", "participants": ["足立"]}
TWO = {"candidates": [{"start": "2026-09-07T13:00:00+09:00", "end": "2026-09-07T14:00:00+09:00"},
                      {"start": "2026-09-07T16:00:00+09:00", "end": "2026-09-07T17:00:00+09:00"}],
       "summary": "篠田さんと打合せ", "participants": ["足立", "篠田"]}


def counting_schedule(tmp_path, fields=None):
    """ScheduleRunner。読み取りAPIを何回呼んだかを数える。"""
    fields = fields or {"events": [], "missing": [], "opening": ""}
    schedule = ScheduleRunner(make_config(tmp_path), client=fake_client(fields))
    schedule.calls = 0
    original = schedule.extract_events

    def counted(text):
        schedule.calls += 1
        schedule.last_text = text
        return original(text)

    schedule.extract_events = counted
    return schedule


class TestRegisterWithoutRepeating:
    """実例（2026-09-07）: 候補を出した後の「登録して」に、日付・件名・場所・時刻を聞き返した。"""

    def test_just_register_uses_the_only_candidate(self, tmp_path, monkeypatch):
        writer = FakeWriter()
        monkeypatch.setattr("raizuinu.scheduletask.build_writer", lambda cfg: writer)
        schedule = counting_schedule(tmp_path)
        reply, meta, usage = runner(tmp_path, {}).register_from_pending(ONE, "登録して", schedule)
        assert writer.inserted[0].summary == "金融機関との打合せ"
        assert writer.inserted[0].start == datetime(2026, 9, 9, 14, 30, tzinfo=JST)
        assert "9月9日（水） 14:30〜15:30　金融機関との打合せ" in reply
        assert schedule.calls == 0  # 何も読み取らずに登録できる（費用も掛けない）

    def test_extra_details_are_taken_without_asking(self, tmp_path, monkeypatch):
        writer = FakeWriter()
        monkeypatch.setattr("raizuinu.scheduletask.build_writer", lambda cfg: writer)
        # 読み取りが日時を間違えて返しても、候補の日時を正とする
        fields = {"events": [{"summary": "設備資金調達の件", "date": "2026-09-10", "start_time": "10:00",
                              "end_time": "11:00", "all_day": False, "location": "本社事務所ミーティングルーム2"}],
                  "missing": [], "opening": "設備資金調達の件ですね。"}
        schedule = counting_schedule(tmp_path, fields)
        text = "その時間で登録して。件名は設備資金調達の件、場所は本社事務所ミーティングルーム②"
        reply, meta, _ = runner(tmp_path, {}).register_from_pending(ONE, text, schedule)
        event = writer.inserted[0]
        assert (event.summary, event.location) == ("設備資金調達の件", "本社事務所ミーティングルーム2")
        assert event.start == datetime(2026, 9, 9, 14, 30, tzinfo=JST)
        assert "9月9日（水） 14:30〜15:30　設備資金調達の件　＠本社事務所ミーティングルーム2" in reply
        assert "14:30〜15:30 に「金融機関との打合せ」" in schedule.last_text  # 分かっていることを渡して読ませる
        assert "教えていただけますか" not in reply

    def test_a_time_picks_the_matching_candidate(self, tmp_path, monkeypatch):
        writer = FakeWriter()
        monkeypatch.setattr("raizuinu.scheduletask.build_writer", lambda cfg: writer)
        schedule = counting_schedule(tmp_path)
        reply, meta, _ = runner(tmp_path, {}).register_from_pending(TWO, "16時で登録して", schedule)
        assert writer.inserted[0].start == datetime(2026, 9, 7, 16, 0, tzinfo=JST)
        assert schedule.calls == 0

    def test_several_candidates_ask_for_the_number_only(self, tmp_path, monkeypatch):
        writer = FakeWriter()
        monkeypatch.setattr("raizuinu.scheduletask.build_writer", lambda cfg: writer)
        schedule = counting_schedule(tmp_path)
        reply, meta, _ = runner(tmp_path, {}).register_from_pending(TWO, "登録して", schedule)
        assert not writer.inserted
        assert reply.startswith("どの時間で登録しましょうか。番号でお知らせください。")
        assert "1. 9月7日（月）13:00〜14:00" in reply and "2. 9月7日（月）16:00〜17:00" in reply
        assert "日付" not in reply and "件名" not in reply
        assert meta["error"] == "which_candidate"

    def test_a_date_in_the_message_means_a_fresh_registration(self, tmp_path):
        assert runner(tmp_path, {}).register_from_pending(ONE, "9/10 14:30で登録して", None) is None
        assert runner(tmp_path, {}).register_from_pending(ONE, "15:00で登録して", None) is None  # 候補に無い時刻

    def test_the_single_candidate_reply_invites_a_plain_register(self, tmp_path):
        reply, _, _ = runner(tmp_path, ONE_DAY, {"足立": [FakeSource(REAL_9TH)]}).propose(
            "9日に金融機関との打合せ予定1時間取れる？", ADACHI, True
        )
        assert "この時間でよければ「登録して」とお知らせください。件名や場所も一緒に書いていただければ、そのまま反映します。" in reply
        assert "1番で登録して" not in reply


class TestContextCarriesOver:
    """直前の日程の話を引き継ぎ、同じ条件を言わせない。"""

    def test_afternoon_only_keeps_the_same_day(self, tmp_path):
        plan = runner(tmp_path, {"time_of_day": "pm", "opening": "午後ですね。"}, {"足立": [FakeSource(REAL_9TH)]})
        context = {"date_from": "2026-09-09", "date_to": "2026-09-09", "duration": 60,
                   "participants": ["足立"], "summary": "金融機関との打合せ"}
        reply, meta, _ = plan.propose("午後で", ADACHI, True, context=context)
        assert "足立さんの9月9日（水）の空きを確認しました（60分、午後" in reply
        assert "1. 9月9日（水）14:30〜15:30" in reply
        assert meta["summary"] == "金融機関との打合せ"

    def test_what_is_said_now_wins(self, tmp_path):
        plan = runner(tmp_path, {"date_from": "2026-09-10", "date_to": "2026-09-10", "duration_minutes": 30},
                      {"足立": [FakeSource(REAL_9TH)]})
        context = {"date_from": "2026-09-09", "date_to": "2026-09-09", "duration": 60}
        _, meta, _ = plan.propose("10日に30分なら？", ADACHI, True, context=context)
        assert meta["duration"] == 30 and meta["candidates"][0]["start"].startswith("2026-09-10")

    def test_context_expires_sooner_than_the_candidates(self, tmp_path):
        plan = runner(tmp_path, {})
        plan.remember(1, ADACHI, 1_000, {"candidates": ONE["candidates"], "summary": "x",
                                          "context": {"date_from": "2026-09-09", "date_to": "2026-09-09", "duration": 60}})
        assert plan.context_for(1, ADACHI, 1_000 + 3600)["date_from"] == "2026-09-09"
        assert plan.context_for(1, ADACHI, 1_000 + 3 * 3600) == {}  # 2時間で条件は引き継がない
        assert plan.pending(1, ADACHI, 1_000 + 3 * 3600) is not None  # 候補は24時間使える


def any_time_runner(tmp_path, fields, sources=None):
    """足立さんは曜日・時間帯の制限なし（本人の指定: 土日祝・24時間すべて可）。"""
    plan = runner(tmp_path, fields, sources)
    plan._config.data["schedule"]["members"][0]["any_time"] = True
    return plan


class TestNoLimitsForTheOwner:
    """足立さん本人の予定には 9:00〜18:00・昼休み・土日祝の制限を掛けない。

    ただし候補はまず通常の時間帯で探し、無いときだけ時間外・土日祝へ広げる
    （深夜0時を最初に勧めないため）。
    """

    def test_no_hours_caveat_in_the_wording(self, tmp_path):
        reply, _, _ = any_time_runner(tmp_path, ONE_DAY, {"足立": [FakeSource(REAL_9TH)]}).propose(
            "9日に金融機関との打合せ予定1時間取れる？", ADACHI, True
        )
        assert "足立さんの9月9日（水）の空きを確認しました（60分）。" in reply
        assert "昼休み" not in reply and "09:00〜18:00" not in reply
        assert "1. 9月9日（水）14:30〜15:30" in reply  # 通常の時間帯に空きがあればそこから

    def test_outside_hours_only_when_normal_hours_are_full(self, tmp_path):
        fields = {**ONE_DAY, "duration_minutes": 120}
        reply, meta, _ = any_time_runner(tmp_path, fields, {"足立": [FakeSource(REAL_9TH)]}).propose(
            "9日に2時間取れる？", ADACHI, True
        )
        assert "1. 9月9日（水）07:00〜09:00" in reply  # 9:30の予定の15分前まで。深夜ではなく朝を選ぶ
        assert "※通常の時間帯（09:00〜18:00）に続けて120分の空きが無かったため、時間外や土日祝も含めて確認しています。" in reply
        assert len(meta["candidates"]) == 1

    def test_weekends_are_allowed(self, tmp_path):
        fields = {**ONE_DAY, "date_from": "2026-09-12", "date_to": "2026-09-13"}  # 土日
        reply, meta, _ = any_time_runner(tmp_path, fields, {"足立": [FakeSource([])]}).propose(
            "土日に1時間取れる？", ADACHI, True
        )
        assert "1. 9月12日（土）09:00〜10:00" in reply
        assert "2. 9月12日（土）12:00〜13:00" in reply  # 昼休みも外さない
        assert "3. 9月13日（日）09:00〜10:00" in reply

    def test_a_late_fixed_time_is_free(self, tmp_path):
        fields = {**ONE_DAY, "fixed_start": "2026-09-08 19:30"}
        reply, meta, _ = any_time_runner(tmp_path, fields, {"足立": [FakeSource(REAL_9TH)]}).propose(
            "8日の19時半に1時間取れる？", ADACHI, True
        )
        assert "9月8日（火）19:30〜20:30は空いています。" in reply
        assert meta["conflicts"] == []

    def test_a_partner_with_hours_still_bounds_the_search(self, tmp_path):
        # 篠田さんは火水が休み。足立さんが制限なしでも、2人の打合せは篠田さんの出勤日から
        fields = {**WEEK, "date_from": "2026-09-08", "date_to": "2026-09-09"}
        reply, meta, _ = any_time_runner(tmp_path, fields, {"足立": [FakeSource([])], "篠田": [FakeSource([])]}).propose(
            "火水で篠田さんと打合せしたい。候補ある？", ADACHI, True
        )
        assert "・9月8日（火）: 篠田さんの出勤日ではありません" in reply
        assert "1. 9月10日（木）09:00〜10:00" in reply
        assert "時間外" not in reply

    def test_the_default_owner_keeps_the_limits(self, tmp_path):
        fields = {**ONE_DAY, "date_from": "2026-09-12", "date_to": "2026-09-13"}
        reply, meta, _ = runner(tmp_path, fields, {"足立": [FakeSource([])]}).propose("土日に1時間取れる？", ADACHI, True)
        assert "・9月12日（土）: 土日" in reply and "近い日でしたら" in reply


class TestMissingFieldsAsked:
    def test_place_is_never_asked(self, tmp_path, monkeypatch):
        monkeypatch.setattr("raizuinu.scheduletask.build_writer", lambda cfg: FakeWriter())
        fields = {"events": [], "missing": ["日付", "件名", "場所", "時刻"], "opening": "予定の登録ですね。"}
        reply, meta, _ = ScheduleRunner(make_config(tmp_path), client=fake_client(fields)).register("登録して")
        assert meta["missing"] == ["日付", "件名", "時刻"]
        assert "場所" not in reply


class TestConflictCheckBeforeRegister:
    def test_no_named_member_means_no_check(self, tmp_path):
        assert runner(tmp_path, {}).conflict_check("明日14時から南都銀行と面談、予定に入れといて", ADACHI) is None

    def test_a_clash_blocks_and_offers_alternatives(self, tmp_path):
        sources = {"足立": [FakeSource([])], "篠田": [FakeSource([ev(7, "14:00", "15:00", "来客対応")])]}
        plan = runner(tmp_path, {}, sources)
        check = plan.conflict_check("月曜14時から篠田さんと打合せを入れて", ADACHI)
        event = Event(summary="篠田さんと打合せ", start=datetime(2026, 9, 7, 14, 0, tzinfo=JST),
                      end=datetime(2026, 9, 7, 15, 0, tzinfo=JST))
        text = check([event])
        assert "・篠田さんに「来客対応」（14:00〜15:00）が入っています" in text
        assert "1. 9月7日（月）" in text
        assert plan.last_check()["summary"] == "篠田さんと打合せ"

    def test_free_time_passes(self, tmp_path):
        plan = runner(tmp_path, {}, {"足立": [FakeSource([])], "篠田": [FakeSource([])]})
        check = plan.conflict_check("月曜14時から篠田さんと打合せを入れて", ADACHI)
        event = Event(summary="篠田さんと打合せ", start=datetime(2026, 9, 7, 14, 0, tzinfo=JST),
                      end=datetime(2026, 9, 7, 15, 0, tzinfo=JST))
        assert check([event]) is None

    def test_register_honours_the_check(self, tmp_path, monkeypatch):
        writer = FakeWriter()
        monkeypatch.setattr("raizuinu.scheduletask.build_writer", lambda cfg: writer)
        fields = {"events": [{"summary": "篠田さんと打合せ", "date": "2026-09-08", "start_time": "14:00",
                              "end_time": "15:00", "all_day": False, "location": ""}],
                  "missing": [], "opening": "承知しました。"}
        schedule = ScheduleRunner(make_config(tmp_path), client=fake_client(fields))
        plan = runner(tmp_path, {}, {"足立": [FakeSource([])], "篠田": [FakeSource([])]})
        check = plan.conflict_check("火曜14時から篠田さんと打合せを入れて", ADACHI)
        reply, meta, _ = schedule.register("火曜14時から篠田さんと打合せを入れて", check=check)
        assert not writer.inserted  # 篠田さんは火曜が休みなので登録しない
        assert meta["error"] == "conflict"
        assert "篠田さんは9月8日（火）は出勤日ではありません" in reply


class TestThroughTheHandler:
    """メンションから返信までを通す。候補→「1番で登録して」の流れと権限。"""

    def _handler(self, tmp_path, monkeypatch, plan_fields, register_fields=None, sources=None):
        from raizuinu.scheduletask import ScheduleRunner
        from tests.test_guest import make_handler

        writer = FakeWriter()
        monkeypatch.setattr("raizuinu.scheduletask.build_writer", lambda cfg: writer)
        config = make_config(tmp_path)
        schedule = ScheduleRunner(config, client=fake_client(register_fields or {"events": [], "missing": [], "opening": ""}))
        plan = runner(tmp_path, plan_fields, sources or {"足立": [FakeSource([])], "篠田": [FakeSource([])]})
        handler, chatwork, _, audit = make_handler(
            tmp_path, monkeypatch, members=(ADACHI, SHINODA), schedule=schedule, plan=plan
        )
        handler._config.data["admin_account_ids"] = [ADACHI]
        handler._config.data["schedule"] = config.data["schedule"]
        return handler, chatwork, audit, writer

    @staticmethod
    def _send(handler, account_id, text, message_id, send_time=1757203200):
        from tests.test_guest import payload
        from tests.test_handler import sign

        raw = payload(account_id, body=f"[To:999] {text}", message_id=message_id, send_time=send_time)
        handler.handle_webhook(raw, sign(raw))

    def test_admin_gets_candidates_and_registers_by_number(self, tmp_path, monkeypatch):
        handler, chatwork, audit, writer = self._handler(tmp_path, monkeypatch, WEEK)
        self._send(handler, ADACHI, "篠田さんと来週1時間打合せしたい。いつがいい？", "1")
        assert "1. 9月7日（月）" in chatwork.sent[0][1]
        assert audit.records[-1]["type"] == "schedule_propose"
        self._send(handler, ADACHI, "1番で登録して", "2", send_time=1757203300)
        assert len(writer.inserted) == 1
        assert writer.inserted[0].summary == "篠田さんと打合せ"
        assert "上記で登録しました。" in chatwork.sent[1][1]
        assert audit.records[-1]["type"] == "schedule_register"
        # 使った候補は消えるので、同じ番号をもう一度言っても登録しない
        self._send(handler, ADACHI, "1番で登録して", "3", send_time=1757203400)
        assert len(writer.inserted) == 1

    def test_a_member_can_ask_but_not_register(self, tmp_path, monkeypatch):
        from raizuinu.scheduletask import NOT_ADMIN_MESSAGE

        handler, chatwork, _, writer = self._handler(tmp_path, monkeypatch, WEEK)
        self._send(handler, SHINODA, "足立さんと打合せしたい。いつがいい？", "1")
        assert "1. 9月7日（月）" in chatwork.sent[0][1]
        assert "1番で登録して" not in chatwork.sent[0][1]
        self._send(handler, SHINODA, "1番で登録して", "2", send_time=1757203300)
        assert NOT_ADMIN_MESSAGE in chatwork.sent[1][1]
        assert not writer.inserted

    def test_the_screenshot_flow_registers_without_asking_again(self, tmp_path, monkeypatch):
        # 「9日に…1時間取れる？」→ 候補1件 → 「登録して」で聞き返さずに登録
        handler, chatwork, audit, writer = self._handler(
            tmp_path, monkeypatch, ONE_DAY, sources={"足立": [FakeSource(REAL_9TH)]}
        )
        self._send(handler, ADACHI, "9日に金融機関との打合せ予定1時間取れる？", "1")
        assert "1. 9月9日（水）14:30〜15:30" in chatwork.sent[0][1]
        self._send(handler, ADACHI, "登録して", "2", send_time=1757203300)
        assert len(writer.inserted) == 1
        assert writer.inserted[0].summary == "金融機関との打合せ"
        assert writer.inserted[0].start == datetime(2026, 9, 9, 14, 30, tzinfo=JST)
        assert "上記で登録しました。" in chatwork.sent[1][1]
        assert "教えていただけますか" not in chatwork.sent[1][1]

    def test_details_with_the_register_are_used(self, tmp_path, monkeypatch):
        register = {"events": [{"summary": "設備資金調達の件", "date": "2026-09-09", "start_time": "14:30",
                                "end_time": "15:30", "all_day": False, "location": "本社事務所ミーティングルーム2"}],
                    "missing": [], "opening": "設備資金調達の件ですね。"}
        handler, chatwork, _, writer = self._handler(
            tmp_path, monkeypatch, ONE_DAY, register, sources={"足立": [FakeSource(REAL_9TH)]}
        )
        self._send(handler, ADACHI, "9日に金融機関との打合せ予定1時間取れる？", "1")
        self._send(handler, ADACHI, "その時間で登録して。件名は設備資金調達の件、場所は本社事務所ミーティングルーム②", "2",
                   send_time=1757203300)
        assert writer.inserted[0].summary == "設備資金調達の件"
        assert writer.inserted[0].location == "本社事務所ミーティングルーム2"
        assert writer.inserted[0].start == datetime(2026, 9, 9, 14, 30, tzinfo=JST)

    def test_answering_the_ask_back_registers_instead_of_proposing(self, tmp_path, monkeypatch):
        # 聞き返しに答えたら登録する。候補の提案（「1番で登録して」）へ戻さない
        from raizuinu.scheduletask import ScheduleRunner
        from tests.test_guest import make_handler

        writer = FakeWriter()
        monkeypatch.setattr("raizuinu.scheduletask.build_writer", lambda cfg: writer)
        replies = [
            {"events": [], "missing": ["日付", "件名", "場所", "時刻"], "opening": "予定の登録ですね。"},
            {"events": [{"summary": "設備資金調達の件", "date": "2026-09-09", "start_time": "14:30",
                         "end_time": "15:30", "all_day": False, "location": "本社事務所ミーティングルーム2"}],
             "missing": [], "opening": "承知しました。"},
        ]
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: fake_client(replies.pop(0)).messages.create(**kw)))
        config = make_config(tmp_path)
        schedule = ScheduleRunner(config, client=client)
        plan = runner(tmp_path, {}, {"足立": [FakeSource(REAL_9TH)]})
        handler, chatwork, _, _ = make_handler(tmp_path, monkeypatch, members=(ADACHI,), schedule=schedule, plan=plan)
        handler._config.data["admin_account_ids"] = [ADACHI]
        handler._config.data["schedule"] = config.data["schedule"]
        self._send(handler, ADACHI, "その予定で登録して", "1")
        assert "場所" not in chatwork.sent[0][1] and "日付" in chatwork.sent[0][1]
        self._send(handler, ADACHI, "下記で。\n・日付 9/9\n・件名 設備資金調達の件\n・場所 本社事務所ミーティングルーム②\n・時刻 14:30〜15:30", "2",
                   send_time=1757203300)
        assert len(writer.inserted) == 1 and writer.inserted[0].summary == "設備資金調達の件"
        assert "上記で登録しました。" in chatwork.sent[1][1]
        assert "1番で登録して" not in chatwork.sent[1][1]

    def test_registering_at_a_busy_time_is_stopped_then_picked(self, tmp_path, monkeypatch):
        register = {"events": [{"summary": "篠田さんと打合せ", "date": "2026-09-07", "start_time": "10:00",
                                "end_time": "11:00", "all_day": False, "location": ""}],
                    "missing": [], "opening": "承知しました。"}
        sources = {"足立": [FakeSource([])], "篠田": [FakeSource([ev(7, "10:00", "11:00", "来客対応")])]}
        handler, chatwork, audit, writer = self._handler(tmp_path, monkeypatch, {}, register, sources)
        self._send(handler, ADACHI, "月曜10時から篠田さんと打合せを入れて", "1")
        assert not writer.inserted
        assert "・篠田さんに「来客対応」（10:00〜11:00）が入っています" in chatwork.sent[0][1]
        assert "1. 9月7日（月）13:00〜14:00" in chatwork.sent[0][1]
        self._send(handler, ADACHI, "1番で登録して", "2", send_time=1757203300)
        assert len(writer.inserted) == 1
        assert writer.inserted[0].start == datetime(2026, 9, 7, 13, 0, tzinfo=JST)
