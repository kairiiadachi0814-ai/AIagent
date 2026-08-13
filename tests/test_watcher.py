import json
from types import SimpleNamespace

from raizuinu.answer import Answer
from raizuinu.config import Config
from raizuinu.cost import CostTracker
from raizuinu.handbook import HandbookLoader
from raizuinu.watcher import DiscussionWatcher

BOT_ID = 999


class FakeChatwork:
    def __init__(self, messages):
        self.messages = messages
        self.sent = []

    def get_me(self):
        return BOT_ID

    def get_recent_messages(self, room_id, limit=30):
        return self.messages

    def send_message(self, room_id, body):
        self.sent.append((room_id, body))
        return "1"


class FakeGenerator:
    def __init__(self, answer):
        self._answer = answer
        self.calls = []

    def generate(self, question, handbook, context=""):
        self.calls.append(question)
        return self._answer


def screen_client(suspicious):
    payload = json.dumps({"suspicious": suspicious, "reason": "テスト"})
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=payload)],
        usage=SimpleNamespace(input_tokens=100, output_tokens=20,
                              cache_creation_input_tokens=0, cache_read_input_tokens=0),
    )
    client = SimpleNamespace(calls=0)

    def create(**kwargs):
        client.calls += 1
        return response

    client.messages = SimpleNamespace(create=create)
    return client


def message(mid, account_id, name, body):
    return {"message_id": str(mid), "account": {"account_id": account_id, "name": name},
            "body": body, "send_time": 1700000000}


def make_watcher(tmp_path, messages, answer=None, suspicious=True, mode="shadow",
                 preset_last_seen=None):
    (tmp_path / "銀行明細取得.md").write_text("# 銀行明細取得\n手順\n", encoding="utf-8")
    config = Config.load(tmp_path / "no-config.json")
    config.base_dir = tmp_path
    config.data["state_dir"] = str(tmp_path / "state")
    config.data["audit_log_dir"] = str(tmp_path / "logs")
    config.data["admin_room_id"] = 55555
    config.data["discussion_watch"] = {
        "enabled": True, "room_ids": [12345], "mode": mode,
        "max_interventions_per_day": 3, "min_message_chars": 10,
        "max_batch_messages": 30,
    }
    answer = answer or Answer(
        has_answer=True,
        text="横から失礼します。経費精算の提出は月初5営業日以内のようです。",
        sources=[{"file": "銀行明細取得.md", "heading": "", "url": ""}],
        usage={"input_tokens": 10, "output_tokens": 10},
    )
    chatwork = FakeChatwork(messages)
    generator = FakeGenerator(answer)
    screen = screen_client(suspicious)
    cost = CostTracker(
        state_dir=tmp_path / "state", limit_jpy=15000.0, alert_threshold=0.7,
        usd_jpy_rate=155.0,
        pricing_usd_per_mtok={"input": 3.0, "output": 15.0, "cache_read": 0.3,
                              "cache_write_5m": 3.75, "cache_write_1h": 6.0},
    )
    watcher = DiscussionWatcher(
        config, chatwork=chatwork, generator=generator, screen_client=screen,
        cost=cost, handbook_loader=HandbookLoader([tmp_path], ["*.md"], [], 300),
    )
    if preset_last_seen is not None:
        watcher._save_state(12345, {"last_seen": preset_last_seen, "date": "", "count": 0,
                                    "cited_today": []})
    return watcher, chatwork, generator, screen


DISCUSSION = [
    message(100, 111, "坂田", "経費精算って15日までに出せばいいんだっけ？"),
    message(101, 222, "田中", "たしか15日締めだったと思いますよ"),
]


class TestFirstRun:
    def test_first_run_only_marks_read(self, tmp_path):
        watcher, chatwork, generator, screen = make_watcher(tmp_path, DISCUSSION)
        watcher.run_once()
        assert screen.calls == 0  # 初回は評価しない（過去分を裁かない）
        assert generator.calls == []
        assert chatwork.sent == []
        state = json.loads((tmp_path / "state" / "watch-12345.json").read_text(encoding="utf-8"))
        assert state["last_seen"] == 101


class TestFiltering:
    def test_bot_own_and_mention_and_short_messages_excluded(self, tmp_path):
        messages = [
            message(101, BOT_ID, "らいずいぬ", "私はボットです。長い発言をしています。"),
            message(102, 111, "坂田", f"[To:{BOT_ID}] らいずいぬさん教えて（これはQ&Aフロー）"),
            message(103, 222, "田中", "了解です"),  # 短文
        ]
        watcher, chatwork, generator, screen = make_watcher(
            tmp_path, messages, preset_last_seen=100
        )
        watcher.run_once()
        assert screen.calls == 0  # 実質的な新規発言なし → 評価に進まない


class TestScreening:
    def test_not_suspicious_skips_verification(self, tmp_path):
        watcher, chatwork, generator, screen = make_watcher(
            tmp_path, DISCUSSION, suspicious=False, preset_last_seen=99
        )
        watcher.run_once()
        assert screen.calls == 1
        assert generator.calls == []  # 2段目に進まない
        assert chatwork.sent == []


class TestIntervention:
    def test_shadow_mode_reports_to_admin_only(self, tmp_path):
        watcher, chatwork, generator, screen = make_watcher(
            tmp_path, DISCUSSION, preset_last_seen=99
        )
        watcher.run_once()
        assert len(generator.calls) == 1
        assert "経費精算って15日" in generator.calls[0]  # 議論が検証プロンプトに載る
        assert len(chatwork.sent) == 1
        room, body = chatwork.sent[0]
        assert room == 55555  # 管理者ルームのみ（対象ルームには投稿しない）
        assert "見習いモード" in body
        assert "横から失礼します" in body
        state = json.loads((tmp_path / "state" / "watch-12345.json").read_text(encoding="utf-8"))
        assert state["count"] == 1

    def test_live_mode_posts_to_room(self, tmp_path):
        watcher, chatwork, _, _ = make_watcher(
            tmp_path, DISCUSSION, mode="live", preset_last_seen=99
        )
        watcher.run_once()
        room, body = chatwork.sent[0]
        assert room == 12345
        assert body.startswith("横から失礼します")
        assert "銀行明細取得" in body  # 出典付き

    def test_silent_when_no_confirmed_error(self, tmp_path):
        no_error = Answer(has_answer=False, text="明らかな誤りはありません")
        watcher, chatwork, _, _ = make_watcher(
            tmp_path, DISCUSSION, answer=no_error, preset_last_seen=99
        )
        watcher.run_once()
        assert chatwork.sent == []  # 裏取りできなければ沈黙

    def test_daily_limit_stops_interventions(self, tmp_path):
        watcher, chatwork, generator, screen = make_watcher(
            tmp_path, DISCUSSION, preset_last_seen=99
        )
        from datetime import datetime
        from raizuinu.watcher import JST

        watcher._save_state(12345, {
            "last_seen": 99, "date": datetime.now(JST).strftime("%Y%m%d"),
            "count": 3, "cited_today": [],
        })
        watcher.run_once()
        assert screen.calls == 0  # 上限到達日は評価もしない
        assert chatwork.sent == []
