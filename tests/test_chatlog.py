"""チャットの過去ログからの回答。

実例: 「楽天BillPayのパスワードは？」— ハンドブックには無く、チャットの
どこかで共有された値。パスワードが何度か変わっている場合は最新を正とする。
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from raizuinu.chatlog import ChatArchive, ChatLogAnswerer, search_terms
from raizuinu.config import Config

JST = timezone(timedelta(hours=9))
ROOM = 384793683
OTHER_ROOM = 444781726


def at(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=JST).timestamp())


def message(message_id, body, when, name="坂田 美穂", account_id=8681926):
    return {
        "message_id": str(message_id),
        "body": body,
        "send_time": at(when),
        "account": {"account_id": account_id, "name": name},
    }


def make_config(tmp_path, rooms=(ROOM,)):
    config = Config.load(tmp_path / "no-config.json")
    config.data["chat_archive"] = {
        "enabled": True, "room_ids": list(rooms), "retention_days": 730, "max_hits": 8,
    }
    config.data["state_dir"] = str(tmp_path / "state")
    config.base_dir = tmp_path
    return config


def fake_client(payload):
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))],
        usage=SimpleNamespace(
            input_tokens=800, output_tokens=100,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        ),
    )
    client = SimpleNamespace(kwargs=None)

    def create(**kwargs):
        client.kwargs = kwargs
        return response

    client.messages = SimpleNamespace(create=create)
    return client


def archive(tmp_path):
    return ChatArchive(tmp_path / "chatlog.sqlite3")


class TestSearchTerms:
    def test_the_subject_words_are_picked_out(self):
        assert search_terms("楽天BillPayのパスワード教えて") == ["楽天", "BillPay", "パスワード"]

    def test_polite_filler_is_dropped(self):
        terms = search_terms("すみません、freeeのログインIDを確認したいのでお願いします")
        assert "freee" in terms
        assert not any(t.startswith("お願") or t.startswith("確認") for t in terms)

    def test_single_characters_are_not_searched(self):
        # 1文字の漢字で引くと、関係ない発言が大量に当たる
        assert "の" not in search_terms("車の鍵はどこ")
        assert all(len(t) >= 2 for t in search_terms("車の鍵はどこ"))


class TestArchive:
    def test_messages_are_stored_and_found(self, tmp_path):
        store = archive(tmp_path)
        assert store.record(ROOM, [message(1, "楽天BillPayのパスワードはabc123です", "2026-05-12 10:00")]) == 1
        hits = store.search(ROOM, ["楽天", "BillPay", "パスワード"])
        assert len(hits) == 1
        assert hits[0]["body"].endswith("abc123です")

    def test_the_same_message_is_not_stored_twice(self, tmp_path):
        # 巡回は直近100件を毎回取り直すので、重複は必ず起きる
        store = archive(tmp_path)
        batch = [message(1, "テスト", "2026-05-12 10:00")]
        assert store.record(ROOM, batch) == 1
        assert store.record(ROOM, batch) == 0
        assert store.stats()["count"] == 1

    def test_the_newest_comes_first(self, tmp_path):
        store = archive(tmp_path)
        store.record(ROOM, [
            message(1, "楽天BillPayのパスワードは old111 です", "2026-01-10 10:00"),
            message(2, "楽天BillPayのパスワードを new222 に変更しました", "2026-06-20 09:00"),
            message(3, "楽天BillPayのパスワードを mid333 に変更しました", "2026-03-05 14:00"),
        ])
        hits = store.search(ROOM, ["楽天", "BillPay", "パスワード"])
        assert [h["message_id"] for h in hits] == ["2", "3", "1"]

    def test_other_rooms_are_never_searched(self, tmp_path):
        # そのルームの人しか見られない情報を、外のルームへ渡さない
        store = archive(tmp_path)
        store.record(OTHER_ROOM, [message(9, "楽天BillPayのパスワードはzzz999", "2026-05-12 10:00")])
        assert store.search(ROOM, ["楽天", "BillPay"]) == []

    def test_empty_messages_are_skipped(self, tmp_path):
        store = archive(tmp_path)
        assert store.record(ROOM, [message(1, "   ", "2026-05-12 10:00")]) == 0

    def test_old_messages_are_pruned(self, tmp_path):
        store = ChatArchive(tmp_path / "chatlog.sqlite3", retention_days=30)
        old = datetime.now(JST) - timedelta(days=90)
        store.record(ROOM, [
            {"message_id": "1", "body": "古い話", "send_time": int(old.timestamp()),
             "account": {"account_id": 1, "name": "誰か"}},
            {"message_id": "2", "body": "最近の話",
             "send_time": int(datetime.now(JST).timestamp()),
             "account": {"account_id": 1, "name": "誰か"}},
        ])
        assert store.prune() == 1
        assert store.stats()["count"] == 1


class TestLookup:
    def setup_room(self, tmp_path):
        store = archive(tmp_path)
        store.record(ROOM, [
            message(1, "楽天BillPayのパスワードは old111 です", "2026-01-10 10:00"),
            message(2, "楽天BillPayのパスワードを new222 に変更しました", "2026-06-20 09:00",
                    name="足立 海里"),
            message(3, "本日の経費精算はお早めに", "2026-06-21 09:00"),
        ])
        return store

    def test_the_latest_value_is_used_and_disclosed(self, tmp_path):
        store = self.setup_room(tmp_path)
        answerer = ChatLogAnswerer(
            make_config(tmp_path), store,
            client=fake_client({
                "has_answer": True,
                "answer": "楽天BillPayのパスワードは new222 です。",
                "used_index": 0, "superseded": True,
            }),
        )
        text, meta, usage = answerer.lookup(ROOM, "楽天BillPayのパスワードは？")
        assert "new222" in text
        assert "old111" not in text
        # 過去ログから答えたことを必ず書く（ご指示）
        assert "過去のやり取りから拾っています" in text
        assert "2026年6月20日 足立 海里さんの発言" in text
        assert "いちばん新しいものを採っています" in text
        assert meta["used_message_id"] == "2"
        assert usage["input_tokens"] == 800

    def test_the_model_is_shown_the_newest_first(self, tmp_path):
        store = self.setup_room(tmp_path)
        client = fake_client({"has_answer": False, "answer": "", "used_index": -1,
                              "superseded": False})
        ChatLogAnswerer(make_config(tmp_path), store, client=client).lookup(
            ROOM, "楽天BillPayのパスワードは？"
        )
        prompt = client.kwargs["messages"][0]["content"]
        assert prompt.index("new222") < prompt.index("old111")
        assert "いちばん新しい発言を正とする" in client.kwargs["system"]

    def test_nothing_found_returns_empty_so_the_normal_reply_stands(self, tmp_path):
        store = self.setup_room(tmp_path)
        answerer = ChatLogAnswerer(
            make_config(tmp_path), store,
            client=fake_client({"has_answer": False, "answer": "", "used_index": -1,
                                "superseded": False}),
        )
        text, meta, _ = answerer.lookup(ROOM, "楽天BillPayのパスワードは？")
        assert text == ""

    def test_a_room_with_no_hits_costs_nothing(self, tmp_path):
        store = self.setup_room(tmp_path)
        client = fake_client({"has_answer": True, "answer": "x", "used_index": 0,
                              "superseded": False})
        answerer = ChatLogAnswerer(make_config(tmp_path), store, client=client)
        text, meta, usage = answerer.lookup(ROOM, "南極の氷の厚さは？")
        assert (text, usage) == ("", {})
        assert client.kwargs is None  # 当たりが無ければAPIを呼ばない

    def test_rooms_outside_the_archive_are_not_searched(self, tmp_path):
        store = self.setup_room(tmp_path)
        client = fake_client({"has_answer": True, "answer": "x", "used_index": 0,
                              "superseded": False})
        answerer = ChatLogAnswerer(make_config(tmp_path, rooms=(ROOM,)), store, client=client)
        text, meta, usage = answerer.lookup(OTHER_ROOM, "楽天BillPayのパスワードは？")
        assert (text, usage) == ("", {})
        assert client.kwargs is None


class TestArchiving:
    def test_the_watcher_records_configured_rooms_only(self, tmp_path):
        from raizuinu.watcher import archive_rooms

        polled = []

        class FakeChatwork:
            def get_recent_messages(self, room_id, limit=20):
                polled.append(room_id)
                return [message(int(room_id), f"ルーム{room_id}の発言", "2026-06-20 09:00")]

        config = make_config(tmp_path, rooms=(ROOM,))
        added = archive_rooms(config, FakeChatwork())
        assert polled == [ROOM]
        assert added == {ROOM: 1}

    def test_it_does_nothing_when_disabled(self, tmp_path):
        from raizuinu.watcher import archive_rooms

        config = make_config(tmp_path)
        config.data["chat_archive"]["enabled"] = False
        assert archive_rooms(config, object()) == {}

    def test_a_failing_room_does_not_stop_the_others(self, tmp_path):
        from raizuinu.watcher import archive_rooms

        class FlakyChatwork:
            def get_recent_messages(self, room_id, limit=20):
                if int(room_id) == ROOM:
                    raise RuntimeError("HTTP 500")
                return [message(7, "別ルームの発言", "2026-06-20 09:00")]

        config = make_config(tmp_path, rooms=(ROOM, OTHER_ROOM))
        assert archive_rooms(config, FlakyChatwork()) == {OTHER_ROOM: 1}


class TestSecretsAreNotCopiedAround:
    def test_the_audit_record_does_not_carry_the_answer(self, tmp_path, monkeypatch):
        """パスワードを監査ログへ写すと、秘密の置き場が増える。"""
        import base64
        import hashlib
        import hmac

        from tests.test_handler import FakeAudit, FakeChatwork, FakeGenerator
        from raizuinu.handbook import HandbookLoader
        from raizuinu.handler import RaizuinuHandler

        token = base64.b64encode(b"chatlog-key").decode()
        monkeypatch.setenv("CHATWORK_WEBHOOK_TOKEN", token)
        (tmp_path / "手順.md").write_text("# 手順\n本文\n", encoding="utf-8")
        config = make_config(tmp_path)
        config.data.update({
            "allowed_room_ids": [ROOM], "webhook_async": False,
            "audit_log_dir": str(tmp_path / "logs"),
        })
        store = ChatArchive(tmp_path / "state" / "chatlog.sqlite3")
        store.record(ROOM, [message(1, "楽天BillPayのパスワードは new222 です", "2026-06-20 09:00")])
        answerer = ChatLogAnswerer(
            config, store,
            client=fake_client({"has_answer": True, "answer": "パスワードは new222 です。",
                                "used_index": 0, "superseded": False}),
        )
        chatwork = FakeChatwork()
        audit = FakeAudit()
        answer = SimpleNamespace(
            has_answer=False, text="ハンドブックに記載がありません。", sources=[],
            usage={"input_tokens": 10, "output_tokens": 5}, refused=False,
            intent="question", reported_manuals=[], reference_url="", stage2="",
            suggested_file="",
        )
        handler = RaizuinuHandler(
            config, chatwork=chatwork, generator=FakeGenerator(answer), audit=audit,
            handbook_loader=HandbookLoader([tmp_path], ["*.md"], [], 300),
            chatlog=answerer,
        )
        body = json.dumps({
            "webhook_event_type": "mention_to_me",
            "webhook_event": {
                "from_account_id": 8681926, "to_account_id": 999, "room_id": ROOM,
                "message_id": "700001", "body": "[To:999] 楽天BillPayのパスワードは？",
                "send_time": 1700000000,
            },
        }).encode()
        digest = hmac.new(base64.b64decode(token), body, hashlib.sha256).digest()
        handler.handle_webhook(body, base64.b64encode(digest).decode())

        assert "new222" in chatwork.sent[-1][1]  # 依頼者には実物を返す
        record = audit.records[-1]
        assert record["type"] == "chatlog_answer"
        assert "new222" not in json.dumps(record, ensure_ascii=False)


class TestOneFetchPerRoom:
    """巡回では3つの処理が同じルームを見に行く。取得は1回にまとめる。"""

    def test_the_cache_fetches_each_room_once(self):
        from raizuinu.chatwork import OneShotCache

        calls = []

        class FakeClient:
            def get_recent_messages(self, room_id, limit=20):
                calls.append((room_id, limit))
                return [message(i, f"発言{i}", "2026-06-20 09:00") for i in range(100)]

            def get_me(self):
                calls.append("me")
                return 11574026

            def send_message(self, room_id, body):
                return "1"

        cache = OneShotCache(FakeClient())
        # 件数が違っても取り直さない（本体は常に100件取って切って返す）
        assert len(cache.get_recent_messages(ROOM, limit=30)) == 30
        assert len(cache.get_recent_messages(ROOM, limit=100)) == 100
        assert len(cache.get_recent_messages(ROOM, limit=50)) == 50
        cache.get_me()
        cache.get_me()
        assert calls == [(ROOM, 100), "me"]  # ルーム1回、自分の確認も1回

    def test_different_rooms_are_fetched_separately(self):
        from raizuinu.chatwork import OneShotCache

        calls = []

        class FakeClient:
            def get_recent_messages(self, room_id, limit=20):
                calls.append(room_id)
                return []

        cache = OneShotCache(FakeClient())
        cache.get_recent_messages(ROOM)
        cache.get_recent_messages(OTHER_ROOM)
        cache.get_recent_messages(ROOM)
        assert calls == [ROOM, OTHER_ROOM]

    def test_other_operations_pass_straight_through(self):
        from raizuinu.chatwork import OneShotCache

        sent = []

        class FakeClient:
            def send_message(self, room_id, body):
                sent.append((room_id, body))
                return "999"

        cache = OneShotCache(FakeClient())
        assert cache.send_message(ROOM, "本文") == "999"
        assert sent == [(ROOM, "本文")]

    def test_a_whole_run_touches_the_shared_room_once(self, tmp_path, monkeypatch):
        """過去ログの保存と議論の巡回が、同じルームを2回取りに行かないこと。"""
        from raizuinu.chatwork import OneShotCache
        from raizuinu.watcher import DiscussionWatcher, archive_rooms

        calls = []

        class FakeClient:
            def get_recent_messages(self, room_id, limit=20):
                calls.append(room_id)
                return [message(1, "共有された発言です", "2026-06-20 09:00")]

            def get_me(self):
                return 999

        config = make_config(tmp_path, rooms=(ROOM,))
        config.data["discussion_watch"] = {
            "enabled": True, "room_ids": [ROOM], "mode": "shadow",
            "max_batch_messages": 30, "min_message_chars": 10,
        }
        chatwork = OneShotCache(FakeClient())
        archive_rooms(config, chatwork)
        DiscussionWatcher(
            config, chatwork=chatwork, generator=object(), screen_client=object(),
            handbook_loader=object(),
        ).run_once()
        assert calls == [ROOM]  # 2回ではなく1回


class TestItReadsLikeAPerson:
    """実例（2026-09-04）: 「au payマーケットのパスワードってなに？」に対し、
    パスワードだけを1行で返してしまった。人の受け答えとして噛み合わない。
    """

    def test_the_prompt_forbids_returning_a_bare_value(self):
        from raizuinu.chatlog import LOOKUP_SYSTEM

        assert "値だけを書いてはならない" in LOOKUP_SYSTEM
        assert "です・ます調" in LOOKUP_SYSTEM
        assert "書き出しは毎回変える" in LOOKUP_SYSTEM

    @pytest.mark.parametrize(
        "answer,expected",
        [
            ("hB6HdhjT0b", "hB6HdhjT0b です。"),          # 値だけ返ってきたら文にする
            ("rise-keiri / xxxx", "rise-keiri / xxxx です。"),
            ("au payマーケットのパスワードは hB6HdhjT0b です。",
             "au payマーケットのパスワードは hB6HdhjT0b です。"),  # 文ならそのまま
            ("", ""),
        ],
    )
    def test_a_bare_value_is_made_into_a_sentence(self, answer, expected):
        from raizuinu.chatlog import as_sentence

        assert as_sentence(answer) == expected

    @pytest.mark.parametrize(
        "display,expected",
        [
            ("足立 海里　資料作成集中（急ぎ案件のみ対応可）　※土日休", "足立 海里"),
            ("坂田 美穂　休:土日祝", "坂田 美穂"),
            ("坂口　美代子【土・日曜日◆祝日】7月2日休み", "坂口 美代子"),
            ("福本　明日香 (休)土日祝", "福本 明日香"),
            ("札葉美早㊡土日祝", "札葉美早"),
            ("中浦 祐子", "中浦 祐子"),
        ],
    )
    def test_work_status_is_stripped_from_the_name(self, display, expected):
        # 「足立 海里　資料作成集中（急ぎ案件のみ対応可）　※土日休さんの発言より」は読みづらい
        from raizuinu.chatlog import clean_name

        assert clean_name(display) == expected

    def test_the_whole_reply_reads_as_a_sentence(self, tmp_path):
        store = archive(tmp_path)
        store.record(ROOM, [
            message(1, "au payマーケットのパスワードは hB6HdhjT0b です", "2026-09-01 10:00",
                    name="足立 海里　資料作成集中（急ぎ案件のみ対応可）　※土日休"),
        ])
        answerer = ChatLogAnswerer(
            make_config(tmp_path), store,
            client=fake_client({"has_answer": True, "answer": "hB6HdhjT0b",
                                "used_index": 0, "superseded": False}),
        )
        text, _, _ = answerer.lookup(ROOM, "au payマーケットのパスワードってなに？")
        head = text.splitlines()[0]
        assert head == "hB6HdhjT0b です。"       # 値だけの行にはしない
        assert "資料作成集中" not in text          # 勤務状況を持ち込まない
        assert "2026年9月1日 足立 海里さんの発言" in text
