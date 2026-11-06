"""期日の進捗確認（経理財務部業務共有チャットだけ）。"""

import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from raizuinu.config import Config
from raizuinu.deadline import (
    JST,
    DeadlineFollower,
    DeadlineRunner,
    DeadlineStore,
    item_line,
    looks_like_deadline_message,
    office_days_between,
)

ROOM = 384793683
ADACHI, SHINODA, NAKAURA = 6945415, 9228914, 10622368
HOLIDAYS = {"2026-09-21", "2026-09-22", "2026-09-23"}


def at(*args):
    return datetime(*args, tzinfo=JST)


MON = at(2026, 9, 7, 10, 0)


def make_config(tmp_path):
    config = Config.load(tmp_path / "no-config.json")
    config.base_dir = tmp_path
    config.data["state_dir"] = str(tmp_path / "state")
    config.data["audit_log_dir"] = str(tmp_path / "logs")
    config.data["deadline"] = {"enabled": True, "room_id": ROOM, "check_days_before": [3, 1, 0],
                               "check_time": "09:00", "overdue_days": 3, "remember_days": 60}
    config.data["schedule"] = {
        "enabled": True, "owner_name": "足立",
        "members": [
            {"name": "足立", "account_id": ADACHI, "owner": True},
            {"name": "篠田", "account_id": SHINODA, "work_days": [0, 3, 4]},
            {"name": "中浦", "account_id": NAKAURA, "work_days": [0, 1, 2]},
        ],
    }
    return config


class FakeChatwork:
    def __init__(self):
        self.sent = []

    def send_message(self, room_id, body):
        self.sent.append((room_id, body))
        return str(9000 + len(self.sent) - 1)

    def get_recent_messages(self, room_id, limit=20):
        return []


def fake_client(fields):
    base = {"action": "other", "title": "", "due_date": "", "due_time": "", "assignees": [],
            "target_id": 0, "note": "", "reply": ""}
    text = json.dumps({**base, **fields}, ensure_ascii=False)
    client = SimpleNamespace(kwargs=None, calls=0)

    def create(**kwargs):
        client.kwargs = kwargs
        client.calls += 1
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=300, output_tokens=60,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=0),
        )

    client.messages = SimpleNamespace(create=create)
    return client


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def runner(tmp_path, fields, now=MON, chatwork=None):
    clock = Clock(now)
    r = DeadlineRunner(make_config(tmp_path), chatwork or FakeChatwork(), client=fake_client(fields),
                       now=clock, holidays=HOLIDAYS)
    r.clock = clock
    return r


def follower(tmp_path, chatwork, now):
    clock = Clock(now)
    f = DeadlineFollower(make_config(tmp_path), chatwork, now=clock, holidays=HOLIDAYS)
    f.clock = clock
    return f


REGISTER = {"action": "register", "title": "決算資料の修正", "due_date": "2026-09-12", "assignees": ["篠田"],
            "reply": "承知しました。"}


class TestDetection:
    @pytest.mark.parametrize("q", [
        "9/12までに決算資料の修正、篠田さんの進捗見といて",
        "来週金曜が期日のアポ、進捗確認して",
        "期日一覧教えて",
        "控えている期日ある？",
        "月末締切の資料、リマインドお願い",
    ])
    def test_related(self, q):
        assert looks_like_deadline_message(q)

    @pytest.mark.parametrize("q", [
        "有効期限は？", "請求書の締切はいつ？", "経費精算の締め日は？", "足立さんの今日の予定教えて", "了解です",
    ])
    def test_not_related(self, q):
        assert not looks_like_deadline_message(q)


class TestOfficeDays:
    def test_counting_skips_weekends_and_holidays(self):
        assert office_days_between(date(2026, 9, 3), date(2026, 9, 4), HOLIDAYS) == 1
        assert office_days_between(date(2026, 9, 4), date(2026, 9, 7), HOLIDAYS) == 1  # 金→月
        assert office_days_between(date(2026, 9, 7), date(2026, 9, 12), HOLIDAYS) == 4  # 8〜11日。12日は土曜
        assert office_days_between(date(2026, 9, 18), date(2026, 9, 24), HOLIDAYS) == 1  # 連休を飛ばす
        assert office_days_between(date(2026, 9, 7), date(2026, 9, 4), HOLIDAYS) == -1
        assert office_days_between(date(2026, 9, 7), date(2026, 9, 7), HOLIDAYS) == 0

    def test_item_line(self):
        item = {"title": "決算資料の修正", "due": "2026-09-11", "assignees": [{"name": "篠田"}]}
        assert item_line(item, date(2026, 9, 7), HOLIDAYS) == "9月11日（金）まで　決算資料の修正（担当: 篠田さん・あと4営業日）"
        assert "今日が期日" in item_line(item, date(2026, 9, 11), HOLIDAYS)
        assert "期日を1営業日過ぎています" in item_line(item, date(2026, 9, 14), HOLIDAYS)


