"""管理者の確認を経て、別ルームへお知らせを投稿する。"""

import json

from raizuinu.announce import Announcer
from raizuinu.config import Config

ADMIN_ROOM, FAX_ROOM = 444945031, 446282163
ADACHI, SHINODA = 6945415, 9228914


class FakeChatwork:
    def __init__(self):
        self.sent = []

    def send_message(self, room_id, body):
        self.sent.append((room_id, body))
        return str(100 + len(self.sent))

    def get_recent_messages(self, room_id, limit=20):
        return []


def make_config(tmp_path):
    config = Config.load(tmp_path / "no-config.json")
    config.base_dir = tmp_path
    config.data["state_dir"] = str(tmp_path / "state")
    config.data["admin_room_id"] = ADMIN_ROOM
    config.data["admin_account_ids"] = [ADACHI]
    return config


TEXT = "[info][title]FAXルームの運用について[/title]お疲れさまです。…[/info]"


class TestAnnouncer:
    def test_the_draft_is_shown_in_the_admin_room_first(self, tmp_path):
        cw = FakeChatwork()
        a = Announcer(make_config(tmp_path), cw)
        mid = a.propose(FAX_ROOM, TEXT, note="FAXルームの本格稼働のお知らせです。")
        assert mid == "101"
        room, body = cw.sent[0]
        assert room == ADMIN_ROOM
        assert body.startswith("FAXルームの本格稼働のお知らせです。\n次の文面をルーム 446282163 へ投稿してよいか")
        assert "「送信」" in body and TEXT in body
        assert a.pending()["target_room_id"] == FAX_ROOM

    def test_send_from_the_admin_posts_it_as_is(self, tmp_path):
        cw = FakeChatwork()
        a = Announcer(make_config(tmp_path), cw)
        a.propose(FAX_ROOM, TEXT)
        reply = a.handle(ADMIN_ROOM, ADACHI, "送信")
        assert reply == "ルーム 446282163 へ投稿しました。"
        assert cw.sent[-1] == (FAX_ROOM, TEXT)  # 一字も変えずに投稿
        assert a.pending() == {}

    def test_cancel_drops_it(self, tmp_path):
        cw = FakeChatwork()
        a = Announcer(make_config(tmp_path), cw)
        a.propose(FAX_ROOM, TEXT)
        assert "取りやめ" in a.handle(ADMIN_ROOM, ADACHI, "やっぱり取りやめで")
        assert a.pending() == {} and len(cw.sent) == 1

    def test_only_the_admin_in_the_admin_room_can_send(self, tmp_path):
        cw = FakeChatwork()
        a = Announcer(make_config(tmp_path), cw)
        a.propose(FAX_ROOM, TEXT)
        assert a.handle(ADMIN_ROOM, SHINODA, "送信") is None
        assert a.handle(384793683, ADACHI, "送信") is None
        assert len(cw.sent) == 1 and a.pending()

    def test_other_talk_is_left_alone(self, tmp_path):
        cw = FakeChatwork()
        a = Announcer(make_config(tmp_path), cw)
        a.propose(FAX_ROOM, TEXT)
        assert a.handle(ADMIN_ROOM, ADACHI, "2段落目の言い回しを直して") is None
        assert a.pending()

    def test_nothing_pending_means_nothing_happens(self, tmp_path):
        a = Announcer(make_config(tmp_path), FakeChatwork())
        assert a.handle(ADMIN_ROOM, ADACHI, "送信") is None

    def test_an_old_draft_expires(self, tmp_path):
        cw = FakeChatwork()
        a = Announcer(make_config(tmp_path), cw, ttl_hours=1)
        a.propose(FAX_ROOM, TEXT)
        stale = json.loads(a._path.read_text(encoding="utf-8"))
        stale["proposed_at"] = "2026-09-01T09:00:00+09:00"
        a._path.write_text(json.dumps(stale), encoding="utf-8")
        assert a.handle(ADMIN_ROOM, ADACHI, "送信") is None
        assert len(cw.sent) == 1


class TestThroughTheHandler:
    def test_the_admins_reply_in_the_admin_room_posts_the_notice(self, tmp_path, monkeypatch):
        from tests.test_guest import make_handler
        from tests.test_handler import sign

        handler, chatwork, generator, audit = make_handler(tmp_path, monkeypatch, members=(ADACHI,))
        handler._config.data["allowed_room_ids"] = [ADMIN_ROOM]
        handler._config.data["admin_room_id"] = ADMIN_ROOM
        handler._config.data["admin_account_ids"] = [ADACHI]
        announcer = Announcer(handler._config, chatwork)
        handler._announcer = announcer
        announcer.propose(FAX_ROOM, TEXT)
        raw = json.dumps({
            "webhook_event_type": "mention_to_me",
            "webhook_event": {"from_account_id": ADACHI, "to_account_id": 999, "room_id": ADMIN_ROOM,
                              "message_id": "9", "body": "[rp aid=999 to=444945031-101] 送信", "send_time": 1757240580},
        }).encode()
        handler.handle_webhook(raw, sign(raw))
        assert (FAX_ROOM, TEXT) in chatwork.sent
        assert "投稿しました" in chatwork.sent[-1][1]
        assert not generator.calls
        assert audit.records[-1]["type"] == "announce"