class TestRegister:
    def test_a_deadline_is_recorded_and_the_plan_is_explained(self, tmp_path):
        r = runner(tmp_path, REGISTER)
        reply, meta, usage = r.handle(ROOM, ADACHI, "9/12までに決算資料の修正、篠田さんの進捗見といて")
        assert reply.startswith("承知しました。次の期日を控えました。")
        assert "・9月12日（土）まで　決算資料の修正（担当: 篠田さん・あと4営業日）" in reply
        assert "3営業日前・前日・当日の朝に、篠田さんへ進捗を伺います。済んだら「完了」と返信してください。" in reply
        item = r._store.load()["items"][0]
        assert item["assignees"] == [{"account_id": SHINODA, "name": "篠田"}]
        assert item["requester_id"] == ADACHI and item["status"] == "open"
        assert meta["item_id"] == 1 and usage["input_tokens"] == 300
        assert "篠田" in r._client.kwargs["system"]  # 担当の一覧を渡す

    def test_without_an_assignee_the_requester_is_in_charge(self, tmp_path):
        r = runner(tmp_path, {**REGISTER, "assignees": []})
        reply, _, _ = r.handle(ROOM, ADACHI, "9/12までに決算資料の修正、進捗管理して")
        assert "担当: 足立さん" in reply

    def test_a_missing_due_date_is_asked(self, tmp_path):
        r = runner(tmp_path, {**REGISTER, "due_date": ""})
        reply, meta, _ = r.handle(ROOM, ADACHI, "決算資料の修正の進捗を追って")
        assert "期日" in reply and meta["error"] == "missing"
        assert r._store.load()["items"] == []

    def test_registering_close_to_the_day_only_promises_what_is_left(self, tmp_path):
        r = runner(tmp_path, {**REGISTER, "due_date": "2026-09-09"}, now=at(2026, 9, 8, 10, 0))  # 前日に登録
        reply, _, _ = r.handle(ROOM, ADACHI, "明日までの決算資料の修正、篠田さんの進捗見といて")
        assert "当日の朝に、篠田さんへ進捗を伺います" in reply and "前日" not in reply

    def test_the_confirmation_message_is_linked_for_replies(self, tmp_path):
        r = runner(tmp_path, REGISTER)
        _, meta, _ = r.handle(ROOM, ADACHI, "9/12までに決算資料の修正、篠田さんの進捗見といて")
        r.remember_message(meta, "9000")
        assert r.is_reply_to_ours("9000")["title"] == "決算資料の修正"
        assert r.looks_related("完了です", "9000")


def registered(tmp_path, now=MON):
    chatwork = FakeChatwork()
    r = runner(tmp_path, REGISTER, now=now, chatwork=chatwork)
    _, meta, _ = r.handle(ROOM, ADACHI, "9/12までに決算資料の修正、篠田さんの進捗見といて")
    r.remember_message(meta, "9000")
    return r, chatwork


class TestFollowUp:
    def test_checks_come_three_days_before_the_day_before_and_on_the_day(self, tmp_path):
        r, chatwork = registered(tmp_path)  # 月曜に登録。期日 9/12（土）→ 営業日で数える
        f = follower(tmp_path, chatwork, at(2026, 9, 8, 8, 55))  # 火 9時前
        assert f.run_once() == 0
        f.clock.now = at(2026, 9, 8, 9, 0)  # 火: あと3営業日（水木金）
        assert f.run_once() == 1
        body = chatwork.sent[-1][1]
        assert body.startswith(f"[To:{SHINODA}]\nおはようございます。「決算資料の修正」の期日が9月12日（土）で、あと3営業日です。進捗はいかがでしょうか。")
        assert "「完了」とお知らせください" in body
        f.clock.now = at(2026, 9, 8, 9, 30)
        assert f.run_once() == 0  # 同じ日に二度は聞かない
        f.clock.now = at(2026, 9, 9, 9, 0)  # 水: あと2営業日 → 聞かない
        assert f.run_once() == 0
        f.clock.now = at(2026, 9, 10, 9, 0)  # 木: あと1営業日
        assert f.run_once() == 1 and "あと1営業日" in chatwork.sent[-1][1]
        f.clock.now = at(2026, 9, 11, 9, 0)  # 金: 期日は土曜なので、営業日は今日が最後
        assert f.run_once() == 1 and "営業日は今日が最後です" in chatwork.sent[-1][1]
        f.clock.now = at(2026, 9, 12, 9, 0)  # 土（期日）: 営業日でないので聞かない
        assert f.run_once() == 0
        f.clock.now = at(2026, 9, 14, 9, 0)  # 月: 期日を過ぎた
        assert f.run_once() == 1 and "期日（9月12日（土））を過ぎています" in chatwork.sent[-1][1]
        f.clock.now = at(2026, 9, 15, 9, 0)
        assert f.run_once() == 1 and "過ぎています" in chatwork.sent[-1][1]
        f.clock.now = at(2026, 9, 16, 9, 0)
        assert f.run_once() == 1  # 3営業日目まで
        f.clock.now = at(2026, 9, 17, 9, 0)
        assert f.run_once() == 0  # それ以上は聞かない
        item = r._store.load()["items"][0]
        assert len(item["message_ids"]) == 1 + 6  # 登録の確認 + 確認6通（3日前・前日・当日・期日後3日）

    def test_a_weekday_due_date_gets_the_on_the_day_check(self, tmp_path):
        chatwork = FakeChatwork()
        r = runner(tmp_path, {**REGISTER, "due_date": "2026-09-11"}, chatwork=chatwork)
        r.handle(ROOM, ADACHI, "9/11までに決算資料の修正、篠田さんの進捗見といて")
        f = follower(tmp_path, chatwork, at(2026, 9, 11, 9, 0))
        assert f.run_once() == 1
        assert "「決算資料の修正」は今日（9月11日（金））が期日です。" in chatwork.sent[-1][1]

    def test_no_checks_for_done_items(self, tmp_path):
        r, chatwork = registered(tmp_path)
        r._client = fake_client({"action": "done", "target_id": 1, "reply": "お疲れさまでした。"})
        r.handle(ROOM, SHINODA, "完了しました", reply_target="9000")
        f = follower(tmp_path, chatwork, at(2026, 9, 9, 9, 0))
        assert f.run_once() == 0


class TestReports:
    def test_done_closes_and_tells_the_requester(self, tmp_path):
        r, chatwork = registered(tmp_path)
        r._client = fake_client({"action": "done", "target_id": 1, "reply": "お疲れさまでした。"})
        reply, meta, _ = r.handle(ROOM, SHINODA, "完了しました", reply_target="9000")
        assert reply.startswith(f"[To:{ADACHI}]\nお疲れさまでした。")
        assert "「決算資料の修正」を控えから外しました。" in reply
        assert "篠田さんから完了の報告がありました。" in reply
        assert r._store.load()["items"][0]["status"] == "done"
        assert "reply_to: ID 1" in r._client.kwargs["messages"][0]["content"]

    def test_done_by_the_requester_does_not_call_themselves(self, tmp_path):
        r, _ = registered(tmp_path)
        r._client = fake_client({"action": "done", "target_id": 1, "reply": "承知しました。"})
        reply, _, _ = r.handle(ROOM, ADACHI, "決算資料の修正、完了で")
        assert "[To:" not in reply and "控えから外しました" in reply

    def test_progress_is_noted_and_the_next_check_is_announced(self, tmp_path):
        r, _ = registered(tmp_path)
        r._client = fake_client({"action": "progress", "target_id": 1, "note": "半分まで進んでいる", "reply": "承知しました。"})
        reply, meta, _ = r.handle(ROOM, SHINODA, "半分くらいまで進んでます", reply_target="9000")
        assert reply == "承知しました。\n次は3営業日前の朝に伺います。引き続きよろしくお願いします。"
        assert r._store.load()["items"][0]["progress"][0]["note"] == "半分まで進んでいる"

    def test_postpone_moves_the_date_and_resets_checks(self, tmp_path):
        r, chatwork = registered(tmp_path)
        f = follower(tmp_path, chatwork, at(2026, 9, 9, 9, 0))
        f.run_once()  # 3営業日前の確認が出た
        r._client = fake_client({"action": "postpone", "target_id": 1, "due_date": "2026-09-18", "reply": "承知しました。"})
        r.clock.now = at(2026, 9, 9, 10, 0)
        reply, _, _ = r.handle(ROOM, ADACHI, "決算資料の修正、9/18に延期で")
        assert "「決算資料の修正」の期日を9月12日（土）から9月18日（金）に変えました。" in reply
        assert "3営業日前・前日・当日の朝に、改めて進捗を伺います。" in reply
        assert r._store.load()["items"][0]["checks"] == {}

    def test_cancel(self, tmp_path):
        r, _ = registered(tmp_path)
        r._client = fake_client({"action": "cancel", "target_id": 1, "reply": "承知しました。"})
        reply, _, _ = r.handle(ROOM, ADACHI, "決算資料の修正の期日管理、取り消して")
        assert "「決算資料の修正」の控えを取り消しました。" in reply
        assert r._store.load()["items"][0]["status"] == "cancelled"

    def test_list(self, tmp_path):
        r, _ = registered(tmp_path)
        r._client = fake_client({"action": "list"})
        reply, _, _ = r.handle(ROOM, ADACHI, "期日一覧教えて")
        assert reply.startswith("控えている期日は1件です。\n1. 9月12日（土）まで　決算資料の修正（担当: 篠田さん・あと4営業日）")

    def test_an_unclear_target_asks_by_number(self, tmp_path):
        r, _ = registered(tmp_path)
        r._client = fake_client({"action": "register", "title": "銀行提出資料", "due_date": "2026-09-15", "assignees": ["中浦"]})
        r.handle(ROOM, ADACHI, "9/15までに銀行提出資料、中浦さんの進捗見といて")
        r._client = fake_client({"action": "done", "target_id": 0, "reply": "お疲れさまでした。"})
        reply, meta, _ = r.handle(ROOM, ADACHI, "完了です")
        assert reply.startswith("どの期日の話か分からなかったので、番号か件名でお知らせください。")
        assert "1. " in reply and "2. " in reply and meta["error"] == "no_target"

    def test_unrelated_talk_is_left_to_the_normal_flow(self, tmp_path):
        r = runner(tmp_path, {"action": "other", "reply": "はい。"})
        assert r.handle(ROOM, ADACHI, "来週の期日、確認して") is None

    def test_the_prompt_forbids_making_up_dates(self, tmp_path):
        r, _ = registered(tmp_path)
        system = r._client.kwargs["system"]
        assert "期日・担当を推測で作らない" in system and "日付・営業日数・件数を書かない" in system


class TestThroughTheHandler:
    def _handler(self, tmp_path, monkeypatch, fields, now=MON):
        from tests.test_guest import make_handler

        handler, chatwork, generator, audit = make_handler(tmp_path, monkeypatch, members=(ADACHI, SHINODA))
        handler._config.data["allowed_room_ids"] = [ROOM, 444945031]
        config = make_config(tmp_path)
        handler._config.data["deadline"] = config.data["deadline"]
        handler._config.data["schedule"] = config.data["schedule"]
        handler._deadline = DeadlineRunner(handler._config, chatwork, client=fake_client(fields),
                                           now=lambda: now, holidays=HOLIDAYS)
        handler._schedule = None
        return handler, chatwork, generator, audit

    @staticmethod
    def _send(handler, account_id, text, room=ROOM, message_id="1"):
        from tests.test_handler import sign

        raw = json.dumps({
            "webhook_event_type": "mention_to_me",
            "webhook_event": {"from_account_id": account_id, "to_account_id": 999, "room_id": room,
                              "message_id": message_id, "body": text, "send_time": 1757203200},
        }).encode()
        handler.handle_webhook(raw, sign(raw))

    def test_registering_in_the_department_room(self, tmp_path, monkeypatch):
        handler, chatwork, generator, audit = self._handler(tmp_path, monkeypatch, REGISTER)
        self._send(handler, ADACHI, "[To:999] 9/12までに決算資料の修正、篠田さんの進捗見といて")
        assert "次の期日を控えました" in chatwork.sent[0][1]
        assert not generator.calls
        assert audit.records[-1]["type"] == "deadline" and audit.records[-1]["detail"]["item_id"] == 1
        assert handler._deadline._store.load()["items"][0]["message_ids"] == ["1"]  # 返信の紐付け

    def test_other_rooms_are_not_covered(self, tmp_path, monkeypatch):
        handler, chatwork, generator, _ = self._handler(tmp_path, monkeypatch, REGISTER)
        self._send(handler, ADACHI, "[To:999] 9/12までに決算資料の修正、篠田さんの進捗見といて", room=444945031)
        assert handler._deadline._store.load()["items"] == []
        assert generator.calls  # 通常のQ&Aへ

    def test_a_reply_to_the_check_reports_done(self, tmp_path, monkeypatch):
        handler, chatwork, _, _ = self._handler(tmp_path, monkeypatch, REGISTER)
        self._send(handler, ADACHI, "[To:999] 9/12までに決算資料の修正、篠田さんの進捗見といて")
        handler._deadline._client = fake_client({"action": "done", "target_id": 1, "reply": "お疲れさまでした。"})
        self._send(handler, SHINODA, f"[rp aid=999 to={ROOM}-1] 完了しました", message_id="2")
        assert "控えから外しました" in chatwork.sent[-1][1]

    def test_a_knowledge_question_with_the_word_kigen_stays_a_question(self, tmp_path, monkeypatch):
        handler, chatwork, generator, _ = self._handler(tmp_path, monkeypatch, {"action": "other"})
        self._send(handler, ADACHI, "[To:999] 印紙の有効期限は？")
        assert generator.calls  # 期日の機能は反応しない
